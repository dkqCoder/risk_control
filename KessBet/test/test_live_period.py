import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from analyze.live.period import (check_first_half_live,
                                 check_first_half_nth_goals,
                                 check_first_half_result_goal_joint,
                                 check_corner_full_time,
                                 check_future_count_windows,
                                 check_independent_three_way,
                                 check_second_half_at_halftime,
                                 check_future_windows, check_future_nth_goals)


def test_first_half_uses_current_score_and_isolated_market():
    snap = {'phase': 'first_half', 'phase_source': 'inferred',
            'minute': 28, 'state_after': {'ms': 7},
            'home_score': 1, 'away_score': 0,
            'rows': [('1st Half - 1x2', '', '', side, 3.2)
                     for side in ('1', 'X', '2')]
            + [('1x2', '', '', '2', 100)]}
    report = check_first_half_live(snap)
    assert report['status'] == 'shadow_candidate'
    assert report['normalized_result']['verification']['status'] == 'verified'
    assert all(row[0] == '1st Half - 1x2'
               for row in report['source_selections'])
    assert all(desc.startswith('1x2|')
               for desc, _ in report['normalized_result']['chosen'])


def test_first_half_refuses_halftime_and_clock_boundary():
    snap = {'phase': 'first_half', 'minute': 44,
            'state_after': {'ms': 7}, 'home_score': 0, 'away_score': 0,
            'rows': [('1st Half - 1x2', '', '', side, 3.2)
                     for side in ('1', 'X', '2')]}
    assert check_first_half_live(snap)['status'] == 'shadow_candidate'
    snap['minute'] = 45
    assert check_first_half_live(snap)['reason'] == 'first_half_phase_uncertain'
    snap['minute'] = 44
    snap['state_after']['ms'] = 31
    assert check_first_half_live(snap)['status'] == 'unverifiable'


def test_first_half_total_uses_remaining_half_outcomes():
    snap = {'phase': 'first_half', 'minute': 22,
            'state_after': {'ms': 7}, 'home_score': 1, 'away_score': 0,
            'rows': [('1st Half - Total', '', '1.5', label, 2.1)
                     for label in ('over', 'under')]}
    report = check_first_half_live(snap)
    assert report['status'] == 'shadow_candidate'
    assert report['normalized_result']['verification']['status'] == 'verified'
    snap['home_score'] = 2
    assert check_first_half_live(snap)['status'] == 'no_candidate'


def test_first_half_second_goal_is_separate_event_scope():
    snap = {'phase': 'first_half', 'minute': 22,
            'state_after': {'ms': 7}, 'home_score': 1, 'away_score': 0,
            'rows': [('1st Half - 2nd Goal', '', '', label, 3.2)
                     for label in ('1', '2', 'none')]}
    assert check_first_half_live(snap)['status'] == 'no_data'
    report = check_first_half_nth_goals(snap)[0]
    assert report['status'] == 'shadow_candidate'
    assert report['ordinal'] == 2
    assert report['min_payout'] > 10000
    snap['home_score'] = 2
    assert check_first_half_nth_goals(snap)[0]['reason'] == (
        'event_already_occurred')
    snap['home_score'] = 1
    snap['minute'] = 45
    assert check_first_half_nth_goals(snap)[0]['reason'] == (
        'first_half_phase_uncertain')
    snap['minute'] = 22
    snap['rows'] = snap['rows'][:-1]
    assert check_first_half_nth_goals(snap)[0]['reason'] == (
        'incomplete_or_conflicting_market')


def test_first_half_result_and_second_goal_keep_goal_order():
    snap = {'phase': 'first_half', 'minute': 21,
            'state_after': {'ms': 7}, 'home_score': 1, 'away_score': 0,
            'rows': [('1st Half - 1x2', '', '', label, odds)
                     for label, odds in [('1', 1.18), ('X', 4.25), ('2', 23)]]
            + [('1st Half - 2nd Goal', '', '', label, odds)
               for label, odds in [('1', 3.5), ('none', 1.8), ('2', 3.8)]]}
    report = check_first_half_result_goal_joint(snap)[0]
    assert report['status'] == 'no_candidate'
    assert ['X', '1'] in report['reachable_states']
    assert ['2', '1'] in report['reachable_states']
    assert ['1', 'none'] in report['reachable_states']
    assert ['X', 'none'] not in report['reachable_states']
    snap['home_score'] = 2
    assert check_first_half_result_goal_joint(snap)[0]['reason'] == (
        'event_already_occurred')


