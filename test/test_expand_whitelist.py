"""
expand_whitelist 单元测试.

覆盖:
  - collect_untranslated_prompt_texts: 只收 class=prompt + translated=False + whitelisted=False
  - text.strip() 后入集合 (去除首尾空白)
  - main 流程: 读 in whitelist + extract 报告, 合并去重写 out
  - --out 缺失 → argparse error (SystemExit), 保护性必填
"""
from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import expand_whitelist as ew  # noqa: E402


def make_report_file(candidates: list[dict]) -> Path:
    f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump({"candidates": candidates}, f)
    f.close()
    return Path(f.name)


class CollectUntranslatedTests(unittest.TestCase):
    def test_collects_untranslated_prompt(self):
        path = make_report_file([
            {"class": "prompt", "translated": False, "whitelisted": False,
             "text": "Hello"},
        ])
        try:
            self.assertEqual(ew.collect_untranslated_prompt_texts(path), {"Hello"})
        finally:
            path.unlink()

    def test_skips_translated(self):
        path = make_report_file([
            {"class": "prompt", "translated": True, "whitelisted": False,
             "text": "已翻"},
        ])
        try:
            self.assertEqual(ew.collect_untranslated_prompt_texts(path), set())
        finally:
            path.unlink()

    def test_skips_whitelisted(self):
        path = make_report_file([
            {"class": "prompt", "translated": False, "whitelisted": True,
             "text": "in white"},
        ])
        try:
            self.assertEqual(ew.collect_untranslated_prompt_texts(path), set())
        finally:
            path.unlink()

    def test_skips_non_prompt_class(self):
        path = make_report_file([
            {"class": "weak", "translated": False, "whitelisted": False,
             "text": "weak text"},
        ])
        try:
            self.assertEqual(ew.collect_untranslated_prompt_texts(path), set())
        finally:
            path.unlink()

    def test_strips_text(self):
        path = make_report_file([
            {"class": "prompt", "translated": False, "whitelisted": False,
             "text": "  leading and trailing  "},
        ])
        try:
            self.assertEqual(ew.collect_untranslated_prompt_texts(path),
                             {"leading and trailing"})
        finally:
            path.unlink()


class MainFlowTests(unittest.TestCase):
    def _run_main(self, argv: list[str]) -> None:
        with patch.object(sys, "argv", argv), \
             redirect_stderr(io.StringIO()):
            ew.main()

    def test_main_merges_into_literal_exact(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            in_wl = d / "in.json"
            extract = d / "extract.json"
            out_wl = d / "out.json"

            in_wl.write_text(json.dumps({
                "_comment": "test",
                "exact": ["foo"],
                "regex": [r"\d+"],
                "literal_exact": ["existing entry"],
            }))
            extract.write_text(json.dumps({"candidates": [
                {"class": "prompt", "translated": False, "whitelisted": False,
                 "text": "new candidate"},
                {"class": "prompt", "translated": True, "whitelisted": False,
                 "text": "should skip translated"},
                {"class": "prompt", "translated": False, "whitelisted": False,
                 "text": "existing entry"},  # 重复, 集合去重
            ]}))

            self._run_main(["expand_whitelist.py",
                            "--in", str(in_wl),
                            "--from-extract", str(extract),
                            "--out", str(out_wl)])

            out = json.loads(out_wl.read_text())
            self.assertEqual(set(out["literal_exact"]),
                             {"existing entry", "new candidate"})
            self.assertEqual(out["exact"], ["foo"])
            self.assertEqual(out["regex"], [r"\d+"])

    def test_main_out_required(self):
        """--out 缺失应 argparse error (SystemExit)."""
        with tempfile.TemporaryDirectory() as d:
            extract = Path(d) / "e.json"
            extract.write_text(json.dumps({"candidates": []}))
            with self.assertRaises(SystemExit):
                # argparse error 会写 stderr, 同时退出
                with patch.object(sys, "argv",
                                  ["expand_whitelist.py",
                                   "--from-extract", str(extract)]), \
                     redirect_stderr(io.StringIO()):
                    ew.main()


if __name__ == "__main__":
    unittest.main()
