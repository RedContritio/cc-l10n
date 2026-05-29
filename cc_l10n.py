#!/usr/bin/env python3
"""
cc-l10n: Claude Code system prompt 中文化 patch 工具链跨平台入口.

支持 macOS / Linux / Windows. 子命令 dispatch 到 scripts/.

用法:
  python3 cc_l10n.py <command> [args]

子命令:
  install                  自动定位 active CC binary, 备份原版并替换为中文版
  restore                  从备份还原原版 binary
  apply <input> <output>   对任意 binary 应用翻译 (audit + repack strict)
  audit <binary>           预审计翻译应用风险
  repack <input> <output>  单跑 repack (跳过 audit)
  extract <binary>         静态提取 prompt 字面量, 输出 JSON 报告
  diff <report.json>       对比 extract 输出, 看 untranslated/whitelisted 状态
  verify --binary <bin>    static strict 验证: untranslated_prompt 必须为 0
  verify --captured <jsonl> capture strict 验证: 实测 prompt 无英文残留
  coverage <binary>        扫 binary 英文残留 (legacy)
  help                     显示帮助

环境变量:
  CC_L10N_TRANS      覆盖默认 translations 目录 (默认: data/translations)
  CC_L10N_WHITELIST  覆盖默认 whitelist.json (默认: data/whitelist.json)
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SCRIPTS = SCRIPT_DIR / "scripts"
DATA = SCRIPT_DIR / "data"
DEFAULT_TRANS = os.environ.get("CC_L10N_TRANS", str(DATA / "translations"))
DEFAULT_WHITELIST = os.environ.get("CC_L10N_WHITELIST", str(DATA / "whitelist.json"))

sys.path.insert(0, str(SCRIPTS))
from log_setup import setup_logging  # noqa: E402

log = setup_logging("cc_l10n")

USAGE = __doc__


def run_py(script: str, *args: str, extra_env: dict | None = None) -> int:
    """运行 scripts/<script>, 透传退出码."""
    cmd = [sys.executable, str(SCRIPTS / script)] + list(args)
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    r = subprocess.run(cmd, env=env)
    return r.returncode


def cmd_install(args: list[str]) -> int:
    return run_py("install_universal.py", *args)


def cmd_restore(args: list[str]) -> int:
    return run_py("restore_universal.py", *args)


def cmd_apply(args: list[str]) -> int:
    if len(args) < 2:
        log.error("Usage: cc-l10n apply <input-binary> <output-binary> "
                  "[--report-dir PATH] [flags...]")
        return 1
    inp, out, rest = args[0], args[1], args[2:]
    return run_py("apply_translations.py",
                  inp, out,
                  "--translations", DEFAULT_TRANS,
                  *rest)


def cmd_audit(args: list[str]) -> int:
    if len(args) < 1:
        log.error("Usage: cc-l10n audit <binary> [extra args]")
        return 1
    return run_py("audit_replacements.py", args[0], DEFAULT_TRANS, *args[1:])


def cmd_repack(args: list[str]) -> int:
    if len(args) < 2:
        log.error("Usage: cc-l10n repack <input> <output> "
                  "[--translations PATH] [flags...]")
        return 1
    inp, out, rest = args[0], args[1], args[2:]
    translations = DEFAULT_TRANS
    if "--translations" in rest:
        i = rest.index("--translations")
        if i + 1 >= len(rest):
            log.error("--translations 缺少路径参数")
            return 1
        translations = rest[i + 1]
        rest = rest[:i] + rest[i + 2:]
    return run_py("repack_universal.py", inp, out, translations, *rest)


def cmd_extract(args: list[str]) -> int:
    if not args:
        log.error("Usage: cc-l10n extract <binary> [--out report.json] [--show N]")
        return 1
    return run_py("extract_prompt_segments.py",
                  args[0],
                  "--translations", DEFAULT_TRANS,
                  "--whitelist", DEFAULT_WHITELIST,
                  *args[1:])


def cmd_diff(args: list[str]) -> int:
    if not args:
        log.error("Usage: cc-l10n diff <extract-report.json>")
        return 1
    return run_py("diff_coverage.py", *args)


def cmd_verify(args: list[str]) -> int:
    return run_py("verify_coverage.py",
                  "--translations", DEFAULT_TRANS,
                  "--whitelist", DEFAULT_WHITELIST,
                  *args)


def cmd_coverage(args: list[str]) -> int:
    return run_py("legacy/coverage_report.py", *args)


def main() -> int:
    argv = sys.argv[1:]
    if not argv or argv[0] in ("help", "-h", "--help"):
        print(USAGE)
        return 0
    cmd, rest = argv[0], argv[1:]
    table = {
        "install": cmd_install,
        "restore": cmd_restore,
        "apply": cmd_apply,
        "audit": cmd_audit,
        "repack": cmd_repack,
        "extract": cmd_extract,
        "diff": cmd_diff,
        "verify": cmd_verify,
        "coverage": cmd_coverage,
    }
    if cmd not in table:
        log.error(f"未知命令: {cmd}\n")
        log.error(USAGE)
        return 1
    return table[cmd](rest)


if __name__ == "__main__":
    sys.exit(main())
