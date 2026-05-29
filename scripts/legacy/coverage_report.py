"""
扫描 patched CC binary 报告中英翻译覆盖率 + 列出"看起来是 prompt 但仍是英文"的候选.

用法:
  python3 coverage_report.py <patched-binary> [--min-len N] [--top N] [--json out.json]

输出:
  - 已翻字面量数 / 字节数 / 占比
  - 英文残留按 prompt 嫌疑度排序的 top N 列表 (用户决定要不要加 translation)
  - --json 写机器可读报告

prompt 嫌疑度评分维度:
  - 长度 >= 阈值
  - 含足够英文 stop words (the/a/you/your/should/must/...)
  - 开头是 markdown 结构 (# / ## / -)
  - 含关键短语 (IMPORTANT, You are, Use, ...)
"""
import argparse
import bisect
import json
import re
import struct
import sys
from collections import Counter
from pathlib import Path

from js_string_scanner import scan_string_literals

TRAILER = b"\n---- Bun! ----\n"
OFFSETS_SIZE = 32

# 中文字符范围 (CJK 统一汉字 + 扩展)
CN_RE = re.compile(r"[一-鿿㐀-䶿豈-﫿]")

# 英文常见 stop words / 提示词信号词
STOP_WORDS = set("""the a an to of and or is are was were be been being have has had do
does did will would should could may might must can in on at by for with from
as if when while you your this that these those it its not use user tool task
must should never always when do don should not user can be""".split())

PROMPT_KEYWORDS = [
    b"IMPORTANT", b"You are", b"You should", b"You must", b"You can",
    b"Use ", b"Do not", b"Don't", b"Never ", b"Always ",
    b"Use the", b"Avoid", b"Prefer",
]


def has_chinese(text: str) -> bool:
    return bool(CN_RE.search(text))


def extract_cli_js(binary_path: Path) -> bytes:
    """从 binary 提取 module 0 contents (cli.js)"""
    data = binary_path.read_bytes()
    last = 0
    pos = 0
    while True:
        i = data.find(TRAILER, pos)
        if i < 0: break
        last = i; pos = i + 1
    offsets_off = last - OFFSETS_SIZE
    byte_count, _, mod_off, _, *_ = struct.unpack_from("<IIIIIIII", data, offsets_off)
    data_start = offsets_off - byte_count
    modules_start = data_start + mod_off
    cont_off, cont_len = struct.unpack_from("<II", data, modules_start + 8)
    return data[data_start + cont_off : data_start + cont_off + cont_len]


def prompt_score(text: str) -> tuple[int, dict]:
    """
    给字面量评分: 多大概率是用户面向的 prompt 内容 (而非技术日志/错误/变量名).
    返回 (score, breakdown).
    """
    n = len(text)
    bts = text.encode("utf-8", errors="ignore")
    words = re.findall(r"[a-zA-Z]+", text.lower())
    if not words:
        return 0, {}

    stop_hits = sum(1 for w in words if w in STOP_WORDS)
    stop_density = stop_hits / len(words)
    avg_word_len = sum(len(w) for w in words) / len(words)
    has_md_title = text.lstrip().startswith(("#", "##"))
    has_md_bullet = bool(re.match(r"\s*[-*]\s", text))
    period_count = text.count(".") + text.count("?") + text.count("!")
    period_density = period_count / max(n, 1)
    keyword_hits = sum(1 for kw in PROMPT_KEYWORDS if kw in bts)

    score = 0
    if n >= 40: score += 1
    if n >= 100: score += 2
    if stop_hits >= 3: score += 2
    if stop_hits >= 8: score += 3
    if stop_density >= 0.15: score += 2
    if avg_word_len >= 4: score += 1
    if has_md_title: score += 4
    if has_md_bullet: score += 2
    if period_density >= 0.005: score += 1
    if keyword_hits >= 1: score += 2
    if keyword_hits >= 3: score += 3

    return score, {
        "len": n,
        "stop_hits": stop_hits,
        "stop_density": round(stop_density, 3),
        "avg_word_len": round(avg_word_len, 1),
        "md_title": has_md_title,
        "md_bullet": has_md_bullet,
        "period_density": round(period_density, 4),
        "keyword_hits": keyword_hits,
    }


def main():
    parser = argparse.ArgumentParser(
        description="扫 patched CC binary 报告英文残留候选")
    parser.add_argument("binary")
    parser.add_argument("--min-len", type=int, default=30,
                        help="字面量最小长度 (默认 30)")
    parser.add_argument("--min-score", type=int, default=4,
                        help="prompt 嫌疑度最低分 (默认 4, 越高越严格)")
    parser.add_argument("--top", type=int, default=30,
                        help="输出 top N 候选 (默认 30)")
    parser.add_argument("--json", default=None, help="JSON 报告输出路径")
    args = parser.parse_args()

    print(f"提取 cli.js from {args.binary}...")
    cli = extract_cli_js(Path(args.binary))
    print(f"  cli.js: {len(cli):,} 字节")

    print("扫描字符串字面量...")
    ranges = scan_string_literals(cli)
    print(f"  共 {len(ranges):,} 字面量")

    en_literals = []  # [(start, end, text, score, breakdown)]
    cn_literals_bytes = 0
    cn_count = 0
    skipped_short = 0
    skipped_nonprompt = 0
    for s, e, t in ranges:
        if e - s < args.min_len:
            skipped_short += 1
            continue
        content = cli[s:e]
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if has_chinese(text):
            cn_literals_bytes += len(content)
            cn_count += 1
        else:
            score, bd = prompt_score(text)
            if score < args.min_score:
                skipped_nonprompt += 1
                continue
            en_literals.append((s, e, text, score, bd))

    en_literals.sort(key=lambda x: (-x[3], -x[1] + x[0]))  # score desc, len desc

    print()
    print(f"=== 覆盖率统计 ===")
    print(f"  已含中文字面量: {cn_count}, {cn_literals_bytes:,} 字节")
    print(f"  英文残留 (>= {args.min_len}B + score >= {args.min_score}): {len(en_literals)}")
    print(f"  跳过 (太短 < {args.min_len}B): {skipped_short}")
    print(f"  跳过 (英文但 prompt 分数低 < {args.min_score}): {skipped_nonprompt}")
    print()

    print(f"=== Top {args.top} 英文残留候选 (按 prompt 嫌疑度排序) ===")
    print(f"{'#':>3} {'score':>5} {'len':>5} {'stops':>5} {'kw':>3} preview")
    print("-" * 100)
    for i, (s, e, text, score, bd) in enumerate(en_literals[:args.top]):
        preview = text[:80].replace("\n", "\\n").replace('"', "'")
        print(f"{i:>3} {score:>5} {bd['len']:>5} {bd['stop_hits']:>5} "
              f"{bd['keyword_hits']:>3}  {preview}")

    if args.json:
        report = {
            "binary": args.binary,
            "cli_js_size": len(cli),
            "literals_total": len(ranges),
            "cn_literals_count": cn_count,
            "cn_literals_bytes": cn_literals_bytes,
            "en_residuals": [
                {
                    "offset": s, "end": e, "len": e - s,
                    "score": score, "breakdown": bd,
                    "text": text,
                }
                for s, e, text, score, bd in en_literals
            ],
        }
        Path(args.json).write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(f"\nJSON 报告: {args.json}")


if __name__ == "__main__":
    main()
