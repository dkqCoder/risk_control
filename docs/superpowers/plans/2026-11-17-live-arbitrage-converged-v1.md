# 滚球套利收敛开发 v1 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把“完整功能设计”收敛成可验收的 v1：固定快照、状态闭环、白名单无界验证、E1 证据包、影子模式；不实现 E2 页面逐项核验、E3 成交和自动下注。

**Architecture:** 采集侧把“同一次请求”的状态前/原始响应/状态后绑定成不可变快照并发布当前指针；分析侧只按 `snapshot_id` 读取，输出 `current_status` 和验证结果；命中时写不可变证据包。E1 证明“平台接口返回过该报价”，不证明页面展示或可下注。

**Tech Stack:** Python 3.13、pytest、FastAPI、pymysql/MySQL、原生 HTML/JS、Chrome CDP（仅辅助截图）。

**Spec:** `KessBet/docs/live-arbitrage-complete-functional-design-20261117.md`

## Convergence decisions

**v1 做:**

- 不可变快照（请求/响应信封、原始 `mari`、规范选项、状态前后、版本、指纹）。
- 状态机：`waiting / fetching / invalid_input / ready / analyzing / no_candidate / unverifiable / verified_candidate / expired / invalidated`。
- 白名单认证：全场 1x2、Double Chance、BTTS、半球线全场/球队 Total；`other` 继续隔离。
- 独立无界验证 + 金额取整后的最低返还。
- E1 证据包（request/response/normalized/state/analysis/verification + manifest/SHA-256）。
- 影子模式：算、留证、不写正式命中、不发告警。

**v1 不做（明确延后）:**

- E2 页面网络响应 + DOM 逐项核验；All 截图只作辅助。
- E3 实际成交/受理记录。
- 比分盘、半场盘、让球、奇偶、组合盘、退款/半赢半输盘。
- 自动下注、多平台、提高 `G`、关闭验证器绕过。

## Global Constraints

- 将“运行分析”和“正式发布”分开：`LIVE_ANALYZE_ENABLED` 控制是否运行分析；新增 `LIVE_SHADOW_MODE` 控制分析结果仅进入影子记录。影子验收时 `LIVE_ANALYZE_ENABLED=True, LIVE_SHADOW_MODE=True`；正式发布仅在验收后设 `LIVE_SHADOW_MODE=False`。部署前仍保持当前 `LIVE_ANALYZE_ENABLED=False`。
- 正式写库入口统一检查两个开关；影子记录不得写 `kessbet_arbitrage_snapshot` 或发送正式机会告警。
- 只有 `analysis_status='verified_candidate'` 且证据包写成功才可写正式命中；影子模式写 `shadow_candidate`。
- 结果表 `cost` 可为 NULL；任何页面/API 不得对 NULL 调 `float()`，不得把历史 `cost<1` 当当前机会。
- 报价年龄超过 `LIVE_ODDS_MAX_AGE_SEC`（90 秒）必须 `expired`。
- 证据包不得包含 Cookie、Authorization、账号等敏感信息。
- 新增市场必须先有结算函数、完备性论证和正反测试，才进入白名单。
- 本计划所有 `Run:` 命令均从仓库根 `risk_control/` 执行，使用 `KessBet/test/...`。Files 块同样为仓库根相对路径。若已进入 `KessBet/`，命令中的测试路径须改为 `test/...`。数据库测试使用现有测试库连接。

## Review Focus

- `ok_empty` / `transport_error` / `protocol_error` / `parsed_empty` 混淆：空响应被当成整场撤盘，或网络失败被当成空市场。
- 并发/乱序请求下 raw、规范 rows、状态错配：A 请求的 raw 配 B 请求的 rows。
- 取整后亏损：精确比例下盈利，按最小下注单位取整后某结果不足门槛。
- 指针未变但报价过期：无推送时页面仍显示旧机会。
- 状态/NULL 展示：`no_candidate`、`unverifiable`、`expired`、`invalidated` 被显示成“无套利”或继续展示旧成本。

---

### Task 1: 结构化 HTTP 结果（F03）

