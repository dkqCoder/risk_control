"""独立时段判定的安全起点。

只有平台明确返回中场状态时，第二半场尚未发生，才能把第二半场
进球过程视为从 0:0 开始。此模块只产出影子诊断，不发布正式命中。
"""
from __future__ import annotations

import re
from decimal import Decimal, ROUND_FLOOR

from analyze.live import result
from collect import config

_WINDOW = re.compile(r'^(\d+) Minutes - (.+) From (\d+) To (\d+)$')
_NTH_GOAL = re.compile(r'^(\d+)(?:st|nd|rd|th) Goal$')
_FIRST_HALF_NTH_GOAL = re.compile(r'^1st Half - (\d+)(?:st|nd|rd|th) Goal$')
_SUPPORTED_WINDOW_BASE = frozenset({
    '1x2', 'Double Chance', 'Both Teams To Score', 'Total',
    'Exact Goals', 'Clean Sheet', 'Handicap', 'Odd/Even',
})
_COUNT_WINDOW_MARKETS = {
    'Corner 1x2': ('corner', '1x2'),
    'Total Corners': ('corner', 'Total'),
    'Booking 1x2': ('booking', '1x2'),
    'Total Bookings': ('booking', 'Total'),
}


def check_future_count_windows(snapshot: dict) -> list[dict]:
    """Future corner/booking windows, grouped by time and event domain."""
    grouped = {}
    for row in snapshot.get('rows') or []:
        market, side, line, label, odds = row
        match = _WINDOW.fullmatch(market)
        if not match or match.group(2) not in _COUNT_WINDOW_MARKETS:
            continue
        width, base, start, end = match.groups()
        domain, normalized_market = _COUNT_WINDOW_MARKETS[base]
        key = (domain, int(start), int(end))
        group = grouped.setdefault(key, {'source': [], 'normalized': [],
                                         'invalid_width': False,
                                         'three_way': {}})
        group['source'].append(tuple(row))
        if int(end) - int(start) + 1 != int(width) or int(start) < 1:
            group['invalid_width'] = True
        group['normalized'].append(
            (normalized_market, side, line, label, odds))
        if normalized_market == '1x2':
            group['three_way'].setdefault(base, []).append((side, line, label))
    reports = []
    minute = snapshot.get('minute')
    state = snapshot.get('state_after') or {}
    for (domain, start, end), group in sorted(grouped.items()):
        report = {'scope': f'{domain} From {start} To {end}',
                  'domain': domain, 'window': [start, end],
                  'source_selections': group['source']}
        if group['invalid_width']:
            report.update(status='unverifiable', reason='invalid_window_width')
        elif (minute is None or state.get('ms') not in (6, 7)
              or not isinstance(minute, int)):
            report.update(status='unverifiable', reason='clock_or_state_missing')
        elif minute + 2 >= start:
            report.update(status='unverifiable',
                          reason='window_started_or_too_close')
        elif any(len(legs) != 3
                 or {label for _side, _line, label in legs} != {'1', 'X', '2'}
                 or any(side or line for side, line, _label in legs)
                 for legs in group['three_way'].values()):
            report.update(status='unverifiable',
                          reason='incomplete_or_conflicting_market')
        else:
            candidate = result.check_match(group['normalized'], 0, 0, minute=None,
                                           first_half_live=False)
            if candidate is None:
                report.update(status='no_candidate', reason='no_complete_cover')
            else:
                report.update(
                    status=('shadow_candidate' if candidate['is_arbitrage']
                            else 'no_candidate'),
                    reason=((candidate.get('verification') or {}).get('status')
                            or 'cost_or_rounding'),
                    normalized_result=candidate)
        reports.append(report)
    return reports


def check_independent_three_way(snapshot: dict) -> list[dict]:
    """Check exhaustive 1/X/2 markets without mixing different settlement domains."""
    markets = ('Which Team Wins The Rest Of The Match', 'Booking 1x2')
    reports = []
    for market in markets:
        source = [tuple(row) for row in snapshot.get('rows') or []
                  if row[0] == market]
        if not source:
            continue
        report = {'scope': market, 'source_selections': source}
        state = snapshot.get('state_after') or {}
        if state.get('ms') not in (6, 7):
            report.update(status='unverifiable', reason='not_in_play')
        elif (len(source) != 3
              or {row[3] for row in source} != {'1', 'X', '2'}
              or any(row[1] or row[2] for row in source)):
            report.update(status='unverifiable',
                          reason='incomplete_or_conflicting_market')
        else:
            normalized = [('1x2', '', '', label, odds)
                          for _market, _side, _line, label, odds in source]
            candidate = result.check_match(normalized, 0, 0, minute=None,
                                           first_half_live=False)
            if candidate is None:
                report.update(status='unverifiable', reason='invalid_market')
            else:
                report.update(
                    status=('shadow_candidate' if candidate['is_arbitrage']
                            else 'no_candidate'),
                    reason=((candidate.get('verification') or {}).get('status')
                            or 'cost_or_rounding'),
                    normalized_result=candidate)
        reports.append(report)
    return reports


