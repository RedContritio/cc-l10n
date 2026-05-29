"""
audit_replacements 单元测试.

覆盖契约 (placeholder consistency 是 strict pipeline 的最关键安全机关):
  - placeholder 顺序匹配 (allow_reorder=False)  → no violation
  - placeholder 顺序反转 (allow_reorder=False)  → risk_e > 0
  - placeholder 顺序反转 (allow_reorder=True)   → no violation (允许中文语序调整)
  - placeholder 数量不一致 (任一模式)            → risk_e > 0
  - PLACEHOLDER_RE 能正确识别 ${...} 与嵌套 ${a.b}

通过 patch bun_format.extract_cli_js 注入最小化 cli.js 字节流 (绕过 binary 解析).

跑法:
  python3 -m unittest discover -s test -p 'test_*.py' -v
"""
from __future__ import annotations

import sys
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import audit_replacements as ar  # noqa: E402
from audit_replacements import PLACEHOLDER_RE  # noqa: E402


def run_audit(fake_cli: bytes, translations: list[tuple[bytes, bytes, bool]]) -> dict:
    """注入 fake cli.js 跑 audit, 屏蔽 binary 解析."""
    with patch.object(ar, "extract_cli_js", return_value=fake_cli):
        return ar.audit(Path("/dev/null"), translations)


class TestPlaceholderRegex(unittest.TestCase):
    def test_simple_placeholder(self):
        m = PLACEHOLDER_RE.findall(b"hello ${A} world ${B}")
        self.assertEqual(m, [b"${A}", b"${B}"])

    def test_nested_brace_inside_placeholder(self):
        # ${a.b} 与 ${obj["k"]} 都是合法 minified JS 模板插值
        m = PLACEHOLDER_RE.findall(b"x=${a.b} y=${c}")
        self.assertEqual(m, [b"${a.b}", b"${c}"])

    def test_no_placeholder(self):
        self.assertEqual(PLACEHOLDER_RE.findall(b"plain text"), [])


class TestAuditPlaceholderConsistency(unittest.TestCase):
    """直接走 audit() 整链路, 因为这是 strict pipeline 实际入口."""

    def test_order_match_no_violation(self):
        cli = b'var x="say ${A} and ${B} thanks";'
        translations = [
            (b"say ${A} and ${B} thanks", b"\xe8\xaf\xb4 ${A} \xe5\x92\x8c ${B} \xe8\xb0\xa2", False),
        ]
        report = run_audit(cli, translations)
        self.assertEqual(report["risk_e"], 0, f"顺序匹配不应触发 risk_e: {report}")
        self.assertEqual(report["high_risk"], 0)

    def test_order_reversed_strict_mode_violates(self):
        # ${A} ${B} -> ${B} ${A}, allow_reorder=False → 语义反转
        cli = b'var x="say ${A} and ${B} thanks";'
        translations = [
            (b"say ${A} and ${B} thanks", b"reversed ${B} and ${A} order", False),
        ]
        report = run_audit(cli, translations)
        self.assertGreaterEqual(report["risk_e"], 1, f"应触发 placeholder violation: {report}")
        # by_src 应标 placeholder_violation
        violated = [e for e in report["by_src"] if e.get("placeholder_violation")]
        self.assertEqual(len(violated), 1)
        self.assertEqual(violated[0]["src_placeholders"], ["${A}", "${B}"])
        self.assertEqual(violated[0]["dst_placeholders"], ["${B}", "${A}"])

    def test_order_reversed_allow_reorder_passes(self):
        # 同样反转, allow_reorder=True → multiset 匹配, 不算违规
        cli = b'var x="say ${A} and ${B} thanks";'
        translations = [
            (b"say ${A} and ${B} thanks", b"reversed ${B} and ${A} order", True),
        ]
        report = run_audit(cli, translations)
        self.assertEqual(report["risk_e"], 0, f"allow_reorder=True 不应触发 risk_e: {report}")

    def test_placeholder_count_mismatch_always_violates(self):
        # src 有 2 个 placeholder, dst 只有 1 个 → 无论 allow_reorder 都是违规 (multiset 不等)
        cli = b'var x="${A} ${B}";'
        translations = [
            (b"${A} ${B}", b"only ${A}", True),  # allow_reorder=True 也救不了
        ]
        report = run_audit(cli, translations)
        self.assertGreaterEqual(report["risk_e"], 1, f"placeholder 数量不一致必须违规: {report}")

    def test_no_placeholder_no_risk_e(self):
        # src 无 placeholder, dst 无 placeholder → risk_e 不增加
        cli = b'var x="plain literal";'
        translations = [
            (b"plain literal", b"\xe7\xba\xaf\xe6\x96\x87\xe6\x9c\xac", False),
        ]
        report = run_audit(cli, translations)
        self.assertEqual(report["risk_e"], 0)


if __name__ == "__main__":
    unittest.main()
