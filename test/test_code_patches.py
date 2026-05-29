"""
code_patches.apply_code_patches 单元测试.

覆盖契约:
  - src 在 cli.js 中 count == 0  → raise ValueError
  - src 在 cli.js 中 count == 1  → 替换成功, 返回 applied list
  - src 在 cli.js 中 count > 1   → raise ValueError (强制精确化)
  - 多条 patch 顺序应用            → 各自独立
  - 真实 CODE_PATCHES 结构合法     → sanity check
  - 纯函数无副作用                 → 不打印 (由 caller 报告)

跑法:
  python3 -m unittest discover -s test -p 'test_*.py' -v
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import code_patches as cp  # noqa: E402


class ApplyCodePatchesTests(unittest.TestCase):
    def _apply(self, cli: bytes, patches: list[tuple[bytes, bytes, str]]):
        with patch.object(cp, "CODE_PATCHES", patches):
            return cp.apply_code_patches(cli)

    def test_single_patch_replaced(self):
        cli = b'foo cacheScope:"global" bar'
        patches = [(b'cacheScope:"global"', b'cacheScope:"org"', "test reason")]
        new_cli, applied = self._apply(cli, patches)
        self.assertEqual(new_cli, b'foo cacheScope:"org" bar')
        self.assertEqual(len(applied), 1)
        self.assertEqual(applied[0]["src"], 'cacheScope:"global"')
        self.assertEqual(applied[0]["dst"], 'cacheScope:"org"')
        self.assertEqual(applied[0]["why"], "test reason")

    def test_src_not_found_raises(self):
        cli = b'foo bar baz'
        patches = [(b'nonexistent', b'replacement', "why")]
        with self.assertRaises(ValueError) as ctx:
            self._apply(cli, patches)
        self.assertIn("不在", str(ctx.exception))

    def test_src_multiple_occurrences_raises(self):
        cli = b'cacheScope:"global" and cacheScope:"global" twice'
        patches = [(b'cacheScope:"global"', b'cacheScope:"org"', "why")]
        with self.assertRaises(ValueError) as ctx:
            self._apply(cli, patches)
        self.assertIn("2 处", str(ctx.exception))

    def test_multiple_patches_applied_in_order(self):
        cli = b'AAA and BBB'
        patches = [
            (b'AAA', b'aaa', "lower A"),
            (b'BBB', b'bbb', "lower B"),
        ]
        new_cli, applied = self._apply(cli, patches)
        self.assertEqual(new_cli, b'aaa and bbb')
        self.assertEqual([p["src"] for p in applied], ["AAA", "BBB"])

    def test_second_patch_sees_first_replacement(self):
        """后一条 patch 对前一条替换后的 cli 计数; 若新 src 因前替换而出现 0/多次, 应 raise."""
        cli = b'AAA'
        patches = [
            (b'AAA', b'BBB', "first"),
            (b'AAA', b'XXX', "second — AAA 已不在"),
        ]
        with self.assertRaises(ValueError):
            self._apply(cli, patches)

    def test_real_code_patches_well_formed(self):
        self.assertGreaterEqual(len(cp.CODE_PATCHES), 1)
        for src, dst, why in cp.CODE_PATCHES:
            self.assertIsInstance(src, bytes)
            self.assertIsInstance(dst, bytes)
            self.assertIsInstance(why, str)
            self.assertGreater(len(why), 10, "why 必须有实际说明 (>10 字符)")


if __name__ == "__main__":
    unittest.main()
