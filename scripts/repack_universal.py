"""
Universal repack: 支持 macOS Mach-O 与 Linux ELF Bun standalone binary。

接受 translations dict (英文 -> 中文) 或多组替换, 自动:
  1. 找 trailer + Offsets + BlobHeader
  2. 找包含 payload 的 segment (Mach-O LC_SEGMENT_64 / ELF PT_LOAD)
  3. 应用替换到 cli.js contents
  4. 重新分配 segment offsets, 更新 module entries 的 StringPointer
  5. 更新 Offsets + BlobHeader
  6. NUL padding 保持 segment 大小不变
  7. (macOS) 触发 codesign --force --deep --sign -

使用:
  from repack_universal import repack
  repack(input_bin, output_bin, translations={b"old": b"new"})
"""
import bisect
import json
import re
import shutil
import struct
import subprocess
import sys
from collections import Counter
from pathlib import Path

from bun_format import translation_files
from js_codec import decode_literal, encode_dst_decoded
from js_string_scanner import scan_string_literals
from log_setup import setup_logging

log = setup_logging(__name__)

PLACEHOLDER_RE = re.compile(rb"\$\{[^{}]*(?:\{[^}]*\}[^{}]*)*\}")


def _src_forms(src: bytes) -> list[tuple[bytes, str]]:
    """生成 (form_bytes, lit_type) 列表, 用于在 cli.js 中定位 src (子串/sentinel 兜底路)。

    raw form (src 原样, 含 live ${var}) 始终为第一项 — sentinel 跨插值的 src 在此命中。
    其余为 encode_dst_decoded 生成的 dq/sq/tmpl 形态 (非 ASCII raw + \\u 两种), 覆盖
    decoded-form 子串 key 匹配转义字面量的情形。与 dst 写回用同一个 encode_dst_decoded。
    """
    forms: list[tuple[bytes, str]] = [(src, "raw")]
    seen_bytes = {bytes(src)}
    try:
        src.decode("utf-8")
    except UnicodeDecodeError:
        return forms
    for lit_type in ("dq", "sq", "tmpl"):
        for esc_u in (False, True):
            esc = encode_dst_decoded(src, lit_type, escape_unicode=esc_u)
            if esc not in seen_bytes:
                seen_bytes.add(esc)
                forms.append((esc, lit_type))
    return forms


def find_src_with_escape(cli_bytes: bytes, src: bytes) -> list[tuple[int, bytes, str]]:
    """返回 [(pos, form_bytes, lit_type)] 列表, 覆盖 raw + 各字面量转义形式.

    每个 hit 携带实际找到的字节形式 (用于计算替换范围) 与 lit_type
    (用于把 dst 用 encode_dst_decoded 编码为同形式)。
    """
    hits: list[tuple[int, bytes, str]] = []
    for form_bytes, lit_type in _src_forms(src):
        pos = 0
        while True:
            i = cli_bytes.find(form_bytes, pos)
            if i < 0:
                break
            hits.append((i, form_bytes, lit_type))
            pos = i + 1
    return hits


def load_translations(path, platform: str | None = None):
    """
    加载 translations. path 可以是:
      - 单个 .json 文件 (legacy: translations_full.json)
      - 目录 (推荐: data/translations/, 加载根目录所有 *.json 并合并)

    platform (linux/darwin/win32): 额外加载 <path>/<platform>/*.json 平台专属译文,
    仅对该平台 binary 生效。

    单个文件内每条 entry:
      "src": "dst"                                  # 普通: 严格顺序匹配 placeholder
      "src": {"dst": "...", "allow_reorder": true}  # 扩展: 允许 placeholder 重排

    以 `_` 开头的 key 跳过 (供文件级 metadata 使用, 例如 _meta / _comment).

    多文件同 src 重复定义会抛错 (人为避免重复).

    返回 list of (src_bytes, dst_bytes, allow_reorder).
    """
    p = Path(path)
    if not (p.is_dir() or p.is_file()):
        raise FileNotFoundError(f"翻译路径不存在: {p}")
    files = translation_files(p, platform)
    if p.is_dir() and not files:
        raise ValueError(f"目录 {p} 中没有 *.json 翻译文件")

    entries = []
    seen_keys = {}  # src -> file path (用于检测重复)
    for fp in files:
        raw = json.loads(fp.read_text())
        for k, v in raw.items():
            if k.startswith("_"):
                continue
            if k in seen_keys:
                raise ValueError(
                    f"翻译 src 在多个文件中重复定义:\n"
                    f"  src: {k[:80]!r}\n"
                    f"  first: {seen_keys[k]}\n"
                    f"  duplicate: {fp}")
            seen_keys[k] = fp
            if isinstance(v, str):
                entries.append((k.encode("utf-8"), v.encode("utf-8"), False))
            elif isinstance(v, dict):
                dst = v["dst"]
                allow_reorder = bool(v.get("allow_reorder", False))
                entries.append((k.encode("utf-8"), dst.encode("utf-8"), allow_reorder))
            else:
                raise ValueError(f"invalid trans value for {k[:60]!r} in {fp}: type={type(v)}")
    return entries

