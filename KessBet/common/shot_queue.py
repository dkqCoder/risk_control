"""common/shot_queue.py — 串行截图 worker（D03）。

为什么要有这个模块：
  原来每次命中都起一个 daemon 线程，各自 `platform_shot.capture_match`。
  而 `capture_match` 早期固定 9333 端口和同一个 Chrome profile —— 两个
  命中同时发生时，A 线程可能连到 B 线程打开的页面，把 B 的页面存成
  A 的文件名。

MVP 方案：
  - 全局只有一个 worker、一个队列，所有调用方共用调度；
  - 任务串行执行，每个任务内部自己起/关 Chrome，互不共享端口/profile；
  - 队列有界，满了明确丢弃并记日志，不无限堆积。

任务字段：
  snapshot_id, match_id, local_url, local_path, official_path
"""
from __future__ import annotations

import logging
import queue
import threading
import time

log = logging.getLogger(__name__)

_MAX_TASKS = 20
_tasks: queue.Queue = queue.Queue(maxsize=_MAX_TASKS)
_worker: threading.Thread | None = None
_worker_lock = threading.Lock()
_dropped = 0


def submit(task: dict) -> bool:
    """投递一个截图任务。返回 False 表示队列已满。"""
    global _worker, _dropped
    with _worker_lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(
                target=_loop, daemon=True, name='evidence-shot')
            _worker.start()
    try:
        _tasks.put_nowait(task)
        return True
    except queue.Full:
        _dropped += 1
        log.error('截图队列已满（%d），丢弃任务 %s',
                  _MAX_TASKS, task.get('snapshot_id'))
        return False


def pending() -> int:
    return _tasks.qsize()


def dropped() -> int:
    return _dropped


def _loop() -> None:
    while True:
        task = _tasks.get()
        try:
            _run(task)
        except Exception as e:
            log.exception('截图任务异常 %s: %s',
                          task.get('snapshot_id'), e)
        finally:
            _tasks.task_done()


def _run(task: dict) -> None:
    from common import screenshot, platform_shot

    sid = task.get('snapshot_id')
    mid = task.get('match_id')
    started = time.time()

    # 1) 本地冻结证据页 —— 主证据，先截，快且不依赖官网。
    try:
        ok, msg = screenshot.capture(
            task['local_url'], task['local_path'])
        log.warning('本地证据截图%s %s（命中后 %.1fs）',
                    '已保存' if ok else '失败',
                    msg if ok else msg, time.time() - started)
    except Exception as e:
        log.warning('本地证据截图异常 %s: %s', sid, e)

    # 2) 平台官网 —— 辅助证据，天然滞后。
    try:
        r = platform_shot.capture_match_meta(
            mid, task['official_path'])
        log.warning(
            '平台截图 ok=%s tab=%s coverage=%s page_verified=%s '
            'reason=%s path=%s（命中后 %.1fs，非命中时刻）',
            r.get('ok'), r.get('tab'), r.get('coverage'),
            r.get('page_verified'), r.get('reason'), r.get('path'),
            time.time() - started)
    except Exception as e:
        log.warning('平台截图异常 %s: %s', mid, e)
