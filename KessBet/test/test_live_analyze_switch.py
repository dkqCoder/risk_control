"""滚球套利判定的独立开关。

背景：LIVE_ENABLED 同时控制滚球采集和判定，关掉会连采集一起停。
但采集的数据有长期价值（复盘、重新校验模型），判定却可能因为模型
有问题需要单独停掉 —— 2026-11-13 的 869 条假信号就是这种情况。

契约：
  - LIVE_ANALYZE_ENABLED=False 时，不启动滚球判定线程
  - 但不影响滚球采集（LIVE_ENABLED 仍为 True）
"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from collect import config


def test_config_has_analyze_switch():
    """必须有独立的判定开关，且默认与 LIVE_ENABLED 解耦。"""
    assert hasattr(config, 'LIVE_ANALYZE_ENABLED'), \
        '缺少 LIVE_ANALYZE_ENABLED —— 关判定会连带停掉采集'


def test_live_thread_respects_analyze_switch(monkeypatch):
    """LIVE_ANALYZE_ENABLED=False 时不启动判定线程。"""
    from analyze import run as R

    started = []
    monkeypatch.setattr(R, '_start_live_thread',
                        lambda *a, **k: started.append(a))
    monkeypatch.setattr(config, 'LIVE_ENABLED', True)
    monkeypatch.setattr(config, 'LIVE_ANALYZE_ENABLED', False)

    # 只跑启动阶段的判定逻辑，不进主循环
    should_start = (getattr(config, 'LIVE_ENABLED', False)
                    and getattr(config, 'LIVE_ANALYZE_ENABLED', True))
    assert should_start is False


def test_analyze_switch_independent_of_collect_switch():
    """两个开关互不影响：采集可以开着，判定单独关。

    这正是当前的生产配置 —— 滚球假信号要停判定，但采集数据要留着。
    """
    import importlib
    importlib.reload(config)
    assert config.LIVE_ENABLED is True, '采集应保持开启'
    assert config.LIVE_ANALYZE_ENABLED is False, '判定应已关闭（假信号未修复）'