# Sentinel resolver (anchor-offset 模型):
#   每条 (sentinel_name, before, after).
#   在 cli.js 中匹配 <before><${X}><after> 三段连续模式, 提取中间的 ${X} 作为 sentinel 实际值.
#   before/after 都是普通字节串, 不含 ${X} 本身, 越独特越好.
#
# 同一 sentinel_name 可出现多次, 每次对应 cli.js 中一个独立 anchor 实例.
# 实例顺序按 SENTINEL_RESOLVERS 中的声明顺序对齐 (跨 sentinel 同 index 视为"同一位置"),
# 用于覆盖同一段 prompt 在 cli.js 中出现多份且变量名各不同的场景 (例如 powered-by-model
# 段在 cli.js 中有 3 处 named + 3 处 fallback, 每处局部变量名都不同).
#
# 优势 vs 旧 regex 模型:
#   1. anchor 不含 ${X} 占位, 更易写, 更不易碰撞
#   2. 严格三段连续匹配, 不会因变量名变化而失效
#   3. before/after 任一为空时仍可单边定位 (传 b"" 即可)
SENTINEL_RESOLVERS = [
    # (sentinel_name,          before_anchor,                                  after_anchor)
    ("__BASH_TOOL__",          b"Prefer dedicated tools over ",                b" when one fits"),
    ("__TOOL_LIST__",          b" when one fits (",                            b") \\u2014 reserve"),
    ("__TASKCREATE_TOOL__",    b"",                                            b" to plan and track work. Mark each task"),
    ("__AGENT_TOOL__",         b"",                                            b" tool with specialized agents when the task"),
    ("__SKILL_TOOL__",         b"\\`/<skill-name>\\`, invoke it via ",         b". Only use skills"),
    ("__QUERY_THRESHOLD__",    b"that'll take more than ",                     b" queries, spawn"),
    ("__SUBAGENT_TYPE__",      b" with subagent_type=",                        b". Otherwise use"),
    ("__SEARCH_TOOL__",        b". Otherwise use ",                            b" directly."),
    # memory-system 用 (#18/#38/#39 段)
    ("__MEMORY_DIR_VAR__",     b"file-based memory system at \\`",             b"\\`. "),
    ("__MEMORY_INDEX_VAR__",   b"add a pointer to that file in \\`",           b"\\` in the private directory"),
    ("__MAX_INDEX_LINES_VAR__",b" is loaded into your conversation context \\u2014 lines after ", b" will be truncated"),
    # Language 段: `# Language\nAlways respond in ${q}. Use ${q} for ...`
    ("__LANGUAGE_VAR__",       b"# Language\nAlways respond in ",              b". Use "),
    # powered-by-model 段 — 6 处, 名/id 各 3 实例
    # 形态 A (named, 3 处): `You are powered by the model named ${NAME}. The exact model ID is ${ID}.`
    # 形态 B (fallback, 3 处): `You are powered by the model ${ID2}.`
    # 关键: A 与 B 在 cli.js 中三元 (cond?A:B) 紧邻, 顺序 A1 B1 A2 B2 A3 B3.
    # NAME / ID / ID2 每实例的局部变量名都不同 (跨平台跨版本都会变), 需多 anchor.
    ("__MODEL_NAME_VAR__",     b"?`You are powered by the model named ",       b". The exact model ID is ${"),
    ("__MODEL_NAME_VAR__",     b"?`You are powered by the model named ",       b". The exact model ID is ${"),
    ("__MODEL_NAME_VAR__",     b"?`You are powered by the model named ",       b". The exact model ID is ${"),
    ("__MODEL_ID_VAR__",       b". The exact model ID is ",                    b".`:`You are powered by the model ${"),
    ("__MODEL_ID_VAR__",       b". The exact model ID is ",                    b".`:`You are powered by the model ${"),
    ("__MODEL_ID_VAR__",       b". The exact model ID is ",                    b".`:`You are powered by the model ${"),
    ("__MODEL_ID_FB_VAR__",    b".`:`You are powered by the model ",           b".`"),
    ("__MODEL_ID_FB_VAR__",    b".`:`You are powered by the model ",           b".`"),
    ("__MODEL_ID_FB_VAR__",    b".`:`You are powered by the model ",           b".`"),
]

# JS 标识符首字符可以是 `$`, minifier 经常用单个 `$` 作变量名 (出现在 cli.js 中如 `${$}`).
# 因此变量名字符集必须包含 `$`, 不能只用 `\w`. 否则反向模式 (before=b"") 与 _VAR_RE.match
# 都会漏掉 `${$}` 形态的 placeholder, 导致 sentinel zip 不对齐.
_VAR_RE = re.compile(rb"\$\{([\w.$]+)\}")
_VAR_NAME_CHARS = set(b"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.$")


