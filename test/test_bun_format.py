"""
bun_format 单元测试.

覆盖:
  - find_last_trailer: 单/多 trailer / 无 trailer raise
  - extract_cli_js: .js 直接读 / 未知格式 raise / hand-craft 假 Bun ELF + Mach-O
  - 常量 sanity (TRAILER bytes / OFFSETS_SIZE / MODULE_ENTRY_SIZE)

Fake Bun binary 布局:
  [0:4]            magic (Mach-O 或 ELF)         ← data_start = 4
  [4:4+cli_len]    cli.js
  [4+cli_len:...]  module_0 entry (52 bytes, contents SP 在 entry+8)
  ...              Offsets struct (32 bytes)
  ...              TRAILER (16 bytes)

跑法:
  python3 -m unittest discover -s test -p 'test_*.py' -v
"""
from __future__ import annotations

import struct
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import bun_format as bf  # noqa: E402
from bun_format import (  # noqa: E402
    MODULE_ENTRY_SIZE,
    OFFSETS_SIZE,
    TRAILER,
    extract_cli_js,
    find_last_trailer,
)


class FindLastTrailerTests(unittest.TestCase):
    def test_single_trailer(self):
        data = b"prefix" + TRAILER + b"suffix"
        self.assertEqual(find_last_trailer(data), 6)

    def test_multiple_trailers_returns_last(self):
        data = b"a" + TRAILER + b"middle" + TRAILER + b"end"
        expected = 1 + len(TRAILER) + len(b"middle")
        self.assertEqual(find_last_trailer(data), expected)

    def test_no_trailer_raises(self):
        with self.assertRaises(ValueError) as ctx:
            find_last_trailer(b"no trailer here, just some bytes")
        self.assertIn("trailer not found", str(ctx.exception))


class ExtractCliJsTests(unittest.TestCase):
    def _make_fake_bun_binary(self, magic: bytes, cli_js: bytes) -> bytes:
        assert len(magic) == 4
        cli_len = len(cli_js)
        out = bytearray(magic)
        out += cli_js
        mod_off = cli_len  # 相对 data_start = len(magic) = 4

        mod_entry = bytearray(MODULE_ENTRY_SIZE)
        struct.pack_into("<II", mod_entry, 8, 0, cli_len)  # contents SP @ entry+8
        out += mod_entry

        offsets_off = len(out)
        byte_count = offsets_off - len(magic)
        out += struct.pack("<IIIIIIII",
                           byte_count, 0, mod_off, MODULE_ENTRY_SIZE,
                           0, 0, 0, 0)
        out += TRAILER
        return bytes(out)

    def _write_tmp(self, data: bytes, suffix: str) -> Path:
        f = tempfile.NamedTemporaryFile("wb", suffix=suffix, delete=False)
        f.write(data)
        f.close()
        return Path(f.name)

    def test_js_file_returned_directly(self):
        path = self._write_tmp(b"const x = 1;", ".js")
        try:
            self.assertEqual(extract_cli_js(path), b"const x = 1;")
        finally:
            path.unlink()

    def test_unknown_format_raises(self):
        path = self._write_tmp(b"random bytes" + TRAILER, ".bin")
        try:
            with self.assertRaises(ValueError) as ctx:
                extract_cli_js(path)
            self.assertIn("unknown input format", str(ctx.exception))
        finally:
            path.unlink()

    def test_extract_from_fake_macho(self):
        cli = b"console.log('hi from macho');"
        binary = self._make_fake_bun_binary(b"\xcf\xfa\xed\xfe", cli)
        path = self._write_tmp(binary, ".bin")
        try:
            self.assertEqual(extract_cli_js(path), cli)
        finally:
            path.unlink()

    def test_extract_from_fake_macho_other_magic(self):
        """覆盖 MACHO_MAGICS 第二个 magic (0xcffaedfe LE)."""
        cli = b"alt magic body"
        binary = self._make_fake_bun_binary(b"\xfe\xed\xfa\xcf", cli)
        path = self._write_tmp(binary, ".bin")
        try:
            self.assertEqual(extract_cli_js(path), cli)
        finally:
            path.unlink()

    def test_extract_from_fake_elf(self):
        cli = b"another cli.js body 123"
        binary = self._make_fake_bun_binary(b"\x7fELF", cli)
        path = self._write_tmp(binary, ".bin")
        try:
            self.assertEqual(extract_cli_js(path), cli)
        finally:
            path.unlink()


class ConstantsTests(unittest.TestCase):
    def test_trailer_bytes(self):
        self.assertEqual(TRAILER, b"\n---- Bun! ----\n")
        self.assertEqual(len(TRAILER), 16)

    def test_struct_sizes(self):
        self.assertEqual(OFFSETS_SIZE, 32)
        self.assertEqual(MODULE_ENTRY_SIZE, 52)

    def test_macho_magics_two_endians(self):
        self.assertEqual(bf.MACHO_MAGICS, (0xfeedfacf, 0xcffaedfe))


if __name__ == "__main__":
    unittest.main()
