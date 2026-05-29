"""
sentinel 变量名解析单元测试.

覆盖 `resolve_sentinels` / `_VAR_RE` / `_VAR_NAME_CHARS` 对 JS minifier 产物的健壮性.

关键 edge case:
  - 变量名为单个 `$` (minifier 把 `$` 作为合法标识符再分配)
  - 变量名含 `.` (成员访问, 如 `${st.agentType}`)
  - 反向模式 (before=b"") 回溯 `${$}`
  - 多实例 sentinel 中某个实例为 `${$}` (powered-by-model 段在 2.1.143)

历史 bug:
  `_VAR_RE = r"\\$\\{([\\w.]+)\\}"` 用 `\\w = [A-Za-z0-9_]` 漏掉 `$`, 导致 cli.js
  中 `${$}` 形式变量被静默跳过, sentinel zip 计数不对齐 (Linux 2.1.143 上
  __MODEL_NAME_VAR__ 只 resolve 2/3 实例, Linux 2.1.156 上 __TASKCREATE_TOOL__
  / __MEMORY_DIR_VAR__ 完全 0/1).

跑法:
  python3 test/test_sentinel_resolve.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import repack_universal as ru  # noqa: E402


class TestVarRegex(unittest.TestCase):
    """_VAR_RE / _VAR_NAME_CHARS 字符集"""

    def test_single_dollar_var(self):
        """`${$}` 必须能被 _VAR_RE 识别, 提取变量名 `$`."""
        m = ru._VAR_RE.match(b"${$}xxx")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), b"$")

    def test_dollar_in_name_chars(self):
        """反向模式回溯使用的字符集必须含 `$`."""
        self.assertIn(ord("$"), ru._VAR_NAME_CHARS)

    def test_mixed_name_with_dollar(self):
        """`${$x9}` (以 $ 开头的多字符标识符) 也应解析为 `$x9`."""
        m = ru._VAR_RE.match(b"${$x9}rest")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), b"$x9")

    def test_member_access_var(self):
        """`${st.agentType}` 成员访问形态保持兼容."""
        m = ru._VAR_RE.match(b"${st.agentType},more")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), b"st.agentType")


class TestResolveSentinels(unittest.TestCase):
    """resolve_sentinels: forward / reverse / multi-instance"""

    def _patch_resolvers(self, resolvers):
        return patch.object(ru, "SENTINEL_RESOLVERS", resolvers)

    def test_forward_anchor_dollar_var(self):
        """forward 模式 (before 非空) 解析 `${$}`."""
        resolvers = [("__T__", b"foo ", b" bar")]
        with self._patch_resolvers(resolvers):
            sm = ru.resolve_sentinels(b"prefix foo ${$} bar tail")
        self.assertEqual(sm, {"__T__": [b"${$}"]})

    def test_reverse_anchor_dollar_var(self):
        """reverse 模式 (before=b'') 回溯 `${$}`."""
        resolvers = [("__T__", b"", b" tail")]
        with self._patch_resolvers(resolvers):
            sm = ru.resolve_sentinels(b"prefix ${$} tail more")
        self.assertEqual(sm, {"__T__": [b"${$}"]})

    def test_multi_instance_mixed_var_names(self):
        """同一 sentinel 三次声明, cli.js 三处分别 `${f}` `${M}` `${$}` —
        模拟 Linux 2.1.143 powered-by-model 段."""
        anchor_before = b"named "
        anchor_after = b" suffix"
        resolvers = [
            ("__N__", anchor_before, anchor_after),
            ("__N__", anchor_before, anchor_after),
            ("__N__", anchor_before, anchor_after),
        ]
        cli = (b"x named ${f} suffix y "
               b"named ${M} suffix z "
               b"named ${$} suffix end")
        with self._patch_resolvers(resolvers):
            sm = ru.resolve_sentinels(cli)
        self.assertEqual(sm, {"__N__": [b"${f}", b"${M}", b"${$}"]})

    def test_reverse_anchor_unique_match(self):
        """reverse 模式: after 唯一出现, 回溯紧邻的 `${$}`."""
        resolvers = [("__TC__", b"", b" to plan")]
        cli = b"prefix `Use ${$} to plan and more"
        with self._patch_resolvers(resolvers):
            sm = ru.resolve_sentinels(cli)
        self.assertEqual(sm, {"__TC__": [b"${$}"]})

    def test_forward_no_match_when_after_mismatch(self):
        """`${X}` 紧随但 after 不符 → 跳过, cursor 推进, 不无限循环."""
        resolvers = [("__T__", b"foo ", b" bar")]
        cli = b"foo ${$} XYZ no_bar"
        with self._patch_resolvers(resolvers):
            sm = ru.resolve_sentinels(cli)
        self.assertEqual(sm, {})

    def test_reverse_anchor_no_preceding_var(self):
        """reverse 模式: after 前不是 `${...}` 结构 → 不匹配."""
        resolvers = [("__T__", b"", b" tail")]
        cli = b"literal text tail more"
        with self._patch_resolvers(resolvers):
            sm = ru.resolve_sentinels(cli)
        self.assertEqual(sm, {})


class TestSentinelsIn(unittest.TestCase):
    """_sentinels_in: translations 文本中 sentinel 名提取"""

    def test_sentinel_names_extracted(self):
        """合法 sentinel 名 (在 SENTINEL_RESOLVERS 中声明的) 被提取."""
        text = b"hello ${__BASH_TOOL__} world ${__AGENT_TOOL__}"
        result = ru._sentinels_in(text)
        self.assertEqual(set(result), {"__BASH_TOOL__", "__AGENT_TOOL__"})

    def test_dollar_only_var_not_treated_as_sentinel(self):
        """`${$}` 不在 SENTINEL_RESOLVERS 中, 不应被当作 sentinel."""
        text = b"raw placeholder ${$} in text"
        result = ru._sentinels_in(text)
        self.assertEqual(result, [])

    def test_unknown_var_not_sentinel(self):
        """未在 declared_names 中的名字被过滤."""
        text = b"x ${SOME_UNKNOWN_VAR} y"
        result = ru._sentinels_in(text)
        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()
