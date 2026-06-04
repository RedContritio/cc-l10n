"""生成/更新 data/supported_versions.json: 对给定版本×平台下载原始 binary,
提取 cli.js, 算 sha256, 写入配置。幂等 (重跑覆盖给定版本; 未给定的版本保留)。

用法:
  python gen_supported_versions.py 2.1.152 2.1.161
  python gen_supported_versions.py 2.1.162 --platforms @anthropic-ai/claude-code-linux-x64
  python gen_supported_versions.py --remove 2.1.150        # 从配置删除某版本

平台默认 = integration_test.PLATFORM_PKGS (linux/win32/darwin)。
某 (版本×平台) 在 npm 上不存在 (404) → 跳过该格, 不写 (预期: 早期版本只发过 linux)。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from integration_test import PLATFORM_PKGS, fetch_binary, NotPublished  # noqa: E402
from log_setup import setup_logging  # noqa: E402
from supported_versions import (  # noqa: E402
    CONFIG_PATH, cli_js_sha256, platform_key, _version_key,
)

log = setup_logging("gen_supported")


def load_or_init(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {
        "_comment": ("受支持的 CC 版本及其原始 (pre-patch) cli.js sha256 (version×platform)。"
                     "CI 仅对这些版本强制 verify=0; repack/install 前校 orig hash 确认是已知 binary。"
                     "由 scripts/gen_supported_versions.py 生成。"),
        "versions": {},
    }


def write_sorted(cfg: dict, path: Path) -> None:
    """版本序写出 (稳定 diff)。"""
    cfg["versions"] = {
        v: dict(sorted(cfg["versions"][v].items()))
        for v in sorted(cfg["versions"].keys(), key=_version_key)
    }
    path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("versions", nargs="*", help="要录入的版本号 (如 2.1.152)")
    ap.add_argument("--platforms", default=None,
                    help="逗号分隔 platform_pkg; 默认 linux/win32/darwin 三平台")
    ap.add_argument("--remove", action="append", default=[],
                    help="从配置删除指定版本 (可多次)")
    ap.add_argument("--config", default=str(CONFIG_PATH))
    args = ap.parse_args()

    path = Path(args.config)
    cfg = load_or_init(path)

    for v in args.remove:
        if cfg["versions"].pop(v, None) is not None:
            log.info(f"[remove] {v}")

    platform_pkgs = (PLATFORM_PKGS if not args.platforms
                     else [p.strip() for p in args.platforms.split(",") if p.strip()])

    for v in args.versions:
        entry = cfg["versions"].setdefault(v, {})
        for pkg in platform_pkgs:
            pk = platform_key(pkg)
            try:
                binary = fetch_binary(v, pkg)
            except NotPublished:
                log.warning(f"[skip] {v}/{pk}: npm 上不存在 (404)")
                entry.pop(pk, None)
                continue
            sha = cli_js_sha256(binary)
            entry[pk] = sha
            log.info(f"[hash] {v}/{pk} = {sha}")
        if not entry:
            log.warning(f"[drop] {v}: 无任何平台命中, 不写入")
            cfg["versions"].pop(v, None)

    write_sorted(cfg, path)
    log.info(f"已写入 {path} (受支持版本: "
             f"{sorted(cfg['versions'].keys(), key=_version_key)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