**Files:**
- Modify: `KessBet/collect/kessbet/http.py`、`KessBet/collect/kessbet/adapter.py`
- Modify: `KessBet/collect/live.py`
- Create: `KessBet/test/test_fetch_result.py`

**Interfaces:**
- First produces: `_post_result(action, body) -> dict`，在 HTTP 边界保存响应原文、状态码、必要的脱敏响应头、平台时间字段（若有）及异常类别；现有 `_post()` 可调用它并维持旧接口。不得从已解析的 `j['data']` 反推原始响应字节或 HTTP 状态。
- Produces: `KessbetHttp.fetch_raw_result(match_id: str) -> dict`，字段 `status` ∈ `ok_nonempty|ok_empty|transport_error|protocol_error|parsed_empty`、`markets: list[dict]`、`raw_body: str|None`、`response_status: int|None`、`request: dict`、`error: str|None`、`received_at: datetime`。`request` 包含 URL/action/body 和脱敏后必要请求头；`raw_body` 为同次实际响应文本，不是重序列化的 `markets`。
- Produces: `KessbetAdapter.fetch_raw_result(match_id: str) -> dict`。
- Keeps: `fetch_raw(match_id) -> list[dict]` 兼容旧调用。

- [ ] **Step 1: Write the failing test**

```python
def test_fetch_result_classifies_transport_error(monkeypatch):
    def boom(*a, **k): raise TimeoutError("timeout")
    monkeypatch.setattr(KessbetHttp, "_post_result", boom)
    r = KessbetHttp().fetch_raw_result("m1")
    assert r["status"] == "transport_error"
    assert r["markets"] == [] and r["error"]

def test_fetch_result_ok_empty_vs_nonempty(monkeypatch):
    monkeypatch.setattr(KessbetHttp, "_post_result", lambda *a, **k: response(data=[], text='{"code":1,"data":[]}'))
    assert KessbetHttp().fetch_raw_result("m1")["status"] == "ok_empty"
    monkeypatch.setattr(KessbetHttp, "_post_result", lambda *a, **k: response(data=[{"mari": valid_mari}], text=valid_response_text))
    assert KessbetHttp().fetch_raw_result("m1")["status"] == "ok_nonempty"

def test_fetch_result_protocol_error(monkeypatch):
    monkeypatch.setattr(KessbetHttp, "_post_result", lambda *a, **k: response(data={"bad": 1}, text=invalid_response_text))
    assert KessbetHttp().fetch_raw_result("m1")["status"] == "protocol_error"
```

上述 `response(...)`、`valid_mari` 和响应文本由测试文件内的 fixture 定义，使用真实合法 `mari` 样本；另用假 session 响应测试底层确实保留状态码、原文和接收时间。

- [ ] **Step 2: Run test to verify it fails**

Run: `py -m pytest KessBet/test/test_fetch_result.py -q`
Expected: FAIL with `fetch_raw_result` not defined.

- [ ] **Step 3: Implement structured classification**

In `http.py`, first add the HTTP-boundary result object, then classify its `data`: confirmed complete empty list → `ok_empty`; malformed envelope or non-list → `protocol_error`; timeout/connection/HTTP failure → `transport_error`（保留具体原因；403不得伪装成空市场）；list with no usable parsed selections → `parsed_empty`; otherwise → `ok_nonempty`. 保存真实 `raw_body` 与响应码、脱敏请求信封和时间。平台业务错误码单独保留原码并归入协议/服务端错误，不判为 `ok_empty`。只有确认接口 `0015` 的 `[]` 语义是完整空市场，才允许发布 `ok_empty`；否则先记 `unverifiable` 并重试。
`fetch_raw()` returns `result["markets"]`; `adapter.fetch_raw_result` delegates.

- [ ] **Step 4: Run tests and verify pass**

Run: `py -m pytest KessBet/test/test_fetch_result.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add KessBet/collect/kessbet/http.py KessBet/collect/kessbet/adapter.py KessBet/test/test_fetch_result.py
git commit -m "feat: classify live odds fetch results"
```

---

### Task 2: 快照信封、指纹与版本

**Files:**
- Modify: `KessBet/db/schema.sql`、`KessBet/db/dao.py`
- Create: `KessBet/db/migrate_live_envelope.sql`
- Create: `KessBet/common/fingerprint.py`
- Modify: `KessBet/collect/live.py`
- Create: `KessBet/test/test_live_snapshot_envelope.py`

