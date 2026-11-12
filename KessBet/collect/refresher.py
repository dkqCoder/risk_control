"""collect/refresher.py — 脏队列 → HTTP 全量刷新。

这是「推送触发，HTTP 取数」架构的核心。

推送到达时只做两件事：记下 match_id、记下时刻。真正的数据来自
后续的 HTTP 全量接口。这样：

- 不需要解析推送的 update/insert/delete
- 不需要处理"推送可能不完整"
- 不需要软删除标记（全量替换天然清理撤盘）
- 一次 HTTP 返回天然是原子快照，不需要 round_id
"""
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from collect import config
from collect.writer import replace_match
from common.timeutil import now_beijing

log = logging.getLogger(__name__)


class FatalCollectError(RuntimeError):
    """采集连续失败到不可恢复，需要重启进程。"""


class Refresher:
    # 连续失败达到该值 -> 主动重建 DB 连接（可能只是连接池坏了）
    failure_reset_threshold = 20
    # 连续失败达到该值 -> 抛异常退出。僵死不退出的进程比崩溃更危险。
    max_consecutive_failures = 100

    def __init__(self, adapter, dao, concurrency: int | None = None,
                 debounce: float | None = None):
        self.adapter = adapter
        self.dao = dao
        self.concurrency = concurrency or config.REFRESH_CONCURRENCY
        self.debounce = debounce if debounce is not None else config.REFRESH_DEBOUNCE_SEC

        self._dirty: dict[str, float] = {}
        self._lock = threading.Lock()

        # WS delete 信号：match_id -> 收到信号时的 monotonic 时刻。
        #
        # 比例保护（REFRESH_MIN_RATIO）防的是「接口残缺返回」，但整场撤盘
        # 时接口返回 0 也会被它挡住，几百个已失效选项就永久留在库里。
        # WS 推送的 delete 字段是平台撤盘的直接证据，收到后就放行一次
        # 覆盖 —— 放行只对下一次刷新有效，避免残缺响应被永久放行。
        self._removed_at: dict[str, float] = {}
        self._removed_lock = threading.Lock()
        # 信号有效期。撤盘后通常会很快触发刷新，留 10 分钟足够，
        # 太久会让一个陈旧的信号影响后面的无关刷新。
        self.remove_signal_ttl = 600.0

        self._running = False
        # 连续失败计数（成功一次清零）。用锁保护，因为刷新是并发的。
        self._fail_lock = threading.Lock()
        self._consecutive_failures = 0
        # 致命错误：run() 捕获后置位，让主循环知道该退出了
        self.fatal = threading.Event()
        self.fatal_error: str | None = None

        self.stats = {
            'marked': 0,
            'refreshed': 0,
            'skipped_kickoff': 0,
            'skipped_suspicious': 0,
            'failed': 0,
            'rows': 0,
            'changed': 0,        # 写入历史表的行数
            'removed_signals': 0,   # 收到的 WS delete 信号数
            'bypassed_ratio': 0,    # 因 delete 信号而绕过比例保护的次数
            'consecutive_failures': 0,
            'db_resets': 0,
        }

    # ---------- 自愈 ----------

    def _on_failure(self, why: str) -> None:
        """记录一次失败，必要时重置连接或退出。"""
        with self._fail_lock:
            self._consecutive_failures += 1
            n = self._consecutive_failures
            self.stats['consecutive_failures'] = n

        if n == self.failure_reset_threshold:
            log.warning('连续失败 %d 次，重置数据库连接', n)
            try:
                reset = getattr(self.dao, 'reset_connections', None)
                if reset:
                    reset()
                    self.stats['db_resets'] += 1
            except Exception as e:
                log.error('reset_connections 失败: %s', e)

        if n >= self.max_consecutive_failures:
            self.fatal_error = '连续失败 %d 次（%s），采集无法恢复，需要重启进程' % (n, why)
            self.fatal.set()
            raise FatalCollectError(self.fatal_error)

    def _on_success(self) -> None:
        with self._fail_lock:
            if self._consecutive_failures:
                log.info('从 %d 次连续失败中恢复', self._consecutive_failures)
            self._consecutive_failures = 0
            self.stats['consecutive_failures'] = 0

    # ---------- 脏标记 ----------

    def mark_dirty(self, match_id: str) -> None:
        """WS 回调入口。同一场多次推送只需刷新一次。"""
        with self._lock:
            self._dirty[match_id] = time.monotonic()
            self.stats['marked'] += 1

    def mark_removed(self, match_id: str) -> None:
        """WS 推送 delete 时调用：授权该场下一次刷新绕过比例保护。

        与 mark_dirty 分开是刻意的 —— delete 帧不一定同时触发赔率变动，
        但它是「平台确实撤了盘口」的独立证据。
        """
        with self._removed_lock:
            self._removed_at[match_id] = time.monotonic()
            self.stats['removed_signals'] += 1

    def _take_removed(self, match_id: str) -> bool:
        """取走该场的 delete 许可（一次性 + 有时效）。"""
        with self._removed_lock:
            t = self._removed_at.pop(match_id, None)
        return t is not None and (time.monotonic() - t) <= self.remove_signal_ttl

    def _take_ready(self, now: float) -> list[str]:
        """取出已过防抖期的比赛。"""
        with self._lock:
            ready = [m for m, t in self._dirty.items() if now - t >= self.debounce]
            for m in ready:
                del self._dirty[m]
        return ready

    @property
    def pending(self) -> int:
        with self._lock:
            return len(self._dirty)

    # ---------- 刷新单场 ----------

    def _clear_if_removed(self, match_id, kickoff, now, sport, home, away):
        """接口返回空且有 WS delete 信号时，清空该场的库内数据。

        返回删除的行数；没有信号或无需清理时返回 None（调用方按跳过处理）。

        为什么必须清理：整场撤盘时接口返回 0 项，比例保护会拒绝覆盖，
        几百个已失效选项永久留在 kessbet_odds_prematch。analyze 读到的
        就是死数据，可能算出假套利。
        """
        if not self._take_removed(match_id):
            return None

        try:
            db_keys = self.dao.fetch_keys(match_id)
        except Exception as e:
            log.warning('fetch_keys %s failed: %s', match_id, e)
            self.stats['failed'] += 1
            self._on_failure(str(e))
            return None
        if not db_keys:
            return None

        # 先记撤盘日志（含最后赔率），再删 —— 删完就查不到赔率了
        try:
            current = {(m, s, l, lab): o
                       for m, s, l, lab, o in self.dao.get_selections(match_id)}
            self.dao.log_removed(
                match_id,
                [(k[0], k[1], k[2], k[3], current[k])
                 for k in db_keys if k in current],
                removed_time=now, source='ws',
            )
        except Exception as e:
            log.warning('log_removed %s failed: %s', match_id, e)

        try:
            self.dao.delete_keys(match_id, db_keys)
            self.dao.conn.commit()
        except Exception as e:
            log.warning('clear %s failed: %s', match_id, e)
            try:
                self.dao.conn.rollback()
            except Exception:
                pass
            self.stats['failed'] += 1
            self._on_failure(str(e))
            return None

        self._on_success()
        self.stats['removed_signals_used'] = \
            self.stats.get('removed_signals_used', 0) + 1
        log.info('cleared %s: 平台整场撤盘，删除 %d 个失效选项',
                 match_id, len(db_keys))
        return 0

    def refresh_one(self, match_id: str) -> int:
        """拉 HTTP 全量并整场替换。返回写入行数（0 表示跳过）。"""
        try:
            row = self.dao.get_match(match_id)
        except Exception as e:
            log.warning('get_match %s failed: %s', match_id, e)
            self.stats['failed'] += 1
            self._on_failure(str(e))
            return 0
        if not row:
            log.debug('skip %s: not in kessbet_match', match_id)
            return 0
        sport, home, away, kickoff = row

        now = now_beijing()
        if kickoff <= now:
            self.stats['skipped_kickoff'] += 1
            return 0

        try:
            selections = self.adapter.fetch_odds(match_id, home, away)
        except Exception as e:
            log.warning('fetch_odds %s failed: %s', match_id, e)
            self.stats['failed'] += 1
            self._on_failure(str(e))
            return 0

        # 空响应：整场撤盘时接口就是这个样子。
        # 如果收到过 WS delete 信号，说明平台确实撤了，直接清空库内数据；
        # 否则仍按原来的「跳过」处理（可能是接口异常）。
        if not selections:
            rows = self._clear_if_removed(match_id, kickoff, now, sport, home, away)
            if rows is None:
                log.debug('skip %s: empty response', match_id)
                return 0
            return rows

        # 比例保护：接口返回远少于库内，判定为残缺响应，不覆盖。
        # 但 WS delete 信号是平台撤盘的直接证据，此时放行一次。
        try:
            db_count = self.dao.count_odds(match_id)
        except Exception as e:
            log.warning('count_odds %s failed: %s', match_id, e)
            self.stats['failed'] += 1
            self._on_failure(str(e))
            return 0
        if db_count > 0 and len(selections) < db_count * config.REFRESH_MIN_RATIO:
            if self._take_removed(match_id):
                self.stats['bypassed_ratio'] += 1
                log.info('removed-signal %s: api=%d db=%d, 平台撤盘，允许覆盖',
                         match_id, len(selections), db_count)
            else:
                log.warning('suspicious %s: api=%d db=%d, skip overwrite',
                            match_id, len(selections), db_count)
                self.stats['skipped_suspicious'] += 1
                return 0
        # 比例正常时不消费信号：信号是基于时间的一次性许可（ttl 到期自动失效），
        # 在这里消费掉会让紧跟其后的整场撤盘（下一次返回 0）失去放行机会。

        # 构造 Match 对象供 writer 使用
        from common.models import Match
        match = Match(
            platform='kessbet',
            match_id=match_id,
            sport=sport,
            league='',
            country='',
            home_team=home,
            away_team=away,
            kickoff_time=kickoff,
        )

        try:
            result = replace_match(self.dao, match, selections, now)
        except Exception as e:
            log.exception('write %s failed: %s', match_id, e)
            self.stats['failed'] += 1
            self._on_failure(str(e))
            return 0

        self._on_success()
        self.stats['refreshed'] += 1
        self.stats['rows'] += result['written']
        self.stats['changed'] += result['changed']
        return result['written']

    # ---------- 主循环 ----------

    def run(self) -> None:
        """阻塞运行，直到 stop() 或命中致命错误。

        致命错误不往上抛——这个函数跑在守护线程里，抛出去只会打印栈，
        主线程照旧空转，等于又回到「僵死」状态。改成置位 self.fatal，
        由主循环检测后决定退出。
        """
        self._running = True
        pool = ThreadPoolExecutor(self.concurrency, thread_name_prefix='refresh')
        try:
            while self._running:
                ready = self._take_ready(time.monotonic())
                if ready:
                    try:
                        list(pool.map(self.refresh_one, ready))
                    except FatalCollectError as e:
                        log.critical('刷新线程致命错误，停止采集: %s', e)
                        break
                time.sleep(1)
        finally:
            self._running = False
            # 不等待：worker 可能卡在失败的 socket 上，等下去就是二次僵死
            pool.shutdown(wait=False, cancel_futures=True)

    def start(self) -> threading.Thread:
        t = threading.Thread(target=self.run, daemon=True, name='refresher')
        t.start()
        return t

    def stop(self) -> None:
        self._running = False

    # ---------- 启动基线 ----------

    def bootstrap(self, hours: int | None = None) -> int:
        """启动时给所有未开赛比赛建基线。"""
        hours = hours or 24
        rows = self.dao.upcoming(hours)
        if not rows:
            log.warning('bootstrap: no upcoming matches in %dh', hours)
            return 0

        ids = [r[0] for r in rows]
        log.info('bootstrap: %d matches within %dh', len(ids), hours)

        total = 0
        with ThreadPoolExecutor(self.concurrency,
                                thread_name_prefix='bootstrap') as pool:
            for n in pool.map(self.refresh_one, ids):
                total += n
        log.info('bootstrap done: %d rows', total)
        return total
