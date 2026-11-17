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

import pymysql                                 # noqa: E402

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

from analyze import payout, solver            # noqa: E402
from analyze.covers import soccer             # noqa: E402
from collect import config                    # noqa: E402
from common.markets import describe_pick, group_markets  # noqa: E402
from db.dao import Dao, beijing_now            # noqa: E402

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


# ---------------------------------------------------------------- 滚球

# 比赛状态码 -> 中文标签。实测值（2026-11-11）。
LIVE_STATUS_LABEL = {
    0: '未开始',
    6: '进行中',
    7: '进行中',
    31: '中场休息',      # 实测：比分和分钟都在，但久停不动，典型的中场
    100: '已结束',
    120: '点球大战',
}

# 状态码是平台私有编号，不要假设范围 —— 实测美式足球出现过 303。
# 未知码不要显示成「未知(303)」这种吓人的样子：只要它有比分/有分钟，
# 用户关心的是「在踢」，不是那个数字。保留原值方便排查。


def _status_label(status) -> str:
    if status is None:
        return '—'
    known = LIVE_STATUS_LABEL.get(status)
    if known:
        return known
    return '进行中 · 码 %s' % status


def _live_row(r: dict) -> dict:
    """把一行滚球状态转成 API 结构。"""
    status = r.get('status')
    return {
        'match_id': r['match_id'],
        'home_team': r.get('home_team') or '',
        'away_team': r.get('away_team') or '',
        'league': r.get('league') or '',
        'sport': r.get('sport') or '',
        'kickoff_time': _iso(r.get('kickoff_time')),
        'status': status,
        'status_label': _status_label(status),
        'minute': r.get('minute'),
        'home_score': r.get('home_score'),
        'away_score': r.get('away_score'),
        'home_corner': r.get('home_corner'),
        'away_corner': r.get('away_corner'),
        'home_yellow': r.get('home_yellow'),
        'away_yellow': r.get('away_yellow'),
        'home_red': r.get('home_red'),
        'away_red': r.get('away_red'),
        'n_markets': int(r.get('n_markets') or 0),
        'n_selections': int(r.get('n_selections') or 0),
        'fetched_time': _iso(r.get('fetched_time')),
        'odds_updated': _iso(r.get('odds_updated')),
    }


_STATUS_TEXT = {
    'waiting': '等待数据',
    'invalid_input': '输入无效',
    'no_candidate': '未发现候选',
    'unverifiable': '无法认证',
    'verified_candidate': '已验证候选',
    'expired': '已过期',
    'invalidated': '已失效',
    'shadow_candidate': '影子候选（未发布）',
}


def _status_text(status: str | None) -> str:
    return _STATUS_TEXT.get(status or '', status or '等待数据')


def _live_result_state(rc: dict, cur_sid) -> tuple[str | None, float | None]:
    """把存储状态映射成当前机会状态，并检查报价年龄。

    存储值（analysis_status）描述“分析结果是什么”；当前机会状态还要
    叠加当前快照指针、报价年龄和验证结论。
    """
    if not rc:
        return None, None
    age = None
    ts = rc.get('odds_updated')
    if ts is not None:
        age = max(0.0, (beijing_now() - ts).total_seconds())

    if rc.get('snapshot_id') != cur_sid:
        return 'invalidated', age
    if age is not None and age > config.LIVE_ODDS_MAX_AGE_SEC:
        return 'expired', age

    raw = rc.get('analysis_status')
    ver = rc.get('verification_status')
    cost = rc.get('cost')
    if raw in ('no_data', 'no_score'):
        return 'invalid_input', age
    if raw == 'no_cover':
        return 'no_candidate', age
    if raw == 'incomplete_model':
        return 'unverifiable', age
    if raw == 'valid':
        if (ver == 'verified' and cost is not None and cost < 1.0):
            return 'verified_candidate', age
        if ver in ('counterexample_found', 'unsupported_rules'):
            return 'unverifiable', age
        return 'no_candidate', age
    if raw in ('expired', 'invalidated'):
        return raw, age
    return 'waiting', age


