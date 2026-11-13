"""kessbet HTTP 客户端的状态接口测试。

`fetch_state()` 拉的是 ACTION=0014 —— 比赛的实时状态（比分、时间、红黄牌）。
这是滚球分析的必要输入：盘口接口(0015)只给赔率，不给「现在几比几」。

字段含义全部来自 2026-11-10 的真实响应，不是推测：

    ms=0    未开始      无比分、无 mt
    ms=7    进行中      有 mt（'65:54'）、hmp/amp 实时比分
    ms=100  已结束      无 mt，比分固定

运行: py -m pytest test_fetch_state.py -v
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from collect.kessbet import http as H


# 真实响应样本（比赛 245162411202482764，进行到 61:55，比分 0:0）
LIVE_SAMPLE = {
    'matchId': '245162411202482764',
    'ms': 7,
    'mt': '61:55',
    'hmp': '0',
    'amp': '0',
    'hrc': 0, 'arc': 0,
    'hyc': 0, 'ayc': 2,
    'stop': 0,
    'tmi': 'sr:match:76409632',
    'tsi': 'sr:sport:1',
    'mbt': '09:00', 'mdt': '10/11',
    'sct': 1794277530637,
    'htn': 'AD Rosario', 'atn': 'Pitbulls Santa Barbara FC',
    'ln': 'Liga de Ascenso', 'cn': 'Costa Rica',
    # 注意：这里是分阶段快照，不是当前比分
    'msScoreList': [{'amp': '0', 'hmp': '0', 'ms': 6},
                    {'amp': '0', 'hmp': '0', 'ms': 7}],
}

# 已结束的比赛（有比分，无 mt）
FINISHED_SAMPLE = {
    'matchId': '62942855477', 'ms': 100, 'hmp': '1', 'amp': '1',
    'hrc': 0, 'arc': 0, 'hyc': 4, 'ayc': 2, 'msScoreList': [],
}

# 未开始的比赛（无比分）
UPCOMING_SAMPLE = {
    'matchId': '90863728927', 'ms': 0, 'mt': '',
    'hmp': '', 'amp': '', 'hrc': 0, 'arc': 0, 'hyc': 0, 'ayc': 0,
    'msScoreList': [],
}


def make_client(response):
    """构造一个 _post 返回固定值的客户端，不打真网络。"""
    c = H.KessbetHttp.__new__(H.KessbetHttp)
    c._post = lambda action, body: response
    return c


# ---------------------------------------------------------------- 基本

def test_returns_dict_for_live_match():
    c = make_client(LIVE_SAMPLE)
    d = c.fetch_state('245162411202482764')
    assert isinstance(d, dict)
    assert d['ms'] == 7
    assert d['mt'] == '61:55'


def test_returns_none_when_match_finished():
    """比赛结束后接口 data 为 null —— 调用方必须能处理 None。

    _post 内部 return j['data']，结束的比赛 data 就是 null。
    如果不判空直接 .get() 会 AttributeError。
    """
    c = make_client(None)
    assert c.fetch_state('anything') is None


def test_returns_none_for_non_dict_3():
    """万一接口返回列表或标量，也要安全返回 None 而不是崩。"""
    for bad in ([], 'x', 0, [{'a': 1}]):
        c = make_client(bad)
        assert c.fetch_state('m') is None


# ---------------------------------------------------------------- 字段语义

def test_score_comes_from_hmp_amp_not_msScoreList():
    """当前比分必须用 hmp/amp，不能用 msScoreList。

    实测量到过一场：hmp=0 amp=1（比分 0:1），但 msScoreList 里全是 0。
    拿 msScoreList 当比分是错的 —— 它是分阶段快照。
    """
    c = make_client(LIVE_SAMPLE)
    d = c.fetch_state('m')
    assert (d['hmp'], d['amp']) == ('0', '0')
    # msScoreList 只记录阶段性的值，可能滞后
    assert all(x['hmp'] == '0' and x['amp'] == '0' for x in d['msScoreList'])


def test_finished_match_has_score_but_no_clock():
    c = make_client(FINISHED_SAMPLE)
    d = c.fetch_state('m')
    assert d['ms'] == 100
    assert 'mt' not in d or not d['mt']
    assert (d['hmp'], d['amp']) == ('1', '1')


def test_upcoming_match_has_empty_score():
    c = make_client(UPCOMING_SAMPLE)
    d = c.fetch_state('m')
    assert d['ms'] == 0
    assert d['hmp'] == '' and d['amp'] == ''


def test_card_counts_available():
    """红黄牌当前状态也在 0014 里，角球不在。"""
    c = make_client(LIVE_SAMPLE)
    d = c.fetch_state('m')
    assert d['hyc'] == 0 and d['ayc'] == 2     # 黄牌
    assert d['hrc'] == 0 and d['arc'] == 0     # 红牌
    assert 'hck' not in d, '角球字段尚未确认，不应假定存在'


def test_sportradar_id_present():
    c = make_client(LIVE_SAMPLE)
    d = c.fetch_state('m')
    assert d['tmi'] == 'sr:match:76409632'
    assert d['tsi'] == 'sr:sport:1'


# ---------------------------------------------------------------- action

def test_uses_action_0014():
    """确认请求打的是状态 action，不是盘口 action。"""
    used = {}

    def fake_post(action, body):
        used['action'] = action
        used['body'] = body
        return LIVE_SAMPLE

    c = H.KessbetHttp.__new__(H.KessbetHttp)
    c._post = fake_post
    c.fetch_state('245162411202482764')
    assert used['action'] == '0014'
    assert used['body'] == {'matchId': '245162411202482764'}
