# tools/ — 辅助工具（非生产代码）

这些脚本**不参与生产采集流程**，是开发期的一次性工具。保留是为了随时能复现当时的验证。

| 文件 | 作用 | 何时用 |
|---|---|---|
| `kessbet_show_calc.py` | 在单场数据上逐步打印套利计算过程（状态映射 → 候选池 → ILP → 覆盖验证） | 怀疑计算结果有问题，想逐步骤核对时 |
| `kessbet_compare.py` | 生成官网页盘口 vs 接口数据的对照 HTML | 怀疑抓取漏盘口时 |
| `kessbet_parse.py` | `data/odds/raw_odds/` → 扁平 CSV | 需要把原始数据导出成表格时 |

## 使用

```bash
# 从项目根目录运行
py tools/kessbet_show_calc.py                    # 默认演示 Ipswich vs Nottingham
py tools/kessbet_compare.py                      # 输出 tools/odds_compare.html
py tools/kessbet_parse.py                        # 生成 data/kessbet_all_odds.csv
py tools/kessbet_parse.py --prepare              # 只生成 match_ids.json
```

## 注意

**这三个脚本都有硬编码。**

- `kessbet_show_calc.py`：`HOME, AWAY = 'Ipswich', 'Nottingham'`，且固定读 `data/snapshots/_today_match.json`
- `kessbet_compare.py`：`HOME, AWAY = 'Arsenal', 'Everton'`，固定读 `_api_snapshot.json` + `_official_page.json`

换比赛要改代码。生产代码不要依赖它们。

## 与生产代码的关系

| 工具 | 依赖生产模块 | 说明 |
|---|---|---|
| `kessbet_show_calc.py` | ✅ 用 `kessbet_arb` 的 `covers`/`collect`/`solve` | 重构 `kessbet_arb` 时，它会跟着失效，需要同步更新 |
| `kessbet_compare.py` | ✅ 用 `kessbet_arb.parse_mari` | 同上 |
| `kessbet_parse.py` | ❌ 完全独立 | 自带一份 `parse_mari` 实现，和 `kessbet_arb` 的略有差异 |

⚠️ `kessbet_parse.py` 的 `parse_mari` 会做 `fill_forward`（盘线向下填充），而 `kessbet_arb.parse_mari` 不做填充、由调用方处理。
两者**不是同一份实现**，输出不要混用。

## 已删除

| 文件 | 删除原因 |
|---|---|
| `kessbet_bridge.py` | 浏览器桥接方案，被代理直连取代，HANDOFF 已标废弃 |
| `kessbet_scrape.js` | 配合上面的浏览器脚本，同样废弃 |
| `kessbet_encrypt.py` | 加密逻辑被 `kessbet_client.encrypt()` 覆盖，且依赖 `openssl` 命令行 |

---
归档时间：2026-10-26
