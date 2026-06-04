"""
capture_proxy / analyze_capture 的纯函数契约测试 (不触网).

覆盖:
  - parse_sse: end_turn 不报畸形; tool_use 合法 json 不报; 截断 json 报畸形;
               stop_reason=tool_use 但无 tool_use 块 报畸形
  - check_against_schema: 缺 required / 多余字段(禁 additionalProperties) / enum / 类型不符 / 合法
  - detect_retry_in_request: 含重试串 → 提取上一条 assistant
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

from capture_proxy import parse_sse, detect_retry_in_request, RETRY_MARKER  # noqa: E402
from analyze_capture import check_against_schema  # noqa: E402


def _sse(*events) -> bytes:
    out = []
    for ev, body in events:
        out.append(f"event: {ev}\n".encode())
        out.append(f"data: {json.dumps(body)}\n\n".encode())
    return b"".join(out)


def _msg_delta(stop_reason):
    return ("message_delta", {"type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None}})


# ---------- parse_sse ----------

def test_parse_sse_end_turn_not_malformed():
    raw = _sse(
        ("content_block_start", {"type": "content_block_start", "index": 0,
                                 "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                 "delta": {"type": "text_delta", "text": "hi"}}),
        _msg_delta("end_turn"),
    )
    r = parse_sse(raw)
    assert r["stop_reason"] == "end_turn"
    assert r["malformed"] is False


def test_parse_sse_tool_use_valid_not_malformed():
    raw = _sse(
        ("content_block_start", {"type": "content_block_start", "index": 0,
                                 "content_block": {"type": "tool_use", "id": "tu_1", "name": "Bash"}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                 "delta": {"type": "input_json_delta", "partial_json": '{"command":'}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                 "delta": {"type": "input_json_delta", "partial_json": '"ls"}'}}),
        _msg_delta("tool_use"),
    )
    r = parse_sse(raw)
    assert r["stop_reason"] == "tool_use"
    assert r["malformed"] is False
    assert r["tool_uses"][0]["name"] == "Bash"
    assert r["tool_uses"][0]["input"] == {"command": "ls"}


def test_parse_sse_tool_use_broken_json_malformed():
    raw = _sse(
        ("content_block_start", {"type": "content_block_start", "index": 0,
                                 "content_block": {"type": "tool_use", "id": "tu_1", "name": "Bash"}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                 "delta": {"type": "input_json_delta", "partial_json": '{"command":'}}),
        _msg_delta("tool_use"),
    )
    r = parse_sse(raw)
    assert r["malformed"] is True
    assert "JSON" in r["reason"]
    assert r["tool_uses"][0]["input_ok"] is False


def test_parse_sse_tool_use_no_block_malformed():
    raw = _sse(
        ("content_block_start", {"type": "content_block_start", "index": 0,
                                 "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                 "delta": {"type": "text_delta", "text": "我来调用工具"}}),
        _msg_delta("tool_use"),
    )
    r = parse_sse(raw)
    assert r["malformed"] is True
    assert "没有任何 tool_use" in r["reason"]


# ---------- check_against_schema ----------

_SCHEMA = {"type": "object",
           "properties": {"command": {"type": "string"},
                          "timeout": {"type": "integer"},
                          "mode": {"type": "string", "enum": ["a", "b"]}},
           "required": ["command"],
           "additionalProperties": False}


def test_schema_missing_required():
    issues = check_against_schema("Bash", {"timeout": 5}, _SCHEMA)
    assert any("缺 required" in i and "command" in i for i in issues)


def test_schema_extra_field_forbidden():
    issues = check_against_schema("Bash", {"command": "ls", "bogus": 1}, _SCHEMA)
    assert any("多余字段" in i and "bogus" in i for i in issues)


def test_schema_enum_violation():
    issues = check_against_schema("Bash", {"command": "ls", "mode": "z"}, _SCHEMA)
    assert any("enum" in i for i in issues)


def test_schema_type_mismatch():
    issues = check_against_schema("Bash", {"command": "ls", "timeout": "soon"}, _SCHEMA)
    assert any("类型应为 integer" in i for i in issues)


def test_schema_valid():
    assert check_against_schema("Bash", {"command": "ls", "timeout": 5, "mode": "a"}, _SCHEMA) == []


def test_schema_extra_field_allowed_when_additional_properties_true():
    # additionalProperties 未显式禁止 (默认 True) 时, 额外字段不是违例 (JSON Schema 语义)
    schema = {"type": "object", "properties": {"command": {"type": "string"}}}
    assert check_against_schema("Bash", {"command": "ls", "env": {"X": "1"}}, schema) == []


# ---------- detect_retry_in_request ----------

def test_detect_retry_extracts_prev_assistant():
    body = {"messages": [
        {"role": "user", "content": "do it"},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "tu_1", "name": "Bash", "input": {"timeout": 5}}]},
        {"role": "user", "content": RETRY_MARKER},
    ]}
    r = detect_retry_in_request(body)
    assert r is not None
    assert r["prev_assistant"]["content"][0]["name"] == "Bash"


def test_detect_retry_absent():
    body = {"messages": [{"role": "user", "content": "hello"}]}
    assert detect_retry_in_request(body) is None


def test_detect_retry_no_false_positive_on_user_quote():
    # 用户只是引用/讨论该串 (非 CC 注入的纯重试串) → 不应误报 (完全等值匹配)
    body = {"messages": [
        {"role": "user", "content": f"My tool failed with: '{RETRY_MARKER}'. How do I fix this?"},
    ]}
    assert detect_retry_in_request(body) is None


def test_detect_retry_ignores_assistant_mentioning_marker():
    # assistant 消息提到该串 (讨论) 不是重试信号
    body = {"messages": [
        {"role": "assistant", "content": f"I will avoid: {RETRY_MARKER}"},
    ]}
    assert detect_retry_in_request(body) is None


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
