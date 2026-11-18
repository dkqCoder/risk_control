"""analyze/live/result.py — 滚球「比赛结果」套利判定（模型 B2）。

## 和模型 A（窗口盘）的区别

模型 A（`window.py`）判的是单个时间窗口盘自不自洽 —— 不需要比分。
模型 B 判的是**整场比赛的终局**：当前比分已知，所有「比赛结果」盘口
组合起来，能不能以 Σ(1/赔率) < 1 覆盖全部可达终局。需要比分。

## 状态空间：复用赛前的四元组，不新写一套

赛前 `soccer.STATES` 是 2025 个 `(h1, a1, h2, a2)`。
滚球时它仍然适用 —— 只是**不是所有终局都可能**。

    当前 1:0，第 62 分钟  →  只可能 h2>=1 且 a2>=0，且半场比分已固定

关键设计：**保持 2025 位宽，用一个「不可达状态」的幽灵候选覆盖掉**。

为什么不用「把 mask 重映射到子集」：
  重映射要维护一份 index 映射，状态空间定义一改（H/G 调整）就静默错位。
  实测两种方法结果完全一致，性能差 47ms（90 状态时），不值得。
  详见 docs/superpowers/specs/2026-11-12-live-result-arbitrage.md §3.1

## 时段过滤：半场盘口必须按时段筛

实测（2026-11-12）：第 60 分钟时 `kessbet_odds_live` 里仍有
12 个 `1st Half - *` 选项 —— 平台赛前就挂，滚球时不主动撤。
上半场结束后这些盘口**已结算**，参与判定会算出不可能的套利。

反向同理：`2nd Half - *` 赛前就挂着，上半场时还没生效。
"""
from __future__ import annotations

import logging
from collections import defaultdict

from analyze import solver
from analyze.covers import soccer
from collect import config

log = logging.getLogger(__name__)

NS = soccer.NS
FULL = soccer.FULL
STATES = soccer.STATES

# 幽灵候选的赔率。1/1e9 ≈ 1e-9，对最优值的影响远小于浮点精度；
# 用 0 会除零，用极小值能让 solver 选它（成本几乎为 0）又不影响结果。
GHOST_ODDS = 1e9
GHOST_DESC = '__unreachable__'

# 状态空间的每队进球上限是 soccer.G-1 = 9。一旦当前比分逼近这个上限，
# 模型就无法表示“再进一球”的终局（例如 9:0 时的 10:0），任何依赖上限
# 边界的覆盖都可能是假的。
#
# 这是**临时防线**，不是完备性证明：把上限从 9 改到 20/30 只是移动漏洞
# 边界。正式认证必须由独立的无界反例验证通过（见 docs/
# live-arbitrage-audit-and-remediation-20261117.md F01）。
#
# 阈值取 G-2：8:0 时再进两球就超过上限，模型已经不可信。
MODEL_CAP_GUARD = soccer.G - 2


def score_space_incomplete(home: int | None, away: int | None) -> bool:
    """当前比分是否已接近状态空间上限，导致模型不完备。

    8:0 及以上（任一队）一律拒绝认证，直到无界验证器上线。
    """
    if home is None or away is None:
        return True
    return home >= MODEL_CAP_GUARD or away >= MODEL_CAP_GUARD

# ---------- 参与判定的盘口 ----------

# 全场结果盘。只列 covers() 支持、且语义是「比赛最终结果」的。
FULL_TIME_MARKETS = frozenset({
    '1x2', 'Total', 'Double Chance', 'Both Teams To Score',
    'Exact Goals', 'Clean Sheet', 'Halftime/Fulltime',
    'Highest Scoring Half', 'Odd/Even', 'Correct Score', 'Handicap',
    '1x2 & Both Teams To Score',
    '1x2 & Total', 'Win To Nil', 'To Win', 'Which Team To Score',
    'Multigoals', 'Goal Range', 'Winning Margin',
    'Total & Both Teams To Score', 'Double Chance & Total',
    'Double Chance & Both Teams To Score',
    'Or Over 2.5', 'Or Under 2.5', 'Or Both Teams To Score',
    'Or Any Clean Sheet', 'Draw Or Over 2.5', 'Draw Or Under 2.5',
    'Draw Or Both Teams To Score', 'Draw Or Any Clean Sheet',
})

# 半场盘。两条都靠 prefix 识别，时段由 half_of() 决定。
HALF_PREFIXES = ('1st Half - ', '2nd Half - ')

