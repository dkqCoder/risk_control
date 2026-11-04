"""analyze/run.py — 分析主循环。

从数据库读当前赔率，判定是否存在单场内部的无损套利。
**只读数据库，不依赖采集侧任何模块。**

两套模型并行求解并交叉验证：
  ILP（solver.solve）    覆盖模型：选哪几个选项，成本 = Σ(1/赔率)
  LP （payout.lp_solve） 资金配置：每个选项投多少钱，最大化最差状态净收益

两者在数学上等价（t = 1/cost - 1），实测 80 场零差异。
任何一次不一致都说明建模或求解有问题，会告警。
"""
import json
import logging
import signal
import time
from datetime import datetime, timezone

from analyze import payout, solver
from analyze.covers import soccer
from collect import config
from common.timeutil import now_beijing

log = logging.getLogger(__name__)

# ---------------------------------------------------------------
# ILP 与 LP 的关系（曾经写错，务必看清楚）
# ---------------------------------------------------------------
# 两者**不**等价，相等只是特例：
#
#   ILP  x_i ∈ {0,1}   每个选项全买或不买   最优成本 C
#   LP   s_i ≥ 0 连续  允许按比例分配      最优最差收益 t
#
# 「选项只能全买」时，t = 1/C - 1。但 LP 允许**部分下注**，能找到
# 比 ILP 更便宜的资金分配，所以一般情况下：
#
#       t >= 1/C - 1        （LP 不劣于 ILP）
#
# 实测 122 场里有 5 场（4%）LP 严格更优，差值可达 0.3%。
# 早期代码假设两者相等、容差 1e-4，这 5 场被误报成 CROSS-CHECK FAIL。
#
# 现在的判据：
#   - 必须 t >= 1/C - 1 - EPS      （LP 不能比 ILP 差，否则是求解有问题）
#   - 差值本身不告警，但记下来     （差异大小说明「部分下注」的收益空间）
#   - 真正的一致性检查：把 LP 的权重独立复算一遍，验证最差收益与报告值相符
CROSS_CHECK_EPS = 1e-6      # 数值误差容限（LP 不该比 ILP 差超过这个）

# 成本变化判定阈值。低于它视为「没变」，不记流水 ——
# 求解器的浮点噪声在 1e-9 量级，赔率微调导致的成本变化通常 >= 1e-4。
# 取 1e-6 是为了避开噪声又不漏掉真实的小幅变动。
COST_EPS = 1e-6

# 兼容旧名字：某些调用方/测试仍引用 CROSS_CHECK_TOL
CROSS_CHECK_TOL = CROSS_CHECK_EPS


class MatchAnalysis:
    """一场比赛的完整分析结果。"""
    __slots__ = ('match_id', 'home', 'away', 'kickoff', 'sport',
                 'ilp', 'lp', 'cross_ok', 'cross_gap', 'n_options',
                 'n_candidates', 'error')

    def __init__(self, match_id, home, away, kickoff, sport):
        self.match_id = match_id
        self.home = home
        self.away = away
        self.kickoff = kickoff
        self.sport = sport
        self.ilp = None
        self.lp = None
        self.cross_ok = True
        self.cross_gap = 0.0
        self.n_options = 0
        self.n_candidates = 0
        self.error = None

    @property
    def is_arbitrage(self) -> bool:
        """两套模型都判定套利才算。"""
        ilp_arb = (self.ilp is not None
                   and self.ilp.cost is not None
                   and self.ilp.cost < 1.0)
        return ilp_arb and self.lp is not None and self.lp.is_arbitrage

    @property
    def best_cost(self):
        return self.ilp.cost if self.ilp else None


