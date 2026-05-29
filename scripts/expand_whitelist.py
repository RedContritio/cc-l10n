"""
从 extract_prompt_segments 报告读 untranslated_prompt 候选, 批量合入 whitelist.literal_exact.

用例: CC 升 patch version 后, 跑 `cc-l10n extract` → 新出现的 untranslated prompt
即可用本脚本一次性入白 (对应 README 已知限制 #3).

用法:
  python3 expand_whitelist.py --from-extract <report.json> --in <whitelist.json> --out <new.json>

  --in   现有 whitelist.json (默认 data/whitelist.json)
  --out  输出新 whitelist.json (必填; 不允许默认覆盖输入, 避免误改)
  --from-extract  extract_prompt_segments 输出的 JSON 报告
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from log_setup import setup_logging  # noqa: E402

log = setup_logging(__name__)


def collect_untranslated_prompt_texts(extract_report_path: Path) -> set[str]:
    """从 extract 报告读 untranslated prompt 候选 text 集合 (strip 后)."""
    rep = json.loads(extract_report_path.read_text())
    out: set[str] = set()
    for c in rep["candidates"]:
        if c["class"] != "prompt":
            continue
        if c["translated"] or c["whitelisted"]:
            continue
        out.add(c["text"].strip())
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--in", dest="in_file",
                    default=str(SCRIPT_DIR.parent / "data" / "whitelist.json"),
                    help="输入 whitelist.json (默认 data/whitelist.json)")
    ap.add_argument("--from-extract", required=True,
                    help="extract_prompt_segments 输出 JSON 报告")
    ap.add_argument("--out", required=True,
                    help="输出新 whitelist.json (必填, 避免误覆盖输入)")
    args = ap.parse_args()

    in_path = Path(args.in_file)
    out_path = Path(args.out)
    wl_raw = json.loads(in_path.read_text())

    all_hits: set[str] = set()
    for s in wl_raw.get("literal_exact", []):
        if isinstance(s, str) and not s.startswith("_comment"):
            all_hits.add(s)
    log.info(f"现有 literal_exact 起始: {len(all_hits)}")

    log.info(f"读 extract 报告: {args.from_extract}")
    extras = collect_untranslated_prompt_texts(Path(args.from_extract))
    log.info(f"  untranslated prompt 候选: {len(extras)} "
             f"(其中 {sum(1 for x in extras if len(x) > 500)} 条 >500 字符)")
    all_hits.update(extras)

    log.info(f"\n合并去重后: {len(all_hits)} 条")

    literal_exact = sorted(all_hits, key=lambda s: (len(s), s))

    out = {
        "_comment": wl_raw.get("_comment", ""),
        "_rules": [
            "1. exact: 字面字符串 token, 段落减法 (case sensitive)",
            "2. regex: 正则模式 token (URL/数字/版本号等)",
            "3. literal_exact: 字面量整体完全相等 (str.strip() 后比较, case sensitive)",
            "   - 用 scripts/expand_whitelist.py --from-extract 从 extract 报告增量合入",
            "4. verify 段落 (split by \\n\\n): 含中文 OK; 全段 in literal_exact OK; "
            "否则减去 exact/regex 后无 3+ 字母英文词 OK; 否则 WARN",
        ],
        "exact": wl_raw.get("exact", []),
        "regex": wl_raw.get("regex", []),
        "literal_exact": literal_exact,
    }

    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n")
    log.info(f"\n写入 {out_path}")
    log.info(f"  literal_exact 条目: {len(literal_exact)}")
    if literal_exact:
        log.info(f"  最长 entry: {max(len(s) for s in literal_exact):,} 字符")
        log.info(f"  总字符数: {sum(len(s) for s in literal_exact):,}")


if __name__ == "__main__":
    main()