# 明确排除的（不要因为「看起来像结果盘」就放进来）：
#   Total Corners / Corner 1x2 / Booking 1x2 / Sending Off  → 角球和牌
#   N Minutes - * From A To B                                → 时间窗口盘
#   Which Team Wins The Rest Of The Match                    → 剩余时间语义
#   1st Goal / 2nd Goal / Nth Corner                         → 事件盘
#   Draw No Bet                                              → covers() 不支持
EXCLUDED_PREFIXES = ('N Minutes',)


def is_result_market(market: str) -> bool:
    """该盘口是否属于「比赛结果」类，参与判定。"""
    if not market:
        return False
    if any(market.startswith(p) for p in HALF_PREFIXES):
        base = market.split(' - ', 1)[1]
        return base in FULL_TIME_MARKETS
    return market in FULL_TIME_MARKETS


def half_of(market: str) -> int:
    """0=全场，1=上半场，2=下半场。"""
    if market.startswith('1st Half - '):
        return 1
    if market.startswith('2nd Half - '):
        return 2
    return 0


def market_active(market: str, minute: int | None) -> bool:
    """该盘口在此刻是否还「活着」（未结算、已生效）。

    没有分钟信息时一律返回 True —— 宁可多算一场，
    也不要因为缺一个次要字段就整场跳过。

    实测的边界情形：
      - 第 60 分钟仍有 1st Half 盘口挂着（已结算但没撤）
      - 第 0 分钟就有 2nd Half 盘口挂着（未生效）
    """
    if minute is None:
        return True
    h = half_of(market)
    if h == 1:
        return minute <= 45
    if h == 2:
        return minute > 45
    return True


# ---------- 可达终局 ----------

def reach_mask(home: int, away: int,
               ht_home: int | None = None,
               ht_away: int | None = None,
               first_half_live: bool = False) -> int:
    """当前比分下可达的终局，返回 2025 位 mask。

    home/away:    当前全场比分
    ht_home/away: 半场比分。下半场时必须给（半场已结束，已知）；
                  上半场时传 None（半场比分就是当前比分或更高）。
    """
    m = 0
    for i, (h1, a1, h2, a2) in enumerate(STATES):
        if h2 < home or a2 < away:
            continue
        if first_half_live and (h1 < home or a1 < away):
            continue
        if ht_home is not None and (h1 != ht_home or a1 != ht_away):
            continue
        m |= 1 << i
    return m


def reach_count(mask: int) -> int:
    return bin(mask).count('1')


# ---------- 候选构造 ----------

def build_candidates(selections, minute: int | None = None,
                     rm: int | None = None):
    """把选项转成求解器候选。

    selections: [(market, side, line, label, odds), ...]
    rm:         可达终局 mask。给了就顺手剔除「已确定」的选项（见下）。

    返回 [(mask, odds, desc), ...]。

    ⚠️ **已确定的选项必须剔除** —— 这是滚球特有的一步，赛前不需要。

    实测（2026-11-12，当前 1:0）：
        Total|H|0.5|over    覆盖 1980/1980 = 100%   必然成立
        Total||0.5|over     覆盖 1980/1980 = 100%   必然成立
        Total|H|0.5|under   覆盖    0/1980 =   0%   必然失败

    平台**不会**因为已经发生就撤掉这些盘，也不把赔率改成 1.00。
    实测第 15 分钟 1:0 时，`Total H 2.5 over @2.25` 和
    `Total A 0.5 under @2.50` 都在正常挂牌。

    把它们留在池子里，ILP 会立刻发现「买一个必然成立的选项就够了」，
    算出 Σ = 1/1.17 = 0.85 这种假套利。实测未过滤时 67% 的快照都
    被误判成套利，最低 Σ=0.0533。

    剔除后覆盖率既不是 0 也不是 100% 的选项才是真正「未定」的，
    才构成一次有意义的覆盖问题。
    """
    by_market = {}
    for mkt, side, line, lab, odds in selections:
        # F05/v1：半场盘整体隔离。阶段来源未权威前，不用 minute 启用它们。
        if mkt.startswith(('1st Half - ', '2nd Half - ')):
            continue
        if not is_result_market(mkt):
            continue
        if not market_active(mkt, minute):
            continue
        by_market.setdefault((mkt, side), []).append((line, lab, float(odds)))

    cands = []
    for (market, side), sels in by_market.items():
        if market in ('Correct Score', '1st Half - Correct Score'):
            named = 0
            other = None
            is_half = market.startswith('1st Half')
            limit = soccer.H if is_half else soccer.G
            for line, lab, odds in sels:
                if ':' in lab:
                    try:
                        hh, aa = map(int, lab.split(':'))
                    except ValueError:
                        continue
                    if hh >= limit or aa >= limit:
                        continue
                    if is_half:
                        m = soccer.build(lambda h1, a1, h2, a2, x=hh, y=aa:
                                         h1 == x and a1 == y)
                    else:
                        m = soccer.build(lambda h1, a1, h2, a2, x=hh, y=aa:
                                         h2 == x and a2 == y)
                    named |= m
                    cands.append((m, odds, f'{market}|{side}|{line}|{lab}'))
                elif lab == 'other':
                    other = odds
            # ★ 默认隔离 `other`，不参与认证。
            #
            # 平台临近结束时会锁定/撤下当前比分（3:0 等），解析器只保留
            # 可售项，`named` 因此缺少它；`FULL & ~named` 就把当前比分补进
            # `other`，和 Total 大球拼出假套利（2026-11-17 实测 Σ=0.33）。
            #
            # 平台对“其他比分”的结算规则没有可核验的定义前，不能根据
            # 当前可售列表反推它的覆盖范围。原始 `other` 仍保留在快照里，
            # 只是不进入求解器。
            if other is not None and getattr(
                    config, 'LIVE_CERTIFY_CORRECT_SCORE_OTHER', False):
                cands.append((FULL & ~named, other, f'{market}|other'))
            continue

        for line, lab, odds in sels:
            m = soccer.covers(market, side, line, lab)
            if m is not None:
                cands.append((m, odds, f'{market}|{side}|{line}|{lab}'))

    if getattr(config, 'LIVE_DROP_INCOMPLETE_MARKETS', True):
        cands = _drop_incomplete_markets(cands)

    if getattr(config, 'LIVE_DROP_BROKEN_MARKETS', False):
        cands = _drop_broken_markets(cands)

    if rm is not None:
        cands = _drop_decided(cands, rm)
    return cands


