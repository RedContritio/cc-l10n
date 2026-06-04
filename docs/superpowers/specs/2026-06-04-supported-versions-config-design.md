# supported_versions 配置设计

日期: 2026-06-04 · 状态: 已确认, 实现中

## 问题

CI 在运行时从 npm dist-tags (latest+stable) 动态解析受测版本, 并对它们强制 `verify=0`。
后果: npm 把 latest 跳到 2.1.162 (带 31.6K 字符的 design-sync 新文档)、stable 跳到 2.1.152
(带版本偏移的散文 prompt), 我们没主动选却被迫要全覆盖 → CI 被动变红。
同时缺少 orig (pre-patch) binary 指纹, 无法在 patch 前确认操作的是已知 binary
(code-patch "src 已不在 cli.js" / 版本偏移 都是事后才在 repack 里炸出来)。

## 方案 (用户裁定)

新增 `data/supported_versions.json`: 显式声明受支持版本及其原始 cli.js sha256。
配置成为"哪些版本要 `verify=0`"的**唯一真相**, 与 npm dist-tags **解耦**;
patch 前用 orig hash **断言**操作的是已知 binary。

### 配置 schema

```json
{
  "_comment": "...",
  "versions": {
    "2.1.152": { "darwin-arm64": "<sha256>", "linux-x64": "<sha256>", "win32-x64": "<sha256>" },
    "2.1.161": { "darwin-arm64": "<sha256>", "linux-x64": "<sha256>", "win32-x64": "<sha256>" }
  }
}
```

- hash = `sha256(extract_cli_js(orig_binary))` 的 hex。
- 粒度 version×platform (实测 cli.js 跨平台不一致: 2.1.152 darwin 15434761B vs linux 15429365B)。
- 平台键 = npm 包 arch 后缀 (`claude-code-` 之后): `darwin-arm64` / `linux-x64` / `win32-x64`。

### 共享模块 `scripts/supported_versions.py`

单一职责的加载+断言层, 供三处消费复用:
- `load_config(path=CONFIG_PATH) -> dict`
- `cli_js_sha256(binary: Path) -> str`
- `platform_key(platform_pkg: str) -> str` ("@.../claude-code-linux-x64" → "linux-x64")
- `supported_versions(cfg) -> list[str]` (版本序)
- `expected_hash(cfg, version, platform_key) -> str | None`
- `find_match(cfg, sha) -> list[(version, platform_key)]` (install/repack 不知版本时用)

### 生成脚本 `scripts/gen_supported_versions.py`

`python gen_supported_versions.py 2.1.152 2.1.161 [--platforms ...]`:
对每个 version×platform `fetch_binary` → `extract_cli_js` → sha256 → 写配置。幂等。
404 (该平台未发布) → 跳过该格, 不写。

### 三处消费 (决策 A: 支持集真相 + hash 断言)

1. **integration_test.py**: 新增 `--supported` 模式。版本集 = 配置 `supported_versions`;
   逐 version×platform 先**断言**下载到的 orig cli.js sha256 == 配置 (不符 → FAIL
   "binary 漂移/版本不符"), 再 repack --strict + verify=0。`resolve_supported` (no-stale)
   亦改读配置。
2. **repack_universal.py**: `--assert-orig <config>` 开关。patch 前算 orig cli.js sha256,
   若不是配置中任一已知 hash → raise; 是 → 放行。默认关 (ad-hoc repack 不受影响)。
3. **install_universal.py**: patch 活动 binary 前算其 orig cli.js sha256, **必须** `find_match`
   命中某受支持版本, 否则 raise 拒装 ("未支持的 CC 版本, 译文未对该版构建"); 显式 `--force` 绕过。

### CI workflow

PR 门改跑 `integration_test.py --supported` (读配置, 测 supported × 三平台, hash + verify=0)。
dist-tags / 抽样降为周跑信息门 (保留新版预警)。

## 本次落地

- 配置 supported = {2.1.152, 2.1.161} × 三平台。
- 补完 2.1.152 的 3 条版本偏移 prompt (Worker results: `subagent_tokens`↔`total_tokens` /
  AskUserQuestion 描述 / subscribe_pr_activity 片段) → 2.1.152 verify=0。
  (Angle A/B/C 合并 vs 拆分译条重叠冲突已先行修复: 合并条 src 补尾 \n 使拆分条嵌套被跳过 +
  Angle→视角 统一。)
- 2.1.161 已绿 (darwin 已验; 补验 linux/win32)。
- 2.1.162 **不入配置** → 不被门禁 → design-sync (技能 4K / 工具 3.7K / 包源文档 31.6K / tone 句)
  缓译, 列为后续版本轮次。

## 测试 (每契约配测试)

- `test_supported_versions.py`: load_config / cli_js_sha256 (对缓存 binary) / platform_key /
  expected_hash / find_match; hash 不符断言 raise (pytest.raises); install --force 绕过。
- 真环境验证: 本地 `integration_test.py --supported --platforms darwin,linux` (缓存 binary) 全绿。

## 提交

单 commit (用户裁定): 配置 + 共享模块 + 生成脚本 + 三处消费 + CI + 2.1.152 补条 + 测试。
dev 分支直接 commit (无需确认)。
