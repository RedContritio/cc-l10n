"""
Token 级 strict 覆盖率验证: 对已 patch 的 binary 或捕获的 system prompt 做 100% 检查.

两种模式 (均为 token 级 strict, 不接受任何宽容阈值):
  1. STATIC 模式 (--binary): 调用 extract_prompt_segments, 检查 score>=5 字面量
     是否全部翻或入 whitelist. 不依赖运行时 capture, 是 strict pipeline 主关.
  2. CAPTURE 模式 (--captured): 读取 fake_anthropic 落盘的 jsonl, 提取实际发往
     LLM 的 system prompt 文本, token 级减去 whitelist (literal_exact + exact + regex)
     后, 剩余任何 3+ 字母英文 token 都报为残留. 不切段, 不豁免 "段含中文" 的段落.

用法:
  python3 verify_coverage.py --binary <patched-binary> [--whitelist ...] [--translations ...]
  python3 verify_coverage.py --captured <captured.jsonl> [--whitelist ...]

退出码:
  0  无残留
  1  STATIC 模式: untranslated_prompt > 0
  2  CAPTURE 模式: prompt 中残留英文 token (不在 whitelist)
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from extract_prompt_segments import load_whitelist
from log_setup import setup_logging

log = setup_logging(__name__)


def _walk_schema_descriptions(node, path: str):
    """递归 yield (label, text): JSON Schema 树中所有非空 description 字符串字段.

    input_schema 里的 description 同样发给模型 (字段级说明), 不限于 properties
    一层 —— 递归 DFS 覆盖 items / nested object / oneOf 等任意深度, 避免漏扫。
    """
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "description" and isinstance(v, str):
                if v.strip():
                    yield (f"{path}.description", v)
            else:
                yield from _walk_schema_descriptions(v, f"{path}.{k}")
    elif isinstance(node, list):
        for idx, item in enumerate(node):
            yield from _walk_schema_descriptions(item, f"{path}[{idx}]")


def extract_all_prompts(jsonl_path: Path) -> list[tuple[str, str]]:
    """提取实际发往 LLM 的全部英文承载文本: system[] + tools[] 描述 + input_schema descriptions.

    tools[].description 与 input_schema 内的 description 字段是常驻工具集的一部分,
    随每个请求发给模型 —— ground-truth 必须覆盖它们, 不受 binary 内如何存储/切碎/拼装影响。
    """
    out = []
    for line_no, line in enumerate(jsonl_path.read_text().splitlines(), 1):
        rec = json.loads(line)
        try:
            body = json.loads(rec["body"])
        except (KeyError, json.JSONDecodeError):
            continue
        for i, s in enumerate(body.get("system", [])):
            t = s.get("text", "")
            if t:
                out.append((f"rec{line_no}.sys[{i}]", t))
        tools = body.get("tools", [])
        if not isinstance(tools, list):
            tools = []
        for ti, tool in enumerate(tools):
            if not isinstance(tool, dict):
                continue
            name = tool.get("name", "?")
            # 标签含工具下标 ti 保证唯一 (同名工具不会 label 碰撞导致 dict 丢值)
            base = f"rec{line_no}.tool[{ti}:{name}]"
            desc = tool.get("description", "")
            if isinstance(desc, str) and desc.strip():
                out.append((f"{base}.description", desc))
            schema = tool.get("input_schema")
            if isinstance(schema, dict):
                out.extend(_walk_schema_descriptions(schema, f"{base}.input_schema"))
    return out


def find_residual_tokens(text: str, wl: dict) -> list[str]:
    """
    Token 级 strict: 返回 text 中所有不在 whitelist 内的 3+ 字母英文 token.

    不切段, 不做 "段含中文 = OK" 豁免 - 用户 feedback 明确要求:
    任一英文 token 要么被翻译 (从 prompt 文本中替换为中文, 此处看不到), 要么在
    whitelist 中. 不接受密度/比例/段豁免阈值.

    实现: 全文先减去 whitelist literal_exact / exact / regex 匹配, 再提取剩余
    3+ 字母英文 token. 用空格替换 (而非空串) 避免 "Claude Code" -> 减去
    "Claude" 后变 " Code" 仍触发, 减成空格后保留 token 边界.
    """
    if not text.strip():
        return []

    s = text
    # 减去 literal_exact (整段完全匹配; 长度倒序以最长优先)
    for item in sorted(wl.get("literal_exact", ()), key=len, reverse=True):
        if item and item in s:
            s = s.replace(item, " ")
    # 减去 exact (子串匹配; 长度倒序避免短词截断长词)
    for item in sorted(wl.get("exact", []), key=len, reverse=True):
        if not item:
            continue
        s = s.replace(item, " ")
    # 减去 regex
    for pat in wl.get("regex", []):
        s = pat.sub(" ", s)

    return re.findall(r"[A-Za-z]{3,}", s)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    src_group = ap.add_mutually_exclusive_group(required=True)
    src_group.add_argument("--binary", help="STATIC 模式: 已 patch 的 CC binary 或 cli.js")
    src_group.add_argument("--captured", help="CAPTURE 模式: fake_anthropic 落盘的 jsonl")
    ap.add_argument("--translations", default=str(SCRIPT_DIR.parent / "data" / "translations"),
                    help="translations 目录 (默认: data/translations)")
    ap.add_argument("--whitelist", default=str(SCRIPT_DIR.parent / "data" / "whitelist.json"),
                    help="whitelist.json")
    ap.add_argument("--json", default=None, help="JSON 报告输出路径")
    ap.add_argument("--show-residual", type=int, default=20,
                    help="CAPTURE 模式: 打印前 N 段残留 (默认 20)")
    args = ap.parse_args()

    wl = load_whitelist(Path(args.whitelist))

    if args.binary:
        import subprocess
        tmp_report = Path("/tmp") / f"verify-static-{Path(args.binary).name}.json"
        result = subprocess.run([
            sys.executable, str(SCRIPT_DIR / "extract_prompt_segments.py"),
            args.binary,
            "--translations", args.translations,
            "--whitelist", args.whitelist,
            "--out", str(tmp_report),
            "--show", "0",
        ], capture_output=True, text=True)
        if result.stderr:
            log.info(result.stderr.rstrip("\n"))
        report = json.loads(tmp_report.read_text())
        s = report["summary"]
        log.info(f"\n=== STATIC strict 验证 ===")
        log.info(f"  prompt 候选 (score>=5): {s['scored_prompt']}")
        log.info(f"  已翻译                  : {s['translated']}")
        log.info(f"  白名单覆盖              : {s['whitelisted']}")
        log.info(f"  untranslated_prompt    : {s['untranslated_prompt']}")

        # Stale whitelist 检测: 扫 whitelist.literal_exact, 对每条 src 检查是否
        # 在 cli.js 字面量中存在 (raw bytes / escape-aware / verify text 严格相等).
        # 找不到任何匹配的 src 即为 stale (CC 升级后 cli.js 已不含该字面量).
        sys.path.insert(0, str(SCRIPT_DIR))
        from bun_format import extract_cli_js
        from repack_universal import find_src_with_escape
        cli = extract_cli_js(Path(args.binary))

        candidate_texts = {c["text"].strip() for c in report.get("candidates", [])}
        stale_srcs = []
        for src in wl.get("literal_exact", ()):
            if not src or src.startswith("_comment"):
                continue
            stripped = src.strip()
            if stripped in candidate_texts:
                continue
            if cli.find(src.encode("utf-8")) >= 0:
                continue
            hits = find_src_with_escape(cli, src.encode("utf-8"))
            if any(h[2] != "raw" for h in hits):
                continue
            stale_srcs.append(src)

        if stale_srcs:
            log.info(f"  stale whitelist        : {len(stale_srcs)} 条 (cli.js 已不含)")
            for src in sorted(stale_srcs, key=len)[:5]:
                head = src[:60].replace("\n", "\\n")
                log.info(f"      ({len(src):5d}B) {head!r}")
            if len(stale_srcs) > 5:
                log.info(f"      ... ({len(stale_srcs) - 5} more)")

        if args.json:
            Path(args.json).write_text(json.dumps({
                "mode": "static",
                "binary": args.binary,
                "summary": s,
                "stale_whitelist": stale_srcs,
            }, ensure_ascii=False, indent=2))
            log.info(f"\nJSON 报告: {args.json}")

        # 覆盖率门: untranslated_prompt=0 (此 binary 每个 prompt 都翻或 whitelist)。
        # stale_whitelist 此处仅信息提示: whitelist 是跨版本/平台并集, 某条不在【这个】
        # binary 很正常 (它为别的版本/平台 whitelist 的)。真正的 "全平台全版本都没有"
        # 才是死 whitelist, 由 integration_test 跨 (版本×平台) 聚合判定。
        if stale_srcs:
            log.info(f"  (stale-vs-this-binary whitelist: {len(stale_srcs)} 条, "
                     f"非失败; 真 stale 由聚合判)")
        if s["untranslated_prompt"] > 0:
            log.error(f"\n[FAIL] STATIC strict 失败: {s['untranslated_prompt']} 条 prompt 候选未覆盖. "
                      f"详细 → {tmp_report}")
            sys.exit(1)
        log.info(f"\n[OK] STATIC strict pass")
        return

    # CAPTURE 模式
    prompts = extract_all_prompts(Path(args.captured))
    log.info(f"=== CAPTURE strict 验证 ===")
    log.info(f"提取 {len(prompts)} 段 system prompt")

    all_residual = []
    by_label = {}
    for label, text in prompts:
        residuals = find_residual_tokens(text, wl)
        if residuals:
            by_label[label] = sorted(set(residuals))
            all_residual.extend(residuals)

    log.info(f"\n=== 摘要 ===")
    log.info(f"  prompt 段数  : {len(prompts)}")
    log.info(f"  含残留段数  : {len(by_label)}")
    log.info(f"  残留 token 总数 : {len(all_residual)}")
    log.info(f"  唯一残留 token : {len(set(all_residual))}")

    if by_label:
        log.info(f"\n=== 含残留段 (前 {args.show_residual}) ===")
        for label, tokens in list(by_label.items())[:args.show_residual]:
            log.info(f"  {label}: {tokens[:20]}")

    if args.json:
        Path(args.json).write_text(json.dumps({
            "mode": "capture",
            "captured": args.captured,
            "total_prompts": len(prompts),
            "prompts_with_residual": len(by_label),
            "total_residual_tokens": len(all_residual),
            "unique_residual_tokens": sorted(set(all_residual)),
            "by_label": by_label,
        }, ensure_ascii=False, indent=2))
        log.info(f"\nJSON 报告: {args.json}")

    if all_residual:
        log.error(f"\n[FAIL] CAPTURE strict 失败: {len(set(all_residual))} 个未翻译/未 whitelist 的英文 token")
        sys.exit(2)
    log.info(f"\n[OK] CAPTURE strict pass")


if __name__ == "__main__":
    main()
