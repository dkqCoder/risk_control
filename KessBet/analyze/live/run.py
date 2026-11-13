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
from common.timeutil import now_beijing

log = logging.getLogger(__name__)

LIVE_REPLAY_SCHEMA_VERSION = 1
LIVE_ANALYZER_VERSION = 'live-result-b2-v2'


def evaluate_live_match(dao, meta: dict, now) -> dict:
    """对一场滚球比赛跑「比赛结果」判定。

    返回 {'match_id', 'result': dict|None, 'error': str|None}
    """
    mid = meta['match_id']
    out = {'match_id': mid, 'result': None, 'error': None,
           'ht': None, 'selections': None}

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

    # ★ 赔率必须比当前比分新。
    #
    # 进球后：状态轮询每 10 秒就更新了比分，但赔率要等平台下一次推送
    # 才重新定价。这中间拿「新比分」套「按旧比分定的赔率」，是两个世界
    # 的价格硬拼在一起 —— 必然算出假套利。
    #
    # 实测（2026-11-13，Atletico Morelia）：
    #   11:11:08  赔率最后一次更新（当时 0:0）
    #   11:11:58  分析命中 Σ=0.7465，此时状态已经是 1:0 第 50 分钟
    #   11:12:35  赔率才跟上进球（16 条变动）
    # 前后各查一次成本都正常，只有中间这 87 秒是错的。
    score_changed = meta.get('score_changed_time')
    if score_changed is not None and odds_updated < score_changed:
        out['error'] = 'score_newer_than_odds'
        return out

    # ★ 只取「跟得上本场最新采集」的选项。
    #
    # `odds_updated` 是 MAX(updated_time)，只要有一个盘口在推就是新鲜的，
    # 但同场的其他盘口可能已经停推几十分钟（实测同场新旧行差 2.3 小时）。
    # 拿这种混合数据算覆盖，会得出 Σ=0.05 的荒谬套利 —— 1 秒前的 1x2
    # 配 50 分钟前的 Handicap，那两个价格从来没同时存在过。
    #
    # 阈值取 120 秒：一次正常推送周期内所有选项都会一起刷新，
    # 超过 2 分钟没刷新的说明那次接口响应里就没有它了。
    sels = dao.get_live_selections(
        mid, max_lag_sec=getattr(config, 'LIVE_ROW_MAX_LAG_SEC', 120))
    if not sels:
        out['error'] = 'no_data'
        return out
    out['selections'] = sels

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


def analyze_live_snapshot(dao, meta: dict, selections: list[tuple], now=None) -> dict:
    """立即分析一次采集成功的完整滚球快照。"""
    # 判定总开关。它必须在这里也检查一遍 —— 采集侧的快照回调不受
    # analyze/run.py 里的 `LIVE_ANALYZE_ENABLED` 控制：
    # 那个开关只停掉了独立分析线程，采集器每收到一次推送仍会调这里。
    # 不挡的话「关掉判定」只关掉了一半。
    if not getattr(config, 'LIVE_ANALYZE_ENABLED', True):
        return {'result': None, 'error': 'analyze_disabled'}
    if meta.get('sport') != 'sr:sport:1':
        return {'result': None, 'error': 'unsupported_sport'}
    if now is None:
        now = now_beijing()
    minute = meta.get('minute')
    home = meta.get('home_score')
    away = meta.get('away_score')
    if home is None or away is None:
        return {'result': None, 'error': 'no_score'}

    ht = None
    if minute is not None and minute > 45:
        ht = result.half_time_score(dao.live_history_for_half_time(
            meta['match_id']))
    meta = dict(meta)
    meta['ht_home'] = ht[0] if ht else None
    meta['ht_away'] = ht[1] if ht else None

    r = result.check_match(
        selections, home, away, minute=minute,
        ht_home=ht[0] if ht else None,
        ht_away=ht[1] if ht else None,
    )
    if r is None:
        return {'result': None, 'error': 'no_cover_or_no_candidate', 'ht': ht}

    _persist_cost(dao, meta, r, ht, now)
    if r['is_arbitrage']:
        log.warning('LIVE ARBITRAGE(snapshot) %s 第%s分钟 %s:%s Σ=%.4f 收益 %.2f%%',
                    meta['match_id'], minute, home, away,
                    r['cost'], r['profit_pct'])
        if _persist_hit(dao, meta, r, now):
            _shoot_async(meta, r)
    return {'result': r, 'error': None, 'ht': ht}


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
        if err == 'score_newer_than_odds':
            stats['odds_behind_score'] = stats.get('odds_behind_score', 0) + 1
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
                    evidence_meta = dict(meta)
                    evidence_meta['ht_home'] = a['ht'][0] if a['ht'] else None
                    evidence_meta['ht_away'] = a['ht'][1] if a['ht'] else None
                    evidence_meta['snapshot_selections'] = a.get('selections') or []
                    if _persist_hit(dao, evidence_meta, r, now):
                        # 只有首次持久化成功的路径截图，避免即时分析和兜底扫描重复留证。
                        _shoot_async(meta, r)
                except Exception as e:
                    log.exception('写滚球命中快照失败 %s: %s',
                                  meta['match_id'], e)

    return stats


