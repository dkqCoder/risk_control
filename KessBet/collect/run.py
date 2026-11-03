"""collect/run.py — 采集进程主入口。

    py -m collect.run                  # 正常运行
    py -m collect.run --once           # 只跑一轮就退出（调试用）
    py -m collect.run --no-ws          # 不连 WebSocket，纯定时刷新（降级模式）

流程：
    启动
     ├─ 拉 7 个运动赛程 → kessbet_match
     ├─ bootstrap：拉 24 小时内全部比赛赔率
     └─ WebSocket 连接，订阅全部未开赛比赛

    运行中
     ├─ bet_change → 标记脏 → 每 5 秒刷 HTTP 全量
     ├─ 每小时重新拉赛程（发现新比赛、剔除已结束）
     ├─ 每 6 小时清理过期数据
     └─ WS 断开 → 重连 → 重新订阅 → 重新 bootstrap
"""
import argparse
import logging
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from collect import config
from collect.kessbet.adapter import KessbetAdapter
from collect.refresher import Refresher
from common.timeutil import now_beijing
from db.dao import Dao

log = logging.getLogger(__name__)


class Collector:
    def __init__(self, use_ws: bool = True):
        self.dao = Dao()
        self.adapter = KessbetAdapter()
        self.refresher = Refresher(self.adapter, self.dao)
        self.use_ws = use_ws
        self.ws = None
        self._running = False

        self._last_schedule = 0.0
        self._last_cleanup = 0.0
        self.stats = {'schedule_refresh': 0, 'cleanup': 0}

    # ---------- 赛程 ----------

    def refresh_schedule(self) -> dict:
        """拉全部运动的赛程并落库。"""
        t0 = time.time()
        now = now_beijing()
        by_sport = {}
        total = 0

        for sport in config.SPORTS:
            try:
                matches = self.adapter.list_matches(sport)
                n = self.dao.upsert_matches(matches)
                # 同步跨平台映射。失败不影响采集——这张表只服务页面和跨平台比价，
                # 单平台分析走 (platform, match_id)，不依赖它。
                try:
                    self.dao.sync_canonical(matches)
                except Exception as e:
                    log.warning('sync_canonical %s failed: %s', sport, e)
                upcoming = sum(1 for m in matches if m.kickoff_time > now)
                by_sport[sport] = (n, upcoming)
                total += n
            except Exception as e:
                log.error('list_matches %s failed: %s', sport, e)
                by_sport[sport] = (0, 0)

        elapsed = time.time() - t0
        log.info('schedule: %d matches in %.1fs  %s', total, elapsed,
                 ' '.join(f'{k.split(":")[-1]}={v[0]}/{v[1]}'
                          for k, v in by_sport.items()))
        self.dao.log_event('schedule', now,
                           matches=total, error=None)
        self._last_schedule = time.monotonic()
        self.stats['schedule_refresh'] += 1
        return by_sport

    def upcoming_ids(self, hours: int = 24) -> list[str]:
        return [r[0] for r in self.dao.upcoming(hours)]

    # ---------- 订阅 ----------

    def sync_subscription(self) -> None:
        """把订阅集合同步成"当前未开赛的比赛"，并补拉新比赛的初始赔率。

        只订阅是不够的——推送只在赔率变动时触发。新进入窗口的比赛
        如果赔率不动（冷门联赛、手球等盘口少的运动），会一直没有数据。
        实测：14 场新比赛进窗口 25 分钟后仍无赔率，而接口明明有数据。
        """
        if not self.ws:
            return
        ids = self.upcoming_ids(24)
        self.ws.subscribe(ids)
        # 本地剔除已结束的
        removed = self.ws.prune(set(ids))
        if removed:
            log.info('pruned %d ended matches from subscription', removed)

        # 补拉还没有赔率的比赛
        self.fetch_missing_odds(ids)

    def fetch_missing_odds(self, ids: list[str] | None = None) -> int:
        """给没有赔率数据的比赛补一次基线。

        每小时赛程刷新后调用，保证新进入窗口的比赛有初始数据。
        """
        hours = 24
        with self.dao.conn.cursor() as c:
            c.execute("""
                SELECT m.match_id
                FROM kessbet_match m
                WHERE m.platform='kessbet'
                  AND m.kickoff_time BETWEEN NOW() AND NOW() + INTERVAL %s HOUR
                  AND NOT EXISTS (
                      SELECT 1 FROM kessbet_odds_prematch o
                      WHERE o.platform=m.platform AND o.match_id=m.match_id)
                ORDER BY m.kickoff_time
            """, (hours,))
            missing = [r[0] for r in c.fetchall()]

        if not missing:
            return 0

        log.info('fetching odds for %d matches without data', len(missing))
        t0 = time.time()
        total = 0
        with ThreadPoolExecutor(self.refresher.concurrency,
                                thread_name_prefix='backfill') as pool:
            for n in pool.map(self.refresher.refresh_one, missing):
                total += n
        log.info('backfilled %d rows for %d matches in %.1fs',
                 total, len(missing), time.time() - t0)
        return total

    # ---------- 主循环 ----------

    def start(self, once: bool = False) -> int:
        self._running = True

        log.info('=== phase 1: schedule ===')
        self.refresh_schedule()

        log.info('=== phase 2: bootstrap ===')
        t0 = time.time()
        total = self.refresher.bootstrap(hours=24)
        log.info('bootstrap: %d rows in %.1fs', total, time.time() - t0)

        log.info('=== phase 3: websocket ===')
        if self.use_ws:
            self._start_ws()
        else:
            log.warning('websocket disabled (--no-ws)')

        log.info('=== phase 4: loops ===')
        self.refresher.start()

        if once:
            log.info('--once given, exiting after one cycle')
            self.stop()
            return 0

        return self._main_loop()

    def _start_ws(self) -> None:
        try:
            self.adapter.on_push(self.refresher.mark_dirty)
            self.ws = self.adapter.start_ws(initial_ids=self.upcoming_ids(24))
            log.info('ws subscribed to %d matches', self.ws.subscribed_count)
        except Exception as e:
            log.error('ws start failed: %s (will retry in loop)', e)
            self.ws = None

    def _main_loop(self) -> None:
        exit_code = 0
        try:
            while self._running:
                time.sleep(30)

                # 刷新线程报告致命错误 -> 整个进程退出，交给外部守护重启
                if self.refresher.fatal.is_set():
                    log.critical('刷新线程已停止（%s），进程退出',
                                 self.refresher.fatal_error)
                    exit_code = 1
                    break

                # WS 掉线则重连
                if self.use_ws and (self.ws is None or not self._ws_alive()):
                    log.warning('ws not alive, reconnecting...')
                    self._start_ws()
                    if self.ws:
                        self.refresher.bootstrap(hours=24)

                # 每小时刷新赛程
                if time.monotonic() - self._last_schedule > 3600:
                    try:
                        self.refresh_schedule()
                        self.sync_subscription()
                    except Exception as e:
                        log.exception('schedule refresh failed: %s', e)

                # 每 6 小时清理
                if time.monotonic() - self._last_cleanup > 6 * 3600:
                    try:
                        r = self.dao.cleanup()
                        log.info('cleanup: %s', r)
                        self._last_cleanup = time.monotonic()
                        self.stats['cleanup'] += 1
                    except Exception as e:
                        log.exception('cleanup failed: %s', e)

        except KeyboardInterrupt:
            log.info('interrupted')
        finally:
            self.stop()
        return exit_code

    def _ws_alive(self) -> bool:
        if not self.ws:
            return False
        thread = getattr(self.ws, '_recv_thread', None)
        return bool(getattr(self.ws, '_running', False) and thread and thread.is_alive())

    def stop(self) -> None:
        self._running = False
        log.info('stopping...')
        try:
            self.refresher.stop()
        except Exception:
            pass
        if self.ws:
            try:
                self.ws.stop()
            except Exception:
                pass

    def report(self) -> None:
        log.info('=== stats ===')
        log.info('refresher: %s', self.refresher.stats)
        if self.ws:
            log.info('ws: %s', self.ws.stats)
        log.info('collector: %s', self.stats)
        with self.dao.conn.cursor() as c:
            c.execute("SELECT COUNT(*) FROM kessbet_match")
            log.info('kessbet_match: %d rows', c.fetchone()[0])
            c.execute("SELECT COUNT(*) FROM kessbet_odds_prematch")
            log.info('kessbet_odds_prematch: %d rows', c.fetchone()[0])
            c.execute("SELECT COUNT(*) FROM kessbet_odds_history")
            log.info('kessbet_odds_history: %d rows', c.fetchone()[0])


def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
        datefmt='%H:%M:%S',
    )


def main():
    ap = argparse.ArgumentParser(description='KessBet 采集进程')
    ap.add_argument('--once', action='store_true', help='跑一轮就退出')
    ap.add_argument('--no-ws', action='store_true', help='不连 WebSocket')
    ap.add_argument('-v', '--verbose', action='store_true')
    args = ap.parse_args()

    setup_logging(args.verbose)

    c = Collector(use_ws=not args.no_ws)
    code = 0
    try:
        code = c.start(once=args.once)
    finally:
        try:
            c.report()
        except Exception as e:
            log.error('report failed: %s', e)
        c.dao.close()
    sys.exit(code or 0)


if __name__ == '__main__':
    main()
