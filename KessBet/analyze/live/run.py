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

import hashlib
import json
import logging
import os
import signal
import time

from analyze.live import result
from collect import config
from common.timeutil import now_beijing

log = logging.getLogger(__name__)

LIVE_REPLAY_SCHEMA_VERSION = 2
# D05：other 隔离、比分 guard、ghost 成本剔除都改变了结果，必须升级版本。
LIVE_ANALYZER_VERSION = 'live-result-b3-v12'
LIVE_STATE_SPACE_VERSION = 'soccer-h6-g10'
LIVE_CANDIDATE_POLICY_VERSION = 'expanded-final-score-no-other/v2'


def _effective_ht(obj: dict) -> tuple[int | None, int | None]:
    """只有权威来源的半场比分才能用于缩小认证结果域。

    v1 没有 official_state 半场来源，因此实际总是返回 (None, None)。
    """
    if (obj or {}).get('ht_source') != 'official_state':
        return None, None
    return (obj or {}).get('ht_home'), (obj or {}).get('ht_away')


def _persist_scope_reports(dao, meta: dict, period: dict,
                           windows: list[dict], events: list[dict],
                           first_half: dict | None = None,
                           independent: list[dict] | None = None,
                           corners: dict | None = None,
                           count_windows: list[dict] | None = None,
                           first_half_events: list[dict] | None = None,
                           first_half_joint: list[dict] | None = None) -> None:
    """独立分析只写影子记录，按输入快照绑定可回溯结果。"""
    snapshot_id = meta.get('snapshot_id')
    if not snapshot_id or not hasattr(dao, 'save_live_scope_analysis'):
        return
    reports = []
    if first_half and first_half.get('source_selections'):
        reports.append(('first_half', first_half))
    if corners and corners.get('source_selections'):
        reports.append(('full_time_corners', corners))
    if period.get('source_selections') or meta.get('phase') == 'halftime':
        reports.append(('second_half', period))
    for report in windows:
        start, end = report['window']
        reports.append((f'window_{start}_{end}', report))
    for report in count_windows or []:
        start, end = report['window']
        reports.append((f"{report['domain']}_window_{start}_{end}", report))
    for report in events:
        reports.append((f"goal_{report['scope'].split()[0]}", report))
    for report in first_half_events or []:
        reports.append((f"first_half_goal_{report['ordinal']}", report))
    for report in first_half_joint or []:
        reports.append((f"first_half_1x2_goal_{report['ordinal']}", report))
    for report in independent or []:
        scope_key = {
            'Which Team Wins The Rest Of The Match': 'rest_of_match',
            'Booking 1x2': 'booking_1x2',
        }[report['scope']]
        reports.append((scope_key, report))
    for scope_key, report in reports:
        try:
            dao.save_live_scope_analysis(snapshot_id, meta['match_id'],
                                         scope_key, report)
        except Exception as exc:
            log.warning('时段影子结果落表失败 %s/%s: %s',
                        meta['match_id'], scope_key, exc)


