"""analyze 系列脚本共享的纯函数."""
from __future__ import annotations

import re
from pathlib import Path


def extract_project_name(path) -> str:
    """从 CC 对话路径或目录名提取项目短名.

    CC 把项目路径编码为目录名 -Users-<user>-Projects-<proj> (或 -home-<user>-...).
    用户名本身可能含连字符 (john-doe), 故以 '-Projects-' 为锚做 rsplit, 而非贪婪
    匹配用户名 (后者会把用户名尾段误并入项目名)。非 Projects 路径退回为去掉
    -Users-/-home-<user>- 前缀后的 '~<rest>', 与旧行为一致。
    """
    s = str(path)
    name = None
    for part in s.split("/"):
        if part.startswith("-Users-") or part.startswith("-home-"):
            name = part
            break
    if name is None:
        name = Path(s).name
    if "-Projects-" in name:
        return name.rsplit("-Projects-", 1)[1]
    m = re.match(r"^-(?:Users|home)-[^-]+-(.*)$", name)
    if m:
        return "~" + m.group(1)
    return name