def evaluate_match(dao, match_id: str, sport: str,
                   home: str = '', away: str = '',
                   kickoff=None) -> MatchAnalysis:
    """对单场比赛跑两套模型并交叉验证。"""
    result = MatchAnalysis(match_id, home, away, kickoff, sport)

    if sport != 'sr:sport:1':
        result.error = 'unsupported_sport'
        return result

    rows = dao.get_selections(match_id)
    if not rows:
        result.error = 'no_data'
        return result

    result.n_options = len(rows)
    cands = soccer.collect_from_rows(rows)
    if not cands:
        result.error = 'no_candidates'
        return result
    result.n_candidates = len(cands)

    # --- ILP：覆盖模型 ---
    try:
        result.ilp = solver.solve(cands, soccer.NS)
    except Exception as e:
        log.exception('ILP %s failed: %s', match_id, e)
        result.error = f'ilp_error: {e}'
        return result

    if result.ilp.status == solver.NO_COVER:
        result.error = 'no_cover'
        return result

    # --- LP：资金配置 ---
    try:
        result.lp = payout.lp_solve(cands, soccer.NS)
    except Exception as e:
        log.exception('LP %s failed: %s', match_id, e)
        result.error = f'lp_error: {e}'
        return result

    if result.lp.status != 'ok':
        result.error = f'lp_{result.lp.status}'
        return result

    # --- 交叉验证 ---
    #
    # 两个检查，缺一不可：
    #
    # (1) LP 不能比 ILP 差。
    #     LP 允许部分下注，可行域严格包含 ILP 的 0/1 解，所以
    #     t >= 1/C - 1 必然成立。反过来 t 明显更差就说明求解或建模有问题。
    #
    # (2) LP 的权重独立复算一遍。
    #     不信任求解器返回的 t，用 G 矩阵和 s 向量重新算每个状态的净收益，
    #     必须与报告值一致。这能同时抓出「权重越界」「矩阵构造错」两类问题。
    if result.ilp.cost:
        ilp_implied = 1.0 / result.ilp.cost - 1.0
        t = result.lp.worst_profit

        # (1) LP 应不劣于 ILP
        result.cross_gap = t - ilp_implied      # 正数 = LP 更优（正常）
        result.cross_ok = result.cross_gap >= -CROSS_CHECK_EPS

        # (2) 独立复算
        try:
            chk = payout.verify_payout(result.lp, cands, soccer.NS)
            recomputed = chk['worst_profit']
            if abs(recomputed - t) > 1e-6:
                log.error('LP 复算不符 %s: 报告=%.8f 复算=%.8f',
                          match_id, t, recomputed)
                result.cross_ok = False
                result.cross_gap = recomputed - ilp_implied
            if not chk['is_profitable'] and result.lp.is_arbitrage:
                log.error('LP 套利标记与复算不符 %s', match_id)
                result.cross_ok = False
        except Exception as e:
            log.warning('LP 复算失败 %s: %s', match_id, e)

    return result


def format_payout_table(a: MatchAnalysis, stake: float = 1000.0) -> str:
    """把一段分析结果渲染成人类可读的收益表。"""
    lines = []
    lines.append('=' * 72)
    lines.append('%s vs %s' % (a.home[:30], a.away[:30]))
    if a.kickoff:
        lines.append('开赛 %s  |  matchId %s' % (a.kickoff, a.match_id))
    lines.append('=' * 72)

    if a.error:
        lines.append('无法分析: %s' % a.error)
        lines.append('  选项 %d 个，可建模 %d 个' % (a.n_options, a.n_candidates))
        return '\n'.join(lines)

    ilp, lp = a.ilp, a.lp
    lines.append('数据: %d 个选项，%d 个可建模' % (a.n_options, a.n_candidates))
    lines.append('')
    lines.append('【模型 1 · 覆盖成本 ILP】')
    lines.append('  最小覆盖成本 = %.6f' % ilp.cost)
    lines.append('  选中 %d 个:' % len(ilp.chosen))
    for d in ilp.chosen:
        lines.append('    %s' % d)

    lines.append('')
    lines.append('【模型 2 · 资金配置 LP】')
    lines.append('  总投注 %.2f 元，最差状态净收益 %.2f 元 (%.3f%%)' % (
        stake, lp.worst_profit * stake, lp.worst_profit * 100))
    lines.append('  建议投注 %d 项:' % len(lp.chosen))
    for desc, weight in lp.chosen:
        lines.append('    %-44s %8.2f 元  (%.2f%%)' % (
            desc[:44], weight * stake, weight * 100))

    if lp.state_profits is not None:
        profits = lp.state_profits
        n = len(profits)
        worst = min(profits)
        best = max(profits)
        loss = sum(1 for p in profits if p < -1e-9)
        gain = sum(1 for p in profits if p > 1e-9)

        lines.append('')
        lines.append('  逐状态结算（投注 %.0f 元）:' % stake)
        lines.append('    结果范围   净收益       总返还      状态数')
        lines.append('    最差       %+9.2f   %9.2f   %5d' % (
            worst * stake, (1 + worst) * stake, sum(
                1 for p in profits if abs(p - worst) < 1e-9)))
        if best > worst + 1e-9:
            lines.append('    最好       %+9.2f   %9.2f   %5d' % (
                best * stake, (1 + best) * stake, sum(
                    1 for p in profits if abs(p - best) < 1e-9)))
        lines.append('    ─────────────────────────────────────────')
        lines.append('    亏损状态 %d 个，盈利状态 %d 个，保本 %d 个' % (
            loss, gain, n - loss - gain))
        if a.lp.worst_states:
            lines.append('    最差状态 idx: %s' % a.lp.worst_states[:5])

    lines.append('')
    lines.append('【交叉验证】')
    expected = 1.0 / ilp.cost - 1.0 if ilp.cost else None
    if a.cross_ok:
        if expected is not None and a.cross_gap > CROSS_CHECK_EPS:
            lines.append('  ✓ 一致，且 LP 更优 %.4f%%（允许部分下注带来的收益）'
                         % (a.cross_gap * 100))
        else:
            lines.append('  ✓ 两模型一致')
    else:
        lines.append('  ✗ 交叉验证失败')
        if expected is not None:
            lines.append('    ILP 隐含最差收益 %.6f，LP 实际 %.6f' % (
                expected, lp.worst_profit))
            lines.append('    LP 比 ILP 差了 %.6f —— 求解或建模有问题' % (-a.cross_gap))

    lines.append('')
    lines.append('【判定】%s' % ('存在套利' if a.is_arbitrage else '无套利'))
    return '\n'.join(lines)


