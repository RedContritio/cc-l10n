# cc-l10n — Claude Code 系统提示词中文化

把 Anthropic Claude Code CLI（`claude` 命令）内置的英文 system prompt 替换为中文版，对抗长会话中模型逐渐"漂回"中英混杂状态的问题。

不是 wrapper、不是 proxy、不靠 hook 注入提醒——**直接在 binary 内原地替换 JS 字符串字面量**。

跨 macOS / Linux / Windows。仅依赖 Python 3.10+ 与目标平台的 codesign 工具（macOS）。

---

## 项目背景

观察：CC 在长对话中会逐渐从纯中文回复退化为中英混杂。根因是初始的 `Always respond in 简体中文` 指令随上下文变长被稀释，加上工具结果与代码大量为英文，模型逐渐对齐到环境的语言分布。

直觉上有几种解法：

| 方案 | 优点 | 缺点 |
|---|---|---|
| UserPromptSubmit hook 每轮注入提醒 | 实现简单 | 提示词噪音，仍在英文环境里跑 |
| 在 CLAUDE.md 加约束 | 零侵入 | 同样随距离衰减 |
| **替换内置系统提示词为中文** | 从根本上消除"环境拉力" | 工程量大 |

本项目走第三条路。

---

## 快速开始

`git clone` 后即可使用，无需额外依赖（除 Python 3.10+）。

```bash
git clone git@github.com:RedContritio/cc-l10n.git
cd cc-l10n
```

### macOS / Linux

```bash
./cc-l10n install        # 自动定位 active binary, 备份原版 + 替换中文版
./cc-l10n restore        # 还原原版
./cc-l10n apply <in> <out>   # 对任意 binary 应用翻译
```

也可直接调 Python 入口：

```bash
python3 cc_l10n.py install
```

### Windows

```cmd
cc-l10n.cmd install
cc-l10n.cmd apply <in> <out>
```

或：

```cmd
python cc_l10n.py install
```

**Windows 注意**：CC 在 Windows 不依赖 macOS 的 codesign。`install_universal.py` 自动检测平台跳过相应步骤。如自动定位失败，用 `--binary <path>` 显式指定。

---

## 子命令一览

```
cc-l10n install                    自动定位 active CC binary 并替换为中文版
cc-l10n restore                    还原原版 binary
cc-l10n apply <in> <out>           对任意 binary 应用翻译 (audit + repack strict)
cc-l10n audit <binary>             预审计翻译应用风险 (B/C/D/E)
cc-l10n repack <in> <out>          单跑 repack (跳过 audit)
cc-l10n extract <binary>           静态提取所有 prompt 字面量, 输出 JSON 报告
cc-l10n diff <report.json>         对比 extract 输出, 看 untranslated/whitelisted
cc-l10n verify --binary <bin>      STATIC strict: untranslated_prompt 必须为 0
cc-l10n verify --captured <jsonl>  CAPTURE strict: 实测 prompt 无英文残留
cc-l10n coverage <binary>          扫 binary 英文残留 (legacy)
```

### 退出码

| 退出码 | 含义 |
|---|---|
| `0` | 全部成功 |
| `1` | audit 检测到高危 / static verify 失败 |
| `2` | repack strict 失败 (miss > 0 或 真 skip > 0) / capture verify 失败 |
| `3` | placeholder violation（语义反转风险，永远拒绝） |

---

## 目录结构

```
cc_zh/
├── cc_l10n.py                       # 跨平台 Python 入口
├── cc-l10n                          # bash wrapper (Unix)
├── cc-l10n.cmd                      # cmd wrapper (Windows)
├── README.md
├── data/
│   ├── translations/                # 按主题分文件加载, 目录下所有 *.json 都会合并
│   └── whitelist.json               # 显式不翻的英文片段 (exact / regex / literal_exact)
├── scripts/                         # 核心工具链
│   ├── repack_universal.py          # scope-aware 字面量替换核心
│   ├── audit_replacements.py        # patch 前 5 类风险审计
│   ├── apply_translations.py        # audit + repack strict 端到端
│   ├── install_universal.py         # 跨平台 install (macOS codesign / rm+cp)
│   ├── restore_universal.py         # 从 .orig 备份还原
│   ├── extract_prompt_segments.py   # 静态提取 prompt 字面量 + 启发式分类
│   ├── diff_coverage.py             # 对比 extract 输出
│   ├── verify_coverage.py           # STATIC / CAPTURE strict 验证
│   ├── expand_whitelist.py          # CC 升级后从 extract 报告增量入白 (--from-extract)
│   ├── code_patches.py              # binary-level surgical fix (cacheScope 等)
│   ├── bun_format.py                # Bun trailer/Offsets/extract_cli_js 公共解析
│   ├── js_string_scanner.py         # JS 字面量边界扫描 (含嵌套 tmpl)
│   └── legacy/coverage_report.py    # 旧英文残留扫描 (legacy)
├── tools/                           # 可选辅助 (binary 解 / prompt 探针)
│   ├── unpack.py / dump_linux_cli.py    # 解 binary 拿 cli.js
│   ├── extract_prompts_v3.py            # 扫所有 prompt 候选 (legacy)
│   ├── find_subagents.py
│   └── dump_subagent_prompts.py
└── test/
    ├── fake_anthropic.py            # mock server 拦截 /v1/messages (CAPTURE 验证用)
    ├── test_audit_replacements.py
    ├── test_extract_prompt_segments.py
    ├── test_js_string_scanner.py
    └── test_verify_coverage.py
```

