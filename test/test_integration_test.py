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
    def test_csv_explicit(self):
        s = resolve_supported("2.1.150, 2.1.160 ,2.1.158")
        self.assertEqual(s, {"2.1.150", "2.1.160", "2.1.158"})

    def test_default_uses_dist_tags(self):
        # 无 CSV -> 调 dist_tags_fn (此处 stub, 不碰网络)
        s = resolve_supported(None, dist_tags_fn=lambda: ["2.1.150", "2.1.160"])
        self.assertEqual(s, {"2.1.150", "2.1.160"})

    def test_empty_csv_falls_back(self):
        # 空串视为未指定? 实现: 空 CSV -> {} (split 出空). 契约: 空串不当默认。
        s = resolve_supported("", dist_tags_fn=lambda: ["2.1.160"])
        self.assertEqual(s, {"2.1.160"})


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
