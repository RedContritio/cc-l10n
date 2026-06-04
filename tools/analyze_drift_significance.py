#!/usr/bin/env python3
"""
Stronger statistical evidence for language drift:

1. Aggregate normalized curve — pool all conversations, normalize position to [0,1],
   show average EN% rises with conversation progress.
2. Per-conversation Mann-Kendall trend test — non-parametric monotonic trend test,
   gives p-value per conversation. Count how many have significant upward trend.
3. Permutation test — shuffle message order within each conversation 1000 times,
   compare real slope to shuffled distribution. Per-conversation p-value.
4. Sign test — what fraction of conversations have positive slope? Binomial test
   against 50%.
5. Wilcoxon signed-rank test — are slopes systematically > 0?
"""

import json
import math
import os
import re
import sys
from collections import defaultdict
from pathlib import Path
import random


# ── Char classification ──────────────────────────────────────────────

def is_cjk(ch):
    cp = ord(ch)
    return any([
        0x4E00 <= cp <= 0x9FFF, 0x3400 <= cp <= 0x4DBF,
        0x20000 <= cp <= 0x2A6DF, 0x2A700 <= cp <= 0x2B73F,
        0x2B740 <= cp <= 0x2B81F, 0x2B820 <= cp <= 0x2CEAF,
        0xF900 <= cp <= 0xFAFF, 0x2F800 <= cp <= 0x2FA1F,
    ])


def is_ascii_letter(ch):
    return ('a' <= ch <= 'z') or ('A' <= ch <= 'Z')


def extract_text_blocks(content):
    texts = []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                texts.append(block.get("text", ""))
    elif isinstance(content, str):
        texts.append(content)
    return "\n".join(texts)


def strip_code_and_markup(text):
    text = re.sub(r'```[\s\S]*?```', ' ', text)
    text = re.sub(r'`[^`]+`', ' ', text)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'https?://\S+', ' ', text)
    text = re.sub(r'(?<!\w)/[\w/._-]{5,}', ' ', text)
    return text


def count_chars(text):
    cjk = 0
    asc = 0
    for ch in text:
        if is_cjk(ch):
            cjk += 1
        elif is_ascii_letter(ch):
            asc += 1
    return cjk, asc


# ── Load per-message EN% for each conversation ───────────────────────

def load_conversation(path):
    """Return list of (cjk, asc, total, en_ratio) per assistant text message."""
    messages = []
    is_subagent = Path(path).stem.startswith("agent-")

    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("type") != "assistant":
                continue
            msg = obj.get("message", {})
            if msg.get("role") != "assistant":
                continue
            content = msg.get("content", [])
            text = extract_text_blocks(content)
            if not text.strip():
                continue
            cleaned = strip_code_and_markup(text)
            cjk, asc = count_chars(cleaned)
            total = cjk + asc
            if total < 5:
                continue
            messages.append((cjk, asc, total, asc / total))

    return messages, is_subagent


# ── Statistical tests ─────────────────────────────────────────────────

def linear_slope(ys):
    n = len(ys)
    if n < 2:
        return 0
    xs = list(range(n))
    sx = sum(xs)
    sy = sum(ys)
    sxx = sum(x * x for x in xs)
    sxy = sum(x * y for x, y in zip(xs, ys))
    denom = n * sxx - sx * sx
    if denom == 0:
        return 0
    return (n * sxy - sx * sy) / denom


def mann_kendall(ys):
    """Mann-Kendall trend test. Returns (S, z, p_value, trend_direction)."""
    n = len(ys)
    if n < 4:
        return 0, 0, 1.0, "none"

    s = 0
    for i in range(n - 1):
        for j in range(i + 1, n):
            diff = ys[j] - ys[i]
            if diff > 0:
                s += 1
            elif diff < 0:
                s -= 1

    # Variance (without ties correction for simplicity)
    var_s = n * (n - 1) * (2 * n + 5) / 18

    if s > 0:
        z = (s - 1) / math.sqrt(var_s) if var_s > 0 else 0
    elif s < 0:
        z = (s + 1) / math.sqrt(var_s) if var_s > 0 else 0
    else:
        z = 0

    # Two-tailed p-value from normal approximation
    p = 2 * (1 - normal_cdf(abs(z)))

    if p < 0.05:
        trend = "up" if s > 0 else "down"
    else:
        trend = "none"

    return s, z, p, trend