def resolve_sentinels(cli_bytes: bytes) -> dict[str, list[bytes]]:
    """
    用 anchor 模型从 cli.js 中提取每个 sentinel 对应的实际 ${...} 变量.

    SENTINEL_RESOLVERS 中相同 sentinel_name 可出现多次, 每次按 cli.js 中出现顺序消费下一个匹配位置:
      - 同名 sentinel 第 k 次声明 -> 取 cli.js 中第 k 次匹配出的变量
    单次声明的 sentinel 退化为旧行为 (返回长度 1 的 list).

    策略 (按 anchor 形态自动选):
      - 若 before 非空: 找 before 在 cli.js 中下一个位置 -> 紧跟 ${X} -> 紧跟 after
      - 若 before 为空 (b""): 用 after 反向定位, 找 after 在 cli.js 中下一个位置 ->
        向前回溯紧贴的 ${X}

    返回 dict: { "__SENTINEL__": [b"${X1}", b"${X2}", ...] }
    """
    # 记录每个 sentinel 已消费到的搜索起点, 实现"同名第 k 次声明 -> cli.js 中第 k 处匹配"
    cursor: dict[str, int] = {}
    resolved: dict[str, list[bytes]] = {}
    for name, before, after in SENTINEL_RESOLVERS:
        start = cursor.get(name, 0)
        if before:
            i = cli_bytes.find(before, start)
            if i < 0:
                continue
            scan_from = i + len(before)
            m = _VAR_RE.match(cli_bytes, scan_from)
            if not m:
                # 匹配位置不合规, 跳过该 anchor 但仍推进 cursor 避免下次又找同处
                cursor[name] = i + 1
                continue
            end = m.end()
            if cli_bytes[end:end + len(after)] != after:
                cursor[name] = i + 1
                continue
            resolved.setdefault(name, []).append(b"${" + m.group(1) + b"}")
            cursor[name] = end + len(after)
        else:
            i = cli_bytes.find(after, start)
            if i < 0:
                continue
            if i < 4 or cli_bytes[i-1] != ord('}'):
                cursor[name] = i + 1
                continue
            j = i - 2
            while j > 1 and cli_bytes[j] in _VAR_NAME_CHARS:
                j -= 1
            if cli_bytes[j-1:j+1] != b"${":
                cursor[name] = i + 1
                continue
            var_name = cli_bytes[j+1:i-1]
            if not var_name:
                cursor[name] = i + 1
                continue
            resolved.setdefault(name, []).append(b"${" + var_name + b"}")
            cursor[name] = i + len(after)
    return resolved


def _sentinels_in(text: bytes) -> list[str]:
    """返回 text 中所有 ${__SENTINEL__} 名 (去重, 保留首次顺序)"""
    declared_names = {n for n, _, _ in SENTINEL_RESOLVERS}
    seen = []
    seen_set = set()
    for m in _VAR_RE.finditer(text):
        n = m.group(1).decode()
        if n in declared_names and n not in seen_set:
            seen.append(n)
            seen_set.add(n)
    return seen


def expand_translations(translations, sentinel_map: dict[str, list[bytes]]):
    """
    展开含 sentinel 的 translation entry 为多实例.

    输入: translations = list[(src_bytes, dst_bytes, allow_reorder)]
          sentinel_map = dict[name_str -> list[bytes_var]]
    输出: 同形态 list, 但 src/dst 中的 sentinel 已替换为实际变量名;
          若一条 src 含 sentinel 且该 sentinel 在 cli.js 中有 N 实例, 该条展开为 N 条.

    一条 src 含多个 sentinel 时要求实例数对齐 (按 index zip).
    sentinel 名未在 cli.js 中解析到任何实例 -> 该 entry 跳过 (不展开, 不报错; 由上层 'unresolved' 检测).

    去重: 同一 (src, dst, ar) 多实例展开后若得到相同字节串, 只保留一条 (避免下游
    把同一位置匹配多次, 触发 risk_d hit-overlap 冲突). 这发生在多实例中某 sentinel
    解析为同一变量名的情况 (比如 fallback 形态在 macOS 上 3 实例都是 ${H}).
    """
    out = []
    seen_in_entry: set
    for src, dst, ar in translations:
        names_in_src = _sentinels_in(src)
        if not names_in_src:
            out.append((src, dst, ar))
            continue
        # 收集每个 sentinel 的实例数
        counts = [len(sentinel_map.get(n, [])) for n in names_in_src]
        if any(c == 0 for c in counts):
            # 至少一个 sentinel 未解析, 跳过 (上层 unresolved 检测会报)
            continue
        n_instances = counts[0]
        if any(c != n_instances for c in counts):
            raise ValueError(
                f"sentinel 实例数不对齐: src 含 {names_in_src}, "
                f"各实例数 {counts}, 无法 zip 展开")
        seen_in_entry = set()
        for k in range(n_instances):
            new_src = src
            new_dst = dst
            for n in names_in_src:
                placeholder = f"${{{n}}}".encode()
                actual = sentinel_map[n][k]
                new_src = new_src.replace(placeholder, actual)
                new_dst = new_dst.replace(placeholder, actual)
            key = (new_src, new_dst)
            if key in seen_in_entry:
                continue
            seen_in_entry.add(key)
            out.append((new_src, new_dst, ar))
    return out

TRAILER = b"\n---- Bun! ----\n"
OFFSETS_SIZE = 32
MODULE_ENTRY_SIZE = 52
SP_FIELDS = ["name", "contents", "sourcemap", "bytecode", "module_info", "bop"]
SP_OFFSETS = [0, 8, 16, 24, 32, 40]


def _find_last_trailer(data: bytes) -> int:
    """从尾部往前找最后一次 trailer 出现 (避开 runtime 常量副本)"""
    last = -1
    pos = 0
    while True:
        i = data.find(TRAILER, pos)
        if i < 0:
            break
        last = i
        pos = i + 1
    if last < 0:
        raise ValueError("trailer not found")
    return last