---

## 严格化 pipeline

cc-l10n 的核心保证是 **strict no-tolerance**：

1. **每个英文字面量必须二选一**：已翻译 OR 显式入 whitelist
2. **任何 miss/skip 都失败**：`apply_translations` 的 `--strict` 默认 miss=0 skip=0 不可调
3. **placeholder 不匹配立即拒绝**：src 与 dst 的 `${...}` 占位符不一致 = exit 3

### Whitelist 3 类规则

```json
{
  "exact": ["Claude Code", "agent", ...],
  "regex": ["https?://[^\\s]+", "\\$\\{[\\w.]+\\}", ...],
  "literal_exact": [
    "You are a security monitor for autonomous AI coding agents.",
    "# Anthropic CLI (",
    ...
  ]
}
```

- `exact` / `regex`：token 级减法（段落减去所有匹配后无英文词即视为已翻）
- `literal_exact`：整段字面量 strip 后精确相等（用于子 agent prompt、内置 skill 文档、tool description 等）

### 端到端验证

```bash
# 1. STATIC: 直接扫 binary, 检查所有 score>=5 字面量 100% 覆盖
./cc-l10n verify --binary ~/.local/share/claude/versions/2.1.142.orig

# 2. apply pipeline (audit + repack strict)
./cc-l10n apply ~/.local/share/claude/versions/2.1.142.orig /tmp/cc-zh-out

# 3. 验证 patched binary
./cc-l10n verify --binary /tmp/cc-zh-out
```

实测三步全 pass：412 prompt 候选 (score>=5) 100% 覆盖（已翻 OR whitelist）。

---

## 翻译条目格式

translations 按主题分文件加载，目录下所有 `*.json` 都会合并。每条 entry：

**普通**（严格 placeholder 顺序匹配）：

```json
{
  "with ${X} and ${Y}": "用 ${X} 和 ${Y}"
}
```

**允许重排 placeholder**（中文语序要求）：

```json
{
  "spawn ${AGENT} with type=${TYPE}": {
    "dst": "以 type=${TYPE} 派 ${AGENT}",
    "allow_reorder": true
  }
}
```

**文件级 metadata**（`_` 前缀 key 被 loader 跳过）：

```json
{
  "_meta": {
    "category": "doing_tasks",
    "description": "# Doing tasks 段"
  },
  "src": "dst"
}
```

---

## 跨平台 / 跨版本 sentinel

不同平台和 CC 版本的 cli.js minified 变量名不同：

| 含义 | macOS 2.1.142 | Linux 2.1.143 |
|---|---|---|
| Bash 工具 | `${O}` | `${O}` |
| Tool list | `${T}` | `${Y}` |
| TaskCreate | `${_}` | `${K}` |
| Agent | `${J7}` | `${T9}` |
| Skill | `${Af}` | `${WM}` |
| Search | `${z}` | `${z}` |
| Query threshold | `${j87}` | `${xK9}` |
| Subagent type | `${tt.agentType}` | (同) |
| Memory dir | `${_}` | `${_}` |
| Memory index | `${xD}` | `${BP}` |
| Max index lines | `${X9H}` | `${W_8}` |
| Language | `${H}` | `${q}` |

translations 写 sentinel `${__SENTINEL_NAME__}`，repack 时从目标 cli.js 自动提取真实变量名替换。当前 sentinel：

- 工具调度：`__BASH_TOOL__` `__TOOL_LIST__` `__TASKCREATE_TOOL__` `__AGENT_TOOL__` `__SKILL_TOOL__` `__SEARCH_TOOL__`
- 阈值/类型：`__QUERY_THRESHOLD__` `__SUBAGENT_TYPE__`
- Memory：`__MEMORY_DIR_VAR__` `__MEMORY_INDEX_VAR__` `__MAX_INDEX_LINES_VAR__`
- 语言：`__LANGUAGE_VAR__`
- powered-by-model（含多实例展开）：`__MODEL_NAME_VAR__` `__MODEL_ID_VAR__` `__MODEL_ID_FB_VAR__`

