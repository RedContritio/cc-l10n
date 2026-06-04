"""integration_test 聚合 no-stale 版本范围契约测试 (#4).

只测纯函数 (无 npm/网络):
  - resolve_supported: CSV 显式 / 默认 dist-tags
  - compute_stale: (translation ∪ whitelist) - matched

全量跨版本集成跑由 CI 负责 (需 npm + binary 下载)。

跑法: python3 -m unittest discover -s test -p 'test_*.py' -v
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from integration_test import resolve_supported, compute_stale  # noqa: E402


class TestResolveSupported(unittest.TestCase):
    def test_csv_explicit_overrides_run_set(self):
        # 显式 CSV 收窄/指定判定范围, 无视运行集
        s = resolve_supported("2.1.150, 2.1.160 ,2.1.158", ["2.1.160"])
        self.assertEqual(s, {"2.1.150", "2.1.160", "2.1.158"})

    def test_default_is_run_versions(self):
        # 无 CSV -> 默认取本次运行的版本集 (跨整个运行集判 stale)
        s = resolve_supported(None, ["2.1.150", "2.1.156", "2.1.160"])
        self.assertEqual(s, {"2.1.150", "2.1.156", "2.1.160"})

    def test_empty_csv_falls_back_to_run_set(self):
        s = resolve_supported("", ["2.1.160"])
        self.assertEqual(s, {"2.1.160"})

    def test_whitespace_only_csv_falls_back(self):
        # 仅空白/逗号的 CSV 不应返回空集 (空集会让全部条目判 stale), 应回退运行集
        self.assertEqual(resolve_supported("   ", ["2.1.160"]), {"2.1.160"})
        self.assertEqual(resolve_supported(" , , ", ["2.1.158", "2.1.160"]),
                         {"2.1.158", "2.1.160"})


class TestComputeStale(unittest.TestCase):
    def test_dead_item_surfaces(self):
        # "dead" 不在 matched -> stale
        stale = compute_stale(
            all_trans_keys={"alive_a", "dead_b"},
            wl_srcs=["alive_c", "dead_wl"],
            matched={"alive_a", "alive_c"},
        )
        self.assertEqual(stale, ["dead_b", "dead_wl"])

    def test_all_matched_no_stale(self):
        stale = compute_stale({"a"}, ["b"], {"a", "b"})
        self.assertEqual(stale, [])

    def test_matched_superset_no_stale(self):
        # matched 含 union 之外的 (无关) 项 -> 不影响, stale 仍空
        stale = compute_stale({"a"}, ["b"], {"a", "b", "extra"})
        self.assertEqual(stale, [])


if __name__ == "__main__":
    unittest.main()
