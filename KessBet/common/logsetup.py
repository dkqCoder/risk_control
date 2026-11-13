"""common/logsetup.py — 统一的日志配置。

## 为什么要有这个模块

原来三个进程各自 `logging.basicConfig()`，日志走 stderr，由启动脚本
重定向到 `logs/<name>.log`。两个问题：

  1. **重启就覆盖**：手动 `nohup ... > logs/collect.log` 用的是 `>`，
     会把上一次的日志清空。排查「昨天那次异常」时已经没有现场了。
  2. **不切分**：一个文件无限增长。采集进程一天几十 MB。

改成由 Python 直接写文件，用 `TimedRotatingFileHandler` 按天切：

    logs/collect.log            今天的
    logs/collect.log.2026-11-13 昨天的
    logs/collect.log.2026-11-12 前天的   （保留 30 天）

## 控制台 vs 文件

用 `stderr.isatty()` 判断：

  - 在终端里直接跑（`py -m collect.run`）→ 只输出到控制台，方便调试
  - 被启动脚本拉起 → 只写文件，避免和 shell 的重定向重复记录

所以 `deploy/start.sh` 里的重定向改成写 `logs/<name>.out`，
那个文件只会有「日志系统初始化之前」的异常（比如 import 失败），
正常运行时是空的。
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import sys

# 日志目录：<项目根>/logs
LOG_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'logs')

_FORMAT = '%(asctime)s [%(levelname)s] %(name)s: %(message)s'
_DATEFMT = '%H:%M:%S'

# 保留多少天。采集/分析都是排障导向，一个月足够回看。
DEFAULT_BACKUP_DAYS = 30


def daily_handler(filename: str, backupCount: int = DEFAULT_BACKUP_DAYS,
                  encoding: str = 'utf-8'):
    """给 dictConfig 用的工厂函数（uvicorn 的 --log-config）。

    为什么不直接在 JSON 里写 `"class": "logging.handlers.TimedRotatingFileHandler"`
    配 `"suffix": "%Y-%m-%d"`：`suffix` 是**属性**不是构造参数，
    dictConfig 会把所有键当 kwargs 传进去，直接
    `TypeError: unexpected keyword argument 'suffix'`。

    用 `"()"` 指定工厂就没这个问题 —— 顺手把 suffix 设成只到日，
    切出来的文件叫 `api.log.2026-11-13` 而不是默认的
    `api.log.2026-11-13_00-00-00`。
    """
    h = logging.handlers.TimedRotatingFileHandler(
        filename, when='midnight', backupCount=backupCount,
        encoding=encoding, delay=False)
    h.suffix = '%Y-%m-%d'
    return h


def setup(component: str, verbose: bool = False,
          backup_days: int = DEFAULT_BACKUP_DAYS) -> str | None:
    """配置日志。返回写入的文件路径（终端模式下返回 None）。

    component: 文件名主干，如 'collect' / 'analyze' / 'api'
    verbose:   DEBUG 级别
    """
    level = logging.DEBUG if verbose else logging.INFO
    root = logging.getLogger()
    root.setLevel(level)

    # 清掉已有的 handler，避免重复调用时日志打两遍
    for h in list(root.handlers):
        root.removeHandler(h)

    fmt = logging.Formatter(_FORMAT, datefmt=_DATEFMT)

    if sys.stderr.isatty():
        ch = logging.StreamHandler(sys.stderr)
        ch.setFormatter(fmt)
        root.addHandler(ch)
        return None

    os.makedirs(LOG_DIR, exist_ok=True)
    path = os.path.join(LOG_DIR, f'{component}.log')
    fh = logging.handlers.TimedRotatingFileHandler(
        path, when='midnight', backupCount=backup_days,
        encoding='utf-8', delay=False)
    # 切分后的文件名：collect.log.2026-11-13（默认会带时分秒，太啰嗦）
    fh.suffix = '%Y-%m-%d'
    fh.setFormatter(fmt)
    root.addHandler(fh)
    return path