def evaluate_live_match(dao, meta: dict, now) -> dict:
    """对一场滚球比赛跑「比赛结果」判定。

    返回 {'match_id', 'result': dict|None, 'error': str|None}
    """
    mid = meta['match_id']
    out = {'match_id': mid, 'result': None, 'error': None,
           'ht': None, 'selections': None}

    # ★ D01：分析输入只能是**不可变快照**。
    #
    # 不再用 MAX(updated_time) + 当前赔率表 + 时间窗口拼输入，也不再用
    # get_live_raw_near 按 15 秒猜 raw。快照里 row/state/raw 来自同一次
    # HTTP 请求，按 snapshot_id 精确读取；当前指针变了也不会改变本次输入。
    snapshot_id = meta.get('current_snapshot_id')
    if not snapshot_id:
        out['error'] = 'no_snapshot'
        return out
    snap = dao.get_live_snapshot(mid, snapshot_id)
    if snap is None:
        out['error'] = 'snapshot_missing'
        return out

    out['snapshot_id'] = snapshot_id
    out['generation'] = snap.get('generation')
    odds_updated = snap.get('response_received_at')
    if odds_updated is None:
        out['error'] = 'no_odds_ts'
        return out
    out['odds_updated'] = odds_updated

    # ★ 赔率新鲜度：过期时仍然重算，只是不告警。
    age = (now - odds_updated).total_seconds()
    out['is_stale'] = age > config.LIVE_ODDS_MAX_AGE_SEC

    sels = snap.get('rows') or []
    if not sels:
        out['error'] = 'no_data'
        return out
    out['selections'] = sels
    from analyze.live.period import (check_first_half_live,
                                     check_first_half_nth_goals,
                                     check_first_half_result_goal_joint,
                                     check_corner_full_time,
                                     check_future_count_windows,
                                     check_independent_three_way,
                                     check_second_half_at_halftime,
                                     check_future_windows,
                                     check_future_nth_goals)
    out['scope_reports'] = (
        check_second_half_at_halftime(snap),
        check_future_windows(snap),
        check_future_nth_goals(snap),
        check_first_half_live(snap),
        check_independent_three_way(snap),
        check_corner_full_time(snap),
        check_future_count_windows(snap),
        check_first_half_nth_goals(snap),
        check_first_half_result_goal_joint(snap),
    )
    out['phase'] = snap.get('phase')
    out['raw'] = {
        'payload': snap.get('raw_payload'),
        'fetched_time': odds_updated,
    }

    minute = snap.get('minute')
    home = snap.get('home_score')
    away = snap.get('away_score')
    if home is None or away is None:
        out['error'] = 'no_score'
        return out

    # 比分接近状态空间上限（每队 9 球）时，模型无法表示再进一球的终局。
    # 这是临时防线；正式修复是无界反例验证（F01）。
    if result.score_space_incomplete(home, away):
        out['error'] = 'score_near_model_cap'
        return out

    # 半场比分用快照冻结值；但只有权威来源才允许缩小认证结果域。
    ht_home, ht_away = _effective_ht(snap)
    out['ht_home'] = ht_home
    out['ht_away'] = ht_away
    out['ht_source'] = snap.get('ht_source')
    out['phase'] = snap.get('phase')
    out['phase_source'] = snap.get('phase_source')
    out['ht'] = ((ht_home, ht_away)
                 if ht_home is not None and ht_away is not None else None)
    state_after = snap.get('state_after') or {}
    out['minute'] = minute
    out['home_score'] = home
    out['away_score'] = away
    out['match_status'] = (state_after.get('ms')
                           if isinstance(state_after, dict) else None)
    out['match_clock'] = (state_after.get('mt')
                          if isinstance(state_after, dict) else None)

    r = result.check_match(
        sels, home, away, minute=minute,
        ht_home=ht_home,
        ht_away=ht_away,
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
    from analyze.live.period import (check_first_half_live,
                                     check_first_half_nth_goals,
                                     check_first_half_result_goal_joint,
                                     check_corner_full_time,
                                     check_future_count_windows,
                                     check_independent_three_way,
                                     check_second_half_at_halftime,
                                     check_future_windows,
                                     check_future_nth_goals)
    period_shadow = check_second_half_at_halftime(
        dict(meta, rows=selections))
    window_shadow = check_future_windows(dict(meta, rows=selections))
    event_shadow = check_future_nth_goals(dict(meta, rows=selections))
    first_half_shadow = check_first_half_live(dict(meta, rows=selections))
    independent_shadow = check_independent_three_way(
        dict(meta, rows=selections))
    corner_shadow = check_corner_full_time(dict(meta, rows=selections))
    count_window_shadow = check_future_count_windows(
        dict(meta, rows=selections))
    first_half_events = check_first_half_nth_goals(
        dict(meta, rows=selections))
    first_half_joint = check_first_half_result_goal_joint(
        dict(meta, rows=selections))
    _persist_scope_reports(dao, meta, period_shadow, window_shadow,
                           event_shadow, first_half_shadow,
                           independent_shadow, corner_shadow,
                           count_window_shadow, first_half_events,
                           first_half_joint)
    if period_shadow['status'] == 'shadow_candidate':
        log.info('下半场独立影子候选 %s snapshot=%s',
                 meta['match_id'], meta.get('snapshot_id'))
    if result.score_space_incomplete(home, away):
        return {'result': None, 'error': 'score_near_model_cap',
                'period_shadow': period_shadow,
                'window_shadow': window_shadow,
                'event_shadow': event_shadow}

    # 半场比分由采集侧随快照一起传入；仅权威来源才用于缩小结果域。
    ht_home, ht_away = _effective_ht(meta)
    ht = ((ht_home, ht_away)
          if ht_home is not None and ht_away is not None else None)
    meta = dict(meta)
    meta['ht_home'] = ht_home
    meta['ht_away'] = ht_away
    r = result.check_match(
        selections, home, away, minute=minute,
        ht_home=ht_home,
        ht_away=ht_away,
    )
    if r is None:
        # 新快照没有覆盖结果时也要把当前状态写下来，覆盖旧机会。
        _persist_status(dao, meta, 'no_cover', now)
        return {'result': None, 'error': 'no_cover_or_no_candidate',
                'ht': ht, 'period_shadow': period_shadow,
                'window_shadow': window_shadow,
                'event_shadow': event_shadow}

    shadow = bool(getattr(config, 'LIVE_SHADOW_MODE', True))
    _persist_cost(dao, meta, r, ht, now, shadow=shadow)
    if r['is_arbitrage']:
        tag = 'SHADOW CANDIDATE(snapshot)' if shadow else 'LIVE ARBITRAGE(snapshot)'
        log.warning('%s %s 第%s分钟 %s:%s Σ=%.4f 收益 %.2f%%',
                    tag, meta['match_id'], minute, home, away,
                    r['cost'], r['profit_pct'])
        if shadow:
            snap_id = _persist_hit(dao, meta, r, now, shadow=True)
            if not snap_id:
                log.warning('影子候选落表失败（证据包不完整或重复）%s',
                            meta['match_id'])
            else:
                # 影子命中也要截图：影子模式的作用就是验证完整流程，
                # 不截图就验证不了截图功能本身。
                _shoot_async(meta, r, snap_id)
        else:
            snap_id = _persist_hit(dao, meta, r, now)
            if snap_id:
                _shoot_async(meta, r, snap_id)
            else:
                # 证据包失败：撤销 verified_candidate，保留可重试原因。
                _persist_status(dao, meta, 'evidence_failed', now)
    return {'result': r, 'error': None, 'ht': ht,
            'period_shadow': period_shadow,
            'window_shadow': window_shadow,
            'event_shadow': event_shadow}


def analyze_live_once(dao, window_min: int | None = None,
                      write_hits: bool = True,
                      write_costs: bool = True,
                      force: bool = False,
                      diagnostic: bool = False) -> dict:
    """扫一轮滚球比赛。

    force=False: 只算赔率比上次计算新的（live_analyze_pending）。
    force=True:  全量扫（主循环每秒轮询后的兜底）。

    diagnostic=True 只在判定关闭时使用：允许计算，但强制不写正式结果
    和命中。所有正式写库入口都必须先通过 LIVE_ANALYZE_ENABLED。
    """
    if not getattr(config, 'LIVE_ANALYZE_ENABLED', True):
        if not diagnostic:
            log.info('滚球判定已关闭（LIVE_ANALYZE_ENABLED=False），'
                     '跳过分析；诊断请用 diagnostic/--no-write')
            return {'disabled': True, 'matches': 0, 'evaluated': 0,
                    'hits': 0, 'errors': 0, 'no_data': 0, 'no_score': 0,
                    'no_cover': 0, 'min_cost': None}
        # 诊断模式：只算不写。
        write_hits = False
        write_costs = False

    shadow = bool(getattr(config, 'LIVE_SHADOW_MODE', True))
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

        if write_costs and a.get('scope_reports'):
            (period_report, window_reports, event_reports,
             first_half_report, independent_reports,
             corner_report, count_window_reports,
             first_half_event_reports,
             first_half_joint_reports) = a['scope_reports']
            _persist_scope_reports(
                dao, {'snapshot_id': a.get('snapshot_id'),
                      'match_id': meta['match_id'],
                      'phase': a.get('phase')},
                period_report, window_reports, event_reports,
                first_half_report, independent_reports,
                corner_report, count_window_reports,
                first_half_event_reports,
                first_half_joint_reports)

        err = a['error']
        if err == 'no_data':
            stats['no_data'] += 1
            if write_costs:
                _persist_status(dao, meta, 'no_data', now, a)
            continue
        if err == 'no_score':
            stats['no_score'] += 1
            if write_costs:
                _persist_status(dao, meta, 'no_score', now, a)
            continue
        if err == 'score_newer_than_odds':
            stats['odds_behind_score'] = stats.get('odds_behind_score', 0) + 1
            continue
        if err == 'score_near_model_cap':
            stats['model_cap'] = stats.get('model_cap', 0) + 1
            if write_costs:
                _persist_status(dao, meta, 'incomplete_model', now, a)
            continue
        if err == 'no_cover_or_no_candidate':
            stats['no_cover'] += 1
            if write_costs:
                _persist_status(dao, meta, 'no_cover', now, a)
            continue
        if err:
            stats['errors'] += 1
            continue

        r = a['result']
        # 结果/命中使用的比分、分钟、半场、阶段全部来自同一快照，
        # 不能用 live_analyze_targets 里的当前值。
        snap_meta = dict(meta)
        for k in ('minute', 'home_score', 'away_score',
                  'ht_home', 'ht_away', 'match_status', 'match_clock',
                  'phase', 'phase_source', 'ht_source',
                  'snapshot_id', 'generation', 'odds_updated',
                  'input_fingerprint'):
            if a.get(k) is not None:
                snap_meta[k] = a[k]

        stats['evaluated'] += 1
        if a.get('is_stale'):
            stats['stale_odds'] += 1
        if stats['min_cost'] is None or r['cost'] < stats['min_cost']:
            stats['min_cost'] = r['cost']

        # 成本永远写 —— 保证结果反映库内当前数据。
        # 赔率过期只影响「要不要告警」，不影响「算出来的成本是真是假」。
        if write_costs:
            try:
                _persist_cost(dao, snap_meta, r, a.get('ht'), now,
                              shadow=shadow)
            except Exception as e:
                log.exception('写滚球结果成本失败 %s: %s', meta['match_id'], e)

        # 过期数据算出的套利不告警：赔率可能早就不存在了，
        # 那是个幽灵机会（实测 Thailand 0:2 曾经报过 Σ=0.9984）。
        if r['is_arbitrage'] and not a.get('is_stale'):
            stats['hits'] += 1
            tag = 'SHADOW CANDIDATE' if shadow else 'LIVE ARBITRAGE'
            log.warning('%s %s %s vs %s  第%s分钟 %s:%s  '
                        'Σ=%.4f  收益 %.2f%%',
                        tag,
                        snap_meta['match_id'], snap_meta.get('home_team'),
                        snap_meta.get('away_team'), snap_meta.get('minute'),
                        snap_meta.get('home_score'),
                        snap_meta.get('away_score'),
                        r['cost'], r['profit_pct'])
            for d, o, w in result.stakes(r['chosen'], r['cost']):
                log.warning('    %-34s @ %.2f  下注 %.1f%%', d, o, w * 100)
            if shadow:
                stats['shadow_hits'] = stats.get('shadow_hits', 0) + 1
            if write_hits:
                try:
                    evidence_meta = dict(snap_meta)
                    evidence_meta['snapshot_selections'] = a.get('selections') or []
                    # D01：证据必须绑定输入快照，不能用「当前」或时间猜。
                    evidence_meta['snapshot_id'] = (
                        a.get('snapshot_id') or meta.get('current_snapshot_id'))
                    evidence_meta['generation'] = (
                        a.get('generation') or meta.get('generation') or 0)
                    evidence_meta['odds_updated'] = (
                        a.get('odds_updated') or meta.get('odds_updated'))
                    raw = a.get('raw') or {}
                    if raw.get('payload'):
                        evidence_meta['raw_payload'] = raw['payload']
                        evidence_meta['raw_fetched_time'] = str(
                            raw.get('fetched_time') or '')
                    snap_id = _persist_hit(dao, evidence_meta, r, now,
                                           shadow=shadow)
                    if snap_id:
                        # 正式命中与影子命中都要截图 —— 影子模式是验证用，
                        # 不截图就无法验证截图链路本身。
                        _shoot_async(meta, r, snap_id)
                    else:
                        if shadow:
                            log.warning('影子候选落表失败（证据包不完整或重复）'
                                        ' %s', meta['match_id'])
                        else:
                            _persist_status(dao, snap_meta,
                                            'evidence_failed', now, a)
                except Exception as e:
                    log.exception('写滚球命中快照失败 %s: %s',
                                  meta['match_id'], e)

    return stats


def _shoot_async(meta: dict, r: dict, snap_id: int) -> None:
    """命中时异步截图留证。

    证据分两层，按可靠性排序：

    1) **本地证据页** `/evidence/<snapshot_id>`：只读命中快照和同一次请求的
       原始 `mari` 响应，不可变。这是主证据，先截它。
    2) **平台官网**：截图天然滞后 20 秒以上，只能作辅助，必须记录实际拍摄
       时间和「命中后 N 秒」，不能当成命中时刻的页面。

    为什么要异步：起 Chrome 要 15~20 秒，分析线程每 2 秒一轮，
    同步截图会把判定流程整个堵死。
    """
    if not getattr(config, 'LIVE_SHOT_ENABLED', False):
        return

    import os
    from common import shot_queue

    out_dir = getattr(config, 'LIVE_SHOT_DIR', '')
    mid = meta['match_id']
    stamp = now_beijing().strftime('%Y%m%d-%H%M%S')
    # 文件名带上 snapshot_id：秒级时间戳不足以代表证据身份。
    base_name = f'{stamp}_{mid}_{snap_id}'
    base_url = getattr(config, 'LIVE_SHOT_BASE_URL', 'http://127.0.0.1:8080')
    ok = shot_queue.submit({
        'snapshot_id': snap_id,
        'match_id': mid,
        'local_url': f'{base_url}/evidence/{snap_id}',
        'local_path': os.path.join(out_dir, base_name + '_local.png'),
        'official_path': os.path.join(out_dir, base_name + '_kessbet.png'),
        'submitted_at': now_beijing().isoformat(sep=' '),
    })
    if not ok:
        log.warning('截图任务未入队 snapshot=%s（队列满）', snap_id)


def _persist_cost(dao, meta: dict, r: dict, ht, now,
                  shadow: bool = False) -> None:
    """写结果成本（一场一行，供列表页只读）。

    shadow=True 且 r.is_arbitrage 时写 shadow_candidate，不发布正式机会。
    """
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
        snapshot_id=meta.get('snapshot_id') or meta.get('current_snapshot_id'),
        generation=meta.get('generation') or 0,
        verification_status=(r.get('verification') or {}).get('status'),
        verification_detail=(json.dumps(r.get('verification'),
                                        ensure_ascii=False)
                             if r.get('verification') else None),
        analysis_status=('shadow_candidate'
                         if shadow and r.get('is_arbitrage') else 'valid'),
        status_reason=(
            'shadow_candidate'
            if shadow and r.get('is_arbitrage')
            else ((r.get('verification') or {}).get('status')
                  or ('verified' if r.get('is_arbitrage')
                      else 'no_candidate'))),
        input_fingerprint=meta.get('input_fingerprint'),
    )


