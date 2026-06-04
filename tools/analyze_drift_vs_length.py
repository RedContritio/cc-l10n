#!/usr/bin/env python3
"""Verify: does drift correlate with conversation length?

Reads the TSV report, applies minimal filtering, and computes:
- Pearson/Spearman correlation between n_messages and half_delta_pp
- Linear regression: drift = a + b * n_messages
- Bucketed view with finer granularity
- Scatter plot (ASCII)
"""

import csv
import math
import sys
from pathlib import Path


def pearson(xs, ys):
    n = len(xs)
    if n < 3:
        return 0, 0, 0
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    if sxx == 0 or syy == 0:
        return 0, 0, 0
    r = sxy / math.sqrt(sxx * syy)
    b = sxy / sxx
    a = my - b * mx
    return r, a, b


def spearman(xs, ys):
    n = len(xs)
    if n < 3:
        return 0

    def rank(vs):
        indexed = sorted(range(n), key=lambda i: vs[i])
        ranks = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j < n - 1 and vs[indexed[j + 1]] == vs[indexed[j]]:
                j += 1
            avg_rank = (i + j) / 2 + 1
            for k in range(i, j + 1):
                ranks[indexed[k]] = avg_rank
            i = j + 1
        return ranks

    rx = rank(xs)
    ry = rank(ys)
    return pearson(rx, ry)[0]


