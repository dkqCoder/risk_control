"""web/api.py — 只读 HTTP API，供前端页面调用。

设计约束：
  1. **只读**。不写任何表，不碰采集链路。
  2. **不重复实现分析逻辑**。单场深挖直接 import analyze/ 下的
     covers 和 solver / payout，保证页面看到的数字和后台判定完全一致。
  3. **所有联表查询必须带 platform**。`kessbet_odds_prematch` 的主键是
     (platform, match_id, market, side, line, selection)，只按 match_id
     关联用不上最左前缀，会退化成几十万行的笛卡尔积（实测卡死 2 分钟）。

启动：
    uvicorn web.api:app --host 0.0.0.0 --port 8080

多平台：
    返回结构里所有跟平台相关的地方都是 **列表/映射**，不是单值。
    现在只有一个平台，接入第二家时不用改前端结构。
"""
import json
import logging
import os
import sys
from datetime import datetime
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

from analyze import payout, solver            # noqa: E402
from analyze.covers import soccer             # noqa: E402
from collect import config                    # noqa: E402
from common.markets import describe_pick, group_markets  # noqa: E402
from db.dao import Dao                        # noqa: E402

log = logging.getLogger(__name__)

app = FastAPI(title='Odds Monitor API', version='1.0')


# ---------------------------------------------------------------- 工具

def _dao() -> Dao:
    """每个请求一个 Dao（pymysql 连接不是线程安全的）。"""
    return Dao()


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat(sep=' ', timespec='minutes') if dt else None


# ---------------------------------------------------------------- 列表

@app.get('/api/platforms')
def list_platforms() -> dict:
    """当前有数据的平台。前端用它渲染筛选器。"""
    dao = _dao()
    try:
        with dao.conn.cursor() as c:
            c.execute("SELECT platform, COUNT(*) FROM kessbet_match "
                      "GROUP BY platform ORDER BY platform")
            return {'platforms': [{'id': p, 'matches': n} for p, n in c.fetchall()]}
    finally:
        dao.close()