def _drop_broken_markets(cands):
    """剔除内部和明显不可能的盘口（数据损坏）。

    单一博彩公司内部必然有抽水，所以**同一个盘口**的 Σ(1/赔率)
    不可能显著小于 1。实测反例（2026-11-13 Madagascar）：

        Handicap 0:1   1 @100.0   2 @100.0   X @5.0
        Σ = 0.01 + 0.01 + 0.20 = 0.22

    这是平台停盘/占位时给的假价格（两边主胜都标 100），
    拿它和新鲜的 1x2 组合，能算出 Σ=0.05 的「套利」。
    真实盘口的内部和最小也在 1.05 以上（实测过 6 万组窗口盘，
    最低 1.0710）。

    阈值 0.90 留了余量：即使出现真实的单盘口定价错误，
    也很少低到 0.90 以下 —— 而数据损坏往往低到 0.2 这种量级。
    """
    by_market = {}
    for mask, odds, desc in cands:
        # desc 有两种形态，key 必须归一到同一个维度（market, side, line）：
        #   `market|side|line|label`  → 4 段（Correct Score 的具名比分也是这种）
        #   `market|other`            → 2 段（补集）
        parts = desc.split('|')
        if len(parts) >= 4:
            key = (parts[0], parts[1], parts[2])
        else:
            key = (parts[0], '', '')
        by_market.setdefault(key, []).append((mask, odds, desc))

    out = []
    for key, group in by_market.items():
        total = sum(1.0 / o for _, o, _ in group if o > 0)
        if total < 0.90:
            log.warning('盘口内部和异常，数据不可信，已剔除：%s  Σ=%.4f  (%d 个选项)',
                        key, total, len(group))
            continue
        out.extend(group)
    return out


# 各类盘口「完整」时应该有的标签。
#
# 用于剔除残缺盘口：平台经常锁掉一条腿（原始串里标记 `,0,`），
# 只留另一条。剩下的那条价格是冻结的 —— 因为平台锁腿通常意味着
# 这个盘口马上要结算。拿它去拼覆盖会得到下不了注的「套利」。
#
# 实测（2026-11-13，231 轮滚球采样）：127 个内部和异常的盘口
# **全部**是这种形态，没有一个是解析错误。
_EXPECTED_LABELS = {
    '1x2': {'1', 'X', '2'},
    'Double Chance': {'1X', '12', 'X2'},
    'Both Teams To Score': {'yes', 'no'},
    'Clean Sheet': {'yes', 'no'},
    'Win To Nil': {'yes', 'no'},
    'To Win': {'yes', 'no'},
    'Which Team To Score': {'none', 'only 1', 'only 2', 'both teams'},
    'Or Over 2.5': {'yes', 'no'},
    'Or Under 2.5': {'yes', 'no'},
    'Or Both Teams To Score': {'yes', 'no'},
    'Or Any Clean Sheet': {'yes', 'no'},
    'Draw Or Over 2.5': {'yes', 'no'},
    'Draw Or Under 2.5': {'yes', 'no'},
    'Draw Or Both Teams To Score': {'yes', 'no'},
    'Draw Or Any Clean Sheet': {'yes', 'no'},
    'Handicap': {'1', 'X', '2'},
    'Odd/Even': {'odd', 'even'},
    'Highest Scoring Half': {'1st half', '2nd half', 'equal'},
    '1st Half - 1x2': {'1', 'X', '2'},
    '1st Half - Double Chance': {'1X', '12', 'X2'},
    '1st Half - Both Teams To Score': {'yes', 'no'},
    '1st Half - Handicap': {'1', 'X', '2'},
    '2nd Half - 1x2': {'1', 'X', '2'},
    '2nd Half - Double Chance': {'1X', '12', 'X2'},
    '2nd Half - Both Teams To Score': {'yes', 'no'},
}


