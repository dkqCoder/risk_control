"""common/markets.py — 盘口分组与命名的共享逻辑。

为什么需要单独一个模块：
  - `web/api.py` 要把数据库行按 (market, side) 分组展示
  - `analyze/run.py` 要在命中套利时把同样的分组写进快照表

两处如果各写一份，早晚会不一致 —— 用户看到的页面和留存的事后证据对不上，
那比没有快照更糟。
"""
from __future__ import annotations


def group_markets(rows, home: str = '', away: str = '') -> list[dict]:
    """把 (market, side, line, selection, odds) 行分组成盘口列表。

    返回 [{'name', 'base', 'side', 'selections': [{'line','label','odds'}]}, ...]

    必须按 (market, side) 分组，不能只按 market：
    队名盘口入库时被拆成 market='Total' + side='H'/'A'，
    只按 market 分组会把「全场 Total」「主队 Total」「客队 Total」
    混成一块 —— 接口 66 个盘口会显示成 49 个。
    """
    groups: dict[tuple[str, str], list] = {}
    for market, side, line, label, odds in rows:
        groups.setdefault((market, side), []).append({
            'line': line or '', 'label': label, 'odds': float(odds),
        })
    return [{
        'name': market_name(base, side, home, away),
        'base': base,
        'side': side,
        'selections': sels,
    } for (base, side), sels in sorted(groups.items())]


def market_name(base: str, side: str, home: str = '', away: str = '') -> str:
    """把 (基础盘口名, 归属) 还原成用户看到的写法。

        ('Total', '')                -> 'Total'
        ('Total', 'H') with Arsenal  -> 'Arsenal Total'
        ('1st Half - Total', 'A')    -> '1st Half - Everton Total'
    """
    if not side:
        return base
    team = home if side == 'H' else away
    if not team:
        return f'{base} ({side})'
    for p in ('1st Half - ', '2nd Half - '):
        if base.startswith(p):
            return f'{p}{team} {base[len(p):]}'
    return f'{team} {base}'


def describe_pick(desc: str, home: str = '', away: str = '') -> str:
    """把候选的 desc 内部键转成人能读的选项名。

    desc 是求解器里用来去重的字符串，格式见 analyze/covers/soccer.py：
        f'{market}|{side}|{line}|{label}'     普通盘口
        f'{market}|other'                     比分盘的 other 补集
        f'{nm}|{line}|{lab}'                  文件来源（与上面同构）
    直接显示到页面就成了 `Total|H|2.5|under` —— 用户看不出这是什么。

    解析失败时原样返回：宁可显示内部键，也不能因为一个字段丢了整个选项。
    """
    parts = desc.split('|')
    if len(parts) < 2:
        return desc
    if parts[-1] == 'other' and len(parts) == 2:
        return f'{market_name(parts[0], "", home, away)} · 其他'

    market = parts[0]
    if len(parts) == 4:
        side, line, label = parts[1], parts[2], parts[3]
    elif len(parts) == 3:
        side, line, label = '', parts[1], parts[2]
    else:
        return desc

    name = market_name(market, side, home, away)
    tail = ' '.join(x for x in (line, label) if x)
    return f'{name} · {tail}' if tail else name
