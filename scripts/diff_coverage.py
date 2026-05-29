"""
对比 extract_prompt_segments 输出与 translations / whitelist, 输出覆盖率差距报告.

用法:
  python3 diff_coverage.py <extract-report.json> [--min-score N] [--len-bucket]
                          [--top-untranslated K] [--show-context C]
                          [--out diff-report.json]

输出:
  - score 分布
  - 长度分桶 (<60 / 60-200 / >=200)
  - 未翻字面量按 score 降序前 K 条 (stderr)
  - JSON diff-report (可选)
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from log_setup import setup_logging  # noqa: E402

log = setup_logging(__name__)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("report", help="extract_prompt_segments 输出的 JSON")
    ap.add_argument("--min-score", type=int, default=2,
                    help="只统计 score >= N 的字面量 (默认 2)")
    ap.add_argument("--top-untranslated", type=int, default=50,
                    help="stderr 打印未翻 top K (默认 50)")
    ap.add_argument("--show-context", type=int, default=120,
                    help="打印时文本上下文字符数 (默认 120)")
    ap.add_argument("--out", default=None, help="diff 报告 JSON 输出路径")
    args = ap.parse_args()

    data = json.loads(Path(args.report).read_text())
    cand = [c for c in data["candidates"] if c["score"] >= args.min_score]

    untrans = [c for c in cand if not c["translated"] and not c["whitelisted"]]
    trans = [c for c in cand if c["translated"]]
    wl = [c for c in cand if c["whitelisted"]]

    score_dist = Counter(c["score"] for c in untrans)
    log.info("=== 未翻 score 分布 ===")
    for s in sorted(score_dist.keys(), reverse=True):
        log.info(f"  score={s:>3}: {score_dist[s]:>5}")

    if untrans:
        lens = [c["len"] for c in untrans]
        log.info(f"\n=== 未翻长度统计 ===")
        log.info(f"  min={min(lens)} p50={statistics.median(lens):.0f} "
                 f"p95={sorted(lens)[int(len(lens) * 0.95)]} max={max(lens)}")
        bucket_short = sum(1 for c in untrans if c["len"] < 60)
        bucket_mid = sum(1 for c in untrans if 60 <= c["len"] < 200)
        bucket_long = sum(1 for c in untrans if c["len"] >= 200)
        log.info(f"  <60   : {bucket_short}")
        log.info(f"  60-200: {bucket_mid}")
        log.info(f"  >=200 : {bucket_long}")

    total = len(cand)
    if total == 0:
        raise ValueError(
            f"extract 报告中没有 score >= {args.min_score} 的候选, "
            f"无法计算覆盖率 (源报告: {args.report})")
    log.info(f"\n=== 覆盖率汇总 (score >= {args.min_score}) ===")
    log.info(f"  候选总数      : {total}")
    log.info(f"  已翻译        : {len(trans)} ({100*len(trans)/total:.1f}%)")
    log.info(f"  白名单覆盖    : {len(wl)} ({100*len(wl)/total:.1f}%)")
    log.info(f"  未覆盖        : {len(untrans)} ({100*len(untrans)/total:.1f}%)")

    if untrans:
        untrans.sort(key=lambda c: (-c["score"], c["offset"]))
        log.info(f"\n=== 未翻 top {min(args.top_untranslated, len(untrans))} ===")
        for c in untrans[:args.top_untranslated]:
            t = c["text"][:args.show_context].replace("\n", "\\n")
            if c["len"] > args.show_context:
                t += "..."
            log.info(f"  @{c['offset']:>8} len={c['len']:>5} score={c['score']:>2} "
                     f"[{c['class']:<6}] {','.join(c['evidence'])[:60]}")
            log.info(f"           {t!r}")

    if args.out:
        diff_report = {
            "source_report": str(Path(args.report).resolve()),
            "min_score": args.min_score,
            "total_candidates": total,
            "translated": len(trans),
            "whitelisted": len(wl),
            "untranslated": len(untrans),
            "score_distribution": dict(sorted(score_dist.items(), reverse=True)),
            "untranslated_top": [
                {"offset": c["offset"], "len": c["len"], "score": c["score"],
                 "class": c["class"], "evidence": c["evidence"],
                 "text": c["text"]}
                for c in untrans[:args.top_untranslated]
            ],
        }
        Path(args.out).write_text(json.dumps(diff_report, ensure_ascii=False, indent=2))
        log.info(f"\ndiff report: {args.out}")

    if untrans:
        sys.exit(1)


if __name__ == "__main__":
    main()
