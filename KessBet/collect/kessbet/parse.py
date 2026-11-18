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

# 带盘线的选项段，逗号后面紧跟 `{id}/{flag}={id},`。
# 用来把「盘线前缀」和「普通选项段」区分开 —— 见 _line_prefix()。
_LINE_PREFIX_RE = re.compile(r'^\w+/[01]=\w+,')


def _line_prefix(seg: str) -> str:
    """取选项段里的盘线前缀。没有前缀返回 ''。

    段格式：`{盘线,}{selId}/{标志}={selId},{赔率},{标志},{标签}`

    盘线**只出现在每条盘线的第一个选项上**，其余选项形如
    `{selId},{赔率},{标志},{标签}`。

    区分依据：**盘线前缀的逗号在 `/` 之前**，其余选项的逗号都在 `/` 之后。

    ## 为什么必须单独取（2026-11-13 踩的坑）

    原来是在 `_split_selection` 成功之后才 `if line: current_line = line`。
    如果某条盘线的**第一个选项被锁定**（`,0,`）或赔率为 0，
    `_split_selection` 返回 None → `continue` → 盘线名丢了 →
    后面所有选项继承上一条盘线，串到别人家里去。

    实测后果（matchId ...123237，第 45 分钟 1:2）：

        line 2:0   2 @12.50   ← 真属于 2:0（客队赢 4 球）
        line 2:0   X @3.55    ← 真属于 2:0
        line 2:0   2 @4.85    ← 其实属于 1:0（客队赢 2 球以上）

    两个 `2` 挤在同一条线下，内部和只有 0.55，`1` 完全消失。
    拿去和别的盘口组合，就得到 Σ=0.13 的假套利。
    """
    # 每组的第一段前面带 `{matchId},{tabId}|`，先剥掉
    if '|' in seg:
        seg = seg.rsplit('|', 1)[-1]
    seg = seg.strip()

    head, _, rest = seg.partition(',')
    if not rest:
        return ''
    # 带盘线的段，逗号后面紧跟 `{id}/{flag}={id},`。
    # 用这个判据而不是「逗号在斜杠前」：标签本身可能含斜杠
    # （Halftime/Fulltime 的 `1/1`、`X/2`），那种会被误判。
    if _LINE_PREFIX_RE.match(rest):
        return head
    return ''


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

    # `,0,` = 该选项被平台锁定（网页上显示锁图标），不能投注。
    #
    # 实测（2026-11-12，APIA 0:2 第 78 分钟）原始串：
    #   3.5 线： `245171615317397527,1.75,1,over`   ← ,1, 可投注
    #   4.5 线： `245171701216743441,2.55,0,over`   ← ,0, 已锁定
    # 锁定的选项平台**照样返回赔率**（2.55 / 1.37 都是正常数值），
    # 只是网页上打锁。不按标记过滤的话，我们会拿一个根本下不了注的
    # 盘口去凑覆盖，算出偏低的成本。
    if marker == m0:
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
            # 先更新盘线 —— 必须在解析选项之前。
            # 这条线的名字写在这一段里，如果这个选项本身无效
            # （锁定 / 零赔率）就跳过它，但**盘线名必须留下**，
            # 否则同一盘线的后续选项会被算到上一条线头上。
            prefix = _line_prefix(seg)
            if prefix:
                current_line = prefix

            parsed = _split_selection(seg)
            if parsed is None:
                continue
            _, label, odds = parsed
            sels.append((current_line, label, odds))

    return name, sels


def split_team(name: str, home: str, away: str) -> tuple[str, str]:
    """把带队名前缀的盘口名拆成 (归属, 基础盘口名)。

    >>> split_team('Arsenal Total', 'Arsenal', 'Everton')
    ('H', 'Total')
    >>> split_team('1st Half - Arsenal Total', 'Arsenal', 'Everton')
    ('H', '1st Half - Total')

    半场前缀必须保留在基础名里，否则下游既认不出队名，也认不出是半场盘。
    仅允许大小写差异；若两队前缀无法唯一确定，保留原名并跳过建模。
    """
    prefix = ''
    rest = name
    for p in HALF_PREFIXES:
        if rest.startswith(p):
            prefix, rest = p, rest[len(p):]
            break

    matches = []
    for side, team in (('H', home), ('A', away)):
        if (team and rest[:len(team)].casefold() == team.casefold()
                and rest[len(team):].startswith(' ')
                and rest[len(team) + 1:]):
            matches.append((len(team), side, rest[len(team) + 1:]))
    if matches:
        matches.sort(reverse=True)
        if len(matches) == 1 or matches[0][0] > matches[1][0]:
            _length, side, base = matches[0]
            return side, prefix + base
    return '', prefix + rest


def parse_market_id(mari: str) -> str:
    """取 `mari` 第一段的 marketId。

    仅作元数据用于排查，**不要当作主键**——它是平台私有编号且无法跨平台。
    """
    if not mari or '|' not in mari:
        return ''
    return mari.split('|', 1)[0].split(',')[0]
