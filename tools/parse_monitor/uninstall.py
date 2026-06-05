"""
卸载 parse-monitor LaunchAgent (停止常驻并删除 plist)。运行时数据/日志默认保留。

  python3 tools/parse_monitor/uninstall.py            # 停止并删除 plist
  python3 tools/parse_monitor/uninstall.py --purge     # 同时删除 ~/.claude/parse-monitor 数据
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
from pathlib import Path

LABEL = "com.cc-l10n.parse-watcher"
MONITOR_DIR = Path.home() / ".claude" / "parse-monitor"
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--purge", action="store_true", help="同时删除运行时数据目录")
    args = ap.parse_args()

    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{LABEL}"],
                   capture_output=True, text=True)
    subprocess.run(["launchctl", "unload", "-w", str(PLIST_PATH)],
                   capture_output=True, text=True)  # legacy 退路
    if PLIST_PATH.exists():
        PLIST_PATH.unlink()
        print(f"[OK] 已删除 {PLIST_PATH} 并停止常驻")
    else:
        print(f"[skip] plist 不存在 {PLIST_PATH}")

    if args.purge and MONITOR_DIR.exists():
        shutil.rmtree(MONITOR_DIR)
        print(f"[purge] 已删除 {MONITOR_DIR}")


if __name__ == "__main__":
    main()
