"""
跨平台 restore: 从 .orig 备份还原 active CC binary.

用法:
  python3 restore_universal.py [--binary <path>]
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from install_universal import find_active_binary
from log_setup import setup_logging

log = setup_logging(__name__)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--binary", default=None,
                    help="显式指定 active CC binary 路径")
    args = ap.parse_args()

    if args.binary:
        binary = Path(args.binary).resolve()
    else:
        try:
            binary = find_active_binary()
        except FileNotFoundError as e:
            log.error(f"[FAIL] {e}")
            sys.exit(1)

    if binary.is_symlink():
        binary = binary.resolve()

    backup = binary.with_suffix(binary.suffix + ".orig" if binary.suffix else ".orig")
    if not backup.exists():
        log.error(f"[FAIL] 备份 {backup} 不存在, 无法还原")
        sys.exit(1)

    log.info(f"还原 {binary} ← {backup}")
    binary.unlink(missing_ok=True)
    shutil.copy2(backup, binary)
    binary.chmod(0o755)
    log.info(f"[OK] 已还原")


if __name__ == "__main__":
    main()