def test_rest_and_booking_three_way_are_isolated():
    snap = {'state_after': {'ms': 7},
            'rows': [(market, '', '', label, 3.2)
                     for market in ('Which Team Wins The Rest Of The Match',
                                    'Booking 1x2')
                     for label in ('1', 'X', '2')]}
    reports = check_independent_three_way(snap)
    assert len(reports) == 2
    assert all(r['status'] == 'shadow_candidate' for r in reports)
    assert all(r['normalized_result']['verification']['status'] == 'verified'
               for r in reports)
    snap['rows'] = snap['rows'][:-1]
    assert check_independent_three_way(snap)[1]['status'] == 'unverifiable'
    snap['state_after']['ms'] = 100
    assert all(r['reason'] == 'not_in_play'
               for r in check_independent_three_way(snap))


def test_full_time_corner_domain_uses_corner_score_only():
    snap = {'state_after': {'ms': 7, 'hck': '1', 'ack': '4'},
            'home_score': 0, 'away_score': 1,
            'rows': [('Corner 1x2', '', '', label, 3.2)
                     for label in ('1', 'X', '2')]
            + [('1x2', '', '', '2', 100)]}
    report = check_corner_full_time(snap)
    assert report['status'] == 'shadow_candidate'
    assert report['corner_score'] == [1, 4]
    assert len(report['source_selections']) == 3
    assert report['normalized_result']['verification']['status'] == 'verified'
    snap['state_after'].pop('hck')
    assert check_corner_full_time(snap)['reason'] == 'corner_count_missing'


def test_future_booking_window_three_way_is_separate_from_corner_window():
    booking = '5 Minutes - Booking 1x2 From 76 To 80'
    corner = '5 Minutes - Corner 1x2 From 76 To 80'
    snap = {'minute': 72, 'state_after': {'ms': 7},
            'rows': [(market, '', '', label, 3.2)
                     for market in (booking, corner)
                     for label in ('1', 'X', '2')]}
    reports = check_future_count_windows(snap)
    assert [(r['domain'], r['status']) for r in reports] == [
        ('booking', 'shadow_candidate'), ('corner', 'shadow_candidate')]
    assert check_future_windows(snap) == []
    snap['minute'] = 74
    assert all(r['reason'] == 'window_started_or_too_close'
               for r in check_future_count_windows(snap))
    snap['minute'] = 72
    snap['rows'] = snap['rows'][:-1]
    assert check_future_count_windows(snap)[1]['reason'] == (
        'incomplete_or_conflicting_market')


def test_future_corner_total_shares_only_its_corner_window():
    snap = {'minute': 72, 'state_after': {'ms': 7},
            'rows': [('5 Minutes - Total Corners From 76 To 80',
                      '', '0.5', label, 2.1)
                     for label in ('over', 'under')]
            + [('5 Minutes - Booking 1x2 From 76 To 80',
                '', '', label, 2.0) for label in ('1', 'X', '2')]}
    reports = check_future_count_windows(snap)
    assert len(reports) == 2
    assert reports[0]['domain'] == 'booking'
    assert reports[0]['status'] == 'no_candidate'
    assert reports[1]['domain'] == 'corner'
    assert reports[1]['status'] == 'shadow_candidate'
    assert reports[1]['normalized_result']['n_candidates'] == 2


def test_corner_total_and_team_range_can_share_corner_domain():
    snap = {'state_after': {'ms': 7, 'hck': 1, 'ack': 4},
            'rows': [('Total Corners', '', '5.5', label, 1.8)
                     for label in ('over', 'under')]
            + [('Corner Range', 'H', '', '0-4', 2.0)]}
    report = check_corner_full_time(snap)
    assert report['status'] == 'no_candidate'
    assert report['normalized_result'] is not None
    assert any(row[0] == 'Corner Range' for row in report['source_selections'])


def test_period_requires_official_halftime():
    rows = [('2nd Half - 1x2', '', '', '1', 3.2)]
    snap = {'phase': 'second_half', 'phase_source': 'inferred',
            'state_after': {'ms': 7}, 'home_score': 1, 'away_score': 0,
            'rows': rows}
    assert check_second_half_at_halftime(snap)['status'] == 'unverifiable'


