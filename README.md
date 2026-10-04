# 专利权利要求依附审查 API

基于 **Python + FastAPI + Pydantic + SQLite** 的服务：按版本链记录专利权利要求修订，
按目标日期重建当日有效版本，并审查从属权利要求的依附关系（引用有效性、成环、多项引用语义）。

## 一条命令启动

```bash
docker compose up --build
```

- **API 端口：`8000`**（宿主机 `8000` → 容器 `8000`，见 `docker-compose.yml`）
- 交互式文档：http://localhost:8000/docs （Swagger UI）
- 健康检查：`GET http://localhost:8000/health`
- SQLite 数据持久化在命名卷 `patent-data`（容器内 `/data/app.db`，可用环境变量 `DATABASE_PATH` 覆盖）

本地开发（不用 Docker）：

```bash
pip install -r requirements.txt
uvicorn app.main:app --port 8000
pytest tests/ -q        # 运行测试
```

## API

### POST /applications — 提交申请及修订（幂等、事务性）

```json
{
  "request_id": "req-001",
  "application_number": "CN202610001",
  "revisions": [
    {"claim_number": 1, "version": 1, "claim_type": "independent",
     "references": [], "text": "一种装置……",
     "effective_from": "2026-01-01", "effective_to": null},
    {"claim_number": 2, "version": 1, "claim_type": "dependent",
     "references": [1], "reference_mode": "all", "text": "如权利要求1所述……",
     "effective_from": "2026-01-01", "effective_to": null}
  ]
}
```

**不可跳跃的替代链**（同一 `claim_number` 的修订）：

- 版本号必须从 1 开始、逐版本连续递增（不允许 v1 → v3 跳号）；
- 时间链必须衔接：新版本的 `effective_from` 必须等于上一版本的 `effective_to`；
- 上一版本若为开放区间（`effective_to = null`），提交新版本时会被**自动封闭**为
  `effective_to = 新版本.effective_from`，保证任意日期恰好一个有效版本。

**幂等与事务**：

- `request_id` 重复且负载一致 → 返回首次响应（`201`，响应头 `X-Idempotent-Replay: true`），不重复写入；
- `request_id` 重复但负载不同 → `409`；
- 任一条修订校验失败 → 整批回滚（`422`，附全部错误明细），**不写入部分数据**。

### POST /applications/window-audit — 按日期窗口逐段送审后再提交

在 `POST /applications` 的请求体（`request_id`、`application_number`、同一套 `revisions` 字段）
之上**额外**给出半开区间审查窗口 `[start_date, end_date)`，`start_date` 必须严格早于 `end_date`：

```bash
curl -X POST http://localhost:8000/applications/window-audit \
  -H 'Content-Type: application/json' \
  -d '{
    "request_id": "req-window-001",
    "application_number": "CN202610001",
    "start_date": "2026-01-01",
    "end_date": "2027-01-01",
    "revisions": [
      {"claim_number": 1, "version": 1, "claim_type": "independent",
       "references": [], "text": "一种装置……",
       "effective_from": "2026-01-01", "effective_to": "2026-06-01"},
      {"claim_number": 1, "version": 2, "claim_type": "independent",
       "references": [], "text": "一种改进装置……",
       "effective_from": "2026-06-01", "effective_to": null},
      {"claim_number": 2, "version": 1, "claim_type": "dependent",
       "references": [1], "reference_mode": "all", "text": "如权利要求1所述……",
       "effective_from": "2026-01-01", "effective_to": null}
    ]
  }'
```

处理流程（**全部在单个数据库事务内**）：

1. 沿用 `/applications` 的全部规则把整批修订（含开放版本的**自动封闭**）**临时**写入事务；
2. 收集窗口内所有权利要求版本的生效/失效边界，把 `[start_date, end_date)` 划成若干段——
   段起日为 `start_date`，以及每个落在 `(start_date, end_date)` 内的边界日（`end_date` 本身不划段）；
3. 用**现有 `/audit` 的同一套规则**审查每个段起日：该日必须**存在至少一个有效权利要求**且审查通过；
4. **每段都非空且审查通过**，才一次性提交修订与 `request_id`；任一段为空或不通过，
   整批修订及 `request_id` 全部回滚，旧数据与此前冻结的证据包不受影响。

成功响应（`201`）给出各段审查日期与提交结果：

