"""验证「盘线串门」bug。

## 假设

parse_mari 里，如果某条盘线的**第一个选项**被锁定（`,0,`）或赔率为 0，
`_split_selection` 返回 None，那段的前缀（盘线名）就被丢掉了。
后续选项会继承**上一条盘线**的名字 → 跨盘线串门。

## 验证方法

扫原始 mari 串，找形态：
    ...^2:0,id/0=id,4.85,0,2@...
                ↑ 第一条腿锁定
如果解析结果里 2:0 这条线混进了属于别的线的选项，就证实了。

具体判据：解析后同一个 line 下出现**重复标签**（如两个 `2`），
或者某一组缺少应有的标签（如三选一只有 2 和 X，没有 1）。
"""
import os
import sys
import re
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collect.kessbet.adapter import KessbetAdapter
from collect.kessbet.parse import parse_mari, split_team
from db.dao import Dao


def scan_raw(mari, home, away):
    """返回 (盘口名, 解析结果, 原始串) 中可疑的那些。"""
    name, sels = parse_mari(mari)
    if not name:
        return None

    # 原始串里每条盘线第一个选项的标记（/1 或 /0）
    first_flags = re.findall(r'(?:\^|\|)[^|^]*?(\d+):(\d+),[^@^]*?/([01])=', mari)
    locked_lines = [(f'{a}:{b}') for a, b, flag in first_flags if flag == '0']
    if not locked_lines:
        return None

    by_line = defaultdict(list)
    for line, label, odds in sels:
        by_line[line].append(label)

    problems = []
    for line, labels in by_line.items():
        if len(labels) != len(set(labels)):
            problems.append((line, labels, '重复标签'))
        elif len(labels) < 2:
            problems.append((line, labels, '只剩一个选项'))

    if not problems:
        return None
    return name, problems, locked_lines, mari


def main():
    adapter = KessbetAdapter()
    dao = Dao()
    checked = 0
    hits = 0
    try:
        with dao.conn.cursor() as c:
            c.execute("""
            SELECT match_id, home_team, away_team FROM kessbet_match
            WHERE platform='kessbet' AND sport='sr:sport:1'
              AND kickoff_time > NOW()
            ORDER BY kickoff_time LIMIT 40
            """)
            metas = c.fetchall()

        for mid, home, away in metas:
            try:
                raw = adapter.fetch_raw(mid)
            except Exception:
                continue
            checked += 1
            for mk in raw:
                r = scan_raw(mk.get('mari') or '', home, away)
                if not r:
                    continue
                name, problems, locked, mari = r
                hits += 1
                print('=' * 78)
                print('%s vs %s  盘口 %s' % (home[:20], away[:20], name))
                print('  锁定的盘线（第一个选项 /0）：', locked)
                for line, labels, why in problems:
                    print('  ⚠ line=%-6s 解析出 %s  → %s' % (line, labels, why))
                print('  原始串: %s' % mari[:300])

        print()
        print('检查 %d 场，发现 %d 处可疑' % (checked, hits))
    finally:
        dao.close()


if __name__ == '__main__':
    main()
