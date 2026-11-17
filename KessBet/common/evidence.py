"""common/evidence.py — E1 证据包（Task 6）。

每次已验证候选写一个不可变目录：

    request.json       请求 URL/action/body（脱敏）
    response.json      实际响应的状态码、必要响应头、原始文本、解包 markets
    normalized.json    规范选项（含原始 mari/市场 ID/可售标记）
    state.json         请求前后状态、比分、阶段、半场来源
    analysis.json      固定组合、赔率、金额、模型版本、结论
    verification.json  无界验证/取整后的最低返还与反例
    manifest.json      文件字节数、SHA-256、生成时间、版本；最后写

manifest 里的哈希只能发现文件后续变化，不能独立证明平台来源。
"""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime

SCHEMA_VERSION = 1
_SENSITIVE_KEYS = {
    'authorization', 'cookie', 'set-cookie', 'password', 'passwd',
    'secret', 'token', 'api_key', 'apikey', 'x-api-key',
}


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sanitize(value):
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            kl = str(k).lower()
            if kl in _SENSITIVE_KEYS:
                continue
            if any(s in kl for s in ('authorization', 'cookie',
                                     'password', 'secret', 'token')):
                continue
            out[k] = _sanitize(v)
        return out
    if isinstance(value, list):
        return [_sanitize(v) for v in value]
    if isinstance(value, str):
        return _sanitize_url(value)
    return value


def _sanitize_url(s: str) -> str:
    if not (s.startswith('http://') or s.startswith('https://')):
        return s
    if '?' not in s:
        return s
    base, query = s.split('?', 1)
    parts = []
    for pair in query.split('&'):
        name = pair.split('=', 1)[0].lower()
        if any(k in name for k in ('token', 'key', 'secret', 'auth',
                                   'password')):
            parts.append(pair.split('=', 1)[0] + '=***')
        else:
            parts.append(pair)
    return base + '?' + '&'.join(parts)


def _load(value):
    if isinstance(value, (dict, list)):
        return value
    if not value:
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


def _write_json(path: str, obj) -> None:
    tmp = f'{path}.tmp-{uuid.uuid4().hex}'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def write_evidence_package(evidence_dir: str, snapshot: dict,
                           analysis: dict, verification: dict) -> dict:
    """写一个完整证据包并返回 manifest。失败抛异常，调用方决定是否降级。"""
    os.makedirs(evidence_dir, exist_ok=True)

    request = _sanitize(_load(snapshot.get('request_json'))
                        or snapshot.get('request') or {})
    response = {
        'status': snapshot.get('status'),
        'response_status': snapshot.get('response_status'),
        'response_headers': _sanitize(
            snapshot.get('response_headers') or {}),
        'raw_body': snapshot.get('raw_body'),
        'response_hash': snapshot.get('response_hash'),
        'markets': _load(snapshot.get('raw_payload')) or
                    snapshot.get('markets') or [],
        'note': 'raw_body 是实际响应文本；markets 是解包后的列表，两者不混称',
    }
    rows = _load(snapshot.get('rows_json')) or snapshot.get('rows') or []
    normalized = {
        'parser_version': snapshot.get('parser_version'),
        'filter_policy': snapshot.get('filter_policy'),
        'input_fingerprint': snapshot.get('input_fingerprint'),
        'selections': rows,
    }
    state = {
        'match_id': snapshot.get('match_id'),
        'snapshot_id': snapshot.get('snapshot_id'),
        'generation': snapshot.get('generation'),
        'request_started_at': str(snapshot.get('request_started_at') or ''),
        'response_received_at': str(
            snapshot.get('response_received_at') or ''),
        'state_before': _load(snapshot.get('state_before')),
        'state_after': _load(snapshot.get('state_after')),
        'minute': snapshot.get('minute'),
        'home_score': snapshot.get('home_score'),
        'away_score': snapshot.get('away_score'),
        'ht_home': snapshot.get('ht_home'),
        'ht_away': snapshot.get('ht_away'),
        'ht_source': snapshot.get('ht_source'),
        'phase': snapshot.get('phase'),
        'phase_source': snapshot.get('phase_source'),
    }
    files = {
        'request.json': request,
        'response.json': response,
        'normalized.json': normalized,
        'state.json': state,
        'analysis.json': analysis,
        'verification.json': verification,
    }

    manifest = {
        'schema_version': SCHEMA_VERSION,
        'generated_at': datetime.now().isoformat(sep=' ', timespec='seconds'),
        'evidence_id': snapshot.get('snapshot_id'),
        'snapshot_id': snapshot.get('snapshot_id'),
        'analysis_id': (analysis or {}).get('analysis_id'),
        'match_id': snapshot.get('match_id'),
        'input_fingerprint': snapshot.get('input_fingerprint'),
        'analyzer_version': (analysis or {}).get('analyzer_version'),
        'files': {},
    }
    for name, obj in files.items():
        path = os.path.join(evidence_dir, name)
        _write_json(path, obj)
        with open(path, 'rb') as f:
            data = f.read()
        manifest['files'][name] = {
            'bytes': len(data),
            'sha256': _sha256_bytes(data),
        }
    # manifest 最后写；它本身不列入 files，避免自引用。
    _write_json(os.path.join(evidence_dir, 'manifest.json'), manifest)
    return manifest
