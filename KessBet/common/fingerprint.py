"""common/fingerprint.py — 滚球输入的稳定指纹。

指纹用于：
  - 判断两次观察是不是同一份输入（重复 WS、只变接收时间）；
  - 命中幂等（同一输入不重复写正式命中）。

**不包含接收时间**。身份优先用平台 `mari` 串（含市场/选项 ID 和锁定标记）；
规范五元组只作辅助。v1 尚未把平台市场/选项 ID 拆成独立字段，因此以
`mari` 原文的哈希承担平台身份。
"""
from __future__ import annotations

import hashlib
import json

RULES_VERSION = 'live-rules-v1'


def _norm(s) -> dict:
    if isinstance(s, dict):
        odds = s.get('odds')
        return {
            'mari': str(s.get('mari') or ''),
            'market': str(s.get('market') or ''),
            'side': str(s.get('side') or ''),
            'line': str(s.get('line') or ''),
            'label': str(s.get('label') or ''),
            'odds': (format(float(odds), '.10g')
                     if odds is not None else ''),
            'locked': bool(s.get('locked', False)),
            'market_id': str(s.get('market_id') or ''),
        }
    # 兼容五元组 (market, side, line, label, odds)；没有平台身份。
    market, side, line, label, odds = s
    return {
        'mari': '', 'market': str(market or ''), 'side': str(side or ''),
        'line': str(line or ''), 'label': str(label or ''),
        'odds': format(float(odds), '.10g') if odds is not None else '',
        'locked': False, 'market_id': '',
    }


def input_fingerprint(selections, home: int, away: int, phase: str,
                      ht, policy: str, rules_version: str) -> str:
    """返回 64 位 SHA-256。输入相同（含顺序无关）则结果相同。"""
    items = [_norm(s) for s in selections]
    items.sort(key=lambda x: json.dumps(x, sort_keys=True,
                                        ensure_ascii=False))
    payload = {
        'home': int(home), 'away': int(away),
        'phase': str(phase or 'unknown'),
        'ht': list(ht) if ht else None,
        'policy': str(policy or ''),
        'rules_version': str(rules_version or ''),
        'selections': items,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(',', ':'),
                     ensure_ascii=False).encode('utf-8')
    return hashlib.sha256(raw).hexdigest()
