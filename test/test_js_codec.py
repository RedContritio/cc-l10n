"""js_codec 单元测试: decode_literal 与 encode_dst_decoded 互为逆.

核心契约:
  decode_literal(encode_dst_decoded(x, lt), 0, len, lt) == x   (round-trip)
对每种 lit_type (dq/sq/tmpl_frag) 在含换行/tab/引号/反斜杠/反引号/$/${}/非ASCII/emoji
的代表性字符串上验证。

回归点 (本次重构修的 bug):
  - encode 把非 ASCII (— → 中文 emoji) 保持原始 UTF-8, 不转 \\uXXXX
  - tmpl_frag 的 ` 与 $ 正确转义且能精确还原 (含与反斜杠相邻的刁钻情形)

跑法: python3 -m unittest discover -s test -p 'test_*.py'
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from js_codec import decode_literal, encode_dst_decoded  # noqa: E402

CASES = [
    "你好 world",
    "line1\nline2\ttab\rcr",
    "em — dash 中文 → arrow ← back ≥ ≤ ×",
    "code `inline` and $var and ${X} literal",
    'mixed quotes " and \' together',
    "backslash \\ path C:\\users\\x and regex \\d+",
    "emoji 🤖 surrogate test",
    "trailing backslash\\",
    "adjacent \\` and \\$ sequences",
    "# Managed Agents — Overview\n\nProvisions ${COUNT} containers.",
    "",
]


class RoundTripTests(unittest.TestCase):
    def _roundtrip(self, x: str, lt: str) -> str:
        enc = encode_dst_decoded(x.encode("utf-8"), lt)
        return decode_literal(enc, 0, len(enc), lt)

    def test_roundtrip_all_lit_types(self):
        for lt in ("dq", "sq", "tmpl_frag"):
            for x in CASES:
                with self.subTest(lit_type=lt, text=x[:30]):
                    self.assertEqual(self._roundtrip(x, lt), x)

    def test_nonascii_kept_raw_not_uXXXX(self):
        """回归: 非 ASCII 必须原始 UTF-8 写回, 不能转 \\uXXXX (旧 bug 根因)."""
        for lt in ("dq", "sq", "tmpl_frag"):
            enc = encode_dst_decoded("破折号 — 与中文".encode("utf-8"), lt)
            self.assertIn("破折号".encode("utf-8"), enc)
            self.assertIn("—".encode("utf-8"), enc)
            self.assertNotIn(b"\\u", enc)

    def test_tmpl_escapes_backtick_and_dollar(self):
        enc = encode_dst_decoded("`x` and $y".encode("utf-8"), "tmpl_frag")
        self.assertEqual(enc, "\\`x\\` and \\$y".encode("utf-8"))

    def test_tmpl_escapes_backslash_keeps_newline_raw(self):
        enc = encode_dst_decoded("a\\b\nc".encode("utf-8"), "tmpl_frag")
        # 反斜杠转义为 \\, 真换行保持原样 (模板字面量合法)
        self.assertEqual(enc, "a\\\\b\nc".encode("utf-8"))

    def test_dq_escapes_quote_newline_backslash(self):
        enc = encode_dst_decoded('say "hi"\nend\\path'.encode("utf-8"), "dq")
        self.assertEqual(enc, 'say \\"hi\\"\\nend\\\\path'.encode("utf-8"))

    def test_sq_escapes_single_quote_only(self):
        enc = encode_dst_decoded("it's a \"test\"".encode("utf-8"), "sq")
        # sq 下双引号原样, 单引号转义
        self.assertEqual(enc, "it\\'s a \"test\"".encode("utf-8"))


class UnifiedEncoderTests(unittest.TestCase):
    """encode_dst_decoded 是唯一编码器: raw 透传 / tmpl 别名 / escape_unicode / 全 lit_type。"""

    def test_raw_passthrough(self):
        b = "中文 ${var} `code`".encode("utf-8")
        self.assertEqual(encode_dst_decoded(b, "raw"), b)

    def test_tmpl_alias_equals_tmpl_frag(self):
        b = "标题 — `x` $y ${z}\n续行".encode("utf-8")
        self.assertEqual(encode_dst_decoded(b, "tmpl"), encode_dst_decoded(b, "tmpl_frag"))

    def test_escape_unicode_produces_uXXXX(self):
        enc = encode_dst_decoded("破折号—".encode("utf-8"), "dq", escape_unicode=True)
        self.assertIn(b"\\u", enc)
        self.assertNotIn("—".encode("utf-8"), enc)

    def test_unknown_lit_type_raises(self):
        with self.assertRaises(ValueError):
            encode_dst_decoded(b"x", "weird")

    def test_tmpl_escapes_dollar_for_literal_interpolation_text(self):
        # 单字面量里的 ${X} 是字面文本, 必须转义防止变插值
        enc = encode_dst_decoded("用 ${TOOL} 来做".encode("utf-8"), "tmpl_frag")
        self.assertIn(b"\\${TOOL}", enc)


class DecodeLiteralTests(unittest.TestCase):
    def test_dq_decodes_uXXXX_and_escapes(self):
        cli = b'a\\u2014b\\nc'
        self.assertEqual(decode_literal(cli, 0, len(cli), "dq"), "a—b\nc")

    def test_tmpl_frag_fully_decodes(self):
        # 统一为完全解码: \\` \\$ \\u 都解
        cli = b'x\\`y\\$z\\u2014w'
        self.assertEqual(decode_literal(cli, 0, len(cli), "tmpl_frag"), "x`y$z—w")

    def test_all_lit_types_decode_identically(self):
        # 同一字节序列 (无引号歧义部分) 三种 lit_type 解码结果一致 -> canonical
        cli = b"a\\u2014b\\nc\\td"
        r = [decode_literal(cli, 0, len(cli), lt) for lt in ("dq", "sq", "tmpl_frag")]
        self.assertEqual(r[0], "a—b\nc\td")
        self.assertEqual(r[0], r[1])
        self.assertEqual(r[1], r[2])

    def test_invalid_utf8_returns_none(self):
        cli = b"\xff\xfe bad"
        self.assertIsNone(decode_literal(cli, 0, len(cli), "dq"))


if __name__ == "__main__":
    unittest.main()
