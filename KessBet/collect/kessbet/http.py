"""collect/kessbet/http.py — KessBet HTTP 接口封装。

请求体格式：request=<urlencode(base64(AES-192-CBC(JSON)))>
响应明文 JSON。
"""
import base64
import json
import logging
import time
import urllib.parse
from datetime import datetime, timezone

from Crypto.Cipher import AES
from curl_cffi import requests as creq

from common.models import Match
from common.timeutil import from_ts, now_beijing
from collect import config
from collect.kessbet.parse import parse_mari, parse_market_id

log = logging.getLogger(__name__)

API = 'https://api.kessbet.com/cpop/ots/unct/public/handler'
KEY = b'QipVAlnxoMjeQ4I47ne7kWDk'      # AES-192，前端硬编码，不过期
IV = b'0000000000000000'

ACTION_LIST = '00252'
ACTION_LIVE_LIST = '00232'      # 滚球列表 —— 赛程接口(00252)不返回滚球
ACTION_MATCH = '0015'
ACTION_STATE = '0014'

HEADERS = {
    'content-type': 'application/x-www-form-urlencoded;charset=UTF-8',
    'origin': 'https://www.kessbet.com',
    'referer': 'https://www.kessbet.com/',
}


class ForbiddenError(Exception):
    """Cloudflare 403——说明代理失效，不是单场问题。"""


def encrypt(payload: dict) -> str:
    raw = json.dumps(payload, separators=(',', ':')).encode()
    pad = 16 - len(raw) % 16
    raw += bytes([pad]) * pad
    ct = AES.new(KEY, AES.MODE_CBC, IV).encrypt(raw)
    b64 = base64.b64encode(ct).decode()
    return 'request=' + urllib.parse.quote(b64, safe='=').replace('+', '%2B')


def _header(action: str) -> dict:
    return {
        'url': '//api.kessbet.com/cpop/ots/unct/public/handler',
        'method': 'post',
        'action': action,
        'isApp': 0,
        'brand': 'KYA',
        'cmdName': 'h5_zz',
        'platformVersion': '1.4.5',
        'appVersion': '1.4.5',
        'platformCode': 'pc',
        'token': '',
        'uuid': 'x',
        'zoneId': 'Asia/Shanghai',
    }


