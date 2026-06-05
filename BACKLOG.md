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

**(2026-06-05 升级方向, 用户确认)** 更根本: score 阈值本身就是被 feedback-no-tolerance-thresholds 禁止的密度阈值 —— `untranslated_prompt=0` 只卡 score>=5, weak-class(2-4) 不计入,掩盖了被划为 weak 的真 CC prompt 残留(2.1.163 weak 约 4700 条,粗估混着真 CC prompt + 大量第三方库 SDK 串 + CC 配置 schema `.describe()`)。**正解 = 废 score 阈值,改"扫全量英文 − 显式 whitelist(含第三方库语料 + `.describe()`/工具-description 上下文去噪) = 残留"**,见 weak-residual-exclusion-plan。下列 3a/3b 是退而求其次的评分改进,真正治本是换判据。

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

Claude Code 自动更新会替换 binary,补丁丢失,更新后需重跑 `install`。**(2026-06-05 实锤)**
2.1.162.zh 被自动更新到 2.1.163 覆盖, active 一度变回原版英文; 已补译 18 条新 prompt + 重
install 恢复 (active=2.1.163.zh, commit 291ffc5)。本次又印证: 每次新版本发布都要手动重跑
install (+ 可能补译新 prompt)。

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

## 11. parse-monitor 实现 / 文档对齐(两轮对抗评审剩余,均 minor)

诊断工具线。对抗评审(2026-06-05)对 `tools/parse_monitor/` 的发现。SILENT 静默逃逸检测
已修(2078c37);第三轮 review(验修复 commit)又发现 2 个 important —— SILENT 对 fenced
代码块假阳性 + SILENT 跨请求错并(真实数据复现)—— 已修(8c6d70b:fenced 守卫 + note_prompt
继承链,"零误报"诚实降级为 best-effort)。下列 minor 待后续(3g 的 SILENT 声明已随 8c6d70b
落地,余 probe/mtime 表述待 3c/3d):

- 3b:incident `first_line`/`last_line` 实为本批起始字节偏移而非行号(生产前 23 条为 0,
  `watcher.py:66` 传 `off` / `incident.py:60-61`)。改 `read_new_lines`/`scan_once` 传该记录
  精确字节偏移并字段改名 `byteOffset`(或维护真行号),补断言测试。
- 3c:历史事件(早于 `binary_state` 时间线起点)无 patch 归因(生产 57.5% 缺 `patched`);
  `DESIGN.md:61` 承诺的 mtime 回退未实现。实现 binary mtime 反推,或删该承诺改坦承不可归因。
- 3d:probe 判据论述错 —— 真二进制 patched 态 src/dst 共存(英文串在 Bun 元数据区有残留
  拷贝),"含源串→unpatched" 是伪命题(对外契约 `patched=n_dst>0` 仍正确、无功能 bug)。改
  docstring/DESIGN 判据只依赖 `n_dst`;探针从 `data/translations` 动态加载或加启动自检
  (现硬编码 3 条译串,译表改措辞会误判 patched→unpatched);real-env 校验脚本改用真 Bun
  二进制(现验的是解码后单段 cli.js,与运行行为不一致)。
- 3e:归并只用 promptId 继承,`DESIGN.md:34` 声称的 parentUuid 链未实现(事件交错时可错并,
  现实罕见)。实现链回溯,或 DESIGN 改"promptId 近似归并"标 known-limitation。
- 3f:SOFT/HARD marker 串在 `capture_proxy.py` / `parse_monitor/detect.py` / 测试里各硬编码
  一份,无单一来源。提到共享常量模块。
- 3g:DESIGN/README 对齐实现 —— 声明 SILENT 检测为 best-effort(仅已知损坏 opener 形态,
  可能漏报新形态),SOFT/HARD 为可靠零误报核心;并修正上述 probe/mtime/parentUuid 的表述。

## 12. 语言漂移文档统计严谨性修订(对抗评审发现)

研究/文档线。`docs/language-drift-analysis.md` 是面向公众的项目 Why 论证,drift-doc
reviewer(2026-06-05)发现的统计严谨性问题,公开发布前应修:

- **(important)** between-within 分解无法区分"任务阶段跳变"与"持续线性漂移"(模拟证实纯
  线性漂移也使两者都显著;`analyze_within_phase.py:154` between=段末−段首均值,等于 within
  斜率对段距的积分,无额外信息)。"三法排除任务阶段假说"被过度声称,实为"两法 + 一个无
  区分力的分解"。改措辞或用残差法重定义 between。剩余三条独立证据线仍支持核心结论。