@app.get('/api/live')
def list_live(
    sport: str = Query('sr:sport:1'),
    only_in_play: bool = Query(True, description='只看进行中的'),
    limit: int = Query(50, ge=1, le=200),
) -> dict:
    """滚球比赛列表。

    数据来自 kessbet_match_live（采集器每 10 秒刷一次状态）。
    赔率和盘口数来自 kessbet_odds_live。

    与 /api/matches 的区别：那个是赛前，这个是已经开赛的。
    两者时间区间不重叠。
    """
    dao = _dao()
    try:
        where = ["l.platform = 'kessbet'", 'm.sport = %s']
        args: list[Any] = [sport]
        if only_in_play:
            # 用**黑名单**（排除终态和未开赛），不要用白名单（IN (6,7)）。
            #
            # 白名单的坑实测踩过两次：
            #   1. ms=31（中场休息）被排除 —— 中场当然还属于「进行中」
            #   2. 美式足球的 ms=303 也会被排除
            # 状态码是平台私有编号，未知码会不断出现。用黑名单的话，
            # 新码默认当成进行中显示出来，最多是多显示一场；
            # 用白名单则会静默隐藏，而且很难发现。
            where.append('l.status NOT IN (0, 100, 120)')
        # 只显示还在滚球窗口内的，避免堆积历史
        where.append(
            "l.kickoff_time > NOW() - INTERVAL %s MINUTE")
        args.append(config.LIVE_WINDOW_MIN)

        sql = f"""
        SELECT l.match_id, l.status, l.minute,
               l.home_score, l.away_score,
               l.home_corner, l.away_corner,
               l.home_yellow, l.away_yellow,
               l.home_red, l.away_red,
               l.kickoff_time, l.fetched_time, l.current_snapshot_id,
               m.home_team, m.away_team, m.league, m.sport,
               (SELECT COUNT(DISTINCT CONCAT_WS('|', o.market, o.side))
                FROM kessbet_odds_live o
                WHERE o.platform = l.platform AND o.match_id = l.match_id)
                 AS n_markets,
               (SELECT COUNT(*) FROM kessbet_odds_live o
                WHERE o.platform = l.platform AND o.match_id = l.match_id)
                 AS n_selections,
               (SELECT MAX(o.updated_time) FROM kessbet_odds_live o
                WHERE o.platform = l.platform AND o.match_id = l.match_id)
                 AS odds_updated
        FROM kessbet_match_live l
        JOIN kessbet_match m
          ON m.platform = l.platform AND m.match_id = l.match_id
        WHERE {' AND '.join(where)}
        ORDER BY l.kickoff_time DESC
        LIMIT %s
        """
        with dao.conn.cursor(pymysql.cursors.DictCursor) as c:
            c.execute(sql, args + [limit])
            rows = c.fetchall()

        # 列表页要显示「这场比赛有没有套利」—— 一次批量查，不要逐场查
        costs = dao.get_live_result_costs([r['match_id'] for r in rows])
        out = []
        for r in rows:
            item = _live_row(r)
            rc = costs.get(r['match_id'])
            cur_sid = r.get('current_snapshot_id')
            if rc:
                status, age = _live_result_state(rc, cur_sid)
                item['result'] = {
                    'cost': rc['cost'],
                    'profit_pct': rc['profit_pct'],
                    'is_arbitrage': status == 'verified_candidate',
                    'is_candidate': status == 'verified_candidate',
                    'verification_status': rc.get('verification_status'),
                    'analysis_status': status,
                    'status_text': _status_text(status),
                    'status_reason': rc.get('status_reason'),
                    'is_stale': bool(
                        age is not None
                        and age > config.LIVE_ODDS_MAX_AGE_SEC),
                    'quote_age_sec': (round(age, 1)
                                      if age is not None else None),
                    'odds_updated': _iso(rc.get('odds_updated')),
                    'reach_states': rc['reach_states'],
                    'n_candidates': rc['n_candidates'],
                    'minute': rc['minute'],
                    'computed_time': _iso(rc.get('computed_time')),
                }
            else:
                item['result'] = None
            out.append(item)
        return {
            'total': len(rows),
            'matches': out,
            'live_analyze_enabled': bool(
                getattr(config, 'LIVE_ANALYZE_ENABLED', False)),
        }
    finally:
        dao.close()


