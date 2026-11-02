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
import time
from datetime import datetime, timezone

from analyze import payout, solver
from analyze.covers import soccer
from collect import config

log = logging.getLogger(__name__)

# ILP 与 LP 结果允许的最大偏差。
# 两者理论等价，超过这个值说明有一边的实现有问题。
CROSS_CHECK_TOL = 1e-4


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
    # ILP 成本 C 和 LP 最差收益 t 应满足 t = 1/C - 1
    if result.ilp.cost:
        expected = 1.0 / result.ilp.cost - 1.0
        result.cross_gap = abs(result.lp.worst_profit - expected)
        result.cross_ok = result.cross_gap <= CROSS_CHECK_TOL

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
    if a.cross_ok:
        lines.append('  ✓ 两模型一致 (偏差 %.2e)' % a.cross_gap)
    else:
        lines.append('  ✗ 两模型不一致! 偏差 %.6f' % a.cross_gap)
        expected = 1.0 / ilp.cost - 1.0 if ilp.cost else None
        if expected is not None:
            lines.append('    ILP 隐含最差收益 %.6f，LP 实际 %.6f' % (
                expected, lp.worst_profit))

    lines.append('')
    lines.append('【判定】%s' % ('存在套利' if a.is_arbitrage else '无套利'))
    return '\n'.join(lines)


def analyze_once(dao, horizon_hours: int | None = None,
                 write_hits: bool = True,
                 verbose: bool = False) -> dict:
    """扫一轮。"""
    horizon = horizon_hours or config.ANALYZE_HORIZON_HOURS
    stats = {
        'matches': 0, 'evaluated': 0, 'hits': 0,
        'no_cover': 0, 'skipped_sport': 0, 'errors': 0,
        'cross_fail': 0, 'min_cost': None,
    }

    for match_id, sport, home, away, kickoff in dao.upcoming(horizon):
        stats['matches'] += 1
        a = evaluate_match(dao, match_id, sport, home, away, kickoff)

        if a.error == 'unsupported_sport':
            stats['skipped_sport'] += 1
            continue
        if a.error:
            if a.error == 'no_cover':
                stats['no_cover'] += 1
            else:
                stats['errors'] += 1
            if verbose:
                log.info(format_payout_table(a))
            continue

        stats['evaluated'] += 1

        cost = a.best_cost
        if cost is not None:
            if stats['min_cost'] is None or cost < stats['min_cost']:
                stats['min_cost'] = cost

        # 交叉验证失败 —— 建模或求解有问题，必须告警
        if not a.cross_ok:
            stats['cross_fail'] += 1
            log.error('CROSS-CHECK FAIL %s %s vs %s: ILP=%.6f LP_worst=%.6f gap=%.6f',
                      match_id, home, away, cost, a.lp.worst_profit, a.cross_gap)

        if verbose:
            log.info(format_payout_table(a))

        if a.is_arbitrage:
            stats['hits'] += 1
            log.warning('ARBITRAGE %s %s vs %s cost=%.6f worst_profit=%.6f',
                        match_id, home, away, cost, a.lp.worst_profit)
            log.warning('\n%s', format_payout_table(a))
            if write_hits:
                detail = {
                    'ilp_chosen': a.ilp.chosen,
                    'lp_chosen': [{'desc': d, 'weight': w} for d, w in a.lp.chosen],
                    'worst_profit': a.lp.worst_profit,
                    'cross_gap': a.cross_gap,
                }
                dao.write_hit(match_id, sport, cost,
                              json.dumps(detail, ensure_ascii=False),
                              kickoff)

    return stats


def run_loop(dao, interval: int | None = None) -> None:
    """阻塞运行，每轮扫一次。"""
    interval = interval or config.ANALYZE_INTERVAL
    while True:
        t0 = time.time()
        try:
            stats = analyze_once(dao)
            log.info('round done in %.1fs: %s', time.time() - t0, stats)
        except Exception as e:
            log.exception('analyze round failed: %s', e)
        time.sleep(max(0, interval - (time.time() - t0)))


def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
        datefmt='%H:%M:%S',
    )


def main() -> int:
    import argparse
    import sys

    from db.dao import Dao

    ap = argparse.ArgumentParser(description='KessBet 分析进程')
    ap.add_argument('--once', action='store_true', help='扫一轮就退出')
    ap.add_argument('--interval', type=int, default=None,
                    help=f'每轮间隔秒数，默认 {config.ANALYZE_INTERVAL}')
    ap.add_argument('--horizon', type=int, default=None,
                    help=f'只看未来 N 小时开赛的比赛，默认 {config.ANALYZE_HORIZON_HOURS}')
    ap.add_argument('--no-write', action='store_true', help='不写 hit 表')
    ap.add_argument('-v', '--verbose', action='store_true')
    args = ap.parse_args()

    setup_logging(args.verbose)
    dao = Dao()
    try:
        if args.once:
            stats = analyze_once(dao, horizon_hours=args.horizon,
                                 write_hits=not args.no_write,
                                 verbose=args.verbose)
            log.info('single round: %s', stats)
        else:
            run_loop(dao, args.interval)
    except KeyboardInterrupt:
        log.info('interrupted')
    finally:
        dao.close()
    return 0


if __name__ == '__main__':
    import sys
    sys.exit(main())