def normal_cdf(x):
    """Approximation of the standard normal CDF."""
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def permutation_test(ys, n_perms=2000):
    """Test if the slope is significantly positive by permutation.
    Returns (real_slope, p_value)."""
    real_slope = linear_slope(ys)
    count_ge = 0
    ys_copy = list(ys)
    for _ in range(n_perms):
        random.shuffle(ys_copy)
        perm_slope = linear_slope(ys_copy)
        if perm_slope >= real_slope:
            count_ge += 1
    p = count_ge / n_perms
    return real_slope, p


def binomial_p(k, n, p0=0.5):
    """One-sided binomial test: P(X >= k) under H0: p = p0."""
    if k <= n * p0:
        return 1.0
    # Normal approximation
    mu = n * p0
    sigma = math.sqrt(n * p0 * (1 - p0))
    if sigma == 0:
        return 0.0
    z = (k - 0.5 - mu) / sigma
    return 1 - normal_cdf(z)


def wilcoxon_signed_rank(values):
    """Wilcoxon signed-rank test: are values systematically > 0?
    Returns (W+, z, p_value)."""
    # Remove zeros
    nonzero = [(abs(v), 1 if v > 0 else -1) for v in values if v != 0]
    n = len(nonzero)
    if n < 5:
        return 0, 0, 1.0

    # Rank by absolute value
    nonzero.sort(key=lambda x: x[0])
    ranks = []
    i = 0
    while i < n:
        j = i
        while j < n - 1 and nonzero[j + 1][0] == nonzero[j][0]:
            j += 1
        avg_rank = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks.append((avg_rank, nonzero[k][1]))
        i = j + 1

    w_plus = sum(r for r, s in ranks if s > 0)
    w_minus = sum(r for r, s in ranks if s < 0)

    # Normal approximation
    mu = n * (n + 1) / 4
    sigma = math.sqrt(n * (n + 1) * (2 * n + 1) / 24)
    if sigma == 0:
        return w_plus, 0, 1.0
    z = (w_plus - mu) / sigma
    p = 1 - normal_cdf(z)  # one-tailed
    return w_plus, z, p


# ── Main ──────────────────────────────────────────────────────────────

