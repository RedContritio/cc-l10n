"""
v3: 从 unpacked cli.js (干净 JS 源码) 提取提示词候选。

vs v2:
  - 输入: cli.js 源码 (不是 binary), 没有 binary 噪音
  - 同时匹配 "..." / '...' / 反引号 template literal 内的纯文本片段
  - 保留 stop_word + 长度过滤

输出:
  candidates_v3.csv  - 概览
  candidates_v3.txt  - 完整文本
  candidates_v3.json - 结构化数据
"""
import argparse
import json
import pathlib
import re
from collections import Counter

MIN_LEN = 40

# 双引号字符串: " 开头, 直到下一个未转义的 "
DQ_RE = re.compile(r'"((?:[^"\\\n]|\\.)*)"')
# 单引号字符串: ' 开头, 直到下一个未转义的 '
SQ_RE = re.compile(r"'((?:[^'\\\n]|\\.)*)'")
# 反引号 template literal (简化: 不跨行, 内部允许 ${...}, 取整段)
# 然后再用 \$\{[^}]*\} 切分内部, 取静态片段
BT_RE = re.compile(r"`((?:[^`\\]|\\.)*)`", re.DOTALL)
TEMPLATE_PLACEHOLDER_RE = re.compile(r"\$\{[^{}]*(?:\{[^}]*\}[^{}]*)*\}")

STOP_WORDS = {
    "the", "a", "an", "to", "of", "and", "or", "is", "are", "was", "were",
    "be", "been", "being", "have", "has", "had", "do", "does", "did", "will",
    "would", "should", "could", "may", "might", "must", "can", "in", "on",
    "at", "by", "for", "with", "from", "as", "if", "when", "while", "you",
    "your", "this", "that", "these", "those", "it", "its", "not", "use",
    "user", "tool", "task"
}


def count_stop_words(text: str) -> int:
    words = re.findall(r"[a-zA-Z]+", text.lower())
    return sum(1 for w in words if w in STOP_WORDS)


def features(s: str) -> dict:
    n = len(s)
    if n == 0:
        return {"len": 0}
    code_chars = sum(c in "{}();,=<>$" for c in s)
    alpha_chars = sum(c.isalpha() for c in s)
    space_chars = sum(c == " " for c in s)
    period_chars = sum(c in ".?!" for c in s)
    words = re.findall(r"[a-zA-Z]+", s)
    word_count = len(words)
    avg_word_len = (sum(len(w) for w in words) / word_count) if word_count else 0
    stop_word_hits = count_stop_words(s)

    return {
        "len": n,
        "code_ratio": code_chars / n,
        "alpha_ratio": alpha_chars / n,
        "space_ratio": space_chars / n,
        "period_density": period_chars / n,
        "word_count": word_count,
        "avg_word_len": avg_word_len,
        "stop_word_hits": stop_word_hits,
    }


