"""
verify_coverage CAPTURE 模式契约测试.

重点保护刚重写的 find_residual_tokens 不退化回 "段含中文豁免" 的盲点逻辑.

覆盖契约:
  - 全英文且全在 whitelist  → 0 residual
  - 全英文且部分未在 whitelist → 报告未覆盖 token
  - **混合中英文段, 但英文未在 whitelist** → 必须报告残留 (旧 bug 会跳过此段)
  - whitelist regex 匹配 URL  → URL 不计为残留
  - 空字符串 → 0 residual
  - exact 减法保留 token 边界 (用空格替换, 不让 "Claude Code" 减 "Claude" 变 " Code"
    后误判 "Code" 已不存在)

跑法:
  python3 -m unittest discover -s test -p 'test_*.py' -v
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from verify_coverage import find_residual_tokens  # noqa: E402


def make_wl(exact=None, regex=None, literal_exact=None) -> dict:
    return {
        "exact": list(exact or []),
        "regex": [re.compile(p) for p in (regex or [])],
        "literal_exact": frozenset(literal_exact or ()),
    }


class TestFindResidualTokens(unittest.TestCase):
    def test_empty_returns_no_residual(self):
        self.assertEqual(find_residual_tokens("", make_wl()), [])
        self.assertEqual(find_residual_tokens("   \n\t ", make_wl()), [])

    def test_pure_chinese_no_residual(self):
        # 纯中文段不应有任何 3+ 字母英文 token (含 "agent" 这种就会报, 见 mixed 测试)
        wl = make_wl()
        self.assertEqual(find_residual_tokens("你是一个交互式的助手，按下述指令", wl), [])

    def test_whitelist_exact_covers(self):
        # "Claude Code" 在 whitelist, "by" 只 2 字母不算英文 token
        wl = make_wl(exact=["Claude Code", "Anthropic"])
        residual = find_residual_tokens("Claude Code by Anthropic", wl)
        self.assertEqual(residual, [])

    def test_whitelist_regex_covers_url(self):
        wl = make_wl(regex=[r"https?://[^\s]+"])
        residual = find_residual_tokens(
            "see https://example.com/path for more", wl)
        # "see" "for" "more" 都是 3+ 字母英文 token 但不在 whitelist → 应报告
        # 但 URL 部分被 regex 吃掉, 不出现在残留中
        # 验证: 残留不含 "example" "com" "path"
        self.assertNotIn("example", residual)
        self.assertNotIn("path", residual)
        self.assertIn("see", residual)
        self.assertIn("for", residual)
        self.assertIn("more", residual)

    def test_literal_exact_covers_whole_text(self):
        wl = make_wl(literal_exact={"Reserved field"})
        # "Reserved" 单独不在 literal_exact 但和 field 一起整段是
        # 函数会先按 long-first 减掉 "Reserved field" → 无残留
        self.assertEqual(find_residual_tokens("Reserved field", wl), [])

    def test_mixed_chinese_english_still_reports_english(self):
        """
        关键: 旧 bug 是 "段含中文 = 整段跳过", 让混合段的未翻英文逃过 strict.
        新实现必须对混合段也按 token 级 strict 检查.
        """
        wl = make_wl(exact=["Claude"])
        text = "你在使用 Claude 但 You should configure foo manually 这段没翻."
        residual = find_residual_tokens(text, wl)
        # "You" "should" "configure" "foo" "manually" 都应报告
        self.assertIn("You", residual)
        self.assertIn("should", residual)
        self.assertIn("configure", residual)
        self.assertIn("foo", residual)
        self.assertIn("manually", residual)
        # "Claude" 被 whitelist 吃掉, 不报
        self.assertNotIn("Claude", residual)

    def test_substitution_preserves_token_boundary(self):
        """
        减法应用空格替换 (而非空串), 避免 "Claude Code" 减 "Claude" 后变 " Code"
        把 "Code" 当作合法 token 的能力被破坏.
        反向 case: 仅 "Claude" 在 whitelist, "Code" 不在 → "Code" 仍应被报告.
        """
        wl = make_wl(exact=["Claude"])
        residual = find_residual_tokens("Claude Code is here", wl)
        # "Code" 不在 whitelist 应报
        self.assertIn("Code", residual)

    def test_uncovered_token_reported(self):
        wl = make_wl(exact=["Anthropic"])
        residual = find_residual_tokens("You should read the manual", wl)
        self.assertEqual(set(residual), {"You", "should", "read", "the", "manual"})


if __name__ == "__main__":
    unittest.main()
