# 综合交通通道项目组合协同决策服务

面向"十五五"通道目标年度分解场景：沿海、沿边、沿江、出疆入藏等方向同时申报扩能方案时，
在同一口径下登记走廊拓扑、断点能力、前置工程、区域服务缺口、投资估算与政策目标，
在每轮评审中**冻结数据与评价规则**，识别**空间重叠、依赖冲突、预算挤占**，
形成**可解释的建设时序**，并通过**竞争会签只产生一个生效方案**。

服务在原有运营机构/操作者/场所/资料登记、角色权限、请求幂等、SQLite 事务与
哈希串联审计能力之上扩展，运行时仅依赖 Python 标准库与 SQLite。

## 核心能力

- **登记**：走廊节点、区段（现有/缺失、现状与目标能力）、断点、边境/乡村服务区域、
  政策目标，以及项目（建设单位、覆盖区段与断点、前置工程、分年度投资、政策目标）。
- **评审轮冻结**：冻结时刻把全部登记数据与评价规则写入快照并计算联合哈希；
  此后历史版本只能按快照复算，登记数据的后续变化不影响已冻结轮次。
- **组合评价（`evaluation.py`，纯函数、确定性）**：
  - 枢纽间网络连通比、逐走廊主骨架贯通率与达标区段比；
  - 边境、乡村服务覆盖（区域节点与枢纽同属一个连通分量）；
  - 断点闭合率与加权总分（权重在冻结规则中固定）；
  - 识别 `spatial_overlap`（不同单位就同一区段重复申报）、`duplicate_gap`
    （同一瓶颈重复申报）、`dependency_conflict`（前置退出/未纳入/无法安排）、
    `dependency_cycle`、`budget_squeeze`（年度预算内无安排窗口）；
  - 在前置约束与年度预算下，按"边际贡献最大且可复算"的贪心顺序给出建设时序，
    每个入选项目带 `reasons`（新闭合断点、新覆盖边境/乡村片区、贯通走廊、
    前置完成年份、连通增益、分年度投资）。
- **项目版本谱系**：修订、延期、退出、拆分、合并均生成新版本并写入谱系；
  每次调整自动对**所有已生效组合**复算影响（得分差、指标差、时序变化）并沿版本保留。
- **不可倒改**：已批准组合引用冻结快照中的项目版本，跨年度调整不会修改历史组合；
  随时可用相同规则与快照复算出相同的指标哈希。
- **竞争会签**：同一轮可提交多个方案；按冻结规则要求的角色会签，
  借助单条 `UPDATE ... WHERE effective_proposal_id IS NULL` 的原子认领，
  **全局只有一个方案生效**，其余方案标记为 `superseded`；任一角色否决即驳回。
- **重启续签**：签署记录持久化在 SQLite，服务重启后可继续未完成的会签。

## 目录

- `src/transport_coordination/`
  - `evaluation.py`：确定性评价引擎（连通/骨架/覆盖/断点、冲突识别、可解释时序、哈希）；
  - `portfolio.py`：组合领域服务（登记、冻结、提案、会签、版本谱系、复算查询）；
  - `service.py` / `storage.py` / `audit.py` / `clock.py`：基础权限、事务、审计链、时钟；
  - `api.py`：标准库 HTTP/JSON 边界；`acceptance.py`：离线端到端验收。
- `tests/`：评价引擎、组合服务、竞争会签、版本谱系、HTTP 路由与端到端验收测试。

## 环境

- Linux，Python 3.11 或更高版本，仅使用 Python 标准库和 SQLite。

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m transport_coordination.acceptance
```

验收会在临时 SQLite 中登记网络与项目、冻结首轮、提交两个竞争方案（其一含重复申报）、
完成多角色会签，并在**中途关闭并重启服务**后续签生效，随后做跨年度延期与拆分，
核对历史组合不可倒改与审计链完整。成功时输出一行 `status` 为 `ok` 的 JSON 并以退出码 `0` 结束。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m transport_coordination.api --database transport.sqlite3 --host 127.0.0.1 --port 8080
```

写入接口通过 `X-Actor-Id` 标识操作者，所有写操作要求幂等键 `request_id`。
健康检查 `GET /health` 返回审计链校验状态。

### 主要接口

| 方法与路径 | 说明 |
| --- | --- |
| `POST /nodes` `/edges` `/gaps` `/service-areas` `/policy-targets` | 登记走廊拓扑、断点、服务缺口、政策目标 |
| `POST /projects` | 登记项目（区段/断点/前置/分年度投资/建设单位） |
| `POST /project-adjustments` | `revise`/`delay`/`exit`/`split`/`merge`，沿版本留痕并复算对已批准组合的影响 |
| `GET /projects/{id}/versions` | 项目全部历史版本 |
| `GET /lineage?project_id=` | 拆分/合并/延期/退出谱系与影响 |
| `POST /rounds` | 创建评审轮（评价规则：年度预算、规划年度、枢纽、权重、会签角色） |
| `POST /rounds/{id}/freeze` | 冻结数据与规则，返回快照哈希 |
| `GET /rounds/{id}` | 轮状态、规则哈希、快照哈希、生效方案 |
| `POST /proposals` | 在冻结轮提交组合方案，返回评价、时序与 findings |
| `POST /proposals/{id}/sign` | `approve`/`reject` 会签；角色齐备时原子生效 |
| `GET /proposals/{id}` | 方案、评价结果、当前会签进度 |
| `POST /proposals/{id}/recalculate` | 按冻结快照复算（可用替换 items 做假设分析），返回哈希一致性 |
| `GET /rounds/{id}/proposals` | 一轮内全部竞争方案及其状态 |
| `GET /audit-events` | 哈希串联审计事件 |

### 评价规则示例

```json
{
  "budgets": {"2026": 320, "2027": 320},
  "horizon": [2026, 2027],
  "hubs": ["hub-east"],
  "weights": {"connectivity": 0.3, "skeleton": 0.3, "coverage": 0.3, "gap": 0.1},
  "border_weight": 1.0,
  "rural_weight": 0.6,
  "required_roles": ["admin", "reviewer"]
}
```

## 设计约束

- 评价引擎不访问数据库与时钟，同一冻结快照 + 同一方案必然得到相同 `metrics_hash`。
- 所有业务写操作在单条 SQLite `BEGIN IMMEDIATE` 事务内完成并追加审计事件。
- 已生效方案与冻结快照一经确定不可修改；任何调整只产生新版本与影响记录。