def _find_containing_segment_macho(data: bytes, target_off: int) -> tuple[int, int]:
    """解析 Mach-O LC_SEGMENT_64, 找包含 target_off 的段, 返回 (seg_start, seg_end)"""
    # Mach-O 64 header: magic(4) cputype(4) cpusubtype(4) filetype(4)
    #                   ncmds(4) sizeofcmds(4) flags(4) reserved(4)
    magic = struct.unpack_from("<I", data, 0)[0]
    if magic not in (0xfeedfacf, 0xcffaedfe):
        raise ValueError(f"not Mach-O 64: magic=0x{magic:x}")
    ncmds = struct.unpack_from("<I", data, 16)[0]

    cursor = 32  # mach_header_64 size
    for _ in range(ncmds):
        cmd, cmdsize = struct.unpack_from("<II", data, cursor)
        if cmd == 0x19:  # LC_SEGMENT_64
            # segname(16) vmaddr(8) vmsize(8) fileoff(8) filesize(8) ...
            fileoff = struct.unpack_from("<Q", data, cursor + 24 + 16)[0]
            filesize = struct.unpack_from("<Q", data, cursor + 24 + 24)[0]
            if fileoff <= target_off < fileoff + filesize:
                return (fileoff, fileoff + filesize)
        cursor += cmdsize
    raise ValueError(f"no segment contains offset {target_off}")


def _find_containing_segment_elf(data: bytes, target_off: int) -> tuple[int, int]:
    """解析 ELF64 program headers, 找包含 target_off 的 PT_LOAD"""
    if not (data[:4] == b"\x7fELF" and data[4] == 2):
        raise ValueError(f"not ELF64: magic={data[:5]!r}")
    e_phoff = struct.unpack_from("<Q", data, 32)[0]
    e_phentsize = struct.unpack_from("<H", data, 54)[0]
    e_phnum = struct.unpack_from("<H", data, 56)[0]
    for i in range(e_phnum):
        ph = data[e_phoff + i * e_phentsize : e_phoff + (i + 1) * e_phentsize]
        p_type, _p_flags, p_offset, _vaddr, _paddr, p_filesz, _p_memsz, _p_align = \
            struct.unpack("<IIQQQQQQ", ph)
        if p_type == 1 and p_offset <= target_off < p_offset + p_filesz:
            return (p_offset, p_offset + p_filesz)
    raise ValueError(f"no PT_LOAD contains offset {target_off}")


def _find_containing_segment_pe(data: bytes, target_off: int) -> tuple[int, int]:
    """解析 Windows PE section table, 找包含 target_off 的节 (Bun payload 在 .bun 节).

    返回该节的 (raw_offset, raw_offset + raw_size) — 与 ELF/Mach-O segment 类比,
    repack 在此节内 NUL-padding 保持节大小不变。
    """
    if data[:2] != b"MZ":
        raise ValueError(f"not PE: magic={data[:2]!r}")
    e_lfanew = struct.unpack_from("<I", data, 0x3C)[0]
    if data[e_lfanew:e_lfanew + 4] != b"PE\x00\x00":
        raise ValueError(f"bad PE signature @ {e_lfanew}")
    coff = e_lfanew + 4
    n_sections = struct.unpack_from("<H", data, coff + 2)[0]
    size_opt_header = struct.unpack_from("<H", data, coff + 16)[0]
    sect_table = coff + 20 + size_opt_header
    for i in range(n_sections):
        so = sect_table + i * 40
        # name(8) virt_size(4) virt_addr(4) raw_size(4) raw_off(4) ...
        raw_size, raw_off = struct.unpack_from("<II", data, so + 16)
        if raw_off <= target_off < raw_off + raw_size:
            return (raw_off, raw_off + raw_size)
    raise ValueError(f"no PE section contains offset {target_off}")


def _detect_format(data: bytes) -> str:
    if data[:4] == b"\x7fELF":
        return "elf"
    magic = struct.unpack_from("<I", data, 0)[0]
    if magic in (0xfeedfacf, 0xcffaedfe):
        return "macho"
    if data[:2] == b"MZ":
        return "pe"
    raise ValueError(f"unknown format: {data[:8]!r}")


