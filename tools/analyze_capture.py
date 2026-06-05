"""
分析 capture_proxy 落盘的 jsonl, 定位 "tool call could not be parsed" 的根因.

对每个畸形轮次, 用【同一请求】里的 tools[].input_schema 反查 model 吐出的 tool_use
为什么不被接受 (工具名对不上 / input 不是合法 JSON / 缺 required / 多余字段 / 类型不符).

用法
  python3 tools/analyze_capture.py [--log <path>] [--full]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LOG = REPO_ROOT / ".capture" / "capture.jsonl"


def load(path: Path):
    recs = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            recs.append(json.loads(line))
    return recs


def tool_schema_map(req_body) -> dict:
    out = {}
    if isinstance(req_body, dict):
        for t in req_body.get("tools", []) or []:
            if isinstance(t, dict) and "name" in t:
                out[t["name"]] = t.get("input_schema", {}) or {}
    return out


def check_against_schema(name, inp, schema) -> list[str]:
    """轻量 schema 校验, 返回不符项列表 (非完整 JSON Schema 实现, 抓常见违例)."""
    issues = []
    if not isinstance(schema, dict):
        return ["该请求的 tools 里找不到此工具的 input_schema"]
    props = schema.get("properties", {}) or {}
    required = schema.get("required", []) or []
    if not isinstance(inp, dict):
        return [f"input 不是对象 (type={type(inp).__name__})"]
    for r in required:
        if r not in inp:
            issues.append(f"缺 required 字段: {r!r}")
    addl = schema.get("additionalProperties", True)
    for k in inp:
        if k not in props and addl is False:
            issues.append(f"多余字段: {k!r} (schema 禁止 additionalProperties)")
    _JSON_T = {"string": str, "number": (int, float), "integer": int,
               "boolean": bool, "array": list, "object": dict}
    for k, v in inp.items():
        spec = props.get(k)
        if not isinstance(spec, dict):
            continue
        jt = spec.get("type")
        if isinstance(jt, str) and jt in _JSON_T and not isinstance(v, _JSON_T[jt]):
            issues.append(f"字段 {k!r} 类型应为 {jt}, 实际 {type(v).__name__}")
        enum = spec.get("enum")
        if isinstance(enum, list) and v not in enum:
            issues.append(f"字段 {k!r} 值 {v!r} 不在 enum {enum}")
    return issues


def report_record(rec, full: bool):
    print("=" * 78)
    print(f"{rec.get('ts_iso','?')}  {rec.get('method')} {rec.get('path')}  status={rec.get('status')}")
    schemas = tool_schema_map(rec.get("req_body"))
    print(f"  请求声明工具数: {len(schemas)}  ({', '.join(sorted(schemas)) [:200]})")

    fr = rec.get("FLAG_malformed_response")
    if fr:
        print(f"\n  [响应畸形] {fr.get('reason')}")
        for tu in fr.get("tool_uses", []):
            name = tu.get("name")
            print(f"    - tool_use name={name!r} input_ok={tu.get('input_ok')}")
            print(f"      input_raw: {tu.get('input_raw','')[:500]}")
            if tu.get("input_ok") and name in schemas:
                issues = check_against_schema(name, tu.get("input"), schemas[name])
                if issues:
                    print(f"      schema 不符:")
                    for it in issues:
                        print(f"        · {it}")
                else:
                    print(f"      (input 合法 JSON 且过本地 schema 检查 —— 失败更可能是工具名/未知字段/CC 侧严格校验)")
            elif name and name not in schemas:
                print(f"      ⚠ 工具名 {name!r} 不在该请求声明的 tools 里 → CC 必丢弃")

    rr = rec.get("FLAG_retry_after_malformed")
    if rr:
        prev = rr.get("prev_assistant")
        print(f"\n  [重试标记] 该请求是上一轮畸形 tool_use 之后的重试")
        if prev:
            content = prev.get("content")
            blocks = content if isinstance(content, list) else [{"type": "text", "text": content}]
            for b in blocks:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    nm = b.get("name")
                    print(f"    上一轮 assistant tool_use: name={nm!r} input={json.dumps(b.get('input'), ensure_ascii=False)[:400]}")
                    if nm in schemas:
                        issues = check_against_schema(nm, b.get("input"), schemas[nm])
                        for it in issues:
                            print(f"        · {it}")
                    elif nm:
                        print(f"        ⚠ 工具名 {nm!r} 不在 tools 里")
                elif isinstance(b, dict) and b.get("type") == "text":
                    txt = (b.get("text") or "")[:300]
                    if txt.strip():
                        print(f"    上一轮 assistant text: {txt!r}")
        else:
            print(f"    (历史里未保留可解析的上一轮 assistant turn —— 看 resp_sse_raw 原文)")

    if full:
        print(f"\n  --- resp_sse_raw (前 2000 字符) ---")
        print("  " + (rec.get("resp_sse_raw", rec.get("resp_body", "")) or "")[:2000].replace("\n", "\n  "))
    print()


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--log", default=str(DEFAULT_LOG), help="capture jsonl 路径")
    ap.add_argument("--full", action="store_true", help="打印 SSE 原文片段")
    args = ap.parse_args()

    path = Path(args.log)
    if not path.exists():
        print(f"[空] 日志不存在: {path}")
        return
    recs = load(path)

    n_total = len(recs)
    n_msgs = sum(1 for r in recs if r.get("path", "").endswith("/v1/messages"))
    flagged = [r for r in recs if r.get("FLAG_malformed_response") or r.get("FLAG_retry_after_malformed")]
    by_stop = {}
    for r in recs:
        s = (r.get("sse_summary") or {}).get("stop_reason")
        if s:
            by_stop[s] = by_stop.get(s, 0) + 1

    print(f"日志: {path}")
    print(f"总记录: {n_total}  其中 /v1/messages: {n_msgs}")
    print(f"stop_reason 分布: {by_stop}")
    print(f"畸形标记命中: {len(flagged)} 条\n")

    for r in flagged:
        report_record(r, args.full)

    if not flagged:
        print("(暂无畸形命中 —— 继续挂着 capture_proxy 等偶发复现)")


if __name__ == "__main__":
    main()
