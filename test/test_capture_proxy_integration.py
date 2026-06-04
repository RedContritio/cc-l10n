"""capture_proxy ProxyHandler 转发主路径集成测试 (真 HTTP, 用 fake_anthropic 作 upstream).

评审指出: 纯函数测试不覆盖 ProxyHandler._handle/_record 主路径 (转发 / 脱敏落盘 / 502)。
本文件补真转发集成测试: 起 fake_anthropic 作 upstream + 起 ProxyHandler 反代, 经真
HTTP 发请求, 断言 SSE 完整转发 / api-key 落盘脱敏 / 上游不可达回 502。
不触真 API (upstream 是本地 mock), 全程 127.0.0.1。
"""
import http.client
import http.server
import json
import os
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "test"))

from capture_proxy import ProxyHandler  # noqa: E402
from fake_anthropic import Handler as FakeUpstream  # noqa: E402


def _start(handler_cls):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def _post(port, path, body, headers):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request("POST", path, body=body, headers=headers)
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


def _configure_proxy(host, port, log_path):
    ProxyHandler.upstream_scheme = "http"
    ProxyHandler.upstream_host = host
    ProxyHandler.upstream_port = port
    ProxyHandler.log_path = Path(log_path)
    ProxyHandler.max_resp_bytes = 4 * 1024 * 1024


def test_proxy_forwards_sse_and_logs_redacted():
    upstream, up_port = _start(FakeUpstream)
    fd, logname = tempfile.mkstemp(suffix=".jsonl")
    os.close(fd)
    try:
        _configure_proxy("127.0.0.1", up_port, logname)
        proxy, px_port = _start(ProxyHandler)
        try:
            body = json.dumps({"model": "claude-mock",
                               "messages": [{"role": "user", "content": "hi"}]})
            status, data = _post(px_port, "/v1/messages", body,
                                 {"Content-Type": "application/json",
                                  "x-api-key": "sk-secret-ABC"})
        finally:
            proxy.shutdown()
        # 1) SSE 完整转发 (message_start ... message_stop 原样到达客户端)
        assert status == 200, status
        assert b"message_start" in data and b"message_stop" in data
        # 2) 落盘 + 头部脱敏 (api-key 不得明文落盘)
        lines = Path(logname).read_text().strip().splitlines()
        assert len(lines) == 1, f"expected 1 log record, got {len(lines)}"
        rec = json.loads(lines[0])
        hdrs = json.dumps(rec["req_headers"], ensure_ascii=False)
        assert "***REDACTED***" in hdrs
        assert "sk-secret-ABC" not in hdrs
        assert rec["status"] == 200
        assert rec["sse_summary"]["stop_reason"] == "end_turn"
    finally:
        upstream.shutdown()
        os.unlink(logname)


def test_proxy_returns_502_on_unreachable_upstream():
    fd, logname = tempfile.mkstemp(suffix=".jsonl")
    os.close(fd)
    try:
        _configure_proxy("127.0.0.1", 1, logname)  # 端口 1: 无服务, 连接被拒
        proxy, px_port = _start(ProxyHandler)
        try:
            status, data = _post(px_port, "/v1/messages", "{}",
                                 {"Content-Type": "application/json"})
        finally:
            proxy.shutdown()
        assert status == 502, status
        assert b"upstream error" in data
    finally:
        os.unlink(logname)


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        fn()
        passed += 1
        print(f"  ok  {fn.__name__}")
    print(f"\n[OK] {passed}/{len(fns)} 通过")


if __name__ == "__main__":
    _run_all()