**Interfaces:**
- Produces: `input_fingerprint(selections: list[dict], home: int, away: int, phase: str, ht: tuple|None, policy: str, rules_version: str) -> str`（SHA-256，不含接收时间）。每项 dict 必须含平台市场/选项 ID、可售状态、规范键和赔率；排序/序列化须稳定。现有五元组 `rows` 不含身份与锁定项，不能直接承担此接口。
- Modifies: `Dao.save_live_snapshot(...)` 接受 `request_json, response_json, response_hash, parser_version, filter_policy, input_fingerprint, phase, phase_source, ht_source`。
- Modifies: `Dao.get_live_snapshot(...)` 返回上述字段。

- [ ] **Step 1: Write failing tests**

```python
def test_fingerprint_ignores_receive_time():
    a = input_fingerprint(selections, 1, 0, "second_half", (1, 0), "p", "r1")
    b = input_fingerprint(selections, 1, 0, "second_half", (1, 0), "p", "r1")
    assert a == b and len(a) == 64

def test_fingerprint_changes_with_odds_or_phase():
    assert fp(selections, ...) != fp(selections_with_new_odds, ...)
    assert fp(selections, phase="first_half") != fp(selections, phase="second_half")
    assert fp(selections, ...) != fp(selections_with_locked_leg, ...)
```

- [ ] **Step 2: Run to verify fail**

Run: `py -m pytest KessBet/test/test_live_snapshot_envelope.py -q`
Expected: FAIL with import/attribute error.

- [ ] **Step 3: Add migration + writer**

Migration adds `request_json MEDIUMTEXT`, `response_json MEDIUMTEXT`, `response_hash CHAR(64)`, `parser_version VARCHAR(32)`, `input_fingerprint CHAR(64)`, `phase VARCHAR(16)`, `phase_source VARCHAR(16)`, `ht_source VARCHAR(16)`.
Collector stores `request_json` from the same fetch result, `response_json` with the complete original HTTP response and classification, `response_hash` = SHA-256 of the actually received raw response bytes, and `input_fingerprint`. 若底层只获得文本，明确编码及其限制。快照发布保持原子事务；响应/rows/状态按 `snapshot_id` 精确绑定。

在采集/调度入口实际使用指纹：与最近**已分析且仍有效**的输入指纹、规则版本一致时跳过重复求解；若只接收时间改变，可刷新观察时间和到期时间但不能改写已有不可变快照。比分、阶段、可售状态或分析策略改变必须形成新分析。首次有效快照必算。测试覆盖重复 WS、只变赔率、只变比分和策略升级。

每场采集加串行约束或条件发布：A 请求先发后回、B 请求后发先回时，A 不能把更旧响应覆盖成当前快照。只靠“事务提交时 generation 增加”不足以证明报价时间顺序正确。

- [ ] **Step 4: Run tests**

Run: `py -m pytest KessBet/test/test_live_snapshot_envelope.py KessBet/test/test_live_collector.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add KessBet/db/schema.sql KessBet/db/migrate_live_envelope.sql KessBet/db/dao.py KessBet/common/fingerprint.py KessBet/collect/live.py KessBet/test/test_live_snapshot_envelope.py
git commit -m "feat: bind snapshot envelope and input fingerprint"
```

---

### Task 3: 阶段与半场来源分级（F05）

**Files:**
- Create: `KessBet/common/phase.py`
- Modify: `KessBet/collect/live.py`、`KessBet/analyze/live/run.py`
- Create: `KessBet/test/test_phase.py`

**Interfaces:**
- Produces: `classify_phase(state: dict) -> tuple[str, str]`，phase ∈ `first_half|halftime|second_half|finished|unknown`，source ∈ `official_state|inferred`。
- Produces: `ht_source(phase: str) -> str`，v1 仅 `inferred_history|unknown`。
- 首期候选池和认证池均排除所有 `1st Half -`、`2nd Half -` 盘口，且不使用 `minute` 启用它们；`check_match` 在半场来源不是已核实的权威来源时传 `ht_home=None, ht_away=None`，不缩小结果域。

