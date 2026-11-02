"""KessBet 采集健康检查。

检查两类问题：
  A. 采集是否在正常工作
  B. 数据是否正确

用法: py test/scratch/verify_collect.py [--json]
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE))

from db.dao import Dao

BEIJING = timezone(timedelta(hours=8))

# 判定阈值
COVERAGE_MIN = 0.95          # 覆盖度下限
STALE_HOURS = 3              # prematch 完全无更新多少小时算异常

# 覆盖检查分两段：
#
# 1. 硬性窗口（CRITICAL_HOURS=6）：距开赛 6 小时内的比赛必须有赔率。
#    实测这些场次 100% 覆盖，平台不会临开赛才挂盘。
#
# 2. 软性窗口（COVERAGE_HOURS=23）：6~23 小时的比赛，允许少量缺失。
#    平台对远期比赛的开盘节奏不固定，实测约 2% 的场次在 6~12 小时区间
#    仍无盘口——这是平台的节奏，不是采集漏洞。
#
# 不用 24 小时是因为采集每小时刷新一次，新进窗口的比赛要等下一轮才补拉。
CRITICAL_HOURS = 6
COVERAGE_HOURS = 23
CRITICAL_MIN = 1.0          # 6 小时内必须 100% 覆盖
SOFT_MIN = 0.95             # 6~23 小时允许 5% 缺失

# 判断"这场比赛是否还在平台上"：赛程每小时刷新一次，upsert 会把
# 平台返回的每场比赛的 updated_time 刷成当前时刻。所以 updated_time
# 停在上一轮之前的比赛，就是已经下架的（延期/取消/改期）。
# 这些场次不该算进覆盖度分母，否则每次都会误报。
ACTIVE_WITHIN_MIN = 90
# 赛程基线本身的新鲜度下限。如果连最新的 updated_time 都很旧，
# 说明赛程刷新已经停了——这时候所有比赛都会被判为"已下架"，
# 覆盖度会变成 0/0 假通过。必须先报 FAIL。
SCHEDULE_STALE_HOURS = 2


def _active_clause():
    """只统计仍在平台上的比赛。"""
    return ("m.updated_time >= (SELECT MAX(updated_time) FROM kessbet_match) "
            "- INTERVAL %d MINUTE" % ACTIVE_WITHIN_MIN)


def check_schedule_fresh(c, now):
    """赛程刷新是否还在跑。覆盖度判定依赖它，必须先查。"""
    c.execute("SELECT MAX(updated_time) FROM kessbet_match")
    last = c.fetchone()[0]
    if not last:
        return {'ok': False, 'note': 'kessbet_match 表为空'}
    age = (now - last).total_seconds() / 3600
    return {
        'ok': age <= SCHEDULE_STALE_HOURS,
        'last': last.strftime('%m-%d %H:%M:%S'),
        'age_hours': round(age, 2),
        'note': '' if age <= SCHEDULE_STALE_HOURS else
                '赛程已 %.1f 小时未刷新（每小时应刷一次），覆盖度判定不可信' % age,
    }


def _now():
    return datetime.now(BEIJING).replace(tzinfo=None)


def check_coverage(c, now):
    """分段检查覆盖度：6 小时内必须全覆盖，6~23 小时允许少量缺失。"""
    # 先查 6 小时内的硬性指标
    c.execute("""SELECT COUNT(*) FROM kessbet_match m
                 WHERE m.kickoff_time BETWEEN %s AND %s
                   AND """ + _active_clause(),
              (now, now + timedelta(hours=CRITICAL_HOURS)))
    critical_should = c.fetchone()[0]

    if critical_should:
        c.execute("""SELECT COUNT(DISTINCT o.match_id) FROM kessbet_odds_prematch o
                     JOIN kessbet_match m ON m.platform=o.platform AND m.match_id=o.match_id
                     WHERE m.kickoff_time BETWEEN %s AND %s
                       AND """ + _active_clause(),
                  (now, now + timedelta(hours=CRITICAL_HOURS)))
        critical_have = c.fetchone()[0]
        critical_ratio = critical_have / critical_should

        if critical_ratio < CRITICAL_MIN:
            c.execute("""SELECT m.match_id, m.sport, m.home_team, m.away_team,
                                m.kickoff_time
                         FROM kessbet_match m
                         WHERE m.kickoff_time BETWEEN %s AND %s
                           AND """ + _active_clause() + """
                           AND NOT EXISTS (SELECT 1 FROM kessbet_odds_prematch o
                                           WHERE o.platform=m.platform
                                             AND o.match_id=m.match_id)
                         ORDER BY m.kickoff_time LIMIT 20""",
                      (now, now + timedelta(hours=CRITICAL_HOURS)))
            missing = [{'match_id': r[0], 'sport': r[1], 'home': r[2],
                        'away': r[3], 'kickoff': r[4].strftime('%m-%d %H:%M')}
                       for r in c.fetchall()]
            return {'ok': False,
                    'critical_should': critical_should,
                    'critical_have': critical_have,
                    'should': critical_should, 'have': critical_have,
                    'ratio': round(critical_ratio, 4),
                    'missing': missing,
                    'note': '%dh 内缺 %d 场（必须全覆盖）' % (
                        CRITICAL_HOURS, critical_should - critical_have)}

    c.execute("""SELECT COUNT(*) FROM kessbet_match m
                 WHERE m.kickoff_time BETWEEN %s AND %s
                   AND """ + _active_clause(),
              (now, now + timedelta(hours=COVERAGE_HOURS)))
    should = c.fetchone()[0]

    if should == 0:
        return {'ok': True, 'should': 0, 'have': 0, 'missing': [],
                'critical_should': critical_should if critical_should else 0,
                'critical_have': critical_have if critical_should else 0,
                'note': '%dh 内无比赛' % COVERAGE_HOURS}

    c.execute("""SELECT COUNT(DISTINCT o.match_id) FROM kessbet_odds_prematch o
                 JOIN kessbet_match m ON m.platform=o.platform AND m.match_id=o.match_id
                 WHERE m.kickoff_time BETWEEN %s AND %s
                   AND """ + _active_clause(),
              (now, now + timedelta(hours=COVERAGE_HOURS)))
    have = c.fetchone()[0]

    c.execute("""SELECT m.match_id, m.sport, m.home_team, m.away_team,
                        m.kickoff_time
                 FROM kessbet_match m
                 WHERE m.kickoff_time BETWEEN %s AND %s
                   AND """ + _active_clause() + """
                   AND NOT EXISTS (SELECT 1 FROM kessbet_odds_prematch o
                                   WHERE o.platform=m.platform AND o.match_id=m.match_id)
                 ORDER BY m.kickoff_time LIMIT 20""",
              (now, now + timedelta(hours=COVERAGE_HOURS)))
    missing = [{
        'match_id': r[0], 'sport': r[1],
        'home': r[2], 'away': r[3],
        'kickoff': r[4].strftime('%m-%d %H:%M'),
    } for r in c.fetchall()]

    ratio = have / should
    return {
        'ok': ratio >= SOFT_MIN,
        'should': should, 'have': have,
        'ratio': round(ratio, 4),
        'critical_should': critical_should if critical_should else 0,
        'critical_have': critical_have if critical_should else 0,
        'missing': missing,
    }


def check_consistency(c, now, sample=8):
    """抽样比对：库内当前值 vs 接口实时值。

    这是最能反映"采集是否正常"的检查——直接验证抓到的数据对不对。
    其他检查（新鲜度、覆盖）只能发现明显的故障，一致性检查能发现
    数据陈旧、WS 漏推、部分写入失败等隐蔽问题。

    抽样的选择：优先抽临近开赛的比赛（这些最容易变，最可能暴露问题），
    不足时补随机场次。
    """
    # 候选：临近开赛但有缓冲的比赛（5 分钟 ~ 6 小时）。
    # 不能取"最早开赛"——刚开赛的接口会返回空，造成误报。
    c.execute("""SELECT m.match_id, m.home_team, m.away_team, m.sport
                 FROM kessbet_match m
                 WHERE m.platform='kessbet'
                   AND m.kickoff_time BETWEEN %s AND %s
                   AND EXISTS (SELECT 1 FROM kessbet_odds_prematch o
                               WHERE o.platform=m.platform AND o.match_id=m.match_id)
                 ORDER BY m.kickoff_time
                 LIMIT %s""",
              (now + timedelta(minutes=5), now + timedelta(hours=6), sample))
    targets = c.fetchall()

    if not targets:
        return {'ok': True, 'checked': 0, 'note': '无可抽样的比赛'}

    from collect.kessbet.http import KessbetHttp
    http = KessbetHttp()

    checked = []
    mismatched = []
    errors = []

    for mid, home, away, sport in targets:
        db = {(r[0], r[1], r[2], r[3]): r[4]
              for r in _get_selections(c, mid)}
        try:
            fresh = http.fetch_odds(mid, home, away)
        except Exception as e:
            errors.append({'match_id': mid, 'error': str(e)[:80]})
            continue

        api = {(s.market, s.side, s.line, s.label): s.odds for s in fresh}
        if not api:
            errors.append({'match_id': mid, 'error': '接口返回空'})
            continue

        # 只比较两边都有的 key，缺失单独统计
        common = set(db) & set(api)
        changed = [k for k in common
                   if abs(db[k] - api[k]) > 1e-6]
        only_db = len(set(db) - set(api))
        only_api = len(set(api) - set(db))

        checked.append({
            'match_id': mid, 'home': home, 'away': away,
            'db': len(db), 'api': len(api),
            'changed': len(changed),
            'only_db': only_db, 'only_api': only_api,
        })

        # 判定：变动超过 20% 或有大量缺失 -> 不一致
        diverge = len(changed) / max(len(common), 1)
        if diverge > 0.2 or only_api > len(api) * 0.5 or only_db > len(db) * 0.5:
            mismatched.append({
                'match_id': mid, 'home': home, 'away': away,
                'changed': len(changed), 'common': len(common),
                'only_db': only_db, 'only_api': only_api,
                'sample_changes': [
                    (k[0][:30], k[3], db[k], api[k])
                    for k in changed[:3]
                ],
            })

    ok = not mismatched and not errors
    return {
        'ok': ok,
        'checked': len(checked),
        'mismatched': mismatched,
        'errors': errors,
        'details': checked,
    }


def _get_selections(c, match_id):
    c.execute("""SELECT market, side, line, selection, odds
                 FROM kessbet_odds_prematch
                 WHERE platform='kessbet' AND match_id=%s""", (match_id,))
    return [(r[0], r[1], r[2], r[3], float(r[4])) for r in c.fetchall()]


def check_freshness(c, now):
    """prematch 最后写入时间。

    注意：所有比赛都远离开赛时赔率不动，这是正常的，所以阈值放得很宽。
    真正的"采集是否存活"应该看进程/WS 状态，这个检查只是兜底。
    """
    c.execute("SELECT MAX(updated_time) FROM kessbet_odds_prematch")
    last = c.fetchone()[0]
    if not last:
        return {'ok': False, 'note': 'prematch 表为空'}

    age = (now - last).total_seconds() / 3600
    return {
        'ok': age <= STALE_HOURS,
        'last': last.strftime('%m-%d %H:%M:%S'),
        'age_hours': round(age, 2),
    }


def check_integrity(c, now):
    """数据完整性：主键冲突、异常赔率、历史冗余。"""
    issues = []

    c.execute("""SELECT COUNT(*) FROM (
                   SELECT 1 FROM kessbet_odds_prematch
                   GROUP BY platform, match_id, market, side, line, selection
                   HAVING COUNT(*) > 1) x""")
    dup = c.fetchone()[0]
    if dup:
        issues.append('主键冲突 %d 组' % dup)

    c.execute("SELECT COUNT(*) FROM kessbet_odds_prematch WHERE odds < 1")
    bad = c.fetchone()[0]
    if bad:
        issues.append('赔率 <1 的 %d 行' % bad)

    c.execute("""SELECT COUNT(*) FROM kessbet_odds_history
                 WHERE prev_odds IS NOT NULL AND ABS(odds - prev_odds) < 1e-9""")
    redundant = c.fetchone()[0]
    if redundant:
        issues.append('历史表冗余 %d 行（赔率未变却记录）' % redundant)

    return {'ok': not issues, 'issues': issues}


def run(as_json=False):
    dao = Dao()
    now = _now()
    try:
        with dao.conn.cursor() as c:
            schedule = check_schedule_fresh(c, now)
            results = {
                'now': now.strftime('%Y-%m-%d %H:%M:%S'),
                'schedule': schedule,
                # 赛程不新鲜时，覆盖度分母不可信，直接判 FAIL
                'coverage': (check_coverage(c, now) if schedule['ok'] else
                             {'ok': False, 'should': 0, 'have': 0, 'missing': [],
                              'note': schedule['note']}),
                'consistency': check_consistency(c, now),
                'freshness': check_freshness(c, now),
                'integrity': check_integrity(c, now),
            }

            c.execute("SELECT COUNT(*) FROM kessbet_odds_prematch")
            results['prematch_rows'] = c.fetchone()[0]
            c.execute("SELECT COUNT(*) FROM kessbet_odds_history")
            results['history_rows'] = c.fetchone()[0]
    finally:
        dao.close()

    all_ok = all(v['ok'] for v in results.values() if isinstance(v, dict) and 'ok' in v)
    results['all_ok'] = all_ok

    if as_json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return 0 if all_ok else 1

    # 人类可读
    print('[%s]' % results['now'])

    sch = results['schedule']
    mark = 'OK  ' if sch['ok'] else 'FAIL'
    if 'last' in sch:
        print('  赛程   %s  最后刷新 %s (%.1f 小时前)' % (
            mark, sch['last'], sch['age_hours']))
    else:
        print('  赛程   %s  %s' % (mark, sch.get('note')))

    cov = results['coverage']
    if cov['should'] or cov.get('critical_should'):
        mark = 'OK  ' if cov['ok'] else 'FAIL'
        print('  覆盖   %s' % mark)
        if cov.get('critical_should'):
            cmark = 'Y' if cov['critical_have'] == cov['critical_should'] else 'N'
            print('         %s %dh内(硬性)  %d/%d (%.1f%%)' % (
                cmark, CRITICAL_HOURS, cov['critical_have'],
                cov['critical_should'],
                100 * cov['critical_have'] / cov['critical_should']))
        if cov['should']:
            print('           %dh内(软性)  %d/%d (%.1f%%)' % (
                COVERAGE_HOURS, cov['have'], cov['should'], cov['ratio'] * 100))
        for m in cov['missing'][:5]:
            print('         缺: %-18s vs %-18s %s' % (
                m['home'][:18], m['away'][:18], m['kickoff']))
        if len(cov['missing']) > 5:
            print('         ... 共 %d 场缺失' % len(cov['missing']))
    else:
        print('  覆盖   --  %s' % cov.get('note', ''))

    con = results['consistency']
    if con.get('note'):
        print('  一致性 --  %s' % con['note'])
    else:
        mark = 'OK  ' if con['ok'] else 'FAIL'
        print('  一致性 %s  抽样 %d 场, 不一致 %d 场, 错误 %d 场' % (
            mark, con['checked'], len(con['mismatched']), len(con['errors'])))
        for m in con['mismatched'][:3]:
            print('         %-18s vs %-18s  变动 %d/%d 项, 仅库内 %d, 仅接口 %d' % (
                m['home'][:18], m['away'][:18],
                m['changed'], m['common'], m['only_db'], m['only_api']))
            for desc, sel, old, new in m['sample_changes']:
                print('            %-32s %-8s %s -> %s' % (desc, sel, old, new))
        for e in con['errors'][:3]:
            print('         错误: %s %s' % (e['match_id'][-8:], e['error']))

    fr = results['freshness']
    mark = 'OK  ' if fr['ok'] else 'FAIL'
    if 'last' in fr:
        print('  新鲜度 %s  最后写入 %s (%.1f 小时前)' % (
            mark, fr['last'], fr['age_hours']))
    else:
        print('  新鲜度 %s  %s' % (mark, fr.get('note')))

    it = results['integrity']
    mark = 'OK  ' if it['ok'] else 'FAIL'
    print('  完整性 %s  %s' % (mark, '; '.join(it['issues']) if it['issues'] else '无异常'))

    print('  数据量 prematch=%d  history=%d' % (
        results['prematch_rows'], results['history_rows']))
    print('  ' + ('全部正常' if all_ok else '存在问题'))
    return 0 if all_ok else 1


if __name__ == '__main__':
    sys.exit(run('--json' in sys.argv))