def expected_labels(market: str):
    """这条盘口完整时应该有哪些标签。无法判断返回 None（不做限制）。

    注意别把 `1x2 & Total` 这类组合盘算进来 —— 它的标签是
    `1 & over` 这种，没有固定的期望集合。
    """
    if market in _EXPECTED_LABELS:
        return _EXPECTED_LABELS[market]
    if ' & ' in market:
        return None
    if market.startswith('Total') or market.endswith(' Total'):
        return {'over', 'under'}
    return None


def _drop_incomplete_markets(cands):
    """剔除缺腿的盘口。

    cands: [(mask, odds, desc)]，desc 形如 `market|side|line|label`
    """
    by_market = defaultdict(list)
    for mask, odds, desc in cands:
        parts = desc.split('|')
        if len(parts) < 4:
            # `market|other` 这种补集形态，本身就代表完整覆盖
            by_market[None].append((mask, odds, desc))
            continue
        by_market[(parts[0], parts[1], parts[2])].append((mask, odds, desc))

    out = list(by_market.pop(None, []))
    for key, group in by_market.items():
        market = key[0]
        exp = expected_labels(market)
        if exp is None:
            out.extend(group)
            continue
        labels = {c[2].split('|')[3] for c in group}
        if exp.issubset(labels):
            out.extend(group)
        else:
            log.warning('盘口缺腿，整组剔除：%s %s %s  有 %s 缺 %s',
                        key[0], key[1] or '-', key[2] or '-',
                        ','.join(sorted(labels)),
                        ','.join(sorted(exp - labels)))
    return out


def _drop_decided(cands, rm: int):
    """剔除在当前比分下已经确定（必然成立或必然失败）的选项。"""
    n = bin(rm).count('1')
    if n == 0:
        return cands
    out = []
    for mask, odds, desc in cands:
        cov = bin(mask & rm).count('1')
        if cov == 0 or cov == n:
            continue                    # 必然失败 / 必然成立
        out.append((mask, odds, desc))
    return out


# ---------- 判定 ----------

def solve(cands, rm: int):
    """在可达子集上求最小覆盖成本。返回 SolveResult。

    没给候选或没有可达状态时返回 None。
    """
    if not cands or rm == 0:
        return None
    ghost = FULL & ~rm
    pool = list(cands)
    if ghost:
        pool.append((ghost, GHOST_ODDS, GHOST_DESC))
    return solver.solve(pool, NS)


