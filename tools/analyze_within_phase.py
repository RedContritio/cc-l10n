#!/usr/bin/env python3
"""
Within-phase drift analysis.

Split each conversation into K segments (proxy for task phases).
Compute the linear slope of mean_cjk_wrun WITHIN each segment.

If drift is task-stage register shift:
  → changes happen BETWEEN segments (step function)
  → within-segment slopes ≈ 0

If drift is model-internal continuous behavior:
  → each segment independently shows negative slope
  → within-segment slopes < 0

Additionally: compare "between-segment effect" vs "within-segment effect"
to decompose the total trend.
"""

import json
import math
import os
import re
import sys
import random
from pathlib import Path

# Import shared functions from analyze_drift_runs
sys.path.insert(0, str(Path(__file__).parent))
from analyze_drift_runs import (
    is_cjk, is_ascii_letter, extract_text_blocks, strip_code_and_markup,
    tokenize, compute_word_runs, load_conversation_messages,
    analyze_conversation, mean, median, linear_slope, normal_cdf,
    binomial_p, wilcoxon_signed_rank,
)


def main():
    random.seed(42)
    base = Path(os.path.expanduser("~/.claude/projects"))
    all_jsonl = sorted(base.rglob("*.jsonl"))
    filtered = [p for p in all_jsonl if "cc-zh" not in str(p) and "cc_zh" not in str(p)]

    print(f"Scanning {len(filtered)} JSONL files...")

    conversations = []
    for path in filtered:
        if Path(path).stem.startswith("agent-"):
            continue
        try:
            msgs = load_conversation_messages(path, role="assistant")
        except Exception:
            continue
        if len(msgs) < 15:  # Need enough for 3 segments of ≥5
            continue
        all_text = " ".join(msgs)
        n_cjk = sum(1 for c in all_text if is_cjk(c))
        if n_cjk < 30:
            continue
        conversations.append((str(path), msgs))

    n_conv = len(conversations)
    print(f"Eligible conversations (≥15 msgs): {n_conv}")
    print()

    K = 3  # number of segments

    # ══════════════════════════════════════════════════════════════════
    # 1. WITHIN-SEGMENT SLOPE ANALYSIS
    # ══════════════════════════════════════════════════════════════════
    print("=" * 80)
    print(f"WITHIN-SEGMENT SLOPE (K={K} segments)")
    print("  If drift is task-stage: within-segment slopes ≈ 0")
    print("  If drift is model-internal: within-segment slopes < 0")
    print("=" * 80)

    # For each conversation, compute slope within each segment
    # Collect slopes by segment index
    segment_slopes = [[] for _ in range(K)]  # segment_slopes[seg_idx] = list of slopes
    whole_slopes = []

    for path, msgs in conversations:
        per_msg = analyze_conversation(msgs)
        ys = [m['mean_cjk_wrun'] for m in per_msg]
        n = len(ys)

        # Whole-conversation slope
        whole_slopes.append(linear_slope(ys))

        # Split into K segments
        seg_size = n // K
        for seg in range(K):
            start = seg * seg_size
            end = (seg + 1) * seg_size if seg < K - 1 else n
            seg_ys = ys[start:end]
            if len(seg_ys) >= 3:
                segment_slopes[seg].append(linear_slope(seg_ys))

    # Report per-segment
    seg_labels = ["1st third (early)", "2nd third (middle)", "3rd third (late)"]
    print(f"\n  {'Segment':<24} {'N':<6} {'Mean slope':<14} {'Med slope':<14} {'%neg':<8} {'Sign p':<10} {'Wilcox p':<10} {'Sig?'}")
    print("  " + "-" * 100)

    for seg in range(K):
        ss = segment_slopes[seg]
        if len(ss) < 5:
            continue
        sn = len(ss)
        n_neg = sum(1 for s in ss if s < 0)
        n_pos = sum(1 for s in ss if s > 0)
        sign_p = binomial_p(n_neg, n_neg + n_pos, 0.5)
        _, wz, wp = wilcoxon_signed_rank([-s for s in ss])
        sig = '***' if wp < 0.01 else '**' if wp < 0.05 else '*' if wp < 0.1 else 'n.s.'
        print(f"  {seg_labels[seg]:<24} {sn:<6} {mean(ss):<+14.4f} {median(ss):<+14.4f} {n_neg/sn:<8.0%} {sign_p:<10.4f} {wp:<10.4f} {sig}")

    # Also report whole-conversation slope for reference
    wn = len(whole_slopes)
    w_neg = sum(1 for s in whole_slopes if s < 0)
    w_sign_p = binomial_p(w_neg, wn, 0.5)
    _, w_wz, w_wp = wilcoxon_signed_rank([-s for s in whole_slopes])
    w_sig = '***' if w_wp < 0.01 else '**' if w_wp < 0.05 else '*' if w_wp < 0.1 else 'n.s.'
    print(f"  {'WHOLE conversation':<24} {wn:<6} {mean(whole_slopes):<+14.4f} {median(whole_slopes):<+14.4f} {w_neg/wn:<8.0%} {w_sign_p:<10.4f} {w_wp:<10.4f} {w_sig}")
    print()

    # ══════════════════════════════════════════════════════════════════
    # 2. BETWEEN vs WITHIN DECOMPOSITION
    # ══════════════════════════════════════════════════════════════════
    print("=" * 80)
    print("BETWEEN-SEGMENT vs WITHIN-SEGMENT DECOMPOSITION")
    print("  Between = difference in segment means (step-like change)")
    print("  Within = average of within-segment slopes (continuous drift)")
    print("=" * 80)

    between_effects = []  # segment_mean_last - segment_mean_first
    within_effects = []   # average of within-segment slopes

    for path, msgs in conversations:
        per_msg = analyze_conversation(msgs)
        ys = [m['mean_cjk_wrun'] for m in per_msg]
        n = len(ys)
        seg_size = n // K

        seg_means = []
        seg_slopes_local = []
        for seg in range(K):
            start = seg * seg_size
            end = (seg + 1) * seg_size if seg < K - 1 else n
            seg_ys = ys[start:end]
            seg_means.append(mean(seg_ys))
            if len(seg_ys) >= 3:
                seg_slopes_local.append(linear_slope(seg_ys))

        if len(seg_means) >= 2 and seg_slopes_local:
            between_effects.append(seg_means[-1] - seg_means[0])
            within_effects.append(mean(seg_slopes_local))

    if between_effects and within_effects:
        bn = len(between_effects)

        b_neg = sum(1 for b in between_effects if b < 0)
        b_sign_p = binomial_p(b_neg, bn, 0.5)
        _, _, b_wp = wilcoxon_signed_rank([-b for b in between_effects])

        w_neg2 = sum(1 for w in within_effects if w < 0)
        w_sign_p2 = binomial_p(w_neg2, bn, 0.5)
        _, _, w_wp2 = wilcoxon_signed_rank([-w for w in within_effects])

        def sig(p):
            return '***' if p < 0.01 else '**' if p < 0.05 else '*' if p < 0.1 else 'n.s.'

        print(f"\n  {'Effect':<20} {'Mean':<14} {'Median':<14} {'%neg':<8} {'Sign p':<10} {'Wilcox p':<10} {'Sig?'}")
        print("  " + "-" * 85)
        print(f"  {'BETWEEN segments':<20} {mean(between_effects):<+14.2f} {median(between_effects):<+14.2f} {b_neg/bn:<8.0%} {b_sign_p:<10.4f} {b_wp:<10.4f} {sig(b_wp)}")
        print(f"  {'WITHIN segments':<20} {mean(within_effects):<+14.4f} {median(within_effects):<+14.4f} {w_neg2/bn:<8.0%} {w_sign_p2:<10.4f} {w_wp2:<10.4f} {sig(w_wp2)}")
        print()
        print(f"  Interpretation:")
        if b_wp < 0.05 and w_wp2 < 0.05:
            print(f"    BOTH significant → drift is continuous AND has between-segment steps")
            print(f"    (model drifts within phases, AND shifts further between phases)")
        elif b_wp < 0.05 and w_wp2 >= 0.05:
            print(f"    Only BETWEEN significant → drift is step-like (task-stage effect)")
        elif b_wp >= 0.05 and w_wp2 < 0.05:
            print(f"    Only WITHIN significant → drift is continuous (model-internal)")
        else:
            print(f"    Neither significant → weak or absent drift")
    print()

    # ══════════════════════════════════════════════════════════════════
    # 3. SAME-POSITION COMPARISON ACROSS LENGTHS
    # ══════════════════════════════════════════════════════════════════
    print("=" * 80)
    print("SAME-POSITION COMPARISON")
    print("  Compare mean_cjk_wrun at message positions 1-10 vs 11-20")
    print("  across conversations of different total lengths.")
    print("  If drift is task-stage: short convs and long convs should")
    print("  show similar values at position 1-10 (same early phase).")
    print("  If drift is model-internal: long convs might show lower")
    print("  CJK run even at early positions (accumulated context).")
    print("=" * 80)

    # Group conversations by length
    length_groups = [
        ("15-29", 15, 30),
        ("30-59", 30, 60),
        ("60-119", 60, 120),
        ("120+", 120, 99999),
    ]

    print(f"\n  Mean CJK word-run at fixed message positions:")
    print(f"  {'Length group':<14} {'N':<6} {'Pos 1-10':<12} {'Pos 11-20':<12} {'Pos 21-30':<12} {'Last 10':<12}")
    print("  " + "-" * 70)

    for label, lo, hi in length_groups:
        group = [(p, m) for p, m in conversations if lo <= len(m) < hi]
        if len(group) < 3:
            continue

        # Fixed positions
        pos_buckets = {
            '1-10': (0, 10),
            '11-20': (10, 20),
            '21-30': (20, 30),
        }

        vals = {}
        for bk_label, (bk_start, bk_end) in pos_buckets.items():
            bk_vals = []
            for _, msgs in group:
                per_msg = analyze_conversation(msgs)
                seg = [m['mean_cjk_wrun'] for m in per_msg[bk_start:bk_end]]
                if seg:
                    bk_vals.append(mean(seg))
            vals[bk_label] = mean(bk_vals) if bk_vals else float('nan')

        # Last 10
        last_vals = []
        for _, msgs in group:
            per_msg = analyze_conversation(msgs)
            seg = [m['mean_cjk_wrun'] for m in per_msg[-10:]]
            if seg:
                last_vals.append(mean(seg))
        vals['last'] = mean(last_vals) if last_vals else float('nan')

        v1 = f"{vals['1-10']:.1f}" if not math.isnan(vals['1-10']) else "—"
        v2 = f"{vals['11-20']:.1f}" if not math.isnan(vals['11-20']) else "—"
        v3 = f"{vals['21-30']:.1f}" if not math.isnan(vals['21-30']) else "—"
        vl = f"{vals['last']:.1f}" if not math.isnan(vals['last']) else "—"
        print(f"  {label:<14} {len(group):<6} {v1:<12} {v2:<12} {v3:<12} {vl:<12}")

    print()

    # ══════════════════════════════════════════════════════════════════
    # 4. DO THE SAME FOR ASCII RUN LENGTH (confirmation)
    # ══════════════════════════════════════════════════════════════════
    print("=" * 80)
    print("WITHIN-SEGMENT: Mean ASCII word-run length (INCREASE = drift)")
    print("=" * 80)

    asc_segment_slopes = [[] for _ in range(K)]

    for path, msgs in conversations:
        per_msg = analyze_conversation(msgs)
        ys = [m['mean_asc_wrun'] for m in per_msg]
        n = len(ys)
        seg_size = n // K
        for seg in range(K):
            start = seg * seg_size
            end = (seg + 1) * seg_size if seg < K - 1 else n
            seg_ys = ys[start:end]
            if len(seg_ys) >= 3:
                asc_segment_slopes[seg].append(linear_slope(seg_ys))

    print(f"\n  {'Segment':<24} {'N':<6} {'Mean slope':<14} {'Med slope':<14} {'%pos':<8} {'Sign p':<10} {'Wilcox p':<10} {'Sig?'}")
    print("  " + "-" * 100)

    for seg in range(K):
        ss = asc_segment_slopes[seg]
        if len(ss) < 5:
            continue
        sn = len(ss)
        n_pos = sum(1 for s in ss if s > 0)
        n_neg = sum(1 for s in ss if s < 0)
        sign_p = binomial_p(n_pos, n_pos + n_neg, 0.5)
        _, wz, wp = wilcoxon_signed_rank(ss)
        sig_s = '***' if wp < 0.01 else '**' if wp < 0.05 else '*' if wp < 0.1 else 'n.s.'
        print(f"  {seg_labels[seg]:<24} {sn:<6} {mean(ss):<+14.4f} {median(ss):<+14.4f} {n_pos/sn:<8.0%} {sign_p:<10.4f} {wp:<10.4f} {sig_s}")
    print()


if __name__ == "__main__":
    main()
