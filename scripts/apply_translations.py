"""
跨平台 apply_translations: audit + repack strict.

替代旧 apply_translations.sh, 可在 Windows 上直接用.

用法:
  python3 apply_translations.py <input> <output> [--translations DIR] [--report-dir DIR]
"""
from __future__ import annotations

import argparse
import datetime
import json
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from log_setup import setup_logging  # noqa: E402

log = setup_logging(__name__)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", help="原 binary 路径")
    ap.add_argument("output", help="patched binary 输出路径")
    ap.add_argument("--translations",
                    default=str(SCRIPT_DIR.parent / "data" / "translations"),
                    help="translations 目录 或 单文件 .json")
    ap.add_argument("--report-dir",
                    default=str(Path.home() / ".cache" / "cc-l10n" / "reports"),
                    help="报告输出目录")
    args = ap.parse_args()

    report_dir = Path(args.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    audit_report = report_dir / f"audit-{ts}.json"
    repack_report = report_dir / f"repack-{ts}.json"

    log.info("====================================")
    log.info("Step 1/2: Pre-flight audit")
    log.info("====================================")
    r = subprocess.run([
        sys.executable, str(SCRIPT_DIR / "audit_replacements.py"),
        args.input, args.translations,
        "--json", str(audit_report), "--strict",
    ])
    if r.returncode != 0:
        log.error(f"\n[FAIL] audit 检测到高危, 拒绝 patch. 报告: {audit_report}")
        sys.exit(1)

    log.info("")
    log.info("====================================")
    log.info("Step 2/3: Apply translations + repack (strict skip=0; miss 跨版本/平台属预期)")
    log.info("====================================")
    r = subprocess.run([
        sys.executable, str(SCRIPT_DIR / "repack_universal.py"),
        args.input, args.output, args.translations,
        "--strict", "--report", str(repack_report),
    ])
    if r.returncode != 0:
        log.error(f"\n[FAIL] repack 失败 (--strict skip=0 / placeholder). 报告: {repack_report}")
        sys.exit(2)

    log.info("")
    log.info("====================================")
    log.info("Step 3/3: Verify 覆盖率 (untranslated_prompt=0, 平台自动判)")
    log.info("====================================")
    r = subprocess.run([
        sys.executable, str(SCRIPT_DIR / "verify_coverage.py"),
        "--binary", args.output,
        "--translations", args.translations,
        "--whitelist", str(SCRIPT_DIR.parent / "data" / "whitelist.json"),
    ])
    if r.returncode != 0:
        log.error("\n[FAIL] verify 失败 (patched binary 仍有 untranslated_prompt)")
        sys.exit(3)

    log.info("")
    log.info("====================================")
    log.info("[OK] 完成")
    log.info("====================================")
    log.info(f"  输出 binary : {args.output}")
    log.info(f"  audit 报告  : {audit_report}")
    log.info(f"  repack 报告 : {repack_report}")


if __name__ == "__main__":
    main()
