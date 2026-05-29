"""
Bun standalone executable 的 trailer / Offsets / module 解析.

CC binary 是 `bun build --compile` 产物, payload (cli.js 等模块) 嵌在 segment
末尾. runtime 通过末尾的 trailer 反向定位 Offsets, 再定位 module 0 (cli.js)
contents. 多脚本共用同一份解析逻辑, 集中在本模块.
"""
from __future__ import annotations

import struct
from pathlib import Path

TRAILER = b"\n---- Bun! ----\n"
OFFSETS_SIZE = 32
MODULE_ENTRY_SIZE = 52
MACHO_MAGICS = (0xfeedfacf, 0xcffaedfe)


def platform_from_binary(binary: Path) -> str | None:
    """从 binary magic 判平台: ELF→linux, Mach-O→darwin, PE→win32; .js/未知→None.

    平台 = OS (arch 无关): darwin-arm64/x64 都是 darwin, win32-x64/arm64 都是 win32。
    """
    if Path(binary).suffix == ".js":
        return None
    head = Path(binary).open("rb").read(8)
    if head[:4] == b"\x7fELF":
        return "linux"
    if len(head) >= 4 and struct.unpack_from("<I", head, 0)[0] in MACHO_MAGICS:
        return "darwin"
    if head[:2] == b"MZ":
        return "win32"
    return None


def translation_files(trans_dir: Path, platform: str | None = None) -> list[Path]:
    """返回要加载的译文文件: 公共 (根目录 *.json, 不递归) + 可选 <platform>/ 子目录。

    平台专属 prompt (如 macOS keychain 命令) 放 data/translations/<platform>/*.json,
    只在 patch 该平台时加载, 不会在别平台算漏翻/stale。
    """
    p = Path(trans_dir)
    if p.is_file():
        return [p]
    files = sorted(p.glob("*.json"))  # 公共 (glob 不递归, 不含子目录)
    if platform:
        sub = p / platform
        if sub.is_dir():
            files += sorted(sub.glob("*.json"))
    return files


def find_last_trailer(data: bytes) -> int:
    last = -1
    pos = 0
    while True:
        i = data.find(TRAILER, pos)
        if i < 0:
            break
        last = i
        pos = i + 1
    if last < 0:
        raise ValueError("Bun trailer not found in binary")
    return last


def extract_cli_js(binary_or_js: Path) -> bytes:
    """从 CC binary (ELF / Mach-O) 提取 module 0 contents (cli.js), 或对 .js
    文件直接读取. 输入既不是 Bun standalone binary 也不是 .js 时 raise."""
    data = binary_or_js.read_bytes()
    if binary_or_js.suffix == ".js":
        return data
    is_elf = data[:4] == b"\x7fELF"
    is_macho = len(data) >= 4 and struct.unpack_from("<I", data, 0)[0] in MACHO_MAGICS
    is_pe = data[:2] == b"MZ"  # Windows PE; payload 同样在尾部 trailer 结构 (.bun 节)
    if not (is_elf or is_macho or is_pe):
        raise ValueError(
            f"unknown input format: {binary_or_js} "
            f"(expected ELF / Mach-O / PE / .js, got magic {data[:8]!r})"
        )
    last = find_last_trailer(data)
    offsets_off = last - OFFSETS_SIZE
    byte_count, _, mod_off, _mod_len, *_ = struct.unpack_from(
        "<IIIIIIII", data, offsets_off)
    data_start = offsets_off - byte_count
    modules_start = data_start + mod_off
    cont_off, cont_len = struct.unpack_from("<II", data, modules_start + 8)
    return data[data_start + cont_off : data_start + cont_off + cont_len]
