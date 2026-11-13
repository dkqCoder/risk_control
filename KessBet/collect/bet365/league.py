"""collect/bet365/league.py — 联赛盘口目录与赔率解析。

bet365 的联赛接口分两步：

  ① 盘口目录 `/contentdata/matchmarketscontentapi/markets?pd=#AC#B1#C1#D1002#E{联赛}#G40#H^1#`

     记录顺序是「先比赛表，后各盘口的列」：

         PA;ID=PC257789315;NA=曼城;N2=富勒姆;FD=曼城 v 富勒姆;
            FI=203186235;BC=20261121123000;EX=puw~.../match/72221406~...;PZ=0;
         PA;ID=PC257790163;NA=伯恩茅斯;N2=诺丁汉森林;FD=伯恩茅斯 v 诺丁汉森林;...;PZ=1;
         ...                                    ← 20 场比赛，PZ 0..19

         MG;ID=40;SY=cmx;                         ← 盘口组（全场赛果）
         MA;MA=40;ID=M40;NA=1;FI=203186235;...     ← 列头：1
         PA;ID=257789315;FI=203186235;OD=12/25;PZ=0;
         PA;ID=257790163;FI=203186239;OD=1/1;PZ=1;
         ...                                       ← 每场比赛一个赔率，按 PZ 对齐
         MA;MA=40;ID=M40;NA=X;FI=203186235;...     ← 列头：X
         PA;...                                    ← 同样一串
         MA;MA=40;ID=M40;NA=2;FI=203186235;...     ← 列头：2

     所以：**MA 行是列头，其后紧邻的 PA 行是按 PZ 排列的各场赔率**。

  ② 单盘口 `/contentdata/matchmarketscontentapi/markets?pd=<盘口 PD>`
     结构相同，只是只含一个盘口。

字段说明：

    FI    比赛编号，贯穿比赛表与赔率行
    FD    比赛全名 `主队 v 客队`
    BC    开赛时间 `yyyyMMddHHmmss`（英国时间，非 UTC）
    EX    含 Sportradar 比赛 ID：.../match/72221406~
    OD    赔率，**英式分数**（12/25 → 1.48）
    PZ    排序，比赛在列中的位置
    MA    盘口组编号（列头行）
"""
import re
from datetime import datetime

from collect.bet365.parse import fractional_to_decimal, parse_records

# EX 里的 Sportradar 链接：.../match/72221372~...
_SR_RE = re.compile(r'/match/(\d+)')

# BC：yyyyMMddHHmmss
_BC_RE = re.compile(r'^\d{14}$')

# MA 列头里这些 NA 是页面分区标签，不是赔率选项
_SKIP_LABELS = {
    '比赛', '盘口编辑器+', '球队', '早期投注', '积分榜', '全部', '相关盘口',
    'Matches', 'Props', 'Bet Builder +', 'Teams', 'Outrights', 'Table', 'All',
}


def parse_fixtures(text: str) -> list[dict]:
    """抽出比赛表。比赛行是 ID 以 'PC' 开头的 PA。按 FI 去重。"""
    out: dict[str, dict] = {}
    for typ, f in parse_records(text):
        if typ != 'PA':
            continue
        if not (f.get('ID') or '').startswith('PC'):
            continue
        fi = (f.get('FI') or '').strip()
        fd = (f.get('FD') or '').strip()
        if not fi or fi in out or ' v ' not in fd:
            continue
        home, away = (s.strip() for s in fd.split(' v ', 1))
        sr = ''
        m = _SR_RE.search(f.get('EX') or '')
        if m:
            sr = f'sr:match:{m.group(1)}'
        kick = None
        bc = (f.get('BC') or '').strip()
        if _BC_RE.match(bc):
            try:
                kick = datetime.strptime(bc, '%Y%m%d%H%M%S')
            except ValueError:
                kick = None
        out[fi] = {
            'fixture_id': fi,
            'home': home,
            'away': away,
            'name': fd,
            'sportradar_id': sr,
            'kickoff_uk': kick,
            'pz': int(f['PZ']) if (f.get('PZ') or '').isdigit() else None,
        }
    return list(out.values())


def parse_market_catalog(text: str) -> list[dict]:
    """抽出盘口目录：[{market, pd}]。

    目录里 MA 行的 NA 是**页面标签**，和真正的盘口名不完全一样：
    1X2 那一条叫「比赛」，PD 才指向「全场赛果」。所以这里同时保留
    NA 原文和从 PD 里能认出来的信息。
    """
    out, seen = [], set()
    for typ, f in parse_records(text):
        if typ != 'MA':
            continue
        name = (f.get('NA') or '').strip()
        pd = (f.get('PD') or '').strip()
        if not name or not pd.startswith('#AC#'):
            continue
        if not _is_market_path(pd):
            continue
        if (name, pd) in seen:
            continue
        seen.add((name, pd))
        out.append({'label': name, 'market': CANONICAL.get(name, name), 'pd': pd})
    return out