@app.get('/api/live/result-costs')
def live_result_costs(
    limit: int = Query(100, ge=1, le=500),
    max_cost: float | None = Query(None, description='只看成本 <= 该值的'),
) -> dict:
    """滚球「比赛结果」套利成本，按成本升序。

    一场一行：把全部比赛结果盘（1x2 / Total / 让球 / 正确比分 / 半场盘）
    组合起来，覆盖所有可达终局的最小 Σ(1/赔率)。< 1 才是套利。

    必须定义在 /api/live/{match_id} 之前 —— FastAPI 按注册顺序匹配，
    否则 'result-costs' 会被当成 match_id。
    """
    dao = _dao()
    try:
        with dao.conn.cursor(pymysql.cursors.DictCursor) as c:
            where = ["r.platform='kessbet'"]
            args: list = []
            if max_cost is not None:
                where.append('r.cost <= %s')
                args.append(max_cost)
            c.execute(f"""
            SELECT r.match_id, r.cost, r.profit_pct, r.reach_states,
                   r.n_candidates, r.minute, r.home_score, r.away_score,
                   r.ht_home, r.ht_away, r.kickoff_time, r.computed_time,
                   r.verification_status, r.analysis_status, r.snapshot_id,
                   r.odds_updated, r.status_reason, r.input_fingerprint,
                   l.current_snapshot_id,
                   m.home_team, m.away_team, m.league
            FROM kessbet_live_result_cost r
            JOIN kessbet_match m
              ON m.platform = r.platform AND m.match_id = r.match_id
            LEFT JOIN kessbet_match_live l
              ON l.platform = r.platform AND l.match_id = r.match_id
            WHERE {' AND '.join(where)}
            ORDER BY r.cost IS NULL, r.cost ASC
            LIMIT %s
            """, args + [limit])
            rows = c.fetchall()

            c.execute("""
            SELECT COUNT(*) AS n, MIN(cost) AS lo, AVG(cost) AS avg_
            FROM kessbet_live_result_cost WHERE platform='kessbet'
            """)
            st = c.fetchone()

        out_rows = []
        verified = 0
        for r in rows:
            cost = float(r['cost']) if r['cost'] is not None else None
            status, age = _live_result_state({
                'analysis_status': r['analysis_status'],
                'verification_status': r['verification_status'],
                'snapshot_id': r['snapshot_id'],
                'odds_updated': r['odds_updated'],
                'cost': cost,
            }, r['current_snapshot_id'])
            is_arb = status == 'verified_candidate'
            if is_arb:
                verified += 1
            out_rows.append({
                'match_id': r['match_id'],
                'cost': cost,
                'profit_pct': (float(r['profit_pct'])
                               if r['profit_pct'] is not None else None),
                'reach_states': r['reach_states'],
                'n_candidates': r['n_candidates'],
                'minute': r['minute'],
                'score': ('%s:%s' % (r['home_score'], r['away_score'])
                          if r['home_score'] is not None
                          and r['away_score'] is not None else None),
                'half_time': ('%s:%s' % (r['ht_home'], r['ht_away'])
                              if r['ht_home'] is not None else None),
                'home_team': r['home_team'],
                'away_team': r['away_team'],
                'league': r['league'],
                'kickoff_time': _iso(r['kickoff_time']),
                'computed_time': _iso(r['computed_time']),
                'verification_status': r['verification_status'],
                'analysis_status': status,
                'status_text': _status_text(status),
                'status_reason': r['status_reason'],
                'is_arbitrage': is_arb,
                'is_stale': bool(age is not None
                                 and age > config.LIVE_ODDS_MAX_AGE_SEC),
                'quote_age_sec': round(age, 1) if age is not None else None,
                'odds_updated': _iso(r['odds_updated']),
                'snapshot_id': r['snapshot_id'],
                'input_fingerprint': r['input_fingerprint'],
            })

        return {
            'total': int(st['n'] or 0),
            'min_cost': (float(st['lo']) if st['lo'] is not None else None),
            'avg_cost': (float(st['avg_']) if st['avg_'] is not None else None),
            'arbitrage': verified,
            'rows': out_rows,
        }
    finally:
        dao.close()


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