def test_second_half_uses_own_zero_zero_score():
    snap = {'phase': 'halftime', 'phase_source': 'official_state',
            'state_after': {'ms': 31}, 'home_score': 3, 'away_score': 0,
            'rows': [('2nd Half - 1x2', '', '', side, 3.2)
                     for side in ('1', 'X', '2')]}
    r = check_second_half_at_halftime(snap)
    assert r['status'] == 'shadow_candidate'
    assert r['normalized_result']['verification']['status'] == 'verified'
    assert all(x[0] == '2nd Half - 1x2' for x in r['source_selections'])


def test_future_window_is_an_independent_score_domain():
    snap = {'minute': 45, 'state_after': {'ms': 7},
            'rows': [('10 Minutes - 1x2 From 51 To 60', '', '', side, 3.2)
                     for side in ('1', 'X', '2')]}
    reports = check_future_windows(snap)
    assert len(reports) == 1
    assert reports[0]['status'] == 'shadow_candidate'
    assert reports[0]['normalized_result']['verification']['status'] == 'verified'


def test_started_window_needs_start_checkpoint():
    snap = {'minute': 51, 'state_after': {'ms': 7},
            'rows': [('10 Minutes - 1x2 From 51 To 60', '', '', side, 3.2)
                     for side in ('1', 'X', '2')]}
    assert check_future_windows(snap)[0]['reason'] == 'window_started_or_too_close'


def test_future_nth_goal_requires_all_three_outcomes(monkeypatch):
    from collect import config
    monkeypatch.setattr(config, 'LIVE_CERT_TOTAL_STAKE', 10000)
    monkeypatch.setattr(config, 'LIVE_CERT_STAKE_UNIT', 1)
    monkeypatch.setattr(config, 'LIVE_CERT_MIN_PROFIT', 0)
    rows = [('6th Goal', '', '', label, 3.2)
            for label in ('1', '2', 'none')]
    snap = {'home_score': 3, 'away_score': 2, 'rows': rows}
    r = check_future_nth_goals(snap)[0]
    assert r['status'] == 'shadow_candidate'
    assert r['min_payout'] > 10000
    snap['rows'] = rows[:2]
    assert check_future_nth_goals(snap)[0]['status'] == 'unverifiable'
    snap['rows'] = rows
    snap['home_score'] = 4
    assert check_future_nth_goals(snap)[0]['reason'] == 'event_already_occurred'


def test_scope_reports_bind_to_immutable_snapshot():
    from analyze.live.run import _persist_scope_reports

    class Recorder:
        def __init__(self):
            self.saved = []

        def save_live_scope_analysis(self, *args):
            self.saved.append(args)

    dao = Recorder()
    _persist_scope_reports(
        dao, {'snapshot_id': 's1', 'match_id': 'm1', 'phase': 'halftime'},
        {'status': 'no_candidate', 'reason': 'no_complete_cover'},
        [{'status': 'unverifiable', 'window': [51, 60],
          'reason': 'window_started_or_too_close'}],
        [{'status': 'unverifiable', 'scope': '6th Goal',
          'reason': 'incomplete_or_conflicting_market'}],
        {'status': 'shadow_candidate', 'scope': 'first_half_live',
         'source_selections': [('1st Half - 1x2', '', '', '1', 3.2)]},
        [{'status': 'no_candidate', 'scope': 'Booking 1x2',
          'source_selections': [('Booking 1x2', '', '', '1', 2.0)]}],
        {'status': 'no_candidate', 'scope': 'full_time_corners',
         'source_selections': [('Corner 1x2', '', '', '1', 2.0)]},
        [{'status': 'no_candidate', 'domain': 'booking',
          'window': [76, 80], 'source_selections': [
              ('5 Minutes - Booking 1x2 From 76 To 80', '', '', '1', 2.0)]}],
        [{'status': 'no_candidate', 'scope': '1st Half - 2nd Goal',
          'ordinal': 2,
          'source_selections': [('1st Half - 2nd Goal', '', '', '1', 2.0)]}],
        [{'status': 'no_candidate', 'scope': '1st Half - 1x2 & 2nd Goal',
          'ordinal': 2,
          'source_selections': [('1st Half - 1x2', '', '', '1', 2.0)]}])
    assert [x[2] for x in dao.saved] == [
        'first_half', 'full_time_corners', 'second_half', 'window_51_60',
        'booking_window_76_80', 'goal_6th', 'first_half_goal_2',
        'first_half_1x2_goal_2',
        'booking_1x2']
    assert all(x[:2] == ('s1', 'm1') for x in dao.saved)
