# KessBet 数据抓取与套利分析 · 交接文档

> 最后更新：2026-10-23（2026-11-02 迁移目录）
> 目录：`G:\worker-space\risk_control\KessBet`
>
> ⚠️ 本目录已从 `H:\WorkSpace\python\sssj_algorithm\KessBet` 迁出，
> 现在归属 `risk_control` 仓库。原仓库里从未跟踪过本目录（`git ls-files KessBet` 为 0 项），
> 因此这次是**搬移**而不是从历史里恢复。

---

## 一、这个目录是干什么的

抓取 [kessbet.com](https://www.kessbet.com/soccer)（肯尼亚博彩网站）的足球赛程和赔率，
并用**整数规划**判断同一场比赛内部是否存在跨玩法无损套利。

**核心结论（截至 2026-10-23）**：
扫描了 195 场比赛、349 种盘口、74,409 条赔率，**没有任何一场存在组合返奖 >1 的机会**。
最小成本稳定在 1.0238 ~ 1.0780（中位 1.0398），这就是这家公司的抽水水平。

---

## 二、快速开始

```bash
cd G:\worker-space\risk_control\KessBet

# 依赖（已装好的可跳过）
pip install curl_cffi pycryptodome scipy numpy pymysql

# 拉今天的赛程
py kessbet_client.py list --today

# 拉某场全部盘口
py kessbet_client.py odds 245131096595927104 -o data/snapshots/_api_snapshot.json

# 套利扫描（输出最小覆盖成本）
py kessbet_arb.py

# 单场逐步计算演示
py tools/kessbet_show_calc.py 245131079416058509

# 回归测试
py -m pytest test/ --ignore=test/scratch -v
```

---

## 三、网络前提（最关键的一条）

**中国大陆 IP 会被 Cloudflare 直接 403 拦截**（返回 `Attention Required! | Cloudflare`）。

实测结果：

| 方式 | 结果 |
|---|---|
| curl 直连（换 UA / 完整浏览器头） | 403 |
| curl_cffi 模拟 Chrome/Safari/Edge 指纹，直连 | 403 |
| 走代理 `127.0.0.1:7897` | **200** |

**原因**：浏览器自动走系统代理（出口 = 东京 AWS `3.115.13.53`），
而 curl / Python 默认直连（出口 = 南京电信 `221.224.91.50`），后者被防火墙规则拉黑。

所以**必须有境外出口**。当前配置：

```
系统代理: 127.0.0.1:7897  (Clash / V2Ray 类工具)
```

可用 `--proxy` 参数或 `KESSBET_PROXY` 环境变量覆盖：

```bash
py kessbet_client.py list --proxy http://127.0.0.1:7890
set KESSBET_PROXY=http://10.0.0.5:8080
```

> 换节点 / 换 VPS 都行，只要能拿到 200。如果报 403，先检查代理是否开着。

---

## 四、接口协议

### 4.1 基本信息

```
POST https://api.kessbet.com/cpop/ots/unct/public/handler
Content-Type: application/x-www-form-urlencoded;charset=UTF-8

请求体: request=<urlencode(base64(AES-192-CBC(JSON)))>
响应:   明文 JSON（不需要解密）
```

### 4.2 加密参数

```python
KEY = b'QipVAlnxoMjeQ4I47ne7kWDk'   # 24 字节 = AES-192
IV  = b'0000000000000000'
mode = CBC, padding = PKCS7
```

加密后 base64、urlencode，`+` 要替换成 `%2B`。

**这个 key 是前端 JS 里硬编码的**，不会过期。`uuid`、Cookie、`token` 都不是必需的
（实测空字符串、随机 UUID 都能拿到数据）。**没有任何需要定时刷新的凭据。**

### 4.3 请求头结构

```json
{
  "header": {
    "url": "//api.kessbet.com/cpop/ots/unct/public/handler",
    "method": "post",
    "action": "0015",
    "isApp": 0, "brand": "KYA", "cmdName": "h5_zz",
    "platformVersion": "1.4.5", "appVersion": "1.4.5",
    "platformCode": "pc", "token": "", "uuid": "x",
    "zoneId": "Asia/Shanghai"
  },
  "body": { "matchId": "245131096595927104" }
}
```

实测 `brand` / `version` / `cmdName` / `zoneId` 删掉也能工作，但建议保留。

### 4.4 有效 action

| action | 用途 | body 参数 |
|---|---|---|
| `00252` | **赛程列表** | `{tabId:1, playType:'', timeslot:'All', leagues:[]}` |
| `00251` | 场次统计（不是列表） | 同上 |
| `0015` | **单场全盘口** | `{matchId:'...'}` |

### 4.5 赛程列表的两个坑

**坑 1：`timeslot` 不传只返回每个联赛的下一场**

```python
{'tabId':1}                        →  16 场（每个联赛 1 场）
{'tabId':1,'timeslot':'All'}       →  198 场
```

**坑 2：必须游标翻页，否则只拿第一页**

```python
# 用上一页最后一个联赛的 li 和最后一场的 matchId 作为游标
{'lastLeagueId': '87', 'lastMatchId': '245130302026977298'}
```

我实测共 **18 页**，累计 **1567 场**。

`kessbet_client.py` 已经封装好这两点，直接调 `fetch_schedule()` 即可。

---

## 五、数据格式

### 5.1 赛程字段（`matchList` 里每场）

| 字段 | 含义 |
|---|---|
| `matchId` | 比赛 ID，拉盘口用 |
| `htn` / `atn` | 主队 / 客队 |
| `ln` | 联赛名 |
| `mdt` | 日期 `23/10` |
| `mbt` | 时间 `22:00` |
| `mbtp` | Unix 毫秒时间戳（**用这个最可靠**） |
| `mari` | 该场首个盘口（1x2）的原始串 |

⚠️ **时间陷阱**：`mbt` / `mdt` 是**肯尼亚时间（UTC+3）**，`mbtp` 是正确的时间戳。
用 `datetime.fromtimestamp(mbtp/1000)` 转换即可得到本地时间。

**做日期过滤必须用 `mbtp`，不能用 `mdt`。** 实测同一天，按 `mdt` 筛出 263 场，
按 `mbtp` 换本地日期只筛出 112 场（两者交集 112）——直接用 `mdt` 会混进大量
本地已经是次日/前日的比赛。`kessbet_client.filter_today()` 已按 `mbtp` 实现。

### 5.2 盘口字段 `mari`

这是所有赔率的载体，格式：

```
{marketId}|{matchId},1|{盘线},{selId}/1={selId},{赔率},1,{标签}@{selId},{赔率},1,{标签}^...
```

- `^` 分隔不同**盘线**
- `@` 分隔同一盘线的多个**选项**
- 末尾 `|盘口名` 是市场名
- **盘线只出现在每条盘线的第一个选项上**，后续选项需要向下填充

解析函数：`kessbet_arb.parse_mari(mari)` → `(盘口名, [(盘线, 选项, 赔率), ...])`

---

## 六、套利判定算法

### 6.1 核心思想

把每个盘口选项**映射到比赛结果的"状态空间"**，然后求一组选项：
**覆盖所有可能结果、且 `Σ(1/赔率)` 最小**。

- `Σ(1/赔率) < 1` → **存在套利**（不论结果如何都赚钱）
- `Σ(1/赔率) > 1` → 无套利，差额就是抽水

### 6.2 状态空间

当前建模（`kessbet_arb.py`）：

```
(h1, a1, h2, a2)  半场主/客进球, 全场主/客进球
h1,a1 ∈ 0..5
h2,a2 ∈ 0..9
约束: h2 >= h1 且 a2 >= a1  （全场进球不可能少于半场）
合法状态数: 2025
```

### 6.3 计算流程

1. **收集**：每个选项 → 一个 2025 位的 bitmask（`covers()`）
2. **求解**：整数规划
   ```
   min  Σ (1/oᵢ) · xᵢ          xᵢ ∈ {0,1}
   s.t. 对每个状态 s: Σ xᵢ ≥ 1  （所有覆盖 s 的 i）
   ```
3. **验证**：选中项的 mask 取并集，应等于全集
4. **判定**：结果 <1 套利，>1 亏损额 = 结果-1

### 6.4 必须用精确解，不能用贪心

我一开始用贪心（每次选"每格成本最低"），算出 1.35，比真实值高 0.33。
**贪心在这个问题上会严重高估**，因为大覆盖面会挤掉后续更优的小选项。

已用穷举（≤3 项全组合）交叉验证 ILP 结果，两者完全一致。

---

## 七、已验证的结论

### 7.1 静态扫描结果

| 建模范围 | 场次 | 最低成本 | 中位 | <1 的场次 |
|---|---|---|---|---|
| 仅全场比分 | 195 | 1.0238 | 1.0398 | **0** |
| 半场 + 全场 | 195 | 1.0238 | 1.0396 | **0** |

**加入半场维度没有改善**，因为半场盘抽水更高，永远不会被选入最优解。

### 7.2 典型最优组合

| 比赛 | 成本 | 组合 |
|---|---|---|
| Brest vs Nice | 1.0238 | `Total 0.5 over` (1.05) + `1x2&BTTS X&no` (13.0) |
| Napoli vs Roma | 1.0253 | `1x2` 三项 |
| Arsenal vs Everton | 1.0275 | `1x2` 三项 |

**注意**：多数情况下最优解就是 1X2 三项本身——它是这家公司抽水最低的盘口。

### 7.3 为什么单家内部不会有套利

最小覆盖成本 ≈ 公司抽水，这是数学必然：

```
若所有赔率统一缩放 1/m，则全覆盖成本 = m
```

实测两者高度重合：

```
1X2 单盘抽水:      1.0253 ~ 1.0843  (中位 1.0459)
全盘最小覆盖成本:  1.0238 ~ 1.0780  (中位 1.0398)
```

**结论：正常定价下，单家博彩公司内部不可能出现套利。**
出现套利只可能是调价错误（通常几十秒，且有权作废）。

---

## 八、文件清单

### 8.1 可执行脚本

| 文件 | 作用 |
|---|---|
| `kessbet_client.py` | **主入口**。拉赛程 / 拉单场盘口 |
| `kessbet_arb.py` | 套利核心：状态映射 + ILP 求解 |
| `tools/kessbet_show_calc.py` | 单场逐步计算演示（教学/验证用） |
| `tools/kessbet_compare.py` | 生成官网页面 vs 接口的对照 HTML |
| `tools/kessbet_parse.py` | 早期版盘口解析 → CSV |
| `test/test_kessbet_arb.py` | 解析/覆盖语义的回归测试（12 项） |
| `test/test_kessbet_client.py` | 日期过滤与排序的回归测试（3 项） |

> 目录整理（2026-10-26）：`tools/` 下是一次性辅助工具，不参与生产流程。
> `kessbet_bridge.py` / `kessbet_scrape.js` / `kessbet_encrypt.py` 已删除。

### 8.2 数据文件

全部数据在 `data/` 下，详见 [data/README.md](data/README.md)。

| 文件 | 说明 |
|---|---|
| `data/kessbet_soccer_raw.json` | 赛程原始数据（195 场，旧快照） |
| `data/kessbet_soccer_odds.csv` | 简易 1X2 表 |
| `data/kessbet_all_odds.csv` | 全盘口长表（74,409 行、349 种盘口） |
| `data/odds/raw_odds/` | 195 场原始盘口 JSON |
| `data/snapshots/_all_matches.json` | 完整赛程（1567 场，2026-10-23 快照） |
| `data/snapshots/_official_page.json` | 官网页面的盘口抓取结果（对照用） |
| `data/snapshots/_api_snapshot.json` | 同一场的接口快照（**测试依赖**） |
| `tools/odds_compare.html` | **赔率对照页面**（浏览器打开） |

### 8.3 其他归档

- `data/js/` — 前端 bundle 分析产物（`_idx.js`、`_idx2.js`、`_chunks/`）
- `data/snapshots/` — 其余接口快照（`_today_list.json`、`_today_match.json`、
  `_verify_arsenal.json`、`_all_list.json`）
- `test/scratch/` — 多运动探测的一次性脚本

---

## 九、数据校验

已做过的验证（`odds_compare.html`）：

| 项 | 结果 |
|---|---|
| 官网页面盘口 | 81 个 |
| 接口盘口 | 87 个（多 6 个，页面折叠未渲染） |
| 逐项比对 | 269 项 |
| 一致 | **269（100%）** |
| 不一致 | **0** |

失败的验证（已修复）：表头对齐 bug 导致 31 项假报警。

---

## 十、已知限制

1. **只建模了全场比分 + 半场比分**
   未覆盖：角球盘、红黄牌盘、进球时间区间。这些不在比分空间里。
   如果要纳入，需要扩展状态空间。

2. **半场网格只到 0..5，全场到 0..9**
   单队进球 ≥10 的概率可忽略，但如果要严格，需要加尾格。

3. **比分盘的 `other` 必须处理**
   `Correct Score` / `1st Half - Correct Score` 最后有个 `other` 选项（覆盖所有未列出的比分）。
   代码里用 `FULL & ~named` 计算它的覆盖范围。漏掉它会导致误判。

   ⚠️ 具名比分（`0:0`、`1:0`…）**有意不进候选池**，只有 `other` 补集进。
   因为单个精确比分的 `1/赔率` 很大，进池只会拖慢求解，不会进最优解。

4. **`Halftime/Fulltime Correct Score` 尚未建模**
   46 个选项/场，是未建模盘口里体量最大的。但标签语义有歧义
   （`0:0 4+` 既可能是"全场主队 0:0→4+"也可能是"全场总进球 4+"），
   在拿到官方说明前**不要猜**——猜错会算出假套利。

5. **赔率实时变动**
   任何结论都是某一时刻的快照。

---

## 十一、下一步（如果继续做）

### 方案 A：监控工具

每 N 分钟扫描一次，发现最小成本 <1 就告警。

**必须先解决的现实问题**（实测，2026-10-23）：

| 项 | 数值 |
|---|---|
| 全部比赛 | 1567 场 |
| 今日比赛 | 本地日期 112 场 / 肯尼亚 mdt 263 场 |
| 单场盘口大小 | ~30 KB |
| **单场 `collect`+`solve` 耗时** | **~1.2 秒** |
| 候选池 | 平均 208 个选项/场 |

- 扫今日（本地 112 场）：约 **2.2 分钟** / 轮
- 扫全量 1567 场：约 **32 分钟** / 轮

> 早期文档写的"0.3 秒/场、80 秒/轮"是低估的，实际慢 4 倍。轮询方案要按上表排期。

**告警阈值不能用固定数字**。因为最小成本 ≈ 抽水，正常就在 1.02~1.08 之间，
设 1.05 会让 60% 的比赛持续告警。应该用**相对自身基线的下降**作为信号。

### 方案 B：扩展状态空间

加入角球 / 红黄牌维度，覆盖目前未建模的盘口。

### 方案 C：多家比价

真正的套利机会基本都在**不同博彩公司之间**。单家内部几乎不可能出现。

---

## 十二、经验教训（避免重复踩坑）

1. **TLS 指纹不是关键，出口 IP 才是**。花时间试 curl_cffi 各种 impersonate 全是白费，
   真正原因是没走系统代理。

2. **贪心算法在这里不可用**。必须用 ILP 精确求解。

3. **解析 `mari` 时盘线要向下填充**。否则后续选项的盘线全是空的，
   会把不同盘线的选项混在一起。

4. **盘口名要先判断单队后缀再判断通用分支**。
   `Alaves Multigoals` 是"Alaves 进 N 球"，不是"全场进 N 球"。
   分支顺序写反会导致覆盖范围被放大 100 倍，算出 0.38 的假套利。

5. **列表接口的两个参数**（`timeslot:All` + 游标翻页）缺一个就会少拿大量数据。

6. **`covers()` 收的是"基础盘口名"，不是完整盘口名**。
   `collect()` 里必须先 `split_team()` 再传 `base`。
   直接传 `Arsenal Total` 会返回 `None`，队名盘口静默丢失 30~40 个选项/场。

7. **半场前缀要保留在基础名里**。
   `split_team('1st Half - Arsenal Total')` 必须返回 `('H', '1st Half - Total')`，
   而不是 `('', '1st Half - Arsenal Total')`——否则既认不出队名，也认不出半场。

8. **`1/X`、`1/2`、`X/2` 和 `1X`、`12`、`X2` 是两种写法，都要认**。
   实际接口 `Double Chance & Total` 用的是斜杠写法，只写字典里的一种会静默漏掉整组。

9. **假套利的第一嫌疑是 yes/no 弄反**。
   建 `Draw Or Over`、`Both Halves Over` 这类"名字里带方向"的盘口时，
   `no` 必须是 `FULL & ~yes`，不能照抄 `yes` 的谓词。

---

## 十三、本次修复记录（2026-10-23）

修复前 `collect()` 每场只收约 180 个选项，修复后 208 个。**全量 195 场复跑，
成本分布完全不变**（min 1.0238 / median 1.0396 / max 1.0780 / 0 场 <1），
说明之前漏掉的盘口都不影响最优解，结论依然成立。

| 问题 | 位置 | 影响 |
|---|---|---|
| `collect` 把完整盘口名传给 `covers` | `kessbet_arb.collect` | 队名盘口全部丢失 |
| `1st Half - Correct Score` 分支条件写错 | `kessbet_arb.collect` | 整盘连同 other 补集丢失 |
| `split_team` 不处理半场前缀 | `kessbet_arb.split_team` | 半场+队名盘口无解 |
| `Double Chance & Total` 只认 `1X` 写法 | `kessbet_arb.covers` | `1/X` 一组丢失 |
| `--today` 用 `mdt`（肯尼亚日期）过滤 | `kessbet_client.cmd_list` | 筛出 263 场而非本地 112 场 |
| 性能数字低估 4 倍 | 本文档 | 轮询方案排期失真 |

新增 `test_kessbet_arb.py`、`test_kessbet_client.py` 共 15 项回归测试。

---

## 十四、合规提醒

抓取的是公开赔率数据，仅用于个人数据分析。请自行确认目标站点的服务条款。
博彩有风险，套利机会在单家内部极罕见，且可能被判定为定价错误而作废。
