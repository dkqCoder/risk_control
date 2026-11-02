# KessBet 赔率抓取

抓取 <https://www.kessbet.com/soccer> 的赛程与全盘口赔率。

## 目录结构

```
KessBet/
├── kessbet_client.py     采集客户端（加密 / 拉赛程 / 拉单场盘口）
├── kessbet_arb.py        盘口解析 + 状态空间映射 + ILP 求解
├── data/                 全部数据（详见 data/README.md）
├── tools/                辅助工具（详见 tools/README.md）
└── test/                 pytest 回归测试
```

```bash
py -m pytest test/ --ignore=test/scratch -v
```

## 数据文件

全部数据在 `data/` 下，详细说明见 [data/README.md](data/README.md)。

| 文件 | 说明 |
|---|---|
| `data/kessbet_soccer_raw.json` | 赛程原始 JSON（195 场，含 1X2 主盘） |
| `data/kessbet_soccer_odds.csv` | 由上面整理出的简易赛程表 |
| `data/match_ids.json` | 195 场的 `matchId` 列表 |
| `data/odds/raw_odds/{matchId}.json` | 每场完整盘口原始 JSON（约 30KB/场） |
| `data/kessbet_all_odds.csv` | **全盘口长表**，74409 行、349 种盘口 |

`kessbet_all_odds.csv` 字段：

| 字段 | 含义 |
|---|---|
| 日期 / 开赛 / 联赛 / 主队 / 客队 | 比赛信息 |
| 盘口 | 盘口名，如 `Total`、`Handicap`、`Correct Score` |
| 归属 | 队名专属盘口时填 `主队`/`客队` 的队名，通用盘口为空 |
| 盘线 | 让球值（`0:1`）或大小球线（`2.5`），无则为空 |
| 选项 | `1` / `X` / `2` / `over` / `under` / `yes` 等 |
| 赔率 | 十进制赔率 |
| matchId | 内部比赛 ID |

## 重新抓取的步骤

现在纯 Python 即可完成，不需要浏览器。

### 1. 拉赛程

```bash
py kessbet_client.py list --today -o data/snapshots/matches.json
```

需要境外出口（见"技术说明"里的代理配置）。

### 2. 拉单场盘口

```bash
py kessbet_client.py odds <matchId> -o data/odds/raw_odds/<matchId>.json
```

### 3. 套利扫描

```bash
py kessbet_arb.py                # 读 data/ 全量扫一遍
py tools/kessbet_show_calc.py    # 单场逐步演示
```

### 4. 导出 CSV（可选）

```bash
py tools/kessbet_parse.py        # data/odds/raw_odds/ → data/kessbet_all_odds.csv
```

## 技术说明

### 请求加密

单场盘口接口 `POST https://api.kessbet.com/cpop/ots/unct/public/handler`
的请求体是加密的：

```
AES-192-CBC
key = QipVAlnxoMjeQ4I47ne7kWDk
IV  = 0000000000000000
明文 = {"header":{"action":"0015",...},"body":{"matchId":"..."}}
```

加密后再 `base64` + `urlencode`，拼成 `request=...`。加密实现见 `kessbet_encrypt.py`。

`action` 取值：`0015` = 单场全盘口。

### 为什么必须走浏览器

`api.kessbet.com` 和 `www.kessbet.com` 都挂在 Cloudflare 后面，直接 `curl`
（即使换成浏览器 UA）一律返回 **403 "Attention Required!"**。只有真实浏览器
能通过指纹/JS 挑战，所以请求必须由页面内的 `fetch` 发出。

`kessbet_bridge.py` 的作用就在于此：它只负责两件本地的事——下发加密好的请求体、
接收抓取结果落盘，实际的网络请求由浏览器完成。

### 依赖

```bash
pip install pycryptodome
```

### 解析格式

`mari` 字段格式：

```
{market_id}|{matchId},1|{line},{selId}/1={selId},{odds},1,{label}@{selId},{odds},1,{label}^...
```

- `^` 分隔不同盘线，`@` 分隔同一盘线的多个选项
- 盘线只在每个盘线的第一个选项上出现，解析时需要向下填充（`fill_forward`）
- 队名专属盘口在盘口名前带队名（如 `Bournemouth Total`），解析时按主客队名归一化

## 注意

- 这是合规抓取公开赔率数据，仅用于数据分析。请自行确认目标站点的服务条款。
- 赔率实时变动，每次抓取都是当时快照。