def check_corner_full_time(snapshot: dict) -> dict:
    """Solve full-time corner markets in a separate two-team count domain."""
    mapping = {'Corner 1x2': '1x2', 'Total Corners': 'Total',
               'Corner Range': 'Multigoals',
               'Odd/Even Corners': 'Odd/Even'}
    source = [tuple(row) for row in snapshot.get('rows') or []
              if row[0] in mapping]
    report = {'scope': 'full_time_corners', 'source_selections': source}
    if not source:
        report.update(status='no_data', reason='no_corner_selections')
        return report
    state = snapshot.get('state_after') or {}
    if state.get('ms') not in (6, 7):
        report.update(status='unverifiable', reason='not_in_play')
        return report
    try:
        home, away = int(state['hck']), int(state['ack'])
    except (KeyError, ValueError, TypeError):
        report.update(status='unverifiable', reason='corner_count_missing')
        return report
    if home < 0 or away < 0 or result.score_space_incomplete(home, away):
        report.update(status='unverifiable', reason='corner_model_cap')
        return report
    normalized = [(mapping[market], side, line, label, odds)
                  for market, side, line, label, odds in source]
    candidate = result.check_match(normalized, home, away, minute=None,
                                   first_half_live=False)
    if candidate is None:
        report.update(status='no_candidate', reason='no_complete_cover')
    else:
        report.update(status=('shadow_candidate' if candidate['is_arbitrage']
                              else 'no_candidate'),
                      reason=((candidate.get('verification') or {}).get('status')
                              or 'cost_or_rounding'),
                      normalized_result=candidate,
                      corner_score=[home, away])
    return report


def check_first_half_live(snapshot: dict) -> dict:
    """Use the current score as the first-half baseline while play is clearly before 45'.

    The platform's in-play status does not identify the half. A clock below 45'
    supports an internal calculation, but cannot by itself certify the phase or
    prove that the selections were available in the site's betslip.
    """
    source = [tuple(row) for row in snapshot.get('rows') or []
              if row[0].startswith('1st Half - ')
              and result.is_result_market(row[0])]
    if not source:
        return {'status': 'no_data', 'reason': 'no_first_half_selections'}
    report = {'scope': 'first_half_live', 'source_selections': source}
    minute = snapshot.get('minute')
    state = snapshot.get('state_after') or {}
    home, away = snapshot.get('home_score'), snapshot.get('away_score')
    if (snapshot.get('phase') != 'first_half'
            or state.get('ms') not in (6, 7)
            or minute is None or not 0 <= minute < 45):
        report.update(status='unverifiable', reason='first_half_phase_uncertain')
        return report
    if (not isinstance(home, int) or not isinstance(away, int)
            or home < 0 or away < 0):
        report.update(status='unverifiable', reason='score_missing')
        return report
    if result.score_space_incomplete(home, away):
        report.update(status='unverifiable', reason='score_near_model_cap')
        return report
    normalized = [(market[len('1st Half - '):], side, line, label, odds)
                  for market, side, line, label, odds in source]
    from analyze.live.verify import is_whitelisted
    by_market = {}
    for row in normalized:
        by_market.setdefault((row[0], row[1], row[2]), set()).add(row[3])
    analyzed = []
    for original, row in zip(source, normalized):
        base, side, line, label, _odds = row
        expected = result.expected_labels(base)
        if (is_whitelisted(base, side, line, label)
                and (expected is None
                     or expected.issubset(by_market[(base, side, line)]))):
            analyzed.append(original)
    report['analyzed_selections'] = analyzed
    candidate = result.check_match(normalized, home, away, minute=None,
                                   first_half_live=False)
    if candidate is None:
        report.update(status='no_candidate', reason='no_complete_cover')
    else:
        report.update(
            status=('shadow_candidate' if candidate['is_arbitrage']
                    else 'no_candidate'),
            reason=((candidate.get('verification') or {}).get('status')
                    or 'cost_not_below_one'),
            normalized_result=candidate)
    return report


