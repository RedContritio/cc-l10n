"""
最简 minified JS 字符串字面量扫描器 (v2: 正确处理嵌套).

输出: List[(start, end, type)]  字节范围 (不含引号)
  type ∈ {'dq', 'sq', 'tmpl_frag'}

关键: ${...} 表达式内的嵌套字符串字面量也要 yield (它们仍然是合法 string literal,
patch 时同样可以替换).

支持:
  - 双/单引号字符串 (含转义)
  - 反引号 template literal (静态片段 yield 为 tmpl_frag, ${...} 内嵌套 string 仍 yield)
  - 单行 // 与多行 /* */ 注释 (跳过)
  - 正则字面量 /.../  (跳过)
"""
from typing import Iterator

REGEX_PREV = set(b"=([,;:!&|?+-*/%^~{}<>\n\t ")

# JS keywords that, when immediately preceding '/', make '/' start a regex literal
# rather than a division operator. Critical for minified code like `return/regex/`.
REGEX_PREV_KEYWORDS = frozenset({
    b"return", b"typeof", b"instanceof", b"in", b"of",
    b"void", b"delete", b"new", b"throw", b"yield", b"await",
    b"else", b"do", b"case", b"default",
})

_IDENT_CHARS = set(
    bytes(range(ord('a'), ord('z') + 1))
    + bytes(range(ord('A'), ord('Z') + 1))
    + bytes(range(ord('0'), ord('9') + 1))
    + b"_$"
)


def _ident_ending_at(src: bytes, end: int) -> bytes:
    if end <= 0 or src[end - 1] not in _IDENT_CHARS:
        return b""
    i = end - 1
    while i > 0 and src[i - 1] in _IDENT_CHARS:
        i -= 1
    # 数字开头 = 数字字面量, 其后 '/' 是除法不是 regex
    if src[i:i + 1].isdigit():
        return b""
    return src[i:end]


