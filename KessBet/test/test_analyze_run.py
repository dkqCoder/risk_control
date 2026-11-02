"""analyze/run.py 的测试。用真实 MySQL 已有数据。"""
import os
import sys
from datetime import datetime, timedelta, timezone

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from analyze.run import (CROSS_CHECK_TOL, MatchAnalysis, analyze_once,
                         evaluate_match, format_payout_table)
from db.dao import Dao

BEIJING = timezone(timedelta(hours=8))


def _soccer_matches(dao, limit=3):
    with dao.conn.cursor() as c:
        c.execute("""SELECT m.match_id, MAX(m.home_team), MAX(m.away_team),
                            MAX(m.kickoff_time)
                     FROM kessbet_match m
                     JOIN kessbet_odds_prematch o ON o.match_id = m.match_id
                     WHERE m.sport='sr:sport:1' AND m.kickoff_time > NOW()
                     GROUP BY m.match_id
                     ORDER BY MIN(m.kickoff_time) LIMIT %s""", (limit,))
        return c.fetchall()


def test_unsupported_sport():
    dao = Dao()
    try:
        a = evaluate_match(dao, 'x', 'sr:sport:2')
        assert a.error == 'unsupported_sport'
        assert a.ilp is None
    finally:
        dao.close()


def test_evaluate_runs_both_models():
    dao = Dao()
    try:
        rows = _soccer_matches(dao, 1)
        assert rows, '库里没有可测的足球比赛'
        mid, home, away, ko = rows[0]
        a = evaluate_match(dao, mid, 'sr:sport:1', home, away, ko)

        assert a.error is None, f'分析失败: {a.error}'
        assert a.ilp is not None
        assert a.lp is not None
        assert a.ilp.cost is not None
        assert a.lp.status == 'ok'
        assert a.n_options > 0
        assert a.n_candidates > 0
    finally:
        dao.close()


def test_cross_check_passes_on_real_data():
    """真实数据上 ILP 和 LP 必须一致。"""
    dao = Dao()
    try:
        for mid, home, away, ko in _soccer_matches(dao, 3):
            a = evaluate_match(dao, mid, 'sr:sport:1', home, away, ko)
            if a.error:
                continue
            assert a.cross_ok, (
                f'{home} vs {away} 交叉验证失败: '
                f'ILP={a.ilp.cost}, LP={a.lp.worst_profit}, gap={a.cross_gap}')
            assert a.cross_gap <= CROSS_CHECK_TOL
    finally:
        dao.close()


def test_payout_table_renders():
    """收益表能正常渲染，包含关键字段。"""
    dao = Dao()
    try:
        rows = _soccer_matches(dao, 1)
        if not rows:
            return
        mid, home, away, ko = rows[0]
        a = evaluate_match(dao, mid, 'sr:sport:1', home, away, ko)
        text = format_payout_table(a, stake=1000)

        assert home[:10] in text
        assert '模型 1' in text
        assert '模型 2' in text
        assert '交叉验证' in text
        assert '判定' in text
        if a.error is None:
            assert '最小覆盖成本' in text
            assert '最差状态净收益' in text
    finally:
        dao.close()


def test_analyze_once_returns_stats():
    dao = Dao()
    try:
        stats = analyze_once(dao, horizon_hours=2, write_hits=False)
        for k in ('matches', 'evaluated', 'hits', 'cross_fail', 'errors'):
            assert k in stats
        assert stats['cross_fail'] == 0, '不应有交叉验证失败'
    finally:
        dao.close()


def test_no_arbitrage_on_current_data():
    """当前数据不应该有套利（单家内部）。"""
    dao = Dao()
    try:
        stats = analyze_once(dao, horizon_hours=2, write_hits=False)
        assert stats['hits'] == 0
        if stats['min_cost'] is not None:
            assert stats['min_cost'] >= 1.0
    finally:
        dao.close()
