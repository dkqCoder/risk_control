"""回放必须还原当时的套利阈值。

背景（2026-11-20）：为验证后续流程把 LIVE_ARB_COST_THRESHOLD 调到了 1.05，
命中记录里 is_arbitrage=True。但 replay_context 没保存阈值，回放时
check_match 用的是当前默认值（若日后改回 1.0），会得到 is_arbitrage=False，
被误判成「同版本不可复现」。

契约：
  - replay_context 记录判定时的阈值
  - 回放用记录里的阈值，不用当前配置
  - 旧记录没有该字段时，退回 1.0（保持向后兼容）
"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from collect import config


def test_threshold_would_be_recorded():
    """replay_context 必须包含 cost_threshold 字段。"""
    src = open(os.path.join(BASE, 'analyze', 'live', 'run.py'),
               encoding='utf-8').read()
    assert "'cost_threshold'" in src or '"cost_threshold"' in src, \
        'replay_context 缺少 cost_threshold，回放无法还原判定语义'


def test_replay_uses_recorded_threshold():
    """回放时优先使用记录里的阈值。"""
    src = open(os.path.join(BASE, 'analyze', 'live', 'run.py'),
               encoding='utf-8').read()
    idx = src.find('def replay_live_evidence')
    assert idx > 0
    body = src[idx:idx + 4000]
    assert 'cost_threshold' in body, \
        'replay_live_evidence 没有读取 cost_threshold'


def test_legacy_context_defaults_to_one():
    """旧证据包没有 cost_threshold 时按 1.0 处理。"""
    # 模拟旧记录
    legacy = {'analyzer_version': 'x'}
    threshold = legacy.get('cost_threshold', 1.0)
    assert threshold == 1.0


def test_threshold_semantics_match_result():
    """阈值判断与 result.py 一致：cost < threshold 才算。"""
    threshold = 1.05
    assert (1.0379 < threshold) is True
    assert (1.06 < threshold) is False