- 约 26 个假设检验无多重比较校正(段内 2nd-segment p=0.0225 不过 Bonferroni 阈值 ~0.0019)。
  方法论节补说明哪些结论校正后仍成立(核心 mean_cjk/asc_wrun 全局检验 p<0.0001 稳健)。
- 长度分层表静默省略了不显著的 20-49 桶(N=12,67% 负斜率,p=0.19),破坏"随长度单调增强"
  叙事。补该行或注明省略原因。
- 数字过时(数据集 78→84):200+ 组文档写 100% 实为 90%;用户对照 p=0.20→0.084;段内 3rd
  p=0.042→0.134(已不显著);200+ Sign p=0.002→0.013。重跑脚本更新,"完全没随对话变化"等
  强表述降级。
- "四轮独立审查"由同模型家族(Opus 4.8)执行 = 循环依赖,"82% 置信度"是 AI 主观判断而非
  统计量。改"对抗性自审(Opus 4.8 作批评方)"+ 注明非独立第三方评审 / 非统计置信区间。
- "所有 p 值为单尾"与 Mann-Kendall 实际用双尾(`analyze_drift_significance.py:151`)矛盾。
  区分说明(Sign/Wilcoxon 单尾;MK 双尾,仅用于对话内趋势计数)。

## 13. 第三轮评审残留 minor(test-quality / 边界,非阻断,跨工具)

review 修复 commit 时发现,均不影响功能,补强可选:

- `detect_retry_in_request` 的 tool_result block 路径有代码无测试(主路径 plain string 已
  覆盖、binary 实证无漏报);补 `test_detect_retry_in_tool_result_block`。
- `extract_project_name` 对"项目目录名本身含 `-Projects-`"(如 `-Users-x-Projects-Research-Projects-X`)
  rsplit 锚点截短错(返回 X 而非 Research-Projects-X,罕见);补测试,需要时改捕获组。
- `test_capture_proxy_integration` 未断言 resp_sse_raw 落盘内容、502 路径"无 log";补断言。
- `test_silent_no_false_positive_on_discussion_backtick` 名称只覆盖行内反引号(fenced 已另测);
  可改名或合并。
- (第四轮) `note_prompt` 使 HARD 可能继承同 session 后续 user 的 promptId, 致 SOFT 与 HARD
  分裂为两个 incident。仅当 SOFT→HARD 之间夹入真实 user turn 才触发, 而 CC 的 SOFT→HARD 是同
  一次重试的连续注入、中间无 user turn, 故正常流程不触发(理论边界, 未来 CC 行为变更才需修)。
- (第四轮) SILENT 端到端测试 `test_scan_once_silent_not_misattributed_across_prompts` 未覆盖
  "fenced 闭合围栏之后的真逃逸"(detect 层 `test_silent_real_escape_after_fenced_block` 已覆盖);
  可在 scan_once 端到端补一条。

> 注: parse_monitor SILENT 经 5 轮 loop-until-dry 对抗评审收敛(2026-06-05): 第三轮发现漏抓
> 静默变体→实现; 第四轮发现 fenced 守卫(前一行启发式)FN→改全局区间追踪; 第五轮零 important
> 收敛(UTF-8/边界全过, 真环境 silent=14 不变)。修复 commit: 2078c37/8c6d70b/cdd2a40/91ef34e。

## 14. 2.1.163 译文第二轮 QC (轻 minor, l10n 线)

2.1.163 适配 (commit 291ffc5, active=2.1.163.zh, verify=0) 第一轮翻译 + 代码块逐字还原已上线;
按 cc-l10n 两轮 QC 传统, 第二轮散文复核待做:

- #12 (`# Package source shape` design-sync 子文档) 文档级 `##` 标题部分中文部分英文不一致
  (代码块内 bash 注释已随代码块还原修正)。统一: 全英文 (同 design-sync 章节标题保留, 防交叉
  引用断锚) 或全中文。不影响 verify=0 / 功能。
- 18 条散文逐条只读 QC: workflow 第二层验证已过 (14 ok + 4 minor 已修代码块/inline), 但术语
  一致性 / glossary、个别长文档 (Cowork 19K / Package source 32K) 的细节漂移可再核一轮。