def main():
    tsv = Path(__file__).parent / "conversation_drift_report.tsv"
    if not tsv.exists():
        print("Run analyze_all_conversations.py first.")
        sys.exit(1)

    with open(tsv) as f:
        reader = csv.DictReader(f, delimiter='\t')
        all_rows = list(reader)

    print(f"Total rows in TSV: {len(all_rows)}")
    print()

    # ── Filtering layers ──────────────────────────────────────────────
    # Layer 0: everything in TSV (already has total_cjk >= 10, n_messages >= 5)
    # Layer 1: exclude subagents only
    # Layer 2: exclude subagents + require meaningful Chinese (first_third_en < 95%)
    # Layer 3: the old "bilingual" filter (first_third_en < 85%, n >= 8)

    layers = [
        ("ALL (CJK≥10, msgs≥5)", all_rows),
        ("Non-subagent", [r for r in all_rows if r["is_subagent"] == "False"]),
        ("Non-sub + 1st-third EN<95%", [r for r in all_rows
            if r["is_subagent"] == "False" and float(r["first_third_en"]) < 0.95]),
        ("Non-sub + 1st-third EN<85% + msgs≥8 (old filter)", [r for r in all_rows
            if r["is_subagent"] == "False" and float(r["first_third_en"]) < 0.85
            and int(r["n_messages"]) >= 8]),
    ]

    print("=== Filter comparison ===")
    print(f"{'Filter':<45} {'N':<6} {'Avg Δ':<10} {'Med Δ':<10} {'%drift':<8} {'Pearson r':<10} {'Spearman ρ'}")
    print("-" * 100)
    for label, rows in layers:
        n = len(rows)
        if n < 3:
            print(f"{label:<45} {n:<6} (too few)")
            continue
        deltas = [float(r["half_delta_pp"]) for r in rows]
        msgs = [int(r["n_messages"]) for r in rows]
        avg_d = sum(deltas) / n
        med_d = sorted(deltas)[n // 2]
        pct = sum(1 for d in deltas if d > 2) / n
        pr, _, _ = pearson(msgs, deltas)
        sr = spearman(msgs, deltas)
        print(f"{label:<45} {n:<6} {avg_d:<+10.1f} {med_d:<+10.1f} {pct:<8.0%} {pr:<+10.3f} {sr:<+10.3f}")
    print()

    # ── Use relaxed filter for remaining analysis ─────────────────────
    # Non-subagent, first_third_en < 95% (allow more mixed conversations)
    data = [r for r in all_rows
            if r["is_subagent"] == "False" and float(r["first_third_en"]) < 0.95]

    n = len(data)
    deltas = [float(r["half_delta_pp"]) for r in data]
    msgs = [int(r["n_messages"]) for r in data]
    slopes = [float(r["slope_pp_per_msg"]) for r in data]

    print(f"Working set: {n} conversations (non-subagent, 1st-third EN% < 95%)")
    print()

    # ── Correlation: half_delta vs n_messages ─────────────────────────
    pr, a, b = pearson(msgs, deltas)
    sr = spearman(msgs, deltas)
    print("=== Correlation: half_delta_pp vs n_messages ===")
    print(f"  Pearson r  = {pr:+.3f}")
    print(f"  Spearman ρ = {sr:+.3f}")
    print(f"  Regression: Δ(pp) = {a:+.2f} + {b:+.4f} × n_messages")
    print(f"  → 10 msgs: predicted Δ = {a + b * 10:+.1f} pp")
    print(f"  → 50 msgs: predicted Δ = {a + b * 50:+.1f} pp")
    print(f"  → 100 msgs: predicted Δ = {a + b * 100:+.1f} pp")
    print(f"  → 200 msgs: predicted Δ = {a + b * 200:+.1f} pp")
    print()

    # ── Also correlate slope (pp/msg) vs n_messages ───────────────────
    pr2, a2, b2 = pearson(msgs, slopes)
    sr2 = spearman(msgs, slopes)
    print("=== Correlation: slope (pp/msg) vs n_messages ===")
    print(f"  Pearson r  = {pr2:+.3f}")
    print(f"  Spearman ρ = {sr2:+.3f}")
    print(f"  (negative = longer convs have smaller per-message slope, which is expected")
    print(f"   if drift is sub-linear; positive = acceleration)")
    print()

    # ── Fine-grained buckets ──────────────────────────────────────────
    buckets = [
        ("5-9", 5, 10),
        ("10-14", 10, 15),
        ("15-19", 15, 20),
        ("20-29", 20, 30),
        ("30-49", 30, 50),
        ("50-79", 50, 80),
        ("80-119", 80, 120),
        ("120-199", 120, 200),
        ("200-499", 200, 500),
        ("500+", 500, 99999),
    ]
    print("=== Drift by conversation length (fine-grained) ===")
    print(f"  {'Range':<12} {'N':<5} {'Avg Δ(pp)':<12} {'Med Δ(pp)':<12} {'%drift':<8} {'Avg slope':<12} {'Visual'}")
    print("  " + "-" * 85)
    for label, lo, hi in buckets:
        bk = [(deltas[i], msgs[i], slopes[i]) for i in range(n) if lo <= msgs[i] < hi]
        if not bk:
            continue
        bn = len(bk)
        ds = [x[0] for x in bk]
        ss = [x[2] for x in bk]
        avg_d = sum(ds) / bn
        med_d = sorted(ds)[bn // 2]
        pct = sum(1 for d in ds if d > 2) / bn
        avg_s = sum(ss) / bn
        # Visual bar: center at 0
        bar_val = avg_d
        bar_len = min(30, max(-30, int(bar_val)))
        if bar_len >= 0:
            bar = " " * 15 + "|" + "▓" * bar_len
        else:
            bar = " " * (15 + bar_len) + "░" * (-bar_len) + "|"
        print(f"  {label:<12} {bn:<5} {avg_d:<+12.1f} {med_d:<+12.1f} {pct:<8.0%} {avg_s:<+12.3f} {bar}")
    print()

    # ── Scatter: n_messages vs half_delta ─────────────────────────────
    # ASCII scatter plot
    print("=== Scatter: n_messages (x) vs half_delta_pp (y) ===")
    max_x = min(500, max(msgs))
    min_y, max_y = -30, 40

    WIDTH = 70
    HEIGHT = 25

    grid = [[" "] * WIDTH for _ in range(HEIGHT)]

    # Axes
    y_zero_row = int((max_y / (max_y - min_y)) * (HEIGHT - 1))
    y_zero_row = max(0, min(HEIGHT - 1, y_zero_row))
    for c in range(WIDTH):
        grid[y_zero_row][c] = "-"

    for i in range(n):
        x = msgs[i]
        y = deltas[i]
        if x > max_x or y < min_y or y > max_y:
            continue
        col = int(x / max_x * (WIDTH - 1))
        row = int((max_y - y) / (max_y - min_y) * (HEIGHT - 1))
        col = max(0, min(WIDTH - 1, col))
        row = max(0, min(HEIGHT - 1, row))
        if grid[row][col] == " " or grid[row][col] == "-":
            grid[row][col] = "·"
        elif grid[row][col] == "·":
            grid[row][col] = "o"
        elif grid[row][col] == "o":
            grid[row][col] = "O"
        else:
            grid[row][col] = "#"

    # Regression line
    for x_val in range(0, max_x + 1, max(1, max_x // WIDTH)):
        y_val = a + b * x_val
        if y_val < min_y or y_val > max_y:
            continue
        col = int(x_val / max_x * (WIDTH - 1))
        row = int((max_y - y_val) / (max_y - min_y) * (HEIGHT - 1))
        col = max(0, min(WIDTH - 1, col))
        row = max(0, min(HEIGHT - 1, row))
        if grid[row][col] == " " or grid[row][col] == "-":
            grid[row][col] = "/"

    # Y axis labels
    for row_idx in range(HEIGHT):
        y_val = max_y - (row_idx / (HEIGHT - 1)) * (max_y - min_y)
        if row_idx % 5 == 0:
            label = f"{y_val:+5.0f}|"
        else:
            label = "     |"
        print(f"  {label}{''.join(grid[row_idx])}")

    # X axis
    print(f"      +{''.join(['-'] * WIDTH)}")
    x_labels = "      0"
    for frac in [0.25, 0.5, 0.75, 1.0]:
        pos = int(frac * WIDTH)
        val = int(frac * max_x)
        x_labels += " " * (pos - len(x_labels) + 6) + str(val)
    print(x_labels)
    print(f"      {'n_messages →':^{WIDTH}}")
    print(f"      Regression: Δ = {a:+.2f} + {b:+.4f} × msgs  (r={pr:+.3f})")
    print()

    # ── Cumulative: what % of conversations drift at each length ──────
    print("=== Cumulative drift rate by minimum length ===")
    print(f"  {'Min msgs':<12} {'N':<6} {'%drift':<8} {'Avg Δ'}")
    for threshold in [5, 10, 15, 20, 30, 50, 80, 100, 150, 200]:
        subset = [(deltas[i], msgs[i]) for i in range(n) if msgs[i] >= threshold]
        if not subset:
            continue
        sn = len(subset)
        sd = [x[0] for x in subset]
        avg = sum(sd) / sn
        pct = sum(1 for d in sd if d > 2) / sn
        print(f"  ≥{threshold:<10} {sn:<6} {pct:<8.0%} {avg:+.1f} pp")


if __name__ == "__main__":
    main()