@app.get('/api/live/{match_id}')
def live_detail(match_id: str) -> dict:
    """滚球单场详情：当前状态 + 全部盘口 + 窗口盘成本 + 赔率变动。

    和 /api/matches/{id}（赛前详情）的区别：
      赛前那套有 ILP/LP 两套模型和 2025 状态的收益表；
      滚球只做窗口盘判定（模型 A），所以这里给的是每个窗口盘的
      内部成本，以及它自己的赔率变动序列。
    """
    dao = _dao()
    try:
        with dao.conn.cursor(pymysql.cursors.DictCursor) as c:
            c.execute("""
            SELECT l.*, m.home_team, m.away_team, m.league, m.sport,
                   (SELECT COUNT(DISTINCT CONCAT_WS('|', o.market, o.side))
                    FROM kessbet_odds_live o
                    WHERE o.platform = l.platform AND o.match_id = l.match_id)
                     AS n_markets,
                   (SELECT COUNT(*) FROM kessbet_odds_live o
                    WHERE o.platform = l.platform AND o.match_id = l.match_id)
                     AS n_selections
            FROM kessbet_match_live l
            JOIN kessbet_match m
              ON m.platform = l.platform AND m.match_id = l.match_id
            WHERE l.platform='kessbet' AND l.match_id=%s
            """, (match_id,))
            row = c.fetchone()
        if not row:
            raise HTTPException(404, '滚球状态不存在')

        with dao.conn.cursor() as c:
            # 必须只取 5 列：group_markets 按 (market, side, line, label, odds)
            # 解包。带上 prev_odds 会直接 ValueError。
            c.execute("""SELECT market, side, line, selection, odds
                         FROM kessbet_odds_live
                         WHERE platform='kessbet' AND match_id=%s
                         ORDER BY market, side, line, selection""",
                      (match_id,))
            rows = c.fetchall()

        out = _live_row(row)
        out['markets'] = group_markets(rows, row['home_team'], row['away_team'])
        # 判定是否在跑。关闭时 result 里是最后一次计算的历史值，
        # 页面必须标成「历史结果」，不能继续显示为当前机会。
        out['live_analyze_enabled'] = bool(
            getattr(config, 'LIVE_ANALYZE_ENABLED', False))

        # 比赛结果套利判定（核心指标）
        rc = dao.get_live_result_cost(match_id)
        if rc:
            status, age = _live_result_state(
                rc, row.get('current_snapshot_id'))
            cost = rc.get('cost')
            chosen = json.loads(rc.get('chosen') or '[]')
            out['result'] = {
                'cost': cost,
                'profit_pct': rc.get('profit_pct'),
                'is_arbitrage': status == 'verified_candidate',
                'is_candidate': status == 'verified_candidate',
                'verification_status': rc.get('verification_status'),
                'verification_detail': (
                    json.loads(rc['verification_detail'])
                    if rc.get('verification_detail') else None),
                'analysis_status': status,
                'status_text': _status_text(status),
                'status_reason': rc.get('status_reason'),
                'is_stale': bool(
                    age is not None and age > config.LIVE_ODDS_MAX_AGE_SEC),
                'quote_age_sec': (round(age, 1)
                                  if age is not None else None),
                'odds_updated': _iso(rc.get('odds_updated')),
                'reach_states': rc.get('reach_states'),
                'n_candidates': rc.get('n_candidates'),
                'minute': rc.get('minute'),
                'score': ('%s:%s' % (rc['home_score'], rc['away_score'])
                          if rc.get('home_score') is not None
                          and rc.get('away_score') is not None else None),
                'half_time': ('%s:%s' % (rc['ht_home'], rc['ht_away'])
                              if rc.get('ht_home') is not None else None),
                'chosen': [{
                    'desc': c['desc'],
                    'text': describe_pick(c['desc'], row['home_team'],
                                          row['away_team']),
                    'odds': float(c['odds']) if c.get('odds') else None,
                    'weight': c.get('weight'),
                } for c in chosen],
                'computed_time': _iso(rc.get('computed_time')),
            }
        else:
            out['result'] = None
        return out
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


