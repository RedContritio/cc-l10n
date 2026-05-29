"""
合并多个 extract 报告的 untranslated_prompt 候选, 按目标分组数平均切, 输出 N 个
JSON 文件供并行翻译 subagent 使用.

用法:
  python3 prep_translation_groups.py <报告1.json> [<报告2.json> ...]
                                     [--groups N] [--out-dir DIR]
                                     [--exclude-sentinel]

输入: extract_prompt_segments.py --out 生成的 JSON 报告
输出: <out-dir>/group-<i>.json (i = 1..N), 每个含 candidates 数组,
      每条 entry 形如:
        {"src": "...", "context": "@offset score=N class=prompt evidence=...",
         "len": N, "from_versions": ["2.1.145", "2.1.154"]}
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from log_setup import setup_logging  # noqa: E402

log = setup_logging(__name__)

SENTINEL_RE = re.compile(r"\$\{__[A-Z0-9_]+__\}")


def collect_untranslated(report_paths: list[Path]) -> dict[str, dict]:
    """src → entry (含 from_versions 列表). 同 src 跨版本合并."""
    by_src: dict[str, dict] = {}
    for p in report_paths:
        rep = json.loads(p.read_text())
        ver = Path(rep.get("source", str(p))).parent.name  # e.g. "2.1.154"
        for c in rep["candidates"]:
            if c["class"] != "prompt":
                continue
            if c["translated"] or c["whitelisted"]:
                continue
            text = c["text"].strip()
            if not text:
                continue
            if text in by_src:
                by_src[text]["from_versions"].append(ver)
            else:
                by_src[text] = {
                    "src": text,
                    "len": c["len"],
                    "score": c["score"],
                    "class": c["class"],
                    "evidence": c.get("evidence", []),
                    "offset_example": c["offset"],
                    "from_versions": [ver],
                }
    return by_src


def load_entries_from_group_json(path: Path) -> dict[str, dict]:
    """从已 prep 的 group-X.json (array of entries) 重新读为 by_src dict, 供再分组."""
    arr = json.loads(path.read_text())
    return {e["src"]: e for e in arr}


def write_info_mode(entries: list[dict], out_dir: Path, n_groups: int) -> None:
    """default 模式: 写 group-{i}.json (full info, array of entries)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    groups: list[list[dict]] = [[] for _ in range(n_groups)]
    for i, e in enumerate(entries):
        groups[i % n_groups].append(e)
    for i, g in enumerate(groups, 1):
        out_path = out_dir / f"group-{i}.json"
        out_path.write_text(json.dumps(g, ensure_ascii=False, indent=2))
        ver_set: set[str] = set()
        for e in g:
            ver_set.update(e["from_versions"])
        log.info(f"  group-{i}: {len(g)} 条, 涉及版本 {sorted(ver_set)} → {out_path}")


def write_ready_mode(entries: list[dict], out_prefix: Path, n_groups: int) -> None:
    """ready 模式: 直接写 ready-to-translate 文件 (dict src -> "__TODO__"),
    cc-l10n loader 兼容. 翻译 subagent 只需 Edit 替换 __TODO__."""
    groups: list[list[dict]] = [[] for _ in range(n_groups)]
    for i, e in enumerate(entries):
        groups[i % n_groups].append(e)
    suffixes = "abcdefghij"[:n_groups] if n_groups <= 10 else [str(i + 1) for i in range(n_groups)]
    out_prefix.parent.mkdir(parents=True, exist_ok=True)
    for i, g in enumerate(groups):
        suffix = suffixes[i]
        out_path = out_prefix.with_name(out_prefix.name + suffix + ".json")
        doc: dict = {"_meta": {"todo_marker": "__TODO__",
                               "entries": len(g),
                               "note": "subagent 把每个 __TODO__ 替换为中文翻译"}}
        for e in g:
            doc[e["src"]] = "__TODO__"
        out_path.write_text(json.dumps(doc, ensure_ascii=False, indent=2))
        log.info(f"  group-{i+1} ({suffix}): {len(g)} 条 → {out_path}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("reports", nargs="*", type=Path,
                    help="extract 报告 JSON 路径 (一个或多个); 或用 --from-group")
    ap.add_argument("--from-group", type=Path, default=None,
                    help="从已 prep 的 group-X.json 再分组 (替代 reports)")
    ap.add_argument("--groups", type=int, default=4,
                    help="分组数 (默认 4)")
    ap.add_argument("--out-dir", type=Path, default=Path("/tmp/cc-l10n-groups"),
                    help="default 模式输出目录 (默认 /tmp/cc-l10n-groups)")
    ap.add_argument("--ready", action="store_true",
                    help="ready 模式: 直接写 data/translations 兼容文件 (dict src->__TODO__), "
                         "subagent 极简任务只需替换 __TODO__")
    ap.add_argument("--out-prefix", type=Path, default=None,
                    help="ready 模式输出文件名前缀 (e.g. data/translations/18_group_1), "
                         "自动加 a/b/c.json 后缀")
    ap.add_argument("--exclude-sentinel", action="store_true",
                    help="排除含 ${__SENTINEL__} 的候选 (留给 sentinel resolver 修)")
    args = ap.parse_args()

    if args.from_group:
        by_src = load_entries_from_group_json(args.from_group)
        log.info(f"从 {args.from_group} 读 {len(by_src)} 条")
    elif args.reports:
        by_src = collect_untranslated(args.reports)
        log.info(f"读 {len(args.reports)} 份报告, 合并去重 {len(by_src)} 条 untranslated_prompt")
    else:
        ap.error("必须提供 reports 或 --from-group")

    if args.exclude_sentinel:
        before = len(by_src)
        by_src = {k: v for k, v in by_src.items() if not SENTINEL_RE.search(k)}
        log.info(f"排除含 sentinel 的 {before - len(by_src)} 条, 剩 {len(by_src)} 条")

    entries = sorted(by_src.values(), key=lambda e: (-e["score"], -e["len"]))

    if args.ready:
        if not args.out_prefix:
            ap.error("--ready 模式必须指定 --out-prefix")
        log.info(f"ready 模式 → {args.out_prefix}<a/b/...>.json (subagent 替换 __TODO__):")
        write_ready_mode(entries, args.out_prefix, args.groups)
    else:
        log.info(f"分 {args.groups} 组 (default info 模式):")
        write_info_mode(entries, args.out_dir, args.groups)

    return 0


if __name__ == "__main__":
    sys.exit(main())