def check_first_half_nth_goals(snapshot: dict) -> list[dict]:
    """Future Nth goal of the first half; keep it outside the score cover."""
    grouped = {}
    for row in snapshot.get('rows') or []:
        match = _FIRST_HALF_NTH_GOAL.fullmatch(row[0])
        if match:
            grouped.setdefault(row[0], []).append(tuple(row))
    reports = []
    minute = snapshot.get('minute')
    state = snapshot.get('state_after') or {}
    home, away = snapshot.get('home_score'), snapshot.get('away_score')
    for market, source in sorted(grouped.items()):
        ordinal = int(_FIRST_HALF_NTH_GOAL.fullmatch(market).group(1))
        report = {'scope': market, 'ordinal': ordinal,
                  'source_selections': source}
        if (snapshot.get('phase') != 'first_half'
                or state.get('ms') not in (6, 7)
                or not isinstance(minute, int) or not 0 <= minute < 45):
            report.update(status='unverifiable',
                          reason='first_half_phase_uncertain')
        elif (not isinstance(home, int) or not isinstance(away, int)
              or home < 0 or away < 0):
            report.update(status='unverifiable', reason='score_missing')
        else:
            base = market[len('1st Half - '):]
            normalized = [(base, side, line, label, odds)
                          for _market, side, line, label, odds in source]
            checked = check_future_nth_goals(
                {'home_score': home, 'away_score': away,
                 'rows': normalized})[0]
            report.update({key: value for key, value in checked.items()
                           if key not in ('scope', 'source_selections')})
        reports.append(report)
    return reports


def check_first_half_result_goal_joint(snapshot: dict) -> list[dict]:
    """Joint 1x2/Nth-goal cover with goal order retained in the state space."""
    score_rows = [tuple(row) for row in snapshot.get('rows') or []
                  if row[0] == '1st Half - 1x2']
    event_groups = {}
    for row in snapshot.get('rows') or []:
        match = _FIRST_HALF_NTH_GOAL.fullmatch(row[0])
        if match:
            event_groups.setdefault(row[0], []).append(tuple(row))
    reports = []
    state = snapshot.get('state_after') or {}
    minute = snapshot.get('minute')
    home, away = snapshot.get('home_score'), snapshot.get('away_score')
    for market, event_rows in sorted(event_groups.items()):
        ordinal = int(_FIRST_HALF_NTH_GOAL.fullmatch(market).group(1))
        source = score_rows + event_rows
        report = {'scope': f"1st Half - 1x2 & {market[len('1st Half - '):]}",
                  'ordinal': ordinal, 'source_selections': source}
        if (snapshot.get('phase') != 'first_half'
                or state.get('ms') not in (6, 7)
                or not isinstance(minute, int) or not 0 <= minute < 45):
            report.update(status='unverifiable',
                          reason='first_half_phase_uncertain')
            reports.append(report)
            continue
        if (not isinstance(home, int) or not isinstance(away, int)
                or home < 0 or away < 0):
            report.update(status='unverifiable', reason='score_missing')
            reports.append(report)
            continue
        k = ordinal - home - away
        if k <= 0:
            report.update(status='unverifiable',
                          reason='event_already_occurred')
            reports.append(report)
            continue
        if (len(score_rows) != 3 or len(event_rows) != 3
                or {row[3] for row in score_rows} != {'1', 'X', '2'}
                or {row[3] for row in event_rows} != {'1', '2', 'none'}
                or any(row[1] or row[2] for row in source)):
            report.update(status='unverifiable',
                          reason='incomplete_or_conflicting_market')
            reports.append(report)
            continue
        # If the Nth goal never arrives, fewer than k further goals occur.
        # Once team 1 or 2 scores it, any half-time 1/X/2 result remains
        # reachable because later goals are unbounded. These nine-or-fewer
        # abstract states therefore cover every score and goal order.
        states = []
        for dh in range(k):
            for da in range(k - dh):
                h, a = home + dh, away + da
                outcome = '1' if h > a else ('2' if h < a else 'X')
                if (outcome, 'none') not in states:
                    states.append((outcome, 'none'))
        states.extend((outcome, scorer)
                      for scorer in ('1', '2')
                      for outcome in ('1', 'X', '2'))
        try:
            odds = [Decimal(str(row[4])) for row in source]
            budget = Decimal(str(config.LIVE_CERT_TOTAL_STAKE))
            unit = Decimal(str(config.LIVE_CERT_STAKE_UNIT))
            min_profit = Decimal(str(config.LIVE_CERT_MIN_PROFIT))
            if (any(value <= 1 for value in odds) or unit <= 0
                    or budget <= 0 or budget % unit):
                raise ValueError('invalid price or stake unit')
            masks = []
            for row in source:
                index = 0 if row[0] == '1st Half - 1x2' else 1
                masks.append(sum(1 << i for i, outcome in enumerate(states)
                                 if outcome[index] == row[3]))
            full = (1 << len(states)) - 1
            best = None
            for subset in range(1, 1 << len(source)):
                covered = 0
                cost = Decimal(0)
                for i, mask in enumerate(masks):
                    if subset & (1 << i):
                        covered |= mask
                        cost += Decimal(1) / odds[i]
                if covered == full and (best is None or cost < best[0]):
                    best = (cost, subset)
            if best is None:
                report.update(status='no_candidate', reason='no_complete_cover')
                reports.append(report)
                continue
            cost, subset = best
            chosen = [i for i in range(len(source)) if subset & (1 << i)]
            units = {i: int(((Decimal(1) / odds[i]) / cost
                             * (budget / unit)).to_integral_value(
                                 rounding=ROUND_FLOOR)) for i in chosen}
            remaining = int(budget / unit) - sum(units.values())
            order = sorted(chosen, key=lambda i: (
                -((Decimal(1) / odds[i]) / cost * (budget / unit)
                  - units[i]), i))
            for i in order[:remaining]:
                units[i] += 1
            payouts = [sum(unit * units[i] * odds[i]
                           for i in chosen if masks[i] & (1 << state_index))
                       for state_index in range(len(states))]
            worst = min(payouts)
            verified = (cost < 1 and worst > budget
                        and worst >= budget * (1 + min_profit))
            report.update(status='shadow_candidate' if verified else 'no_candidate',
                          reason='verified' if verified else 'cost_or_rounding',
                          cost=float(cost), min_payout=float(worst),
                          chosen=[{'selection': source[i],
                                   'stake': float(unit * units[i])}
                                  for i in chosen],
                          worst_state=list(states[payouts.index(worst)]),
                          reachable_states=[list(x) for x in states])
        except (ValueError, ArithmeticError, ZeroDivisionError):
            report.update(status='unverifiable', reason='invalid_input')
        reports.append(report)
    return reports