def _persist_status(dao, meta: dict, status: str, now,
                    a: dict | None = None) -> None:
    """把“当前快照没有判定结果”写成当前状态。

    不能只在有成本时才写结果：新快照 no_cover/no_data 时必须覆盖旧成本，
    否则页面会一直显示上一份赔率算出来的旧机会。
    """
    a = a or {}
    snapshot_id = a.get('snapshot_id') or meta.get('current_snapshot_id')
    if not snapshot_id:
        return
    try:
        dao.write_live_result_cost(
            match_id=meta['match_id'],
            kickoff_time=meta['kickoff_time'],
            cost=None, profit_pct=None, chosen='[]', reach_states=0,
            n_candidates=0,
            minute=a.get('minute', meta.get('minute')),
            home_score=a.get('home_score', meta.get('home_score')),
            away_score=a.get('away_score', meta.get('away_score')),
            ht_home=a.get('ht_home', meta.get('ht_home')),
            ht_away=a.get('ht_away', meta.get('ht_away')),
            odds_updated=(a.get('odds_updated')
                          or meta.get('odds_updated')),
            computed_time=now,
            snapshot_id=snapshot_id,
            generation=(a.get('generation')
                        or meta.get('generation') or 0),
            analysis_status=status,
            status_reason=status,
            input_fingerprint=meta.get('input_fingerprint'),
        )
    except Exception as e:
        log.warning('写滚球判定状态失败 %s/%s: %s',
                    meta.get('match_id'), status, e)