@app.get('/api/live/{match_id}/history')
def live_selection_history(match_id: str,
                           market: str, label: str,
                           side: str = '', line: str = '') -> dict:
    """滚球单个选项的赔率走势。

    读 kessbet_odds_live_history —— 只有赔率**变化**才记一行，
    所以点数是变动的时刻，不是采样序列。
    每点带当时的比赛分钟和比分，能看到「进球后赔率怎么跳的」。
    """
    dao = _dao()
    try:
        with dao.conn.cursor() as c:
            c.execute("""SELECT odds, prev_odds, changed_time, minute,
                                home_score, away_score
                         FROM kessbet_odds_live_history
                         WHERE platform='kessbet' AND match_id=%s
                           AND market=%s AND side=%s AND line=%s
                           AND selection=%s
                         ORDER BY changed_time""",
                      (match_id, market, side, line, label))
            pts = [{'time': _iso(t), 'odds': float(o),
                    'prev': float(p) if p is not None else None,
                    'minute': m, 'home_score': hs, 'away_score': as_}
                   for o, p, t, m, hs, as_ in c.fetchall()]
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
    cost_f = float(r['cost']) if r.get('cost') is not None else None

    # 收益率的来源有两个：
    #   赛前命中 → LP 实测的最差净收益（允许部分下注，比 ILP 隐含值更优）
    #   滚球命中 → 没跑 LP，只能由成本推算 t = 1/cost - 1
    # 不统一的话滚球那些行「最差收益」永远是空的，页面看起来像没算出结果。
    if worst is not None:
        profit_pct = round(float(worst) * 100, 4)
        profit_src = 'lp'
    elif cost_f:
        profit_pct = round((1.0 / cost_f - 1.0) * 100, 4)
        profit_src = 'cost'
    else:
        profit_pct = None
        profit_src = None
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
        'profit_pct': profit_pct,
        'profit_src': profit_src,
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
        'is_live': bool(r.get('is_live')),
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
        detail = json.loads(r.get('market_detail') or '[]')
        _annotate_detail(detail, r.get('home_team') or '',
                         r.get('away_team') or '')
        out['market_detail'] = detail
        out['replay_available'] = bool(r.get('replay_context'))
        return out
    finally:
        dao.close()


@app.get('/api/evidence/{snap_id}')
def evidence_detail(snap_id: int) -> dict:
    """不可变证据页的数据：命中快照 + 同一次请求的原始平台响应。

    和 `/api/arbitrage/{id}` 的区别：这里返回完整 `replay_context`
    （含原始 `mari` 响应），页面只读快照，不读实时赔率表。
    """
    dao = _dao()
    try:
        r = dao.get_arbitrage(snap_id)
        if not r:
            raise HTTPException(404, 'snapshot not found')
        out = _arb_row(r)
        detail = json.loads(r.get('market_detail') or '[]')
        _annotate_detail(detail, r.get('home_team') or '',
                         r.get('away_team') or '')
        out['market_detail'] = detail
        try:
            replay = (json.loads(r['replay_context'])
                      if r.get('replay_context') else None)
        except (TypeError, ValueError):
            replay = None
        out['replay'] = replay
        raw = (replay or {}).get('raw')
        ver = (replay or {}).get('verification') or {}
        vstatus = ver.get('status')
        if vstatus == 'verified':
            analysis_status = 'verified'
            analysis_text = '白名单无界验证通过；仍需官网共同观察核验'
        elif vstatus == 'counterexample_found':
            ce = ver.get('counterexample') or {}
            analysis_status = 'counterexample_found'
            analysis_text = (
                '发现亏损反例：%s:%s 返还 %.4f'
                % (ce.get('home_score'), ce.get('away_score'),
                   ce.get('payout') or 0.0))
        elif vstatus == 'unsupported_rules':
            analysis_status = 'unsupported_rules'
            analysis_text = '包含非白名单市场，不能认证'
        else:
            analysis_status = 'internal_candidate'
            analysis_text = '内部候选，尚未通过完备性/官网核验'
        out['evidence'] = {
            'raw_available': bool(raw),
            'raw_fetched_time': (replay or {}).get('raw_fetched_time'),
            'level': 'E1' if raw else 'E0',
            'level_label': '原始接口已存' if raw else '仅内部记录',
            'level_text': ('已保存同一次请求的原始接口响应；'
                           '尚未完成官网共同观察核验'
                           if raw else
                           '仅内部计算记录：没有保存同一次请求的原始接口响应'),
            'analysis_status': analysis_status,
            'analysis_status_text': analysis_text,
            'verification': ver,
        }
        return out
    finally:
        dao.close()


