"""collect/bet365/parse.py — bet365 内容接口（pod 格式）解析。

bet365 的赛程/赔率是竖线分隔的扁平记录流，每行以类型码开头：

    F|CL;IT=#HO#P31524#;PV=...;|EV;NA=欧洲冠军联赛;|MG;PT=62;SY=bbr;TL=#PB#AS#B1#P31524#;|

主要类型：

    CL  容器（IT 是页面描述符）
    EV  事件/分组
    MG  盘口组，带 NA 时就是**一场比赛**
    MA  盘口选项行（有 N2 + MN + OD 时才是可用的赔率）
    PA  其它选项（球员、大小球等）

本模块只关心 MA 里同时带 N2（比赛名）、MN（盘口名）、OD（赔率）的行——
这三者齐全的才是可入库的赔率，其余一律跳过。

赔率是英式分数（'6/5'、'1/1'），转成欧赔小数要 +1。

数据来源是 `homepagecontentapi/pod?pd=#HO#P31524#`，该接口**不需要 token**，
只要带上网关下发的 cookie 即可。注意它只返回「首页精选」的少量比赛和盘口。
"""
import re
from fractions import Fraction

# MA 行里这三个字段齐全才算一条赔率
_ODD_RE = re.compile(r'^\d+(?:\.\d+)?(?:/\d+(?:\.\d+)?)?$')


def parse_records(text: str) -> list[tuple[str, dict]]:
    """把 pod 记录流拆成 [(类型码, 字段字典), ...]。

    记录之间用 '|' 分隔，字段之间用 ';' 分隔，每行带一个 \\x08 前缀控制符。
    字段值里可能含 '='（少见），所以只按第一个 '=' 切。
    """
    out = []
    for raw in text.split('|'):
        rec = raw.strip('\x08 \r\n')
        if not rec:
            continue
        parts = rec.split(';')
        typ = parts[0].strip()
        fields = {}
        for kv in parts[1:]:
            if '=' in kv:
                k, v = kv.split('=', 1)
                fields[k] = v
        out.append((typ, fields))
    return out


def fractional_to_decimal(odds: str) -> float | None:
    """英式分数赔率 → 欧赔小数（含本金）。'6/5' → 2.2，'1/1' → 2.0。

    已经是小数（如 '2.20'）的直接返回。无法解析返回 None。
    """
    s = (odds or '').strip()
    if not s or not _ODD_RE.match(s):
        return None
    try:
        if '/' in s:
            num, den = s.split('/', 1)
            return round(float(Fraction(num) / Fraction(den)) + 1.0, 4)
        val = float(s)
        # 小数赔率本身就是含本金的欧赔；分数才需要 +1
        return round(val, 4)
    except (ValueError, ZeroDivisionError):
        return None


def parse_odds(text: str) -> dict[str, list[dict]]:
    """从 pod 文本里抽出 {比赛名: [选项, ...]}。

    每个选项是 dict：market / selection / opponent / line / odds_raw / odds。
    只收 (N2, MN, OD) 三者齐全的行。
    """
    events: dict[str, list[dict]] = {}
    for typ, f in parse_records(text):
        if typ != 'MA':
            continue
        event = f.get('N2')
        market = f.get('MN')
        raw = f.get('OD')
        if not event or not market or not raw:
            continue
        dec = fractional_to_decimal(raw)
        if dec is None:
            continue
        events.setdefault(event, []).append({
            'market': market,
            'selection': (f.get('NA') or '').strip(),
            'opponent': (f.get('EX') or '').strip(),
            'line': (f.get('HO') or '').strip(),
            'odds_raw': raw,
            'odds': dec,
        })
    return events


def parse_events(text: str) -> list[dict]:
    """抽出带比赛名的 MG 记录，即比赛本身，按名字去重。

    首页接口会把同一场比赛在多处重复（推荐位 + 联赛列表），
    去重时保留第一次出现的那条。
    """
    out: dict[str, dict] = {}
    for typ, f in parse_records(text):
        if typ != 'MG':
            continue
        name = f.get('NA') or ''
        if ' v ' not in name:
            continue
        if name in out:
            continue
        out[name] = {
            'name': name,
            'home': name.split(' v ', 1)[0].strip(),
            'away': name.split(' v ', 1)[1].strip(),
            'competition': f.get('CC') or f.get('L3') or '',
            # BT 在首页记录里常为空；有值时为 1（正常比赛）
            'starts_at': f.get('BT') or '',
        }
    return list(out.values())


def summarize(text: str) -> dict:
    """给调试用的一行摘要。"""
    ev = parse_odds(text)
    return {
        'bytes': len(text),
        'events': len(ev),
        'selections': sum(len(v) for v in ev.values()),
        'markets': len({s['market'] for v in ev.values() for s in v}),
    }