- [ ] **Step 1: Write failing tests**

```python
def test_phase_uses_status_and_clock():
    assert classify_phase({"ms": 31, "mt": "45:00"})[0] == "halftime"
    assert classify_phase({"ms": 100})[0] == "finished"
    assert classify_phase({"ms": 7})[0] == "unknown"
    # ms=7 加分钟只支持带来源标记的推断，不能冒充权威阶段。
    assert classify_phase({"ms": 7, "mt": "60:00"})[1] == "inferred"
```

- [ ] **Step 2: Run to verify fail**

Run: `py -m pytest KessBet/test/test_phase.py -q`
Expected: FAIL.

- [ ] **Step 3: Implement phase classification + use in analyzer**

Do not trust minute alone；平台状态码和时钟的阶段语义先用现场原始样本核实。`ms=7, mt=60:00` 不得直接标 `official_state`。未知或缺时钟时 `unknown`，保存 `phase/phase_source/ht_source`；校验 0≤半场比分≤当前全场比分。v1 的半场来源仍为启发式，分析不得用它缩小认证结果域。首期在进入候选求解之前就过滤半场盘；仅在以后取得权威来源并补结算验证后开放。

- [ ] **Step 4: Run tests**

Run: `py -m pytest KessBet/test/test_phase.py KessBet/test/test_live_result.py KessBet/test/test_live_run.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add KessBet/common/phase.py KessBet/collect/live.py KessBet/analyze/live/run.py KessBet/test/test_phase.py
git commit -m "feat: classify phase and stop using inferred half-time to narrow certification"
```

---

### Task 4: 金额取整后的最低返还验证

**Files:**
- Modify: `KessBet/analyze/live/verify.py`、`KessBet/analyze/live/result.py`、`KessBet/collect/config.py`
- Modify: `KessBet/test/test_live_result.py`
- Create: `KessBet/test/test_verify_rounding.py`

**Interfaces:**
- `verify_chosen(chosen, home, away, ht_home=None, ht_away=None, total_stake=10000.0, stake_unit=1.0, min_profit=0.0) -> dict`，返回 `status, min_payout, total_stake, actual_total, stakes, counterexample`。`min_profit` 是最低净收益率门槛；即使设为0，也必须严格正收益，另有数值容差。
- Config: `LIVE_CERT_TOTAL_STAKE=10000`、`LIVE_CERT_STAKE_UNIT=1`、`LIVE_CERT_MIN_PROFIT=0.0`。

- [ ] **Step 1: Write failing tests**

```python
def test_rounding_can_reject_marginal_candidate():
    # 精确比例盈利，但按 100 元单位取整后某腿为 0，反例出现
    v = verify_chosen(..., total_stake=100, stake_unit=100, min_profit=0.01)
    assert v["status"] == "counterexample_found"

def test_rounded_positive_case_verifies():
    v = verify_chosen(1x2_3.2x3, 0, 0, total_stake=300, stake_unit=1)
    assert v["status"] == "verified" and v["min_payout"] > 300
```

- [ ] **Step 2: Run to verify fail**

Run: `py -m pytest KessBet/test/test_verify_rounding.py -q`
Expected: FAIL (missing params).

- [ ] **Step 3: Implement rounded stakes**

用固定精度金额单位分配，明确预算处理：所有腿金额为 `stake_unit` 整数倍，合计**恰好等于**配置预算；若预算不能按单位分配，返回 `invalid_input`。采用确定性的余数分配规则并保存各腿实际金额。对每个代表终局计算实际 `payout`，要求最坏净收益 `payout - actual_total` 严格大于数值容差，且达到配置的最低收益率门槛；不使用 `>= total` 接受零利润。返回各腿金额、实际总投入、最差返还和反例，边界比较用 Decimal/整数分单位避免浮点误差。

- [ ] **Step 4: Run tests**

Run: `py -m pytest KessBet/test/test_verify_rounding.py KessBet/test/test_live_result.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add KessBet/analyze/live/verify.py KessBet/analyze/live/result.py KessBet/collect/config.py KessBet/test/test_verify_rounding.py KessBet/test/test_live_result.py
git commit -m "feat: verify rounded stakes and minimum payout"
```

