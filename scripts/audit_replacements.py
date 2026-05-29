"""
审计 translations.json 应用到 cli.js 时的风险.

风险类别:
  A: 同一 src 在多个不同字面量出现 (会被一起替换, 用户应确认是预期)
  B: src 出现在 JS 代码区 (非字符串字面量, 替换会破坏语法)
  C: 跨字面量但 placeholder consistency 不一致 (会破坏 template literal 结构)
  D: 两条 src 的 hit 字节范围重叠 (替换会冲突)

输出:
  - stderr 人类可读报告
  - --json 输出机器可读 JSON
  - --strict 时 (B>0 或 C>0 或 D>0) exit 1

CLI:
  python3 audit_replacements.py <binary> <translations.json> [--json out.json] [--strict]

也可作模块用: from audit_replacements import audit
"""
import argparse
import bisect
import json
import re
import sys
from collections import Counter
from pathlib import Path

from bun_format import extract_cli_js
from js_string_scanner import scan_string_literals
from log_setup import setup_logging
from repack_universal import resolve_sentinels, load_translations, expand_translations

log = setup_logging(__name__)

PLACEHOLDER_RE = re.compile(rb"\$\{[^{}]*(?:\{[^}]*\}[^{}]*)*\}")


def audit(input_path: Path, translations) -> dict:
    """
    审计 translations 应用到给定 binary 的 cli.js 时的所有替换风险.

    translations 接受:
      - dict[bytes, bytes]                 (legacy)
      - list[(src, dst, allow_reorder)]    (推荐)

    返回 dict: total_src, total_hits, risk_a/b/c/d, high_risk, by_src: [...]
    """
    if isinstance(translations, dict):
        translations = [(k, v, False) for k, v in translations.items()]

    cli = extract_cli_js(Path(input_path))

    # 与 repack 保持一致: 把 translations 中的 sentinel 实例化为 cli.js 实际变量名
    # (多实例展开 - 与 repack 完全等价)
    sentinel_map = resolve_sentinels(cli)
    if sentinel_map:
        translations = expand_translations(translations, sentinel_map)

    ranges = scan_string_literals(cli)
    lit_starts = [r[0] for r in ranges]
    lit_ends = [r[1] for r in ranges]

    def in_literal(i: int, end: int) -> bool:
        if not lit_starts:
            return False
        idx = bisect.bisect_right(lit_starts, i) - 1
        if idx < 0:
            return False
        return lit_starts[idx] <= i and end <= lit_ends[idx]

    def placeholder_safe(src: bytes, dst: bytes, allow_reorder: bool) -> bool:
        # 与 repack_universal 保持一致
        src_ph = PLACEHOLDER_RE.findall(src)
        if not src_ph:
            return False
        dst_ph = PLACEHOLDER_RE.findall(dst)
        if allow_reorder:
            return Counter(src_ph) == Counter(dst_ph)
        return src_ph == dst_ph

    by_src = []
    all_hits = []  # [(pos, src, src_index)]
    risk_a = 0
    risk_b = 0
    risk_c = 0
    risk_e = 0  # placeholder 不匹配 (语义反转风险)

    for src_idx, (src, dst, allow_reorder) in enumerate(translations):
        positions = []
        pos = 0
        while True:
            i = cli.find(src, pos)
            if i < 0:
                break
            positions.append(i)
            pos = i + 1
        entry = {
            "src_preview": src[:80].decode("utf-8", errors="replace"),
            "src_len": len(src),
            "allow_reorder": allow_reorder,
            "occurrences": len(positions),
            "in_literal_count": 0,
            "cross_literal_safe": 0,  # 跨字面量但 placeholder 匹配
            "code_region": 0,         # risk B
            "cross_literal_unsafe": 0, # risk C
        }
        if len(positions) >= 2:
            risk_a += 1
            entry["risk_a"] = True

        # 提前检测 placeholder 不匹配 (risk E: 语义反转风险, 永远高危)
        src_ph = PLACEHOLDER_RE.findall(src)
        if src_ph:
            dst_ph = PLACEHOLDER_RE.findall(dst)
            if allow_reorder:
                ph_match = Counter(src_ph) == Counter(dst_ph)
            else:
                ph_match = src_ph == dst_ph
            if not ph_match:
                entry["placeholder_violation"] = True
                entry["src_placeholders"] = [p.decode() for p in src_ph]
                entry["dst_placeholders"] = [p.decode() for p in dst_ph]
                risk_e += 1
                by_src.append(entry)
                continue

        for i in positions:
            end = i + len(src)
            if in_literal(i, end):
                entry["in_literal_count"] += 1
                all_hits.append((i, src, src_idx))
            elif placeholder_safe(src, dst, allow_reorder):
                entry["cross_literal_safe"] += 1
                all_hits.append((i, src, src_idx))
            else:
                if PLACEHOLDER_RE.search(src):
                    entry["cross_literal_unsafe"] += 1
                    risk_c += 1
                else:
                    entry["code_region"] += 1
        by_src.append(entry)

    # 区分真 risk_b vs scanner false negative:
    # - 真 risk_b: code_region > 0 且 in_literal == 0 (该 src 在 cli.js 中没有任何合法字面量内匹配)
    # - 误报: code_region > 0 且 in_literal > 0 (scanner 漏识别嵌套字面量, src 本身设计是合法的)
    for e in by_src:
        if e["code_region"] > 0:
            if e["in_literal_count"] == 0:
                e["true_risk_b"] = True
                risk_b += e["code_region"]
            else:
                e["scanner_fn"] = True  # scanner false negative, 不计入 high_risk

    # risk D: 检查 hits 之间重叠
    # 完全嵌套 (短 src 完全在长 src 范围内) 不算 risk D — repack 应用时跳过被覆盖的短 src,
    # 由长 src 的 dst 覆盖整段 (假设翻译时已含子段翻译).
    all_hits.sort(key=lambda h: h[0])
    risk_d = 0
    conflicts = []
    for k in range(len(all_hits)):
        a_pos, a_src, _ = all_hits[k]
        a_end = a_pos + len(a_src)
        for j in range(k + 1, len(all_hits)):
            b_pos, b_src, _ = all_hits[j]
            if b_pos >= a_end:
                break
            b_end = b_pos + len(b_src)
            if b_end <= a_end:
                continue
            risk_d += 1
            conflicts.append({
                "pos1": a_pos, "src1": a_src.decode("utf-8", errors="replace")[:80],
                "pos2": b_pos, "src2": b_src.decode("utf-8", errors="replace")[:80],
            })

    return {
        "total_src": len(translations),
        "total_hits": len(all_hits),
        "risk_a": risk_a,      # 多次出现 (可能预期)
        "risk_b": risk_b,      # 代码区匹配 (高危)
        "risk_c": risk_c,      # 跨字面量但占位符不一致 (高危)
        "risk_d": risk_d,      # hit 范围重叠 (高危)
        "risk_e": risk_e,      # placeholder 顺序/集合不匹配 (语义反转, 高危)
        "high_risk": risk_b + risk_c + risk_d + risk_e,
        "by_src": by_src,
        "conflicts": conflicts,
    }


