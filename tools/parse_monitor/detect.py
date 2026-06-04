"""
探测 transcript jsonl 记录是否为 CC 注入的 "tool call could not be parsed" 事件标记.

精确谓词 (结构 + 完全等值), 对 "对话里引用该串" 的记录零误报:
  HARD: type=="assistant" ∧ isApiErrorMessage is True ∧ message.model=="<synthetic>"
        ∧ 任一 content text 块完全等于 HARD_MARKER
  SOFT: type=="user" ∧ isMeta is True ∧ message.content (纯字符串) 完全等于 SOFT_MARKER
"""
from __future__ import annotations

HARD_MARKER = "The model's tool call could not be parsed (retry also failed)."
SOFT_MARKER = "Your tool call was malformed and could not be parsed. Please retry."

SYNTHETIC_MODEL = "<synthetic>"


def classify_record(rec) -> dict | None:
    """返回 None 或 {"severity": "hard"|"soft", "marker": <text>}."""
    if not isinstance(rec, dict):
        return None
    msg = rec.get("message")
    if not isinstance(msg, dict):
        return None

    # SOFT: CC 注入的重试请求 (isMeta user 消息, content 是裸标记串)
    if rec.get("type") == "user" and rec.get("isMeta") is True:
        if msg.get("content") == SOFT_MARKER:
            return {"severity": "soft", "marker": SOFT_MARKER}

    # HARD: CC 注入的合成错误消息 (重试仍失败)
    if (rec.get("type") == "assistant"
            and rec.get("isApiErrorMessage") is True
            and msg.get("model") == SYNTHETIC_MODEL):
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if (isinstance(block, dict)
                        and block.get("type") == "text"
                        and block.get("text") == HARD_MARKER):
                    return {"severity": "hard", "marker": HARD_MARKER}

    return None
