"""
extract_prompt_segments 单元测试.

覆盖契约:
  - score_text: 启发式打分对典型 prompt 形态的判定 (高分进入 strict, 低分排除)
  - load_whitelist: JSON 中 _comment 字段被过滤掉
  - is_whitelisted:
      * literal_exact 整段完全相等 → True
      * exact + regex 减掉后无 3+ 字母英文 → True
      * 减不掉的英文 token → False
      * 空字符串 → True

跑法:
  python3 -m unittest discover -s test -p 'test_*.py' -v
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from extract_prompt_segments import score_text, load_whitelist, is_whitelisted  # noqa: E402


class TestScoreText(unittest.TestCase):
    def test_high_score_prompt(self):
        # starts_with_You + stop_words >= 3 -> 进入 strict
        # 注意 starts_with_You 用 ^ 锚定字面量开头, 不能有前导 markdown heading
        text = "You are an interactive agent that helps the user with software engineering tasks."
        score, ev = score_text(text)
        self.assertGreaterEqual(score, 5, f"应进入 strict (score>=5), 实际 {score}, evidence={ev}")
        self.assertIn("starts_with_You", ev)

    def test_imperative_marker_boost(self):
        # IMPORTANT: 关键字 (+3) + 长度 >= 80 (+1 medium 或 +2 long) + stop_words>=3 (+2)
        # 总分预期 >= 5
        text = ("IMPORTANT: never modify the configuration file without a backup, "
                "because it is the only record of the previous state and you cannot recover it later.")
        score, ev = score_text(text)
        self.assertGreaterEqual(score, 5, f"实际 score={score}, evidence={ev}")
        self.assertIn("imperative_marker", ev)

    def test_identifier_only_low_score(self):
        # 纯标识符 -> 不应进入 strict
        text = "someVariableName"
        score, ev = score_text(text)
        self.assertLess(score, 5)
        self.assertIn("identifier", ev)

    def test_url_only_low_score(self):
        text = "https://example.com/some/path"
        score, ev = score_text(text)
        self.assertLess(score, 5)
        self.assertIn("url", ev)

    def test_js_code_negative(self):
        # 含 JS 代码标记 -> 强降权
        text = "function(x){return Object.keys(x).filter(a=>a.length);}"
        score, ev = score_text(text)
        # 至少触发 js_code 标记 -10
        self.assertTrue(any("js_code" in e for e in ev), f"应触发 js_code penalty, evidence={ev}")
        self.assertLess(score, 5)


class TestLoadWhitelist(unittest.TestCase):
    def test_filters_comment_keys(self):
        with tempfile.TemporaryDirectory() as td:
            wl_path = Path(td) / "wl.json"
            wl_path.write_text(json.dumps({
                "_comment": "header",
                "_rules": ["1.", "2."],
                "exact": ["Claude Code", "_comment_value_should_skip"],
                "regex": ["^\\d+$", "_comment_pattern_skip"],
                "literal_exact": ["Whole literal A", "_comment_lit_skip"],
            }))
            wl = load_whitelist(wl_path)
        # exact: _comment_value_should_skip 应过滤
        self.assertIn("Claude Code", wl["exact"])
        self.assertNotIn("_comment_value_should_skip", wl["exact"])
        # regex: 编译为 Pattern, 数量正确 (过滤 _comment_pattern_skip)
        self.assertEqual(len(wl["regex"]), 1)
        self.assertTrue(isinstance(wl["regex"][0], re.Pattern))
        # literal_exact: frozenset
        self.assertIn("Whole literal A", wl["literal_exact"])
        self.assertNotIn("_comment_lit_skip", wl["literal_exact"])


class TestIsWhitelisted(unittest.TestCase):
    def setUp(self):
        self.wl = {
            "exact": ["Claude Code", "Anthropic"],
            "regex": [re.compile(r"https?://[^\s]+")],
            "literal_exact": frozenset({"Reserved field"}),
        }

    def test_empty_string_whitelisted(self):
        self.assertTrue(is_whitelisted("", self.wl))
        self.assertTrue(is_whitelisted("   \n\t ", self.wl))

    def test_literal_exact_match(self):
        # 整段完全等于 literal_exact 中某项
        self.assertTrue(is_whitelisted("Reserved field", self.wl))
        # strip 后等于也算
        self.assertTrue(is_whitelisted("  Reserved field\n", self.wl))

    def test_exact_subtraction_covers(self):
        # "Claude Code" 在 exact 中, 减掉后剩 " by " — "by" 只 2 字母不算英文 token
        # 实际剩 " by " — by 是 2 字母, 不匹配 [A-Za-z]{3,}
        self.assertTrue(is_whitelisted("Claude Code by Anthropic", self.wl))

    def test_regex_subtraction_covers(self):
        # URL 被 regex 吃掉
        self.assertTrue(is_whitelisted("https://example.com/path", self.wl))

    def test_uncovered_token_fails(self):
        # 含未在 whitelist 的 3+ 字母英文 token
        self.assertFalse(is_whitelisted("You should configure foo", self.wl))


if __name__ == "__main__":
    unittest.main()
