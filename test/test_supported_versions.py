"""supported_versions 加载 + orig hash 断言层单元测试.

覆盖契约:
  - load_config: 文件缺失 → FileNotFoundError; 缺 'versions' → ValueError; 真配置结构合法
  - platform_key: 完整 npm 包名 / claude-code- 前缀 / 裸 arch 名 → 统一 arch 后缀
  - supported_versions: 版本序
  - expected_hash: 命中返回 hash; 缺 (版本/平台) → None
  - find_match: 反查命中 (version, platform); 未命中 → 空
  - assert_orig_supported: 命中 → 返回 tuple; 未命中 → raise; 未命中+force → None
  - cli_js_sha256: 对真缓存 binary 算出的 hash == 配置 (真环境, 缓存缺则 skip)

真实 (config × 真 binary) 一致性由 integration_test.py --supported 守 (hash 断言门)。

跑法: python3 -m unittest discover -s test -p 'test_*.py' -v
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import supported_versions as sv  # noqa: E402

FAKE_CFG = {
    "versions": {
        "2.1.161": {"darwin-arm64": "d" * 64, "linux-x64": "5" * 64},
        "2.1.152": {"darwin-arm64": "f" * 64, "linux-x64": "9" * 64, "win32-x64": "0" * 64},
    }
}

INTEGRATION_CACHE = Path.home() / ".cache" / "cc-l10n" / "integration"


class LoadConfigTests(unittest.TestCase):
    def test_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            sv.load_config(Path("/nonexistent/supported_versions.json"))

    def test_missing_versions_key_raises(self):
        import tempfile
        import json
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"foo": 1}, f)
            p = f.name
        with self.assertRaises(ValueError):
            sv.load_config(p)

    def test_real_config_structure(self):
        cfg = sv.load_config()  # 真 data/supported_versions.json
        self.assertIn("versions", cfg)
        self.assertTrue(cfg["versions"], "受支持版本集不应为空")
        for ver, plats in cfg["versions"].items():
            self.assertRegex(ver, r"^\d+\.\d+\.\d+$")
            self.assertTrue(plats, f"{ver} 应至少含一个平台")
            for pk, h in plats.items():
                self.assertRegex(h, r"^[0-9a-f]{64}$", f"{ver}/{pk} 应为 sha256 hex")


class PlatformKeyTests(unittest.TestCase):
    def test_full_npm_pkg(self):
        self.assertEqual(sv.platform_key("@anthropic-ai/claude-code-linux-x64"), "linux-x64")

    def test_cache_dir_name(self):
        self.assertEqual(sv.platform_key("claude-code-darwin-arm64"), "darwin-arm64")

    def test_bare_arch(self):
        self.assertEqual(sv.platform_key("win32-x64"), "win32-x64")


class LookupTests(unittest.TestCase):
    def test_supported_versions_sorted(self):
        self.assertEqual(sv.supported_versions(FAKE_CFG), ["2.1.152", "2.1.161"])

    def test_expected_hash_hit(self):
        self.assertEqual(sv.expected_hash(FAKE_CFG, "2.1.152", "win32-x64"), "0" * 64)

    def test_expected_hash_miss_version(self):
        self.assertIsNone(sv.expected_hash(FAKE_CFG, "9.9.9", "linux-x64"))

    def test_expected_hash_miss_platform(self):
        self.assertIsNone(sv.expected_hash(FAKE_CFG, "2.1.161", "win32-x64"))

    def test_find_match_hit(self):
        self.assertEqual(sv.find_match(FAKE_CFG, "f" * 64), [("2.1.152", "darwin-arm64")])

    def test_find_match_miss(self):
        self.assertEqual(sv.find_match(FAKE_CFG, "a" * 64), [])


class AssertOrigTests(unittest.TestCase):
    def setUp(self):
        # 隔离: 用假 cli_js_sha256, 测断言逻辑而非 hash 计算
        self._orig = sv.cli_js_sha256

    def tearDown(self):
        sv.cli_js_sha256 = self._orig

    def test_hit_returns_match(self):
        sv.cli_js_sha256 = lambda b: "9" * 64
        self.assertEqual(sv.assert_orig_supported("x", FAKE_CFG), ("2.1.152", "linux-x64"))

    def test_miss_raises(self):
        sv.cli_js_sha256 = lambda b: "a" * 64
        with self.assertRaises(ValueError):
            sv.assert_orig_supported("x", FAKE_CFG)

    def test_miss_force_returns_none(self):
        sv.cli_js_sha256 = lambda b: "a" * 64
        self.assertIsNone(sv.assert_orig_supported("x", FAKE_CFG, force=True))


class CliJsSha256RealTests(unittest.TestCase):
    """真环境: 对缓存 binary 算 hash, 须与真配置一致 (缓存缺则 skip — 非翻译漏检)."""

    def test_cached_binary_matches_config(self):
        cfg = sv.load_config()
        checked = 0
        for ver, plats in cfg["versions"].items():
            for pk, want in plats.items():
                binname = "claude.exe" if pk.startswith("win32") else "claude"
                binary = INTEGRATION_CACHE / f"claude-code-{pk}" / ver / binname
                if not binary.exists():
                    continue
                self.assertEqual(sv.cli_js_sha256(binary), want,
                                 f"{ver}/{pk} cli.js hash 与配置不符")
                checked += 1
        if checked == 0:
            self.skipTest("无缓存 binary (集成测试缓存未填充); 真一致性由 integration_test --supported 守")


if __name__ == "__main__":
    unittest.main()
