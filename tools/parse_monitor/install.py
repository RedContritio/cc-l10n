"""
安装 parse-monitor 为 launchd LaunchAgent (RunAtLoad + KeepAlive, 开机自起/崩溃自拉)。

  python3 tools/parse_monitor/install.py            # 安装并启动
  python3 tools/parse_monitor/install.py --dry-run   # 只打印将写入的 plist, 不动系统

digest 先行: 默认 config 仅开 digest, iMessage/macOS 关。日志见 ~/.claude/parse-monitor/watcher.log。
卸载: python3 tools/parse_monitor/uninstall.py
"""
from __future__ import annotations

import argparse
import json
import os
import plistlib
import subprocess
import sys
from pathlib import Path

LABEL = "com.cc-l10n.parse-watcher"
TOOLS_DIR = Path(__file__).resolve().parent.parent          # .../tools
REPO_ROOT = TOOLS_DIR.parent
MONITOR_DIR = Path.home() / ".claude" / "parse-monitor"
CONFIG_PATH = MONITOR_DIR / "config.json"
LOG_PATH = MONITOR_DIR / "watcher.log"
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _python() -> str:
    venv = REPO_ROOT / ".venv" / "bin" / "python"
    return str(venv if venv.exists() else sys.executable)


def build_plist() -> dict:
    return {
        "Label": LABEL,
        "ProgramArguments": [_python(), "-m", "parse_monitor.watcher",
                             "--config", str(CONFIG_PATH)],
        "EnvironmentVariables": {"PYTHONPATH": str(TOOLS_DIR), "PYTHONUNBUFFERED": "1"},
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Background",
        "StandardOutPath": str(LOG_PATH),
        "StandardErrorPath": str(LOG_PATH),
    }


def bootstrap_config():
    MONITOR_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_PATH.exists():
        sys.path.insert(0, str(TOOLS_DIR))
        from parse_monitor.config import default_config
        CONFIG_PATH.write_text(json.dumps(default_config(), ensure_ascii=False, indent=2))
        print(f"[config] 写入默认配置 {CONFIG_PATH} (digest 开, iMessage/macOS 关)")
    else:
        print(f"[config] 已存在, 保留 {CONFIG_PATH}")


def _launchctl(*args, check=False):
    return subprocess.run(["launchctl", *args], capture_output=True, text=True, check=check)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="只打印 plist, 不写不加载")
    args = ap.parse_args()

    plist = build_plist()
    if args.dry_run:
        print(plistlib.dumps(plist).decode())
        return

    bootstrap_config()
    PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(PLIST_PATH, "wb") as f:
        plistlib.dump(plist, f)
    print(f"[plist] 写入 {PLIST_PATH}")

    uid = os.getuid()
    domain = f"gui/{uid}"
    # 幂等: 先 bootout 旧的 (忽略未加载的报错), 再 bootstrap
    _launchctl("bootout", f"{domain}/{LABEL}")
    r = _launchctl("bootstrap", domain, str(PLIST_PATH))
    if r.returncode != 0:
        # 退回 legacy load
        r = _launchctl("load", "-w", str(PLIST_PATH))
    _launchctl("enable", f"{domain}/{LABEL}")
    _launchctl("kickstart", f"{domain}/{LABEL}")

    print(f"[launchctl] 加载结果 rc={r.returncode} {r.stderr.strip()}")
    print(f"[OK] 已安装常驻。日志: {LOG_PATH}")
    print(f"     状态: launchctl print {domain}/{LABEL} | grep -i state")
    print(f"     卸载: python3 {Path(__file__).parent / 'uninstall.py'}")


if __name__ == "__main__":
    main()
