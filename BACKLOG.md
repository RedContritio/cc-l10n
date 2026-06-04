# Backlog

已知未完成 / 已推迟的工作项。按优先级与依赖大致排序。

## 1. 工具 / agent 描述覆盖不全(中英混合)

binary 内的工具与 agent 描述是带 `${}` 插值的模板字面量,且由多个变量在运行时拼装。
静态检测器对每个片段单独评分并按阈值取舍,导致大量描述片段落在阈值以下、保持英文,
最终描述呈中英混合。

- 受影响:`Agent` / `Bash` / `Edit` / `Write` / `Read` / `Workflow` / `ToolSearch` /
  `AskUserQuestion` / `ScheduleWakeup` / `Skill` 工具描述;`general-purpose` / `Plan` /
  `claude-code-guide` agent 描述。
- 现状:27 个非空片段中 23 个未翻。
- 做法:补译未翻片段(保留 `${}` 插值位置,使重组后语序通顺;含代码 / 类型签名的片段
  保留英文)。译文进 `data/translations` 公共目录(工具描述跨平台一致)。

## 2. 检测:capture 模式应扫描完整请求(含 `tools[]`)

`verify_coverage` 的 CAPTURE 模式当前只扫 `body["system"]`,忽略 `body["tools"][*].description`
与 input_schema 里的 description —— 正是第 1 项漏掉的那批串。

- 做法:扩展 capture 解析器,token 级扫描 `tools[].description` 与 input_schema;
  补一个驱动 binary 发出携带完整常驻工具集请求的 harness。
- 这是 ground-truth 门:度量"真正发给模型的英文",不受字面量在 binary 里如何
  存储 / 切碎 / 拼装影响。

## 3. 检测:静态评分改为结构感知

静态评分依赖手维护的动词表 + 内容相似度阈值,二者都有盲区(动词表漏词;散文被插值切碎)。

- 3a(便宜、通用):评分前先重组模板片段(片段 + `${}` 占位),整体判为 prompt 则
  要求其全部片段必翻。
- 3b(较重、可选):按角色识别描述(注册位 `description:` 字段的值),无视评分强制要求。

## 4. 聚合 no-stale:确定译文集支持的版本范围

`integration_test` 的聚合 no-stale 目前仅信息提示(`--check-stale` 默认关)。译文集是
跨版本并集,"相对抽样集 stale" ≠ 真死译文。npm `stable` = 2.1.145、`latest` = 2.1.156,
两者都是合法支持版。

- 需定:译文集官方支持哪些版本 → 决定 no-stale 失败判据,以及是否清理版本专属变体 /
  重复条目。
- 定后在 CI 开启 `--check-stale`。

## 5. 自动更新会覆盖补丁

Claude Code 自动更新会替换 binary,补丁丢失,更新后需重跑 `install`。

- 可选改进:版本变更时自动重应用(post-update 钩子或包装器)。

## 6. CI 广度

CI 当前以 dist-tags(latest + stable)× 三平台为快速门;多版本 / `--all` 覆盖走手动
(`workflow_dispatch`)。

- 第 4 项定范围后,可加定期(如每周)更广的运行。

## 7. (已完成 2026-06-04) 2.1.162 design-sync 补译 + 入 supported

4 段(技能 / 工具 / 31.6K 包文档 / tone)全译,workflow 多 agent + 3 视角对抗验证。2.1.162
入 `supported_versions` → {2.1.152, 2.1.161, 2.1.162},`--supported` 3 版 × 3 平台全绿 verify=0。

## 8. 2 个版本脆 code_patch 暂停

`code_patches.py` 用 `EnterPlanMode` / `Write` 的 FN=0 模板 frag patch(锚 post-translation
字节),CC 升级后 minified 变量名变 → 失配 raise。commit 32bda78 已暂停用。待 `build_fn0_code_patches.py`
按当前版本自动重算 src 再启用(supported 配置可辅助按版本对锚)。

## 9. risk D 修复扩展:line 30 Plan mode 长 prompt 截短(跨版本)

2.1.162 native install 撞 audit risk D 已修(commit 707e8fe,截短 `15_skill_docs.json` line 269):
`37_v162_weak_residual.json` 的 `\n\n## Plan File Info:\n` 是 Plan mode 长 prompt 尾部
`\n\n## Plan File Info:` 的子串,apply 时 hit 范围重叠。**line 30**(`...received.` 结尾变体,
2.1.162 为 0-hit 故未改)同样以 `## Plan File Info:` 结尾,在 152/161 若命中会撞同一 risk D。

- 做法:用 152/161 真 binary 验证后,同样截短 line 30 src/dst 去尾部章节标题,交由短条目统一译。
- 依赖:需 152/161 真环境验证(用户铁律:正确性测试跑真环境,不盲改)。

## 10. CI 加 apply / audit 门(堵 hit-overlap 盲区)

risk D 是 `install` 的 apply 阶段 audit 才检出的 hit 范围重叠,但 CI 的 supported 门只跑
STATIC `verify`(检 `untranslated_prompt`),**不跑 apply 的 hit-overlap audit** —— 故 ff15ff3
引入的 risk D 让 supported verify=0 全绿却仍 install 失败(假绿)。

- 做法:`--supported` 门对每个 version × platform 增跑一次 `apply`(或独立 audit strict),
  使 risk A–E 高危在 PR 阶段即暴露,而非到 install 才发现。
- 关联第 6 项(CI 广度)。
