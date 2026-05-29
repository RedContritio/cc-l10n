"""
跨平台 install: 定位 active CC binary, 备份, patch, 替换.

平台行为差异:
  macOS:  /Users/<user>/.local/share/claude/versions/<ver>  + ad-hoc codesign
                                                            + rm+cp (避免 amfi cache)
  Linux:  ~/.local/share/claude/versions/<ver>              + chmod 755
  Windows: %LOCALAPPDATA%\\AnthropicClaude\\... 或 npm global  + 无 codesign

active binary 通过 ~/.local/bin/claude symlink 或 PATH 中第一个 claude 命令定位.

用法:
  python3 install_universal.py [--binary <path>]

参数:
  --binary <path>   显式指定 active CC binary 路径 (跳过自动检测)
"""
from __future__ import annotations

import argparse
import hashlib
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))

from log_setup import setup_logging  # noqa: E402

log = setup_logging(__name__)


def md5_file(path: Path) -> str:
    h = hashlib.md5()
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def find_active_binary_unix() -> Path:
    """通过 ~/.local/bin/claude symlink 或 PATH 找 active binary."""
    candidates = [
        Path.home() / ".local" / "bin" / "claude",
    ]
    # Linux package install 可能在 /usr/local/bin/claude 或 /usr/bin/claude
    for c in [Path("/usr/local/bin/claude"), Path("/usr/bin/claude")]:
        if c.exists():
            candidates.append(c)
    # PATH 第一个 claude
    which = shutil.which("claude")
    if which:
        candidates.insert(0, Path(which))

    for c in candidates:
        if c.exists():
            target = c.resolve() if c.is_symlink() else c
            if target.exists() and target.is_file():
                return target
    raise FileNotFoundError(
        "找不到 active claude binary. 试试 --binary <path> 显式指定, 或检查是否安装 Claude Code."
    )


def find_active_binary_windows() -> Path:
    """Windows 上 Claude Code 常见位置."""
    candidates = []
    # npm global install
    if os.environ.get("APPDATA"):
        candidates.append(Path(os.environ["APPDATA"]) / "npm" / "claude.cmd")
        candidates.append(Path(os.environ["APPDATA"]) / "npm" / "node_modules" /
                          "@anthropic-ai" / "claude-code" / "cli.js")
    # LocalAppData install
    if os.environ.get("LOCALAPPDATA"):
        la = Path(os.environ["LOCALAPPDATA"])
        for sub in ["AnthropicClaude", "Anthropic", "claude-code"]:
            p = la / sub
            if p.exists():
                for f in p.rglob("claude.exe"):
                    candidates.append(f)
                for f in p.rglob("cli.js"):
                    candidates.append(f)
    # PATH 第一个
    which = shutil.which("claude") or shutil.which("claude.exe") or shutil.which("claude.cmd")
    if which:
        candidates.insert(0, Path(which))

    for c in candidates:
        if c.exists() and c.is_file():
            return c.resolve()
    raise FileNotFoundError(
        "Windows 上找不到 active claude binary. 试试 --binary <path> 显式指定."
    )


def find_active_binary() -> Path:
    if platform.system() == "Windows":
        return find_active_binary_windows()
    return find_active_binary_unix()


def install(binary: Path) -> None:
    log.info(f"active binary : {binary}")

    backup = binary.with_suffix(binary.suffix + ".orig" if binary.suffix else ".orig")
    zh = binary.with_suffix(binary.suffix + ".zh" if binary.suffix else ".zh")

    log.info(f"备份目标       : {backup}")
    log.info(f"中文版输出     : {zh}\n")

    # Step 1: 备份 (幂等)
    if not backup.exists():
        shutil.copy2(binary, backup)
        backup.chmod(0o444)
        log.info(f"[OK] Step 1: 原版备份到 {backup} (chmod 444 只读)")
    else:
        cur_md5 = md5_file(binary)
        bak_md5 = md5_file(backup)
        if cur_md5 == bak_md5:
            log.info(f"[OK] Step 1: 备份已存在且与当前一致 ({backup})")
        else:
            log.info(f"[OK] Step 1: 备份存在 ({backup}), 与当前不同 (re-install)")

    # Step 2: patch + sign
    log.info("\nStep 2: patch + sign")
    log.info("--------------------")
    apply_script = SCRIPT_DIR / "apply_translations.py"
    r = subprocess.run([
        sys.executable, str(apply_script),
        str(backup), str(zh),
    ])
    if r.returncode != 0:
        log.error("[FAIL] apply_translations 失败, 中断 install")
        sys.exit(r.returncode)

    # Step 3: 替换 active binary
    # 关键: 必须 rm+cp (不是 cp 覆盖). 否则 macOS launchd/amfi 缓存的 signature
    # trust 仍指向旧内容 → 新 binary 启动 SIGKILL. rm 删 inode → cp 创建新 inode.
    zh.chmod(0o755)
    if binary.is_symlink():
        # 不要碰 symlink, 操作其指向的目标
        binary = binary.resolve()
    if binary.exists():
        binary.unlink()
    shutil.copy2(zh, binary)
    binary.chmod(0o755)

    log.info(f"\n[OK] Step 3: {binary} 已替换为中文版 (新 inode)")
    log.info(f"\n===================================================")
    log.info(f"  完成. 在新终端跑 `claude` 即中文版.")
    log.info(f"\n  还原 (恢复原版):")
    log.info(f"    python3 {SCRIPT_DIR / 'restore_universal.py'}")
    log.info(f"\n  或手动:")
    log.info(f"    cp {backup} {binary}")
    log.info(f"===================================================")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--binary", default=None,
                    help="显式指定 active CC binary 路径")
    args = ap.parse_args()

    if args.binary:
        binary = Path(args.binary).resolve()
        if not binary.exists():
            log.error(f"[FAIL] {binary} 不存在")
            sys.exit(1)
    else:
        try:
            binary = find_active_binary()
        except FileNotFoundError as e:
            log.error(f"[FAIL] {e}")
            sys.exit(1)

    install(binary)


if __name__ == "__main__":
    main()
