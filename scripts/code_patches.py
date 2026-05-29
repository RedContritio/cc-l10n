"""
CC client bug 的 surgical binary 替换 — 与翻译 pipeline 分离.

每条 (src_bytes, dst_bytes, why). src 必须在 cli.js 中唯一出现; 不满足契约时 raise.
"""
from __future__ import annotations

CODE_PATCHES: list[tuple[bytes, bytes, str]] = [
    (
        b'cacheScope:"global"',
        b'cacheScope:"org"',
        "CC client bug: IQ8 在 tools 非空时仍发 scope:'global' 触发 API "
        "'system[0] is not a true prefix' 校验错. 改为 org 跳过 global cache 校验.",
    ),
]


def apply_code_patches(cli: bytes) -> tuple[bytes, list[dict]]:
    """对 cli 应用所有 CODE_PATCHES, 返回 (new_cli, applied). 不打印; 由 caller 决定如何报告."""
    new_cli = cli
    applied = []
    for src, dst, why in CODE_PATCHES:
        count = new_cli.count(src)
        if count == 0:
            raise ValueError(f"code-patch src 已不在 cli.js (CC 升级后需重对): {src!r}")
        if count > 1:
            raise ValueError(f"code-patch src 在 cli.js 中出现 {count} 处, 需精确化: {src!r}")
        new_cli = new_cli.replace(src, dst)
        applied.append({"src": src.decode(), "dst": dst.decode(), "why": why})
    return new_cli, applied