def check_future_nth_goals(snapshot: dict) -> list[dict]:
    """完整的未来第 N 球三选一盘；只输出影子诊断。"""
    home, away = snapshot.get('home_score'), snapshot.get('away_score')
    if home is None or away is None:
        return []
    groups = {}
    for row in snapshot.get('rows') or []:
        market, side, line, label, odds = row
        match = _NTH_GOAL.fullmatch(market)
        if match:
            groups.setdefault((int(match.group(1)), market), []).append(tuple(row))
    reports = []
    for (ordinal, market), rows in sorted(groups.items()):
        report = {'scope': market, 'source_selections': rows}
        if home + away >= ordinal:
            report.update(status='unverifiable', reason='event_already_occurred')
        else:
            options = {}
            conflict = False
            for _market, side, line, label, odds in rows:
                if side or line or label not in ('1', '2', 'none'):
                    conflict = True
                    continue
                if label in options and options[label] != odds:
                    conflict = True
                options[label] = odds
            if conflict or set(options) != {'1', '2', 'none'}:
                report.update(status='unverifiable', reason='incomplete_or_conflicting_market')
            else:
                try:
                    prices = {k: Decimal(str(v)) for k, v in options.items()}
                    budget = Decimal(str(config.LIVE_CERT_TOTAL_STAKE))
                    unit = Decimal(str(config.LIVE_CERT_STAKE_UNIT))
                    min_profit = Decimal(str(config.LIVE_CERT_MIN_PROFIT))
                    if (any(p <= 1 for p in prices.values()) or unit <= 0
                            or budget <= 0 or budget % unit):
                        raise ValueError('invalid odds or budget')
                    cost = sum(Decimal(1) / p for p in prices.values())
                    weights = {k: (Decimal(1) / p) / cost for k, p in prices.items()}
                    total_units = int(budget / unit)
                    units = {k: int((w * total_units).to_integral_value(
                        rounding=ROUND_FLOOR)) for k, w in weights.items()}
                    remainder = total_units - sum(units.values())
                    order = sorted(weights, key=lambda k: (
                        -(weights[k] * total_units - units[k]), k))
                    for k in order[:remainder]:
                        units[k] += 1
                    payout = {k: unit * units[k] * prices[k] for k in prices}
                    worst = min(payout.values())
                    verified = cost < 1 and worst > budget and (
                        worst >= budget * (1 + min_profit))
                    report.update(
                        status='shadow_candidate' if verified else 'no_candidate',
                        reason='verified' if verified else 'cost_or_rounding',
                        cost=float(cost), min_payout=float(worst),
                        stakes={k: float(unit * units[k]) for k in units},
                        payout={k: float(v) for k, v in payout.items()})
                except (ValueError, ArithmeticError, ZeroDivisionError):
                    report.update(status='unverifiable', reason='invalid_input')
        reports.append(report)
    return reports