`SENTINEL_RESOLVERS` 允许同名 sentinel 多次声明，按 cli.js 中出现顺序 zip-匹配多实例（覆盖 powered-by-model 段在 cli.js 中有 6 处、每处局部变量名都不同的场景）。

**anchor-offset 模型**：每条 sentinel = `(name, before, after)`，匹配 `<before><${X}><after>` 三段连续提取中间变量。`before` 为空时改用 `after` 反向定位。

新增 sentinel：在 `scripts/repack_universal.py` 的 `SENTINEL_RESOLVERS` 加一行，跑一次 `cc-l10n apply` 看 strict pass 即验证完成。

---

## 技术原理

### CC 是 Bun standalone executable

`claude` 在 macOS 是 200+ MB Mach-O，Linux 容器是同体量 ELF，Windows 是 `.exe`。都是 `bun build --compile` 产物：

```
[Bun runtime binary]
[payload: 各模块的 minified JS 源码 + JSC 预编译字节码 + sourcemap]
[CompiledModuleGraphFile 数组 (52 字节/项)]
[Offsets struct (32 字节): byte_count, modules_ptr, entry_point_id, ...]
[16 字节 trailer: "\n---- Bun! ----\n"]
```

**BlobHeader (segment 起点 8 字节 u64)** = `byte_count + 48`，runtime 通过它定位 trailer。

### JSC bytecode fails-open

Bun 预编译 JSC bytecode（约 113 MB）。但 [Bun 官方文档](https://bun.com/docs/bundler/bytecode)：**bytecode 与源码 hash mismatch 时 JSC 静默丢弃 bytecode 重新解析源码**。我们只改 JS 源码，bytecode 池里副本自动失效。

### Segment 容量约束

Binary 整体字节数不能变（否则破坏 Mach-O LINKEDIT / ELF segment 偏移、codesign 拒绝）。新 payload + Offsets + trailer + NUL padding 必须正好填满原 segment。同时同步更新 BlobHeader。

| 平台 | Segment 内可用 padding |
|---|---|
| macOS 2.1.142 | ~12 KB |
| Linux 2.1.143 | ~9.8 KB |

中文译文 UTF-8 字节通常比英文短（汉字 3 字节但承载信息密度高），实测主提示词翻译后**净缩短 ~9 KB**，安全在预算内。

### macOS amfi cache 陷阱

部署时 **必须 rm + cp** 替换 active binary（不是 cp 覆盖）：

- cp 覆盖会复用 inode
- macOS launchd/amfi 缓存的 signature trust 仍指向旧内容
- 新 binary 启动 SIGKILL（exit 137，无 stderr）

`install_universal.py` 自动用 unlink + copy。

### em-dash 形态

cli.js minifier 把非 ASCII 字符 escape 成 `\uXXXX` 形式（实际字节是 `\` + `u` + `2` + `0` + `1` + `4` 6 字符），不是 UTF-8 3 字节。translations src 必须用 `\\u2014` JSON 形态匹配 —— 直接在 src 里写真 UTF-8 em-dash 会扫不到对应字面量。

---

## 验证方法

为了证明翻译真实进入 API 请求（而不只是 binary 里有中文字符），跑 fake Anthropic server 拦截 `/v1/messages`：

```bash
# 启动 mock
python3 test/fake_anthropic.py 18888 &

# 触发 CC 跑一次 (会被拦截)
ANTHROPIC_BASE_URL=http://127.0.0.1:18888 \
  ANTHROPIC_API_KEY=mock-key \
  claude -p "test"

# CAPTURE 验证
./cc-l10n verify --captured /tmp/captured.jsonl
```

---

## 已知限制

1. **静态片段才能翻译**：被 `${...}` 占位符切开的句子，每个片段单独翻。
2. **重叠 src 的 fallback 形态**：例如 `"You are powered by the model "` 是 `"You are powered by the model named "` 的子串前缀，无法独立替换，前者保留英文（仅 model name 未配置时的罕见 fallback）。
3. **CC 更新需重对**：每次 CC 升 patch version 跑一遍 `extract` + `diff` 看新出现的 untranslated_prompt。
4. **子 agent / skill 文档暂保留英文**：通过 `literal_exact` 显式标记为 whitelist。需要翻时移到 translations。

---

## 参考资源

- Bun standalone graph 格式：[oven-sh/bun StandaloneModuleGraph.zig](https://github.com/oven-sh/bun/blob/main/src/standalone_graph/StandaloneModuleGraph.zig)
- Bun bytecode 文档：[https://bun.com/docs/bundler/bytecode](https://bun.com/docs/bundler/bytecode)
- JSC CachedTypes：[WebKit JSC CachedTypes.h](https://github.com/WebKit/WebKit/blob/main/Source/JavaScriptCore/runtime/CachedTypes.h)
