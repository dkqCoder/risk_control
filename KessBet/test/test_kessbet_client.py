"""kessbet_client 的日期过滤测试。

运行: py -m pytest test_kessbet_client.py -v
"""
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import kessbet_client as C


def ts(local_dt):
    """本地时间 -> 接口的 mbtp 毫秒时间戳。"""
    return int(local_dt.timestamp() * 1000)


def test_today_matches_use_local_time_from_mbtp():
    """--today 应基于 mbtp 换成本地时间判断，而不是 mdt（肯尼亚 UTC+3）。"""
    today = datetime.datetime.now().replace(hour=20, minute=0, second=0, microsecond=0)
    yesterday = today - datetime.timedelta(days=1)
    tomorrow = today + datetime.timedelta(days=1)

    matches = [
        # 肯尼亚日历日和本地日历日故意不一致
        {'matchId': 'a', 'mbtp': ts(today), 'mdt': (today - datetime.timedelta(days=1)).strftime('%d/%m')},
        {'matchId': 'b', 'mbtp': ts(yesterday), 'mdt': today.strftime('%d/%m')},
        {'matchId': 'c', 'mbtp': ts(tomorrow), 'mdt': today.strftime('%d/%m')},
    ]
    got = {m['matchId'] for m in C.filter_today(matches)}
    assert got == {'a'}


def test_today_threshold_is_local_midnight():
    """本地今天 00:00 起算，跨年/月都要正确。"""
    now = datetime.datetime.now()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    matches = [
        {'matchId': 'before', 'mbtp': ts(midnight - datetime.timedelta(minutes=1)), 'mdt': ''},
        {'matchId': 'at', 'mbtp': ts(midnight), 'mdt': ''},
        {'matchId': 'after', 'mbtp': ts(midnight + datetime.timedelta(minutes=1)), 'mdt': ''},
    ]
    got = [m['matchId'] for m in C.filter_today(matches)]
    assert got == ['at', 'after']


def test_sort_by_mbtp():
    now = datetime.datetime.now()
    matches = [
        {'matchId': 'late', 'mbtp': ts(now + datetime.timedelta(hours=2)), 'mdt': ''},
        {'matchId': 'early', 'mbtp': ts(now), 'mdt': ''},
    ]
    got = [m['matchId'] for m in sorted(matches, key=C.sort_key)]
    assert got == ['early', 'late']
