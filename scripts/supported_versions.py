"""受支持版本配置 (data/supported_versions.json) 的加载 + orig hash 断言层.

配置声明"哪些 CC 版本要 verify=0"的唯一真相 (与 npm dist-tags 解耦), 并钉死每个
受支持版本×平台的原始 (pre-patch) cli.js sha256。patch/测试前用它断言操作的是已知 binary。

schema:
  {
    "versions": {
      "2.1.152": {"darwin-arm64": "<sha256>", "linux-x64": "<sha256>", "win32-x64": "<sha256>"},
      ...
    }
  }

平台键 = npm 包 arch 后缀 (claude-code- 之后): darwin-arm64 / linux-x64 / win32-x64。
hash 粒度为 version×platform —— 实测 cli.js 跨平台不一致 (Bun 打包平台特定代码)。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
CONFIG_PATH = PROJECT_ROOT / "data" / "supported_versions.json"


def _version_key(v: str) -> tuple[int, ...]:
    """'2.1.152' → (2,1,152), 供版本序排序。非数字段落降级为 0 以免崩。"""
    out = []
    for part in v.split("."):
        try:
            out.append(int(part))
        except ValueError:
            out.append(0)
    return tuple(out)


def load_config(path: Path | str = CONFIG_PATH) -> dict:
    """读配置。文件不存在 → 抛 FileNotFoundError (严格契约, 不静默返回空)。"""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"supported_versions 配置不存在: {p}")
    cfg = json.loads(p.read_text(encoding="utf-8"))
    if "versions" not in cfg or not isinstance(cfg["versions"], dict):
        raise ValueError(f"supported_versions 配置缺少 'versions' 字典: {p}")
    return cfg


def platform_key(platform_pkg: str) -> str:
    """'@anthropic-ai/claude-code-linux-x64' → 'linux-x64'。

    既接受完整 npm 包名, 也接受已是 'claude-code-linux-x64' 或 'linux-x64' 的形式。
    """
    name = platform_pkg.rsplit("/", 1)[-1]  # claude-code-linux-x64
    prefix = "claude-code-"
    if name.startswith(prefix):
        name = name[len(prefix):]
    return name


def cli_js_sha256(binary: Path | str) -> str:
    """提取 binary 的 cli.js 并算 sha256 hex。binary 须为原始 (pre-patch) 文件。"""
    from bun_format import extract_cli_js  # 延迟导入: 避免无谓依赖链
    cli = extract_cli_js(Path(binary))
    return hashlib.sha256(cli).hexdigest()


def supported_versions(cfg: dict) -> list[str]:
    """配置中声明的受支持版本 (版本序)。"""
    return sorted(cfg["versions"].keys(), key=_version_key)


def expected_hash(cfg: dict, version: str, plat_key: str) -> str | None:
    """查 (version, platform) 的钉死 hash; 缺失返回 None (该格未发布/未录)。"""
    return cfg["versions"].get(version, {}).get(plat_key)


def find_match(cfg: dict, sha: str) -> list[tuple[str, str]]:
    """反查: 哪些 (version, platform_key) 的钉死 hash == sha。

    install/repack 不知版本时用 (拿 binary 算 hash → 反查是否受支持)。
    """
    hits = []
    for ver, plats in cfg["versions"].items():
        for pk, h in plats.items():
            if h == sha:
                hits.append((ver, pk))
    return sorted(hits, key=lambda t: (_version_key(t[0]), t[1]))


def assert_orig_supported(binary: Path | str, cfg: dict | None = None,
                          *, force: bool = False) -> tuple[str, str] | None:
    """断言 binary 的 orig cli.js hash 命中某受支持 (version, platform)。

    命中 → 返回 (version, platform_key)。
    未命中且 force=False → raise ValueError (拒绝对未知版本操作)。
    未命中且 force=True → 返回 None (调用方放行)。
    """
    if cfg is None:
        cfg = load_config()
    sha = cli_js_sha256(binary)
    hits = find_match(cfg, sha)
    if hits:
        return hits[0]
    if force:
        return None
    raise ValueError(
        f"binary 的 orig cli.js sha256={sha[:16]}… 未命中任何受支持版本 "
        f"(supported={supported_versions(cfg)})。译文未对该版构建; 如确需操作请用 --force。"
    )
