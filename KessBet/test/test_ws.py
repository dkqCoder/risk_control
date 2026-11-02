"""collect/kessbet/ws.py 的单元测试（不依赖网络）。

运行: py -m pytest test/test_ws.py -v
"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from collect.kessbet.ws import KessbetWs


# ---------- extract_match_id ----------

def test_extract_from_bet_change():
    raw = ('42["bet_change","{\\"matchId\\":\\"245132131683049470\\",'
           '\\"update\\":[\\"1,...\\"],\\"insert\\":[],\\"delete\\":[]}",""]')
    assert KessbetWs.extract_match_id(raw) == '245132131683049470'


def test_extract_from_object_payload():
    """payload 已是对象（非字符串）时也要能解析。"""
    raw = '42["bet_change",{"matchId":"m123","update":[],"insert":[],"delete":[]},""]'
    assert KessbetWs.extract_match_id(raw) == 'm123'


def test_ignore_other_events():
    assert KessbetWs.extract_match_id('42["match_change","{\\"matchId\\":\\"m1\\"}"]') is None
    assert KessbetWs.extract_match_id('42["casino_draw","x"]') is None
    assert KessbetWs.extract_match_id('40') is None
    assert KessbetWs.extract_match_id('2') is None


def test_ignore_malformed():
    assert KessbetWs.extract_match_id('42["bet_change"') is None
    assert KessbetWs.extract_match_id('42["bet_change","not json"]') is None
    assert KessbetWs.extract_match_id('') is None


def test_real_frame():
    """用实测抓到的真实帧验证。"""
    raw = ('42["bet_change","{\\"delete\\":[],\\"insert\\":[],'
           '\\"matchId\\":\\"245064541782708268\\",\\"update\\":['
           '\\"1,245144690167422503/1|245132131683049470,1|245144690167422506,1.95,1,1\\"]}",""]')
    assert KessbetWs.extract_match_id(raw) == '245064541782708268'


# ---------- 订阅集合 ----------

class FakeWs:
    """记录发送内容的假 WebSocket。"""
    def __init__(self):
        self.sent = []

    def send_str(self, s):
        self.sent.append(s)


def test_subscribe_dedups():
    calls = []
    ws = KessbetWs(on_change=lambda m: calls.append(m))
    ws._ws = FakeWs()

    n1 = ws.subscribe(['a', 'b', 'c'])
    n2 = ws.subscribe(['b', 'c', 'd'])      # 只有 d 是新的

    assert n1 == 3
    assert n2 == 1
    assert ws.subscribed_count == 4
    assert len(ws._ws.sent) == 2, '应该只发两次（首批 + 新的一批）'


def test_subscribe_batches():
    ws = KessbetWs(on_change=lambda m: None)
    ws._ws = FakeWs()
    ws.subscribe([f'm{i}' for i in range(75)], batch_size=30)
    # 75 个 -> 30 + 30 + 15 = 3 批
    assert len(ws._ws.sent) == 3
    assert all(s.startswith('42["watch_match",') for s in ws._ws.sent)


def test_prune_removes_ended():
    ws = KessbetWs(on_change=lambda m: None)
    ws._ws = FakeWs()
    ws.subscribe(['a', 'b', 'c', 'd'])
    removed = ws.prune({'a', 'c'})
    assert removed == 2
    assert ws.subscribed_count == 2


def test_prune_keeps_all_when_superset():
    ws = KessbetWs(on_change=lambda m: None)
    ws._ws = FakeWs()
    ws.subscribe(['a', 'b'])
    assert ws.prune({'a', 'b', 'c'}) == 0
    assert ws.subscribed_count == 2