def _shoot_async(meta: dict, r: dict) -> None:
    """命中时异步截图留证。

    截**平台官网**的比赛页 —— 这是判断「这个套利到底真不真」的第一手证据：
    我们库里的赔率是采来的，平台页面才是最终事实。

        https://www.kessbet.com/match/<matchId>

    顺带也截一张本地详情页（能看到我们算出的下注组合），两张同名不同后缀。

    为什么要异步：每次截图要起一个 Chrome，启动 + 渲染 15~20 秒，
    而分析线程每 2 秒一轮。同步截图会把判定流程整个堵死。

    为什么要截图：滚球赔率秒级变动，命中那一刻的价格组合事后无法复现。
    """
    if not getattr(config, 'LIVE_SHOT_ENABLED', False):
        return

    import os
    import threading

    out_dir = getattr(config, 'LIVE_SHOT_DIR', '')
    mid = meta['match_id']
    stamp = now_beijing().strftime('%Y%m%d-%H%M%S')
    base_name = f'{stamp}_{mid}'

    def work():
        # 1) 平台官网
        try:
            from common import platform_shot
            ok, msg = platform_shot.capture_match(
                mid, os.path.join(out_dir, base_name + '_kessbet.png'))
            if ok:
                log.warning('平台截图已保存 %s', msg)
            else:
                log.warning('平台截图失败 %s: %s', mid, msg)
        except Exception as e:
            log.warning('平台截图异常 %s: %s', mid, e)

        # 2) 本地详情页（含我们算出的下注组合）
        try:
            from common import screenshot
            url = '%s/live/%s' % (
                getattr(config, 'LIVE_SHOT_BASE_URL', 'http://127.0.0.1:8080'),
                mid)
            ok, msg = screenshot.capture(
                url, os.path.join(out_dir, base_name + '_local.png'))
            if ok:
                log.warning('本地截图已保存 %s', msg)
        except Exception as e:
            log.warning('本地截图异常 %s: %s', mid, e)

    try:
        threading.Thread(target=work, daemon=True,
                         name='arb-shot').start()
    except Exception as e:
        log.warning('启动截图线程失败: %s', e)


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


def _persist_hit(dao, meta: dict, r: dict, now) -> bool:
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
        'source_snapshot_id': meta.get('snapshot_id'),
        'source_snapshot': [
            {'market': m, 'side': s, 'line': line,
             'label': label, 'odds': round(float(odds), 3)}
            for m, s, line, label, odds
            in meta.get('snapshot_selections', [])
        ],
    }]
    descs = [describe_pick(d, home, away) for d, _, _ in chosen]
    replay_context = {
        'schema_version': LIVE_REPLAY_SCHEMA_VERSION,
        'analyzer_version': LIVE_ANALYZER_VERSION,
        'snapshot_id': meta.get('snapshot_id'),
        'minute': meta.get('minute'),
        'home_score': meta.get('home_score'),
        'away_score': meta.get('away_score'),
        'ht_home': meta.get('ht_home'),
        'ht_away': meta.get('ht_away'),
        'match_status': meta.get('match_status'),
        'match_clock': meta.get('match_clock'),
        'first_half_live': (
            meta.get('minute') is not None and meta.get('minute') <= 45
            and meta.get('ht_home') is None
        ),
        'reach_mask_hex': hex(r.get('reach_mask', 0)),
        'reach_count': r.get('reach'),
        'expected': {
            'cost': r.get('cost'),
            'chosen': r.get('chosen'),
            'n_candidates': r.get('n_candidates'),
            'is_arbitrage': r.get('is_arbitrage'),
        },
        'selections': [list(x) for x in meta.get('snapshot_selections', [])],
    }

    inserted = dao.write_arbitrage_snapshot(
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
        replay_context=json.dumps(replay_context, ensure_ascii=False),
        odds_updated=meta.get('odds_updated'),
        alert_time=now,
        is_live=1,
    )
    if inserted:
        log.warning('滚球命中已保存 %s  (Σ=%.4f, 可达终局 %d)',
                    mid, r['cost'], r['reach'])
    return inserted


def replay_live_evidence(replay_context) -> dict:
    """用命中时保存的输入重跑滚球判定，并与历史结果逐项比对。"""
    if isinstance(replay_context, str):
        replay_context = json.loads(replay_context)
    if not replay_context:
        return {'ok': False, 'error': 'no_replay_context'}

    selections = [tuple(x) for x in replay_context.get('selections', [])]
    replayed = result.check_match(
        selections,
        replay_context.get('home_score'),
        replay_context.get('away_score'),
        minute=replay_context.get('minute'),
        ht_home=replay_context.get('ht_home'),
        ht_away=replay_context.get('ht_away'),
        first_half_live=replay_context.get('first_half_live'),
    )
    expected = replay_context.get('expected') or {}
    if replayed is None:
        return {'ok': False, 'error': 'replay_no_result',
                'expected': expected, 'replayed': None}

    expected_chosen = sorted(tuple(x) for x in expected.get('chosen', []))
    replayed_chosen = sorted(tuple(x) for x in replayed.get('chosen', []))
    checks = {
        'cost': abs(float(replayed['cost']) - float(expected.get('cost'))) < 1e-9,
        'chosen': replayed_chosen == expected_chosen,
        'n_candidates': replayed.get('n_candidates') == expected.get('n_candidates'),
        'is_arbitrage': replayed.get('is_arbitrage') == expected.get('is_arbitrage'),
        'reach_mask': hex(replayed.get('reach_mask', 0)) == replay_context.get('reach_mask_hex'),
    }
    conclusion_checks = {k: v for k, v in checks.items() if k != 'chosen'}
    return {
        # 并列最优时 chosen 可能不同，但只要成本、结论和状态空间一致，
        # 历史套利结论仍然被成功复现。
        'ok': all(conclusion_checks.values()),
        'exact_match': all(checks.values()),
        'alternative_optimum': (
            all(conclusion_checks.values()) and not checks['chosen']),
        'schema_version': replay_context.get('schema_version'),
        'saved_analyzer_version': replay_context.get('analyzer_version'),
        'current_analyzer_version': LIVE_ANALYZER_VERSION,
        'checks': checks,
        'expected': expected,
        'replayed': replayed,
    }


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

    from common import logsetup
    logsetup.setup('analyze-live', args.verbose)

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