---

### Task 5: 当前状态机与 API/页面一致

**Files:**
- Modify: `KessBet/analyze/live/run.py`、`KessBet/db/dao.py`、`KessBet/web/api.py`
- Modify: `KessBet/collect/run.py`、`KessBet/collect/live.py`、`KessBet/collect/kessbet/ws.py`（滚球撤盘信号）
- Modify: `KessBet/web/live.html`、`KessBet/web/live_detail.html`
- Modify: `KessBet/test/test_live_run.py`、`KessBet/test/test_live_page.py`

**Interfaces:**
- `write_live_result_cost(..., analysis_status, status_reason)`；`analysis_status` ∈ 状态机值。
- `web/api.py:_live_result_state(rc, cur_sid) -> (status, age)` 扩展为映射 `waiting|invalid_input|no_candidate|unverifiable|verified_candidate|expired|invalidated`。
- `/api/live`、`/api/live/{id}`、`/api/live/result-costs` 统一使用该函数；`cost=NULL` 安全序列化。
- WS `delete` 携带的市场 ID 与原始时间要保留并通知滚球采集器。能精确映射时使涉及该市场的机会失效；无法映射时保守使整场当前机会失效，排队全量重取。不得把“删1个市场”解释成整场成功空市场。

- [ ] **Step 1: Write failing tests**

```python
def test_result_costs_null_cost_is_json_safe(client):
    seed_result(cost=None, analysis_status="no_candidate")
    r = client.get("/api/live/result-costs")
    assert r.status_code == 200 and r.json()["rows"][0]["cost"] is None

def test_detail_shows_expired_not_arbitrage(client):
    seed_result(cost=0.8, snapshot_id="s1", analysis_status="valid",
                odds_updated=now_beijing()-timedelta(seconds=120))
    d = client.get(f"/api/live/{MID}").json()
    assert d["result"]["analysis_status"] == "expired"
    assert d["result"]["is_arbitrage"] is False
```

- [ ] **Step 2: Run to verify fail**

Run: `py -m pytest KessBet/test/test_live_page.py -q`
Expected: FAIL on the new assertions.

- [ ] **Step 3: Implement status mapping**

Persist status/reason on every analysis branch, including fetch failure, `ok_empty`、`parsed_empty`、WS delete and no-cover; compare current snapshot/generation/age in the API; return `analysis_status`, `status_reason`, `is_stale`, `quote_age_sec`. All three live APIs must treat `cost=NULL` safely and count only current verified candidates. Pages show the status text and never label `no_candidate` as “全市场无套利” or `unverifiable` as “无套利”. 失效时历史分析保留，当前指针/当前机会立即撤销。

- [ ] **Step 4: Run tests**

Run: `py -m pytest KessBet/test/test_live_page.py KessBet/test/test_live_run.py KessBet/test/test_live_dao.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add KessBet/analyze/live/run.py KessBet/db/dao.py KessBet/collect/run.py KessBet/collect/live.py KessBet/collect/kessbet/ws.py KessBet/web/api.py KessBet/web/live.html KessBet/web/live_detail.html KessBet/test/test_live_page.py KessBet/test/test_live_run.py
git commit -m "feat: unify current result state across API and pages"
```

---

### Task 6: E1 证据包与固定页

**Files:**
- Create: `KessBet/common/evidence.py`
- Modify: `KessBet/analyze/live/run.py`、`KessBet/db/dao.py`、`KessBet/db/schema.sql`、`KessBet/web/api.py`、`KessBet/web/evidence.html`
- Create: `KessBet/db/migrate_live_evidence.sql`
- Create: `KessBet/test/test_evidence_package.py`

**Interfaces:**
- `write_evidence_package(evidence_dir: str, snapshot: dict, analysis: dict, verification: dict) -> dict`，返回 `manifest`（含每个文件 SHA-256）；临时目录写完再原子 rename。
- 每次分析生成与正式命中表无关的 `analysis_id`；影子和正式模式都以 `analysis_id/evidence_id` 关联证据。数据库保存证据状态、包路径、manifest hash 和重试次数；正式命中另引用已完成的证据包。
- 文件：`manifest.json, request.json, response.json, normalized.json, state.json, analysis.json, verification.json`。
- `/api/evidence/{id}` 优先读包内 manifest；无包时按现有 replay_context 降级 E0。

