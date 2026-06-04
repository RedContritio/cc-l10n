"""
cc-tool-parse-monitor 契约测试 (纯函数, 不触网/不发 iMessage/不读真二进制).

运行: .venv/bin/python test/test_parse_monitor.py   (standalone, 无需 pytest)
     或 pytest test/test_parse_monitor.py

分区:
  [detect]  classify_record HARD/SOFT 谓词 + 零误报
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

from parse_monitor.detect import classify_record  # noqa: E402
from parse_monitor.incident import build_incident_record, IncidentGrouper  # noqa: E402
from parse_monitor.binary_state import (  # noqa: E402
    probe_patch_state, BinaryStateLog, attribute, resolve_active_binary, sha256_bytes,
)
from parse_monitor.notify import notify, format_message  # noqa: E402
from parse_monitor.config import default_config, load_config  # noqa: E402
from parse_monitor.watcher import read_new_lines, scan_once  # noqa: E402

# 来自真实 transcript 的标记串 (ground truth, 测试里独立硬编码)
HARD = "The model's tool call could not be parsed (retry also failed)."
SOFT = "Your tool call was malformed and could not be parsed. Please retry."


def _soft_record(content=SOFT, type_="user", is_meta=True, session="s1",
                 prompt_id="P1", ts="2026-05-31T18:18:26.828Z", git_branch="main"):
    rec = {"type": type_, "message": {"role": "user", "content": content},
           "timestamp": ts, "sessionId": session, "cwd": "/p",
           "version": "2.1.158", "gitBranch": git_branch}
    if prompt_id is not None:
        rec["promptId"] = prompt_id
    if is_meta is not None:
        rec["isMeta"] = is_meta
    return rec


def _hard_record(text=HARD, model="<synthetic>", api_err=True, type_="assistant",
                 session="s1", ts="2026-05-31T18:31:08.753Z", git_branch="main"):
    rec = {"type": type_, "timestamp": ts, "sessionId": session,
           "cwd": "/p", "version": "2.1.158", "gitBranch": git_branch,
           "message": {"model": model, "role": "assistant", "stop_reason": "stop_sequence",
                       "type": "message", "content": [{"type": "text", "text": text}]}}
    if api_err is not None:
        rec["isApiErrorMessage"] = api_err
    return rec


def _silent_record(text='完成验证。\n\ncore\n<invoke name="Bash">\n<parameter name="command">ls</parameter>\n</invoke>',
                   stop="end_turn", with_tool_use=False, session="s1",
                   ts="2026-06-01T07:04:00.000Z"):
    content = [{"type": "text", "text": text}]
    if with_tool_use:
        content.append({"type": "tool_use", "id": "tu1", "name": "Bash", "input": {}})
    return {"type": "assistant", "timestamp": ts, "sessionId": session, "cwd": "/p",
            "version": "2.1.159", "gitBranch": "main",
            "message": {"role": "assistant", "model": "claude-opus-4-8", "type": "message",
                        "stop_reason": stop, "content": content}}


# ---------- [detect] 正例 ----------

def test_classify_hard_positive():
    r = classify_record(_hard_record())
    assert r is not None and r["severity"] == "hard"


def test_classify_soft_positive():
    r = classify_record(_soft_record())
    assert r is not None and r["severity"] == "soft"


# ---------- [detect] 零误报: "讨论/引用该串" 的记录 ----------

def test_discussion_user_long_text_not_marker():
    # 用户在对话里引用了该串, 嵌在长句中 (非完全等值, 且无 isMeta)
    rec = _soft_record(content=f"为什么会报 {SOFT} 这个错?", is_meta=None)
    assert classify_record(rec) is None


def test_assistant_quoting_marker_real_model():
    # 助手正文里引用 HARD 串, 但是真模型、无 isApiErrorMessage
    rec = _hard_record(model="claude-opus-4-8", api_err=None)
    assert classify_record(rec) is None


def test_soft_content_is_block_list_not_string():
    # 正常 user turn: content 是 block 列表而非裸字符串
    rec = _soft_record(content=[{"type": "text", "text": SOFT}])
    assert classify_record(rec) is None


def test_hard_real_model_rejected():
    # isApiErrorMessage 为真但 model 不是 <synthetic> → 非 CC 注入的合成错误
    rec = _hard_record(model="claude-sonnet-4-6")
    assert classify_record(rec) is None


def test_soft_missing_is_meta_rejected():
    # 用户原样粘贴该串作为整条消息, 但无 isMeta → 不算事件
    rec = _soft_record(is_meta=None)
    assert classify_record(rec) is None


def test_soft_wrong_type_rejected():
    rec = _soft_record(type_="assistant")
    assert classify_record(rec) is None


# ---------- [detect] 静默文本化逃逸 (best-effort, 仅已知损坏 opener 形态) ----------

def test_classify_silent_escape_positive():
    # 损坏 function_calls opener (core) 单独成行紧跟 invoke, 无 tool_use 块, stop=end_turn
    r = classify_record(_silent_record())
    assert r is not None and r["severity"] == "silent"


def test_silent_escape_other_broken_openers():
    for op in ("care", "court", "course"):
        rec = _silent_record(text=f'内容\n\n{op}\n<invoke name="Read">x</invoke>')
        assert classify_record(rec)["severity"] == "silent", op


def test_silent_not_flagged_when_tool_use_present():
    # 有 tool_use 块 = 正常工具调用 (即便附带文本含 invoke), 非逃逸
    assert classify_record(_silent_record(with_tool_use=True)) is None


def test_silent_not_flagged_when_stop_is_tool_use():
    # stop=tool_use 走 SOFT/HARD retry 路径, 不归 silent (避免重复计数)
    assert classify_record(_silent_record(stop="tool_use")) is None


def test_silent_no_false_positive_on_discussion_backtick():
    # 讨论该 bug: invoke 被反引号 markdown 引用 (非损坏 opener 裸词行) → 零误报
    rec = _silent_record(text="正确谓词应是 assistant text 块含 `<invoke name=` 且无 tool_use 块")
    assert classify_record(rec) is None


def test_silent_no_false_positive_on_inline_mention():
    # 句中提及 invoke (无独立的损坏 opener 行) → 零误报
    rec = _silent_record(text="模型把 <invoke name=Bash> 当成文本吐出来了")
    assert classify_record(rec) is None


def test_silent_no_false_positive_on_fenced_code_block():
    # 把逃逸原文贴进 fenced 代码块 (讨论该 bug 最常见方式, 含本评审对话) → 不误报
    rec = _silent_record(text='逃逸案例:\n```\ncore\n<invoke name="Bash">x</invoke>\n```\n注意此模式。')
    assert classify_record(rec) is None


def test_silent_no_false_positive_on_fenced_with_lang_tag():
    # 带语言标签的 fenced opener (```python) 同样防护
    rec = _silent_record(text='```python\ncore\n<invoke name="Bash">x</invoke>\n```')
    assert classify_record(rec) is None


def test_silent_real_escape_after_fenced_block():
    # 真逃逸紧跟在 fenced 代码块【闭合】之后 (opener 在块外) → 应识别为 silent (防 FN)
    rec = _silent_record(text='讨论:\n```\nsome code\n```\ncore\n<invoke name="Bash">x</invoke>')
    r = classify_record(rec)
    assert r is not None and r["severity"] == "silent"


def test_silent_no_false_positive_on_fenced_with_blank_line():
    # fenced 块内容与围栏间有空行 (合法 markdown), opener 仍在块内 → 不误报
    rec = _silent_record(text='```\n\ncore\n<invoke name="X">x</invoke>\n```')
    assert classify_record(rec) is None


def test_fenced_spans_never_inverted():
    # 末尾无换行的孤立围栏不产生倒置区间 (s>e); 零行为变化, 仅消除畸形内部状态
    from parse_monitor.detect import _fenced_spans
    for t in ("", "```", "X\n```", "\n```", "a\n```\nb\n```"):
        for s, e in _fenced_spans(t):
            assert s <= e, (t, s, e)


# ---------- [detect] 健壮性 ----------

def test_empty_record_returns_none():
    assert classify_record({}) is None


def test_missing_message_returns_none():
    assert classify_record({"type": "assistant", "isApiErrorMessage": True}) is None


# ---------- [incident] 单记录提取 ----------

def test_build_incident_record_fields():
    rec = _soft_record(session="sess-9", prompt_id="P7", git_branch="dev",
                       ts="2026-05-31T18:18:26.828Z")
    cls = classify_record(rec)
    inc = build_incident_record(rec, cls, "/x/t.jsonl", 42)
    assert inc["sessionId"] == "sess-9"
    assert inc["project"] == "/p"
    assert inc["gitBranch"] == "dev"
    assert inc["version"] == "2.1.158"
    assert inc["severity"] == "soft"
    assert inc["promptId"] == "P7"
    assert inc["transcriptPath"] == "/x/t.jsonl"
    assert inc["lineNo"] == 42
    assert inc["ts"] == "2026-05-31T18:18:26.828Z"


# ---------- [incident] 归并 / 去重 ----------

def _add(g, rec, path="/t.jsonl", line=1):
    return g.add(rec, classify_record(rec), path, line)


def test_grouper_silent_incident():
    g = IncidentGrouper()
    r = _add(g, _silent_record())
    assert r["is_new"] is True
    assert r["incident"]["severity"] == "silent"
    assert r["incident"]["silent_count"] == 1


def test_grouper_silent_then_hard_escalates():
    g = IncidentGrouper()
    _add(g, _silent_record(), line=1)
    r = _add(g, _hard_record(), line=2)   # 同 session, 同归并 key
    inc = r["incident"]
    assert r["escalated"] is True
    assert inc["severity"] == "hard"
    assert inc["silent_count"] == 1 and inc["hard_count"] == 1


def test_grouper_hard_then_silent_no_downgrade():
    g = IncidentGrouper()
    _add(g, _hard_record(), line=1)
    r = _add(g, _silent_record(), line=2)
    inc = r["incident"]
    assert inc["severity"] == "hard"        # silent 不降级已有 hard
    assert r["escalated"] is False
    assert inc["silent_count"] == 1 and inc["hard_count"] == 1


def test_format_message_includes_silent_count():
    inc = {"severity": "silent", "project": "/p/proj", "version": "2.1.159",
           "silent_count": 2, "soft_count": 0, "hard_count": 0, "first_ts": "T"}
    msg = format_message(inc)
    assert "silent×2" in msg and "parse silent" in msg


def test_grouper_note_prompt_prevents_silent_misattribution():
    # 真实 bug (codeRail 复现): scan_once 跳过普通 user 记录使 _last_prompt 不更新,
    # SILENT (无 promptId) 错继承旧 SOFT 的 promptId 并错并入其 incident。note_prompt
    # 让普通 user 记录的真实 promptId 更新继承链, SILENT 归正确请求。
    g = IncidentGrouper()
    _add(g, _soft_record(prompt_id="P1"))        # SOFT incident key (s1, P1)
    g.note_prompt("s1", "P2")                     # scan_once 对普通 user(P2) 记录会调
    r = _add(g, _silent_record())                 # SILENT 无 promptId
    assert r["is_new"] is True                    # 独立 incident, 不错并入 SOFT
    assert r["incident"]["promptId"] == "P2"      # 继承 P2 而非陈旧 P1
    assert r["incident"]["silent_count"] == 1


def test_grouper_soft_then_hard_one_incident():
    g = IncidentGrouper()
    r1 = _add(g, _soft_record(), line=10)
    assert r1["is_new"] is True and r1["incident"]["severity"] == "soft"
    r2 = _add(g, _hard_record(), line=12)  # 同 session, HARD 继承 P1
    assert r2["is_new"] is False           # 不是新事件
    assert r2["escalated"] is True
    inc = r2["incident"]
    assert inc["severity"] == "hard"
    assert inc["soft_count"] == 1 and inc["hard_count"] == 1


def test_grouper_different_prompt_two_incidents():
    g = IncidentGrouper()
    a = _add(g, _soft_record(prompt_id="P1"))
    b = _add(g, _soft_record(prompt_id="P2"))
    assert a["is_new"] is True and b["is_new"] is True
    assert len(g.incidents()) == 2


def test_grouper_lone_soft():
    g = IncidentGrouper()
    r = _add(g, _soft_record())
    assert r["is_new"] is True and r["incident"]["severity"] == "soft"
    assert r["escalated"] is False


def test_grouper_lone_hard_defensive():
    g = IncidentGrouper()
    r = _add(g, _hard_record(session="solo"))
    assert r["is_new"] is True and r["incident"]["severity"] == "hard"


def test_grouper_counts_accumulate():
    g = IncidentGrouper()
    _add(g, _soft_record(ts="t1"))
    _add(g, _soft_record(ts="t2"))   # 同 prompt P1 的二次重试
    r = _add(g, _hard_record(ts="t3"))
    inc = r["incident"]
    assert inc["soft_count"] == 2 and inc["hard_count"] == 1
    assert inc["severity"] == "hard"
    assert len(g.incidents()) == 1   # 全归一条


def test_grouper_separate_sessions():
    g = IncidentGrouper()
    _add(g, _soft_record(session="A"))
    _add(g, _hard_record(session="B"))
    assert len(g.incidents()) == 2   # 不同 session 不合并


# ---------- [binary_state] patch 指纹探测 ----------

import os  # noqa: E402
import tempfile  # noqa: E402

# 测试用合成探针 (英文源 → 中文译, 不依赖真译表)
_PROBES = [
    (b"Executes a bash command and returns its output.",
     "执行一条 bash 命令并返回其输出。".encode("utf-8")),
    (b"Performs exact string replacement in a file.",
     "在文件中执行精确的字符串替换。".encode("utf-8")),
]


def test_probe_patched_binary():
    blob = b"....prefix...." + _PROBES[0][1] + b"...mid..." + _PROBES[1][1] + b"...tail"
    r = probe_patch_state(blob, _PROBES)
    assert r["patched"] is True
    assert r["n_dst"] == 2 and r["determinate"] is True


def test_probe_unpatched_binary():
    blob = b"xx" + _PROBES[0][0] + b"yy" + _PROBES[1][0] + b"zz"
    r = probe_patch_state(blob, _PROBES)
    assert r["patched"] is False
    assert r["n_src"] == 2 and r["n_dst"] == 0 and r["determinate"] is True


def test_probe_indeterminate_when_neither():
    r = probe_patch_state(b"totally unrelated bytes", _PROBES)
    assert r["patched"] is False
    assert r["determinate"] is False  # 既无源也无译 → 无法判定


def test_sha256_bytes_stable():
    assert sha256_bytes(b"abc") == sha256_bytes(b"abc")
    assert sha256_bytes(b"abc") != sha256_bytes(b"abd")
    assert len(sha256_bytes(b"abc")) == 64


# ---------- [binary_state] 时间线日志 ----------

def test_binary_state_log_dedup():
    with tempfile.TemporaryDirectory() as d:
        log = BinaryStateLog(os.path.join(d, "bin.jsonl"))
        s1 = {"sha256": "aaa", "version": "2.1.158", "patched": False}
        assert log.record(s1, "2026-06-01T00:00:00Z") is True
        assert log.record(s1, "2026-06-01T00:05:00Z") is False  # 同 sha 不重复
        s2 = {"sha256": "bbb", "version": "2.1.158", "patched": True}
        assert log.record(s2, "2026-06-01T00:10:00Z") is True
        assert len(log.entries()) == 2


# ---------- [binary_state] 按时间戳归因 ----------

def _timeline():
    return [
        {"ts": "2026-05-31T17:34:00Z", "sha256": "orig", "patched": False, "version": "2.1.158"},
        {"ts": "2026-05-31T18:38:00Z", "sha256": "zh", "patched": True, "version": "2.1.158"},
    ]


def test_attribute_between_entries():
    st = attribute("2026-05-31T18:31:08Z", _timeline())  # stock HARD 事件
    assert st is not None and st["patched"] is False      # 落在未 patch 窗口


def test_attribute_after_patch():
    st = attribute("2026-05-31T19:00:00Z", _timeline())
    assert st["patched"] is True


def test_attribute_before_any():
    assert attribute("2026-05-31T10:00:00Z", _timeline()) is None


# ---------- [binary_state] 符号链接解析 ----------

def test_resolve_active_binary_follows_symlink():
    with tempfile.TemporaryDirectory() as d:
        real = os.path.join(d, "2.1.158")
        Path(real).write_bytes(b"BINARY")
        link = os.path.join(d, "claude")
        os.symlink(real, link)
        # 两边都 resolve: macOS tempdir 在 /var(软链到 /private/var), 需规范化后比
        assert resolve_active_binary(link) == Path(real).resolve()


# ---------- [notify] 渠道分发 ----------

import json as _json  # noqa: E402

_INC = {"severity": "hard", "project": "/Users/x/Projects/stock", "sessionId": "sess-1",
        "version": "2.1.158", "patched": False, "soft_count": 1, "hard_count": 1,
        "first_ts": "2026-05-31T18:31:08Z"}


class _FakeRunner:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def __call__(self, args):
        self.calls.append(args)
        if self.fail:
            raise RuntimeError("osascript boom")


def test_format_message_has_key_fields():
    m = format_message(_INC)
    assert "hard" in m and "stock" in m and "2.1.158" in m
    assert "unpatched" in m or "patched" in m   # patch 状态必须出现


def test_digest_appends_jsonl():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "incidents.jsonl")
        cfg = {"digest": {"enabled": True, "path": p}}
        notify(_INC, cfg, runner=_FakeRunner())
        notify(_INC, cfg, runner=_FakeRunner())
        lines = [l for l in Path(p).read_text().splitlines() if l.strip()]
        assert len(lines) == 2
        assert _json.loads(lines[0])["severity"] == "hard"


def test_imessage_uses_runner_with_handle_and_text():
    r = _FakeRunner()
    cfg = {"digest": {"enabled": False},
           "imessage": {"enabled": True, "handle": "+1999"}}
    res = notify(_INC, cfg, runner=r)
    assert res["imessage"] is True
    flat = " ".join(a for call in r.calls for a in call)
    assert "+1999" in flat and "osascript" in flat


def test_macos_uses_runner():
    r = _FakeRunner()
    cfg = {"digest": {"enabled": False}, "macos": {"enabled": True}}
    res = notify(_INC, cfg, runner=r)
    assert res["macos"] is True and len(r.calls) == 1


def test_channel_failure_isolated():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "incidents.jsonl")
        r = _FakeRunner(fail=True)
        cfg = {"digest": {"enabled": True, "path": p},
               "imessage": {"enabled": True, "handle": "h"}}
        res = notify(_INC, cfg, runner=r)   # imessage 抛错不应影响 digest/不应崩
        assert res["imessage"] is False
        assert res["digest"] is True
        assert len([l for l in Path(p).read_text().splitlines() if l.strip()]) == 1


def test_disabled_channels_skipped():
    r = _FakeRunner()
    cfg = {"digest": {"enabled": False},
           "imessage": {"enabled": False}, "macos": {"enabled": False}}
    res = notify(_INC, cfg, runner=r)
    assert r.calls == []
    assert res.get("imessage", False) is False and res.get("macos", False) is False


# ---------- [config] ----------

def test_default_config_shape():
    c = default_config()
    assert c["digest"]["enabled"] is True
    assert "projects_root" in c and "poll_interval" in c
    assert c["imessage"]["enabled"] is False


def test_load_config_merges_over_defaults():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "config.json")
        Path(p).write_text(_json.dumps({"imessage": {"enabled": True, "handle": "+1"}}))
        c = load_config(p)
        assert c["imessage"]["enabled"] is True and c["imessage"]["handle"] == "+1"
        assert c["digest"]["enabled"] is True   # 默认仍在


# ---------- [watcher] 增量读 / 偏移 / 续读 ----------

def test_read_new_lines_complete():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "t.jsonl")
        Path(p).write_bytes(b"a\nb\n")
        lines, off = read_new_lines(p, 0)
        assert lines == ["a", "b"] and off == 4


def test_read_new_lines_holds_partial():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "t.jsonl")
        Path(p).write_bytes(b"a\nb\n")
        _, off = read_new_lines(p, 0)
        Path(p).write_bytes(b"a\nb\npartial")        # 追加无换行的半行
        lines, off2 = read_new_lines(p, off)
        assert lines == [] and off2 == off           # 半行被保留, 偏移不动
        Path(p).write_bytes(b"a\nb\npartial\n")       # 补上换行
        lines, off3 = read_new_lines(p, off)
        assert lines == ["partial"] and off3 == 12


def test_read_new_lines_truncation_resets():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "t.jsonl")
        Path(p).write_bytes(b"x\n")
        lines, off = read_new_lines(p, 999)           # offset 超过文件 → 重置
        assert lines == ["x"] and off == 2


# ---------- [watcher] scan_once 端到端 ----------

def _write_jsonl(path, records):
    with open(path, "w") as f:
        for r in records:
            f.write(_json.dumps(r) + "\n")


def test_scan_once_detects_and_resumes():
    with tempfile.TemporaryDirectory() as d:
        proj = os.path.join(d, "projects", "-proj")
        os.makedirs(proj)
        jl = os.path.join(proj, "sess.jsonl")
        _write_jsonl(jl, [{"type": "user", "message": {"content": "hi"}}, _soft_record()])
        g = IncidentGrouper()
        seen = []
        offsets = {}
        scan_once(os.path.join(d, "projects"), offsets, g, seen.append)
        assert len(seen) == 1 and seen[0]["is_new"] is True
        # 追加 HARD 行, 再扫: 只处理新行, 不重复旧行
        with open(jl, "a") as f:
            f.write(_json.dumps(_hard_record()) + "\n")
        scan_once(os.path.join(d, "projects"), offsets, g, seen.append)
        assert len(seen) == 2 and seen[1]["escalated"] is True
        assert len(g.incidents()) == 1


def test_scan_once_enriches_patch_state():
    with tempfile.TemporaryDirectory() as d:
        proj = os.path.join(d, "projects")
        os.makedirs(proj)
        _write_jsonl(os.path.join(proj, "s.jsonl"), [_soft_record()])
        g = IncidentGrouper()
        seen = []
        scan_once(proj, {}, g, seen.append,
                  binary_provider=lambda ts: {"patched": False, "sha256": "orig"})
        inc = seen[0]["incident"]
        assert inc["patched"] is False and inc["binary_sha256"] == "orig"


def test_scan_once_silent_not_misattributed_across_prompts():
    # 真实 codeRail bug 复现: SOFT(P1) → 普通 user(P2) → SILENT(无 promptId)。
    # scan_once 须用普通 user 的 P2 更新继承链, 使 SILENT 独立成 incident 而非错并入 SOFT。
    with tempfile.TemporaryDirectory() as d:
        proj = os.path.join(d, "projects")
        os.makedirs(proj)
        _write_jsonl(os.path.join(proj, "s.jsonl"), [
            _soft_record(prompt_id="P1"),
            {"type": "user", "promptId": "P2", "sessionId": "s1",
             "message": {"role": "user", "content": "继续"}},
            _silent_record(),
        ])
        g = IncidentGrouper()
        scan_once(proj, {}, g, lambda res: None)
        incs = g.incidents()
        assert len(incs) == 2                       # SOFT(P1) 与 SILENT(P2) 各自独立
        soft_inc = next(i for i in incs if i["promptId"] == "P1")
        silent_inc = next(i for i in incs if i["promptId"] == "P2")
        assert soft_inc["silent_count"] == 0        # SOFT incident 未被 SILENT 污染
        assert silent_inc["severity"] == "silent" and silent_inc["silent_count"] == 1


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        fn()
        passed += 1
        print(f"  ok  {fn.__name__}")
    print(f"\n[OK] {passed}/{len(fns)} 通过")


if __name__ == "__main__":
    _run_all()