def check_future_windows(snapshot: dict) -> list[dict]:
    """只计算尚未开始、且结算依赖区间进球数的窗口盘。"""
    minute = snapshot.get('minute')
    state = snapshot.get('state_after') or {}
    grouped = {}
    for row in snapshot.get('rows') or []:
        market, side, line, label, odds = row
        match = _WINDOW.fullmatch(market)
        if not match:
            continue
        width, base, start, end = match.groups()
        if base in _COUNT_WINDOW_MARKETS:
            continue  # 独立的角球/罚牌事件域，不能和进球窗口合并。
        start, end, width = int(start), int(end), int(width)
        key = (start, end)
        group = grouped.setdefault(key, {'source_selections': [],
                                         'analyzed_selections': [],
                                         'normalized': [], 'unsupported': []})
        group['source_selections'].append(tuple(row))
        if end - start + 1 != width or base not in _SUPPORTED_WINDOW_BASE:
            group['unsupported'].append(market)
            continue
        group['normalized'].append((base, side, line, label, odds))
        group['analyzed_selections'].append(tuple(row))
    if not grouped:
        return []
    if minute is None or state.get('ms') not in (6, 7, 31):
        return [{'status': 'unverifiable',
                 'reason': 'clock_or_state_missing', 'window': list(key)}
                for key in sorted(grouped)]
    reports = []
    for (start, end), group in sorted(grouped.items()):
        report = {'window': [start, end],
                  'source_selections': group['source_selections'],
                  'analyzed_selections': group['analyzed_selections']}
        if minute + 2 >= start:
            report.update(status='unverifiable',
                          reason='window_started_or_too_close')
        elif not group['normalized']:
            report.update(status='unverifiable',
                          reason='unsupported_window_rules')
        else:
            candidate = result.check_match(
                group['normalized'], 0, 0, minute=None,
                first_half_live=False)
            if candidate is None:
                report.update(status='no_candidate', reason='no_complete_cover')
            else:
                report.update(
                    status=('shadow_candidate' if candidate['is_arbitrage']
                            else 'no_candidate'),
                    reason=((candidate.get('verification') or {}).get('status')
                            or 'cost_not_below_one'),
                    normalized_result=candidate)
        reports.append(report)
    return reports


def check_second_half_at_halftime(snapshot: dict) -> dict:
    """对中场快照中的第二半场盘口做独立候选判定。"""
    if (snapshot.get('phase') != 'halftime'
            or snapshot.get('phase_source') != 'official_state'):
        return {'status': 'unverifiable', 'reason': 'phase_not_authoritative'}
    state = snapshot.get('state_after') or {}
    if state.get('ms') != 31:
        return {'status': 'unverifiable', 'reason': 'not_halftime_state'}
    if snapshot.get('home_score') is None or snapshot.get('away_score') is None:
        return {'status': 'unverifiable', 'reason': 'score_missing'}

    source = [tuple(row) for row in snapshot.get('rows') or []
              if row[0].startswith('2nd Half - ')]
    if not source:
        return {'status': 'no_data', 'reason': 'no_second_half_selections'}
    normalized = [(market[len('2nd Half - '):], side, line, label, odds)
                  for market, side, line, label, odds in source]
    candidate = result.check_match(normalized, 0, 0, minute=None,
                                   first_half_live=False)
    if candidate is None:
        return {'status': 'no_candidate', 'reason': 'no_complete_cover',
                'source_selections': source}
    return {'status': ('shadow_candidate' if candidate['is_arbitrage']
                       else 'no_candidate'),
            'reason': ((candidate.get('verification') or {}).get('status')
                       or 'cost_not_below_one'),
            'source_selections': source,
            'normalized_result': candidate,
            'scope': 'second_half_from_halftime'}