- [ ] **Step 1: Write failing tests**

```python
def test_package_has_manifest_and_hashes(tmp_path):
    m = write_evidence_package(str(tmp_path), snapshot, analysis, verification)
    for name, meta in m["files"].items():
        p = tmp_path / name
        assert p.exists()
        assert sha256(p.read_bytes()) == meta["sha256"]

def test_package_omits_secrets(tmp_path):
    write_evidence_package(..., request={"headers": {"Authorization": "x"}})
    assert "Authorization" not in (tmp_path/"request.json").read_text()
```

- [ ] **Step 2: Run to verify fail**

Run: `py -m pytest KessBet/test/test_evidence_package.py -q`
Expected: FAIL.

- [ ] **Step 3: Implement writer + wire to verified hits**

Write files atomically; sanitize headers/query; include versions. `response.json` 标明原始 HTTP 字节/文本与解包后的 `mari`，两者不能混称。文件系统与数据库无法共享一个事务：先以 `analysis_id` 创建 `pending` 证据记录，再写临时目录和原子 rename，校验 manifest 后条件更新为 `complete`；重复执行须幂等。正式模式只有包 `complete` 后才能写正式命中并发布；包失败保持 `evidence_failed` 并重试，影子模式仍保留独立分析和失败原因。旧 E0 快照不能事后补成 E1。

- [ ] **Step 4: Run tests**

Run: `py -m pytest KessBet/test/test_evidence_package.py KessBet/test/test_live_run.py KessBet/test/test_live_page.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add KessBet/common/evidence.py KessBet/analyze/live/run.py KessBet/db/dao.py KessBet/db/schema.sql KessBet/db/migrate_live_evidence.sql KessBet/web/api.py KessBet/web/evidence.html KessBet/test/test_evidence_package.py
git commit -m "feat: write E1 evidence package for verified candidates"
```

---

### Task 7: 影子模式

**Files:**
- Modify: `KessBet/collect/config.py`、`KessBet/analyze/live/run.py`、`KessBet/web/api.py`
- Create: `KessBet/test/test_shadow_mode.py`
- Modify: `KessBet/docs/live-arbitrage-development-changes-20261117.md`

**Interfaces:**
- Config: `LIVE_SHADOW_MODE=True`（默认）。部署前两个开关均关闭分析/正式发布；影子验收时设 `LIVE_ANALYZE_ENABLED=True, LIVE_SHADOW_MODE=True`，正式发布仅在验收后设 `LIVE_SHADOW_MODE=False`。
- 影子模式：`analysis_status='shadow_candidate'`；写独立的影子分析/证据记录和诊断日志，**不写** `kessbet_arbitrage_snapshot` 正式命中、不发正式告警。当前机会 API 不把影子结果标为已发布套利。
- 正式写入需 `LIVE_ANALYZE_ENABLED=True` 且 `LIVE_SHADOW_MODE=False`，并检查证据包 `complete`。

- [ ] **Step 1: Write failing tests**

```python
def test_shadow_computes_but_does_not_write_hit(monkeypatch, fake_dao):
    monkeypatch.setattr(config, "LIVE_ANALYZE_ENABLED", True)
    monkeypatch.setattr(config, "LIVE_SHADOW_MODE", True)
    hit = []
    monkeypatch.setattr(L, "_persist_hit", lambda *a, **k: hit.append(1))
    L.analyze_live_once(fake_dao)
    assert hit == []

def test_formal_mode_writes_hit(monkeypatch, fake_dao):
    monkeypatch.setattr(config, "LIVE_ANALYZE_ENABLED", True)
    monkeypatch.setattr(config, "LIVE_SHADOW_MODE", False)
    ...
    assert hit == [1]
```

- [ ] **Step 2: Run to verify fail**

Run: `py -m pytest KessBet/test/test_shadow_mode.py -q`
Expected: FAIL.

- [ ] **Step 3: Implement shadow gating**

