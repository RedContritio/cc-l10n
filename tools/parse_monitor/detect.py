"""
探测 transcript jsonl 记录是否为 CC 注入的 "tool call could not be parsed" 事件标记.

精确谓词 (结构 + 完全等值), 对 "对话里引用该串" 的记录零误报:
  HARD: type=="assistant" ∧ isApiErrorMessage is True ∧ message.model=="<synthetic>"
        ∧ 任一 content text 块完全等于 HARD_MARKER
  SOFT: type=="user" ∧ isMeta is True ∧ message.content (纯字符串) 完全等于 SOFT_MARKER
  SILENT: 工具调用文本化逃逸 (assistant, stop!=tool_use, 无 tool_use 块, text 含损坏
          opener 裸词行 + <invoke)。**best-effort, 非零误报** (见 _is_silent_escape_text):
          已防 fenced 代码块, 但散文同形仍会假阳性, 且漏报非 [a-z]{2,12} 形态。
"""
from __future__ import annotations

import re

HARD_MARKER = "The model's tool call could not be parsed (retry also failed)."
SOFT_MARKER = "Your tool call was malformed and could not be parsed. Please retry."

SYNTHETIC_MODEL = "<synthetic>"

# 损坏的 <function_calls> opener 单独成行 (core/care/court/course/... 是 function_calls
# 被 tokenizer 切坏的碎片) 紧跟逃逸的 <invoke name=。
_SILENT_ESCAPE_RE = re.compile(r"\n[a-z]{2,12}\n<invoke name=")


def _is_silent_escape_text(text: str) -> bool:
    """text 是否含"损坏 opener 裸词行 + <invoke"的文本化逃逸模式。

    best-effort, **不是零误报**:
    - 已防 fenced 代码块: opener 词前一行若是 ``` (含 ```lang) 则视为"贴逃逸原文",
      非真逃逸 —— 这是讨论该 bug 时最常见的误报来源 (含本类评审对话)。
    - 仍会假阳性: 散文里"普通文本行 + 损坏 opener 词单独成行 + <invoke" 与真逃逸字节
      同形, 无法区分 (罕见, 主要见于讨论该 bug 的散文)。
    - 漏报: opener 非 [a-z]{2,12} 形态 (含数字/大写/超长) 不匹配。
    """
    for m in _SILENT_ESCAPE_RE.finditer(text):
        last_line = text[:m.start()].rsplit("\n", 1)[-1]
        if last_line.lstrip().startswith("```"):
            continue  # opener 词在 fenced 代码块内 → 贴原文, 跳过
        return True
    return False


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
    # CC 仅在 stop_reason==tool_use 时注入 SOFT/HARD, 故 stop!=tool_use 的逃逸被 retry
    # 路径漏抓。三重约束 (损坏 opener 行 + 无 tool_use 块 + stop!=tool_use) + fenced 守卫;
    # best-effort 非零误报 (散文同形残留, 见 _is_silent_escape_text)。
    if rec.get("type") == "assistant":
        content = msg.get("content")
        if isinstance(content, list):
            has_tool_use = any(isinstance(b, dict) and b.get("type") == "tool_use" for b in content)
            if not has_tool_use and msg.get("stop_reason") != "tool_use":
                for block in content:
                    if (isinstance(block, dict) and block.get("type") == "text"
                            and _is_silent_escape_text(block.get("text") or "")):
                        return {"severity": "silent", "marker": "<invoke name= (textualized escape)"}

    return None
