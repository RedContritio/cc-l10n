#!/usr/bin/env python3
"""术语一致性 checker (用户铁律: 每个英文名词/动词的译文是固定子集; src 出现该词,
dst 必须含对应集合中的元素, 或该词标 keep-English 时在 dst 保留)。

glossary 格式 (data/glossary.json):
  {
    "_comment": "...",
    "terms": {
      "task":   {"zh": ["任务"]},              # 该译: dst 含 任务 之一
      "plan":   {"zh": ["计划", "方案"]},        # 固定子集 (多选一)
      "worktree": {"keep": true},               # keep-English: dst 保留 worktree
      "plan mode": {"keep": true, "phrase": true}  # 多词术语 (phrase), 整体匹配, 优先于单词 plan
    }
  }

判据 (词边界, **完整词非子串** —— 见 feedback-wl-word-boundary):
  对每条 (src, dst):
    - 先扣除 phrase 术语 (多词, keep): 若 phrase 在 src, 要求在 dst 也在 (keep); 并从 src/dst
      抠掉该 phrase 区间, 避免其中单词 (如 plan mode 里的 plan) 触发单词规则。
    - 再对单词术语: 若 word 以词边界出现在剩余 src:
        keep=true  → dst (词边界) 必须仍含 word; 否则 violation (该留英却没了/或被译走)。
        zh=[...]   → dst 必须含 zh 中某元素; 否则 violation (该译却留英或译法漂移)。
退出码 0=无 violation, 1=有。
"""
import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# 词边界: 两端非 [字母/数字/下划线] 才算完整词 —— snake_case/标识符 (file_path) 视为单 token,
# 'path' 不在 'file_path' 中误命中 (用户铁律: 完整词匹配, 非子串)。
WB = lambda w: re.compile(r"(?<![A-Za-z0-9_])" + re.escape(w) + r"(?![A-Za-z0-9_])")


def load_glossary(path):
    g = json.load(open(path, encoding="utf-8"))["terms"]
    phrases = {k: v for k, v in g.items() if v.get("phrase")}
    words = {k: v for k, v in g.items() if not v.get("phrase")}
    # phrase 先长后短
    phrase_order = sorted(phrases, key=len, reverse=True)
    return words, phrases, phrase_order


def load_pairs():
    pairs = []
    for f in sorted((ROOT / "data" / "translations").glob("*.json")):
        d = json.load(open(f, encoding="utf-8"))
        for k, v in d.items():
            if k.startswith("_") or not isinstance(v, str):
                continue
            pairs.append((f.name, k, v))
    return pairs


_FENCE = re.compile(r"```.*?```", re.DOTALL)
_BACKTICK = re.compile(r"`[^`\n]*`")
_QUOTE = re.compile(r'"[^"\n]*"' + r"|'[^'\n]*'")
_CODELINE = re.compile(r"(?m)^(?:[ \t]{2,}|\t).*$")
_XMLTAG = re.compile(r"<[^>\n]{1,40}>")

def strip_code(t):
    """剥离 code/示例区 (fenced/反引号/引号示例/缩进代码行/<tag>) —— 这些里保留英文是对的,
    只在散文部分判'该译却留英'。"""
    t = _FENCE.sub(" ", t)
    t = _CODELINE.sub(" ", t)
    t = _BACKTICK.sub(" ", t)
    t = _QUOTE.sub(" ", t)
    t = _XMLTAG.sub(" ", t)
    return t

def check_pair(src, dst, words, phrases, phrase_order):
    viols = []
    src_masked, dst_masked = strip_code(src), strip_code(dst)
    # phrase: keep 多词术语, 从 src/dst 抠掉 (用空格占位保边界)
    for ph in phrase_order:
        if WB(ph).search(src_masked):
            if not WB(ph).search(dst_masked):
                viols.append(("phrase_keep", ph))
            src_masked = WB(ph).sub(" ", src_masked)
            dst_masked = WB(ph).sub(" ", dst_masked)
    # 单词术语。聚焦【keep-vs-translate 不一致】(用户核心: 同词时译时留英), 不报中文译法变体。
    for w, spec in words.items():
        if not WB(w).search(src_masked):
            continue
        if spec.get("keep"):
            # 该 keep 却在 dst 里没了该英文词 (被译走/丢失) → 不一致
            if not WB(w).search(dst_masked):
                viols.append(("keep_lost", w))
        else:
            # 该译却在 dst 里仍保留了英文该词 → 不一致 (留英)。
            # (dst 同时含中文译法属"部分译", 仍按留英计, 应彻底译。)
            if WB(w).search(dst_masked):
                viols.append(("kept_english", w))
    return viols


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--glossary", default=str(ROOT / "data" / "glossary.json"))
    ap.add_argument("--show", type=int, default=40, help="每类 violation 打印前 N 条")
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    words, phrases, phrase_order = load_glossary(args.glossary)
    pairs = load_pairs()
    print(f"glossary: {len(words)} 单词 + {len(phrases)} phrase; 译文对: {len(pairs)}")

    by_word = defaultdict(list)  # (kind, word) -> [(file, src_head)]
    total = 0
    for fname, src, dst in pairs:
        for kind, w in check_pair(src, dst, words, phrases, phrase_order):
            by_word[(kind, w)].append((fname, src[:55]))
            total += 1

    print(f"\n=== 一致性 violation: {total} (词条数 {len(by_word)}) ===")
    for (kind, w), hits in sorted(by_word.items(), key=lambda x: -len(x[1])):
        spec = words.get(w) or phrases.get(w) or {}
        want = "keep-English" if spec.get("keep") else "→" + "/".join(spec.get("zh", []))
        print(f"\n[{kind}] {w!r} ({want}) — {len(hits)} 处")
        for fn, head in hits[:args.show]:
            print(f"    {fn}: {head!r}")
    if args.json:
        Path(args.json).write_text(json.dumps(
            {f"{k[0]}:{k[1]}": v for k, v in by_word.items()}, ensure_ascii=False, indent=2))
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