def _write_evidence(dao, meta: dict, r: dict) -> dict:
    """写 E1 证据包，返回 evidence 字段（不改命中表）。

    影子和正式模式共用；正式模式只有 complete 才能写命中。
    """
    mid = meta['match_id']
    snapshot_id = meta.get('snapshot_id') or meta.get('current_snapshot_id')
    out = {
        'evidence_id': None, 'evidence_status': 'failed',
        'evidence_path': None, 'manifest_hash': None,
        'analysis_id': snapshot_id, 'snapshot_id': snapshot_id,
    }
    try:
        if not snapshot_id or not hasattr(dao, 'get_live_snapshot'):
            raise RuntimeError('no snapshot id for evidence')
        snap = dao.get_live_snapshot(mid, snapshot_id)
        if not snap:
            raise RuntimeError('snapshot not found')
        resp = snap.get('response_json') or {}
        snap_ev = dict(snap)
        snap_ev.update({
            'status': resp.get('status'),
            'response_status': resp.get('response_status'),
            'response_headers': resp.get('response_headers'),
            'raw_body': resp.get('raw_body'),
            'markets': resp.get('markets') or snap.get('raw_payload'),
        })
        from common.evidence import write_evidence_package
        evidence_path = os.path.join(
            config.LIVE_EVIDENCE_DIR, mid, str(snapshot_id))
        manifest = write_evidence_package(
            evidence_path, snap_ev,
            {
                'analysis_id': snapshot_id,
                'analyzer_version': LIVE_ANALYZER_VERSION,
                'state_space_version': LIVE_STATE_SPACE_VERSION,
                'candidate_policy_version': LIVE_CANDIDATE_POLICY_VERSION,
                'expected': {
                    'cost': r.get('cost'),
                    'chosen': r.get('chosen'),
                    'n_candidates': r.get('n_candidates'),
                    'is_arbitrage': r.get('is_arbitrage'),
                    'is_candidate': r.get('is_candidate'),
                },
                'verification': r.get('verification'),
            },
            r.get('verification') or {})
        with open(os.path.join(evidence_path, 'manifest.json'), 'rb') as f:
            manifest_hash = hashlib.sha256(f.read()).hexdigest()
        out.update({
            'evidence_id': snapshot_id, 'evidence_status': 'complete',
            'evidence_path': evidence_path, 'manifest_hash': manifest_hash,
        })
    except Exception as e:
        log.exception('证据包写入失败 %s: %s', mid, e)
    return out