def main():
    parser = argparse.ArgumentParser(description="审计 translations.json 应用到 CC binary 的风险")
    parser.add_argument("binary", help="CC binary 路径 (Mach-O 或 ELF)")
    parser.add_argument("translations", help="translations.json 路径")
    parser.add_argument("--json", dest="json_out", default=None,
                        help="JSON 报告输出路径")
    parser.add_argument("--strict", action="store_true",
                        help="发现高危 risk (B/C/D) 时 exit 1")
    args = parser.parse_args()

    trans = load_translations(args.translations)
    log.info(f"审计 {len(trans)} 条 translations "
             f"({sum(1 for _,_,ar in trans if ar)} 条 allow_reorder) "
             f"应用到 {args.binary}...")

    report = audit(Path(args.binary), trans)
    log.info(f"\n=== 风险摘要 ===")
    log.info(f"  total_src   : {report['total_src']}")
    log.info(f"  total_hits  : {report['total_hits']}")
    log.info(f"  risk A (多次出现, 可能预期)  : {report['risk_a']}")
    log.info(f"  risk B (代码区匹配, 高危)    : {report['risk_b']}")
    log.info(f"  risk C (占位符不一致, 高危)  : {report['risk_c']}")
    log.info(f"  risk D (hit 范围重叠, 高危)  : {report['risk_d']}")
    log.info(f"  risk E (placeholder 顺序/集合不匹配, 语义反转高危): {report['risk_e']}")
    log.info(f"  high_risk 合计              : {report['high_risk']}")

    if report["conflicts"]:
        log.info(f"\n=== 冲突详情 ===")
        for c in report["conflicts"][:10]:
            log.info(f"  src1@{c['pos1']} ({c['src1'][:50]!r})")
            log.info(f"  src2@{c['pos2']} ({c['src2'][:50]!r})")
            log.info("")

    risky = [e for e in report["by_src"] if e.get("code_region") or e.get("cross_literal_unsafe")]
    if risky:
        log.info(f"\n=== 高危条目 ===")
        for e in risky[:10]:
            log.info(f"  {e['src_preview'][:60]!r}: "
                     f"代码区={e['code_region']}, 占位符不一致={e['cross_literal_unsafe']}")

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(report, ensure_ascii=False, indent=2))
        log.info(f"\nJSON 报告: {args.json_out}")

    if args.strict and report["high_risk"] > 0:
        log.error(f"\n[FAIL] --strict 模式, 检测到 {report['high_risk']} 项高危, exit 1")
        sys.exit(1)


if __name__ == "__main__":
    main()