def is_prompt_like(s: str, f: dict, source: str) -> tuple[bool, str]:
    if f["len"] < MIN_LEN:
        return False, "too_short"

    # JS 代码特征
    if "function(" in s or "=>" in s or "===" in s or "!==" in s or s.count("{") > 3:
        return False, "js_code"
    if f["code_ratio"] > 0.05:
        return False, "code_ratio"

    if f["alpha_ratio"] < 0.5:
        return False, "alpha_ratio"
    if f["space_ratio"] < 0.08:
        return False, "space_ratio"
    if f["avg_word_len"] < 3.5:
        return False, "short_words"

    # stop word 锚定
    if f["stop_word_hits"] < 3:
        return False, "few_stopwords"

    # 排除 troff / CLI help / 错误信息
    if r"\fB" in s or r"\fP" in s:
        return False, "troff"
    if len(re.findall(r"--[a-z][a-z\-]+", s)) > 5:
        return False, "cli_help"

    return True, "PASS"


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cli_js", help="已 unpack 的 cli.js 源码路径")
    ap.add_argument("--out", default="extracted",
                    help="输出目录 (默认: ./extracted)")
    args = ap.parse_args()
    cli_js = pathlib.Path(args.cli_js)
    out_dir = pathlib.Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"读取 {cli_js} ...")
    src = cli_js.read_text("utf-8", errors="replace")
    print(f"cli.js 大小: {len(src):,} 字符")

    # 1. 提取所有 string literal
    cand_set = set()

    print("\n扫描 double-quoted...")
    for m in DQ_RE.finditer(src):
        cand_set.add(("dq", m.group(1)))

    print("扫描 single-quoted...")
    for m in SQ_RE.finditer(src):
        cand_set.add(("sq", m.group(1)))

    print("扫描 backtick template literal...")
    bt_count = 0
    bt_frag_count = 0
    for m in BT_RE.finditer(src):
        bt_count += 1
        whole = m.group(1)
        # 切分内部 ${...} 取静态片段
        parts = TEMPLATE_PLACEHOLDER_RE.split(whole)
        for p in parts:
            if len(p) >= MIN_LEN:
                cand_set.add(("bt_frag", p))
                bt_frag_count += 1
        # 整段也加入候选 (可能本身就没有 placeholder)
        if "${" not in whole:
            cand_set.add(("bt", whole))

    print(f"  template literal 总数: {bt_count}")
    print(f"  template literal 内静态片段 (>={MIN_LEN}): {bt_frag_count}")
    print(f"\n总候选 (去重前去掉太短): {len(cand_set):,}")

    # 2. 过滤
    passed = []
    rejected_by = Counter()
    for source, s in cand_set:
        f = features(s)
        ok, reason = is_prompt_like(s, f, source)
        if ok:
            passed.append((s, f, source))
        else:
            rejected_by[reason] += 1

    print(f"\n通过启发式过滤: {len(passed):,}")
    print(f"被过滤掉的桶:")
    for bucket, cnt in rejected_by.most_common():
        print(f"  {bucket}: {cnt}")

    passed.sort(key=lambda x: -x[1]["len"])

    csv_path = out_dir / "candidates_v3.csv"
    with csv_path.open("w") as fp:
        fp.write("idx,source,len,words,stops,preview\n")
        for i, (s, f, src_type) in enumerate(passed):
            preview = s[:80].replace("\n", " ").replace('"', "'")
            fp.write(f'{i},{src_type},{f["len"]},{f["word_count"]},'
                     f'{f["stop_word_hits"]},"{preview}"\n')

    txt_path = out_dir / "candidates_v3.txt"
    with txt_path.open("w") as fp:
        for i, (s, f, src_type) in enumerate(passed):
            fp.write(f"=== #{i}  [{src_type}]  len={f['len']}  "
                     f"words={f['word_count']}  stops={f['stop_word_hits']} ===\n")
            fp.write(s)
            fp.write("\n\n")

    json_path = out_dir / "candidates_v3.json"
    with json_path.open("w") as fp:
        json.dump([{"text": s, "source": src_type, **f}
                   for s, f, src_type in passed],
                  fp, ensure_ascii=False, indent=2)

    print(f"\n输出: {csv_path}")
    print(f"       {txt_path}")
    print(f"       {json_path}")

    total_bytes = sum(f["len"] for _, f, _ in passed)
    print(f"\n通过过滤的总字节数: {total_bytes:,}")

    # 来源分布
    src_dist = Counter(src_type for _, _, src_type in passed)
    print(f"\n来源分布:")
    for src_type, cnt in src_dist.most_common():
        print(f"  {src_type}: {cnt}")

    print(f"\n按长度 top 30:")
    for i, (s, f, src_type) in enumerate(passed[:30]):
        preview = s[:70].replace("\n", " ")
        print(f"  #{i:3d}  [{src_type:8s}]  {f['len']:5d}B  s={f['stop_word_hits']:3d}  |  {preview}")


if __name__ == "__main__":
    main()
