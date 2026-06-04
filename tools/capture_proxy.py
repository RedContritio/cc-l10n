"""
透明日志反向代理: 夹在 Claude Code 与 Anthropic API 之间, 双向落盘.

用途
  诊断 "The model's tool call could not be parsed (retry also failed)." 这类偶发错误.
  该错误在 CC 内部触发条件是: 响应 stop_reason=="tool_use" 但抽取到的有效 tool_use
  块数为 0 (名字对不上 / input 不是合法 JSON / 不过 schema). 这一层只有在 API 边界
  抓原始字节才看得到 —— CC 自己的 transcript 记录的是 parse 之后的结果, 失败的块不会
  被如实落盘.

机制
  - CC 通过 ANTHROPIC_BASE_URL=http://127.0.0.1:<port> 把流量打到本代理 (明文, 免 CA).
  - 本代理用真实 TLS 转发到 api.anthropic.com (默认校验证书), 把响应原样流式回传给 CC.
  - 每个 /v1/messages 往返完整落盘: 请求体 (system/tools/messages) + 响应 (原始 SSE).
  - 在线检测两种畸形信号, 命中即在控制台打醒目标记:
      (1) 请求 messages 里含 CC 注入的重试串 "...malformed and could not be parsed..."
          => 上一轮 assistant tool_use 被判畸形 (CC 自己的 ground-truth 判定)
      (2) 响应 stop_reason=="tool_use" 但 tool_use 块缺失 / input JSON 解析失败

落盘格式: 每行一条 JSON, 字段见 _record. 仅请求头脱敏 (x-api-key / authorization);
  req_body (system / tools / messages 全文) 与 resp_sse_raw (完整模型输出) 不脱敏、原样落盘,
  含完整会话内容, 高度敏感 —— 故 .capture/ 必须 gitignore, 切勿提交或外传.

用法
  python3 tools/capture_proxy.py [--port 18889] [--upstream https://api.anthropic.com]
                                 [--log <path>] [--max-resp-bytes N]
  然后另开一个 shell:
  ANTHROPIC_BASE_URL=http://127.0.0.1:18889 claude

  本地自检 (不接真 API, 配合 test/fake_anthropic.py):
  python3 test/fake_anthropic.py 18888 &
  python3 tools/capture_proxy.py --port 18889 --upstream http://127.0.0.1:18888
"""
from __future__ import annotations

import argparse
import http.client
import http.server
import json
import socket
import ssl
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

REPO_ROOT = Path(__file__).resolve().parent.parent

# hop-by-hop 头不转发 (RFC 7230 §6.1)
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade",
}
# 请求方向额外跳过 (由代理自行设定)
SKIP_REQ = HOP_BY_HOP | {"host", "content-length", "accept-encoding"}
# 响应方向额外跳过 (代理重新分帧为 chunked)
SKIP_RESP = HOP_BY_HOP | {"content-length"}
REDACT = {"x-api-key", "authorization", "proxy-authorization"}

RETRY_MARKER = "Your tool call was malformed and could not be parsed. Please retry."

_log_lock = threading.Lock()


def _redact_headers(headers) -> dict:
    out = {}
    for k, v in headers.items():
        out[k] = "***REDACTED***" if k.lower() in REDACT else v
    return out


def _message_is_retry_marker(m) -> bool:
    """该 user message 的内容是否【就是】CC 注入的重试串本身.

    用完全等值 (而非子串) 匹配, 排除用户/assistant 仅仅引用或讨论该串的误报 ——
    与 parse_monitor detect 的结构谓词同思路。content 可能是 str 或 block 列表
    (text / tool_result)。"""
    c = m.get("content", "")
    if isinstance(c, str):
        return c.strip() == RETRY_MARKER
    if isinstance(c, list):
        for b in c:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text" and (b.get("text") or "").strip() == RETRY_MARKER:
                return True
            if b.get("type") == "tool_result":
                tc = b.get("content", "")
                if isinstance(tc, str) and tc.strip() == RETRY_MARKER:
                    return True
    return False


def detect_retry_in_request(body_obj) -> dict | None:
    """请求 messages 里若 CC 注入了重试串 (role=user 且内容完全等于该串),
    返回 {retry_index, prev_assistant}; 否则 None.

    完全等值匹配排除"对话里引用/讨论该串"的误报 (此前子串匹配会对用户提问里
    引用该串误报; 仅影响日志标记不影响转发, 但仍应消除)。"""
    if not isinstance(body_obj, dict):
        return None
    messages = body_obj.get("messages")
    if not isinstance(messages, list):
        return None
    for i, m in enumerate(messages):
        if not isinstance(m, dict) or m.get("role") != "user":
            continue
        if not _message_is_retry_marker(m):
            continue
        prev_assistant = None
        for j in range(i - 1, -1, -1):
            if messages[j].get("role") == "assistant":
                prev_assistant = messages[j]
                break
        return {"retry_index": i, "prev_assistant": prev_assistant}
    return None


