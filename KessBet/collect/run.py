"""collect/run.py — 采集进程主入口。

    py -m collect.run                  # 正常运行
    py -m collect.run --once           # 只跑一轮就退出（调试用）
    py -m collect.run --no-ws          # 不连 WebSocket，纯定时刷新（降级模式）

流程：
    启动
     ├─ 拉 7 个运动赛程 → kessbet_match
     ├─ bootstrap：拉 HORIZON_HOURS（默认 72）小时内全部比赛赔率
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
        self.live = None          # 滚球采集器，在 start() 里初始化
        self._running = False

        self._last_schedule = 0.0
        self._last_live_schedule = 0.0
        self._last_cleanup = 0.0
        self.stats = {'schedule_refresh': 0, 'cleanup': 0,
                      'live_schedule_added': 0}

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

    def refresh_live_schedule(self) -> int:
        """拉**滚球列表**并补进 kessbet_match。返回新增场次。

        ## 为什么必须单独做这一步

        赛程接口（`00252`）只返回未开赛的比赛 —— 实测 123 场 ms 全是 0、
        开赛时间已过的 0 场。所以如果一场比赛在赛前任何一次刷新时都没
        被拉到，开赛后它就永远不会出现在我们库里。

        实测缺口（2026-11-12）：网页 Live 页上的 `Club America vs Toluca`
        在我们库里完全查不到 —— 它从没进过 kessbet_match，
        于是 collected 里没有它，分析里没有它，页面上也没有它。

        `00232` 是网页 Live 页用的接口，直接给滚球比赛。每次刷赛程时
        也拉一遍，把「赛前漏掉但正在踢」的比赛补进来。

        返回新增的场次数（upsert_matches 的返回值包含已存在的，
        所以这里单独比一次）。
        """
        if not config.LIVE_ENABLED:
            return 0

        known = self.dao.known_match_ids()
        added = 0
        new_ids = []
        for sport in config.LIVE_SPORTS:
            try:
                matches = self.adapter.list_live_matches(sport)
            except Exception as e:
                log.error('list_live_matches %s failed: %s', sport, e)
                continue

            fresh = [m for m in matches if m.match_id not in known]
            if not fresh:
                continue
            try:
                self.dao.upsert_matches(fresh)
                try:
                    self.dao.sync_canonical(fresh)
                except Exception as e:
                    log.warning('sync_canonical(live) %s failed: %s', sport, e)
                added += len(fresh)
                new_ids.extend(m.match_id for m in fresh)
                log.info('live schedule: +%d matches for %s  (%s)', len(fresh),
                         sport,
                         ', '.join(f'{m.home_team[:16]} vs {m.away_team[:16]}'
                                   for m in fresh[:3]))
            except Exception as e:
                log.error('upsert live matches %s failed: %s', sport, e)

        if added:
            self.stats['live_schedule_added'] = (
                self.stats.get('live_schedule_added', 0) + added)

        # ★ 新发现的滚球比赛要立即触发一次赔率+状态采集。
        #   否则只能等 WS 推送，而推送只在赔率变化时触发，
        #   比赛进来时可能赔率正停在当前值不动，就永远没数据。
        if new_ids:
            log.info('triggering live collection for %d new matches', len(new_ids))
            if getattr(self, 'live', None):
                for mid in new_ids:
                    self.live.mark_dirty(mid)

        return added

    def upcoming_ids(self, hours: int | None = None) -> list[str]:
        return [r[0] for r in self.dao.upcoming(hours or config.HORIZON_HOURS)]

    def live_ids(self) -> list[str]:
        """当前处于滚球窗口内的比赛。

        实测确认平台会推送滚球赔率（2026-11-11 验证：3 次推送/180 秒，
        全部含滚球盘口）。所以要把这些比赛也加进订阅集，
        否则收不到推送、只能轮询。
        """
        if not config.LIVE_ENABLED:
            return []
        return [r['match_id'] for r in self.dao.live_matches(
            config.LIVE_WINDOW_MIN)]

    # ---------- 订阅 ----------

    def sync_subscription(self) -> None:
        """把订阅集合同步成「未开赛 ∪ 进行中」，并补拉新比赛的初始赔率。

        只订阅是不够的——推送只在赔率变动时触发。新进入窗口的比赛
        如果赔率不动（冷门联赛、手球等盘口少的运动），会一直没有数据。
        实测：14 场新比赛进窗口 25 分钟后仍无赔率，而接口明明有数据。

        滚球比赛（已开赛）在 2026-11-11 之前是不订阅的 —— 因为那时
        假设「开赛后没有数据」。实测证明平台会推送滚球赔率，所以现在
        把进行中的比赛也纳入订阅。
        """
        if not self.ws:
            return
        upcoming = self.upcoming_ids()
        live = self.live_ids()
        ids = upcoming + live
        self.ws.subscribe(ids)
        # 本地剔除已结束的。注意 keep 里要含 live —— 否则刚开赛的比赛
        # 会被 prune 掉，订阅集里就只剩未开赛的了。
        removed = self.ws.prune(set(ids))
        if removed:
            log.info('pruned %d ended matches from subscription', removed)
        if live:
            log.debug('subscription: %d upcoming + %d live = %d',
                      len(upcoming), len(live), len(ids))

        # 补拉还没有赔率的比赛
        self.fetch_missing_odds(upcoming)

    def fetch_missing_odds(self, ids: list[str] | None = None) -> int:
        """给没有赔率数据的比赛补一次基线。

        每小时赛程刷新后调用，保证新进入窗口的比赛有初始数据。
        """
        hours = config.HORIZON_HOURS
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

        # 滚球采集器必须第一个创建：refresh_live_schedule 里
        # 要给新发现的比赛发 mark_dirty，需要 self.live 已经就绪。
        if config.LIVE_ENABLED:
            try:
                from collect.live import LiveCollector
                self.live = LiveCollector(self.adapter, self.dao,
                                          getattr(self.dao, '_dsn', None))
            except Exception as e:
                log.error('live collector init failed: %s', e)
                self.live = None

            if self.live is not None:
                try:
                    from analyze.live.run import analyze_live_snapshot
                    self.live.set_snapshot_handler(analyze_live_snapshot)
                except Exception as e:
                    log.exception('live snapshot analyzer unavailable; '
                                  'collection continues: %s', e)

        log.info('=== phase 1: schedule ===')
        self.refresh_schedule()
        # 滚球列表要单独拉 —— 赛程接口不返回已开赛的比赛，
        # 漏掉的场次只能靠这个补进来。放在 phase 2 之前，
        # 这样 bootstrap / 订阅都能看到它们。
        self.refresh_live_schedule()

        log.info('=== phase 2: bootstrap ===')
        t0 = time.time()
        total = self.refresher.bootstrap(hours=config.HORIZON_HOURS)
        log.info('bootstrap: %d rows in %.1fs', total, time.time() - t0)

        log.info('=== phase 3: websocket ===')
        # WS 回调需要通知 live collector —— 它已经创建好了

        if self.use_ws:
            self._start_ws()
        else:
            log.warning('websocket disabled (--no-ws)')

        log.info('=== phase 4: loops ===')
        self.refresher.start()
        if self.live:
            try:
                self.live.start()
                log.info('live collector started')
            except Exception as e:
                log.error('live collector start failed: %s', e)
                self.live = None
        else:
            log.info('live collector disabled')

        if once:
            log.info('--once given, exiting after one cycle')
            self.stop()
            return 0

        return self._main_loop()

    def _on_push(self, match_id: str) -> None:
        """WS 推送回调：同时通知赛前刷新器和滚球采集器。

        同一场比赛在「未开赛」和「进行中」两个阶段都可能推送，
        交给各自的逻辑判断该不该处理 —— refresher 会因 kickoff
        已过而跳过，live 只在滚球窗口内才处理。
        """
        self.refresher.mark_dirty(match_id)
        if self.live:
            self.live.mark_dirty(match_id)

    def _on_remove(self, match_id: str) -> None:
        """WS 删盘：保守地让整场当前机会失效并排队全量重取。

        v1 尚未把删除事件精确映射到市场 ID；按设计，无法映射时整场
        失效比继续沿用旧机会安全。
        """
        self.refresher.mark_removed(match_id)
        if self.live:
            try:
                self.live.dao.invalidate_live_snapshot(match_id)
            except Exception:
                log.exception('live invalidation on remove failed: %s',
                              match_id)
            self.live.mark_dirty(match_id)

    def _start_ws(self) -> None:
        try:
            self.adapter.on_push(self._on_push)
            self.adapter.on_remove(self._on_remove)
            self.ws = self.adapter.start_ws(
                initial_ids=self.upcoming_ids() + self.live_ids())
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
                        self.refresher.bootstrap(hours=config.HORIZON_HOURS)

                # 每小时刷新赛程
                if time.monotonic() - self._last_schedule > 3600:
                    try:
                        self.refresh_schedule()
                        self.refresh_live_schedule()
                        self.sync_subscription()
                    except Exception as e:
                        log.exception('schedule refresh failed: %s', e)

                # 滚球列表独立节奏（默认 60 秒）。比赛程密得多：
                # 新开赛的比赛如果这一轮没补进来，就要等下一轮才采得到。
                # 补进来之后 sync_subscription 也会把它纳入订阅集。
                if (config.LIVE_ENABLED
                        and time.monotonic() - self._last_live_schedule
                        > config.LIVE_SCHEDULE_SEC):
                    try:
                        added = self.refresh_live_schedule()
                        self._last_live_schedule = time.monotonic()
                        if added:
                            self.sync_subscription()
                    except Exception as e:
                        log.exception('live schedule refresh failed: %s', e)

                # 每 6 小时清理
                if time.monotonic() - self._last_cleanup > 6 * 3600:
                    try:
                        r = self.dao.cleanup()
                        log.info('cleanup: %s', r)
                        # 滚球数据保留期更短（3 天），一并清
                        if getattr(self, 'live', None):
                            live_r = self.dao.cleanup_live(
                                days=config.LIVE_RETAIN_DAYS)
                            log.info('cleanup_live: %s', live_r)
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
        if getattr(self, 'live', None):
            try:
                self.live.stop()
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
    """日志：终端里跑输出控制台，被启动脚本拉起则写日志文件（按天切分）。"""
    from common import logsetup
    logsetup.setup('collect', verbose)


def install_signal_handlers() -> None:
    """把 SIGTERM 转成 KeyboardInterrupt。

    deploy/stop.sh 发的是 SIGTERM，而 Python 只把 SIGINT 转成
    KeyboardInterrupt。不处理的话 SIGTERM 会直接终止进程，`except
    KeyboardInterrupt` 和 `finally` 都不执行——Refresher 的线程池不会
    shutdown，正在途中的整场替换事务被硬断。
    """
    def _to_keyboard_interrupt(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _to_keyboard_interrupt)


def main():
    ap = argparse.ArgumentParser(description='KessBet 采集进程')
    ap.add_argument('--once', action='store_true', help='跑一轮就退出')
    ap.add_argument('--no-ws', action='store_true', help='不连 WebSocket')
    ap.add_argument('-v', '--verbose', action='store_true')
    args = ap.parse_args()

    setup_logging(args.verbose)
    install_signal_handlers()

    c = Collector(use_ws=not args.no_ws)
    code = 0
    try:
        code = c.start(once=args.once)
    except KeyboardInterrupt:
        log.info('interrupted (SIGTERM/SIGINT)')
        c.stop()
    finally:
        try:
            c.report()
        except Exception as e:
            log.error('report failed: %s', e)
        c.dao.close()
    sys.exit(code or 0)


if __name__ == '__main__':
    main()
