"""
集成测试: 拉取多个 CC 版本逐个跑 cc-l10n apply + verify, 验 patch 流水线对 CC
版本演进的鲁棒性.

抽样策略 (默认):
  - 每个 minor 抽 latest patch (e.g. 0.2.126 / 1.0.128 / 2.0.77 / 2.1.156)
  - 当前 minor (latest minor 系列) 额外抽最近 5 个 patch

失败语义: 任一版本 cc-l10n apply 或 verify 失败 → exit 1.

用法:
  python3 integration_test.py --dist-tags              # 快速 CI: latest+stable × 三平台 (全要 verify=0)
  python3 integration_test.py                          # 抽样策略跑全部
  python3 integration_test.py --version 2.1.156        # 单版本 (显式点名 → verify=0)
  python3 integration_test.py --all                    # 全历史 (慢)
  python3 integration_test.py --list                   # 仅列抽样版本到 stdout
  python3 integration_test.py --list-json              # 抽样版本 JSON 数组 (CI matrix 用)

平台子包 (Bun standalone binary) 仅 2.1.110+ 才有; 更老版本是纯 JS npm 包 (无 binary)。
某 (版本×平台) 组合 npm 上不存在 → HTTP 404 视为 skip (不算 FAIL)。
聚合 no-stale 默认仅信息提示; --check-stale 才作失败门。

环境:
  需要 npm CLI / curl / tar / python3.10+
  CC binary 子包大约 100 MB/平台, 默认下到 ~/.cache/cc-l10n/integration/
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tarfile
import urllib.error
import urllib.request
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))

from log_setup import setup_logging  # noqa: E402

log = setup_logging("integration_test")


class NotPublished(Exception):
    """该 (版本×平台) 组合在 npm 上不存在 (HTTP 404). 早期版本只发过 linux,
    win32/darwin 子包是后来才有的 — 这种缺失是预期, 应 skip 不算 FAIL."""


NPM_PKG = "@anthropic-ai/claude-code"
PLATFORM_PKG = "@anthropic-ai/claude-code-linux-x64"  # 默认单平台 (--platform-pkg 覆盖)
# 多平台默认集 (linux/win/mac); --platforms 可改。OS 决定格式 (elf/pe/macho), arch 无关。
PLATFORM_PKGS = [
    "@anthropic-ai/claude-code-linux-x64",
    "@anthropic-ai/claude-code-win32-x64",
    "@anthropic-ai/claude-code-darwin-arm64",
]
CACHE_DIR = Path(os.environ.get("CC_L10N_INTEGRATION_CACHE",
                                str(Path.home() / ".cache" / "cc-l10n" / "integration")))
TRANS_DIR = PROJECT_ROOT / "data" / "translations"
WHITELIST = PROJECT_ROOT / "data" / "whitelist.json"


def npm_view_versions(pkg: str = NPM_PKG) -> list[str]:
    """从 npm registry 拉所有版本号."""
    r = subprocess.run(["npm", "view", pkg, "versions", "--json"],
                       capture_output=True, text=True, check=True)
    return json.loads(r.stdout)


def dist_tag_versions(pkg: str = NPM_PKG,
                      tags: tuple[str, ...] = ("latest", "stable")) -> list[str]:
    """解析 npm dist-tags, 返回指定 tag 对应的版本 (去重, 版本序).

    快速 CI 默认验真实发布通道: latest (默认 npm install) + stable。
    """
    r = subprocess.run(["npm", "view", pkg, "dist-tags", "--json"],
                       capture_output=True, text=True, check=True)
    dt = json.loads(r.stdout)
    picked = {dt[t] for t in tags if t in dt}
    return sorted(picked, key=parse_version)


def parse_version(v: str) -> tuple[int, int, int]:
    """'2.1.156' → (2, 1, 156). 不支持 prerelease."""
    parts = v.split(".")
    return (int(parts[0]), int(parts[1]), int(parts[2]))


def pick_versions(all_versions: list[str], current_minor_recent: int = 5) -> list[str]:
    """抽样: 每 minor latest + 当前 minor 最近 N patch (含 latest, 去重)."""
    by_minor: dict[tuple[int, int], list[str]] = {}
    for v in all_versions:
        try:
            major, minor, _ = parse_version(v)
        except (ValueError, IndexError):
            continue
        by_minor.setdefault((major, minor), []).append(v)
    for k in by_minor:
        by_minor[k].sort(key=parse_version)

    picked: set[str] = set()
    for k, vs in by_minor.items():
        picked.add(vs[-1])  # latest patch of this minor

    if by_minor:
        current_minor = max(by_minor.keys())
        for v in by_minor[current_minor][-current_minor_recent:]:
            picked.add(v)

    return sorted(picked, key=parse_version)


def tarball_url(version: str, platform_pkg: str = PLATFORM_PKG) -> str:
    short = platform_pkg.split("/", 1)[1]  # claude-code-linux-x64
    return f"https://registry.npmjs.org/{platform_pkg}/-/{short}-{version}.tgz"


def fetch_binary(version: str, platform_pkg: str = PLATFORM_PKG) -> Path:
    """下载并解 binary, 返回 binary 路径. 缓存 ~/.cache/cc-l10n/integration/.

    binary 名跨平台不同 (linux/mac: claude, win: claude.exe)。
    """
    version_dir = CACHE_DIR / platform_pkg.split("/")[-1] / version
    is_win = "win32" in platform_pkg
    binary_name = "claude.exe" if is_win else "claude"
    binary_path = version_dir / binary_name
    if binary_path.exists():
        log.info(f"[cache] {binary_path}")
        return binary_path

    version_dir.mkdir(parents=True, exist_ok=True)
    url = tarball_url(version, platform_pkg)
    tgz_path = version_dir / "package.tgz"
    log.info(f"[download] {url}")
    try:
        with urllib.request.urlopen(url) as resp, tgz_path.open("wb") as f:
            shutil.copyfileobj(resp, f)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise NotPublished(f"{platform_pkg}@{version} 不存在 (npm 404)") from e
        raise

    log.info(f"[extract] {tgz_path}")
    with tarfile.open(tgz_path) as tar:
        for member in tar.getmembers():
            if member.name.endswith("/" + binary_name) and member.isfile():
                member.name = binary_name  # strip "package/" prefix
                tar.extract(member, version_dir)
                break
        else:
            raise RuntimeError(f"binary {binary_name!r} not found in {tgz_path}")
    binary_path.chmod(0o755)
    tgz_path.unlink()
    return binary_path


def _ver_key(v: str) -> tuple:
    try:
        return tuple(int(x) for x in v.split("."))
    except ValueError:
        return (0,)


def coverage_versions(versions: list[str]) -> set[str]:
    """每 minor 的最新 patch 需 verify=0 (全覆盖); 过渡 patch 只验流水线鲁棒 (B 语义)."""
    best: dict[str, str] = {}
    for v in versions:
        m = ".".join(v.split(".")[:2])
        if m not in best or _ver_key(v) > _ver_key(best[m]):
            best[m] = v
    return set(best.values())


def run_pipeline(binary: Path, require_coverage: bool) -> tuple[bool, str]:
    """B 语义: repack --strict (流水线鲁棒: skip=0/placeholder=0) 对所有版本;
    coverage-required 版再 verify (untranslated_prompt=0). 平台由 binary 自动判。"""
    out = binary.parent / (binary.name + ".zh")
    r = subprocess.run(
        [sys.executable, str(SCRIPT_DIR / "repack_universal.py"),
         str(binary), str(out), str(TRANS_DIR), "--strict",
         "--report", str(out) + ".report.json"],
        capture_output=True, text=True)
    if r.returncode != 0:
        return False, f"repack(pipeline) exit={r.returncode}\n{(r.stderr or r.stdout)[-1500:]}"
    if require_coverage:
        r2 = subprocess.run(
            [sys.executable, str(SCRIPT_DIR / "verify_coverage.py"),
             "--binary", str(out), "--translations", str(TRANS_DIR),
             "--whitelist", str(WHITELIST)],
            capture_output=True, text=True)
        if r2.returncode != 0:
            return False, f"verify(coverage) exit={r2.returncode}\n{(r2.stdout + r2.stderr)[-1500:]}"
    return True, ""


def collect_matched(binary: Path, trans_keys: set[str], wl_srcs: list[str],
                    matched: set[str]) -> None:
    """把此 binary 命中的 translation key + whitelist src 累加进 matched (供聚合 no-stale)."""
    from bun_format import extract_cli_js
    from js_string_scanner import scan_string_literals
    from js_codec import decode_literal
    from repack_universal import find_src_with_escape
    cli = extract_cli_js(binary)
    decoded = set()
    for s, e, lt in scan_string_literals(cli):
        t = decode_literal(cli, s, e, lt)
        if t is not None:
            decoded.add(t)
    for k in trans_keys:
        if k not in matched and k in decoded:
            matched.add(k)
    for src in wl_srcs:
        if src not in matched and find_src_with_escape(cli, src.encode("utf-8")):
            matched.add(src)


def resolve_supported(supported_csv: str | None,
                      default_versions: list[str]) -> set[str]:
    """译文集官方支持的版本范围 (--check-stale 据此判真 stale)。

    --supported-versions CSV 显式指定; 否则默认 = 本次运行的版本集 (default_versions)。

    为何默认取运行集而非 dist-tags: 译文集是跨版本并集, 含为 latest/stable 之外的
    维护变体 (实测补过 2.1.150/158/159 等)。若把 supported 钉死在窄窄两个 dist-tag,
    那些维护变体会被误判真 stale。正解是【跑足够广的集合 (--all / 抽样) 并对运行集判
    stale】: 跨整个运行集都不命中的条目才是真死译文。窄运行集 (如 --dist-tags) 不应开
    --check-stale (会误杀维护变体); 想显式收窄判定范围时用 --supported-versions。
    """
    if supported_csv:
        picked = {v.strip() for v in supported_csv.split(",") if v.strip()}
        if picked:  # 仅空白/逗号的 CSV 视为未指定, 回退默认 (而非空集 → 全判 stale)
            return picked
    return set(default_versions)


def compute_stale(all_trans_keys: set[str], wl_srcs: list[str],
                  matched: set[str]) -> list[str]:
    """死译文/死白名单 = (全部 translation key ∪ whitelist src) - matched。

    matched 的语义由调用方决定: --check-stale 时只累计【受支持版本】binary 的命中,
    故此处算出的是真 stale (跨所有受支持版本都不命中); 信息模式下是 stale-vs-抽样。
    """
    return sorted((set(all_trans_keys) | set(wl_srcs)) - set(matched))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="append", default=None,
                    help="只跑指定版本 (可多次, e.g. --version 2.1.156 --version 2.1.155)")
    ap.add_argument("--versions-csv", default=None,
                    help="逗号分隔版本列表 (CI 输入用; 优先级高于 --version 和 --all)")
    ap.add_argument("--dist-tags", action="store_true",
                    help="只跑 npm dist-tags 的 latest+stable 版 (快速 CI 默认; 全要 verify=0)")
    ap.add_argument("--all", action="store_true",
                    help="跑 npm 上所有版本 (慢; 默认: 抽样策略)")
    ap.add_argument("--check-stale", action="store_true",
                    help="启用聚合 no-stale 失败门 (真 stale = 不命中任何受支持版本; "
                         "运行集会自动补齐 supported 以保证判定可靠)")
    ap.add_argument("--supported-versions", default=None,
                    help="逗号分隔的官方支持版本范围 (默认 = 本次运行的版本集); "
                         "--check-stale 据此判真 stale (窄运行集会误杀跨版本维护变体)")
    ap.add_argument("--current-minor-recent", type=int, default=5,
                    help="当前 minor 额外抽最近 N 个 patch (默认 5)")
    ap.add_argument("--list", action="store_true",
                    help="仅列将要跑的版本 (每行 1 个), 不跑")
    ap.add_argument("--list-json", action="store_true",
                    help="仅输出版本 JSON 数组 (CI matrix 用)")
    ap.add_argument("--platform-pkg", default=None,
                    help="单平台子包 (设了则只跑该平台; 否则跑 --platforms 全部)")
    ap.add_argument("--platforms", default=",".join(PLATFORM_PKGS),
                    help="逗号分隔多平台子包 (默认 linux+win+mac)")
    args = ap.parse_args()

    # explicit_targets: 用户显式点名/dist-tag 的版本 = 真实发布目标, 全要 verify=0;
    # 抽样/--all 走 B 语义 (每 minor 最新才全覆盖, 过渡 patch 仅流水线鲁棒)。
    explicit_targets = False
    if args.versions_csv:
        versions = [v.strip() for v in args.versions_csv.split(",") if v.strip()]
        explicit_targets = True
    elif args.version:
        versions = args.version
        explicit_targets = True
    elif args.dist_tags:
        versions = dist_tag_versions()
        explicit_targets = True
    elif args.all:
        versions = npm_view_versions()
    else:
        all_versions = npm_view_versions()
        versions = pick_versions(all_versions, args.current_minor_recent)

    if args.list:
        for v in versions:
            print(v)
        return 0
    if args.list_json:
        print(json.dumps(versions))
        return 0

    platforms = ([args.platform_pkg] if args.platform_pkg
                 else [p.strip() for p in args.platforms.split(",") if p.strip()])

    # #4 聚合 no-stale 版本范围: --check-stale 时定义 supported 范围, 并把它并入运行集
    # (保证每个受支持版本都被跑过 → matched 完整 → stale 判定可靠); matched 之后只累计
    # supported 版本的命中, 故 stale = 跨所有受支持版本都不命中 = 真死译文。
    supported: set[str] = set()
    if args.check_stale:
        supported = resolve_supported(args.supported_versions, versions)
        log.info(f"--check-stale: 受支持版本范围 = {sorted(supported, key=parse_version)}")
        # 护栏: 窄运行集 (dist-tags/单版本) 且无显式 --supported-versions 时, supported
        # 仅含那一两个版本, 跨版本维护变体会被误判 stale。提醒用户用抽样/--all 或显式指定。
        if not args.supported_versions and len(supported) <= 2 and (args.dist_tags or args.version):
            log.warning(f"[WARN] --check-stale 配窄运行集 (supported 仅 {len(supported)} 版): "
                        f"跨版本维护变体可能被误判 stale。广覆盖请用抽样/--all, 或显式 --supported-versions。")
        missing = supported - set(versions)
        if missing:
            log.info(f"  运行集补齐 supported 缺失版本: {sorted(missing, key=parse_version)}")
            versions = sorted(set(versions) | supported, key=parse_version)

    cov_versions = set(versions) if explicit_targets else coverage_versions(versions)
    if args.check_stale:
        cov_versions |= supported  # 受支持版本是发布目标, 全要 verify=0

    log.info(f"将跑 {len(versions)} 版 × {len(platforms)} 平台 (B 语义)")
    log.info(f"  版本: {versions}")
    log.info(f"  平台: {[p.split('/')[-1] for p in platforms]}")
    log.info(f"  需全覆盖 (每 minor 最新, verify=0): {sorted(cov_versions)}")
    log.info(f"  其余: 仅验流水线鲁棒 (apply 成功/skip=0); cache: {CACHE_DIR}")

    # 聚合 no-stale 用: 所有 translation key + whitelist src, 跨所有 binary 累计命中
    from bun_format import platform_from_binary  # noqa
    from extract_prompt_segments import load_translations_keys, load_whitelist
    wl = load_whitelist(WHITELIST)
    wl_srcs = [s for s in wl.get("literal_exact", ()) if s and not s.startswith("_comment")]
    matched: set[str] = set()
    all_trans_keys: set[str] = set()
    collected_versions: set[str] = set()  # 实际成功 collect_matched 的版本 (≥1 平台)

    failed = []
    skipped = []
    for pkg in platforms:
        plat = "win32" if "win32" in pkg else ("darwin" if "darwin" in pkg else "linux")
        plat_keys = load_translations_keys(TRANS_DIR, plat)
        all_trans_keys |= plat_keys
        for v in versions:
            label = f"{v}/{plat}"
            log.info(f"\n========== {label} ==========")
            try:
                binary = fetch_binary(v, pkg)
            except NotPublished as e:
                log.info(f"[SKIP] {label}: {e}")
                skipped.append(label)
                continue
            except Exception as e:
                log.error(f"[FAIL] {label}: fetch error: {e}")
                failed.append(label)
                continue
            ok, err = run_pipeline(binary, require_coverage=(v in cov_versions))
            if ok:
                log.info(f"[OK] {label}" + (" (full coverage)" if v in cov_versions else " (pipeline)"))
            else:
                log.error(f"[FAIL] {label}:\n{err}")
                failed.append(label)
            # --check-stale 时只累计【受支持版本】的命中 (matched 据此判真 stale);
            # 信息模式累计所有运行版本 (stale-vs-抽样, 仅提示)。
            if (not args.check_stale) or (v in supported):
                try:
                    collect_matched(binary, plat_keys, wl_srcs, matched)
                    collected_versions.add(v)
                except Exception as e:
                    log.warning(f"[WARN] {label}: collect_matched 失败: {e}")

    # 聚合 no-stale: 任一 translation key / whitelist src 跨所有 (版本×平台) 都没命中 = 死译文/死白名单。
    # 默认仅信息提示 (译文集支持哪些版本尚未定; 145=stable/156=latest 都合法, 历史变体不算死);
    # --check-stale 才作失败门 (待版本范围定后开)。
    stale = compute_stale(all_trans_keys, wl_srcs, matched)
    log.info("")
    log.info("=" * 40)
    log.info(f"运行: {len(versions)} 版 × {len(platforms)} 平台; "
             f"失败 {len(failed)}; skip {len(skipped)} (npm 上无该组合)")
    if skipped:
        log.info(f"  skip: {skipped[:20]}")
    stale_fail = bool(stale) and args.check_stale
    # 护栏: 若某受支持版本全平台都没能 collect (404/fetch 失败), matched 缺其贡献,
    # 仅存于该版本的活译文会被误判 stale → 判定不可靠, 本次不作失败门 (只警告)。
    if args.check_stale:
        uncollected = supported - collected_versions
        if uncollected:
            log.warning(f"[WARN] {len(uncollected)} 个受支持版本全平台未能收集 (404/失败): "
                        f"{sorted(uncollected, key=parse_version)}; matched 不完整, stale 判定不可靠, "
                        f"本次不作失败门 (修复版本可用性后重判)")
            stale_fail = False
    scope = (f"跨 {len(supported)} 个受支持版本" if args.check_stale else "全平台全抽样版本")
    log.info(f"聚合 no-stale: translation+whitelist 共 {len(all_trans_keys)+len(wl_srcs)} 条, "
             f"{scope}未命中 {len(stale)} 条"
             + ("" if args.check_stale else " (信息提示; --check-stale 才判失败)"))
    if stale:
        level = log.error if stale_fail else log.info
        level(f"{'[FAIL] ' if stale_fail else ''}{len(stale)} 条未命中任何抽样 binary:")
        for s in stale[:15]:
            level(f"     {s[:80]!r}")
    if failed:
        log.error(f"[FAIL] {len(failed)} 个 (版本/平台) 未过: {failed[:20]}")
    if failed or stale_fail:
        return 1
    log.info("[OK] 全部通过 (流水线鲁棒 + 目标版全覆盖"
             + ("" if not args.check_stale else " + 无死译文") + ")")
    return 0


if __name__ == "__main__":
    sys.exit(main())
