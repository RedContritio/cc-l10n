"""
CC client bug 的 surgical binary 替换 + FN=0 模板 frag 译 — 与翻译 pipeline 分离.

每条 (src_bytes, dst_bytes, why). src 必须在 cli.js 中唯一出现; 不满足契约时 raise.
注: apply_code_patches 在翻译 repack **之后**跑 (对已译 binary), 故 FN=0 模板 frag 的 src
须用 post-translation 字节 (含已译中文锚定 + minified 变量名使其唯一)。版本脆: CC 升级后
minified 变量名变, src 不命中即 raise, 需重对。
"""
from __future__ import annotations

CODE_PATCHES: list[tuple[bytes, bytes, str]] = [
    (
        b'cacheScope:"global"',
        b'cacheScope:"org"',
        "CC client bug: IQ8 在 tools 非空时仍发 scope:'global' 触发 API "
        "'system[0] is not a true prefix' 校验错. 改为 org 跳过 global cache 校验.",
    ),
    # FN=0 模板 frag 的字节级 patch (EnterPlanMode 'and'→'和' / Write 删 'instead') 暂停用:
    # src 锚定 post-translation 字节, 与相邻译条 (35 的 '2. Understand...') 边界交互致脆,
    # 译表一改即失配 raise 中断 install。待 build_fn0_code_patches.py 自动重算后再启用。
    # 这 2 个 token 是 57/14 处共享 tmpl_frag, 暂回退为 CAPTURE 残留 2 (and/instead)。
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
