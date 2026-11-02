"""在一场真实比赛上，逐步骤展示套利计算过程。"""
import sys, json, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import kessbet_arb as PH

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(BASE, 'data', 'snapshots')
import pulp

MID = sys.argv[1] if len(sys.argv) > 1 else '245131079416058509'
HOME, AWAY = 'Ipswich', 'Nottingham'

print('=' * 78)
print(f'比赛: {HOME} vs {AWAY}   (matchId {MID})')
print(f'状态空间: 半场 {PH.H}x{PH.H} + 全场 {PH.G}x{PH.G}，合法组合 {PH.NS} 个')
print('=' * 78)

# ---------- 第 1 步：解析原始盘口 ----------
markets = json.load(open(os.path.join(DATA, '_today_match.json'), encoding='utf-8'))
print(f'\n【第 1 步】原始数据: {len(markets)} 个盘口\n')

parsed = []
for mk in markets:
    nm, sels = PH.parse_mari(mk['mari'])
    if nm:
        parsed.append((nm, sels))

# 展示几个关键盘口的原文
for want in ['1x2', 'Total', 'Correct Score']:
    for nm, sels in parsed:
        if nm == want:
            print(f'  --- {nm} 原文 ---')
            print(f'    {[ (l, lab, o) for l, lab, o in sels ][:6]}')
            break
print()

# ---------- 第 2 步：映射到比分格 ----------
print('【第 2 步】把选项映射到状态格（举 3 个例子）\n')
examples = [
    ('1x2', '', '', '1', '全场主队进球 > 客队进球'),
    ('Total', '', '2.5', 'over', '全场总进球 > 2.5'),
    ('Both Teams To Score', '', '', 'yes', '全场主客队都进球 (h>0 且 a>0)'),
]
for mk, side, line, lab, desc in examples:
    m = PH.covers(mk, side, line, lab)
    if m:
        print(f'  {mk:22} {lab:6} 赔率见下')
        print(f'    规则: {desc}')
        print(f'    覆盖 {bin(m).count("1"):>5} / {PH.NS} 个状态')
        # 找这个选项的实际赔率
        for nm, sels in parsed:
            if nm == mk:
                for l, lb, o in sels:
                    if lb == lab and (l == line or not line):
                        print(f'    赔率: {o}   ->  1/赔率 = {1/o:.6f}')
                        break
        print()

# ---------- 第 3 步：收集所有候选 ----------
print('【第 3 步】收集所有可映射的选项\n')
cands = PH.collect(os.path.join(DATA, '_today_match.json'), HOME, AWAY)
print(f'  共 {len(cands)} 个选项进入候选池')
from collections import Counter
kinds = Counter(c[2].split('|')[0] for c in cands)
print(f'  来自 {len(kinds)} 种玩法:')
for k, v in kinds.most_common(8):
    print(f'     {v:>3}  {k}')
print()

# ---------- 第 4 步：整数规划求解 ----------
print('【第 4 步】整数规划：最小化 Σ(1/赔率)，约束是每个状态至少被覆盖一次\n')
print('  目标函数:  min  Σ (1/oᵢ) · xᵢ       xᵢ ∈ {0,1}')
print(f'  约束条件:  对每个状态 s ∈ [0, {PH.NS}):  Σ xᵢ ≥ 1   (对所有覆盖 s 的 i)')
print()

pool = [(m, 1.0 / o, d) for m, o, d in cands if m]
prob = pulp.LpProblem('arb', pulp.LpMinimize)
xs = [pulp.LpVariable(f'x{i}', cat=pulp.LpBinary) for i in range(len(pool))]
prob += pulp.lpSum(pool[i][1] * xs[i] for i in range(len(pool)))
for c in range(PH.NS):
    cv = [i for i, (m, cc, d) in enumerate(pool) if (m >> c) & 1]
    prob += pulp.lpSum(xs[i] for i in cv) >= 1
prob.solve(pulp.PULP_CBC_CMD(msg=0))

chosen = [(pool[i][2], pool[i][1], bin(pool[i][0]).count('1'))
          for i in range(len(pool)) if xs[i].value() and xs[i].value() > 0.5]

print('  === 最优解 ===')
total = 0
for d, c, cells in chosen:
    parts = d.split('|')
    name, line, lab = parts[0], parts[1], parts[2]
    odds = 1 / c
    print(f'    {name:28} {("盘线=" + line) if line else "":14} {lab:8}')
    print(f'      赔率 {odds:>6}  ->  1/赔率 = {c:.6f}   覆盖 {cells} 格')
    total += c
print(f'    {"-" * 60}')
print(f'    合计成本 = {total:.6f}')
print()

# ---------- 第 5 步：验证覆盖 ----------
print('【第 5 步】验证覆盖完整性\n')
u = 0
for i in range(len(pool)):
    if xs[i].value() and xs[i].value() > 0.5:
        u |= pool[i][0]
covered = bin(u).count('1')
print(f'  覆盖状态数: {covered} / {PH.NS}')
print(f'  是否完整: {"是 ✅" if u == PH.FULL else "否 ❌"}')
print()

# ---------- 第 6 步：套利判定 ----------
print('【第 6 步】套利判定\n')
print(f'  最小成本 = {total:.6f}')
print(f'  返奖率   = 1 / {total:.6f} = {1/total:.6f}  ({1/total*100:.2f}%)')
print()
if total < 1:
    print(f'  ✅ 套利机会！每投入 {total*100:.2f} 元，稳收 100 元，净赚 {(1-total)*100:.2f} 元')
else:
    print(f'  ❌ 无套利。每投入 {total*100:.2f} 元，只能收回 100 元，亏 {(total-1)*100:.2f} 元')
