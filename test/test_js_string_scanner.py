"""
js_string_scanner 单元测试.

覆盖契约:
  - 双/单引号字面量边界 (含转义)
  - template literal: 静态片段 yield, ${...} 内嵌字符串也 yield
  - 注释 (// 与 /* ... */) 不被误识别为字符串
  - 正则字面量 (含 minified `return/re/`) 不被识别为除法的"字符串"

跑法:
  python3 -m unittest test.test_js_string_scanner -v
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from js_string_scanner import scan_string_literals  # noqa: E402


def _decode(src: bytes, ranges: list[tuple[int, int, str]]) -> list[tuple[str, str]]:
    """把 (start, end, type) 范围解码为 (type, text) 便于断言."""
    return [(t, src[s:e].decode("utf-8")) for s, e, t in ranges]


class TestDoubleQuoted(unittest.TestCase):
    def test_basic(self):
        src = b'var x = "hello";'
        out = _decode(src, scan_string_literals(src))
        self.assertEqual(out, [("dq", "hello")])

    def test_escape_quote(self):
        # \" 不应被当成 string 结束
        src = b'var x = "say \\"hi\\"";'
        out = _decode(src, scan_string_literals(src))
        self.assertEqual(out, [("dq", 'say \\"hi\\"')])

    def test_escape_backslash(self):
        src = b'var x = "a\\\\b";'  # "a\\b"
        out = _decode(src, scan_string_literals(src))
        self.assertEqual(out, [("dq", "a\\\\b")])


class TestSingleQuoted(unittest.TestCase):
    def test_basic(self):
        src = b"var x = 'hello';"
        out = _decode(src, scan_string_literals(src))
        self.assertEqual(out, [("sq", "hello")])


class TestTemplateLiteral(unittest.TestCase):
    def test_static_only(self):
        src = b"var x = `hello world`;"
        out = _decode(src, scan_string_literals(src))
        self.assertEqual(out, [("tmpl_frag", "hello world")])

    def test_interpolation_yields_fragments(self):
        # 头尾静态片段都 yield, ${...} 内不 yield (没有内嵌 string)
        src = b"var x = `pre ${y} post`;"
        out = _decode(src, scan_string_literals(src))
        self.assertEqual(out, [("tmpl_frag", "pre "), ("tmpl_frag", " post")])

    def test_nested_string_inside_interpolation(self):
        # ${...} 内嵌的 string literal 也要 yield
        src = b'var x = `a ${f("inner")} b`;'
        out = _decode(src, scan_string_literals(src))
        # 顺序按 start offset 排序: "a " < "inner" < " b"
        self.assertEqual(out, [
            ("tmpl_frag", "a "),
            ("dq", "inner"),
            ("tmpl_frag", " b"),
        ])


class TestCommentsAndRegex(unittest.TestCase):
    def test_line_comment_does_not_yield(self):
        src = b'// "fake"\nvar x = "real";'
        out = _decode(src, scan_string_literals(src))
        self.assertEqual(out, [("dq", "real")])

    def test_block_comment_does_not_yield(self):
        src = b'/* "fake" */ var x = "real";'
        out = _decode(src, scan_string_literals(src))
        self.assertEqual(out, [("dq", "real")])

    def test_regex_after_keyword(self):
        # minified: `return/re/` — `/re/` 是 regex 不是除法,
        # regex 内的 `"` 不应被当作字符串开始
        src = b'function f(){return/foo"bar/.test(s)}var z="ok";'
        out = _decode(src, scan_string_literals(src))
        # 应只 yield 末尾的 "ok"; regex 中 `"bar` 不能污染状态
        self.assertEqual(out, [("dq", "ok")])

    def test_division_not_regex(self):
        # `a / b` 是除法, 不会启动 regex 扫描
        src = b'var r = a / b; var s = "hello";'
        out = _decode(src, scan_string_literals(src))
        self.assertEqual(out, [("dq", "hello")])


if __name__ == "__main__":
    unittest.main()