def _persist_hit(dao, meta: dict, r: dict, now,
                 shadow: bool = False) -> int | None:
    """把命中写进 kessbet_arbitrage_snapshot（is_live=1）。

    shadow=True 时写 is_shadow=1 的影子候选记录：可在命中页查看/复核，
    但不计入正式发布。两种记录用不同 is_shadow 做指纹去重。
    返回新快照 id；同一份赔率已经记过时返回 None。
    """
    from common.markets import describe_pick

    mid = meta['match_id']
    shadow_flag = 1 if shadow else 0
    # 同一输入指纹已经写过同类型命中时幂等返回，不重复落库。
    fp = meta.get('input_fingerprint')
    if fp and dao.has_arbitrage_fingerprint(
            mid, fp, is_shadow=shadow_flag):
        log.info('同一输入指纹已命中过，跳过 %s %s', mid, fp[:12])
        return None
    chosen = result.stakes(r['chosen'], r['cost'])
    home, away = meta.get('home_team') or '', meta.get('away_team') or ''

    # 同一次 HTTP 请求的原始 mari 响应 —— 命中取证的主证据。
    # 解析器会丢掉锁定项，原始串保留完整盘面，才能解释 other 之类的语义。
    raw_payload = meta.get('raw_payload')
    raw_obj = None
    if raw_payload:
        try:
            raw_obj = (json.loads(raw_payload)
                       if isinstance(raw_payload, str) else raw_payload)
        except (TypeError, ValueError):
            raw_obj = {'payload_unparsed': str(raw_payload)[:2000]}

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
        'state_space_version': LIVE_STATE_SPACE_VERSION,
        'candidate_policy_version': LIVE_CANDIDATE_POLICY_VERSION,
        # 判定时的套利阈值。必须记录 —— 回放要用它还原当时的判定语义，
        # 否则改了 LIVE_ARB_COST_THRESHOLD 会让历史命中无法复现。
        'cost_threshold': getattr(config, 'LIVE_ARB_COST_THRESHOLD', 1.0),
        'snapshot_id': meta.get('snapshot_id'),
        'generation': meta.get('generation'),
        'input_fingerprint': fp,
        'phase': meta.get('phase'),
        'phase_source': meta.get('phase_source'),
        'ht_source': meta.get('ht_source'),
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
            'is_candidate': r.get('is_candidate'),
        },
        'verification': r.get('verification'),
        'selections': [list(x) for x in meta.get('snapshot_selections', [])],
        # 同一次请求的平台原始响应。证据页直接渲染这份数据；
        # 解析器丢弃的锁定项、当前比分是否在盘面上，都能在这里核对。
        'raw_fetched_time': meta.get('raw_fetched_time'),
        'raw': raw_obj,
    }

    # ★ Task 6：正式命中先落 E1 证据包；包不完整就不写正式命中。
    ev = _write_evidence(dao, meta, r)
    snapshot_id = ev['snapshot_id']
    evidence_id = ev['evidence_id']
    evidence_status = ev['evidence_status']
    evidence_path = ev['evidence_path']
    manifest_hash = ev['manifest_hash']
    if evidence_status != 'complete':
        log.warning('证据包未完成，不写正式命中 %s', mid)
        return None

    snap_id = dao.write_arbitrage_snapshot(
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
        input_fingerprint=fp,
        evidence_id=evidence_id,
        evidence_status=evidence_status,
        evidence_path=evidence_path,
        manifest_hash=manifest_hash,
        analysis_id=snapshot_id,
        is_shadow=shadow_flag,
    )
    if snap_id:
        log.warning('%s %s snapshot=%s  (Σ=%.4f, 可达终局 %d)',
                    '影子候选已保存' if shadow else '滚球命中已保存',
                    mid, snap_id, r['cost'], r['reach'])
    return snap_id