def main():
    random.seed(42)

    base = Path(os.path.expanduser("~/.claude/projects"))
    all_jsonl = sorted(base.rglob("*.jsonl"))
    filtered = [p for p in all_jsonl if "cc-zh" not in str(p) and "cc_zh" not in str(p)]

    print(f"Scanning {len(filtered)} JSONL files...")

    conversations = []  # list of (path, messages_en_ratios, is_subagent)
    for path in filtered:
        try:
            msgs, is_sub = load_conversation(path)
        except Exception:
            continue
        if is_sub:
            continue
        if len(msgs) < 8:
            continue
        total_cjk = sum(m[0] for m in msgs)
        if total_cjk < 10:
            continue
        en_ratios = [m[3] for m in msgs]
        conversations.append((str(path), en_ratios))

    n_conv = len(conversations)
    print(f"Eligible main conversations (≥8 msgs, CJK≥10, non-subagent): {n_conv}")
    print()

    # ══════════════════════════════════════════════════════════════════
    # 1. AGGREGATE NORMALIZED CURVE
    # ══════════════════════════════════════════════════════════════════
    print("=" * 70)
    print("1. AGGREGATE NORMALIZED CURVE")
    print("   Normalize each conversation to [0,1] progress,")
    print("   average EN% at each percentile bucket.")
    print("=" * 70)

    N_BUCKETS = 20
    bucket_sums = [0.0] * N_BUCKETS
    bucket_counts = [0] * N_BUCKETS

    for _, ens in conversations:
        n = len(ens)
        for i, en in enumerate(ens):
            progress = i / max(1, n - 1)  # 0.0 to 1.0
            bucket = min(N_BUCKETS - 1, int(progress * N_BUCKETS))
            bucket_sums[bucket] += en
            bucket_counts[bucket] += 1

    print(f"\n  {'Progress':<12} {'Avg EN%':<10} {'N':<8} {'Visual'}")
    print("  " + "-" * 60)
    bucket_avgs = []
    for i in range(N_BUCKETS):
        avg = bucket_sums[i] / bucket_counts[i] if bucket_counts[i] > 0 else 0
        bucket_avgs.append(avg)
        pct = (i + 0.5) / N_BUCKETS * 100
        bar = "█" * int(avg * 50)
        print(f"  {pct:5.0f}%      {avg:<10.1%} {bucket_counts[i]:<8} |{bar}")

    start_avg = sum(bucket_avgs[:3]) / 3
    end_avg = sum(bucket_avgs[-3:]) / 3
    print(f"\n  Start avg (0-15%):  {start_avg:.1%}")
    print(f"  End avg (85-100%):  {end_avg:.1%}")
    print(f"  Rise:               {(end_avg - start_avg) * 100:+.1f} pp")
    print()

    # ══════════════════════════════════════════════════════════════════
    # 2. MANN-KENDALL TREND TEST (per conversation)
    # ══════════════════════════════════════════════════════════════════
    print("=" * 70)
    print("2. MANN-KENDALL TREND TEST (per conversation)")
    print("   Non-parametric test for monotonic trend in EN% series.")
    print("=" * 70)

    mk_results = []
    for path, ens in conversations:
        s, z, p, trend = mann_kendall(ens)
        mk_results.append((path, len(ens), s, z, p, trend))

    sig_up = sum(1 for _, _, _, _, p, t in mk_results if p < 0.05 and t == "up")
    sig_down = sum(1 for _, _, _, _, p, t in mk_results if p < 0.05 and t == "down")
    not_sig = n_conv - sig_up - sig_down

    print(f"\n  Significant upward trend (p<0.05):   {sig_up:>4} ({sig_up/n_conv:.0%})")
    print(f"  Significant downward trend (p<0.05): {sig_down:>4} ({sig_down/n_conv:.0%})")
    print(f"  No significant trend:                {not_sig:>4} ({not_sig/n_conv:.0%})")

    # Among significant results, ratio of up:down
    if sig_up + sig_down > 0:
        print(f"  Up:Down ratio among significant:     {sig_up}:{sig_down} = {sig_up/(sig_up+sig_down):.0%} upward")

    # By conversation length
    print(f"\n  --- Mann-Kendall by conversation length ---")
    print(f"  {'Length':<14} {'N':<6} {'Sig up':<10} {'Sig down':<10} {'%sig up'}")
    length_groups = [(8, 20), (20, 50), (50, 100), (100, 200), (200, 9999)]
    for lo, hi in length_groups:
        subset = [(n, t) for _, n, _, _, p, t in mk_results if lo <= n < hi]
        if not subset:
            continue
        sn = len(subset)
        su = sum(1 for _, t in subset if t == "up")
        sd = sum(1 for _, t in subset if t == "down")
        label = f"{lo}-{hi-1}" if hi < 9999 else f"{lo}+"
        print(f"  {label:<14} {sn:<6} {su:<10} {sd:<10} {su/sn:.0%}")
    print()

    # ══════════════════════════════════════════════════════════════════
    # 3. PERMUTATION TEST (per conversation)
    # ══════════════════════════════════════════════════════════════════
    print("=" * 70)
    print("3. PERMUTATION TEST (2000 shuffles per conversation)")
    print("   H0: message order doesn't matter. H1: EN% increases over time.")
    print("=" * 70)

    perm_results = []
    for idx, (path, ens) in enumerate(conversations):
        if (idx + 1) % 20 == 0:
            print(f"  processing {idx+1}/{n_conv}...", file=sys.stderr)
        real_slope, p = permutation_test(ens, n_perms=2000)
        perm_results.append((path, len(ens), real_slope, p))

    sig_perm = sum(1 for _, _, s, p in perm_results if p < 0.05 and s > 0)
    sig_perm_01 = sum(1 for _, _, s, p in perm_results if p < 0.01 and s > 0)
    sig_neg = sum(1 for _, _, s, p in perm_results if p > 0.95)  # slope significantly negative

    print(f"\n  Significant positive slope (p<0.05):  {sig_perm:>4} ({sig_perm/n_conv:.0%})")
    print(f"  Significant positive slope (p<0.01):  {sig_perm_01:>4} ({sig_perm_01/n_conv:.0%})")
    print(f"  Significant negative slope (p>0.95):  {sig_neg:>4} ({sig_neg/n_conv:.0%})")
    print(f"  Non-significant:                      {n_conv-sig_perm-sig_neg:>4} ({(n_conv-sig_perm-sig_neg)/n_conv:.0%})")

    # By length
    print(f"\n  --- Permutation test by conversation length ---")
    print(f"  {'Length':<14} {'N':<6} {'Sig+':<8} {'Sig-':<8} {'%sig+'}")
    for lo, hi in length_groups:
        subset = [(n, s, p) for _, n, s, p in perm_results if lo <= n < hi]
        if not subset:
            continue
        sn = len(subset)
        sp = sum(1 for _, s, p in subset if p < 0.05 and s > 0)
        sm = sum(1 for _, _, p in subset if p > 0.95)
        label = f"{lo}-{hi-1}" if hi < 9999 else f"{lo}+"
        print(f"  {label:<14} {sn:<6} {sp:<8} {sm:<8} {sp/sn:.0%}")
    print()

    # ══════════════════════════════════════════════════════════════════
    # 4. SIGN TEST + WILCOXON
    # ══════════════════════════════════════════════════════════════════
    print("=" * 70)
    print("4. POPULATION-LEVEL TESTS")
    print("   Are slopes systematically > 0 across all conversations?")
    print("=" * 70)

    all_slopes = [linear_slope(ens) for _, ens in conversations]
    n_pos = sum(1 for s in all_slopes if s > 0)
    n_neg = sum(1 for s in all_slopes if s < 0)
    n_zero = n_conv - n_pos - n_neg

    sign_p = binomial_p(n_pos, n_pos + n_neg, 0.5)
    w_plus, w_z, w_p = wilcoxon_signed_rank(all_slopes)

    print(f"\n  Slopes > 0: {n_pos}/{n_conv} ({n_pos/n_conv:.0%})")
    print(f"  Slopes < 0: {n_neg}/{n_conv} ({n_neg/n_conv:.0%})")
    print(f"  Slopes = 0: {n_zero}/{n_conv}")
    print()
    print(f"  Sign test (H0: P(slope>0) = 0.5):")
    print(f"    p = {sign_p:.4f}  {'*** SIGNIFICANT' if sign_p < 0.05 else '(not significant)'}")
    print()
    print(f"  Wilcoxon signed-rank test (H0: median slope = 0):")
    print(f"    W+ = {w_plus:.0f}, z = {w_z:.3f}")
    print(f"    p = {w_p:.4f}  {'*** SIGNIFICANT' if w_p < 0.05 else '(not significant)'}")
    print()

    avg_slope = sum(all_slopes) / n_conv
    med_slope = sorted(all_slopes)[n_conv // 2]
    print(f"  Mean slope:   {avg_slope*100:+.3f} pp/msg")
    print(f"  Median slope: {med_slope*100:+.3f} pp/msg")
    print()

    # ══════════════════════════════════════════════════════════════════
    # 5. LENGTH-STRATIFIED SIGN TEST
    # ══════════════════════════════════════════════════════════════════
    print("=" * 70)
    print("5. SIGN TEST BY LENGTH BUCKET")
    print("=" * 70)
    print(f"\n  {'Length':<14} {'N':<6} {'Pos':<6} {'Neg':<6} {'%pos':<8} {'Sign p':<10} {'Sig?'}")
    for lo, hi in length_groups:
        subset_slopes = [all_slopes[i] for i in range(n_conv)
                         if lo <= len(conversations[i][1]) < hi]
        if len(subset_slopes) < 3:
            continue
        sn = len(subset_slopes)
        sp = sum(1 for s in subset_slopes if s > 0)
        sm = sum(1 for s in subset_slopes if s < 0)
        p = binomial_p(sp, sp + sm, 0.5) if sp + sm > 0 else 1.0
        label = f"{lo}-{hi-1}" if hi < 9999 else f"{lo}+"
        sig = "***" if p < 0.01 else "**" if p < 0.05 else "*" if p < 0.1 else ""
        print(f"  {label:<14} {sn:<6} {sp:<6} {sm:<6} {sp/sn:<8.0%} {p:<10.4f} {sig}")
    print()


if __name__ == "__main__":
    main()
