"""
cc-l10n 统一 logging 配置.

所有 production 脚本顶部:
    from log_setup import setup_logging
    log = setup_logging(__name__)

约定:
- 全部输出走 stderr (CLI 工具 stdout 留给未来 dump 类输出)
- format 仅 message (与原 print 等效, 视觉一致); level 通过 ASCII tag
  嵌入 message: [OK] / [WARN] / [FAIL] / [INFO]
- log.info     = 进度 / summary
- log.warning  = 风险 / 非致命异常
- log.error    = strict fail / 致命错误

basicConfig idempotent: 同一进程多次 import 仅首次生效.
子进程 (cc_l10n.py 调度) 各自重新配置, 不冲突.
"""
from __future__ import annotations

import logging
import sys


def setup_logging(name: str | None = None) -> logging.Logger:
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    return logging.getLogger(name)
