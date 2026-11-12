"""analyze/live/run.py — 滚球套利判定主循环（比赛结果盘）。

与 analyze/run.py（赛前）的分工：
  analyze/run.py   赛前：未开赛比赛，2025 状态全覆盖，ILP + LP 交叉验证
  analyze/live/run 滚球：进行中比赛，可达终局子集，只有 ILP

判定逻辑在 result.py，这里只负责：
  读库 -> 调判定 -> 写快照 / 成本 / 命中

## 为什么是「比赛结果盘」而不是「时间窗口盘」

前一版做的是时间窗口盘（`N Minutes - X From A To B`），搞完了 59,475 组
完整盘口、0 命中。那类盘口本身没什么可看的，而且把角球/牌混了进来。

现在做的是整场比赛的终局覆盖：1x2 / Total / 让球 / 正确比分 / 半场盘
组合起来，看能不能以 Σ(1/赔率) < 1 覆盖所有可达终局。
"""
from __future__ import annotations

import json
import logging
import signal
import time

from analyze.live import result
from collect import config

log = logging.getLogger(__name__)


def evaluate_live_match(dao, meta: dict, now) -> dict:
    """对一场滚球比赛跑「比赛结果」判定。

    返回 {'match_id', 'result': dict|None, 'error': str|None}
    """
    mid = meta['match_id']
    out = {'match_id': mid, 'result': None, 'error': None,
           'ht': None}

    # ★ 赔率新鲜度：过期时**仍然重算**，只是不告警。
    #
    # 为什么不能直接跳过（踩过一次，2026-11-12 APIA）：
    #   18:20:46 算出 cost=1.1073，选中 Total H 4.5 over/under
    #   18:21 解析器修复后，4.5 被识别为锁定盘口，从库中删除
    #   但赔率超过 90 秒没更新 → stale_odds 一直跳过重算
    #   → 结果里永远留着已经不存在的 4.5 组合，页面显示一个幽灵套利
    #
    # 正确做法：结果必须永远反映库内当前数据。
    # 过期只影响「要不要当成机会告警」，不影响「算出真实成本」。
    odds_updated = meta.get('odds_updated')
    if odds_updated is None:
        out['error'] = 'no_odds_ts'
        return out
    age = (now - odds_updated).total_seconds()
    out['is_stale'] = age > config.LIVE_ODDS_MAX_AGE_SEC

    sels = dao.get_live_selections(mid)
    if not sels:
        out['error'] = 'no_data'
        return out

    minute = meta.get('minute')
    home = meta.get('home_score')
    away = meta.get('away_score')
    if home is None or away is None:
        out['error'] = 'no_score'
        return out

    # 下半场需要半场比分。从赔率流水反推（不看 min 也没关系，
    # fetch_state 的 msScoreList 不可靠，见 result.half_time_score 的说明）
    ht = None
    if minute is not None and minute > 45:
        rows = dao.live_history_for_half_time(mid)
        ht = result.half_time_score(rows)
    out['ht'] = ht

    r = result.check_match(
        sels, home, away, minute=minute,
        ht_home=ht[0] if ht else None,
        ht_away=ht[1] if ht else None,
    )
    if r is None:
        out['error'] = 'no_cover_or_no_candidate'
        return out

    out['result'] = r
    return out


def analyze_live_once(dao, window_min: int | None = None,
                      write_hits: bool = True,
                      write_costs: bool = True,
                      force: bool = False) -> dict:
    """扫一轮滚球比赛。

    force=False: 只算赔率比上次计算新的（live_analyze_pending）。
    force=True:  全量扫（主循环每秒轮询后的兜底）。
    """
    from datetime import datetime, timedelta, timezone
    BJ = timezone(timedelta(hours=8))
    now = datetime.now(timezone.utc).astimezone(BJ).replace(tzinfo=None)

    stats = {'matches': 0, 'evaluated': 0, 'hits': 0,
             'errors': 0, 'no_data': 0, 'no_score': 0,
             'no_cover': 0, 'min_cost': None}

    stats.setdefault('stale_odds', 0)

    ws = window_min or config.LIVE_WINDOW_MIN
    targets = (dao.live_analyze_targets(ws) if force
               else dao.live_analyze_pending(ws))
    stats['matches'] = len(targets)

    for meta in targets:
        try:
            a = evaluate_live_match(dao, meta, now)
        except Exception as e:
            log.exception('live analyze %s failed: %s', meta['match_id'], e)
            stats['errors'] += 1
            continue

        err = a['error']
        if err == 'no_data':
            stats['no_data'] += 1
            continue
        if err == 'no_score':
            stats['no_score'] += 1
            continue
        if err == 'no_cover_or_no_candidate':
            stats['no_cover'] += 1
            continue
        if err:
            stats['errors'] += 1
            continue

        r = a['result']
        stats['evaluated'] += 1
        if a.get('is_stale'):
            stats['stale_odds'] += 1
        if stats['min_cost'] is None or r['cost'] < stats['min_cost']:
            stats['min_cost'] = r['cost']

        # 成本永远写 —— 保证结果反映库内当前数据。
        # 赔率过期只影响「要不要告警」，不影响「算出来的成本是真是假」。
        if write_costs:
            try:
                _persist_cost(dao, meta, r, a['ht'], now)
            except Exception as e:
                log.exception('写滚球结果成本失败 %s: %s', meta['match_id'], e)

        # 过期数据算出的套利不告警：赔率可能早就不存在了，
        # 那是个幽灵机会（实测 Thailand 0:2 曾经报过 Σ=0.9984）。
        if r['is_arbitrage'] and not a.get('is_stale'):
            stats['hits'] += 1
            log.warning('LIVE ARBITRAGE %s %s vs %s  第%s分钟 %s:%s  '
                        'Σ=%.4f  收益 %.2f%%',
                        meta['match_id'], meta.get('home_team'),
                        meta.get('away_team'), meta.get('minute'),
                        meta.get('home_score'), meta.get('away_score'),
                        r['cost'], r['profit_pct'])
            for d, o, w in result.stakes(r['chosen'], r['cost']):
                log.warning('    %-34s @ %.2f  下注 %.1f%%', d, o, w * 100)
            if write_hits:
                try:
                    _persist_hit(dao, meta, r, now)
                except Exception as e:
                    log.exception('写滚球命中快照失败 %s: %s',
                                  meta['match_id'], e)

    return stats


