"""JS 字符串字面量 codec: decode (字面量字节 -> Python str) 与其精确逆向 encode.

唯一 canonical 形态 = **完全解码** (fully decoded): 真换行 / 真破折号 / 真反引号,
所有 lit_type (dq/sq/tmpl_frag) 走同一套解码, 输出一致 —— 这样同一逻辑字符串无论以
哪种字面量形式出现, decode 结果都相同, 匹配只需 decoded == decoded 一条路 (无双路、无垫片)。

decode_literal 与 encode_dst_decoded 互为逆: decode(encode(x, lt), lt) == x。
encode 按 lit_type 还原为合法 JS 字面量字节:
  - dq/sq: 转义 \\ 当前引号 换行 tab cr 控制符; 非 ASCII 保持原始 UTF-8
  - tmpl_frag: 转义 \\ ` $; 换行保持真字符 (模板字面量合法); 非 ASCII 保持原始 UTF-8
非 ASCII 一律原始 UTF-8 写回 (与 JS minifier 风格一致、体积更小)。

lit_type ∈ {'dq', 'sq', 'tmpl_frag'} (由 js_string_scanner.scan_string_literals 给出)。
"""
from __future__ import annotations

_SIMPLE_ESCAPES = {
    "n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "v": "\v",
    '"': '"', "'": "'", "`": "`", "$": "$", "\\": "\\", "/": "/",
}


def decode_literal(cli: bytes, start: int, end: int, lit_type: str) -> str | None:
    """把 [start, end) 字面量完全解码为 Python str (canonical 形态).

    所有 lit_type 统一处理: \\n \\t \\r \\b \\f \\v \\\\ \\" \\' \\` \\$ \\/ \\uXXXX \\xHH。
    真换行 (模板里合法) 原样保留。未知转义 (如 \\z) 退化为保留反斜杠 + 字符。
    """
    raw = cli[start:end]
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c == "\\" and i + 1 < n:
            nxt = text[i + 1]
            if nxt in _SIMPLE_ESCAPES:
                out.append(_SIMPLE_ESCAPES[nxt]); i += 2; continue
            if nxt == "u" and i + 5 < n:
                try:
                    cp = int(text[i + 2:i + 6], 16)
                    # UTF-16 surrogate pair (e.g. 🤖 emoji)
                    if 0xD800 <= cp <= 0xDBFF and i + 11 < n \
                            and text[i + 6] == "\\" and text[i + 7] == "u":
                        try:
                            cp2 = int(text[i + 8:i + 12], 16)
                            if 0xDC00 <= cp2 <= 0xDFFF:
                                combined = 0x10000 + (cp - 0xD800) * 0x400 + (cp2 - 0xDC00)
                                out.append(chr(combined))
                                i += 12
                                continue
                        except ValueError:
                            pass
                    if 0xD800 <= cp <= 0xDFFF:
                        # 孤立 surrogate, 保留原字面避免编码错误
                        out.append(text[i:i + 6])
                        i += 6
                        continue
                    out.append(chr(cp))
                    i += 6
                    continue
                except ValueError:
                    pass
            if nxt == "x" and i + 3 < n:
                try:
                    cp = int(text[i + 2:i + 4], 16)
                    out.append(chr(cp))
                    i += 4
                    continue
                except ValueError:
                    pass
            out.append(c); i += 1
        else:
            out.append(c); i += 1
    # 行尾归一: Windows binary 用 CRLF, linux/mac 用 LF; 统一为 LF 使同一 prompt
    # 跨平台匹配同一条译文 (linux/mac 无 \r\n, 此处为 no-op)。
    return "".join(out).replace("\r\n", "\n")


def encode_dst_decoded(dst: bytes, lit_type: str, escape_unicode: bool = False) -> bytes:
    """canonical decoded 文本 (raw utf-8 bytes) -> 指定 lit_type 的字面量字节形式.

    这是【唯一】的 decoded -> 字面量编码器, 既用于写 dst, 也用于生成 src 匹配形态。
    是 decode_literal 的精确逆: decode_literal(encode_dst_decoded(x, lt), 0, len, lt) == x。

    一句话规则: 转义反斜杠、外围引号、控制符 (dq/sq 里换行转 \\n; 模板字面量里换行保留原样),
    以及【仅模板字面量】反引号 ` 与 `$`; 其余一切 (含非 ASCII) 保持原始 UTF-8。
    (escape_unicode=True 时非 ASCII 转 \\uXXXX, 仅供 src 匹配生成兼容形态; lit_type='raw' 原样返回。)
    """
    if lit_type == "raw":
        return dst
    is_tmpl = lit_type in ("tmpl", "tmpl_frag")
    if not (is_tmpl or lit_type in ("dq", "sq")):
        raise ValueError(f"未知 lit_type: {lit_type!r}")
    quote = "`" if is_tmpl else ('"' if lit_type == "dq" else "'")
    s = dst.decode("utf-8")
    out: list[str] = []
    for ch in s:
        cp = ord(ch)
        if ch == "\\":
            out.append("\\\\")
        elif ch == quote:
            out.append("\\" + quote)
        elif is_tmpl and ch == "$":
            out.append("\\$")
        elif ch == "\n":
            out.append(ch if is_tmpl else "\\n")  # 模板字面量里真换行合法, 原样
        elif ch == "\t":
            out.append("\\t")
        elif ch == "\r":
            out.append("\\r")
        elif cp < 0x20:
            out.append(f"\\x{cp:02x}")
        elif cp < 0x80 or not escape_unicode:
            out.append(ch)  # ASCII 原样; 非 ASCII 默认原始 UTF-8
        elif cp <= 0xFFFF:
            out.append(f"\\u{cp:04x}")
        else:
            cp -= 0x10000
            hi = 0xD800 + (cp >> 10)
            lo = 0xDC00 + (cp & 0x3FF)
            out.append(f"\\u{hi:04x}\\u{lo:04x}")
    return "".join(out).encode("utf-8")