class KessbetHttp:
    def __init__(self, proxy: str | None = None, timeout: float | None = None):
        proxy = proxy or config.PROXY
        proxies = {'https': proxy, 'http': proxy} if proxy else None
        self.session = creq.Session(impersonate='chrome', proxies=proxies)
        self.timeout = timeout or config.HTTP_TIMEOUT

    # ---------- 底层 ----------

    def _post_result(self, action: str, body: dict) -> dict:
        """HTTP 边界的结构化结果，保留真实响应文本、状态码和请求信封。

        不抛异常（Forbidden 也作为 error_kind 返回），让调用方能区分
        transport_error / protocol_error / ok_empty。`_post()` 负责保持
        旧的抛异常接口。
        """
        payload = encrypt({'header': _header(action), 'body': body})
        request = {
            'action': action,
            'body': body,
            'url': API,
            'method': 'POST',
        }
        result = {
            'ok': False, 'data': None, 'raw_body': None,
            'response_status': None, 'response_headers': {},
            'error': None, 'error_kind': None,
            'request': request, 'platform_ts': None,
            'received_at': None,
        }
        last_err = None
        for attempt in range(config.HTTP_RETRIES + 1):
            try:
                r = self.session.post(API, data=payload, headers=HEADERS,
                                      timeout=self.timeout)
                result['response_status'] = r.status_code
                result['raw_body'] = r.text
                result['response_headers'] = {
                    'content-type': r.headers.get('content-type'),
                    'date': r.headers.get('date'),
                }
                result['received_at'] = now_beijing()
                if r.status_code == 403:
                    result['error_kind'] = 'forbidden'
                    result['error'] = 'Cloudflare 403 — 检查代理'
                    return result
                r.raise_for_status()
                try:
                    j = r.json()
                except Exception as e:
                    result['error_kind'] = 'protocol'
                    result['error'] = f'bad_json: {e}'
                    return result
                if not isinstance(j, dict) or j.get('code') != 1:
                    result['error_kind'] = 'protocol'
                    code = j.get('code') if isinstance(j, dict) else None
                    msg = j.get('msg') if isinstance(j, dict) else None
                    result['error'] = f'code={code} msg={msg}'
                    if isinstance(j, dict):
                        result['platform_ts'] = j.get('sct')
                    return result
                result['ok'] = True
                result['data'] = j.get('data')
                result['platform_ts'] = j.get('sct')
                return result
            except ForbiddenError:
                raise
            except Exception as e:
                last_err = e
                result['error_kind'] = 'transport'
                result['error'] = str(e)
                if attempt < config.HTTP_RETRIES:
                    time.sleep(1.0 * (attempt + 1))
        if last_err is not None:
            result['error'] = str(last_err)
        return result

    def _post(self, action: str, body: dict) -> dict:
        res = self._post_result(action, body)
        if res['ok']:
            return res['data']
        if res.get('error_kind') == 'forbidden':
            raise ForbiddenError(res.get('error') or 'Cloudflare 403')
        raise RuntimeError(res.get('error') or 'request failed')

    # ---------- 赛程 ----------

    def list_raw(self, tab_id: int) -> list[dict]:
        """拉某个 tabId 的赛程，内部处理游标分页。"""
        base = {'tabId': tab_id, 'playType': '', 'timeslot': 'All', 'leagues': []}
        seen: dict[str, dict] = {}
        last_lid = last_mid = ''

        for _ in range(config.MAX_PAGES):
            body = dict(base)
            if last_lid:
                body['lastLeagueId'] = last_lid
                body['lastMatchId'] = last_mid
            groups = (self._post(ACTION_LIST, body) or {}).get('leagueGroupList') or []
            if not groups:
                break
            new = 0
            for g in groups:
                for m in g.get('matchList') or []:
                    if m['matchId'] not in seen:
                        seen[m['matchId']] = m
                        new += 1
            if new == 0:
                break
            last_lid = str(groups[-1]['li'])
            last_mid = groups[-1]['matchList'][-1]['matchId']

        return list(seen.values())

    def list_matches(self, sport: str) -> list[Match]:
        """拉某运动的赛程，转成统一模型。"""
        tab_id = config.SPORTS[sport]
        out = []
        for raw in self.list_raw(tab_id):
            m = self._to_match(raw, sport)
            if m:
                out.append(m)
        return out

    # ---------- 滚球列表 ----------

    def live_list_raw(self, tab_id: int) -> list[dict]:
        """拉某运动**当前正在滚球**的比赛。返回原始记录。

        ## 为什么必须单独一个接口

        `ACTION=00252`（赛程）**只返回未开赛的比赛**。实测：
        123 场里 ms 全是 0，开赛时间已过的 0 场，翻页到第二页也一样。
        所以「靠赛前入库 + kickoff_time 判断进滚球窗口」这条路有个硬缺口：
        某场比赛如果在赛前任何一次赛程刷新时都没被拉到，开赛后平台
        也不会再告诉你它存在 —— 它就永久消失了。

        实测证据（2026-11-12）：截图里 `Club America vs Toluca` 在
        我们库里完全查不到，因为它从没进过 kessbet_match。

        `ACTION=00232` 是网页 Live 页用的接口，直接返回滚球比赛：
        有 ms（6/7/31）、mt、比分、角球，还带一份 `mari`（当前主盘口）。

        ## 参数

        网页实际发的是 `{'tabId':'1','playType':'1','sortBy':6,'specifiers':''}`。
        实测：
          - playType  取值 0~4 和空串都返回同样结果，无实际作用
          - sortBy    0 会漏（返回 2 场而不是 3 场），1~7 都是全的；
                      3 返回的是另一种结构（只有 leagueGroupList）
          - tabId     按运动区分，和赛前的 SPORTS 映射一致
          - 不需要分页，一次返回全部滚球场次

        sortBy 固定用 6 —— 那是网页默认值，实测返回完整。
        """
        body = {'tabId': str(tab_id), 'playType': '1',
                'sortBy': 6, 'specifiers': ''}
        data = self._post(ACTION_LIVE_LIST, body)
        if not isinstance(data, dict):
            return []
        return list(data.get('matchList') or [])

    def list_live_matches(self, sport: str) -> list[Match]:
        """拉某运动当前滚球的比赛，转成统一模型。"""
        tab_id = config.SPORTS[sport]
        out = []
        for raw in self.live_list_raw(tab_id):
            m = self._to_match(raw, sport)
            if m:
                out.append(m)
        return out

    @staticmethod
    def _to_match(raw: dict, sport: str) -> Match | None:
        try:
            ts = raw.get('mbtp')
            if not ts:
                return None
            kickoff = from_ts(ts)
            return Match(
                platform=config.PLATFORM,
                match_id=raw['matchId'],
                sport=sport,
                league=raw.get('ln') or '',
                country=raw.get('cn') or '',
                home_team=raw.get('htn') or '',
                away_team=raw.get('atn') or '',
                kickoff_time=kickoff,
                sportradar_id=raw.get('tmi') or '',
            )
        except (KeyError, TypeError, ValueError) as e:
            log.warning('bad match record: %s (%s)', raw.get('matchId'), e)
            return None

    # ---------- 单场盘口 ----------

    def fetch_raw_result(self, match_id: str) -> dict:
        """拉单场全部盘口，返回结构化结果（F03）。

        status:
          ok_nonempty    成功且有可解析盘口
          ok_empty       成功且明确返回空列表（不是网络失败）
          transport_error 超时/403/连接失败/服务端错误
          protocol_error  HTTP 成功但响应结构/业务码不对
          parsed_empty   原始市场非空，但全部锁定/无法解析
        """
        res = self._post_result(ACTION_MATCH, {'matchId': match_id})
        out = {
            'status': 'ok_nonempty', 'markets': [],
            'raw_body': res.get('raw_body'),
            'response_status': res.get('response_status'),
            'response_headers': res.get('response_headers') or {},
            'request': res.get('request'),
            'error': res.get('error'),
            'platform_ts': res.get('platform_ts'),
            'received_at': res.get('received_at'),
        }
        if not res.get('ok'):
            kind = res.get('error_kind')
            out['status'] = ('transport_error'
                             if kind in ('transport', 'forbidden')
                             else 'protocol_error')
            return out

        data = res.get('data')
        if not isinstance(data, list):
            out['status'] = 'protocol_error'
            out['error'] = 'response data is not a list'
            return out
        out['markets'] = data
        if not data:
            out['status'] = 'ok_empty'
            return out

        usable = 0
        for mk in data:
            if not isinstance(mk, dict):
                continue
            name, sels = parse_mari(mk.get('mari') or '')
            if name and sels:
                usable += 1
        out['status'] = 'ok_nonempty' if usable else 'parsed_empty'
        return out

    def fetch_raw(self, match_id: str) -> list[dict]:
        """拉单场全部盘口，返回原始列表（兼容旧调用）。"""
        return self.fetch_raw_result(match_id)['markets']

    def fetch_state(self, match_id: str) -> dict | None:
        """拉单场比赛状态（比分、时间、红黄牌）。

        这是滚球分析的**必要**输入 —— 盘口接口(0015)只给赔率，
        不给「现在几比几」，而滚球判定的状态空间必须从当前比分出发。

        返回的原始字段（实测 2026-11-10）：
            ms    比赛状态：0=未开始 7=进行中 100=已结束
            mt    已进行时间，格式 'MM:SS'，如 '65:54'
            hmp   主队进球，字符串
            amp   客队进球，字符串
            hyc   主队黄牌      ayc  客队黄牌
            hrc   主队红牌      arc  客队红牌
            hck   主队角球      ack  客队角球
            msScoreList  分阶段比分记录，不是当前比分，别拿它当比分用
            tmi   Sportradar ID    tsi  运动编号
            mbt/mdt  开赛时间/日期   sct  服务端时间戳(毫秒)

        比赛结束后接口返回 None，调用方要处理。

        注意 `mt` 的可靠性：它是平台抓的现场数据，可能有延迟。
        比分同理 —— 存在从进球到平台更新的窗口期。滚球套利如果
        依赖这个窗口，需要先实测延迟有多大。
        """
        data = self._post(ACTION_STATE, {'matchId': match_id})
        return data if isinstance(data, dict) else None

    def fetch_odds(self, match_id: str, home: str = '', away: str = '') -> list:
        """拉单场全部盘口，解析成 Selection 列表。

        home / away 用于拆分队名盘口（如 'Arsenal Total' -> side='H'）。
        """
        return self.fetch_odds_with_raw(match_id, home, away)[0]

    def fetch_odds_result(self, match_id: str, home: str = '',
                          away: str = '') -> tuple[list, dict, list]:
        """同一次请求返回 (Selection 列表, 结构化 fetch_result, 身份列表)。

        身份列表用于输入指纹：每个选项带原始 mari、规范键、赔率和市场 ID。
        """
        from common.models import Selection
        from collect.kessbet.parse import split_team, parse_market_id

        res = self.fetch_raw_result(match_id)
        out = []
        identity = []
        for mk in res.get('markets') or []:
            mari = mk.get('mari') or ''
            name, sels = parse_mari(mari)
            if not name:
                continue
            side, base = split_team(name, home, away)
            market_id = parse_market_id(mari)
            for line, label, odds in sels:
                out.append(Selection(base, side, line, label, odds))
                identity.append({
                    'mari': mari, 'market': base, 'side': side,
                    'line': line, 'label': label, 'odds': odds,
                    'market_id': market_id, 'locked': False,
                })
        return out, res, identity

    def fetch_odds_with_raw(self, match_id: str, home: str = '',
                            away: str = '') -> tuple[list, list]:
        """兼容旧调用：返回 (Selection 列表, 原始盘口列表)。"""
        out, res, _identity = self.fetch_odds_result(match_id, home, away)
        return out, res.get('markets') or []