```json
{
  "request_id": "req-window-001",
  "application_number": "CN202610001",
  "start_date": "2026-01-01",
  "end_date": "2027-01-01",
  "stored_revisions": 3,
  "auto_closed": [],
  "segments": [
    {"audit_date": "2026-01-01", "effective_claims": 2, "status": "pass"},
    {"audit_date": "2026-06-01", "effective_claims": 2, "status": "pass"}
  ]
}
```

任一失败返回 `422`（`error.code = window_audit_rejected`），`details` 给出
**最早失败日期** `failed_date`、`reason`（`EMPTY_SEGMENT` / `AUDIT_FAILED`）
及该日的完整 `/audit` 问题列表 `issues`；此时修订未保存，窗口内每一天都恢复为提交前的状态
（临时自动封闭也一并撤销，旧开放版本依旧开放），旧证据包仍可照常 `GET /audit/evidence/{id}` 复验。

**幂等与原端点行为**：

- 同一 `request_id` 携带**相同请求**重试：返回首次成功结果（`X-Idempotent-Replay: true`）；
- 同一 `request_id` **更换内容**（含窗口或任一字段）：`409`；`request_id` 在两个提交端点间共用；
- 失败的尝试不占用 `request_id`，可修正后原样重试；
- 原 `POST /applications` 行为完全不变（不做任何窗口审查）。

### POST /audit — 按目标日期审查

```json
{"application_number": "CN202610001", "target_date": "2026-09-23"}
```

处理流程：取 `effective_from <= 目标日 < effective_to` 的版本重建当日权利要求集合，然后校验：

| 问题码 | 含义 |
|---|---|
| `INVALID_REFERENCE` | 交集（`all`）引用中存在无效项：编号不存在，或目标日不在其版本有效区间内 |
| `NO_VALID_REFERENCE` | 并集（`any`）引用中**任一**引用都无效 |
| `CYCLE` | 依附关系成环（含自环），链中给出闭环路径 |
| `INDEPENDENT_WITH_REFERENCES` / `DEPENDENT_WITHOUT_REFERENCE` | 结构性问题 |

**多项引用语义**（`reference_mode`）：

- `all`（交集/累计引用，“如权利要求 1 和 2 所述”）：引用的每一项都必须有效；
- `any`（并集/择一引用，“如权利要求 1 或 2 所述”）：至少一项有效即通过。

每条问题都带**引用链**（`chain`，含各环节 `claim_number` 与 `version`，无效编号版本为 `null`），
问题列表按 `(claim_number, code, 链上编号)` **确定性排序**。响应示例：

```json
{
  "status": "fail",
  "effective_claims": [{"claim_number": 1, "version": 1, "..." : "..."}],
  "issues": [{
    "code": "CYCLE",
    "message": "依附关系成环: 3 -> 4 -> 3",
    "claim_number": 3,
    "claim_version": 1,
    "chain": [{"claim_number": 3, "version": 1},
              {"claim_number": 4, "version": 1},
              {"claim_number": 3, "version": 1}]
  }]
}
```

### POST /audit/evidence — 冻结可复验的审查证据包

请求体与 `POST /audit` 完全相同（申请号 + 目标日期）。服务按**同一套 /audit 规则**审查后，
把「目标日、有效权利要求（含版本）、问题」按**固定字段顺序**冻结为规范 UTF-8 JSON
（紧凑序列化、中文不转义），保存快照并返回 `evidence_id` 与 `SHA-256` 摘要：

```bash
curl -X POST http://localhost:8000/audit/evidence \
  -H 'Content-Type: application/json' \
  -d '{"application_number": "CN202610001", "target_date": "2026-09-23"}'
```

```json
{
  "evidence_id": "bafd97da246040fca6327cd86e77529a",
  "application_number": "CN202610001",
  "target_date": "2026-09-23",
  "sha256": "fed22522a13832e3f42b3d2849b785be7df1fa26c627d6d60f7899be7862b043"
}
```

冻结载荷字段顺序固定为：顶层 `target_date, effective_claims, issues`；
权利要求 `claim_number, version, claim_type, references, reference_mode, text, effective_from, effective_to`；
问题 `code, message, claim_number, claim_version, chain`（链节点 `claim_number, version`）。
**摘要只覆盖该冻结载荷**，不含 `evidence_id`、生成时间等易变字段；
对同一数据状态下的同一输入重复生成，摘要必然相同（每次生成仍是独立的新包，`evidence_id` 不同）。

**签发依据（seal）**：签发时，服务另用一把**保存在数据库之外的密钥**对
`(evidence_id, application_number, target_date, sha256)` 计算 HMAC-SHA256 并随包落库。
密钥不落库，因此该依据不依赖任何可改写的数据库记录：篡改者即使把快照换成另一份
合法快照、并把库内摘要同步改成新摘要（甚至从别的包连 seal 一起复制），也无法伪造出
与新内容匹配的 seal。seal 只用于服务端复验，不出现在任何 API 响应中。