def pending_matches(dao, horizon_hours: int | None = None,
                    sport: str = 'sr:sport:1') -> list[tuple]:
    """返回需要（重新）计算的比赛。

    判定条件：没有快照，或快照比赔率旧。
    一条 SQL 搞定，不再逐场查两次库 —— 事件驱动模式下每 2 秒调一次，
    逐场查询会成为瓶颈。

    返回 [(match_id, sport, home, away, kickoff), ...]，按开赛时间排序。
    """
    horizon = horizon_hours or config.ANALYZE_HORIZON_HOURS
    sql = """
    SELECT m.match_id, m.sport, m.home_team, m.away_team, m.kickoff_time
    FROM kessbet_match m
    LEFT JOIN kessbet_match_cost c
      ON c.platform = m.platform AND c.match_id = m.match_id
    WHERE m.platform = 'kessbet'
      AND m.sport = %s
      AND m.kickoff_time > NOW()
      AND m.kickoff_time <= NOW() + INTERVAL %s HOUR
      AND EXISTS (SELECT 1 FROM kessbet_odds_prematch o
                  WHERE o.platform = m.platform AND o.match_id = m.match_id)
      AND (c.match_id IS NULL
           OR c.computed_time IS NULL
           OR c.computed_time < (SELECT MAX(o.updated_time)
                                 FROM kessbet_odds_prematch o
                                 WHERE o.platform = m.platform
                                   AND o.match_id = m.match_id))
    ORDER BY m.kickoff_time
    """
    with dao.conn.cursor() as cur:
        cur.execute(sql, (sport, horizon))
        return list(cur.fetchall())


def _analyze_one(dao, row, write_hits: bool, verbose: bool,
                 write_costs: bool, now) -> dict:
    """算一场，写快照。返回该场的统计增量。

    每个工作线程用自己的 Dao 连接（pymysql 不是线程安全的），
    由调用方保证传入的 dao 属于当前线程。
    """
    match_id, sport, home, away, kickoff = row
    out = {'evaluated': 0, 'hits': 0, 'errors': 0, 'no_cover': 0,
           'cross_fail': 0, 'min_cost': None}

    a = evaluate_match(dao, match_id, sport, home, away, kickoff)

    if write_costs:
        _persist_cost(dao, match_id, sport, kickoff, a, now,
                      dao.cost_updated_at(match_id))

    if a.error:
        if a.error == 'no_cover':
            out['no_cover'] = 1
        else:
            out['errors'] = 1
        if verbose:
            log.info(format_payout_table(a))
        return out

    out['evaluated'] = 1
    cost = a.best_cost
    if cost is not None:
        out['min_cost'] = cost

    if not a.cross_ok:
        out['cross_fail'] += 1
        log.error('CROSS-CHECK FAIL %s %s vs %s: ILP=%.6f (隐含收益 %.6f) '
                  'LP_worst=%.6f gap=%.6f —— LP 不应劣于 ILP，求解或建模有问题',
                  match_id, home, away, cost, 1.0 / cost - 1.0,
                  a.lp.worst_profit, a.cross_gap)

    if verbose:
        log.info(format_payout_table(a))

    if a.is_arbitrage:
        out['hits'] = 1
        log.warning('ARBITRAGE %s %s vs %s cost=%.6f worst_profit=%.6f',
                    match_id, home, away, cost, a.lp.worst_profit)
        log.warning('\n%s', format_payout_table(a))
        if write_hits:
            # 只写 kessbet_arbitrage_snapshot。原来还有一张 kessbet_odds_hit
            # 只存成本 + desc 列表，没有任何读取方；快照表是它的超集
            # （完整盘口 + ILP/LP + 交叉验证），留着只会让人以为有两处证据。
            try:
                _persist_arbitrage(dao, match_id, sport, home, away, kickoff, a)
            except Exception as e:
                log.exception('写套利快照失败 %s: %s', match_id, e)
    return out


