"""collect/bet365/match.py — 单场比赛的全盘口解析。

联赛页（`matchmarketscontentapi/markets`）只有横向对比的少数盘口，
真正的全盘口在**单场比赛页**的 `matchbettingcontentapi/coupon`：

    /contentdata/matchbettingcontentapi/coupon
        ?lid=10&zid=0&cid=197&cgid=2&ctid=197
        &pd=%23AC%23B1%23C1%23D8%23E{matchId}%23F3%23I{tab}%23

实测一场英超可得 15~22 个盘口 / 592 个赔率，包含联赛页拿不到的
Half Time/Full Time、Handicap 2-Way、Correct Score 等。

## tab 编号

    I1   Popular       主列表，盘口最全（默认）
    I3   Asian Lines   只有 4 个
    I6   Goals         516 个
    I7   Half          164 个
    其余 I2/I4/I5/I8   空

## 数据布局（纵向 + 二维）

盘口组由 MG 开头，NA 就是盘口名；组内 MA 是**维度标签**，PA 是选项：

    MG;ID=981;NA=Goals Over/Under;
    MA;ID=RB4_0_1981;NA= ;              ← 盘线分隔（下面 PA 的 NA 是盘线）
    PA;ID=PC257789320;NA=2.5;
    MA;ID=M981;NA=Over;                 ← 维度：Over
    PA;ID=257789320;OD=1/2;             ← 按 ID 回填：2.5 线上的 Over
    MA;ID=M981;NA=Under;                ← 维度：Under
    PA;ID=257789322;OD=13/8;

多维盘口（赛果/两队均得分）是两个维度交叉：

    MA;ID=M50404;NA= ;                  ← 第一个维度：队伍（下面 PA 的 NA）
    PA;ID=PC258555187;NA=Man City;
    PA;ID=PC258555196;NA=Fulham;
    PA;ID=PC258555192;NA=Draw;
    MA;ID=M50404;NA=Yes;                ← 第二个维度：是
    PA;ID=258555187;OD=7/4;             ← 与上面的 ID 配对 → Man City & Yes

所以规则是：**MA 记住当前维度标签，PA 的 ID 回查同组内「带 NA 的同 ID 行」
拿到另一半标签。** 两半拼起来就是完整的选项名。
"""
from collect.bet365.parse import fractional_to_decimal, parse_records

# MA 里这些 NA 只是版面占位，不是真实维度
_BLANK_LABELS = {'', ' '}


def parse_match_markets(text: str) -> dict[str, list[dict]]:
    """解析单场全盘口。

    返回 {盘口名: [{selection, odds_raw, odds, line}, ...]}。

    ## 维度的组织方式

    MA 行是**维度切换点**，两类：

    * 空标签（`NA=' '`）→ 开始「第一维」段。段内 PA 带名字的行（`PC` 开头）
      是这一维的取值：盘线 2.5 / 队名 Man City / 球员名。
    * 非空标签（Over / Under / Yes / Man City）→ 这一维取该值，其后的
      PA 只有 OD，靠 ID 回查第一维的名字。

    所以 PA 的完整选项名 = 第一维名字 + 当前维度标签：

        第一维=2.5, 维度=Over   → "2.5 & Over"
        第一维=Man City, 维度=Yes → "Man City & Yes"
        第一维=Man City, 维度=(空) → "Man City"（1X2 只有一个维度）

    Correct Score 稍特殊：第一维是队名，维度里没有第二维的取值，
    PA 自己的 NA 就是比分（1-0），所以 `选项 = 队名 + PA.NA`。
    """
    out: dict[str, list[dict]] = {}
    market: str | None = None
    dim: str | None = None           # 当前维度标签（第二维）
    dims_seen = 0                    # 本盘口出现过几个非空 MA 标签
    names: dict[str, str] = {}       # 第一维：ID → 名字

    for typ, f in parse_records(text):
        if typ == 'MG':
            name = (f.get('NA') or '').strip()
            market = name or None
            dim = None
            dims_seen = 0
            names = {}
            if market:
                out.setdefault(market, [])
            continue

        if typ == 'MA':
            label = (f.get('NA') or '').strip()
            sy = (f.get('SY') or '').strip()
            if sy == 'dct':
                # 正确比分的队名分组（Man City），后面 PA 的 NA 是比分
                dim = label
                names.clear()
            elif label in _BLANK_LABELS:
                dim = None
                names.clear()
            else:
                # Over / Under / Yes / No —— 名字表保留，用来回查盘线
                dim = label
                dims_seen += 1
            continue

        if typ != 'PA' or market is None:
            continue

        pid = (f.get('ID') or '').strip()
        label = (f.get('NA') or '').strip()
        key = pid[2:] if pid.startswith('PC') else pid

        if not f.get('OD'):
            if label:
                names[key] = label
            continue

        od = fractional_to_decimal(f.get('OD') or '')
        if od is None:
            continue

        first = names.get(key, '')
        if dim and first:
            # 有第一维名字（盘线 / 队名 / 球员），和第二维拼起来
            selection = f'{first} & {dim}'
        elif dim and label:
            # 正确比分：dim 是队名，PA 的 NA 是比分
            selection = f'{dim} {label}'
        else:
            selection = dim or first or label

        out[market].append({
            'selection': selection.strip(),
            'odds_raw': f.get('OD') or '',
            'odds': od,
            'line': first if _looks_like_line(first) else '',
            'group': first if first and not _looks_like_line(first) else '',
        })

    return {k: v for k, v in out.items() if v}


def _join(a: str, b: str) -> str:
    if a and b:
        return f'{a} & {b}'
    return a or b


def _looks_like_line(s: str) -> bool:
    try:
        float(s)
        return True
    except (TypeError, ValueError):
        return False


def summarize(text: str) -> dict:
    m = parse_match_markets(text)
    return {
        'bytes': len(text),
        'markets': len(m),
        'selections': sum(len(v) for v in m.values()),
    }


def overround(selections: list[dict]) -> float | None:
    """一组选项的覆盖成本（Σ 1/赔率）。

    只对「互斥且穷尽」的盘口有意义：1X2、双胜彩、两队均得分。
    大小球/让球要先按盘线分组。
    """
    if len(selections) < 2:
        return None
    try:
        return sum(1 / s['odds'] for s in selections)
    except ZeroDivisionError:
        return None


def group_by_line(selections: list[dict]) -> dict[str, list[dict]]:
    """按盘线分组，用于大小球 / 让球这类同一盘口多条盘线的情况。"""
    out: dict[str, list[dict]] = {}
    for s in selections:
        out.setdefault(s.get('line') or '', []).append(s)
    return out
