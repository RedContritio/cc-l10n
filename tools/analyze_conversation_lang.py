#!/usr/bin/env python3
"""单对话语言构成分析 (中文 vs 英文): 字符比例 + 趋势.

合并自早期的 quintile 版与 decile/回归 版, 给出单对话内随消息推进的英文占比
(EN%) 多尺度视图: 五分位 / 十分位 / 三等分 / 前后半 / 滑窗 / 线性回归, 并对
assistant 与 (人工撰写的) user 两条轨迹做对照.

EN% = ASCII 字母 / (ASCII 字母 + CJK 汉字), 剥离代码/标记/URL/路径后统计.
注意: EN% 是初代指标, 会混淆"语言选择"与"技术术语密度" —— 一句正常中文技术
交流也可能算出高 EN%. 结构化、免疫术语密度的 word-run 指标见
analyze_drift_runs.py; 本脚本仅用于单对话快速概览.

用法
  python3 tools/analyze_conversation_lang.py <conversation.jsonl>
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


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
    """从 message 的 content (str 或 block 列表) 抽取 text 块拼接."""
    texts = []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                texts.append(block.get("text", ""))
    elif isinstance(content, str):
        texts.append(content)
    return "\n".join(texts)


def strip_code_and_markup(text):
    """剥离 fenced code / inline code / XML 标签 / URL / 文件路径."""
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


def linear_regression(xs, ys):
    """简单 OLS: y = a + b*x. 返回 (a, b, r_squared)."""
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


def load_messages(path, msg_type):
    """按对话顺序加载某角色 (assistant / user) 的文本消息."""
    messages = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("type") != msg_type:
                continue
            msg = obj.get("message", {})
            if msg.get("role") != msg_type:
                continue
            text = extract_text_blocks(msg.get("content", []))
            if text.strip():
                messages.append(text)
    return messages


def filter_human_user(user_msgs):
    """排除系统生成的 user 消息 (task-notification / 命令注入), 只留人工撰写."""
    out = []
    for msg in user_msgs:
        if '<task-notification>' in msg or '<local-command' in msg or '<command-name>' in msg:
            continue
        out.append(msg)
    return out


def per_message_stats(messages):
    """每条消息 -> (cjk, asc, total, en_ratio), 文本已剥离代码/标记."""
    per_msg = []
    for msg in messages:
        cleaned = strip_code_and_markup(msg)
        cjk, asc = count_chars(cleaned)
        total = cjk + asc
        en_ratio = asc / total if total > 0 else 0
        per_msg.append((cjk, asc, total, en_ratio))
    return per_msg


def _segment_table(per_msg, n, k, title, prefix):
    """把对话按 k 等分 (quintile=5 / decile=10) 统计 EN%; 返回各段 EN% 列表."""
    seg_size = n // k
    print(f"\n--- {title} ---")
    print(f"{'Segment':<18} {'Msgs':<6} {'CJK':<8} {'ASCII':<8} {'EN%':<8} {'Visual'}")
    print("-" * 74)
    seg_en = []
    for i in range(k):
        start = i * seg_size
        end = (i + 1) * seg_size if i < k - 1 else n
        s_cjk = sum(per_msg[j][0] for j in range(start, end))
        s_asc = sum(per_msg[j][1] for j in range(start, end))
        s_total = s_cjk + s_asc
        en_r = s_asc / s_total if s_total > 0 else 0
        seg_en.append(en_r)
        bar_en = "#" * int(en_r * 40)
        bar_zh = "." * (40 - int(en_r * 40))
        print(f"{prefix}{i+1:02d} [{start+1:>4}-{end:>4}] {end-start:<6} {s_cjk:<8} {s_asc:<8} {en_r:<8.1%} |{bar_en}{bar_zh}|")
    return seg_en


def _ratio_range(per_msg, lo, hi):
    cjk = sum(per_msg[j][0] for j in range(lo, hi))
    asc = sum(per_msg[j][1] for j in range(lo, hi))
    t = cjk + asc
    return asc / t if t > 0 else 0


def analyze(messages, label):
    n = len(messages)
    if not n:
        print(f"\n=== {label}: 无文本消息 ===")
        return
    print(f"\n{'=' * 74}")
    print(f"=== {label} — {n} 条文本消息 ===")
    print(f"{'=' * 74}")

    per_msg = per_message_stats(messages)

    # 五分位
    if n >= 5:
        _segment_table(per_msg, n, 5, "五分位 (quintile)", "Q")

    # 十分位 + 逐消息线性回归 + 首/末十分位
    if n >= 10:
        decile_en = _segment_table(per_msg, n, 10, "十分位 (decile)", "D")
        xs = list(range(n))
        ys = [pm[3] for pm in per_msg]
        a, b, r2 = linear_regression(xs, ys)
        print(f"\n线性趋势 (逐消息):  EN% = {a:.3f} + {b:.5f} * msg_index")
        print(f"  斜率 {b*100:+.3f} pp/消息   全程 {n} 条预测漂移 {b*n*100:+.1f} pp   R² = {r2:.3f}")
        print(f"  首十分位 D01 = {decile_en[0]:.1%}   末十分位 D10 = {decile_en[-1]:.1%}   "
              f"Δ = {decile_en[-1] - decile_en[0]:+.1%}")

    # 三等分 (文档"前/中/后 1/3"用此) + 前后半
    t1, t2 = n // 3, 2 * n // 3
    print(f"\n三等分 EN%:  前 1/3 = {_ratio_range(per_msg, 0, t1):.1%}   "
          f"中 1/3 = {_ratio_range(per_msg, t1, t2):.1%}   "
          f"后 1/3 = {_ratio_range(per_msg, t2, n):.1%}")
    mid = n // 2
    r1 = _ratio_range(per_msg, 0, mid)
    r2h = _ratio_range(per_msg, mid, n)
    print(f"前后半 EN%:  前半 = {r1:.1%}   后半 = {r2h:.1%}   Δ = {r2h - r1:+.1%}")

    # 滑窗
    window = max(3, n // 10)
    step = max(1, window // 2)
    print(f"\n--- 滑窗 (size={window}, step={step}) ---")
    for start in range(0, n - window + 1, step):
        end = start + window
        w_cjk = sum(per_msg[j][0] for j in range(start, end))
        w_asc = sum(per_msg[j][1] for j in range(start, end))
        w_total = w_cjk + w_asc
        en_r = w_asc / w_total if w_total > 0 else 0
        center = (start + end) // 2 + 1
        bar = "#" * int(en_r * 50)
        print(f"  msg {center:<6} {en_r:<8.1%} |{bar}")

    # 逐消息明细 (首 5 / 末 5)
    print(f"\n--- 逐消息明细 (首 5 / 末 5) ---")
    print(f"{'#':<6} {'CJK':<7} {'ASCII':<7} {'EN%':<7} {'Preview'}")
    show = sorted(set(list(range(min(5, n))) + list(range(max(5, n - 5), n))))
    prev = -1
    for i in show:
        if i > prev + 1 and prev >= 0:
            print("  ...")
        s = per_msg[i]
        preview = messages[i].replace('\n', ' ')[:70]
        print(f"{i+1:<6} {s[0]:<7} {s[1]:<7} {s[3]:<7.1%} {preview}")
        prev = i


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("conversation", help="对话 JSONL 路径")
    args = ap.parse_args()

    path = Path(args.conversation)
    if not path.exists():
        ap.error(f"文件不存在: {path}")

    assistant_msgs = load_messages(path, "assistant")
    user_msgs = load_messages(path, "user")
    human_user = filter_human_user(user_msgs)
    print(f"已加载: {len(assistant_msgs)} assistant, {len(user_msgs)} user "
          f"({len(human_user)} 人工撰写)")

    analyze(assistant_msgs, "ASSISTANT (全部文本)")
    if len(human_user) >= 5:
        analyze(human_user, "USER (仅人工撰写)")


if __name__ == "__main__":
    main()
