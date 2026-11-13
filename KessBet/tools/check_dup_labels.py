"""统计「同一盘线下出现重复标签」的盘口数量。

重复标签是「盘线串门」bug 的直接症状：某条盘线的第一个选项被锁定时，
它的兄弟选项会被算到上一条盘线头上，形成 `line 3:0 -> [1, X, 2, X, 2]`
这种形态。

修复后应该为 0。这个脚本用来做修复前后的对照。
"""
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collect.kessbet.adapter import KessbetAdapter
from collect.kessbet.parse import parse_mari
from db.dao import Dao


def main():
    adapter = KessbetAdapter()
    dao = Dao()
    try:
        with dao.conn.cursor() as c:
            c.execute("""
            SELECT match_id, home_team, away_team FROM kessbet_match
            WHERE platform='kessbet' AND sport='sr:sport:1'
              AND kickoff_time > NOW()
            ORDER BY kickoff_time LIMIT 60
            """)
            metas = c.fetchall()

        n_raw = 0
        bad = []
        for mid, home, away in metas:
            try:
                raw = adapter.fetch_raw(mid)
            except Exception:
                continue
            n_raw += len(raw)
            for mk in raw:
                name, sels = parse_mari(mk.get('mari') or '')
                if not name:
                    continue
                g = defaultdict(list)
                for line, label, odds in sels:
                    g[line].append(label)
                for line, labels in g.items():
                    if len(labels) != len(set(labels)):
                        bad.append((home, away, name, line, labels,
                                    mk.get('mari') or ''))

        print('检查 %d 场 / %d 个盘口' % (len(metas), n_raw))
        print('出现重复标签的盘口: %d 个' % len(bad))
        for home, away, name, line, labels, mari in bad[:10]:
            print()
            print('  %s vs %s  %s' % (home[:18], away[:18], name))
            print('    line=%s 标签=%s' % (line, labels))
            print('    原始串: %s' % mari[:250])
    finally:
        dao.close()


if __name__ == '__main__':
    main()
