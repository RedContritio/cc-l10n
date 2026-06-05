#!/usr/bin/env python3
"""
Global language-drift analysis across all Claude Code conversations.

For each conversation JSONL:
- Extract assistant text messages
- Compute per-message EN% (ASCII letters / (ASCII letters + CJK chars))
- Fit linear regression to measure drift speed
- Report per-conversation and aggregate statistics

Excludes conversations from cc-zh (the current project).
"""

import json
import os
import re
import sys
from pathlib import Path

from analyze_common import extract_project_name


# ── char classification ──────────────────────────────────────────────

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


# ── text extraction & cleaning ───────────────────────────────────────

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


# ── stats ─────────────────────────────────────────────────────────────

def linear_regression(xs, ys):
    n = len(xs)
    if n < 2:
        return 0, 0, 0
    sx = sum(xs)
    sy = sum(ys)
    sxx = sum(x * x for x in xs)
    sxy = sum(x * y for x, y in zip(xs, ys))
    denom = n * sxx - sx * sx
    if denom == 0:
        return sy / n, 0, 0
    b = (n * sxy - sx * sy) / denom
    a = (sy - b * sx) / n
    y_mean = sy / n
    ss_tot = sum((y - y_mean) ** 2 for y in ys)
    ss_res = sum((y - (a + b * x)) ** 2 for x, y in zip(xs, ys))
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0
    return a, b, r2


# ── per-conversation analysis ─────────────────────────────────────────

def analyze_one(path):
    """Analyze a single conversation JSONL. Returns dict or None if too few messages."""
    messages = []
    first_ts = None
    last_ts = None
    has_compaction = False

    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue

            t = obj.get("type", "")
            if t in ("summary", "context-compaction"):
                has_compaction = True

            if t != "assistant":
                continue
            msg = obj.get("message", {})
            if msg.get("role") != "assistant":
                continue

            ts = obj.get("timestamp", "")
            if ts:
                if first_ts is None:
                    first_ts = ts
                last_ts = ts

            content = msg.get("content", [])
            text = extract_text_blocks(content)
            if not text.strip():
                continue

            cleaned = strip_code_and_markup(text)
            cjk, asc = count_chars(cleaned)
            total = cjk + asc
            if total < 5:
                continue
            en_ratio = asc / total
            messages.append((cjk, asc, total, en_ratio))

    n = len(messages)
    if n < 5:
        return None

    # Check if conversation has meaningful Chinese content
    total_cjk = sum(m[0] for m in messages)
    total_asc = sum(m[1] for m in messages)
    total_all = total_cjk + total_asc
    if total_all == 0:
        return None
    overall_en = total_asc / total_all

    # Skip pure-English conversations (no Chinese at all)
    if total_cjk < 10:
        return None

    # Linear regression
    xs = list(range(n))
    ys = [m[3] for m in messages]
    intercept, slope, r2 = linear_regression(xs, ys)

    # Halves
    mid = n // 2
    h1_cjk = sum(messages[j][0] for j in range(mid))
    h1_asc = sum(messages[j][1] for j in range(mid))
    h2_cjk = sum(messages[j][0] for j in range(mid, n))
    h2_asc = sum(messages[j][1] for j in range(mid, n))
    t1 = h1_cjk + h1_asc
    t2 = h2_cjk + h2_asc
    h1_en = h1_asc / t1 if t1 > 0 else 0
    h2_en = h2_asc / t2 if t2 > 0 else 0

    # Thirds
    t1_end = n // 3
    t2_end = 2 * n // 3
    thirds_en = []
    for s, e in [(0, t1_end), (t1_end, t2_end), (t2_end, n)]:
        tc = sum(messages[j][0] for j in range(s, e))
        ta = sum(messages[j][1] for j in range(s, e))
        tt = tc + ta
        thirds_en.append(ta / tt if tt > 0 else 0)

    # Extract project name from path
    project = extract_project_name(path)

    session_id = Path(path).stem

    return {
        "path": str(path),
        "project": project,
        "session_id": session_id,
        "n_messages": n,
        "total_cjk": total_cjk,
        "total_asc": total_asc,
        "overall_en": overall_en,
        "slope_pp_per_msg": slope * 100,  # percentage points per message
        "intercept": intercept,
        "r2": r2,
        "predicted_drift_pp": slope * n * 100,
        "h1_en": h1_en,
        "h2_en": h2_en,
        "half_delta_pp": (h2_en - h1_en) * 100,
        "thirds_en": thirds_en,
        "has_compaction": has_compaction,
        "first_ts": first_ts or "",
        "last_ts": last_ts or "",
    }


