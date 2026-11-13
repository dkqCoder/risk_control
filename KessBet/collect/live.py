"""collect/live.py — 滚球数据采集。

与 refresher.py 的分工：
  refresher  负责赛前。推送驱动的脏标记 + 防抖，写 kessbet_odds_prematch
  live       负责开赛后。按间隔采集状态和赔率，写 kessbet_*_live 三张表

为什么不复用 refresher：
  触发机制不同。refresher 是「WS 推送 -> 脏标记 -> 防抖后拉」，
  而滚球比赛**当前不在 WS 订阅集里**（upcoming() 只查未开赛），
  收不到推送，只能主动轮询。混在一起会让 refresher 的逻辑变复杂。

两种触发方式（由 config.LIVE_USE_PUSH 切换）：
  push=False（当前默认）：固定间隔轮询赔率
  push=True：WS 推送驱动，只在收到 bet_change 时拉赔率

状态（比分/角球/牌）始终是轮询 —— 推送里没有比分。
"""
import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pymysql

from collect import config
from common.timeutil import now_beijing

log = logging.getLogger(__name__)

# 比赛状态码（ms 字段）的终态集合。
#
# 实测（2026-11-10/11/12，足球）：
#   0    未开始。延期/取消的比赛也会一直停在这里，所以不能当终态
#   6    进行中（有 mt、有比分）
#   7    进行中（有 mt、有比分）
#   31   中场休息（有比分、有分钟但停在 45~50）。实测 2026-11-12
#        同时有 3 场停在这个状态 —— 它不是终态，必须继续采
#   100  常规时间结束（无 mt，比分固定）
#   120  点球大战（无 mt，比分出现 6:5、7:5 这类）
#
# 篮球用 13/14/15/16，含义未确认，本模块目前只处理足球。
# 如果将来要支持篮球，必须先补测。
#
# 注意这里只列**终态**。除它之外的一切状态（含 31）都继续采 ——
# 这个方向是安全的：把进行中误判成终态会丢数据，
# 把异常状态当成进行中最多是多采几次。
FINISHED_STATUS = frozenset({100, 120})

# 已知的进行中状态码。仅用于诊断和日志，**不要**用它做「该不该采」的
# 白名单 —— 那样一来 31 这种没见过的码就会被漏掉（实测已经遇到过）。
LIVE_STATUS = frozenset({6, 7, 31})