def check_match(selections, home: int, away: int,
                minute: int | None = None,
                ht_home: int | None = None,
                ht_away: int | None = None,
                first_half_live: bool | None = None) -> dict | None:
    """判定一场滚球比赛的「比赛结果」套利。

    selections: [(market, side, line, label, odds), ...]
    返回 None 表示数据不足（没有可建模的结果盘/无法覆盖）。
    否则返回：
      {'cost', 'chosen': [(desc, odds)], 'reach', 'n_candidates',
       'is_arbitrage', 'profit_pct'}
    """
    # ★ 状态空间不完备时直接拒绝。
    #
    # 模型每队只到 9 球。9:0 时“再进一球”（10:0）不存在于状态空间里，
    # Total 9.5 under + 客队 Total 0.5 over 会被算成覆盖全部 10 个可达
    # 状态，但现实中 10:0 两条腿全输。
    #
    # 这是临时 guard；正式修复见 F01 的无界反例验证器。
    if score_space_incomplete(home, away):
        log.warning('比分接近模型上限，拒绝认证：%s:%s（每队上限 %d）',
                    home, away, soccer.G - 1)
        return None

    cands = build_candidates(selections, minute)
    # ★ 求解池只保留白名单市场：否则非白名单组合可能因为成本略低
    # 抢走真正可认证的白名单组合，最后验证器只能返回 unsupported_rules。
    # build_candidates 仍保留完整盘口（诊断/回放用），这里只过滤求解池。
    from analyze.live.verify import is_whitelisted
    certified = []
    for mask, odds, desc in cands:
        p = str(desc).split('|')
        if len(p) == 4 and is_whitelisted(p[0], p[1], p[2], p[3]):
            certified.append((mask, odds, desc))
    # 同一规范选项可能由多个平台市场 ID 返回。求解器只返回 desc，
    # 无法证明哪个 ID 的报价被选中。赔率冲突时整项隔离，避免把较高
    # 报价与另一个 ID 混用；相同赔率的重复项才合并。
    by_desc = {}
    conflicting = set()
    for mask, odds, desc in certified:
        previous = by_desc.get(desc)
        if previous is None:
            by_desc[desc] = (mask, odds, desc)
        elif previous[0] != mask or previous[1] != odds:
            conflicting.add(desc)
    if conflicting:
        log.warning('同名选项报价冲突，隔离 %d 项：%s',
                    len(conflicting), ','.join(sorted(conflicting)))
    cands = [candidate for desc, candidate in by_desc.items()
             if desc not in conflicting]
    if not cands:
        return None

    if first_half_live is None:
        first_half_live = (
            minute is not None and minute <= 45 and ht_home is None)
    rm = reach_mask(
        home, away, ht_home, ht_away,
        first_half_live=first_half_live,
    )
    # 关键：剔除「在当前比分下已经确定」的选项。
    # 不做这一步会算出 Σ=0.85 这种假套利（买一个必然成立的选项就够了）。
    cands = _drop_decided(cands, rm)
    if not cands:
        return None

    r = solve(cands, rm)
    if r is None or r.status != solver.OK or r.cost is None:
        return None

    # solver 返回的 chosen 是 desc 字符串；把幽灵候选排除掉
    chosen = []
    odds_by_desc = {d: o for _, o, d in cands}
    for d in r.chosen:
        if d == GHOST_DESC:
            continue
        chosen.append((d, odds_by_desc.get(d)))

    # ★ 对外成本只能由真实下注腿计算。
    #
    # solver 的 cost 含幽灵候选的 1e-9（它覆盖不可达状态）；那是求解技巧，
    # 不是可下注的价格，不能进入对外展示的成本/收益率。
    real_cost = sum(1.0 / o for _, o in chosen if o)
    if real_cost <= 0:
        return None

    # ★ D02：白名单 + 无界反例验证。
    #
    # 有限模型只负责**找候选**；只有独立验证器在整个无界比分域上找不到
    # 亏损终局，才允许 is_arbitrage=True（进而写正式命中库）。
    verification = None
    is_arb = real_cost < 1.0
    if is_arb:
        from analyze.live import verify
        verification = verify.verify_chosen(
            chosen, home, away, ht_home=ht_home, ht_away=ht_away,
            total_stake=getattr(config, 'LIVE_CERT_TOTAL_STAKE', 10000),
            stake_unit=getattr(config, 'LIVE_CERT_STAKE_UNIT', 1),
            min_profit=getattr(config, 'LIVE_CERT_MIN_PROFIT', 0.0),
        )
        if verification['status'] == 'counterexample_found':
            ce = verification.get('counterexample') or {}
            log.warning(
                '无界验证发现反例，拒绝认证 %s:%s 终局 %s:%s 返还 %.6f',
                home, away, ce.get('home_score'), ce.get('away_score'),
                ce.get('payout') or 0.0)
        is_arb = verification['status'] == 'verified'

    return {
        'cost': real_cost,
        'chosen': chosen,
        'reach': reach_count(rm),
        'reach_mask': rm,
        'n_candidates': len(cands),
        'is_arbitrage': is_arb,
        'is_candidate': real_cost < 1.0,
        'profit_pct': (1.0 / real_cost - 1.0) * 100,
        'verification': verification,
    }


def stakes(chosen, cost: float) -> list[tuple[str, float, float]]:
    """把命中的组合转成下注方案。

    返回 [(desc, odds, weight), ...]，weight 之和为 1。
    按 1/赔率 反比分配，保证任何结果的返还都相同：
        s_i = (1/o_i) / Σ(1/o_j)
    """
    total = sum(1.0 / o for _, o in chosen if o)
    if total <= 0:
        return []
    return [(d, o, (1.0 / o) / total) for d, o in chosen if o]


# ---------- 半场比分 ----------
# 定义移到 common/scores.py：采集侧要把它冻结进快照，分析侧也从快照读。
from common.scores import half_time_score  # noqa: E402,F401