def _is_market_path(pd: str) -> bool:
    """排除导航项（球队/积分榜/早期投注）的 PD，只留真盘口。

    真盘口的 PD 含 `#N0#` 或 `#G<数字>#` 段；导航项指向列表页。
    """
    if 'K^' in pd:  # 子页签（球队 / 积分榜 / 早期投注）
        return False
    return True


# bet365 目录里用的页面对 1X2 的叫法是「比赛」，语义上等价「全场赛果」
CANONICAL = {
    '比赛': '全场赛果',
    'Matches': 'Full Time Result',
}



def parse_market_odds(text: str) -> list[dict]:
    """解析盘口赔率，兼容两种布局。

    ## 布局 A：横向表（1X2 / 大小球 / 双重机会）

        MG;ID=40;
        MA;...;NA=1;FI=203186235;     ← 列头
        PA;FI=203186235;OD=12/25;PZ=0;  ← 各场，按 PZ 对齐
        PA;FI=203186239;OD=1/1;PZ=1;
        MA;...;NA=X;...                ← 下一列
        PA;FI=...;OD=...;

    ## 布局 B：纵向块（正确比分 / 球员盘口）

        MG;ID=M43;FI=203186235;NA=Man City v Fulham;   ← 一场一个块
        MA;ID=C43;NA=Man City;FI=203186235;             ← 分组标题（不带 OD）
        PA;ID=258547913;NA=1-0;OD=10/1;                 ← 选项，无 FI，靠块归属
        PA;ID=258547919;NA=2-0;OD=17/2;

    区分方式：PA 带 FI 且能对上比赛表的走 A；不带 FI 的按所在 MG 块的 FI 归属。

    返回 [{fixture_id, selection, odds_raw, odds, line, order}]。
    """
    out = []
    current_label = None      # 布局 A 的列头
    block_fi = None           # 布局 B 的当前块所属比赛
    block_sel = None          # 布局 B 的分组标题（如队名）
    order = 0

    for typ, f in parse_records(text):
        if typ == 'MG':
            # 布局 B 的块头：带 FI 和比赛名
            fi = (f.get('FI') or '').strip()
            block_fi = fi or None
            block_sel = None
            current_label = None
            continue

        if typ == 'MA':
            name = (f.get('NA') or '').strip().replace('¬', ' v ')
            fi = (f.get('FI') or '').strip()
            mid = (f.get('ID') or '').strip()
            if name in _SKIP_LABELS or not name:
                current_label = None
                block_sel = None
            elif mid.startswith('C'):
                # 布局 B 的分组标题：正确比分里的队名 / Draw，ID=C43
                block_sel = name
                current_label = None
            else:
                # 布局 A 的列头：1 / X / 2，ID=M40
                current_label = name
                block_sel = None
            continue

        if typ != 'PA':
            continue

        od = fractional_to_decimal(f.get('OD') or '')
        if od is None:
            continue
        if (f.get('ID') or '').startswith('PC'):
            continue

        fi = (f.get('FI') or '').strip()
        if fi:
            # 布局 A：PA 自带 FI
            sel = current_label or ''
            fid = fi
            order += 1
        elif block_fi:
            # 布局 B：PA 无 FI，归属当前块
            sel = (f.get('NA') or '').strip()
            fid = block_fi
            order += 1
        else:
            continue

        out.append({
            'fixture_id': fid,
            'selection': sel,
            'group': block_sel or '',
            'odds_raw': f.get('OD') or '',
            'odds': od,
            'line': (f.get('HO') or '').strip(),
            'order': order,
        })
    return out


def parse_block_fixtures(text: str) -> dict[str, str]:
    """从布局 B 的 MG 块头里抽 {FI: 比赛名}。

    `MG;ID=M43;FI=203186235;NA=Man City v Fulham;` —— 只含正确比分、
    球员盘口这类纵向接口没有比赛表，靠这个补上比赛名。
    """
    out = {}
    for typ, f in parse_records(text):
        if typ != 'MG':
            continue
        fi = (f.get('FI') or '').strip()
        name = (f.get('NA') or '').strip()
        if fi and ' v ' in name:
            out[fi] = name
    return out


def build_market_table(text: str) -> dict[str, dict[str, float]]:
    """把单个盘口整理成 {比赛名: {选项: 赔率}}。

    选项名对布局 B 来说可能重复（正确比分里两队的 '1-0' 是两个不同选项），
    所以有分组标题时用 `分组|选项` 做键，避免覆盖。
    """
    fixtures = {f['fixture_id']: f['name'] for f in parse_fixtures(text)}
    fixtures.update(parse_block_fixtures(text))
    table: dict[str, dict[str, float]] = {}
    for row in parse_market_odds(text):
        name = fixtures.get(row['fixture_id'], row['fixture_id'])
        sel = row['selection']
        if not sel:
            continue
        if row.get('group'):
            sel = f"{row['group']}|{sel}"
        table.setdefault(name, {})[sel] = row['odds']
    return table


def summarize(text: str) -> dict:
    return {
        'bytes': len(text),
        'fixtures': len(parse_fixtures(text)),
        'markets': len(parse_market_catalog(text)),
        'odds_rows': len(parse_market_odds(text)),
    }