class LiveCollector:
    """滚球数据采集器。

    每个工作线程用自己的 Dao 连接（pymysql 不是线程安全的）——
    和 analyze/run.py 的做法一致。
    """

    def __init__(self, adapter, dao, dsn: dict | None = None,
                 on_snapshot=None):
        self.adapter = adapter
        self.dao = dao
        self._dsn = dsn or {}
        self._running = False
        self._thread = None
        self._on_snapshot = on_snapshot

        # 推送模式下，由 WS 回调写在这里；轮询模式下无用
        self._dirty: dict[str, float] = {}
        self._dirty_lock = threading.Lock()

        self.stats = {
            'polls': 0,
            'matches': 0,
            'state_ok': 0,
            'odds_ok': 0,
            'history_rows': 0,
            'finished': 0,
            'failed': 0,
            'skipped_empty': 0,
            'inconsistent_snapshot': 0,
            'snapshot_analyze_failed': 0,
        }

    def set_snapshot_handler(self, handler) -> None:
        """设置快照分析回调；采集器本身不依赖分析模块可用。"""
        self._on_snapshot = handler

    # ---------- 触发入口 ----------

    def mark_dirty(self, match_id: str) -> None:
        """WS 回调入口。收到某场比赛的推送时调用。

        只有在 config.LIVE_USE_PUSH 为 True 时才有意义。
        轮询模式不调用它。
        """
        with self._dirty_lock:
            self._dirty[match_id] = time.monotonic()

    def _take_dirty(self) -> list[str]:
        with self._dirty_lock:
            ids = list(self._dirty)
            self._dirty.clear()
        return ids

    # ---------- 单场采集 ----------

    def _collect_one(self, dao, meta: dict, fetch_odds: bool = True) -> None:
        """采一场：状态（必需）+ 赔率（可选）。

        dao 由调用方传入 —— 每个工作线程必须用自己的连接，
        pymysql 的连接不是线程安全的。

        fetch_odds=False 用于「状态轮询」场景：比分每 10 秒刷新一次，
        但赔率只在 WS 推送时才拉，避免无谓的接口调用。
        """
        match_id = meta['match_id']
        kickoff = meta['kickoff_time']

        state = self.adapter.fetch_state(match_id)

        # 比赛结束了。接口返回 None 或状态码属于「终态」。
        #
        # 实测到的终态（足球，2026-11-10/11）：
        #   100  常规时间结束
        #   120  点球大战（比分会出现 6:5、7:5 这种）
        # 这两个都没有 mt（已进行时间），比分已固定。
        #
        # 注意 ms=0 不是终态 —— 它是「未开始」，而延期/取消的比赛
        # 也会一直停在 0。那种情况靠 LIVE_WINDOW_MIN 让它自然出窗口。
        if state is None or state.get('ms') in FINISHED_STATUS:
            dao.mark_live_finished(match_id)
            self.stats['finished'] += 1
            return

        if not fetch_odds:
            dao.save_live_state(match_id, state, kickoff)
            self.stats['state_ok'] += 1
            return

        odds = self.adapter.fetch_odds(
            match_id, meta.get('home_team') or '', meta.get('away_team') or '')

        # 空响应多半是接口故障，不是「盘口全撤了」——
        # 跳过，不覆盖库里的数据。
        if not odds:
            dao.save_live_state(match_id, state, kickoff)
            self.stats['state_ok'] += 1
            self.stats['skipped_empty'] += 1
            return

        state_after = self.adapter.fetch_state(match_id)
        if state_after is None:
            self.stats['inconsistent_snapshot'] += 1
            self.mark_dirty(match_id)
            return
        if state_after.get('ms') in FINISHED_STATUS:
            dao.mark_live_finished(match_id)
            self.stats['finished'] += 1
            return

        dao.save_live_state(match_id, state_after, kickoff)
        self.stats['state_ok'] += 1
        if not _same_snapshot_state(state, state_after):
            self.stats['inconsistent_snapshot'] += 1
            self.mark_dirty(match_id)
            return

        state = state_after

        minute = _parse_minute(state.get('mt'))
        rows = [(s.market, s.side, s.line, s.label, float(s.odds)) for s in odds]

        # ★ 写入前剔除「内部和不可能」的盘口。
        #
        # 平台在停盘/占位时会返回垃圾赔率，但标记仍是可投注（`,1,`），
        # 所以解析层拦不住。实测（2026-11-13）：
        #   1st Half - Handicap 0:2    1@75.0  2@100.0  X@13.0
        #   Σ(1/赔率) = 0.013 + 0.010 + 0.077 = 0.10
        # 单一博彩公司内部必然有抽水，同一个盘口的 Σ 不可能 < 1。
        # 这种数据留在表里，套利判定会拿它和正常盘口组合，
        # 算出 Σ=0.05 那种根本不存在的机会。
        #
        # 在这里剔除而不是只在分析层过滤，是因为：
        #   1. 库内数据保持干净，任何读取方都不会被误导
        #   2. 流水表也不会记下这些假价格，事后回溯不受污染
        if getattr(config, 'LIVE_DROP_BROKEN_MARKETS', False):
            before = len(rows)
            rows = _drop_broken_markets(rows)
            if len(rows) < before:
                self.stats['broken_markets'] = (
                    self.stats.get('broken_markets', 0) + 1)

        if not rows:
            self.stats['skipped_empty'] += 1
            return

        res = dao.replace_live_odds(
            match_id, kickoff, rows,
            minute=minute,
            home_score=_to_int(state.get('hmp')),
            away_score=_to_int(state.get('amp')),
        )
        self.stats['odds_ok'] += 1
        self.stats['history_rows'] += res.get('changed', 0)

        if self._on_snapshot:
            snapshot = dict(meta)
            snapshot.update({
                'snapshot_id': uuid.uuid4().hex,
                'minute': minute,
                'home_score': _to_int(state.get('hmp')),
                'away_score': _to_int(state.get('amp')),
                'match_status': state.get('ms'),
                'match_clock': state.get('mt'),
                'odds_updated': dao.live_odds_updated_at(match_id),
                'snapshot_selections': rows,
            })
            try:
                self._on_snapshot(dao, snapshot, rows)
            except Exception as e:
                self.stats['snapshot_analyze_failed'] += 1
                log.exception('live snapshot analyze %s failed: %s', match_id, e)

    # ---------- 一轮采集 ----------

    def poll_once(self) -> int:
        """状态轮询：给窗口内所有比赛刷新比分/角球/牌。

        赔率**不在这里**拉 —— 实测确认平台会推送滚球赔率
        （2026-11-11：180 秒 3 次推送，全部含滚球盘口），
        赔率由 push_once() 处理。这里只管状态。
        """
        matches = self.dao.live_matches(config.LIVE_WINDOW_MIN)
        self.stats['polls'] += 1
        self.stats['matches'] = len(matches)
        if not matches:
            return 0

        n = min(config.LIVE_CONCURRENCY, len(matches))
        if n <= 1:
            for m in matches:
                self._safe_collect(self.dao, m, fetch_odds=False)
        else:
            with ThreadPoolExecutor(n, thread_name_prefix='live') as pool:
                list(pool.map(
                    lambda m: self._collect_with_own_dao(m, fetch_odds=False),
                    matches))
        return len(matches)

    def push_once(self) -> int:
        """推送驱动：只处理收到 bet_change 的比赛，状态 + 赔率都拉。

        推送里带的是 `mari` 格式的盘口定义，理论上可以直接解析，
        但现在走「推送只作信号、数据仍取 HTTP 全量」的架构 ——
        这和赛前 refresher 一致，好处是推送丢帧不会导致数据不一致。
        """
        ids = self._take_dirty()
        if not ids:
            return 0
        self.stats['pushes'] = self.stats.get('pushes', 0) + 1

        # 只处理仍在滚球窗口内的
        in_window = {r['match_id']: r for r in
                     self.dao.live_matches(config.LIVE_WINDOW_MIN)}
        targets = [in_window[i] for i in ids if i in in_window]
        if not targets:
            return 0

        n = min(config.LIVE_CONCURRENCY, len(targets))
        if n <= 1:
            for m in targets:
                self._safe_collect(self.dao, m, fetch_odds=True)
        else:
            with ThreadPoolExecutor(n, thread_name_prefix='live-push') as pool:
                list(pool.map(
                    lambda m: self._collect_with_own_dao(m, fetch_odds=True),
                    targets))
        return len(targets)

    def _collect_with_own_dao(self, meta: dict,
                              fetch_odds: bool = True) -> None:
        """工作线程入口：建自己的连接，用完关掉。

        不用连接池是刻意的 —— 滚球采集的节奏是「每 10 秒一批状态」，
        连接闲置期远长于使用期。pymysql 的连接闲置久了会被服务端断开
        （wait_timeout），复用一个池反而要处理重连。
        """
        from db.dao import Dao
        d = Dao(**self._dsn) if self._dsn else Dao()
        try:
            self._safe_collect(d, meta, fetch_odds=fetch_odds)
        finally:
            try:
                d.close()
            except Exception:
                pass

    def _safe_collect(self, dao, meta: dict,
                      fetch_odds: bool = True) -> None:
        try:
            self._collect_one(dao, meta, fetch_odds=fetch_odds)
        except Exception as e:
            log.warning('live collect %s failed: %s',
                        meta.get('match_id'), e)
            self.stats['failed'] += 1
            try:
                dao.conn.rollback()
            except Exception:
                pass

    # ---------- 主循环 ----------

    def run(self) -> None:
        """主循环：状态轮询 + 推送处理。

        两个节奏：
          - 状态（比分/角球/牌）：每 LIVE_POLL_STATE_SEC 秒全量刷一次
          - 赔率：只在收到 WS 推送时拉（push_once）

        分开的理由：状态变化不频繁（进球才是事件），10 秒轮询足够；
        而赔率变动频繁，靠平台推送触发比轮询省很多接口调用。
        """
        self._running = True
        log.info('live collector: state_poll=%ds window=%dmin concurrency=%d',
                 config.LIVE_POLL_STATE_SEC, config.LIVE_WINDOW_MIN,
                 config.LIVE_CONCURRENCY)

        last_state_poll = 0.0
        while self._running:
            try:
                # 推送驱动的赔率采集：有脏标记就处理
                if self.pending_push:
                    self.push_once()

                # 状态轮询：到点就全量刷一遍
                now = time.monotonic()
                if now - last_state_poll >= config.LIVE_POLL_STATE_SEC:
                    self.poll_once()
                    last_state_poll = now
            except Exception as e:
                log.exception('live loop iteration failed: %s', e)

            # 1 秒一个tick，保证推送能及时响应
            time.sleep(1)

    @property
    def pending_push(self) -> int:
        with self._dirty_lock:
            return len(self._dirty)

    def start(self) -> threading.Thread:
        t = threading.Thread(target=self.run, daemon=True,
                             name='live-collector')
        t.start()
        self._thread = t
        return t

    def stop(self) -> None:
        self._running = False


