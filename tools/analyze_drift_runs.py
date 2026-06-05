#!/usr/bin/env python3
"""
Language drift analysis based on CJK/ASCII "runs" — contiguous segments
of the same script type.

Key metrics (immune to technical-term density confound):
- Mean CJK run length: how many consecutive CJK characters per Chinese segment
- Mean ASCII run length: how many consecutive ASCII letters per English segment
- CJK run count ratio: fraction of all runs that are CJK-dominant
- Max CJK run length: longest uninterrupted Chinese stretch

If the model is genuinely shifting from Chinese prose to English prose
(not just sprinkling more English terms into Chinese), we expect:
- CJK run length DECREASING (Chinese shrinks to single-character glue)
- ASCII run length INCREASING (English expands to full sentences)
- CJK run count ratio DECREASING (fewer Chinese segments)
"""

import json
import math
import os
import re
import sys
import random
from collections import defaultdict
from pathlib import Path

from analyze_common import extract_project_name


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
    # Relative paths: ./foo/bar
    text = re.sub(r'\./[\w/._-]{3,}', ' ', text)
    # camelCase identifiers (≥2 parts)
    text = re.sub(r'\b[a-z]+(?:[A-Z][a-z]*)+\b', ' ', text)
    # PascalCase identifiers (≥2 parts, e.g. TaskCreate, WebFetch)
    text = re.sub(r'\b[A-Z][a-z]+(?:[A-Z][a-z]*)+\b', ' ', text)
    # snake_case identifiers
    text = re.sub(r'\b[a-zA-Z]+(?:_[a-zA-Z]+)+\b', ' ', text)
    # dot.notation (e.g. module.func)
    text = re.sub(r'\b[a-zA-Z]+(?:\.[a-zA-Z]+)+\b', ' ', text)
    # Hex hashes (7+ hex chars)
    text = re.sub(r'\b[0-9a-f]{7,}\b', ' ', text)
    return text


def tokenize(text):
    """Tokenize into (type, token) pairs.
    Each CJK character is a separate token (Chinese has no whitespace word boundaries).
    Each contiguous ASCII letter sequence is one token (English word).
    Non-letter/non-CJK characters are boundaries and discarded.
    Uses is_cjk() for full 8-range coverage."""
    tokens = []  # list of 'cjk' or 'ascii'
    i = 0
    while i < len(text):
        ch = text[i]
        if is_cjk(ch):
            tokens.append('cjk')
            i += 1
        elif is_ascii_letter(ch):
            # Consume contiguous ASCII letters as one English word
            j = i + 1
            while j < len(text) and is_ascii_letter(text[j]):
                j += 1
            tokens.append('ascii')
            i = j
        else:
            i += 1
    return tokens


def compute_word_runs(text):
    """Split text into alternating CJK/ASCII "word runs".

    Tokenization: each CJK character = 1 token, each English word = 1 token.
    This makes the two script types comparable in units.

    A word run = consecutive tokens of the same script type.
    Returns (cjk_word_runs, ascii_word_runs) as lists of run lengths (in tokens).
    """
    tokens = tokenize(text)
    if not tokens:
        return [], []

    cjk_word_runs = []
    ascii_word_runs = []
    current_type = tokens[0]
    current_len = 1

    for tok_type in tokens[1:]:
        if tok_type == current_type:
            current_len += 1
        else:
            if current_type == 'cjk':
                cjk_word_runs.append(current_len)
            else:
                ascii_word_runs.append(current_len)
            current_type = tok_type
            current_len = 1

    # Flush last run
    if current_type == 'cjk':
        cjk_word_runs.append(current_len)
    else:
        ascii_word_runs.append(current_len)

    return cjk_word_runs, ascii_word_runs


def mean(xs):
    return sum(xs) / len(xs) if xs else 0


