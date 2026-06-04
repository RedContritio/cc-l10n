"""
parse-monitor 主循环 (常驻入口): 旁路轮询所有 transcript jsonl, 增量读 →
detect → incident 归并 → binary_state 归因 patch → notify。只读、出 API 路径之外,
绝不可能拖垮 CC。重启从 state.json 偏移续读, 不重复告警。

  python3 -m parse_monitor.watcher [--config <path>]   (从 tools/ 下运行)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from parse_monitor import binary_state
from parse_monitor.config import load_config
from parse_monitor.detect import classify_record
from parse_monitor.incident import IncidentGrouper
from parse_monitor.notify import notify


def read_new_lines(path, offset: int):
    """从 offset 读到最后一个换行为止的完整行; 半行保留, 偏移不动。

    offset 超过文件长度 (truncate/rotate) → 重置为 0。返回 (lines, new_offset)。
    """
    size = os.path.getsize(path)
    if offset > size:          # 文件被截断/轮换 → 从头
        offset = 0
    if offset == size:         # 无新增 → 只一次 stat, 不读
        return [], offset
    with open(path, "rb") as f:
        f.seek(offset)
        chunk = f.read()       # 只读 offset 之后的尾部
    nl = chunk.rfind(b"\n")
    if nl < 0:
        return [], offset
    complete = chunk[: nl + 1]
    lines = complete.decode("utf-8", errors="replace").splitlines()
    return lines, offset + len(complete)


def scan_once(projects_root, offsets: dict, grouper: IncidentGrouper,
              on_incident, binary_provider=None) -> dict:
    """扫一遍所有 *.jsonl, 处理各自偏移后的增量行。offsets 原地更新并返回。"""
    for path in sorted(Path(projects_root).rglob("*.jsonl")):
        key = str(path)
        off = offsets.get(key, 0)
        lines, new_off = read_new_lines(path, off)
        for line in lines:
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            cls = classify_record(rec)
            if not cls:
                continue
            res = grouper.add(rec, cls, key, off)
            inc = res["incident"]
            if binary_provider is not None and "patched" not in inc:
                st = binary_provider(inc.get("first_ts"))
                if st:
                    inc["patched"] = st.get("patched")
                    inc["binary_sha256"] = st.get("sha256")
            on_incident(res)
        offsets[key] = new_off
    return offsets


# ---- 以下为常驻 wiring (I/O + 无限循环, 由组合好的纯函数驱动) ----

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _load_offsets(path) -> dict:
    p = Path(path)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def _save_offsets(path, offsets: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(offsets))
    tmp.replace(p)


def _build_binary_tracking(config):
    log = binary_state.BinaryStateLog(config["binary_state"]["path"])
    symlink = config["binary_state"]["claude_symlink"]

    def refresh(now_iso):
        try:
            path = binary_state.resolve_active_binary(symlink)
            info = binary_state.probe_binary(path)
            log.record({"sha256": info["sha256"], "version": info["version"],
                        "patched": info["patched"], "determinate": info["determinate"],
                        "binPath": str(path)}, now_iso)
        except Exception:
            pass  # 探测失败不影响监控主职

    def provider(ts):
        return binary_state.attribute(ts, log.entries())

    return refresh, provider


def run(config_path=None):
    config = load_config(config_path)
    offsets = _load_offsets(config["state_path"])
    grouper = IncidentGrouper()
    refresh, provider = _build_binary_tracking(config)
    notify_on = set(config.get("notify_on", ["new", "escalated"]))

    def on_incident(res):
        should = (res["is_new"] and "new" in notify_on) or \
                 (res["escalated"] and "escalated" in notify_on)
        if should:
            notify(res["incident"], config)

    print(f"[parse-monitor] 启动; 监视 {config['projects_root']}; "
          f"poll={config['poll_interval']}s", flush=True)
    while True:
        try:
            refresh(_now_iso())
            scan_once(config["projects_root"], offsets, grouper, on_incident, provider)
            _save_offsets(config["state_path"], offsets)
        except Exception as e:  # 单轮异常不杀掉常驻
            print(f"[parse-monitor] scan 异常: {e!r}", flush=True)
        time.sleep(config["poll_interval"])


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=None, help="config.json 路径 (默认走内置默认值)")
    args = ap.parse_args()
    run(args.config)


if __name__ == "__main__":
    main()
