"""
最简 Anthropic API mock server, 用于拦截 CC 发出的 /v1/messages 请求并落盘.

监听 0.0.0.0:18888. 把每个 POST 请求的 body 完整写入 /tmp/captured.jsonl,
然后返回一个伪 SSE stream 让 CC 顺利结束 (message_start -> message_stop).
"""
import http.server
import json
import pathlib
import time
import sys

CAPTURE = pathlib.Path("/tmp/captured.jsonl")

# 最小 Anthropic SSE 响应: message_start -> content_block_start -> message_stop
def build_sse() -> bytes:
    events = []
    events.append(("message_start", {
        "type": "message_start",
        "message": {
            "id": "msg_mock",
            "type": "message",
            "role": "assistant",
            "model": "claude-mock",
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 1}
        }
    }))
    events.append(("content_block_start", {
        "type": "content_block_start",
        "index": 0,
        "content_block": {"type": "text", "text": ""}
    }))
    events.append(("content_block_delta", {
        "type": "content_block_delta",
        "index": 0,
        "delta": {"type": "text_delta", "text": "ok"}
    }))
    events.append(("content_block_stop", {"type": "content_block_stop", "index": 0}))
    events.append(("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": "end_turn", "stop_sequence": None},
        "usage": {"output_tokens": 1}
    }))
    events.append(("message_stop", {"type": "message_stop"}))

    out = []
    for ev, body in events:
        out.append(f"event: {ev}\n".encode())
        out.append(f"data: {json.dumps(body)}\n\n".encode())
    return b"".join(out)


class Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(n)
        record = {
            "ts": time.time(),
            "path": self.path,
            "headers": {k: v for k, v in self.headers.items()},
            "body": body.decode("utf-8", errors="replace"),
        }
        with CAPTURE.open("a") as fp:
            fp.write(json.dumps(record, ensure_ascii=False) + "\n")
        print(f"[{time.strftime('%H:%M:%S')}] POST {self.path}  body={len(body)}B", flush=True)

        sse = build_sse()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(sse)

    def do_GET(self):
        # CC 可能会先 GET 一些 health/version
        self.send_response(404)
        self.end_headers()

    def log_message(self, format, *args):
        pass  # 安静


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 18888
    CAPTURE.write_text("")
    print(f"fake anthropic on 0.0.0.0:{port}, capture -> {CAPTURE}", flush=True)
    http.server.HTTPServer(("0.0.0.0", port), Handler).serve_forever()
