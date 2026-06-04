# parse-monitor

全局、常驻、**旁路**监控 Claude Code 偶发的
`The model's tool call could not be parsed (retry also failed).`,并为每条事件**自带**
patched/unpatched 归因。只读 transcript、出 API 路径之外,**绝不可能拖垮 CC**。

设计与诊断背景见 [DESIGN.md](DESIGN.md)。

## 它怎么工作

轮询 `~/.claude/projects/**/*.jsonl` 的增量行 → 按精确结构谓词识别 CC 注入的
SOFT(重试)/ HARD(最终失败)标记(对"对话里引用该串"零误报)→ 按用户请求归并去重 →
探测当前 active 二进制的 patch 状态(扫已知中文译串)并按事件时间戳归因 → 告警。

告警渠道(按优先级,单渠道失败互不影响):
1. **digest**(始终开,事实源):`~/.claude/parse-monitor/incidents.jsonl`
2. **iMessage self-chat**(主推送,后补):osascript → Messages
3. **macOS 通知**(兜底,默认关)

## 安装 / 卸载

```sh
python3 tools/parse_monitor/install.py        # 装为 launchd LaunchAgent 并启动 (digest-only)
python3 tools/parse_monitor/install.py --dry-run   # 只看将写入的 plist
python3 tools/parse_monitor/uninstall.py      # 停止并删除
python3 tools/parse_monitor/uninstall.py --purge   # 同时删运行时数据
```

## 运行时数据(全局,**含对话片段,敏感,不入 git**)

`~/.claude/parse-monitor/`:
- `config.json` — 配置(digest/imessage/macos 开关、handle、poll 间隔、探针)
- `incidents.jsonl` — 事件 digest(事实源)
- `binary_state.jsonl` — active 二进制状态时间线(ts/version/sha256/patched)
- `state.json` — 每文件字节偏移(重启续读、不重复告警)
- `watcher.log` — 守护进程日志

## 查看事件

```sh
python3 tools/analyze_capture.py            # (深抓时) 分析 capture_proxy 落盘
cat ~/.claude/parse-monitor/incidents.jsonl  # 事件流水
cat ~/.claude/parse-monitor/binary_state.jsonl  # 二进制状态时间线
```

## 测试

```sh
.venv/bin/python test/test_parse_monitor.py   # 纯函数契约测试 (不触网/不发 iMessage)
```

## 后补:iMessage 推送

Darwin 25.x 上 daemon 经 AppleScript 发 iMessage 可靠性待验证。启用步骤(后续):
在 `config.json` 设 `imessage.enabled=true` + `imessage.handle="<自身手机号/Apple ID 邮箱>"`,
并在「系统设置→隐私与安全性→自动化」授权该进程控制 Messages,跑一次性试发确认到达。
