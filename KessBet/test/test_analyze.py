"""analyze/run.py 的测试。用真实 MySQL 里已有的数据。

注意：`evaluate_match` 现在的签名是
    evaluate_match(dao, match_id, sport, home='', away='', kickoff=None) -> MatchAnalysis
返回对象始终有值，用 `.error` 表示失败原因。
"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from analyze.run import analyze_once, evaluate_match
from db.dao import Dao


def test_evaluate_skips_non_soccer():
    dao = Dao()
    try:
        a = evaluate_match(dao, 'whatever', 'sr:sport:2')   # 篮球
        assert a.error == 'unsupported_sport'
        assert a.ilp is None
    finally:
        dao.close()


def test_evaluate_reports_missing_data_for_unknown_match():
    dao = Dao()
    try:
        a = evaluate_match(dao, 'no_such_match_id', 'sr:sport:1')
        assert a.error == 'no_data'
        assert a.ilp is None
    finally:
        dao.close()


def test_analyze_once_runs():
    """跑一轮，只验证不抛异常且统计字段齐全。"""
    dao = Dao()
    try:
        stats = analyze_once(dao, horizon_hours=1, write_hits=False)
        assert 'matches' in stats
        assert 'evaluated' in stats
        assert 'hits' in stats
        assert stats['matches'] >= 0
    finally:
        dao.close()