def _persist_arbitrage(dao, match_id: str, sport: str, home: str,
                       away: str, kickoff, a: MatchAnalysis) -> None:
    """把命中的完整盘口写进 kessbet_arbitrage_snapshot。

    用 odds_updated 做去重键：分析每 2 秒轮询，同一份赔率持续命中时
    不能反复写 30~60KB 的快照。
    """
    from common.markets import group_markets

    rows = dao.get_selections(match_id)
    odds_updated = dao.cost_updated_at(match_id)
    league = ''
    try:
        with dao.conn.cursor() as c:
            c.execute("SELECT league, sportradar_id FROM kessbet_match "
                      "WHERE platform='kessbet' AND match_id=%s", (match_id,))
            r = c.fetchone()
        if r:
            league = r[0] or ''
            srid = r[1] or ''
        else:
            srid = ''
    except Exception:
        srid = ''

    detail = group_markets(
        [(m, s, l, lab, o) for m, s, l, lab, o in rows], home, away)

    written = dao.write_arbitrage_snapshot(
        match_id=match_id, sport=sport,
        home=home, away=away, league=league,
        canonical_id=srid or f'local:kessbet:{match_id}',
        kickoff_time=kickoff,
        cost=a.ilp.cost,
        cost_scope='single',
        lp_worst_profit=a.lp.worst_profit if a.lp else None,
        cross_gap=a.cross_gap,
        ilp_chosen=json.dumps(a.ilp.chosen, ensure_ascii=False),
        lp_chosen=json.dumps(
            [{'desc': d, 'weight': round(w, 8)} for d, w in a.lp.chosen],
            ensure_ascii=False),
        n_markets=len(detail),
        n_selections=len(rows),
        n_candidates=a.n_candidates,
        market_detail=json.dumps(detail, ensure_ascii=False),
        odds_updated=odds_updated,
        alert_time=now_beijing(),
    )
    if written:
        log.warning('套利快照已保存 %s (%d 个盘口 / %d 个选项)',
                    match_id, len(detail), len(rows))
    else:
        log.info('套利快照已存在（同一份赔率）%s，跳过', match_id)


def analyze_once(dao, horizon_hours: int | None = None,
                 write_hits: bool = True,
                 verbose: bool = False,
                 write_costs: bool = True,
                 force: bool = False,
                 concurrency: int | None = None) -> dict:
    """扫一轮：只算「待处理」的比赛。

    write_costs: 把每场的判定结果写进 kessbet_match_cost，供页面只读。
                 页面**不能**在请求里跑 ILP（单场 1.2 秒，一屏 50 场 60 秒）。
    force:       忽略「赔率没变」的跳过，强制全部重算。改了建模逻辑后用。
    concurrency: 并发工作线程数。ILP 是 CPU 密集的，设为 CPU 核数的一半比较稳妥。
    """
    horizon = horizon_hours or config.ANALYZE_HORIZON_HOURS
    stats = {
        'matches': 0, 'evaluated': 0, 'hits': 0,
        'no_cover': 0, 'skipped_sport': 0, 'errors': 0,
        'cross_fail': 0, 'min_cost': None, 'skipped_unchanged': 0,
    }

    now = now_beijing()
    if force or not write_costs:
        # 强制模式：扫全部未开赛比赛，走原来的逐场判断
        rows = dao.upcoming(horizon)
    else:
        rows = pending_matches(dao, horizon)

    # 只保留支持的运动会
    todo = [r for r in rows if r[1] == 'sr:sport:1']
    stats['skipped_sport'] = len(rows) - len(todo)
    stats['matches'] = len(rows)

    if not todo:
        return stats

    n = concurrency or getattr(config, 'ANALYZE_CONCURRENCY', 4)
    if n <= 1 or len(todo) <= 1:
        results = [_analyze_one(dao, r, write_hits, verbose, write_costs, now)
                   for r in todo]
    else:
        results = _run_parallel(dao, todo, n, write_hits, verbose,
                                write_costs, now)

    for r in results:
        stats['evaluated'] += r['evaluated']
        stats['hits'] += r['hits']
        stats['errors'] += r['errors']
        stats['no_cover'] += r['no_cover']
        stats['cross_fail'] += r['cross_fail']
        c = r['min_cost']
        if c is not None and (stats['min_cost'] is None or c < stats['min_cost']):
            stats['min_cost'] = c
    return stats