def median(xs):
    if not xs:
        return 0
    s = sorted(xs)
    return s[len(s) // 2]


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


def normal_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def binomial_p(k, n, p0=0.5):
    if n == 0:
        return 1.0
    if k <= n * p0:
        return 1.0
    mu = n * p0
    sigma = math.sqrt(n * p0 * (1 - p0))
    if sigma == 0:
        return 0.0
    z = (k - 0.5 - mu) / sigma
    return 1 - normal_cdf(z)


def wilcoxon_signed_rank(values):
    """单尾 Wilcoxon signed-rank (正态近似, 无连续性修正).

    z=(W+-mu)/sigma 未减 0.5 连续性修正, 对小样本轻微 anti-conservative (p 偏小约
    几个百分点); p<0.0001 量级宣告时可忽略, p~0.05 边界判定需补修正。与
    analyze_drift_significance.py 同名函数保持一致行为。
    """
    nonzero = [(abs(v), 1 if v > 0 else -1) for v in values if v != 0]
    n = len(nonzero)
    if n < 5:
        return 0, 0, 1.0
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
    mu = n * (n + 1) / 4
    sigma = math.sqrt(n * (n + 1) * (2 * n + 1) / 24)
    if sigma == 0:
        return w_plus, 0, 1.0
    z = (w_plus - mu) / sigma
    p = 1 - normal_cdf(z)
    return w_plus, z, p


def load_conversation_messages(path, role="assistant"):
    """Return list of cleaned text per message of given role.
    For user messages, filter out system-generated content (task notifications,
    command outputs, hook results)."""
    messages = []
    target_type = role  # 'assistant' or 'user'
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("type") != target_type:
                continue
            msg = obj.get("message", {})
            if msg.get("role") != role:
                continue
            content = msg.get("content", [])
            text = extract_text_blocks(content)
            if not text.strip():
                continue
            # Filter out system-generated user messages
            if role == "user":
                if '<task-notification>' in text or '<local-command' in text:
                    continue
                if '<command-name>' in text or '<command-message>' in text:
                    continue
                # Tool results (from toolUseResult) are system content
                if obj.get("toolUseResult"):
                    continue
            cleaned = strip_code_and_markup(text)
            n_cjk = sum(1 for c in cleaned if is_cjk(c))
            n_asc = sum(1 for c in cleaned if is_ascii_letter(c))
            if n_cjk + n_asc < 5:
                continue
            messages.append(cleaned)
    return messages


def analyze_conversation(messages):
    """For each message, compute run-based metrics.
    Returns list of per-message dicts."""
    results = []
    for msg in messages:
        cjk_wr, asc_wr = compute_word_runs(msg)
        total_runs = len(cjk_wr) + len(asc_wr)
        results.append({
            'mean_cjk_wrun': mean(cjk_wr),
            'mean_asc_wrun': mean(asc_wr),
            'max_cjk_wrun': max(cjk_wr) if cjk_wr else 0,
            'max_asc_wrun': max(asc_wr) if asc_wr else 0,
            'cjk_run_ratio': len(cjk_wr) / total_runs if total_runs > 0 else 0,
            'n_cjk_runs': len(cjk_wr),
            'n_asc_runs': len(asc_wr),
        })
    return results


def main():
    random.seed(42)
    base = Path(os.path.expanduser("~/.claude/projects"))
    all_jsonl = sorted(base.rglob("*.jsonl"))
    filtered = [p for p in all_jsonl if "cc-zh" not in str(p) and "cc_zh" not in str(p)]

    print(f"Scanning {len(filtered)} JSONL files...")

    conversations = []  # (path, assistant_msgs, user_msgs)
    for path in filtered:
        if Path(path).stem.startswith("agent-"):
            continue
        try:
            a_msgs = load_conversation_messages(path, role="assistant")
            u_msgs = load_conversation_messages(path, role="user")
        except Exception:
            continue
        if len(a_msgs) < 8:
            continue
        # Need meaningful Chinese in assistant messages
        all_text = " ".join(a_msgs)
        n_cjk = sum(1 for c in all_text if is_cjk(c))
        if n_cjk < 30:
            continue
        conversations.append((str(path), a_msgs, u_msgs))

    n_conv = len(conversations)
    print(f"Eligible conversations: {n_conv}")
    print()

    # ── Per-conversation metrics ──────────────────────────────────────
    METRICS = [
        ('mean_cjk_wrun', 'Mean CJK word-run length', 'DECREASE = drift'),
        ('mean_asc_wrun', 'Mean ASCII word-run length', 'INCREASE = drift'),
        ('max_cjk_wrun', 'Max CJK word-run length', 'DECREASE = drift'),
        ('cjk_run_ratio', 'CJK run count ratio', 'DECREASE = drift'),
    ]

    # For each metric: compute per-conversation slope, do sign test + Wilcoxon
    print("=" * 80)
    print("RUN-BASED METRICS: Per-conversation slope analysis")
    print("  (word run = consecutive tokens of same script type)")
    print("=" * 80)

    for metric_key, metric_name, drift_dir in METRICS:
        slopes = []
        for path, msgs, _ in conversations:
            per_msg = analyze_conversation(msgs)
            ys = [m[metric_key] for m in per_msg]
            s = linear_slope(ys)
            slopes.append(s)

        # Determine expected sign for drift
        if 'DECREASE' in drift_dir:
            # Drift = negative slope
            n_drift_dir = sum(1 for s in slopes if s < 0)
            drift_label = "negative"
            # For sign test: test if slopes are systematically negative
            n_pos = sum(1 for s in slopes if s > 0)
            n_neg = sum(1 for s in slopes if s < 0)
            sign_p = binomial_p(n_neg, n_pos + n_neg, 0.5)
            # Wilcoxon: test if median < 0 (negate values)
            _, wz, wp = wilcoxon_signed_rank([-s for s in slopes])
        else:
            # Drift = positive slope
            n_drift_dir = sum(1 for s in slopes if s > 0)
            drift_label = "positive"
            n_pos = sum(1 for s in slopes if s > 0)
            n_neg = sum(1 for s in slopes if s < 0)
            sign_p = binomial_p(n_pos, n_pos + n_neg, 0.5)
            _, wz, wp = wilcoxon_signed_rank(slopes)

        avg_s = mean(slopes)
        med_s = median(slopes)

        print(f"\n  ── {metric_name} ({drift_dir}) ──")
        print(f"  Mean slope:  {avg_s:+.4f} /msg")
        print(f"  Median slope:{med_s:+.4f} /msg")
        print(f"  {drift_label} slopes: {n_drift_dir}/{n_conv} ({n_drift_dir/n_conv:.0%})")
        print(f"  Sign test p:    {sign_p:.4f}  {'***' if sign_p < 0.01 else '**' if sign_p < 0.05 else '*' if sign_p < 0.1 else ''}")
        print(f"  Wilcoxon z={wz:+.3f} p={wp:.4f}  {'***' if wp < 0.01 else '**' if wp < 0.05 else '*' if wp < 0.1 else ''}")

    print()

    # ── Aggregate normalized curve (word-run based) ───────────────────
    N_BUCKETS = 10
    print("=" * 80)
    print("AGGREGATE NORMALIZED CURVE (word-run metrics)")
    print("=" * 80)

    for metric_key, metric_name, drift_dir in METRICS:
        # Per-conversation bucket profiles, then average across conversations
        conv_profiles = []  # list of N_BUCKETS-length lists

        for path, msgs, _ in conversations:
            per_msg = analyze_conversation(msgs)
            n = len(per_msg)
            buckets = [[] for _ in range(N_BUCKETS)]
            for i, m in enumerate(per_msg):
                progress = i / max(1, n - 1)
                bucket = min(N_BUCKETS - 1, int(progress * N_BUCKETS))
                buckets[bucket].append(m[metric_key])
            profile = [mean(b) if b else None for b in buckets]
            conv_profiles.append(profile)

        print(f"\n  ── {metric_name} ({drift_dir}) ──")
        print(f"  {'Progress':<12} {'Value':<10} {'N convs':<9} {'Visual'}")
        avgs = []
        for i in range(N_BUCKETS):
            vals = [p[i] for p in conv_profiles if p[i] is not None]
            avg = mean(vals) if vals else 0
            avgs.append(avg)
            pct = (i + 0.5) / N_BUCKETS * 100
            bar = "█" * int(avg * 10) if avg < 20 else "█" * min(40, int(avg))
            print(f"  {pct:5.0f}%      {avg:<10.2f} {len(vals):<9} |{bar}")

        start = mean(avgs[:2])
        end = mean(avgs[-2:])
        print(f"  Start (0-20%):  {start:.2f}")
        print(f"  End (80-100%):  {end:.2f}")
        delta = end - start
        pct_change = delta / start * 100 if start != 0 else 0
        print(f"  Δ = {delta:+.2f}  ({pct_change:+.0f}%)")

    print()

    # ── Example: show actual runs for a conversation ──────────────────
    # Pick the conversation with the most messages for illustration
    longest = max(conversations, key=lambda x: len(x[1]))
    path, msgs, _ = longest
    n = len(msgs)
    per_msg = analyze_conversation(msgs)

    proj = extract_project_name(path)
    sid = Path(path).stem[:12]
    print(f"=== Example: {proj} / {sid} ({n} msgs) ===")
    print(f"{'#':<6} {'CJK wrun':<12} {'ASC wrun':<12} {'Max CJK':<10} {'CJK ratio':<10} {'Preview'}")
    show = list(range(min(5, n))) + list(range(max(5, n - 5), n))
    show = sorted(set(show))
    prev = -1
    for i in show:
        if i > prev + 1 and prev >= 0:
            print("  ...")
        m = per_msg[i]
        preview = msgs[i].replace('\n', ' ')[:50]
        print(f"{i+1:<6} {m['mean_cjk_wrun']:<12.1f} {m['mean_asc_wrun']:<12.1f} {m['max_cjk_wrun']:<10} {m['cjk_run_ratio']:<10.2f} {preview}")
        prev = i
    print()

    # ── Length-stratified sign test for mean_cjk_wrun ─────────────────
    print("=" * 80)
    print("LENGTH-STRATIFIED: mean_cjk_wrun slope (DECREASE = drift)")
    print("=" * 80)
    all_cjk_slopes = []
    conv_lens = []
    for path, msgs, _ in conversations:
        per_msg = analyze_conversation(msgs)
        ys = [m['mean_cjk_wrun'] for m in per_msg]
        s = linear_slope(ys)
        all_cjk_slopes.append(s)
        conv_lens.append(len(msgs))

    print(f"\n  {'Length':<14} {'N':<6} {'Neg':<6} {'Pos':<6} {'%neg':<8} {'Sign p':<10} {'Avg slope':<12} {'Sig?'}")
    for lo, hi in [(8, 20), (20, 50), (50, 100), (100, 200), (200, 9999)]:
        subset = [(all_cjk_slopes[i], conv_lens[i]) for i in range(n_conv) if lo <= conv_lens[i] < hi]
        if len(subset) < 3:
            continue
        sn = len(subset)
        ss = [s for s, _ in subset]
        n_neg = sum(1 for s in ss if s < 0)
        n_pos = sum(1 for s in ss if s > 0)
        p = binomial_p(n_neg, n_neg + n_pos, 0.5)
        avg_s = mean(ss)
        label = f"{lo}-{hi-1}" if hi < 9999 else f"{lo}+"
        sig = "***" if p < 0.01 else "**" if p < 0.05 else "*" if p < 0.1 else ""
        print(f"  {label:<14} {sn:<6} {n_neg:<6} {n_pos:<6} {n_neg/sn:<8.0%} {p:<10.4f} {avg_s:<+12.4f} {sig}")


    # ══════════════════════════════════════════════════════════════════
    # USER vs ASSISTANT COMPARISON (control group)
    # If drift is model behavior (not task-stage), user messages should
    # NOT show the same CJK run shortening trend.
    # ══════════════════════════════════════════════════════════════════
    print()
    print("=" * 80)
    print("USER vs ASSISTANT COMPARISON (control)")
    print("  If CJK run shortening is task-stage effect, users should show it too.")
    print("  If it's model-specific drift, only assistant should show it.")
    print("=" * 80)

    # Filter to conversations with enough user messages
    both = [(p, a, u) for p, a, u in conversations if len(u) >= 5]
    print(f"\n  Conversations with ≥5 user text messages: {len(both)}")
    if not both:
        print("  (not enough data for comparison)")
    else:
        KEY_METRICS = [
            ('mean_cjk_wrun', 'Mean CJK word-run length', 'DECREASE = drift'),
            ('mean_asc_wrun', 'Mean ASCII word-run length', 'INCREASE = drift'),
        ]
        for metric_key, metric_name, drift_dir in KEY_METRICS:
            a_slopes = []
            u_slopes = []
            for path, a_msgs, u_msgs in both:
                a_per = analyze_conversation(a_msgs)
                u_per = analyze_conversation(u_msgs)
                a_ys = [m[metric_key] for m in a_per]
                u_ys = [m[metric_key] for m in u_per]
                a_slopes.append(linear_slope(a_ys))
                u_slopes.append(linear_slope(u_ys))

            bn = len(both)
            if 'DECREASE' in drift_dir:
                a_dir = sum(1 for s in a_slopes if s < 0)
                u_dir = sum(1 for s in u_slopes if s < 0)
                dir_label = "negative"
                a_sign_p = binomial_p(a_dir, a_dir + (bn - a_dir), 0.5)
                u_sign_p = binomial_p(u_dir, u_dir + (bn - u_dir), 0.5)
                _, a_wz, a_wp = wilcoxon_signed_rank([-s for s in a_slopes])
                _, u_wz, u_wp = wilcoxon_signed_rank([-s for s in u_slopes])
            else:
                a_dir = sum(1 for s in a_slopes if s > 0)
                u_dir = sum(1 for s in u_slopes if s > 0)
                dir_label = "positive"
                a_sign_p = binomial_p(a_dir, bn, 0.5)
                u_sign_p = binomial_p(u_dir, bn, 0.5)
                _, a_wz, a_wp = wilcoxon_signed_rank(a_slopes)
                _, u_wz, u_wp = wilcoxon_signed_rank(u_slopes)

            def sig(p):
                return '***' if p < 0.01 else '**' if p < 0.05 else '*' if p < 0.1 else 'n.s.'

            print(f"\n  ── {metric_name} ──")
            print(f"  {'Role':<12} {'Mean slope':<14} {'Med slope':<14} {'%{d}':<10} {'Sign p':<10} {'Wilcox p':<10} {'Sig?'}".format(d=dir_label))
            print(f"  {'ASSISTANT':<12} {mean(a_slopes):<+14.4f} {median(a_slopes):<+14.4f} {a_dir/bn:<10.0%} {a_sign_p:<10.4f} {a_wp:<10.4f} {sig(a_wp)}")
            print(f"  {'USER':<12} {mean(u_slopes):<+14.4f} {median(u_slopes):<+14.4f} {u_dir/bn:<10.0%} {u_sign_p:<10.4f} {u_wp:<10.4f} {sig(u_wp)}")

        # Aggregate normalized curve comparison
        print(f"\n  --- Aggregate normalized curve: mean_cjk_wrun ---")
        N_B = 5
        for role_label, msg_idx in [("ASSISTANT", 1), ("USER", 2)]:
            conv_profiles = []
            for item in both:
                msgs = item[msg_idx]
                per_msg = analyze_conversation(msgs)
                n = len(per_msg)
                if n < 2:
                    continue
                buckets = [[] for _ in range(N_B)]
                for i, m in enumerate(per_msg):
                    progress = i / max(1, n - 1)
                    bucket = min(N_B - 1, int(progress * N_B))
                    buckets[bucket].append(m['mean_cjk_wrun'])
                profile = [mean(b) if b else None for b in buckets]
                conv_profiles.append(profile)

            print(f"    {role_label}:")
            avgs = []
            for i in range(N_B):
                vals = [p[i] for p in conv_profiles if p[i] is not None]
                avg = mean(vals) if vals else 0
                avgs.append(avg)
                pct = (i + 0.5) / N_B * 100
                bar = "█" * max(1, int(avg))
                print(f"      {pct:5.0f}%  {avg:>8.1f}  |{bar}")
            if avgs[0] > 0:
                delta = avgs[-1] - avgs[0]
                pct_c = delta / avgs[0] * 100
                print(f"      Δ = {delta:+.1f}  ({pct_c:+.0f}%)")
            print()


if __name__ == "__main__":
    main()