def replay_live_evidence(replay_context) -> dict:
    """用命中时保存的输入重跑滚球判定，并与历史结果逐项比对。"""
    if isinstance(replay_context, str):
        replay_context = json.loads(replay_context)
    if not replay_context:
        return {'ok': False, 'error': 'no_replay_context'}

    # D05：算法版本不一致时不能假装能复现。
    # 新算法（other 隔离、guard、ghost 成本）重跑旧输入会得到不同结论；
    # 必须明确返回 unsupported_version，而不是说“同版本不可复现”。
    saved_ver = replay_context.get('analyzer_version')
    if saved_ver != LIVE_ANALYZER_VERSION:
        return {
            'ok': False,
            'error': 'unsupported_version',
            'saved_analyzer_version': saved_ver,
            'current_analyzer_version': LIVE_ANALYZER_VERSION,
            'schema_version': replay_context.get('schema_version'),
            'expected': replay_context.get('expected'),
        }

    selections = [tuple(x) for x in replay_context.get('selections', [])]
    replayed = result.check_match(
        selections,
        replay_context.get('home_score'),
        replay_context.get('away_score'),
        minute=replay_context.get('minute'),
        ht_home=replay_context.get('ht_home'),
        ht_away=replay_context.get('ht_away'),
        first_half_live=replay_context.get('first_half_live'),
        # 用记录时的阈值回放，不受当前配置影响。
        # 旧证据包没有该字段时退回 1.0。
        cost_threshold=replay_context.get('cost_threshold', 1.0),
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
                                      write_costs=not args.no_write,
                                      diagnostic=args.no_write)
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