def _run_parallel(dao, todo, n, write_hits, verbose, write_costs, now) -> list[dict]:
    """并发算。

    每个线程必须有**自己的** Dao 连接：pymysql.Connection 不是线程安全的。
    Dao 内部用 threading.local 存连接，所以每个工作线程各自 new 一个 Dao，
    共享同一个 DSN 配置。
    """
    from concurrent.futures import ThreadPoolExecutor

    # 复用主线程的 DSN，但每个线程独立连接
    dsn = dict(dao._dsn)  # noqa: SLF001  同包内访问，可接受

    def work(row):
        from db.dao import Dao
        td = Dao(**dsn)
        try:
            return _analyze_one(td, row, write_hits, verbose, write_costs, now)
        finally:
            try:
                td.close()
            except Exception:
                pass

    with ThreadPoolExecutor(n, thread_name_prefix='analyze') as pool:
        return list(pool.map(work, todo))


def _persist_cost(dao, match_id: str, sport: str, kickoff, a: MatchAnalysis,
                  now, odds_updated) -> None:
    """把一次判定结果写入快照表，并在成本变化时追加一条流水。

    写失败不影响分析主流程。
    """
    cost = a.ilp.cost if a.ilp and a.ilp.cost else None
    try:
        # 先读上一行的成本，用于判断是否变化、并填 prev_cost。
        # 必须在 write_cost 之前读 —— 它一提交就把上一行的值覆盖掉了。
        prev = dao.get_cost(match_id)
        prev_cost = prev['cost'] if prev else None

        dao.write_cost(
            platform='kessbet',
            match_id=match_id,
            sport=sport,
            kickoff_time=kickoff,
            cost=cost,
            cost_scope='single',
            n_markets=0,                       # 由页面按需从 prematch 统计
            n_selections=a.n_options,
            n_candidates=a.n_candidates,
            ilp_chosen=(json.dumps(a.ilp.chosen, ensure_ascii=False)
                        if a.ilp else None),
            lp_worst_profit=(round(a.lp.worst_profit, 8)
                             if a.lp and a.lp.worst_profit is not None else None),
            lp_chosen=(json.dumps(
                [{'desc': d, 'weight': round(w, 8)} for d, w in a.lp.chosen],
                ensure_ascii=False) if a.lp else None),
            cross_gap=round(a.cross_gap, 10) if a.cross_gap is not None else None,
            error=a.error,
            odds_updated=odds_updated,
            computed_time=now,
        )

        # 只在成本真的变化时记流水。没有变化时写一行也没有意义 ——
        # 列表页要的是「一小时前是多少」，不是「每 2 秒是多少」。
        # 首次计算（prev_cost is None）也记一笔，否则新赛事要等到第二次
        # 变化才有历史，1h 变化会一直空着。
        if cost is not None and (prev_cost is None
                                 or abs(prev_cost - cost) > COST_EPS):
            dao.append_cost_history(
                platform='kessbet',
                match_id=match_id,
                cost=cost,
                prev_cost=prev_cost,
                odds_updated=odds_updated,
                changed_time=now,
            )
        dao.conn.commit()
    except Exception as e:
        log.warning('write_cost %s failed: %s', match_id, e)
        try:
            dao.conn.rollback()
        except Exception:
            pass


