"""
按 Bun 1.3.14 的实际格式 unpack CC binary。

`@shepherdjerred/bun-decompile@0.2.0` 用的是旧 schema (MODULE_ENTRY_SIZE=36)。
Bun 新版本扩展到 52 字节, 加了 module_info 和 bytecode_origin_path 两个 StringPointer。

格式 (验证: modules.len = 572, 572/52 = 11 modules):
  CompiledModuleGraphFile (52 bytes):
    name:                  StringPointer (8)
    contents:              StringPointer (8)
    sourcemap:             StringPointer (8)
    bytecode:              StringPointer (8)
    module_info:           StringPointer (8)
    bytecode_origin_path:  StringPointer (8)
    loader:                u8
    module_format:         u8
    side:                  u8
    padding:               u8
  StringPointer (8 bytes): offset u32, length u32 (little-endian)

  Offsets (32 bytes, just before trailer):
    byteCount      u32  (实际 u32, 高 32 位由后面 padding 字段提供)
    padding        u32
    modulesPtr     StringPointer (8)
    entryPointId   u32
    argsPtr        StringPointer (8)
    flags          u32
"""
import argparse
import json
import pathlib
import struct

TRAILER = b"\n---- Bun! ----\n"
OFFSETS_SIZE = 32
STRING_PTR_SIZE = 8
MODULE_ENTRY_SIZE = 52


def find_trailer(data: bytes) -> int:
    """从尾部往前搜 8 MB 找 trailer"""
    search_limit = min(len(data), 8 * 1024 * 1024)
    for pos in range(len(data) - len(TRAILER), len(data) - search_limit, -1):
        if data[pos : pos + len(TRAILER)] == TRAILER:
            return pos
    raise ValueError("trailer not found")


def read_string_ptr(data: bytes, off: int) -> tuple[int, int]:
    return struct.unpack_from("<II", data, off)


def extract(data: bytes, data_start: int, off: int, ln: int) -> bytes:
    if ln == 0:
        return b""
    start = data_start + off
    end = start + ln
    if end > len(data):
        raise ValueError(f"pointer extends beyond buffer: off={off}, len={ln}, "
                         f"data_start={data_start}, computed end={end}, "
                         f"buffer={len(data)}")
    return data[start:end]


LOADER = {0: "jsx", 1: "js", 2: "ts", 3: "tsx", 4: "css", 5: "file",
          6: "json", 7: "toml", 8: "wasm", 9: "napi", 10: "text", 11: "sqlite"}
ENCODING = {0: "binary", 1: "latin1", 2: "utf8"}
MODULE_FORMAT = {0: "none", 1: "cjs", 2: "esm"}
SIDE = {0: "server", 1: "client"}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("binary", help="CC binary 路径 (Mach-O 或 ELF)")
    ap.add_argument("--out", default="decompiled",
                    help="输出目录 (默认: ./decompiled)")
    args = ap.parse_args()
    bin_path = pathlib.Path(args.binary)
    out_dir = pathlib.Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"读取 {bin_path} ...")
    data = bin_path.read_bytes()
    print(f"file size: {len(data):,}")

    trailer_pos = find_trailer(data)
    print(f"trailer at: {trailer_pos:,} (0x{trailer_pos:x})")

    # 读 Offsets
    offsets_start = trailer_pos - OFFSETS_SIZE
    byte_count, pad, mod_off, mod_len, entry_id, args_off, args_len, flags = \
        struct.unpack_from("<IIIIIIII", data, offsets_start)

    print(f"Offsets:")
    print(f"  byte_count    = {byte_count:,}")
    print(f"  modules_ptr   = off={mod_off:,}, len={mod_len}")
    print(f"  entry_point_id= {entry_id}")
    print(f"  args_ptr      = off={args_off:,}, len={args_len}")
    print(f"  flags         = 0x{flags:x}")

    # 计算 data_start
    data_start = trailer_pos - OFFSETS_SIZE - byte_count
    print(f"data_start    = {data_start:,} (0x{data_start:x})")

    if mod_len % MODULE_ENTRY_SIZE != 0:
        print(f"警告: modules len ({mod_len}) 不能被 {MODULE_ENTRY_SIZE} 整除")
        # 尝试其他大小
        for sz in (36, 40, 44, 48, 52, 56, 60, 64):
            if mod_len % sz == 0:
                print(f"   可整除大小候选: {sz} -> {mod_len // sz} modules")
    mod_count = mod_len // MODULE_ENTRY_SIZE
    print(f"module count  = {mod_count}")

    modules_start = data_start + mod_off
    modules = []
    for i in range(mod_count):
        entry_off = modules_start + i * MODULE_ENTRY_SIZE
        fields = struct.unpack_from("<IIIIIIIIIIII4B", data, entry_off)
        (name_off, name_len, cont_off, cont_len, smap_off, smap_len,
         bc_off, bc_len, minfo_off, minfo_len, bop_off, bop_len,
         loader, mod_fmt, side, padding_byte) = fields

        name_bytes = extract(data, data_start, name_off, name_len)
        try:
            name = name_bytes.decode("utf-8")
        except UnicodeDecodeError:
            name = name_bytes.decode("utf-8", errors="replace")

        modules.append({
            "i": i,
            "name": name,
            "contents": (cont_off, cont_len),
            "sourcemap": (smap_off, smap_len),
            "bytecode": (bc_off, bc_len),
            "module_info": (minfo_off, minfo_len),
            "bop": (bop_off, bop_len),
            "loader": LOADER.get(loader, f"?{loader}"),
            "module_format": MODULE_FORMAT.get(mod_fmt, f"?{mod_fmt}"),
            "side": SIDE.get(side, f"?{side}"),
        })

    print(f"\n=== Modules ({len(modules)}) ===")
    for m in modules:
        marker = " [ENTRY]" if m["i"] == entry_id else ""
        print(f"  #{m['i']:2d} {m['loader']:5s} {m['module_format']:4s} "
              f"contents={m['contents'][1]:>10,}  bc={m['bytecode'][1]:>10,}  "
              f"{m['name']}{marker}")

    # 提取每个 module 的 contents 到磁盘
    print(f"\n输出目录: {out_dir}")
    for m in modules:
        # 用 module name 推 output path; 替换不安全字符
        safe_name = m["name"].lstrip("/").replace("/", "_")
        if not safe_name:
            safe_name = f"module_{m['i']}"

        cont_bytes = extract(data, data_start, *m["contents"])
        out_path = out_dir / safe_name
        out_path.write_bytes(cont_bytes)
        print(f"  写入 {out_path.name}: {len(cont_bytes):,} 字节")

        if m["sourcemap"][1] > 0:
            smap_bytes = extract(data, data_start, *m["sourcemap"])
            (out_dir / f"{safe_name}.map").write_bytes(smap_bytes)

        if m["bytecode"][1] > 0:
            bc_bytes = extract(data, data_start, *m["bytecode"])
            (out_dir / f"{safe_name}.jsc").write_bytes(bc_bytes)

    # 写元数据
    (out_dir / "metadata.json").write_text(json.dumps({
        "bun_version": "1.3.14 (assumed)",
        "byte_count": byte_count,
        "module_count": mod_count,
        "entry_point_id": entry_id,
        "flags": flags,
        "modules": modules,
    }, indent=2))


if __name__ == "__main__":
    main()
