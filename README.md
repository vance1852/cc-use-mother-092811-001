# 统筹跨区域交通通道项目组合协同基础服务

本项目在综合交通业务共享的服务端基础能力（运营机构、操作者、角色权限、请求幂等、SQLite
事务与哈希串联审计）之上，提供面向"十五五"通道目标年度分解的**通道项目组合决策服务**。

规划人员可以登记走廊拓扑、断点能力缺口、前置工程、区域服务缺口、分年投资估算和政策目标；
每轮评审冻结数据快照与评价规则后，系统识别空间重叠（同一瓶颈重复申报）、依赖冲突
（缺失前置、跨走廊依赖、依赖环）与预算挤占，按单位效益生成可解释的建设时序。项目的拆分、
合并、延期和退出沿版本保留血缘与指标影响；跨年度调整只能在新一轮中进行，已经批准的组合
不可倒改；同一轮次的竞争方案经过会签后**只能有一个生效方案**。评审人可以通过 HTTP API
复算任一版本对网络连通、边境与乡村覆盖和资金约束的贡献，服务重启后未完成的会签可以继续。

## 目录

- `src/transport_coordination/`：领域模型、SQLite 存储、权限服务、审计链、组合评价引擎、
  轮次冻结/版本/会签服务、HTTP 路由和离线验收；
- `tests/`：基础规则、评价引擎、组合决策、接口路由和端到端验收测试。

## 核心模型

| 对象 | 说明 |
| --- | --- |
| 政策目标 `policy-goals` | 如"主骨架贯通"，项目通过 `policy_goals` 对齐 |
| 走廊 `corridors` | 节点、边、断点（能力缺口、所在边）、服务缺口（边境/乡村）、主骨架端点对 |
| 项目 `projects` | 空间足迹、缓解断点、前置工程、服务区域、分年投资、效益年份、政策目标 |
| 轮次 `rounds` | 评审年度区间，必须先有开放轮次才能登记或调整；冻结后不可变 |
| 版本 `versions` | 冻结时保存全量数据快照、评价规则、二者哈希、版本哈希与跨版本影响 |
| 方案 `plans` | 系统推荐或人工编制，带建设时序决策与评价结果，参与竞争会签 |

评价引擎（`portfolio.py`）是纯函数模块：并查集计算网络连通和主骨架端点对贯通，
未缓解断点所在边视为中断；覆盖指标按边境/乡村服务缺口区域统计；资金指标按年度预算
上限核算；推荐时序在尊重前置工程的前提下按单位效益贪心排入预算，并给出每个项目
排产或延期的原因。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

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

验收命令会在临时 SQLite 数据库中登记沿海、沿边、沿江和出疆入藏四条走廊及其项目，
故意构造同一断点重复申报、前置工程依赖和预算挤占；随后冻结第一轮、复算指标、提交
竞争方案、拦截超预算方案、在**服务重启后**完成第二位评审人会签并产生唯一生效方案，
再开启第二轮做跨年度延期并核对对已批准组合的影响。成功时输出一行 `status` 为 `ok`
的 JSON 并以退出码 `0` 结束。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m transport_coordination.api --database transport_coordination.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查使用 `GET /health`。写入接口通过 `X-Actor-Id` 标识操作者，所有写操作都要求
唯一的 `request_id` 以保证幂等，服务重启后 SQLite 中的业务状态、审计历史和会签进度
继续保留。

### 主要接口

基础登记：`POST /organizations`、`POST /actors`、`POST /sites`、
`POST /domain-records`、`GET /domain-records`、`GET /audit-events`。

组合决策：

- `POST /policy-goals`：登记政策目标；
- `POST /corridors` / `POST /corridors/update` / `GET /corridors/{id}`：走廊登记与修订；
- `POST /projects`：登记项目；
- `POST /projects/update`、`/projects/split`、`/projects/merge`、`/projects/defer`、
  `/projects/exit`：项目修改与拆分/合并/延期/退出（只在开放轮次内）；
- `GET /projects`、`GET /projects/{id}/lineage`：项目清单与血缘、修订历史；
- `POST /rounds`（可带 `parent_round_id` 关联上一轮）、`GET /rounds`；
- `POST /rounds/freeze`：冻结数据快照与评价规则，生成不可变版本；
- `GET /versions`、`GET /versions/{id}`、`GET /versions/{id}/recompute`：版本查询与复算；
- `POST /plans`：基于冻结版本生成推荐方案（`origin=recommended`）或人工方案（
  `origin=manual` + `selected_project_ids`）；
- `GET /plans?round_id=...`、`GET /plans/{id}`、`GET /plans/{id}/recompute`；
- `POST /plans/sign`：指定会签人表决（`approve` / `reject`）。达到规则要求的批准数时
  生效；生效前复核年度预算硬约束、前置工程纳入和依赖环。生效时同轮其他方案自动置为
  `superseded`，数据库部分唯一索引保证每轮至多一个 `effective` 方案。

### 规则示例（冻结时提交）

```json
{
  "weights": {
    "backbone": 3.0,
    "connectivity": 1.0,
    "border_coverage": 2.0,
    "rural_coverage": 2.0,
    "bottleneck_relief": 2.0,
    "policy_alignment": 1.0
  },
  "budgets": {"2026": 120.0, "2027": 160.0},
  "approvers": ["rev-001", "rev-002"],
  "required_approvals": 2
}
```

## 角色

- `admin`：管理操作者、开启/冻结轮次、参与会签；
- `reviewer`：开启/冻结轮次、编制方案、参与会签；
- `operator`：登记走廊、项目并执行拆分/合并/延期/退出；
- `auditor`：只读与审计查询，不能写入。