def run_loop(dao, interval: int | None = None,
             horizon_hours: int | None = None,
             concurrency: int | None = None,
             full_sweep_sec: int | None = None) -> None:
    """事件驱动 + 定时兜底。

    **事件驱动**：每 interval 秒查一次「赔率比快照新」的比赛，只算这些。
    赔率一动，最多 interval 秒后重算 —— 比原来的定时全量扫快得多，
    也不再有「扫 130 场只为了发现 2 场变了」的浪费。

    **兜底**：每 full_sweep_sec 秒忽略「没变」判断、强制全量算一遍。
    防的是快照表被误删、时钟漂移、或跳过条件写错导致某些场次永远不更新。

    这个循环不关心输进来的是轮询还是 WS —— 采集侧只要保证
    odds_prematch.updated_time 被正确更新，这里就能感知到。
    """
    interval = interval or getattr(config, 'ANALYZE_POLL_SEC', 2)
    full_sweep_sec = full_sweep_sec or getattr(config, 'ANALYZE_FULL_SWEEP_SEC', 300)

    last_sweep = 0.0
    log.info('analyze loop: poll=%ds horizon=%sh concurrency=%s full_sweep=%ds',
             interval, horizon_hours or config.ANALYZE_HORIZON_HOURS,
             concurrency or getattr(config, 'ANALYZE_CONCURRENCY', 4),
             full_sweep_sec)

    while True:
        t0 = time.time()
        force = (t0 - last_sweep) >= full_sweep_sec

        try:
            stats = analyze_once(dao, horizon_hours=horizon_hours,
                                 concurrency=concurrency,
                                 force=force)
            if force:
                last_sweep = t0
                log.info('full sweep done in %.1fs: %s', time.time() - t0, stats)
            elif stats['matches']:
                # 只有真有事可做才打日志，否则每 2 秒刷屏
                log.info('incremental: %d pending, done in %.1fs: %s',
                         stats['matches'], time.time() - t0, stats)
        except Exception as e:
            log.exception('analyze round failed: %s', e)

        time.sleep(max(0, interval - (time.time() - t0)))


def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
        datefmt='%H:%M:%S',
    )


def install_signal_handlers() -> None:
    """把 SIGTERM 转成 KeyboardInterrupt。

    deploy/stop.sh 发的是 SIGTERM，Python 默认不会把它变成
    KeyboardInterrupt，`except KeyboardInterrupt` 和 `finally` 都不会执行。
    转换后主循环能正常退出并关连接。
    """
    def _to_keyboard_interrupt(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _to_keyboard_interrupt)


def main() -> int:
    import argparse
    import sys

    from db.dao import Dao

    ap = argparse.ArgumentParser(description='KessBet 分析进程')
    ap.add_argument('--once', action='store_true', help='扫一轮就退出')
    ap.add_argument('--interval', type=int, default=None,
                    help='增量轮询间隔秒数，默认 %d' %
                         getattr(config, 'ANALYZE_POLL_SEC', 2))
    ap.add_argument('--horizon', type=int, default=None,
                    help=f'只看未来 N 小时开赛的比赛，默认 {config.ANALYZE_HORIZON_HOURS}')
    ap.add_argument('--concurrency', type=int, default=None,
                    help='并发线程数，默认 %d' %
                         getattr(config, 'ANALYZE_CONCURRENCY', 4))
    ap.add_argument('--full-sweep', type=int, default=None,
                    help='全量兜底间隔秒数，默认 %d' %
                         getattr(config, 'ANALYZE_FULL_SWEEP_SEC', 300))
    ap.add_argument('--force', action='store_true',
                    help='配 --once 使用：忽略「没变就跳过」，强制全部重算')
    ap.add_argument('--no-write', action='store_true', help='不写 hit 表')
    ap.add_argument('-v', '--verbose', action='store_true')
    args = ap.parse_args()

    setup_logging(args.verbose)
    install_signal_handlers()
    dao = Dao()
    try:
        if args.once:
            stats = analyze_once(dao, horizon_hours=args.horizon,
                                 write_hits=not args.no_write,
                                 verbose=args.verbose,
                                 concurrency=args.concurrency,
                                 force=args.force)
            log.info('single round: %s', stats)
        else:
            run_loop(dao, args.interval, horizon_hours=args.horizon,
                     concurrency=args.concurrency,
                     full_sweep_sec=args.full_sweep)
    except KeyboardInterrupt:
        log.info('interrupted')
    finally:
        dao.close()
    return 0


if __name__ == '__main__':
    import sys
    sys.exit(main())