Gate `_persist_hit`/formal alerts on shadow in both immediate snapshot and periodic/CLI paths; keep independently keyed shadow analysis/evidence writes. The existing immediate callback and periodic path must call one shared qualification/persistence service so modes do not diverge. API labels `shadow_candidate` as “影子候选，未发布”，not “已验证”. Tests cover both entry points, evidence package success/failure, deduplication, and restart recovery.

- [ ] **Step 4: Run tests**

Run: `py -m pytest KessBet/test/test_shadow_mode.py KessBet/test/test_live_run.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add KessBet/collect/config.py KessBet/analyze/live/run.py KessBet/web/api.py KessBet/test/test_shadow_mode.py KessBet/docs/live-arbitrage-development-changes-20261117.md
git commit -m "feat: add shadow mode for live candidates"
```

---

### Task 8: 验收样例与恢复判定门槛

**Files:**
- Create: `KessBet/test/test_live_acceptance.py`
- Create: `KessBet/tools/live_shadow_report.py`
- Modify: `KessBet/docs/live-arbitrage-complete-functional-design-20261117.md`（追加 v1 验收记录）

**Interfaces:**
- 验收样例覆盖设计 §10 的 10 项；影子报告输出候选/状态/证据完整率。

- [ ] **Step 1: Write failing acceptance tests**

覆盖：9:0/7:0 反例、`other` 隔离、正向白名单、成功空/失败/结构错误/全过滤、比分变化重取、乱序 A/B、无覆盖/过期/开关关闭、E0/E1 等级、离线复算。

- [ ] **Step 2: Run to verify fail/缺实现**

Run: `py -m pytest KessBet/test/test_live_acceptance.py -q`
Expected: FAIL or skip for unimplemented cases; record which.

- [ ] **Step 3: Implement missing fixtures/tool**

`tools/live_shadow_report.py` 从 DB 导出 `analysis_status` 分布、`verified_candidate` 数、证据包完整率、采集/失效延迟；不写正式命中。

- [ ] **Step 4: Run acceptance + full suite**

Run: `py -m pytest KessBet/test/test_live_acceptance.py -q && py -m pytest KessBet/test -q`
Expected: acceptance PASS；全量除既有环境失败外 PASS。

- [ ] **Step 5: Commit + gate record**

```bash
git add KessBet/test/test_live_acceptance.py KessBet/tools/live_shadow_report.py KessBet/docs/live-arbitrage-complete-functional-design-20261117.md
git commit -m "test: add live arbitrage acceptance suite and shadow report"
```

**恢复正式判定门槛（全部满足才可 `LIVE_ANALYZE_ENABLED=True` + `LIVE_SHADOW_MODE=False`）：**

1. 验收 10 项全过；正反样例均有证据。
2. 影子运行至少一个完整比赛日，逐条人工复核影子候选及失败原因；样本不足时继续运行，不以“零命中”证明零误报。`no_candidate/unverifiable/expired/invalidated` 的原因与原始输入相符。
3. 证据包完整率 100%（`verified_candidate` 均可离线复算）。
4. 真实数据库联调、迁移/回滚/部署检查完成；浏览器截图仅作辅助，不把 E2 页面逐项核验作为 v1 门槛。
5. 页面/API/告警对 `cost=NULL`、过期、失效、影子状态显示一致。

## Self-Review

- **Spec coverage:** §4.2 → Task 1；§4.3 → Task 2；§5 → Task 3；§6.1 → Task 4；§3/§9 → Task 5；§7 → Task 6；§10 → Task 7/8；§8（E2）明确不在 v1。
- **Out of scope recorded:** E2/E3、自动下注、非白名单市场、提高 G、关闭验证器。
- **Type consistency:** `fetch_raw_result`、`input_fingerprint`、`classify_phase`、`verify_chosen(... total_stake/stake_unit/min_profit)`、`write_evidence_package`、`_live_result_state` 在各任务中保持同名同签名。
- **Review Focus tests:** 四类 HTTP 结果和真实信封 → Task 1；raw/rows 乱序、指纹去重和旧响应晚到 → Task 2；阶段来源与半场盘隔离 → Task 3；取整亏损与零利润边界 → Task 4；WS delete、报价过期、NULL/历史状态展示 → Task 5；影子证据恢复 → Task 6/7。