@app.get('/api/arbitrage/{snap_id}/replay')
def replay_arbitrage(snap_id: int) -> dict:
    """用命中时保存的证据包重新计算滚球套利，验证结果能否复现。"""
    from analyze.live.run import replay_live_evidence

    dao = _dao()
    try:
        r = dao.get_arbitrage(snap_id)
        if not r:
            raise HTTPException(404, 'snapshot not found')
        if not r.get('is_live'):
            raise HTTPException(400, 'replay is only available for live snapshots')
        replay = replay_live_evidence(r.get('replay_context'))
        replay['snapshot_id'] = snap_id
        replay['match_id'] = r.get('match_id')
        replay['alert_time'] = _iso(r.get('alert_time'))
        replay['odds_updated'] = _iso(r.get('odds_updated'))
        return replay
    finally:
        dao.close()


def _annotate_detail(detail: list, home: str, away: str) -> None:
    """给快照里的每个选项补一个可读的 text。

    两种来源的格式不一样：
      赛前命中 → group_markets 产出，label 是干净的（`over`），line 单独一列
      滚球命中 → 直接存的是求解器的内部键（`Handicap|2:0|1`）

    不统一的话，页面会把内部键原样显示成 `Handicap|2:0|1`，
    用户看不懂这是哪个盘口的哪个选项。
    """
    for mk in detail:
        name = mk.get('name') or mk.get('base') or ''
        for s in mk.get('selections') or []:
            lab = s.get('label') or ''
            if '|' in lab:
                s['text'] = describe_pick(lab, home, away)
                parts = lab.split('|')
                if len(parts) >= 4:
                    s['line'] = s.get('line') or parts[2]
                    s['label'] = parts[3]
            else:
                line = s.get('line') or ''
                tail = ' '.join(x for x in (line, lab) if x)
                s['text'] = f'{name} · {tail}' if tail else name


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


@app.get('/live')
@app.get('/live.html')
def live_page():
    p = os.path.join(WEB_DIR, 'live.html')
    if os.path.isfile(p):
        return _page(p)
    raise HTTPException(404, 'web/live.html not found')


@app.get('/live/detail')
@app.get('/live/detail.html')
@app.get('/live/{match_id}')
def live_detail_page(match_id: str = ''):
    """滚球单场详情页。

    支持两种 URL：
      /live/<match_id>      路径带 id（列表页点进来用的）
      /live/detail?id=<id>   query 带 id（分享链接时更明确）
    页面里的 JS 两处都读。
    """
    p = os.path.join(WEB_DIR, 'live_detail.html')
    if os.path.isfile(p):
        return _page(p)
    raise HTTPException(404, 'web/live_detail.html not found')


@app.get('/evidence/{snap_id}')
def evidence_page(snap_id: int):
    """不可变的命中证据页。只读快照，不读实时赔率表。"""
    p = os.path.join(WEB_DIR, 'evidence.html')
    if os.path.isfile(p):
        return _page(p)
    raise HTTPException(404, 'web/evidence.html not found')
