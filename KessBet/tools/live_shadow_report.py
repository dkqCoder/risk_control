"""tools/live_shadow_report.py — 影子运行只读报告（Task 8）。

不写正式命中、不改数据库。输出：
  - 当前状态分布（no_candidate/unverifiable/shadow_candidate/...）
  - 已验证候选数
  - 证据包完整率（manifest 是否存在）
  - 报价过期数/年龄分布

用法：py tools/live_shadow_report.py [--limit 20]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from db.dao import Dao, beijing_now  # noqa: E402


def build_report(limit: int = 20) -> dict:
    dao = Dao()
    try:
        with dao.conn.cursor() as c:
            c.execute("""SELECT analysis_status, COUNT(*)
                         FROM kessbet_live_result_cost
                         WHERE platform='kessbet'
                         GROUP BY analysis_status""")
            status = {r[0] or 'NULL': int(r[1]) for r in c.fetchall()}

            c.execute("""SELECT COUNT(*)
                         FROM kessbet_live_result_cost
                         WHERE platform='kessbet'
                           AND verification_status='verified'
                           AND cost IS NOT NULL AND cost < 1.0""")
            verified = int(c.fetchone()[0])

            c.execute("""SELECT COUNT(*)
                         FROM kessbet_live_result_cost
                         WHERE platform='kessbet'
                           AND odds_updated < NOW() - INTERVAL 90 SECOND""")
            expired = int(c.fetchone()[0])

            c.execute("""SELECT evidence_status, COUNT(*)
                         FROM kessbet_arbitrage_snapshot
                         WHERE platform='kessbet' AND is_live=1
                         GROUP BY evidence_status""")
            evidence = {r[0] or 'NULL': int(r[1]) for r in c.fetchall()}

            c.execute("""SELECT id, evidence_path FROM
                         kessbet_arbitrage_snapshot
                         WHERE platform='kessbet' AND is_live=1
                           AND evidence_status='complete'
                         ORDER BY id DESC LIMIT %s""", (limit,))
            missing = []
            checked = 0
            for sid, path in c.fetchall():
                checked += 1
                if not path or not os.path.isfile(
                        os.path.join(path, 'manifest.json')):
                    missing.append(sid)

            c.execute("""SELECT match_id, snapshot_id, cost,
                                analysis_status, status_reason,
                                verification_status, odds_updated
                         FROM kessbet_live_result_cost
                         WHERE platform='kessbet'
                         ORDER BY computed_time DESC LIMIT %s""", (limit,))
            latest = [{
                'match_id': r[0],
                'snapshot_id': r[1],
                'cost': float(r[2]) if r[2] is not None else None,
                'status': r[3],
                'reason': r[4],
                'verification': r[5],
                'odds_updated': str(r[6] or ''),
            } for r in c.fetchall()]

        return {
            'generated_at': str(beijing_now()),
            'status_distribution': status,
            'verified_candidates': verified,
            'expired_quotes': expired,
            'evidence_status': evidence,
            'evidence_packages_checked': checked,
            'evidence_manifests_missing': missing,
            'latest': latest,
        }
    finally:
        dao.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--limit', type=int, default=20)
    args = ap.parse_args()
    print(json.dumps(build_report(args.limit), ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