def repack(input_bin: Path, output_bin: Path,
           translations,
           codesign: bool = True) -> dict:
    """
    主入口. 返回执行 summary dict.

    translations 接受两种形态:
      - dict[bytes, bytes]                 (legacy, allow_reorder 默认 False)
      - list[(src, dst, allow_reorder)]    (推荐, 来自 load_translations)
    """
    # 规范化为 list of tuples
    if isinstance(translations, dict):
        translations = [(k, v, False) for k, v in translations.items()]
    data = bytearray(Path(input_bin).read_bytes())
    fmt = _detect_format(data)
    log.info(f"format: {fmt}, size: {len(data):,}")

    trailer_pos = _find_last_trailer(data)
    log.info(f"trailer at: {trailer_pos:,}")

    offsets_off = trailer_pos - OFFSETS_SIZE
    byte_count, _pad, mod_off, mod_len, entry_id, args_off, args_len, flags = \
        struct.unpack_from("<IIIIIIII", data, offsets_off)
    data_start = offsets_off - byte_count
    blob_header_pos = data_start - 8

    # 校验 BlobHeader
    blob_header = struct.unpack_from("<Q", data, blob_header_pos)[0]
    expected_bh = byte_count + OFFSETS_SIZE + len(TRAILER)
    if blob_header != expected_bh:
        raise ValueError(f"BlobHeader mismatch: {blob_header} vs {expected_bh}")

    # 包含 payload 的 segment/section 范围
    if fmt == "macho":
        seg_start, seg_end = _find_containing_segment_macho(bytes(data), data_start)
    elif fmt == "pe":
        seg_start, seg_end = _find_containing_segment_pe(bytes(data), data_start)
    else:
        seg_start, seg_end = _find_containing_segment_elf(bytes(data), data_start)
    log.info(f"containing segment: [{seg_start:,}, {seg_end:,}), size={seg_end - seg_start:,}")

    # 解析所有 segments
    modules_start = data_start + mod_off
    mod_count = mod_len // MODULE_ENTRY_SIZE
    segments = []
    for i in range(mod_count):
        entry_off = modules_start + i * MODULE_ENTRY_SIZE
        for fname, sp_off in zip(SP_FIELDS, SP_OFFSETS):
            off, ln = struct.unpack_from("<II", data, entry_off + sp_off)
            if ln > 0:
                owner_loc = (entry_off - data_start) + sp_off
                segments.append({"off": off, "len": ln,
                                "label": f"module_{i}_{fname}",
                                "owner_loc": owner_loc})
    if args_len > 0:
        segments.append({"off": args_off, "len": args_len, "label": "args"})
    segments.append({"off": mod_off, "len": mod_len, "label": "modules_array"})
    segments.sort(key=lambda s: s["off"])

    # 找 cli.js 段 (module_0_contents)
    cli_seg = next(s for s in segments if s["label"] == "module_0_contents")
    cli_bytes = bytes(data[data_start + cli_seg["off"] : data_start + cli_seg["off"] + cli_seg["len"]])

    # 扫描原 cli.js 一次, 拿所有字符串字面量范围
    log.info("扫描 cli.js 字符串字面量...")
    literal_ranges = list(scan_string_literals(cli_bytes))
    lit_starts = [r[0] for r in literal_ranges]
    lit_ends_by_idx = [r[1] for r in literal_ranges]
    log.info(f"  共 {len(literal_ranges):,} 个字面量, 占 cli.js "
             f"{sum(e-s for s,e,_ in literal_ranges)*100//len(cli_bytes)}%")

    # decode-match 索引: decoded 文本 -> [(start, end, lit_type)]。
    # 整字面量 trans key (canonical 形态) 直接在此精确命中, 对 tmpl_frag/em-dash/反引号鲁棒。
    # 非整字面量 (子串 / sentinel 跨插值) 走 find_src_with_escape 兜底。
    decoded_index: dict[str, list[tuple[int, int, str]]] = {}
    for _s, _e, _lt in literal_ranges:
        _t = decode_literal(cli_bytes, _s, _e, _lt)
        if _t is not None:
            decoded_index.setdefault(_t, []).append((_s, _e, _lt))

    def in_literal(i: int, j: int) -> bool:
        """检查 [i, j) 是否完全落在某个字面量内 (基于原 cli.js offsets)"""
        if not lit_starts:
            return False
        idx = bisect.bisect_right(lit_starts, i) - 1
        if idx < 0:
            return False
        s, e = lit_starts[idx], lit_ends_by_idx[idx]
        return s <= i and j <= e

    # 解析 sentinel 占位符 (跨平台/跨版本通用): 从 cli.js 提取真实变量名,
    # 把 translations 中的 ${__SENTINEL__} 实例化为目标 binary 的实际变量名
    sentinel_map = resolve_sentinels(cli_bytes)
    if sentinel_map:
        total_inst = sum(len(v) for v in sentinel_map.values())
        log.info(f"识别 sentinel: {len(sentinel_map)} 个 / {total_inst} 实例")
        for name, vars_ in sentinel_map.items():
            if len(vars_) == 1:
                log.info(f"  ${{{name}}} -> {vars_[0].decode()}")
            else:
                log.info(f"  ${{{name}}} -> {[v.decode() for v in vars_]} ({len(vars_)} 实例)")
        # 检查哪些 sentinel 在 trans 中被引用但未被解析 (用原始 sentinel 形态做检测)
        used = set()
        for src, _, _ in translations:
            for n in _sentinels_in(src):
                used.add(n)
        unresolved_used = used - set(sentinel_map)
        if unresolved_used:
            log.warning(f"[WARN] 这些 sentinel 在 trans 中被引用但 cli.js 找不到 anchor: {unresolved_used}")
        # 展开 sentinel: 单实例直接替换, 多实例对每个 anchor 实例生成一条 trans
        translations = expand_translations(translations, sentinel_map)
        log.info(f"展开后 translations: {len(translations)} 条")

    def placeholder_safe(src: bytes, dst: bytes, allow_reorder: bool) -> bool:
        """
        src/dst 含同样的 ${...} 占位符 → 跨字面量替换安全.

        默认 (allow_reorder=False): 严格 list 顺序匹配, 防 ${OLD}/${NEW} 调换造成语义反转.
        allow_reorder=True: multiset 匹配, 允许中文语序调整 placeholder 位置.

        src 必须至少含一个 ${...} 才走此通道 (无占位符 = 不应跨字面量).
        """
        src_ph = PLACEHOLDER_RE.findall(src)
        if not src_ph:
            return False
        dst_ph = PLACEHOLDER_RE.findall(dst)
        if allow_reorder:
            return Counter(src_ph) == Counter(dst_ph)
        else:
            return src_ph == dst_ph

    all_hits = []  # [(position, src_bytes, dst_bytes)]
    miss = []      # 完全找不到的 src (list of bytes)
    skipped = []   # 命中但不在字面量内的位置 (list of dict)
    placeholder_violations = []  # 跨字面量但 placeholder 顺序/集合不匹配 (高危: 可能语义反转)

    for src, dst, allow_reorder in translations:
        # placeholder 一致性检测 (两条路径共用): src/dst 的 ${...} 必须匹配, 否则拒绝该 entry。
        src_ph = PLACEHOLDER_RE.findall(src)
        if src_ph:
            dst_ph = PLACEHOLDER_RE.findall(dst)
            if allow_reorder:
                ph_match = (Counter(src_ph) == Counter(dst_ph))
            else:
                ph_match = (src_ph == dst_ph)
            if not ph_match:
                placeholder_violations.append({
                    "src": src.decode("utf-8", errors="replace"),
                    "src_placeholders": [p.decode() for p in src_ph],
                    "dst_placeholders": [p.decode() for p in dst_ph],
                    "allow_reorder": allow_reorder,
                })
                log.error(f"[FAIL] placeholder 不匹配: src 含 {[p.decode() for p in src_ph]}, "
                          f"dst 含 {[p.decode() for p in dst_ph]}, allow_reorder={allow_reorder}")
                continue  # 拒绝该 entry 所有 hits

        # 主路: decode-match — src key 是 canonical 整字面量, 在 decoded_index 精确命中。
        # 命中后按该字面量 lit_type 用 encode_dst_decoded 重编码 dst, 替换整个 [start, end)。
        try:
            src_text = src.decode("utf-8")
        except UnicodeDecodeError:
            src_text = None
        idx_hits = decoded_index.get(src_text) if src_text is not None else None
        if idx_hits:
            for s_pos, e_pos, lit_type in idx_hits:
                dst_encoded = encode_dst_decoded(dst, lit_type)
                all_hits.append((s_pos, cli_bytes[s_pos:e_pos], dst_encoded))
            continue

        # 兜底: 非整字面量 (子串 / sentinel 跨插值 / 跨字面量) 走 byte-find escape-aware。
        raw_hits = find_src_with_escape(cli_bytes, src)
        if not raw_hits:
            miss.append(src)
            continue
        for i, form_bytes, lit_type in raw_hits:
            end = i + len(form_bytes)
            dst_encoded = encode_dst_decoded(dst, lit_type)
            if in_literal(i, end):
                all_hits.append((i, form_bytes, dst_encoded))
            elif placeholder_safe(src, dst, allow_reorder):
                # 跨字面量但占位符按规则匹配 (template literal 段间合法)
                all_hits.append((i, form_bytes, dst_encoded))
            else:
                ctx_start = max(0, i - 30)
                ctx_end = min(len(cli_bytes), end + 30)
                skipped.append({
                    "src": src.decode("utf-8", errors="replace"),
                    "position": i,
                    "lit_type": lit_type,
                    "context": cli_bytes[ctx_start:ctx_end].decode("utf-8", errors="replace"),
                })
                log.warning(f"[WARN] skip 代码区匹配 @{i}: ...{cli_bytes[ctx_start:ctx_end]!r}...")

    # 检查 hits 之间是否冲突 (同一 byte range 被多条 src 覆盖)
    # 完全嵌套 (短 src 完全在长 src 范围内) 不算冲突 — 应用时跳过被覆盖的短 src
    # 排序: pos 升 + 长度降, 使同起点时较长 (outer) 在前, 较短 (含 prefix 子串) 被判为 nested。
    all_hits.sort(key=lambda h: (h[0], -len(h[1])))
    conflicts = []
    nested_inner_indices: set[int] = set()  # all_hits 索引, 这些 hit 在某 outer hit 内部, 应用时跳过
    for k in range(len(all_hits)):
        if k in nested_inner_indices:
            continue
        a_pos, a_src, _ = all_hits[k]
        a_end = a_pos + len(a_src)
        for j in range(k + 1, len(all_hits)):
            b_pos, b_src, _ = all_hits[j]
            if b_pos >= a_end:
                break
            b_end = b_pos + len(b_src)
            if b_end <= a_end:
                # b 完全嵌套在 a 内 (含同起点更短的 prefix) -> 跳过 b (a 的 dst 已覆盖该段)
                nested_inner_indices.add(j)
            else:
                # 部分重叠 (起点不同且交错) -> 真冲突
                conflicts.append({
                    "src1": a_src.decode("utf-8", errors="replace"),
                    "pos1": a_pos,
                    "src2": b_src.decode("utf-8", errors="replace"),
                    "pos2": b_pos,
                })
    if conflicts:
        for c in conflicts:
            log.error(f"[FAIL] 冲突: src1@{c['pos1']} ({c['src1'][:40]!r}) "
                      f"与 src2@{c['pos2']} ({c['src2'][:40]!r}) 部分重叠")
        raise ValueError(f"翻译冲突 {len(conflicts)} 处, 中断")
    if nested_inner_indices:
        log.info(f"[INFO] 嵌套替换: {len(nested_inner_indices)} 处短 src 在长 src 内, 跳过 (由 outer src dst 覆盖)")

    # 从后往前应用, 避免 offset shift; 跳过嵌套内层
    new_cli = bytearray(cli_bytes)
    indexed_hits = list(enumerate(all_hits))
    indexed_hits.sort(key=lambda kv: -kv[1][0])  # by position desc (pos is hit_tuple[0])
    for k, (pos, src, dst) in indexed_hits:
        if k in nested_inner_indices:
            continue
        new_cli[pos : pos + len(src)] = dst
    new_cli = bytes(new_cli)

    from code_patches import apply_code_patches
    new_cli, applied_patches = apply_code_patches(new_cli)
    for p in applied_patches:
        log.info(f"[OK] code-patch: {p['src']} → {p['dst']}")
        log.info(f"  reason: {p['why']}")

    if miss:
        log.warning(f"[WARN] {len(miss)} 条替换未匹配 (cli.js 中找不到):")
        for m in miss[:5]:
            log.warning(f"     {m[:80]!r}")
    if skipped:
        log.warning(f"[WARN] skip 代码区匹配: {len(skipped)} 处 (避免破坏 JS 语法)")
    log.info(f"应用替换 (字面量内): {len(all_hits)} 处命中")
    delta = len(new_cli) - len(cli_bytes)
    log.info(f"cli.js 长度变化: {delta:+d}")
    hit_count = len(all_hits)

    # 重新分配 segments
    cursor = segments[0]["off"]
    by_label = {}
    for s in segments:
        new_s = dict(s)
        if s["label"] == "module_0_contents":
            new_s["new_off"] = cursor
            new_s["new_len"] = len(new_cli)
            new_s["data"] = new_cli
        else:
            new_s["new_off"] = cursor
            new_s["new_len"] = s["len"]
            new_s["data"] = bytes(data[data_start + s["off"] :
                                        data_start + s["off"] + s["len"]])
        cursor += new_s["new_len"]
        by_label[s["label"]] = new_s

    new_byte_count = cursor
    new_blob_header = new_byte_count + OFFSETS_SIZE + len(TRAILER)

    # 检查 segment 容量
    inner_used = (data_start - seg_start) + new_byte_count + OFFSETS_SIZE + len(TRAILER)
    seg_size = seg_end - seg_start
    if inner_used > seg_size:
        raise ValueError(f"超出 segment 容量 {inner_used - seg_size} 字节, "
                         f"需要更短的翻译或扩展 segment")
    nul_pad = seg_size - inner_used
    log.info(f"segment 余量: {seg_size - inner_used:,} 字节 (NUL padding)")

    # 构建 new payload buffer
    first_off = segments[0]["off"]
    prefix = bytes(data[data_start : data_start + first_off])
    new_payload = bytearray(prefix)
    for s in sorted(by_label.values(), key=lambda x: x["new_off"]):
        assert s["new_off"] == len(new_payload)
        new_payload += s["data"]

    # 更新 modules array 内的 StringPointer
    ma = by_label["modules_array"]
    ma_start = ma["new_off"]
    for i in range(mod_count):
        entry_off = ma_start + i * MODULE_ENTRY_SIZE
        for sp_off, fname in zip(SP_OFFSETS, SP_FIELDS):
            label = f"module_{i}_{fname}"
            seg = by_label.get(label)
            if seg is None:
                continue
            struct.pack_into("<II", new_payload, entry_off + sp_off,
                            seg["new_off"], seg["new_len"])

    # 构建新 Offsets
    new_args_off = by_label["args"]["new_off"] if "args" in by_label else args_off
    new_args_len = by_label["args"]["new_len"] if "args" in by_label else args_len
    new_offsets = struct.pack("<IIIIIIII",
                              new_byte_count, 0,
                              by_label["modules_array"]["new_off"],
                              by_label["modules_array"]["new_len"],
                              entry_id, new_args_off, new_args_len, flags)

    # 拼装最终 binary
    # 注意: BlobHeader 不一定紧贴 segment 起点 (Linux ELF 中 segment 起点到 BlobHeader
    # 之间可能有 MB 级别的其他内容, 比如 ELF 的 .rodata 延续). 必须原样保留.
    header_before_seg = bytes(data[:seg_start])
    inside_seg_before_blob = bytes(data[seg_start:blob_header_pos])  # 可能为空, 也可能很大
    new_blob_bytes = struct.pack("<Q", new_blob_header)
    after_seg = bytes(data[seg_end:])
    new_binary = (header_before_seg
                  + inside_seg_before_blob
                  + new_blob_bytes
                  + bytes(new_payload)
                  + new_offsets + TRAILER
                  + b"\x00" * nul_pad + after_seg)

    assert len(new_binary) == len(data), f"size differs: {len(new_binary)} vs {len(data)}"
    Path(output_bin).write_bytes(new_binary)
    log.info(f"已写出 {output_bin} ({len(new_binary):,} 字节)")

    # codesign for macOS: 仅在 codesign 可用时执行。codesign 是 macOS 专有工具,
    # 非 macOS 的 CI runner (ubuntu) 上没有它; CI 只静态 verify 覆盖率、不运行 binary,
    # 跳过签名无妨 (patched Mach-O 仍需在 macOS 上自行重签才能运行)。
    if fmt == "macho" and codesign and shutil.which("codesign"):
        subprocess.run(["codesign", "--force", "--deep", "--sign", "-",
                        str(output_bin)], check=True)
        log.info(f"已 ad-hoc 签名")
    else:
        if fmt == "macho" and codesign:
            log.warning("codesign 不可用 (非 macOS), 跳过 ad-hoc 签名; "
                        "patched Mach-O 需在 macOS 上重签才能运行")
        # 确保可执行
        Path(output_bin).chmod(0o755)

    return {
        "format": fmt,
        "delta": delta,
        "hits": hit_count,
        "misses": len(miss),
        "skips": len(skipped),
        "placeholder_violations": len(placeholder_violations),
        "padding_left": nul_pad,
        # 详细信息 (供 report 写盘 / strict 模式判定)
        "_missed_list": [m.decode("utf-8", errors="replace") for m in miss],
        "_skipped_list": skipped,
        "_placeholder_violations_list": placeholder_violations,
        "_sentinel_resolved": {n: [v.decode() for v in vars_]
                               for n, vars_ in sentinel_map.items()},
        "_segment": [seg_start, seg_end],
        "_byte_count_old": byte_count,
        "_byte_count_new": new_byte_count,
    }


