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
import hashlib
import json
import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pymysql

from collect import config
from common.fingerprint import input_fingerprint
from common.phase import classify_phase, ht_source
from common.scores import half_time_score
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

        # ★ 状态接口返回 None 是**请求失败/未知**，不是“比赛结束”。
        # 把它当结束会误删比赛、让旧机会继续挂着。正确做法：
        # 撤销当前机会，计数失败，并排队重试；只有明确的终态码才结束。
        if state is None:
            dao.invalidate_live_snapshot(match_id)
            self.stats['state_failed'] = self.stats.get('state_failed', 0) + 1
            self.mark_dirty(match_id)
            return

        # 实测到的终态（足球，2026-11-10/11）：
        #   100  常规时间结束
        #   120  点球大战（比分会出现 6:5、7:5 这种）
        # 这两个都没有 mt（已进行时间），比分已固定。
        #
        # 注意 ms=0 不是终态 —— 它是「未开始」，而延期/取消的比赛
        # 也会一直停在 0。那种情况靠 LIVE_WINDOW_MIN 让它自然出窗口。
        if state.get('ms') in FINISHED_STATUS:
            dao.mark_live_finished(match_id)
            self.stats['finished'] += 1
            return

        if not fetch_odds:
            changed = dao.save_live_state(match_id, state, kickoff)
            self.stats['state_ok'] += 1
            if changed:
                # 比分/阶段变化：旧快照已被 save_live_state 置空，
                # 这里排队拉一次完整赔率，形成新快照后才会重新判定。
                self.stats['state_changed_refetch'] = (
                    self.stats.get('state_changed_refetch', 0) + 1)
                self.mark_dirty(match_id)
            return

        # 同一次请求同时拿解析结果和原始盘口列表 —— 原始串是命中取证的
        # 第一手证据，不能在命中后重新请求（赔率早就变了）。
        request_started = now_beijing()
        home_name = meta.get('home_team') or ''
        away_name = meta.get('away_team') or ''
        fetch_result = getattr(self.adapter, 'fetch_odds_result', None)
        fetch_res = None
        identity = None
        raw_markets = None
        if fetch_result:
            odds, fetch_res, identity = fetch_result(
                match_id, home_name, away_name)
            raw_markets = (fetch_res or {}).get('markets') or []
        else:
            fetch_with_raw = getattr(self.adapter, 'fetch_odds_with_raw', None)
            if fetch_with_raw:
                odds, raw_markets = fetch_with_raw(
                    match_id, home_name, away_name)
            else:
                odds = self.adapter.fetch_odds(
                    match_id, home_name, away_name)
        response_received = ((fetch_res or {}).get('received_at')
                             or now_beijing())

        # F03：失败/空市场/结构错误必须区分，不能都当“跳过”。
        fstatus = (fetch_res or {}).get('status')
        if fstatus in ('transport_error', 'protocol_error'):
            dao.invalidate_live_snapshot(match_id)
            self.stats['fetch_error'] = (
                self.stats.get('fetch_error', 0) + 1)
            self.mark_dirty(match_id)
            return
        if fstatus in ('ok_empty', 'parsed_empty'):
            # v1 尚未确认 ok_empty 的服务端语义；保守地当作“当前无可认证
            # 报价”，保留历史但撤销当前机会，不发布空快照。
            dao.save_live_state(match_id, state, kickoff)
            dao.invalidate_live_snapshot(match_id)
            self.stats['skipped_empty'] += 1
            return

        if not odds:
            dao.save_live_state(match_id, state, kickoff)
            # 空响应不能继续认证旧快照：旧赔率留作历史，当前指针清空。
            dao.invalidate_live_snapshot(match_id)
            self.stats['state_ok'] += 1
            self.stats['skipped_empty'] += 1
            return

        state_after = self.adapter.fetch_state(match_id)
        if state_after is None:
            # 状态接口失败 ≠ 确认比赛结束；旧机会先失效，再排队重试。
            dao.invalidate_live_snapshot(match_id)
            self.stats['inconsistent_snapshot'] += 1
            self.mark_dirty(match_id)
            return
        if state_after.get('ms') in FINISHED_STATUS:
            dao.mark_live_finished(match_id)
            self.stats['finished'] += 1
            return

        state_before = state
        state = state_after
        if not _same_snapshot_state(state_before, state_after):
            dao.save_live_state(match_id, state_after, kickoff)
            dao.invalidate_live_snapshot(match_id)
            self.stats['state_ok'] += 1
            self.stats['inconsistent_snapshot'] += 1
            self.mark_dirty(match_id)
            return

        minute = _parse_minute(state.get('mt'))
        rows = [(s.market, s.side, s.line, s.label, float(s.odds)) for s in odds]

        # 阶段与半场来源分级（F05）。分钟只作辅助，ms/补时不能冒充权威阶段。
        phase, phase_source = classify_phase(state)
        hts = ht_source(phase)

        # 半场比分在采集时冻结进快照；分析和回放都用这份值，
        # 不再在分析时另读可变的历史流水（D01 输入一致性）。
        ht = None
        if hts == 'inferred_history' and minute is not None and minute > 45:
            try:
                ht = half_time_score(dao.live_history_for_half_time(match_id))
            except Exception as e:
                log.warning('半场比分读取失败 %s: %s', match_id, e)
        ht_home = ht[0] if ht else None
        ht_away = ht[1] if ht else None

        # ★ 写入前剔除「内部和不可能」的盘口。
        if getattr(config, 'LIVE_DROP_BROKEN_MARKETS', False):
            before = len(rows)
            rows = _drop_broken_markets(rows)
            if len(rows) < before:
                self.stats['broken_markets'] = (
                    self.stats.get('broken_markets', 0) + 1)

        if not rows:
            dao.save_live_state(match_id, state, kickoff)
            dao.invalidate_live_snapshot(match_id)
            self.stats['state_ok'] += 1
            self.stats['skipped_empty'] += 1
            return

        # ★ D01：状态、赔率、输入快照、当前指针在**同一个事务**里提交。
        # 任何一步失败都整体回滚，绝不出现「新赔率配旧 raw」。
        snapshot_id = uuid.uuid4().hex
        state_json = json.dumps(state, ensure_ascii=False)
        state_before_json = json.dumps(state_before, ensure_ascii=False)
        rows_json = json.dumps(rows, ensure_ascii=False)
        if raw_markets is not None:
            raw_payload = json.dumps({
                'match_id': match_id,
                'request_started_at': request_started.isoformat(sep=' '),
                'response_received_at': response_received.isoformat(sep=' '),
                'state_before': state_before,
                'state_after': state,
                'markets': raw_markets,
            }, ensure_ascii=False)
        else:
            raw_payload = ''
        filter_policy = 'drop_broken=%s' % bool(
            getattr(config, 'LIVE_DROP_BROKEN_MARKETS', False))

        # 请求/响应信封 + 输入指纹（Task 2）。指纹不含接收时间；
        # 平台身份暂由原始 mari 串承担（v1 尚未拆平台 ID 字段）。
        request_json = (json.dumps(fetch_res.get('request'),
                                   ensure_ascii=False)
                        if fetch_res else None)
        if fetch_res:
            raw_body = fetch_res.get('raw_body') or ''
            response_json = json.dumps({
                'status': fetch_res.get('status'),
                'response_status': fetch_res.get('response_status'),
                'response_headers': fetch_res.get('response_headers'),
                'raw_body': raw_body,
                'markets': raw_markets,
            }, ensure_ascii=False)
            response_hash = hashlib.sha256(
                raw_body.encode('utf-8')).hexdigest()
        else:
            response_json = None
            response_hash = None
        home_i = _to_int(state.get('hmp'))
        away_i = _to_int(state.get('amp'))
        fp = input_fingerprint(
            identity if identity is not None else rows,
            home_i if home_i is not None else -1,
            away_i if away_i is not None else -1,
            phase, ht, filter_policy, config.LIVE_RULES_VERSION)

        try:
            dao.save_live_state(match_id, state, kickoff, commit=False)
            generation = dao.bump_live_generation(match_id)
            res = dao.replace_live_odds(
                match_id, kickoff, rows,
                minute=minute,
                home_score=_to_int(state.get('hmp')),
                away_score=_to_int(state.get('amp')),
                commit=False,
            )
            dao.save_live_snapshot(
                snapshot_id=snapshot_id,
                match_id=match_id,
                generation=generation,
                request_started_at=request_started,
                response_received_at=response_received,
                state_before=state_before_json,
                state_after=state_json,
                minute=minute,
                home_score=_to_int(state.get('hmp')),
                away_score=_to_int(state.get('amp')),
                ht_home=ht_home,
                ht_away=ht_away,
                request_json=request_json,
                response_json=response_json,
                response_hash=response_hash,
                parser_version=config.LIVE_PARSER_VERSION,
                input_fingerprint=fp,
                phase=phase,
                phase_source=phase_source,
                ht_source=hts,
                raw_payload=raw_payload,
                rows_json=rows_json,
                filter_policy=filter_policy,
                commit=False,
            )
            dao.set_current_snapshot(match_id, snapshot_id, commit=False)
            dao.conn.commit()
        except Exception as e:
            try:
                dao.conn.rollback()
            except Exception:
                pass
            # 事务回滚后旧指针仍在；单独清掉，避免旧机会继续展示。
            try:
                dao.invalidate_live_snapshot(match_id)
            except Exception:
                pass
            self.stats['snapshot_write_failed'] = (
                self.stats.get('snapshot_write_failed', 0) + 1)
            log.exception('写入滚球输入快照失败 %s: %s', match_id, e)
            self.mark_dirty(match_id)
            return

        self.stats['state_ok'] += 1
        self.stats['odds_ok'] += 1
        self.stats['history_rows'] += res.get('changed', 0)

        if self._on_snapshot:
            snapshot = dict(meta)
            snapshot.update({
                'snapshot_id': snapshot_id,
                'generation': generation,
                'minute': minute,
                'home_score': _to_int(state.get('hmp')),
                'away_score': _to_int(state.get('amp')),
                'ht_home': ht_home,
                'ht_away': ht_away,
                'input_fingerprint': fp,
                'phase': phase,
                'phase_source': phase_source,
                'ht_source': hts,
                'match_status': state.get('ms'),
                'match_clock': state.get('mt'),
                # 新鲜度基准 = 本地收到赔率响应的时刻，不是 DB 的 NOW()
                'odds_updated': response_received,
                'request_started_at': request_started.isoformat(sep=' '),
                'response_received_at': response_received.isoformat(sep=' '),
                'state_before': state_before,
                'state_after': state,
                'snapshot_selections': rows,
            })
            if raw_payload:
                snapshot['raw_payload'] = raw_payload
                snapshot['raw_fetched_time'] = response_received.isoformat(
                    sep=' ')
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
            # 请求异常时无法确认当前报价：撤销当前机会并排队重试。
            mid = meta.get('match_id')
            if mid:
                try:
                    dao.invalidate_live_snapshot(mid)
                except Exception:
                    pass
                self.mark_dirty(mid)

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
