"""
把已分类的标记记录提取为 incident, 并按 (sessionId, promptId) 归并去重.

归并规则: 一次用户请求 (promptId) 内的 SOFT 重试 + 升级 HARD 算同一个 incident,
severity 取最重 (hard > soft), soft_count/hard_count 累加。HARD 记录本身不带
promptId, 继承同 session 最近见到的 promptId。不同 session 不合并。
"""
from __future__ import annotations


def build_incident_record(rec, classification, transcript_path, line_no) -> dict:
    """单条标记记录 → 扁平 incident 字段 (不做归并)."""
    return {
        "ts": rec.get("timestamp"),
        "sessionId": rec.get("sessionId"),
        "project": rec.get("cwd"),
        "gitBranch": rec.get("gitBranch"),
        "version": rec.get("version"),
        "severity": classification["severity"],
        "marker": classification.get("marker"),
        "promptId": rec.get("promptId"),
        "transcriptPath": transcript_path,
        "lineNo": line_no,
    }


class IncidentGrouper:
    """流式喂入分类记录, 归并为 incident。add() 报告 is_new / escalated。"""

    def __init__(self):
        self._incidents: dict = {}      # key -> incident
        self._order: list = []          # key 创建顺序
        self._last_prompt: dict = {}    # session -> 最近 promptId

    def add(self, rec, classification, transcript_path, line_no) -> dict:
        session = rec.get("sessionId")
        prompt_id = rec.get("promptId")
        if prompt_id is not None:
            self._last_prompt[session] = prompt_id
        else:
            prompt_id = self._last_prompt.get(session)
        key = (session, prompt_id)
        severity = classification["severity"]

        is_new = key not in self._incidents
        if is_new:
            inc = {
                "key": list(key),
                "sessionId": session,
                "promptId": prompt_id,
                "project": rec.get("cwd"),
                "gitBranch": rec.get("gitBranch"),
                "version": rec.get("version"),
                "severity": severity,
                "soft_count": 0,
                "hard_count": 0,
                "first_ts": rec.get("timestamp"),
                "last_ts": rec.get("timestamp"),
                "transcriptPath": transcript_path,
                "first_line": line_no,
                "last_line": line_no,
            }
            self._incidents[key] = inc
            self._order.append(key)
        else:
            inc = self._incidents[key]
            inc["last_ts"] = rec.get("timestamp")
            inc["last_line"] = line_no

        escalated = False
        if severity == "soft":
            inc["soft_count"] += 1
        else:
            inc["hard_count"] += 1
            if inc["severity"] != "hard":
                inc["severity"] = "hard"
                escalated = True

        return {"incident": inc, "is_new": is_new, "escalated": escalated}

    def incidents(self) -> list:
        return [self._incidents[k] for k in self._order]