if __name__ == "__main__":
    import argparse
    import datetime
    import json

    parser = argparse.ArgumentParser(
        description="Patch CC binary 把英文 system prompt 替换为中文 (跨 macOS/Linux)")
    parser.add_argument("input", help="原 CC binary 路径")
    parser.add_argument("output", help="patched binary 输出路径")
    parser.add_argument("translations", help="translations.json 路径")
    parser.add_argument("--strict", action="store_true",
                        help="skip>0 (代码区误匹配) 即 exit 1; miss 仅提示 (跨版本/平台并集必然有). "
                             "覆盖率由 verify 守, 无死译文由 integration_test 聚合守")
    parser.add_argument("--report", default=None,
                        help="写 JSON 报告路径 (默认: <output>.report.json)")
    parser.add_argument("--audit-first", action="store_true",
                        help="patch 前先跑 audit_replacements, 高风险时拒绝")
    parser.add_argument("--no-codesign", action="store_true",
                        help="跳过 macOS ad-hoc 重签 (仅在你后续手动签时)")
    args = parser.parse_args()

    inp, out, trans_path = args.input, args.output, args.translations
    from bun_format import platform_from_binary
    platform = platform_from_binary(Path(inp))
    trans = load_translations(trans_path, platform)
    log.info(f"加载 translations: {len(trans)} 条 "
             f"({sum(1 for _,_,ar in trans if ar)} 条标记 allow_reorder); platform={platform}")

    # audit-first: patch 前跑风险检查
    if args.audit_first:
        from audit_replacements import audit
        log.info("=== 预审计 (audit-first) ===")
        # 需要先 extract cli.js 才能 audit; 这里走简化路径: 直接传 input binary
        # audit() 会自己抽 cli.js, 返回风险 summary
        audit_report = audit(Path(inp), trans)
        if audit_report["high_risk"]:
            log.error(f"[FAIL] 预审计发现 {audit_report['high_risk']} 项高风险, 拒绝 patch")
            sys.exit(2)
        log.info(f"[OK] 预审计通过 (risk_a={audit_report['risk_a']}, risk_b={audit_report['risk_b']})\n")
    summary = repack(Path(inp), Path(out), trans, codesign=not args.no_codesign)

    # 写 report 到磁盘
    report_path = args.report or f"{out}.report.json"
    report = {
        "timestamp": datetime.datetime.now().isoformat(),
        "input": str(Path(inp).resolve()),
        "output": str(Path(out).resolve()),
        "translations": str(Path(trans_path).resolve()),
        "format": summary["format"],
        "delta": summary["delta"],
        "hits": summary["hits"],
        "misses": summary["misses"],
        "skips": summary["skips"],
        "placeholder_violations": summary["placeholder_violations"],
        "padding_left": summary["padding_left"],
        "byte_count_old": summary["_byte_count_old"],
        "byte_count_new": summary["_byte_count_new"],
        "sentinel_resolved": summary["_sentinel_resolved"],
        "missed_full": summary["_missed_list"],
        "skipped_full": summary["_skipped_list"],
        "placeholder_violations_full": summary["_placeholder_violations_list"],
    }
    Path(report_path).write_text(json.dumps(report, ensure_ascii=False, indent=2))
    log.info(f"报告已写入: {report_path}")

    # 公开 summary (不含 _ 前缀字段)
    public_summary = {k: v for k, v in summary.items() if not k.startswith("_")}
    log.info(f"\nSummary: {public_summary}")

    # strict 模式: miss/skip/placeholder_violations 超阈值 → exit 1
    # placeholder violation 永远是高危 (语义反转), 不可容忍
    if summary["placeholder_violations"] > 0:
        log.error(f"\n[FAIL] {summary['placeholder_violations']} 处 placeholder 不匹配 "
                  f"(可能语义反转), 这些 trans entry 已被拒绝")
        sys.exit(3)

    if args.strict:
        # skip = 代码区误匹配, 真问题, 必须 0。
        # miss = 该译文 src 不在此 binary (跨版本/跨平台并集的必然结果), 非问题:
        #   覆盖率由 verify (untranslated_prompt=0) 守; "无死译文" 由 integration_test
        #   跨 (版本×平台) 聚合 no-stale 守。故 strict 不再卡 miss。
        if summary["skips"] > 0:
            log.error(f"\n[FAIL] --strict: {summary['skips']} skip > 0 (代码区误匹配), exit 1")
            sys.exit(1)
        if summary["misses"] > 0:
            log.info(f"[INFO] {summary['misses']} miss (跨版本/平台并集, 非失败; 覆盖率由 verify 守)")