def parse_sse(raw: bytes) -> dict:
    """从原始 SSE 字节里抽 stop_reason 与 tool_use 块. 解析失败返回 best-effort."""
    stop_reason = None
    blocks: dict[int, dict] = {}  # index -> {type, name, id, json_buf}
    saw_event = False
    for line in raw.split(b"\n"):
        line = line.strip()
        if not line.startswith(b"data:"):
            continue
        payload = line[len(b"data:"):].strip()
        if not payload or payload == b"[DONE]":
            continue
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            continue
        saw_event = True
        t = obj.get("type")
        if t == "content_block_start":
            idx = obj.get("index", 0)
            cb = obj.get("content_block", {}) or {}
            blocks[idx] = {"type": cb.get("type"), "name": cb.get("name"),
                           "id": cb.get("id"), "json_buf": ""}
        elif t == "content_block_delta":
            idx = obj.get("index", 0)
            d = obj.get("delta", {}) or {}
            if d.get("type") == "input_json_delta":
                blocks.setdefault(idx, {"type": "tool_use", "json_buf": ""})
                blocks[idx]["json_buf"] = blocks[idx].get("json_buf", "") + d.get("partial_json", "")
        elif t == "message_delta":
            sr = (obj.get("delta") or {}).get("stop_reason")
            if sr is not None:
                stop_reason = sr
        elif t == "message_start":
            sr = ((obj.get("message") or {}).get("stop_reason"))
            if sr is not None:
                stop_reason = sr

    tool_uses = []
    for idx in sorted(blocks):
        b = blocks[idx]
        if b.get("type") != "tool_use":
            continue
        buf = b.get("json_buf", "")
        ok, parsed = True, None
        if buf == "":
            parsed = {}  # 无 input 的工具 (input 为空对象) 合法
        else:
            try:
                parsed = json.loads(buf)
            except json.JSONDecodeError:
                ok = False
        tool_uses.append({"index": idx, "name": b.get("name"), "id": b.get("id"),
                          "input_raw": buf, "input_ok": ok, "input": parsed})

    malformed = False
    reason = None
    if stop_reason == "tool_use":
        if not tool_uses:
            malformed, reason = True, "stop_reason=tool_use 但响应里没有任何 tool_use 块"
        elif any(not tu["input_ok"] for tu in tool_uses):
            malformed, reason = True, "tool_use.input 不是合法 JSON"
    return {"stop_reason": stop_reason, "tool_uses": tool_uses,
            "saw_event": saw_event, "malformed": malformed, "reason": reason}


class ProxyHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # 由 main 注入
    upstream_scheme = "https"
    upstream_host = "api.anthropic.com"
    upstream_port = 443
    log_path: Path = None  # type: ignore
    max_resp_bytes = 4 * 1024 * 1024

    def log_message(self, fmt, *args):
        pass  # 关掉默认 access log, 用我们自己的

    def _handle(self):
        n = int(self.headers.get("Content-Length", "0") or "0")
        req_body = self.rfile.read(n) if n else b""

        # 转发头
        fwd_headers = {k: v for k, v in self.headers.items() if k.lower() not in SKIP_REQ}
        fwd_headers["Accept-Encoding"] = "identity"  # 避免 gzip, 方便落 SSE 原文

        try:
            if self.upstream_scheme == "https":
                ctx = ssl.create_default_context()
                conn = http.client.HTTPSConnection(self.upstream_host, self.upstream_port,
                                                   timeout=1200, context=ctx)
            else:
                conn = http.client.HTTPConnection(self.upstream_host, self.upstream_port,
                                                  timeout=1200)
            conn.request(self.command, self.path, body=req_body, headers=fwd_headers)
            resp = conn.getresponse()
        except Exception as e:  # noqa: BLE001 — 上游不可达, 回 502 给 CC
            msg = f"capture_proxy upstream error: {e}".encode()
            self.send_response(502)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)
            return

        # 回传头 (重新分帧为 chunked)
        self.send_response(resp.status)
        for k, v in resp.getheaders():
            if k.lower() in SKIP_RESP:
                continue
            self.send_header(k, v)
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        # 流式回传 + tee
        captured = bytearray()
        truncated = False
        try:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                self.wfile.write(b"%X\r\n" % len(chunk))
                self.wfile.write(chunk)
                self.wfile.write(b"\r\n")
                self.wfile.flush()
                if len(captured) < self.max_resp_bytes:
                    captured.extend(chunk[: self.max_resp_bytes - len(captured)])
                else:
                    truncated = True
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass  # CC 提前断开
        finally:
            conn.close()

        self._record(req_body, resp, bytes(captured), truncated)

    do_POST = _handle
    do_GET = _handle
    do_PUT = _handle
    do_DELETE = _handle
    do_PATCH = _handle

    def _record(self, req_body: bytes, resp, resp_bytes: bytes, truncated: bool):
        try:
            req_obj = json.loads(req_body) if req_body else None
        except json.JSONDecodeError:
            req_obj = None

        ctype = resp.getheader("content-type", "") or ""
        is_sse = "text/event-stream" in ctype
        sse = parse_sse(resp_bytes) if is_sse else None
        retry = detect_retry_in_request(req_obj)

        rec = {
            "ts": time.time(),
            "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "method": self.command,
            "path": self.path,
            "status": resp.status,
            "req_headers": _redact_headers(self.headers),
            "req_body": req_obj if req_obj is not None
                        else req_body.decode("utf-8", errors="replace"),
            "resp_content_type": ctype,
            "resp_truncated": truncated,
        }
        if is_sse:
            rec["resp_sse_raw"] = resp_bytes.decode("utf-8", errors="replace")
            rec["sse_summary"] = sse
        else:
            rec["resp_body"] = resp_bytes.decode("utf-8", errors="replace")

        flags = []
        if retry is not None:
            rec["FLAG_retry_after_malformed"] = retry
            flags.append("RETRY-AFTER-MALFORMED")
        if sse and sse["malformed"]:
            rec["FLAG_malformed_response"] = {"reason": sse["reason"],
                                              "tool_uses": sse["tool_uses"]}
            flags.append("MALFORMED-RESPONSE")

        with _log_lock:
            with self.log_path.open("a") as fp:
                fp.write(json.dumps(rec, ensure_ascii=False) + "\n")

        stop = sse["stop_reason"] if sse else "-"
        base = (f"[{rec['ts_iso']}] {self.command} {self.path}  "
                f"status={resp.status} stop={stop} "
                f"req={len(req_body)}B resp={len(resp_bytes)}B")
        if flags:
            print(f"\n{'!' * 70}\n  ⚠  {'  '.join(flags)}  ⚠\n  {base}", flush=True)
            if "MALFORMED-RESPONSE" in flags:
                print(f"  原因: {sse['reason']}", flush=True)
                for tu in sse["tool_uses"]:
                    print(f"    tool_use name={tu['name']!r} input_ok={tu['input_ok']} "
                          f"input_raw={tu['input_raw'][:200]!r}", flush=True)
            print(f"{'!' * 70}\n", flush=True)
        else:
            print(base, flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=18889, help="本代理监听端口")
    ap.add_argument("--upstream", default="https://api.anthropic.com",
                    help="上游 base URL (默认 https://api.anthropic.com)")
    ap.add_argument("--log", default=str(REPO_ROOT / ".capture" / "capture.jsonl"),
                    help="落盘 jsonl 路径")
    ap.add_argument("--max-resp-bytes", type=int, default=4 * 1024 * 1024,
                    help="单条响应最多落盘字节数 (仍全量转发, 仅截断日志)")
    args = ap.parse_args()

    up = urlsplit(args.upstream)
    ProxyHandler.upstream_scheme = up.scheme
    ProxyHandler.upstream_host = up.hostname
    ProxyHandler.upstream_port = up.port or (443 if up.scheme == "https" else 80)
    ProxyHandler.max_resp_bytes = args.max_resp_bytes

    log_path = Path(args.log)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    ProxyHandler.log_path = log_path

    class Server(http.server.ThreadingHTTPServer):
        daemon_threads = True
        allow_reuse_address = True

    httpd = Server(("127.0.0.1", args.port), ProxyHandler)
    print(f"capture_proxy 监听 http://127.0.0.1:{args.port}", flush=True)
    print(f"  上游   : {args.upstream}", flush=True)
    print(f"  落盘   : {log_path}", flush=True)
    print(f"  启动 CC: ANTHROPIC_BASE_URL=http://127.0.0.1:{args.port} claude", flush=True)
    print(f"  (畸形 tool_use 会在此打醒目标记; Ctrl-C 退出)\n", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n收到 Ctrl-C, 退出.", flush=True)
        httpd.shutdown()


if __name__ == "__main__":
    main()
