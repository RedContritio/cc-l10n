"""#2 ground-truth 门 driver harness.

驱动 active CC binary 发一个携带【完整常驻工具集】的请求, 经 fake_anthropic 落盘, 再对
system[] + tools[].description + input_schema descriptions 跑 CAPTURE strict 验证。

CAPTURE 模式度量【真正发给模型的英文】, 不受 binary 内如何存储/切碎/拼装影响 ——
是 STATIC (字面量评分) 之外的独立 ground-truth。tools[] 描述随每个请求发送, 故任意一次
真实 claude 调用都携带完整工具集。

流程:
  1. 启动 test/fake_anthropic.py (离线 mock, 落盘 request body 到 /tmp/captured.jsonl)
  2. 运行 driver 命令 (默认 `claude -p <prompt>`, 经 ANTHROPIC_BASE_URL 打到 mock)
     —— driver 即 "驱动 binary 发请求" 的那一步; 用 --driver 可覆盖 (测试/自定义)
  3. 停 mock, 对 captured.jsonl 跑 verify_coverage --captured

用法:
  python3 capture_verify.py                         # 驱动 active claude
  python3 capture_verify.py --prompt "ping" --port 18888
  python3 capture_verify.py --driver "curl -s ..."  # 自定义 driver (测试用)

退出码: 透传 verify_coverage (0 无残留 / 2 有残留)。
"""
from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from log_setup import setup_logging

log = setup_logging(__name__)

ROOT = SCRIPT_DIR.parent
FAKE = ROOT / "test" / "fake_anthropic.py"
CAPTURE = Path("/tmp/captured.jsonl")  # fake_anthropic 硬编码落盘路径


def wait_ready(port: int, timeout: float = 8.0) -> bool:
    """轮询 mock 端口可达 (POST 会被 do_POST 落盘; 这里只探活)."""
    deadline = time.time() + timeout
    url = f"http://127.0.0.1:{port}/healthz"
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=0.5)
            return True  # 任何 2xx/3xx 响应 = server 已起
        except urllib.error.HTTPError:
            return True  # 4xx/5xx 同样是 server 已起 (mock do_GET 返回 404)
        except Exception:
            time.sleep(0.2)
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=18888)
    ap.add_argument("--prompt", default="ping",
                    help="默认 driver (claude) 的 prompt")
    ap.add_argument("--driver", default=None,
                    help="覆盖 driver 命令 (shell 字符串)。默认 `claude -p <prompt>`")
    ap.add_argument("--whitelist", default=str(ROOT / "data" / "whitelist.json"))
    ap.add_argument("--json", default=None, help="verify JSON 报告输出路径")
    ap.add_argument("--keep-server-on-fail", action="store_true")
    args = ap.parse_args()

    env = dict(os.environ)
    env["ANTHROPIC_BASE_URL"] = f"http://127.0.0.1:{args.port}"
    env.setdefault("ANTHROPIC_API_KEY", "mock-key-for-capture")

    if args.driver:
        driver_cmd = shlex.split(args.driver)
    else:
        driver_cmd = ["claude", "-p", args.prompt]

    log.info(f"[INFO] 启动 fake_anthropic :{args.port} (capture -> {CAPTURE})")
    server = subprocess.Popen([sys.executable, str(FAKE), str(args.port)],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        if not wait_ready(args.port):
            log.error("[FAIL] fake_anthropic 未就绪")
            return 3
        log.info(f"[INFO] driver: {' '.join(driver_cmd)}")
        try:
            subprocess.run(driver_cmd, env=env, timeout=120,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except FileNotFoundError:
            log.error(f"[FAIL] driver 命令未找到: {driver_cmd[0]} "
                      f"(active claude 是否在 PATH? 或用 --driver 覆盖)")
            return 4
        except subprocess.TimeoutExpired:
            log.warning("[WARN] driver 超时 (请求或已落盘, 继续验证)")
    finally:
        server.terminate()
        try:
            server.wait(timeout=3)
        except subprocess.TimeoutExpired:
            server.kill()

    if not CAPTURE.exists() or not CAPTURE.read_text().strip():
        log.error(f"[FAIL] 无捕获数据 ({CAPTURE} 空) —— driver 未发出请求?")
        return 5

    log.info(f"[INFO] 对 {CAPTURE} 跑 CAPTURE strict 验证")
    verify_cmd = [sys.executable, str(SCRIPT_DIR / "verify_coverage.py"),
                  "--captured", str(CAPTURE), "--whitelist", args.whitelist]
    if args.json:
        verify_cmd += ["--json", args.json]
    return subprocess.run(verify_cmd).returncode


if __name__ == "__main__":
    sys.exit(main())