def scan_string_literals(src: bytes) -> Iterator[tuple[int, int, str]]:
    n = len(src)
    out = []
    # 用显式 stack 模拟嵌套: 每个 frame 表示当前在何种 context 中
    #   ('code', last_non_ws)            正常 JS 代码
    #   ('tmpl', seg_start, brace_depth) template literal 内, depth=0 是静态文本, >0 是 ${...} 内
    # 跨 quote 后回到上层 frame.
    # 简化: 用递归函数 + 共享 i.

    pos = [0]

    def scan_code_until(end_marker: int | None):
        # end_marker is not None 时为 ${...} 内: 顶层 '}' 退出
        last_non_ws = b" "[0]
        brace_depth = 0
        while pos[0] < n:
            c = src[pos[0]]

            if c == ord('/') and pos[0] + 1 < n:
                nxt = src[pos[0] + 1]
                if nxt == ord('/'):
                    pos[0] += 2
                    while pos[0] < n and src[pos[0]] != ord('\n'):
                        pos[0] += 1
                    continue
                if nxt == ord('*'):
                    pos[0] += 2
                    while pos[0] + 1 < n:
                        if src[pos[0]] == ord('*') and src[pos[0]+1] == ord('/'):
                            pos[0] += 2
                            break
                        pos[0] += 1
                    continue

            if end_marker is not None:
                if c == ord('{'):
                    brace_depth += 1
                    pos[0] += 1
                    last_non_ws = c
                    continue
                if c == ord('}'):
                    if brace_depth == 0:
                        return
                    brace_depth -= 1
                    pos[0] += 1
                    last_non_ws = c
                    continue

            if c == ord('"'):
                start = pos[0] + 1
                pos[0] += 1
                while pos[0] < n:
                    if src[pos[0]] == ord('\\'):
                        pos[0] += 2; continue
                    if src[pos[0]] == ord('"'):
                        out.append((start, pos[0], 'dq'))
                        pos[0] += 1
                        last_non_ws = ord('"')
                        break
                    pos[0] += 1
                continue

            if c == ord("'"):
                start = pos[0] + 1
                pos[0] += 1
                while pos[0] < n:
                    if src[pos[0]] == ord('\\'):
                        pos[0] += 2; continue
                    if src[pos[0]] == ord("'"):
                        out.append((start, pos[0], 'sq'))
                        pos[0] += 1
                        last_non_ws = ord("'")
                        break
                    pos[0] += 1
                continue

            if c == ord('`'):
                pos[0] += 1
                scan_template()
                last_non_ws = ord('`')
                continue

            # '/' 是 regex 起点的两种情况: (1) 前一非空白是 REGEX_PREV 标点,
            # (2) 前一 token 是 return/typeof 等允许跟 regex 的关键字 (minified JS
            # 常写 `return/re/`).
            is_regex_start = False
            if c == ord('/'):
                if last_non_ws in REGEX_PREV:
                    is_regex_start = True
                elif last_non_ws in _IDENT_CHARS:
                    ident = _ident_ending_at(src, pos[0])
                    if ident in REGEX_PREV_KEYWORDS:
                        is_regex_start = True

            if is_regex_start:
                pos[0] += 1
                in_class = False
                while pos[0] < n:
                    if src[pos[0]] == ord('\\'):
                        pos[0] += 2; continue
                    if src[pos[0]] == ord('[') and not in_class:
                        in_class = True
                    elif src[pos[0]] == ord(']') and in_class:
                        in_class = False
                    elif src[pos[0]] == ord('/') and not in_class:
                        pos[0] += 1
                        while pos[0] < n and src[pos[0]:pos[0]+1] in (b'g',b'i',b'm',b's',b'u',b'y',b'd'):
                            pos[0] += 1
                        last_non_ws = ord('/')
                        break
                    elif src[pos[0]] == ord('\n'):
                        break
                    pos[0] += 1
                continue

            if c not in b" \t\n\r":
                last_non_ws = c
            pos[0] += 1

    def scan_template():
        seg_start = pos[0]
        while pos[0] < n:
            c = src[pos[0]]
            if c == ord('\\'):
                pos[0] += 2; continue
            if pos[0] + 1 < n and c == ord('$') and src[pos[0]+1] == ord('{'):
                if pos[0] > seg_start:
                    out.append((seg_start, pos[0], 'tmpl_frag'))
                pos[0] += 2
                scan_code_until(end_marker=ord('}'))
                # scan_code_until 在 unmatched '}' 处停下但未 consume, 这里 consume
                if pos[0] < n and src[pos[0]] == ord('}'):
                    pos[0] += 1
                seg_start = pos[0]
                continue
            if c == ord('`'):
                if pos[0] > seg_start:
                    out.append((seg_start, pos[0], 'tmpl_frag'))
                pos[0] += 1
                return
            pos[0] += 1

    scan_code_until(end_marker=None)
    out.sort(key=lambda x: x[0])
    return out


if __name__ == "__main__":
    import sys
    import pathlib
    import time

    path = pathlib.Path(sys.argv[1] if len(sys.argv) > 1
                        else "/tmp/cc-zh-poc/cli-linux.js")
    src = path.read_bytes()
    print(f"扫描 {path}: {len(src):,} bytes")
    t0 = time.time()
    ranges = list(scan_string_literals(src))
    t1 = time.time()
    print(f"找到 {len(ranges):,} 个字面量, 用时 {t1-t0:.2f}s")

    by_type = {}
    for s, e, t in ranges:
        by_type.setdefault(t, 0)
        by_type[t] += 1
    print(f"按类型: {by_type}")

    total_bytes = sum(e - s for s, e, t in ranges)
    print(f"字面量总字节数: {total_bytes:,} ({100*total_bytes/len(src):.1f}% of cli.js)")

    # smoke test: 'report the issue at https' 在 cli.js 中的每个 occurrence
    # 都应落在某个 string literal 范围内 (用于回归检测 scanner 漏识别)
    needle = b"report the issue at https"
    cnt = src.count(needle)
    print(f"\n'report the issue at https' in cli.js: {cnt} 处")
    in_lit = 0
    pos = 0
    while True:
        i = src.find(needle, pos)
        if i < 0: break
        end = i + len(needle)
        import bisect
        starts = [r[0] for r in ranges]
        idx = bisect.bisect_right(starts, i) - 1
        if idx >= 0 and ranges[idx][0] <= i and end <= ranges[idx][1]:
            in_lit += 1
        pos = i + 1
    print(f"  其中在字面量内的: {in_lit} 处 (应 = {cnt})")
