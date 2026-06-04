"""
探测 transcript jsonl 记录是否为 CC 注入的 "tool call could not be parsed" 事件标记.

精确谓词 (结构 + 完全等值), 对 "对话里引用该串" 的记录零误报:
  HARD: type=="assistant" ∧ isApiErrorMessage is True ∧ message.model=="<synthetic>"
        ∧ 任一 content text 块完全等于 HARD_MARKER
  SOFT: type=="user" ∧ isMeta is True ∧ message.content (纯字符串) 完全等于 SOFT_MARKER
"""
from __future__ import annotations

import re

HARD_MARKER = "The model's tool call could not be parsed (retry also failed)."
SOFT_MARKER = "Your tool call was malformed and could not be parsed. Please retry."

SYNTHETIC_MODEL = "<synthetic>"

# 损坏的 <function_calls> opener 单独成行 (core/care/court/course/... 是 function_calls
# 被 tokenizer 切坏的碎片) 紧跟逃逸的 <invoke name=。best-effort: 仅匹配此已知形态,
# 故对反引号引用 / 句中提及该标签零误报, 但可能漏报未来新的损坏形态 (见 DESIGN 局限)。
_SILENT_ESCAPE_RE = re.compile(r"\n[a-z]{2,12}\n<invoke name=")


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

    # SILENT: 工具调用文本化逃逸 (模型把 <function_calls> 吐成文本, opener 损坏成裸词).
    # CC 仅在 stop_reason==tool_use 时注入 SOFT/HARD, 故 stop!=tool_use 的逃逸 retry
    # 路径漏抓。结构谓词 (损坏 opener 行 + 无 tool_use 块 + stop!=tool_use) 保零误报。
    if rec.get("type") == "assistant":
        content = msg.get("content")
        if isinstance(content, list):
            has_tool_use = any(isinstance(b, dict) and b.get("type") == "tool_use" for b in content)
            if not has_tool_use and msg.get("stop_reason") != "tool_use":
                for block in content:
                    if (isinstance(block, dict) and block.get("type") == "text"
                            and _SILENT_ESCAPE_RE.search(block.get("text") or "")):
                        return {"severity": "silent", "marker": "<invoke name= (textualized escape)"}

    return None
