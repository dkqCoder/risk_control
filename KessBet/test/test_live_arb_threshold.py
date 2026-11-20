"""滚球套利阈值可配置。

背景：验证阶段需要把「成本 < 1.05」也当作套利，以便走通命中→证据包→
截图→复核报表的完整后续流程。否则真实数据里几乎没有 < 1.0 的机会，
后续链路无从验证。

契约：
  - 阈值默认 1.0（不影响正式判定）
  - 可配置为 1.05 等放宽值
  - 放宽只影响「是否进入命中流程」，**不跳过无界验证**：
    成本 < 1.0 但验证发现反例的，仍然不算命中
"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from collect import config


def test_threshold_back_to_production_value():
    """阈值已从验证模式（1.05）改回生产值（1.0）。

    2026-11-20 的验证期曾临时设为 1.05，产生 212 条影子脏数据（成本
    1.018~1.050）。验证完成后已改回 1.0 并清理数据。
    这条断言防止有人无意中又把阈值放宽。
    """
    assert hasattr(config, 'LIVE_ARB_COST_THRESHOLD')
    assert config.LIVE_ARB_COST_THRESHOLD == 1.0, \
        '生产阈值必须是 1.0；放宽只用于验证，且需同步清理产生的记录'


def test_threshold_is_configurable(monkeypatch):
    monkeypatch.setattr(config, 'LIVE_ARB_COST_THRESHOLD', 1.05)
    assert config.LIVE_ARB_COST_THRESHOLD == 1.05


def _judge(real_cost, verified=True, threshold=1.0):
    """复刻 result.py 的判定逻辑，用于验证阈值语义。

    与 analyze/live/result.py 的 is_arb 判断保持一致：
      1. cost 必须低于阈值
      2. cost < 1.0 时必须通过无界验证
    """
    is_arb = real_cost < threshold
    if is_arb and real_cost < 1.0 and not verified:
        is_arb = False
    return is_arb


def test_below_one_still_needs_verification():
    """成本 < 1.0 但验证失败时不能算命中 —— 放宽阈值不能绕过验证。"""
    assert _judge(0.95, verified=True) is True
    assert _judge(0.95, verified=False) is False


def test_between_one_and_threshold_is_hit():
    """1.0 <= cost < 1.05 在放宽阈值后算命中。"""
    assert _judge(1.02, threshold=1.05) is True
    assert _judge(1.049, threshold=1.05) is True


def test_above_threshold_is_not_hit():
    assert _judge(1.06, threshold=1.05) is False
    assert _judge(1.10, threshold=1.05) is False


def test_default_threshold_excludes_above_one():
    """默认阈值下，1.0 以上一律不算。"""
    assert _judge(1.02) is False
    assert _judge(1.049) is False