# 采集阶段判定「明显异常」的阈值。
#
# 只拦真正不可能的数据，不拦「选项不全的盘口」——
# 平台经常只挂部分选项（1x2 只有 1 和 X），那种盘口内部和天然偏低
# （实测 ~0.71），但它是有意义的真实报价，不该丢。
#
# 实测：
#   完整盘口      Σ = 1.05 ~ 1.16
#   部分选项      Σ ≈ 0.70
#   停盘占位价    Σ = 0.10 ~ 0.22   ← 只拦这一档
#
# 更严的「单一博彩公司不可能负抽水」检查放在分析层
# （analyze/live/result.py 的 _drop_broken_markets，阈值 0.90）——
# 那里才是正确性关键的地方。
MIN_MARKET_SUM = 0.30


def _drop_broken_markets(rows):
    """剔除内部和明显不可能的盘口。

    rows: [(market, side, line, selection, odds), ...]

    实测反例（2026-11-13，Equatorial Guinea）：
       1st Half - Handicap 0:2   1@75.0   2@100.0   X@13.0
       Σ = 0.10

    平台把「几乎不可能」的选项标到 75/100 这种占位数，两边同时高价，
    加起来就是一个根本不存在的负抽水盘口。
    """
    from collections import defaultdict

    groups = defaultdict(list)
    for r in rows:
        groups[(r[0], r[1], r[2])].append(r)

    out = []
    for key, group in groups.items():
        total = sum(1.0 / r[4] for r in group if r[4] > 0)
        if total < MIN_MARKET_SUM:
            log.debug('剔除异常盘口 %s Σ=%.3f (%d 个选项)',
                      '|'.join(x or '-' for x in key), total, len(group))
            continue
        out.extend(group)
    return out


def _parse_minute(mt) -> int | None:
    """'65:54' -> 65。空值或格式不符返回 None。"""
    if not mt or ':' not in str(mt):
        return None
    try:
        return int(str(mt).split(':')[0])
    except (ValueError, IndexError):
        return None


def _to_int(v) -> int | None:
    """接口给的是字符串，转 int。空值返回 None。

    注意：不能用 int(v or 0) —— 空字符串会变成 0，
    而「没采到」和「0 个球」是两件事。
    """
    if v in (None, '', '-'):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _same_snapshot_state(before: dict, after: dict) -> bool:
    """盘口请求前后的关键状态是否属于同一个定价时刻。"""
    if (_to_int(before.get('hmp')), _to_int(before.get('amp'))) != (
            _to_int(after.get('hmp')), _to_int(after.get('amp'))):
        return False
    if before.get('ms') != after.get('ms'):
        return False
    before_minute = _parse_minute(before.get('mt'))
    after_minute = _parse_minute(after.get('mt'))
    if before_minute is None or after_minute is None:
        return True
    return (before_minute <= 45) == (after_minute <= 45)
