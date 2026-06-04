# cc-tool-parse-monitor 设计

全局常驻、旁路监控 Claude Code 偶发 `The model's tool call could not be parsed (retry also failed).`，
并为每条事件**自带** patched/unpatched 归因。

## 背景（诊断结论，2026-06-01）

- 错误语义（2.1.158 cli.js 实测）：`stop_reason==="tool_use" && qH.length===0`（声明调工具但抽不出有效 tool_use 块）。
  CC 先注入 SOFT 重试串重试一次，再失败才发 HARD 串。
- 实测 transcript：5-31 真实事件横跨 **2.1.156**（gicg-mono）与 **2.1.158**（stock）两个版本；
  其中 stock/158 极可能跑在**未 patch** 原版上（158 二进制 17:34 UTC 诞生即原版，patched `.zh` 18:38 UTC 才 build，
  事件 18:18–18:31 UTC 落在“只存在未 patch 158”窗口内，~7 分钟余量）。
- **定性：这是 CC/模型的基线偶发行为，不是中文 patch 引起的。** A 类（repack 撞坏工具定义）此前已排除。
- `version` 字段**不能**表达 patch 状态：patched `.zh` 与原版**都自报同一版本号**。SOFT/HARD 标记串**不在译表里**
  （`grep "could not be parsed" data/translations` 空），故标记语言也当不了 patch 指纹。→ 必须指纹化二进制本身。

## 架构（混合：旁路常驻 + 按需深抓）

```
~/.claude/projects/**/*.jsonl ──poll──> watcher ──> detect ──> incident(归并) ──+──> notify(digest / iMessage / macOS)
                                            │                                     │
                          active claude binary ──> binary_state(patch 探测+时间线)─┘  每条事件附 {patched, sha256}

capture_proxy.py（现有，按需）：digest 指出哪个项目/会话在复发 → 手动 ANTHROPIC_BASE_URL 接入抓原始字节
```

### 单元

- **detect.py** — 纯函数。`classify_record(rec) -> None | {severity, marker_text}`：
  - HARD：`type=="assistant"` ∧ `isApiErrorMessage==true` ∧ `message.model=="<synthetic>"` ∧ 任一 content.text 完全等于 HARD 串。
  - SOFT：`type=="user"` ∧ `isMeta==true` ∧ `message.content`（纯字符串）完全等于 SOFT 串。
  - HARD/SOFT 严格结构 + 完全等值 → 对“对话里引用该串”的记录零误报。
  - **SILENT（best-effort，非零误报）**：工具调用文本化逃逸 —— `type=="assistant"` ∧ 无 tool_use 块 ∧ `stop_reason!=tool_use` ∧ 某 text 块含“损坏 `<function_calls>` opener 裸词行 + `<invoke name=`”（CC 仅在 stop==tool_use 时注入 SOFT/HARD，故 end_turn 逃逸被 retry 路径漏抓）。**局限**：(a) 已防 fenced 代码块（贴原文，opener 前一行为 ``` 则跳过），但散文里“普通文本行 + 损坏 opener 词单独成行 + invoke”与真逃逸字节同形、无法区分，仍会假阳性（罕见，主要见于讨论该 bug 的对话）；(b) 漏报 opener 非 `[a-z]{2,12}` 形态（含数字/大写/超长）。
- **incident.py** — `build_incident(rec, ctx)` 提取 `{ts, sessionId, project(cwd), gitBranch, version, severity, transcriptPath, lineNo, context(截断)}`；
  按 `(sessionId, promptId)` 归并：一次 SOFT→升级 HARD = **一个** incident（severity 取最重，hard>soft>silent，silent 可被覆盖但不反向降级），不重复计数。无 promptId 的记录（HARD/SILENT）继承同 session 最近真实 promptId —— `scan_once` 对普通 user 记录调 `note_prompt` 更新继承链（防 SILENT 错并入陈旧 promptId 的 incident，修真实数据复现的跨请求污染）。**known-limitation**：尚未实现真正的 parentUuid 链回溯，多请求乱序交错时仍可能错并（见 BACKLOG #11-3e）。
- **binary_state.py** — patch 指纹：
  - `resolve_active_binary()`：跟随 `~/.local/bin/claude` 符号链接到真二进制。
  - `probe_patch_state(path) -> {patched, sha256, version, probes}`：扫 2–3 条**已知翻译产物**（取早期 round 的稳定核心串，156/158 通用）。
    含中文目标串→patched；含英文源串→unpatched。版本无关、确定性，免维护 hash 表。
  - `BinaryStateLog`：active 二进制换链/换 hash 时追加 `{ts, version, sha256, patched}` 到 `binary_state.jsonl`。
  - 事件归因：incident 时间戳 → 查 binary_state 时间线（解决“探测有延迟”）；命中时也附一次即时指纹。
- **notify.py** — `notify(incident, config)` 渠道按优先级，单渠道失败不影响其他/不崩主循环：
  1. **digest（始终开，事实源）**：追加 `incidents.jsonl`。
  2. **iMessage self-chat（主推送）**：shell `osascript`→Messages 发给配置的自身 handle，best-effort。
  3. **macOS 通知（兜底，默认可关）**：`osascript display notification`。
- **watcher.py** — 主循环（常驻入口）：poll `~/.claude/projects/**/*.jsonl`，按 `state.json` 持久化每文件字节偏移读增量，
  喂 detect→incident→binary_state→notify。重启从偏移续读，不重复告警。
- **config.py** — 加载/默认：self iMessage handle、poll 间隔、各渠道开关、projects 根、探针串。
- **launchd** — `com.cc-l10n.parse-watcher.plist`（`RunAtLoad`+`KeepAlive`），`install.py`/`uninstall.py`；日志 `watcher.log`。

### 数据位置

- 代码：`tools/parse_monitor/`（repo 内）。
- 运行时（全局）：`~/.claude/parse-monitor/`：`config.json` / `state.json` / `incidents.jsonl` / `binary_state.jsonl` / `watcher.log`。
  含对话片段，敏感 → 不入 git（`.gitignore` 已忽略 `.capture/`，新增忽略此目录或复用）。

## 风险（诚实标注）

1. **iMessage 从 daemon 发**在 Darwin 25.5 上 AppleScript send 可靠性不确定；digest 永远可靠，iMessage 实现后做一次性验证，不行再议替代。
2. **TCC 自动化授权**：launchd 控制 Messages 需在“系统设置→隐私→自动化”授权一次。
3. **daemon 宕机窗口**：宕着且期间换过二进制的事件，patch 归因退回 mtime 推断。常驻即无此问题。
4. **历史事件**（昨天 stock/gicg-mono）无当时状态时间线，patch 归因仍靠 mtime 推断；往后精确。

## 测试

纯函数契约测试为主，不触网、不真发 iMessage：detect 谓词（HARD/SOFT/讨论式零误报/坏 JSON）、incident 提取与 SOFT+HARD 归并去重、
binary_state 探测（patched/unpatched fixture 字节，非真二进制）、watcher 偏移推进与重启续读、notify 分发（osascript mock，digest 用临时文件真写）。

## 任务链（提交就绪边界）

- T1 detect（无依赖）
- T2 incident 提取+归并（blockedBy T1）
- T3 binary_state 探测+时间线（无依赖）
- T4 notify 渠道（无依赖）
- T5 watcher 主循环+state（blockedBy T1,T2,T3；notify 以回调注入便于并行）
- T6 launchd+install/uninstall+config+iMessage 一次性验证（blockedBy T4,T5）
- T7（可选）`cc-capture` 包装 capture_proxy + README（blockedBy T6）