@app.get('/api/matches')
def list_matches(
    hours: int = Query(config.HORIZON_HOURS, ge=1, le=168,
                     description='未来 N 小时内开赛'),
    sport: str = Query('sr:sport:1'),
    platform: str | None = Query(None, description='限定平台；不传则聚合全部'),
    q: str | None = Query(None, description='搜索球队/联赛'),
    max_cost: float | None = Query(None, description='只返回成本 <= 该值的比赛'),
    only_abnormal: bool = Query(False, description='只看异常：数据陈旧/无赔率/无法覆盖'),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict:
    """比赛列表。

    成本来自 kessbet_match_cost（analyze 预计算写入），**不在请求里跑 ILP**。
    没算过的场次 cost=None、status='pending'，前端显示「计算中」。
    """
    dao = _dao()
    try:
        where = [
            "m.platform = 'kessbet'",       # 主键前缀，缺了索引失效
            'm.sport = %s',
            'm.kickoff_time > NOW()',
            'm.kickoff_time <= NOW() + INTERVAL %s HOUR',
        ]
        args: list[Any] = [sport, hours]
        if q:
            where.append('(m.home_team LIKE %s OR m.away_team LIKE %s OR m.league LIKE %s)')
            like = f'%{q}%'
            args += [like, like, like]
        if max_cost is not None:
            # 没有快照的比赛也算不匹配（成本未知，不能当作 <= 阈值）
            where.append(
                'EXISTS (SELECT 1 FROM kessbet_match_cost mc '
                'WHERE mc.platform=m.platform AND mc.match_id=m.match_id '
                'AND mc.cost IS NOT NULL AND mc.cost <= %s)')
            args.append(max_cost)
        if only_abnormal:
            # 只认真正的异常。**不要**用「赔率多久没更新」做判据 ——
            # 采集是推送触发的，冷门比赛可能几小时没有赔率变动，
            # 数据是新鲜的但 updated_time 很旧。实测按 30 分钟筛会命中 96/129 场。
            where.append(
                '(NOT EXISTS (SELECT 1 FROM kessbet_odds_prematch o '
                '  WHERE o.platform=m.platform AND o.match_id=m.match_id) '
                ' OR NOT EXISTS (SELECT 1 FROM kessbet_match_cost mc '
                '  WHERE mc.platform=m.platform AND mc.match_id=m.match_id) '
                ' OR EXISTS (SELECT 1 FROM kessbet_match_cost mc '
                '  WHERE mc.platform=m.platform AND mc.match_id=m.match_id '
                '  AND (mc.error IS NOT NULL OR mc.cross_gap < -1e-6)))')

        sql = f"""
        SELECT m.match_id, m.home_team, m.away_team, m.league, m.country,
               m.kickoff_time, m.sportradar_id,
               -- 必须按 (market, side) 计数。队名盘口入库时被拆成
               -- market='Total' + side='H'/'A'，只数 market 会把三个不同盘口
               -- （全场 Total、主队 Total、客队 Total）算成一个，
               -- 接口 66 个盘口在列表页会显示成 49 个。
               (SELECT COUNT(DISTINCT CONCAT_WS('|', o.market, o.side))
                FROM kessbet_odds_prematch o
                WHERE o.platform = m.platform AND o.match_id = m.match_id) AS n_markets,
               (SELECT COUNT(*) FROM kessbet_odds_prematch o
                WHERE o.platform = m.platform AND o.match_id = m.match_id) AS n_sels,
               (SELECT MAX(o.updated_time) FROM kessbet_odds_prematch o
                WHERE o.platform = m.platform AND o.match_id = m.match_id) AS last_update
        FROM kessbet_match m
        WHERE {' AND '.join(where)}
        ORDER BY m.kickoff_time
        LIMIT %s OFFSET %s
        """
        args += [limit, offset]

        with dao.conn.cursor() as c:
            c.execute(sql, args)
            rows = c.fetchall()

        ids = [r[0] for r in rows]
        costs = dao.get_costs(ids)                       # 一次查询，O(1)
        plats = _platforms_of(dao, ids)
        # 1 小时前的成本。一次批量查询，不在请求里跑 ILP ——
        # 实测按赔率流水反推 25 场要 61.7 秒，页面不可能等。
        # 没有历史行的场次不会出现在结果里，前端据此显示「—」。
        prev_costs = dao.cost_at(ids, minutes=60)
        out = []
        for (mid, home, away, league, country, ko, srid,
             n_mk, n_sel, last_upd) in rows:
            cr = costs.get(mid)
            cost = cr['cost'] if cr else None
            if not n_sel:
                status = 'nodata'
            elif cr is None:
                status = 'pending'                       # 还没轮到此场
            elif cr.get('error'):
                status = 'nocov' if cr['error'] == 'no_cover' else 'error'
            else:
                status = 'ok'
            # 快照是否已经落后于赔率。落后意味着这个 cost 可能是旧值，
            # 详情页在这种情况下会实时重算，两个页面就会显示不同数字。
            # 前端据此打「可能过期」标记，避免用户困惑。
            stale = bool(
                cr is None
                or cr.get('computed_time') is None
                or (last_upd is not None
                    and cr['computed_time'] < last_upd))

            # 1h 变化 = 当前 − 1 小时前。任一缺失就返回 None，
            # 绝不用 0 充数 —— 「没变化」和「不知道」是两件事。
            prev = prev_costs.get(mid)
            prev_cost = prev['cost'] if prev else None
            delta = (round(cost - prev_cost, 6)
                     if cost is not None and prev_cost is not None else None)

            item = {
                'match_id': mid,
                'canonical_id': srid or f'local:kessbet:{mid}',
                'sportradar_id': srid,
                'home_team': home,
                'away_team': away,
                'league': league,
                'country': country,
                'kickoff_time': _iso(ko),
                'n_markets': int(n_mk or 0),
                'n_selections': int(n_sel or 0),
                'last_update': _iso(last_upd),
                'platforms': plats.get(mid) or [],
                'n_platforms': len(plats.get(mid) or []),
                'cost': cost,
                'cost_1h_ago': prev_cost,
                'cost_delta_1h': delta,
                'cost_scope': cr['cost_scope'] if cr else None,
                'computed_time': _iso(cr['computed_time']) if cr else None,
                'cost_stale': stale,
                'is_arbitrage': bool(cost is not None and cost < 1.0),
                'status': status,
            }
            out.append(item)

        # 总数（同样的 where，去掉 limit/offset）
        with dao.conn.cursor() as c:
            c.execute(f"SELECT COUNT(*) FROM kessbet_match m "
                      f"WHERE {' AND '.join(where)}", args[:-2])
            total = c.fetchone()[0]

        return {'total': int(total), 'offset': offset, 'limit': limit,
                'matches': out}
    finally:
        dao.close()


@app.get('/api/summary')
def summary(
    hours: int = Query(config.HORIZON_HOURS, ge=1, le=168),
    sport: str = Query('sr:sport:1'),
    q: str | None = Query(None),
) -> dict:
    """顶部统计指标，基于**全部符合条件的比赛**，不是当前页。

    页面原先在前端对当前页 25 条取 min，标题写着「单平台最低」但实际是
    「本页最低」，翻页就会变。这类全局指标必须由后端聚合。
    """
    dao = _dao()
    try:
        where = [
            "m.platform = 'kessbet'",
            'm.sport = %s',
            'm.kickoff_time > NOW()',
            'm.kickoff_time <= NOW() + INTERVAL %s HOUR',
        ]
        args: list[Any] = [sport, hours]
        if q:
            where.append('(m.home_team LIKE %s OR m.away_team LIKE %s OR m.league LIKE %s)')
            like = f'%{q}%'
            args += [like, like, like]
        w = ' AND '.join(where)

        with dao.conn.cursor() as c:
            c.execute(f"SELECT COUNT(*) FROM kessbet_match m WHERE {w}", args)
            total = c.fetchone()[0]

            # 成本统计：只统计有快照且有赔率的场次
            c.execute(f"""
                SELECT COUNT(*), MIN(cost), MAX(cost),
                       SUM(cost < 1.0),
                       SUM(cost < 1.03),
                       SUM(cost < 1.05)
                FROM kessbet_match_cost c
                WHERE c.platform='kessbet' AND c.error IS NULL
                  AND c.cost IS NOT NULL
                  AND c.match_id IN (SELECT m.match_id FROM kessbet_match m WHERE {w})
            """, args)
            n_cost, cmin, cmax, n_arb, n_103, n_105 = c.fetchone()

            # 还没算过的（有赔率但无快照）
            c.execute(f"""
                SELECT COUNT(*) FROM kessbet_match m
                LEFT JOIN kessbet_match_cost c
                  ON c.platform=m.platform AND c.match_id=m.match_id
                WHERE {w}
                  AND c.match_id IS NULL
                  AND EXISTS (SELECT 1 FROM kessbet_odds_prematch o
                              WHERE o.platform=m.platform AND o.match_id=m.match_id)
            """, args)
            pending = c.fetchone()[0]

            c.execute("SELECT COUNT(DISTINCT platform) FROM kessbet_match "
                      "WHERE platform='kessbet'")
            n_plat = c.fetchone()[0]

            # 中位数（MySQL 5.7 没有窗口函数，用偏移量取）
            median = None
            if n_cost:
                c.execute(f"""
                    SELECT cost FROM kessbet_match_cost c
                    WHERE c.platform='kessbet' AND c.error IS NULL
                      AND c.cost IS NOT NULL
                      AND c.match_id IN (SELECT m.match_id FROM kessbet_match m WHERE {w})
                    ORDER BY cost LIMIT 1 OFFSET %s
                """, args + [int(n_cost) // 2])
                r = c.fetchone()
                median = float(r[0]) if r else None

        return {
            'matches': int(total),
            'platforms': int(n_plat),
            'computed': int(n_cost or 0),
            'pending': int(pending or 0),
            'min_cost': float(cmin) if cmin is not None else None,
            'median_cost': median,
            'max_cost': float(cmax) if cmax is not None else None,
            'arbitrage': int(n_arb or 0),
            'below_1_03': int(n_103 or 0),
            'below_1_05': int(n_105 or 0),
        }
    finally:
        dao.close()


def _platforms_of(dao: Dao, match_ids: list[str]) -> dict[str, list[str]]:
    """批量查这些比赛在哪些平台有报价。"""
    if not match_ids:
        return {}
    ph = ','.join(['%s'] * len(match_ids))
    with dao.conn.cursor() as c:
        c.execute(
            f"SELECT c.match_id, c.platform FROM kessbet_match_canonical c "
            f"WHERE c.platform='kessbet' AND c.match_id IN ({ph})", match_ids)
        out: dict[str, list[str]] = {}
        for mid, p in c.fetchall():
            out.setdefault(mid, []).append(p)
    return out


# ---------------------------------------------------------------- 单场

@app.get('/api/matches/{match_id}')
def match_detail(match_id: str,
                 platform: str = Query('kessbet')) -> dict:
    """单场详情：比赛信息 + 双模型结果 + 完整盘口 + 变动流水。"""
    dao = _dao()
    try:
        row = dao.get_match(match_id)
        if not row:
            raise HTTPException(404, 'match not found')
        sport, home, away, kickoff = row

        # 跨平台信息
        with dao.conn.cursor() as c:
            c.execute("""SELECT canonical_id, GROUP_CONCAT(DISTINCT platform)
                         FROM kessbet_match_canonical
                         WHERE platform=%s AND match_id=%s
                         GROUP BY canonical_id""", (platform, match_id))
            cr = c.fetchone()
        canonical_id = cr[0] if cr else None
        platforms = cr[1].split(',') if cr and cr[1] else [platform]
        if cr and cr[0]:
            with dao.conn.cursor() as c:
                c.execute("SELECT DISTINCT platform FROM kessbet_match_canonical "
                          "WHERE canonical_id=%s ORDER BY platform", (cr[0],))
                platforms = [r[0] for r in c.fetchall()]

        rows = dao.get_selections(match_id)
        if not rows:
            # 比赛在赛程里但平台还没开盘（常见于提前几天挂牌的比赛）。
            # 这不是「找不到比赛」，不该用 404 —— 详情页只能显示
            # 「加载失败：HTTP 404」，用户看不出是没开盘还是链接错了。
            # 返回 200 + status='nodata'，页面渲染「该场暂无赔率」。
            return {
                'match_id': match_id,
                'canonical_id': canonical_id,
                'platform': platform,
                'platforms': platforms,
                'sport': sport,
                'home_team': home,
                'away_team': away,
                'kickoff_time': _iso(kickoff),
                'n_markets': 0,
                'n_selections': 0,
                'n_candidates': 0,
                'cost': None,
                'cost_scope': 'single',
                'cost_source': 'none',
                'error': 'no_data',
                'ilp': None,
                'lp': None,
                'cross_ok': True,
                'cross_gap': 0.0,
                'is_arbitrage': False,
                'markets': [],
                'n_market_groups': 0,
                'timeline': [],
                'status': 'nodata',
            }

        # 优先读 analyze 预计算的快照（毫秒级）。
        # 只有在快照缺失或赔率比快照新时才现场算一次 —— 单场 1.2 秒
        # 可以接受，但不能让列表页承担这个成本。
        snap = dao.get_costs([match_id]).get(match_id)
        odds_upd = dao.cost_updated_at(match_id)
        stale = (not snap
                 or snap['computed_time'] is None
                 or (odds_upd is not None and snap['computed_time'] < odds_upd))

        if stale:
            from analyze.run import evaluate_match
            a = evaluate_match(dao, match_id, sport, home, away, kickoff)
            ilp_cost = round(a.ilp.cost, 6) if a.ilp and a.ilp.cost else None
            lp_data = None if a.lp is None else {
                'worst_profit': round(a.lp.worst_profit, 6),
                'chosen': [{'desc': d, 'weight': round(w, 6)}
                           for d, w in a.lp.chosen],
            }
            cross_ok, cross_gap = a.cross_ok, a.cross_gap
            is_arb, err = a.is_arbitrage, a.error
            n_cand = a.n_candidates
        else:

            ilp_cost = snap['cost']
            lp_data = None if snap['lp_worst_profit'] is None else {
                'worst_profit': round(snap['lp_worst_profit'], 6),
                'chosen': json.loads(snap['lp_chosen'] or '[]'),
            }
            cross_ok = snap['cross_gap'] is not None and snap['cross_gap'] <= 1e-4
            cross_gap = snap['cross_gap'] or 0.0
            err = snap['error']
            is_arb = bool(ilp_cost is not None and ilp_cost < 1.0
                          and lp_data and lp_data['worst_profit'] > 1e-6)
            n_cand = snap['n_candidates']

        # 按 (盘口, 归属) 分组。实现放在 common/markets.py，
        # 因为 analyze 写套利快照时要用同一份逻辑 —— 页面显示和留存证据
        # 必须一致，各写一份早晚会分叉。
        markets = group_markets(rows, home, away)

        # 变动流水（按时间折叠）
        with dao.conn.cursor() as c:
            c.execute("""SELECT changed_time, COUNT(*)
                         FROM kessbet_odds_history
                         WHERE platform=%s AND match_id=%s
                         GROUP BY changed_time
                         ORDER BY changed_time DESC LIMIT 50""", (platform, match_id))
            timeline = [{'time': _iso(t), 'changes': int(n)} for t, n in c.fetchall()]

        return {
            'match_id': match_id,
            'canonical_id': canonical_id,
            'platform': platform,
            'platforms': platforms,
            'sport': sport,
            'home_team': home,
            'away_team': away,
            'kickoff_time': _iso(kickoff),
            'n_markets': len(markets),
            'n_selections': len(rows),
            'n_candidates': n_cand,
            'cost': ilp_cost,
            'cost_scope': 'single',
            'cost_source': 'live' if stale else 'snapshot',
            'error': err,
            'ilp': {'cost': ilp_cost,
                    'chosen': (a.ilp.chosen if stale and a.ilp else
                               (json.loads(snap['ilp_chosen'] or '[]') if snap else []))},
            'lp': lp_data,
            'cross_ok': cross_ok,
            'cross_gap': cross_gap,
            'is_arbitrage': is_arb,
            'markets': markets,
            'n_market_groups': len(markets),
            'timeline': timeline,
            'status': 'ok',
        }
    finally:
        dao.close()


@app.get('/api/matches/{match_id}/history')
def selection_history(match_id: str,
                      market: str, label: str,
                      side: str = '', line: str = '',
                      platform: str = Query('kessbet')) -> dict:
    """单个选项的赔率走势。"""
    dao = _dao()
    try:
        with dao.conn.cursor() as c:
            c.execute("""SELECT odds, prev_odds, changed_time
                         FROM kessbet_odds_history
                         WHERE platform=%s AND match_id=%s
                           AND market=%s AND side=%s AND line=%s AND selection=%s
                         ORDER BY changed_time""",
                      (platform, match_id, market, side, line, label))
            pts = [{'time': _iso(t), 'odds': float(o),
                    'prev': float(p) if p is not None else None}
                   for o, p, t in c.fetchall()]
        return {'market': market, 'side': side, 'line': line,
                'label': label, 'points': pts}
    finally:
        dao.close()


# ---------------------------------------------------------------- 套利命中

def _arb_row(r: dict) -> dict:
    """把命中快照的一行转成 API 结构。

    传入的是 DictCursor 的行，按列名取值 —— 位置解包太脆弱，
    两个 DAO 方法的 SELECT 顺序稍有不同就会把字段错配上，
    而且错误只在运行时暴露（开发时踩过一次）。
    """
    worst = r.get('lp_worst_profit')
    gap = r.get('cross_gap')
    home, away = r['home_team'], r['away_team']
    return {
        'id': r['id'],
        'alert_time': _iso(r['alert_time']),
        'match_id': r['match_id'],
        'canonical_id': r.get('canonical_id') or '',
        'sport': r['sport'],
        'home_team': home,
        'away_team': away,
        'league': r.get('league') or '',
        'kickoff_time': _iso(r['kickoff_time']),
        'cost': float(r['cost']),
        'cost_scope': r.get('cost_scope'),
        'lp_worst_profit': float(worst) if worst is not None else None,
        'profit_pct': round(float(worst) * 100, 4) if worst is not None else None,
        'cross_gap': float(gap) if gap is not None else None,
        'n_markets': int(r.get('n_markets') or 0),
        'n_selections': int(r.get('n_selections') or 0),
        'n_candidates': int(r.get('n_candidates') or 0),
        'ilp_chosen': [{'desc': d, 'text': describe_pick(d, home, away)}
                       for d in json.loads(r.get('ilp_chosen') or '[]')],
        'lp_chosen': [{'desc': x['desc'],
                       'text': describe_pick(x['desc'], home, away),
                       'weight': x['weight']}
                      for x in json.loads(r.get('lp_chosen') or '[]')],
        'odds_updated': _iso(r.get('odds_updated')),
    }


@app.get('/api/arbitrage')
def list_arbitrage(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    match_id: str | None = Query(None),
) -> dict:
    """套利命中列表，按告警时间倒序。

    正常情况下这是空列表 —— 单家博彩公司内部出现 cost<1 只可能是定价错误。
    """
    dao = _dao()
    try:
        rows = dao.list_arbitrage(limit=limit, offset=offset,
                                  match_id=match_id)
        total = dao.count_arbitrage(match_id)
        return {'total': int(total), 'offset': offset, 'limit': limit,
                'hits': [_arb_row(r) for r in rows]}
    finally:
        dao.close()


@app.get('/api/arbitrage/stats')
def arbitrage_stats() -> dict:
    """命中汇总。页面顶部用。

    必须定义在 /api/arbitrage/{snap_id} **之前** —— FastAPI 按注册顺序匹配，
    否则 'stats' 会被当成 snap_id 去转 int，直接 422。
    """
    dao = _dao()
    try:
        with dao.conn.cursor() as c:
            c.execute("""SELECT COUNT(*), COUNT(DISTINCT match_id),
                                MIN(cost), MIN(alert_time), MAX(alert_time)
                         FROM kessbet_arbitrage_snapshot
                         WHERE platform='kessbet'""")
            n, n_match, min_cost, first, last = c.fetchone()
        return {
            'hits': int(n or 0),
            'matches': int(n_match or 0),
            'min_cost': float(min_cost) if min_cost is not None else None,
            'first_alert': _iso(first),
            'last_alert': _iso(last),
        }
    finally:
        dao.close()


@app.get('/api/arbitrage/{snap_id}')
def arbitrage_detail(snap_id: int) -> dict:
    """单条命中快照的完整数据，含当时的全部盘口。"""
    dao = _dao()
    try:
        r = dao.get_arbitrage(snap_id)
        if not r:
            raise HTTPException(404, 'snapshot not found')
        out = _arb_row(r)
        out['market_detail'] = json.loads(r.get('market_detail') or '[]')
        return out
    finally:
        dao.close()


# ---------------------------------------------------------------- 静态页

WEB_DIR = os.path.join(BASE, 'web')
if os.path.isdir(WEB_DIR):
    app.mount('/static', StaticFiles(directory=WEB_DIR), name='static')


def _page(path):
    """返回 HTML 页并禁止缓存。

    FileResponse 默认带 ETag/Last-Modified，浏览器会用启发式缓存 ——
    改完页面 f5 看到的还是旧 JS，而服务端 curl 明明是新的。页面本身只有
    几十 KB，每次取一次比让人怀疑"改了没生效"划算。
    """
    return FileResponse(path, headers={
        'Cache-Control': 'no-store, must-revalidate',
        'Pragma': 'no-cache',
    })


@app.get('/')
def index():
    p = os.path.join(WEB_DIR, 'index.html')
    if os.path.isfile(p):
        return _page(p)
    raise HTTPException(404, 'web/index.html not found')


@app.get('/detail.html')
def detail_page():
    p = os.path.join(WEB_DIR, 'detail.html')
    if os.path.isfile(p):
        return _page(p)
    raise HTTPException(404, 'web/detail.html not found')


@app.get('/arbitrage')
@app.get('/arbitrage.html')
def arbitrage_page():
    p = os.path.join(WEB_DIR, 'arbitrage.html')
    if os.path.isfile(p):
        return _page(p)
    raise HTTPException(404, 'web/arbitrage.html not found')
