"""KessBet 盘口原始串解析。

`mari` 字段的格式：

    {marketId}|{matchId},{tabId}|{盘线},{selId}/1={selId},{赔率},1,{标签}@...|{盘口名}

- `^` 分隔不同盘线
- `@` 分隔同一盘线的多个选项
- **盘线只在每条盘线的第一个选项上出现**，后续选项需要向下填充

本模块只做字符串到结构化数据的转换，不涉及任何业务语义。
"""
import re

# 半场前缀。必须保留在基础盘口名里——
# '1st Half - Arsenal Total' 要拆成 side='H', market='1st Half - Total'
HALF_PREFIXES = ('1st Half - ', '2nd Half - ')


def _split_selection(seg: str) -> tuple[str, str, float] | None:
    """把一个选项段拆成 (盘线, 标签, 赔率)。

    段的格式是 `[盘线,]{selId}[,赔率,{0|1},标签]`，其中：
    - 盘线只在每条盘线的第一个选项上出现
    - `,1,` 表示赔率有效；`,0,` 表示该选项停售（赔率通常为 1.00）
    - **标签本身可能含逗号**（如 Multiscores 的 `1:0, 2:03:0`）

    所以不能简单用 `split(',')` 按位置取值，必须从后往前找 `,X,` 标记，
    它前面那一段是赔率，它后面全部是标签。
    """
    # 从后往前找 ",0," 或 ",1,"，取更靠后的那个
    m1 = seg.rfind(',1,')
    m0 = seg.rfind(',0,')
    marker = max(m1, m0)
    if marker < 0:
        return None

    before = seg[:marker]       # `[盘线,]{selId}[,赔率]`
    label = seg[marker + 3:]    # 标签，可能含逗号

    head = before.rsplit(',', 1)
    if len(head) < 2:
        return None
    try:
        odds = float(head[1])
    except ValueError:
        return None

    # 赔率 <= 0 的选项是平台标记的停售/无报价（原始串里是 `0.00,0` 或 `1.00,0`）。
    # 这类数据没有意义，直接丢弃——保留会污染存储层，也会让 1/odds 除零。
    if odds <= 0:
        return None

    # head[0] 是 `[盘线,]selId`；如果含逗号，逗号前是盘线
    prefix = head[0]
    line = ''
    if ',' in prefix:
        line = prefix.split(',', 1)[0].strip()

    return line, label.strip(), odds


def parse_mari(mari: str) -> tuple[str | None, list[tuple[str, str, float]]]:
    """解析 `mari` 串。

    返回 (盘口名, [(盘线, 选项, 赔率), ...])。
    盘口名取不到时返回 (None, [])。

    盘线已按格式要求向下填充：同一条盘线的后续选项继承第一个选项的盘线。
    """
    if not mari or '|' not in mari:
        return None, []

    name = mari.rsplit('|', 1)[-1]
    body = mari.rsplit('|', 1)[0]

    # 找到含 ^ 或 @ 的那一段；没有的话取最后一段（单选项盘口）
    section = None
    for part in body.split('|'):
        if '^' in part or '@' in part:
            section = part
            break
    if section is None:
        section = body.split('|')[-1]

    sels: list[tuple[str, str, float]] = []
    current_line = ''
    for chunk in section.split('^'):
        for seg in chunk.split('@'):
            parsed = _split_selection(seg)
            if parsed is None:
                continue
            line, label, odds = parsed
            if line:
                current_line = line
            sels.append((current_line, label, odds))

    return name, sels


def split_team(name: str, home: str, away: str) -> tuple[str, str]:
    """把带队名前缀的盘口名拆成 (归属, 基础盘口名)。

    >>> split_team('Arsenal Total', 'Arsenal', 'Everton')
    ('H', 'Total')
    >>> split_team('1st Half - Arsenal Total', 'Arsenal', 'Everton')
    ('H', '1st Half - Total')

    半场前缀必须保留在基础名里，否则下游既认不出队名，也认不出是半场盘。
    队名匹配失败时返回 ('', 原名)——宁可当作未建模盘口跳过，也不要错拆成全场盘。
    """
    prefix = ''
    rest = name
    for p in HALF_PREFIXES:
        if rest.startswith(p):
            prefix, rest = p, rest[len(p):]
            break

    if home and rest.startswith(home + ' '):
        return 'H', prefix + rest[len(home) + 1:]
    if away and rest.startswith(away + ' '):
        return 'A', prefix + rest[len(away) + 1:]
    return '', prefix + rest


def parse_market_id(mari: str) -> str:
    """取 `mari` 第一段的 marketId。

    仅作元数据用于排查，**不要当作主键**——它是平台私有编号且无法跨平台。
    """
    if not mari or '|' not in mari:
        return ''
    return mari.split('|', 1)[0].split(',')[0]
