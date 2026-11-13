"""用 Correct Score 交叉验证 Handicap 盘线的语义。

## 问题

我们的 covers() 对 `Handicap` 的假设是：

    line 'H:A' -> (hl, al) = (H, A)
    '1'  ->  h2 + hl > a2 + al        （主队让球后仍胜）
    'X'  ->  h2 + hl == a2 + al
    '2'  ->  a2 + al > h2 + hl
    '0:1' 表示主队让 0 球、客队让 1 球，即「客队 +1 让球」

实测发现有矛盾的地方（2026-11-13，Equatorial Guinea）：
    同一场、同一比分 0:0、同一盘线 0:1
    01:43  2 @1.15      按我们的语义「客队不输」→ 合理
    01:50  2 @100       按我们的语义应该是 1.05 左右 → 完全不合理

如果语义搞反了，所有用让球盘做的覆盖计算都是错的，必须查清。

## 方法：Cross Score 做基准

`Correct Score` 的标签是 '1:0' / '0:1' 这种，**语义没有歧义**。
同一时刻的 Correct Score 赔率可以算出各终局的隐含概率，
据此可以算出任意让球线下的真实概率分布：

    P('1') = Σ P(终局)  满足 主队进球 + hl > 客队进球 + al
    P('X') = Σ P(终局)  满足两边相等
    P('2') = 1 - P('1') - P('X')

再和 Handicap 盘口自己的赔率反推的概率对比。
如果 1 和 2 对不上但互换后能对上，说明语义反了。

用法：
    py tools/check_handicap_semantics.py
"""
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.dao import Dao


def parse_line(line):
    try:
        a, b = line.split(':')
        return float(a), float(b)
    except Exception:
        return None


def score_from_label(label):
    if ':' not in label:
        return None
    try:
        h, a = label.split(':')
        return int(h), int(a)
    except ValueError:
        return None


def analyse_snapshot(cur, mid, ts, current_score):
    """取一个快照，比较 Correct Score 推出的概率和 Handicap 的价格。"""
    cur.execute("""
    SELECT market, side, line, selection, odds
    FROM kessbet_odds_live_history
    WHERE match_id=%s AND changed_time=%s AND market IN ('Correct Score','Handicap')
    """, (mid, ts))
    rows = cur.fetchall()

    cs = {}
    hcp = defaultdict(dict)
    for mkt, side, line, sel, odds in rows:
        o = float(odds)
        if o <= 1.0:
            continue
        if mkt == 'Correct Score' and not side:
            cs[sel] = o
        elif mkt == 'Handicap' and not side:
            hcp[line][sel] = o

    if not cs or not hcp:
        return None

    # Correct Score 的隐含概率（去掉抽水后归一化）
    probs = {}
    for lab, o in cs.items():
        s = score_from_label(lab)
        if s:
            probs[s] = probs.get(s, 0.0) + 1.0 / o
    if not probs:
        return None
    total = sum(probs.values())
    if total <= 0:
        return None
    probs = {k: v / total for k, v in probs.items()}

    out = []
    for line, sels in hcp.items():
        hp = parse_line(line)
        if not hp or len(sels) < 3:
            continue
        hl, al = hp

        # 我们的语义
        p1 = sum(p for (h, a), p in probs.items() if h + hl > a + al)
        px = sum(p for (h, a), p in probs.items() if h + hl == a + al)
        p2 = 1.0 - p1 - px

        # 盘口自己的隐含概率
        q = {k: 1.0 / v for k, v in sels.items()}
        qt = sum(q.values())
        q = {k: v / qt for k, v in q.items()}

        out.append({
            'line': line,
            'ours': {'1': p1, 'X': px, '2': p2},
            'market': q,
            'odds': sels,
        })
    return out


def show(mid, ts, current_score, results):
    print('=' * 78)
    print('%s @ %s   当时比分 %s' % (mid[-6:], ts, current_score))
    for r in results:
        o, m = r['ours'], r['market']
        # 三种可能的对应关系，看哪个最吻合
        mapping = []
        for name, perm in (('原样 1/X/2', ('1', 'X', '2')),
                           ('1↔2 互换', ('2', 'X', '1'))):
            err = sum(abs(o[perm[i]] - m[k])
                      for i, k in enumerate(('1', 'X', '2')))
            mapping.append((err, name))

        best = min(mapping)
        print()
        print('  line %-6s  赔率 1@%-7s X@%-7s 2@%-7s' % (
            r['line'],
            r['odds'].get('1'), r['odds'].get('X'), r['odds'].get('2')))
        print('    Correct Score 推出: 1=%.3f  X=%.3f  2=%.3f' % (
            o['1'], o['X'], o['2']))
        print('    Handicap 反推:      1=%.3f  X=%.3f  2=%.3f' % (
            m['1'], m['X'], m['2']))
        print('    → 最吻合: %s (误差 %.3f)' % (best[1], best[0]))


def main():
    dao = Dao()
    cur = dao.conn.cursor()
    try:
        cur.execute("""
        SELECT h.match_id, h.changed_time
        FROM kessbet_odds_live_history h
        WHERE h.market IN ('Correct Score','Handicap')
          AND h.changed_time > NOW() - INTERVAL 4 DAY
        GROUP BY h.match_id, h.changed_time
        HAVING SUM(h.market='Correct Score') > 8
           AND SUM(h.market='Handicap') > 5
        ORDER BY h.changed_time DESC LIMIT 8
        """)
        targets = cur.fetchall()
        print('找到 %d 个可用于交叉验证的快照\n' % len(targets))

        for mid, ts in targets:
            cur.execute("""
            SELECT home_score, away_score FROM kessbet_odds_live_history
            WHERE match_id=%s AND changed_time=%s
              AND home_score IS NOT NULL LIMIT 1
            """, (mid, ts))
            r = cur.fetchone()
            score = '%s:%s' % (r[0], r[1]) if r else '?'
            res = analyse_snapshot(cur, mid, ts, score)
            if res:
                show(mid, ts, score, res)
    finally:
        dao.close()


if __name__ == '__main__':
    main()