def _persist_cost(dao, meta: dict, r: dict, ht, now) -> None:
    """写结果成本（一场一行，供列表页只读）。"""
    chosen = [{'desc': d, 'odds': o, 'weight': round(w, 6)}
              for d, o, w in result.stakes(r['chosen'], r['cost'])]
    dao.write_live_result_cost(
        match_id=meta['match_id'],
        kickoff_time=meta['kickoff_time'],
        cost=r['cost'],
        profit_pct=r['profit_pct'],
        chosen=json.dumps(chosen, ensure_ascii=False),
        reach_states=r['reach'],
        n_candidates=r['n_candidates'],
        minute=meta.get('minute'),
        home_score=meta.get('home_score'),
        away_score=meta.get('away_score'),
        ht_home=ht[0] if ht else None,
        ht_away=ht[1] if ht else None,
        odds_updated=meta.get('odds_updated'),
        computed_time=now,
    )


def _persist_hit(dao, meta: dict, r: dict, now) -> None:
    """把命中写进 kessbet_arbitrage_snapshot（is_live=1）。

    和赛前命中共用一张表：事后要从一个地方回溯「都命中过什么」。
    """
    from common.markets import describe_pick

    mid = meta['match_id']
    chosen = result.stakes(r['chosen'], r['cost'])
    home, away = meta.get('home_team') or '', meta.get('away_team') or ''

    detail = [{
        'name': '比赛结果（滚球）',
        'base': 'live_result',
        'side': '',
        'selections': [{'line': '', 'label': d, 'odds': round(o, 3)}
                       for d, o, _ in chosen],
    }]
    descs = [describe_pick(d, home, away) for d, _, _ in chosen]

    dao.write_arbitrage_snapshot(
        match_id=mid,
        sport=meta.get('sport') or 'sr:sport:1',
        home=home, away=away,
        league=meta.get('league') or '',
        canonical_id=f'local:kessbet:{mid}',
        kickoff_time=meta['kickoff_time'],
        cost=r['cost'],
        cost_scope='single',
        lp_worst_profit=None,
        cross_gap=None,
        ilp_chosen=json.dumps(descs, ensure_ascii=False),
        lp_chosen=json.dumps(
            [{'desc': d, 'weight': round(w, 6)} for d, _, w in chosen],
            ensure_ascii=False),
        n_markets=len({d.split('|')[0] for d, _ in r['chosen']}),
        n_selections=len(r['chosen']),
        n_candidates=r['n_candidates'],
        market_detail=json.dumps(detail, ensure_ascii=False),
        odds_updated=meta.get('odds_updated'),
        alert_time=now,
        is_live=1,
    )
    log.warning('滚球命中已保存 %s  (Σ=%.4f, 可达终局 %d)',
                mid, r['cost'], r['reach'])


def run_loop(dao, interval: int | None = None,
             window_min: int | None = None) -> None:
    """滚球判定主循环。

    间隔比赛前（2 秒）宽得多：滚球的结果盘机会来自平台定价失误，
    不是毫秒级抢跑。
    """
    interval = interval or getattr(config, 'LIVE_ANALYZE_SEC', 10)
    log.info('live analyze loop: interval=%ds window=%dmin',
             interval, window_min or config.LIVE_WINDOW_MIN)

    while True:
        t0 = time.time()
        try:
            stats = analyze_live_once(dao, window_min=window_min)
            if stats['evaluated'] or stats['hits']:
                log.info('live round: %s', stats)
        except Exception as e:
            log.exception('live analyze round failed: %s', e)
        time.sleep(max(1, interval - (time.time() - t0)))


def main() -> int:
    import argparse
    import sys

    from db.dao import Dao

    ap = argparse.ArgumentParser(description='滚球比赛结果套利判定')
    ap.add_argument('--once', action='store_true', help='扫一轮就退出')
    ap.add_argument('--interval', type=int, default=None,
                    help='轮询间隔秒数，默认 10')
    ap.add_argument('--window', type=int, default=None,
                    help=f'只看开赛后 N 分钟内的比赛，默认 {config.LIVE_WINDOW_MIN}')
    ap.add_argument('--no-write', action='store_true', help='只报不写')
    ap.add_argument('-v', '--verbose', action='store_true')
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
        datefmt='%H:%M:%S',
    )

    def _sigterm(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, _sigterm)

    dao = Dao()
    try:
        if args.once:
            stats = analyze_live_once(dao, window_min=args.window,
                                      write_hits=not args.no_write,
                                      write_costs=not args.no_write)
            log.info('single round: %s', stats)
        else:
            run_loop(dao, args.interval, args.window)
    except KeyboardInterrupt:
        log.info('interrupted')
    finally:
        dao.close()
    return 0


if __name__ == '__main__':
    import sys
    sys.exit(main())
