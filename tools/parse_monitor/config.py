"""
parse-monitor 配置: 默认值 + 浅合并用户 config.json。

运行时数据默认在全局 ~/.claude/parse-monitor/ (含对话片段, 敏感, 不入 git)。
"""
from __future__ import annotations

import json
from pathlib import Path

MONITOR_DIR = Path.home() / ".claude" / "parse-monitor"


def default_config() -> dict:
    return {
        "projects_root": str(Path.home() / ".claude" / "projects"),
        "poll_interval": 4.0,
        "state_path": str(MONITOR_DIR / "state.json"),
        "notify_on": ["new", "escalated"],
        "digest": {"enabled": True, "path": str(MONITOR_DIR / "incidents.jsonl")},
        "binary_state": {
            "path": str(MONITOR_DIR / "binary_state.jsonl"),
            "claude_symlink": str(Path.home() / ".local" / "bin" / "claude"),
        },
        "imessage": {"enabled": False, "handle": ""},
        "macos": {"enabled": False},
    }


def load_config(path=None) -> dict:
    cfg = default_config()
    if path and Path(path).exists():
        user = json.loads(Path(path).read_text())
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    return cfg
