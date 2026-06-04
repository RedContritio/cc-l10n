"""
active CC 二进制的 patch 状态指纹 + 状态时间线.

version 字段无法区分 patched/unpatched(两者自报同一版本号), 标记串也不在译表里。
故 patch 判定靠"扫已知翻译产物": 二进制含中文译串→patched, 含英文源串→unpatched。
版本无关、确定性, 免维护逐版本 hash 表。

时间线日志解决"探测有延迟": active 二进制换 hash 时追加一条 {ts,version,sha256,patched},
任意事件按其时间戳查时间线即可归因当时状态, 而非"探测那一刻"的状态。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

DEFAULT_CLAUDE_SYMLINK = Path.home() / ".local" / "bin" / "claude"

# 默认探针 (英文源, 中文译) —— 取自译表中跨版本稳定的核心串。
# 实装前应对真实 patched/unpatched 二进制各验证一次 (见 scripts/diagnostic 的 real-env 校验)。
DEFAULT_PROBES = [
    (b"Executes a bash command and returns its output.",
     "执行一条 bash 命令并返回其输出。".encode("utf-8")),
    (b"Performs exact string replacement in a file.",
     "在文件中执行精确的字符串替换。".encode("utf-8")),
    (b"Writes a file to the local filesystem, overwriting if one exists.",
     "向本地文件系统写入文件,若已存在则覆盖。".encode("utf-8")),
]


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def probe_patch_state(data: bytes, probes=DEFAULT_PROBES) -> dict:
    """扫描二进制字节判定 patch 状态。

    返回 {patched, determinate, n_src, n_dst, probes:[{src_found,dst_found}]}。
    determinate=False 表示既无源也无译串, 无法判定 (探针可能已不适配该版本)。
    """
    results = []
    n_src = n_dst = 0
    for src, dst in probes:
        src_found = src in data
        dst_found = dst in data
        n_src += int(src_found)
        n_dst += int(dst_found)
        results.append({"src_found": src_found, "dst_found": dst_found})
    return {
        "patched": n_dst > 0,
        "determinate": (n_src > 0 or n_dst > 0),
        "n_src": n_src,
        "n_dst": n_dst,
        "probes": results,
    }


def resolve_active_binary(symlink_path=None) -> Path:
    """跟随 ~/.local/bin/claude 符号链接到真二进制 (完全规范化)。"""
    p = Path(symlink_path) if symlink_path is not None else DEFAULT_CLAUDE_SYMLINK
    return p.resolve()


def probe_binary(path, probes=DEFAULT_PROBES) -> dict:
    """读真二进制 → {sha256, version, patched, determinate, n_src, n_dst}。"""
    path = Path(path)
    data = path.read_bytes()
    st = probe_patch_state(data, probes)
    return {
        "sha256": sha256_bytes(data),
        "version": path.name,
        "patched": st["patched"],
        "determinate": st["determinate"],
        "n_src": st["n_src"],
        "n_dst": st["n_dst"],
    }


def attribute(ts: str, timeline: list) -> dict | None:
    """给定事件时间戳, 返回时间线中该时刻生效的状态 (最后一条 ts<=event)。

    timeline 各条含 'ts' (ISO8601 UTC, 可字典序比较)。早于所有条目返回 None。
    """
    candidate = None
    for entry in sorted(timeline, key=lambda e: e["ts"]):
        if entry["ts"] <= ts:
            candidate = entry
        else:
            break
    return candidate


class BinaryStateLog:
    """append-only 二进制状态时间线; sha256 不变则不重复记录。"""

    def __init__(self, path):
        self.path = Path(path)
        self._entries: list = []
        if self.path.exists():
            for line in self.path.read_text().splitlines():
                if line.strip():
                    self._entries.append(json.loads(line))

    def entries(self) -> list:
        return list(self._entries)

    def last(self) -> dict | None:
        return self._entries[-1] if self._entries else None

    def record(self, state: dict, now: str) -> bool:
        """state 含 sha256/version/patched。sha 与上条相同则跳过, 返回是否追加。"""
        last = self.last()
        if last is not None and last.get("sha256") == state.get("sha256"):
            return False
        entry = {"ts": now, **state}
        self._entries.append(entry)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return True
