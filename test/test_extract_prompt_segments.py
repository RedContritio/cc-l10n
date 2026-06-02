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

    def test_prose_structure_boost(self):
        # 长散文 prompt: 无 markdown 标题 / "You are" / IMPORTANT: 等强信号, 仅靠
        # 句子标点密度 (逗号/句号/分号后接空白) 判为 prompt (score>=5)。
        # 这正是 weak(score=4)->prompt 漏译根因的修复: 缺强信号的主 system prompt
        # 行为指令散文以前停在 4 分, 落入 class=weak 而静默漏译。
        text = ("For actions that are hard to reverse or outward-facing, confirm first "
                "unless durably authorized or explicitly told to proceed without asking; "
                "approval in one context doesn't extend to the next. Report outcomes "
                "faithfully: if tests fail, say so with the output; if a step was skipped, "
                "say that.")
        score, ev = score_text(text)
        self.assertGreaterEqual(score, 5, f"长散文应判为 prompt, 实际 {score}, ev={ev}")
        self.assertIn("prose_structure", ev)

    def test_minified_js_penalty_kills_residue(self):
        # 含 >=3 个 minified-JS runtime token 的残片 -> minified_js penalty -8, 不进 prompt class
        text = '})}catch(H){N(`error ${H}`,{reason:`failed`}),H.push(_)'
        score, ev = score_text(text)
        self.assertTrue(any(e.startswith("minified_js") for e in ev), f"应触发 minified_js, ev={ev}")
        self.assertLess(score, 5, f"JS 残片不应进入 prompt class, score={score}")

    def test_minified_js_lead_punct(self):
        # 续接标点开头 + 1 个 runtime token + 非 imperative -> 触发
        text = '}),content:`some trailing minified fragment here`'
        score, ev = score_text(text)
        self.assertTrue(any(e.startswith("minified_js") for e in ev), f"ev={ev}")

    def test_minified_js_no_false_positive_on_prompt(self):
        # 真散文 prompt 不以续接标点开头、不含 3 个 runtime token -> 不应被 minified_js penalty
        text = ("You are an interactive agent that helps the user with software "
                "engineering tasks. Always confirm before destructive actions.")
        score, ev = score_text(text)
        self.assertFalse(any(e.startswith("minified_js") for e in ev),
                         f"真 prompt 不应触发 minified_js, ev={ev}")
        self.assertGreaterEqual(score, 5, f"真 prompt 应仍 >=5, score={score}")

    def test_keyword_table_no_prose_boost(self):
        # 语言关键字表: 长 + 含 if/else/for/in 等 stop_words, 但无句子标点 (空格分隔的
        # token 流)。句子标点密度把它与散文 prompt 区分开 — 不触发 prose_structure,
        # 不进入 prompt class (score<5)。避免把 CC 打包的 syntax-highlight 语言表误判。
        text = ("int float string vector matrix if else switch case default while do "
                "for in break continue global proc return array struct enum union typedef "
                "const static void char short long double signed unsigned register extern")
        score, ev = score_text(text)
        self.assertNotIn("prose_structure", ev, f"关键字表不应判为散文, ev={ev}")
        self.assertLess(score, 5, f"关键字表不应进入 prompt class, 实际 {score}, ev={ev}")


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

    def test_padded_literal_exact_raises(self):
        # 契约: literal_exact 条目带外层空白 (前/后导空白) = 静默死条目
        # (is_whitelisted 用 strip 比较, 永不命中). load_whitelist 必须 raise.
        for bad in ("  leading space", "trailing newline\n", "\n\nblock\n"):
            with tempfile.TemporaryDirectory() as td:
                wl_path = Path(td) / "wl.json"
                wl_path.write_text(json.dumps({
                    "literal_exact": ["Clean entry", bad],
                }))
                with self.assertRaises(ValueError):
                    load_whitelist(wl_path)

    def test_clean_literal_exact_loads(self):
        # 已 strip 的条目正常加载, 不 raise
        with tempfile.TemporaryDirectory() as td:
            wl_path = Path(td) / "wl.json"
            wl_path.write_text(json.dumps({
                "literal_exact": ["Clean entry", "Another clean one"],
            }))
            wl = load_whitelist(wl_path)
        self.assertIn("Clean entry", wl["literal_exact"])
        self.assertIn("Another clean one", wl["literal_exact"])


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
