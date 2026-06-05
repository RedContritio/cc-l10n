"""
incident 告警分发: digest(事实源,始终)/ iMessage self-chat(主推送)/ macOS 通知(兜底)。

单渠道失败被吞、不影响其他渠道、不崩主循环。osascript 调用经 runner 注入, 便于测试 mock。
iMessage/macOS 用 osascript 的 `on run argv` 传参 (而非拼进 -e 脚本), 避免文本里的引号注入。
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


def _patch_label(incident) -> str:
    p = incident.get("patched")
    if p is True:
        return "patched"
    if p is False:
        return "unpatched"
    return "patch-state unknown"


def format_message(incident) -> str:
    project = os.path.basename((incident.get("project") or "").rstrip("/")) or "?"
    return (f"[CC parse {incident.get('severity', '?')}] {project} "
            f"({incident.get('version', '?')}, {_patch_label(incident)}) — "
            f"silent×{incident.get('silent_count', 0)}/soft×{incident.get('soft_count', 0)}"
            f"/hard×{incident.get('hard_count', 0)} "
            f"@ {incident.get('first_ts', '?')}")


def _default_runner(args):
    subprocess.run(args, check=True, capture_output=True, timeout=15)


def send_digest(incident, path) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a") as f:
        f.write(json.dumps(incident, ensure_ascii=False) + "\n")


def send_imessage(text, handle, runner) -> None:
    script = ('on run {msg, hdl}\n'
              '  tell application "Messages"\n'
              '    set svc to 1st service whose service type = iMessage\n'
              '    send msg to participant hdl of svc\n'
              '  end tell\n'
              'end run')
    runner(["osascript", "-e", script, text, handle])


def send_macos(text, runner) -> None:
    script = ('on run {msg}\n'
              '  display notification msg with title "CC parse monitor"\n'
              'end run')
    runner(["osascript", "-e", script, text])


def _safe(fn, *args) -> bool:
    try:
        fn(*args)
        return True
    except Exception:
        return False


def notify(incident, config, runner=None) -> dict:
    runner = runner or _default_runner
    results = {}

    dg = config.get("digest", {})
    if dg.get("enabled", True):
        results["digest"] = _safe(send_digest, incident, dg.get("path"))

    im = config.get("imessage", {})
    if im.get("enabled", False):
        results["imessage"] = _safe(send_imessage, format_message(incident),
                                    im.get("handle"), runner)

    mac = config.get("macos", {})
    if mac.get("enabled", False):
        results["macos"] = _safe(send_macos, format_message(incident), runner)

    return results