### GET /audit/evidence/{evidence_id} — 只读复验

只读取冻结快照，**不回查修订表**。复验同时核对三项：**签发依据（seal）、冻结字节、
库存摘要**——三者齐备才判可信：

```bash
curl http://localhost:8000/audit/evidence/bafd97da246040fca6327cd86e77529a
```

- 三项全部一致：`verified: true`，返回 `snapshot`（冻结的目标日 / 有效权利要求 / 问题）；
- 快照或存储摘要被**单独**篡改：仍返回 `200`，但 `verified: false`、`snapshot: null`，
  并在 `integrity_error` 中以 `SNAPSHOT_DIGEST_MISMATCH` 同时给出
  `stored_digest` 与现场重算的 `recomputed_digest`（`digest` 字段即重算值）；
- 快照与摘要被**联动替换**（二者彼此相符）：`verified: false`、`snapshot: null`，
  `integrity_error.code = SEAL_MISMATCH`——库存摘要与重算摘要即使一致也不足以判定可信；
- **无签发依据的旧包**（本机制上线前签发、库中 `seal` 为空的记录）：仍可正常读取
  （`200`，`digest`/`stored_digest` 等字段照常返回），但不会仅凭库内摘要一致返回
  `verified: true`，而是 `verified: false`、`snapshot: null`，
  `integrity_error.code = SEAL_MISSING`；旧包若发生单项篡改，仍按
  `SNAPSHOT_DIGEST_MISMATCH` 报告；
- 证据包不存在：`404`。

```json
{
  "evidence_id": "bafd97da246040fca6327cd86e77529a",
  "application_number": "CN202610001",
  "target_date": "2026-09-23",
  "digest": "fed22522a13832e3f42b3d2849b785be7df1fa26c627d6d60f7899be7862b043",
  "stored_digest": "fed22522a13832e3f42b3d2849b785be7df1fa26c627d6d60f7899be7862b043",
  "verified": true,
  "integrity_error": null,
  "snapshot": {
    "target_date": "2026-09-23",
    "effective_claims": [{"claim_number": 1, "version": 1, "...": "..."}],
    "issues": []
  }
}
```

**后续提交新修订不会改变旧包**：旧证据包永远回放冻结时的字节与摘要
（例如冻结时为开放区间的版本，其 `effective_to` 在旧包中始终是 `null`，
即使之后的修订自动封闭了该区间）。任何持有快照的一方都可以自行复算
`SHA-256(UTF-8 紧凑 JSON)` 与返回摘要比对。

### 签发密钥（seal key）

复验所依赖的签发密钥保存在**数据库之外**，因此改写数据库记录无法伪造签发依据：

- 环境变量 `EVIDENCE_SEAL_KEY`：设置后以其为密钥来源（适合生产环境用密管系统注入）；
- 未设置时，首次签发会在数据库文件旁生成随机密钥文件 `evidence_seal.key`
  （权限 `0600`），之后一直复用——Docker 部署中它落在命名卷 `patent-data` 内，
  随卷持久化，**容器重启后新包、旧包均可继续复验**。

请勿删除或更换该密钥：已签发包的 seal 都是用原密钥计算的，密钥丢失或变更后，
这些包复验会报 `SEAL_MISMATCH`（其冻结字节与摘要本身不受影响，仍原样返回）。

### 其他端点

- `GET /applications/{application_number}` — 查看某申请的完整版本链（调试/核对用）
- `GET /health`

## 项目结构

```
app/
  main.py      # FastAPI 路由与异常映射
  models.py    # Pydantic 请求/响应模型（含提交期结构校验）
  store.py     # 写入路径：幂等 + 版本链校验 + 单事务
  window.py    # 日期窗口送审提交：临时应用 → 边界划段 → 逐段 /audit → 一次提交或整体回滚
  audit.py     # 目标日重建、引用有效性、DFS 成环检测
  evidence.py  # 证据包：固定字段顺序冻结 UTF-8 JSON、SHA-256、库外密钥签发依据、只读复验/篡改报告
  db.py        # SQLite 连接与 schema（含 evidence_packages 快照表与就地迁移）
  errors.py    # 领域错误 → HTTP 状态码
tests/test_api.py
tests/test_evidence.py
tests/test_window.py
Dockerfile / docker-compose.yml / requirements.txt
```