# ── main ──────────────────────────────────────────────────────────────

def main():
    base = Path(os.path.expanduser("~/.claude/projects"))
    if not base.exists():
        print(f"Directory not found: {base}")
        sys.exit(1)

    # Find all JSONL files, excluding cc-zh
    all_jsonl = sorted(base.rglob("*.jsonl"))
    filtered = [p for p in all_jsonl if "cc-zh" not in str(p) and "cc_zh" not in str(p)]
    print(f"Found {len(all_jsonl)} total JSONL files, {len(filtered)} after excluding cc-zh")

    results = []
    skipped = 0
    errors = 0
    pure_en = 0

    for i, path in enumerate(filtered):
        if (i + 1) % 100 == 0:
            print(f"  processing {i+1}/{len(filtered)}...", file=sys.stderr)
        try:
            r = analyze_one(path)
            if r is None:
                skipped += 1
            else:
                results.append(r)
        except Exception as e:
            errors += 1

    print(f"\nAnalyzed: {len(results)} conversations with Chinese content")
    print(f"Skipped: {skipped} (too few messages or no Chinese)")
    print(f"Errors: {errors}")
    print()

    if not results:
        print("No conversations with Chinese content found.")
        return

    # ── Sort by drift severity ────────────────────────────────────────
    results.sort(key=lambda r: -r["half_delta_pp"])

    # ── Global summary ────────────────────────────────────────────────
    print("=" * 90)
    print("GLOBAL SUMMARY")
    print("=" * 90)

    n_conv = len(results)
    avg_slope = sum(r["slope_pp_per_msg"] for r in results) / n_conv
    avg_half_delta = sum(r["half_delta_pp"] for r in results) / n_conv
    median_half_delta = sorted(r["half_delta_pp"] for r in results)[n_conv // 2]
    drifting = sum(1 for r in results if r["half_delta_pp"] > 2)
    stable = sum(1 for r in results if abs(r["half_delta_pp"]) <= 2)
    reverse = sum(1 for r in results if r["half_delta_pp"] < -2)
    compacted = sum(1 for r in results if r["has_compaction"])

    avg_msgs = sum(r["n_messages"] for r in results) / n_conv
    long_convs = sum(1 for r in results if r["n_messages"] >= 20)

    print(f"Conversations analyzed:      {n_conv}")
    print(f"Average messages/conv:       {avg_msgs:.0f}")
    print(f"Long conversations (≥20):    {long_convs}")
    print(f"With context compaction:     {compacted}")
    print()
    print(f"Average half-delta:          {avg_half_delta:+.1f} pp")
    print(f"Median half-delta:           {median_half_delta:+.1f} pp")
    print(f"Average slope:               {avg_slope:+.3f} pp/msg")
    print()
    print(f"Drifting (Δ > +2pp):         {drifting} ({drifting/n_conv:.0%})")
    print(f"Stable (|Δ| ≤ 2pp):          {stable} ({stable/n_conv:.0%})")
    print(f"Reverse (Δ < -2pp):          {reverse} ({reverse/n_conv:.0%})")
    print()

    # ── Distribution of half-delta ────────────────────────────────────
    print("--- Half-delta distribution ---")
    buckets = [
        ("<-10pp", lambda d: d < -10),
        ("-10 to -5pp", lambda d: -10 <= d < -5),
        ("-5 to -2pp", lambda d: -5 <= d < -2),
        ("-2 to +2pp", lambda d: -2 <= d <= 2),
        ("+2 to +5pp", lambda d: 2 < d <= 5),
        ("+5 to +10pp", lambda d: 5 < d <= 10),
        (">+10pp", lambda d: d > 10),
    ]
    for label, pred in buckets:
        count = sum(1 for r in results if pred(r["half_delta_pp"]))
        bar = "█" * (count * 2)
        print(f"  {label:<15} {count:>4}  {bar}")
    print()

    # ── By conversation length ────────────────────────────────────────
    print("--- Drift by conversation length ---")
    length_buckets = [
        ("5-9 msgs", 5, 10),
        ("10-19 msgs", 10, 20),
        ("20-49 msgs", 20, 50),
        ("50-99 msgs", 50, 100),
        ("100+ msgs", 100, 99999),
    ]
    print(f"  {'Length':<14} {'Count':<7} {'Avg Δ(pp)':<12} {'Median Δ(pp)':<14} {'% drifting'}")
    for label, lo, hi in length_buckets:
        bucket = [r for r in results if lo <= r["n_messages"] < hi]
        if not bucket:
            continue
        bn = len(bucket)
        avg_d = sum(r["half_delta_pp"] for r in bucket) / bn
        med_d = sorted(r["half_delta_pp"] for r in bucket)[bn // 2]
        pct_drift = sum(1 for r in bucket if r["half_delta_pp"] > 2) / bn
        print(f"  {label:<14} {bn:<7} {avg_d:<+12.1f} {med_d:<+14.1f} {pct_drift:.0%}")
    print()

    # ── With vs without compaction ────────────────────────────────────
    with_comp = [r for r in results if r["has_compaction"]]
    without_comp = [r for r in results if not r["has_compaction"]]
    if with_comp and without_comp:
        print("--- Compaction effect ---")
        avg_wc = sum(r["half_delta_pp"] for r in with_comp) / len(with_comp)
        avg_nc = sum(r["half_delta_pp"] for r in without_comp) / len(without_comp)
        print(f"  With compaction:    avg Δ = {avg_wc:+.1f} pp  (n={len(with_comp)})")
        print(f"  Without compaction: avg Δ = {avg_nc:+.1f} pp  (n={len(without_comp)})")
        print()

    # ── By project ────────────────────────────────────────────────────
    projects = {}
    for r in results:
        p = r["project"]
        projects.setdefault(p, []).append(r)

    print("--- By project (≥3 conversations) ---")
    print(f"  {'Project':<40} {'Convs':<7} {'Avg Δ(pp)':<12} {'Avg msgs':<10} {'Avg EN%'}")
    proj_list = [(p, rs) for p, rs in projects.items() if len(rs) >= 3]
    proj_list.sort(key=lambda x: -sum(r["half_delta_pp"] for r in x[1]) / len(x[1]))
    for p, rs in proj_list:
        pn = len(rs)
        avg_d = sum(r["half_delta_pp"] for r in rs) / pn
        avg_m = sum(r["n_messages"] for r in rs) / pn
        avg_en = sum(r["overall_en"] for r in rs) / pn
        print(f"  {p:<40} {pn:<7} {avg_d:<+12.1f} {avg_m:<10.0f} {avg_en:.1%}")
    print()

    # ── Top 15 worst drifters ─────────────────────────────────────────
    print("--- Top 15 worst drifters (by half-delta) ---")
    print(f"  {'Project':<30} {'Session':<12} {'Msgs':<6} {'H1 EN%':<9} {'H2 EN%':<9} {'Δ(pp)':<9} {'Slope':<10} {'R²':<6} {'Comp'}")
    for r in results[:15]:
        sid = r["session_id"][:10]
        comp = "Y" if r["has_compaction"] else ""
        print(f"  {r['project']:<30} {sid:<12} {r['n_messages']:<6} {r['h1_en']:<9.1%} {r['h2_en']:<9.1%} {r['half_delta_pp']:<+9.1f} {r['slope_pp_per_msg']:<+10.3f} {r['r2']:<6.3f} {comp}")
    print()

    # ── Top 15 most stable (longest convs with low drift) ─────────────
    stable_long = sorted(
        [r for r in results if r["n_messages"] >= 15],
        key=lambda r: abs(r["half_delta_pp"])
    )
    if stable_long:
        print("--- Top 15 most stable (≥15 msgs, lowest |Δ|) ---")
        print(f"  {'Project':<30} {'Session':<12} {'Msgs':<6} {'H1 EN%':<9} {'H2 EN%':<9} {'Δ(pp)':<9} {'Slope':<10} {'Comp'}")
        for r in stable_long[:15]:
            sid = r["session_id"][:10]
            comp = "Y" if r["has_compaction"] else ""
            print(f"  {r['project']:<30} {sid:<12} {r['n_messages']:<6} {r['h1_en']:<9.1%} {r['h2_en']:<9.1%} {r['half_delta_pp']:<+9.1f} {r['slope_pp_per_msg']:<+10.3f} {comp}")
        print()

    # ── Aggregate thirds trend ────────────────────────────────────────
    # For conversations with ≥15 messages, compute average thirds EN%
    long_results = [r for r in results if r["n_messages"] >= 15]
    if long_results:
        avg_t1 = sum(r["thirds_en"][0] for r in long_results) / len(long_results)
        avg_t2 = sum(r["thirds_en"][1] for r in long_results) / len(long_results)
        avg_t3 = sum(r["thirds_en"][2] for r in long_results) / len(long_results)
        print(f"--- Aggregate thirds (conversations ≥15 msgs, n={len(long_results)}) ---")
        print(f"  First third:  avg EN% = {avg_t1:.1%}")
        print(f"  Middle third: avg EN% = {avg_t2:.1%}")
        print(f"  Last third:   avg EN% = {avg_t3:.1%}")
        print(f"  Trend: {avg_t1:.1%} → {avg_t2:.1%} → {avg_t3:.1%}  (Δ = {(avg_t3-avg_t1)*100:+.1f} pp)")
        print()

    # ── BILINGUAL FOCUS: exclude subagents & pure-English ────────────
    # "Bilingual" = main conversation (not agent-*) where first-third EN% < 85%
    bilingual = [
        r for r in results
        if not r["session_id"].startswith("agent-")
        and r["thirds_en"][0] < 0.85
        and r["n_messages"] >= 8
    ]
    if bilingual:
        bilingual.sort(key=lambda r: -r["half_delta_pp"])
        bn = len(bilingual)
        print("=" * 90)
        print(f"BILINGUAL FOCUS (main convs, first-third EN% < 85%, ≥8 msgs): {bn} conversations")
        print("=" * 90)

        avg_d = sum(r["half_delta_pp"] for r in bilingual) / bn
        med_d = sorted(r["half_delta_pp"] for r in bilingual)[bn // 2]
        avg_slope_b = sum(r["slope_pp_per_msg"] for r in bilingual) / bn
        drift_b = sum(1 for r in bilingual if r["half_delta_pp"] > 2)
        stable_b = sum(1 for r in bilingual if abs(r["half_delta_pp"]) <= 2)
        rev_b = sum(1 for r in bilingual if r["half_delta_pp"] < -2)
        avg_m = sum(r["n_messages"] for r in bilingual) / bn

        print(f"  Avg messages:    {avg_m:.0f}")
        print(f"  Avg half-delta:  {avg_d:+.1f} pp")
        print(f"  Median half-Δ:   {med_d:+.1f} pp")
        print(f"  Avg slope:       {avg_slope_b:+.3f} pp/msg")
        print(f"  Drifting (>+2):  {drift_b} ({drift_b/bn:.0%})")
        print(f"  Stable (±2):     {stable_b} ({stable_b/bn:.0%})")
        print(f"  Reverse (<-2):   {rev_b} ({rev_b/bn:.0%})")
        print()

        # Thirds
        avg_t1 = sum(r["thirds_en"][0] for r in bilingual) / bn
        avg_t2 = sum(r["thirds_en"][1] for r in bilingual) / bn
        avg_t3 = sum(r["thirds_en"][2] for r in bilingual) / bn
        print(f"  Aggregate thirds: {avg_t1:.1%} → {avg_t2:.1%} → {avg_t3:.1%}  (Δ = {(avg_t3-avg_t1)*100:+.1f} pp)")
        print()

        # By length
        print(f"  {'Length':<14} {'Count':<7} {'Avg Δ(pp)':<12} {'Med Δ(pp)':<12} {'% drift'}")
        for label, lo, hi in length_buckets:
            bk = [r for r in bilingual if lo <= r["n_messages"] < hi]
            if not bk:
                continue
            bkn = len(bk)
            ad = sum(r["half_delta_pp"] for r in bk) / bkn
            md = sorted(r["half_delta_pp"] for r in bk)[bkn // 2]
            pd = sum(1 for r in bk if r["half_delta_pp"] > 2) / bkn
            print(f"  {label:<14} {bkn:<7} {ad:<+12.1f} {md:<+12.1f} {pd:.0%}")
        print()

        # Top drifters in bilingual set
        print(f"  --- Top 10 bilingual drifters ---")
        print(f"  {'Project':<30} {'Session':<38} {'Msgs':<6} {'H1 EN%':<9} {'H2 EN%':<9} {'Δ(pp)':<9} {'1/3→2/3→3/3'}")
        for r in bilingual[:10]:
            t = r["thirds_en"]
            thirds_s = f"{t[0]:.0%}→{t[1]:.0%}→{t[2]:.0%}"
            print(f"  {r['project']:<30} {r['session_id']:<38} {r['n_messages']:<6} {r['h1_en']:<9.1%} {r['h2_en']:<9.1%} {r['half_delta_pp']:<+9.1f} {thirds_s}")
        print()

        # Most stable bilingual
        stable_bi = sorted(bilingual, key=lambda r: abs(r["half_delta_pp"]))
        print(f"  --- Top 10 most stable bilingual ---")
        print(f"  {'Project':<30} {'Session':<38} {'Msgs':<6} {'H1 EN%':<9} {'H2 EN%':<9} {'Δ(pp)':<9} {'1/3→2/3→3/3'}")
        for r in stable_bi[:10]:
            t = r["thirds_en"]
            thirds_s = f"{t[0]:.0%}→{t[1]:.0%}→{t[2]:.0%}"
            print(f"  {r['project']:<30} {r['session_id']:<38} {r['n_messages']:<6} {r['h1_en']:<9.1%} {r['h2_en']:<9.1%} {r['half_delta_pp']:<+9.1f} {thirds_s}")
        print()

    # ── SUBAGENT section ──────────────────────────────────────────────
    subagent = [r for r in results if r["session_id"].startswith("agent-")]
    main_conv = [r for r in results if not r["session_id"].startswith("agent-")]
    if subagent and main_conv:
        print("--- Subagent vs main conversation ---")
        sa_n = len(subagent)
        mc_n = len(main_conv)
        sa_avg_en = sum(r["overall_en"] for r in subagent) / sa_n
        mc_avg_en = sum(r["overall_en"] for r in main_conv) / mc_n
        sa_avg_d = sum(r["half_delta_pp"] for r in subagent) / sa_n
        mc_avg_d = sum(r["half_delta_pp"] for r in main_conv) / mc_n
        print(f"  Subagents:  n={sa_n:<5} avg EN%={sa_avg_en:.1%}  avg Δ={sa_avg_d:+.1f} pp")
        print(f"  Main convs: n={mc_n:<5} avg EN%={mc_avg_en:.1%}  avg Δ={mc_avg_d:+.1f} pp")
        print()

    # ── Dump full table as TSV for further analysis ───────────────────
    tsv_path = Path(__file__).parent / "conversation_drift_report.tsv"
    with open(tsv_path, "w") as f:
        headers = [
            "project", "session_id", "is_subagent", "n_messages",
            "total_cjk", "total_asc",
            "overall_en", "first_third_en", "h1_en", "h2_en", "half_delta_pp",
            "slope_pp_per_msg", "r2", "has_compaction", "first_ts", "last_ts"
        ]
        f.write("\t".join(headers) + "\n")
        for r in results:
            row = [
                r["project"], r["session_id"],
                str(r["session_id"].startswith("agent-")),
                str(r["n_messages"]),
                str(r["total_cjk"]), str(r["total_asc"]),
                f"{r['overall_en']:.4f}", f"{r['thirds_en'][0]:.4f}",
                f"{r['h1_en']:.4f}", f"{r['h2_en']:.4f}",
                f"{r['half_delta_pp']:.2f}",
                f"{r['slope_pp_per_msg']:.4f}", f"{r['r2']:.4f}",
                str(r["has_compaction"]), r["first_ts"], r["last_ts"]
            ]
            f.write("\t".join(row) + "\n")
    print(f"Full report saved to: {tsv_path}")


if __name__ == "__main__":
    main()
