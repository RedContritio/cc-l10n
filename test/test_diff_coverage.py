"""
diff_coverage 单元测试.

覆盖:
  - 全部 translated → exit 0
  - 含 untranslated → exit 1
  - whitelisted 不算 untranslated
  - --min-score 过滤生效 (低分被忽略)
  - --min-score 过滤后 0 候选 → ValueError
  - --out 写 diff-report JSON 结构正确

跑法:
  python3 -m unittest discover -s test -p 'test_*.py' -v
"""
from __future__ import annotations

import io
import json
import logging
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import diff_coverage as dc  # noqa: E402


def setUpModule():
    # diff_coverage logger 在 import 时绑死 sys.stderr, redirect_stderr 不生效.
    # 全 module 静音 logging 以净化测试输出.
    logging.disable(logging.CRITICAL)


def tearDownModule():
    logging.disable(logging.NOTSET)


def make_candidate(score: int, translated: bool, whitelisted: bool,
                   text: str = "x", offset: int = 0) -> dict:
    return {
        "score": score,
        "translated": translated,
        "whitelisted": whitelisted,
        "text": text,
        "len": len(text),
        "offset": offset,
        "class": "prompt",
        "evidence": [],
    }


def write_report(path: Path, candidates: list[dict]) -> None:
    path.write_text(json.dumps({"candidates": candidates}))


class DiffCoverageExitCodeTests(unittest.TestCase):
    def _run_main(self, argv: list[str]) -> int:
        """跑 dc.main(), 吞 stderr/stdout, 返回 exit code (0 表示无 SystemExit)."""
        with patch.object(sys, "argv", argv), \
             redirect_stdout(io.StringIO()):
            try:
                dc.main()
                return 0
            except SystemExit as e:
                return e.code if e.code is not None else 0

    def test_all_translated_exit_0(self):
        with tempfile.TemporaryDirectory() as d:
            report = Path(d) / "r.json"
            write_report(report, [make_candidate(5, True, False)])
            self.assertEqual(self._run_main(["diff", str(report)]), 0)

    def test_untranslated_exit_1(self):
        with tempfile.TemporaryDirectory() as d:
            report = Path(d) / "r.json"
            write_report(report, [make_candidate(5, False, False)])
            self.assertEqual(self._run_main(["diff", str(report)]), 1)

    def test_whitelisted_not_counted_as_untranslated(self):
        with tempfile.TemporaryDirectory() as d:
            report = Path(d) / "r.json"
            write_report(report, [make_candidate(5, False, True)])
            self.assertEqual(self._run_main(["diff", str(report)]), 0)

    def test_min_score_filters_low(self):
        """--min-score=5 过滤掉 score=2 的低分 untranslated, 剩余全 translated → exit 0."""
        with tempfile.TemporaryDirectory() as d:
            report = Path(d) / "r.json"
            write_report(report, [
                make_candidate(2, False, False, text="low"),
                make_candidate(5, True, False, text="high"),
            ])
            self.assertEqual(self._run_main(["diff", str(report), "--min-score", "5"]), 0)

    def test_min_score_2_keeps_low(self):
        """--min-score=2 不过滤 score=2, low 算 untranslated → exit 1."""
        with tempfile.TemporaryDirectory() as d:
            report = Path(d) / "r.json"
            write_report(report, [
                make_candidate(2, False, False, text="low"),
                make_candidate(5, True, False, text="high"),
            ])
            self.assertEqual(self._run_main(["diff", str(report), "--min-score", "2"]), 1)

    def test_zero_candidates_after_filter_raises(self):
        """min_score 过滤后 0 候选 → ValueError (strict, 不静默)."""
        with tempfile.TemporaryDirectory() as d:
            report = Path(d) / "r.json"
            write_report(report, [make_candidate(1, False, False)])
            with self.assertRaises(ValueError) as ctx:
                with patch.object(sys, "argv",
                                  ["diff", str(report), "--min-score", "10"]), \
                     redirect_stdout(io.StringIO()):
                    dc.main()
            self.assertIn("没有 score >=", str(ctx.exception))


class DiffReportOutputTests(unittest.TestCase):
    def _run_main_capturing(self, argv: list[str]) -> None:
        with patch.object(sys, "argv", argv), \
             redirect_stdout(io.StringIO()):
            try:
                dc.main()
            except SystemExit:
                pass

    def test_out_writes_diff_report(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            report = d / "r.json"
            out = d / "diff.json"
            write_report(report, [
                make_candidate(5, False, False, text="untranslated entry", offset=10),
                make_candidate(5, True, False, text="translated", offset=20),
                make_candidate(5, False, True, text="whitelisted", offset=30),
            ])
            self._run_main_capturing(["diff", str(report), "--out", str(out)])
            self.assertTrue(out.exists())
            data = json.loads(out.read_text())
            self.assertEqual(data["total_candidates"], 3)
            self.assertEqual(data["translated"], 1)
            self.assertEqual(data["whitelisted"], 1)
            self.assertEqual(data["untranslated"], 1)
            self.assertEqual(len(data["untranslated_top"]), 1)
            self.assertEqual(data["untranslated_top"][0]["text"], "untranslated entry")

    def test_untranslated_top_sorted_by_score_desc(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            report = d / "r.json"
            out = d / "diff.json"
            write_report(report, [
                make_candidate(3, False, False, text="mid",  offset=10),
                make_candidate(7, False, False, text="high", offset=20),
                make_candidate(5, False, False, text="med",  offset=30),
            ])
            self._run_main_capturing(["diff", str(report), "--out", str(out)])
            data = json.loads(out.read_text())
            scores = [e["score"] for e in data["untranslated_top"]]
            self.assertEqual(scores, sorted(scores, reverse=True))
            self.assertEqual(data["untranslated_top"][0]["text"], "high")


if __name__ == "__main__":
    unittest.main()
