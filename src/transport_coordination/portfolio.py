"""通道项目组合的纯函数评价引擎。

所有计算只依赖输入的数据快照与规则对象，不访问数据库，
因此任一轮次冻结后的版本都可以被原样复算。
"""

from __future__ import annotations

from collections import deque
from typing import Any

from .audit import canonical_json, digest


DIRECTIONS = frozenset({"coastal", "border", "riverine", "xinjiang_tibet", "inland"})


class UnionFind:
    """网络连通性使用的并查集。"""

    def __init__(self, nodes: list[str]) -> None:
        self.parent = {node: node for node in nodes}
        self.rank = {node: 0 for node in nodes}

    def add(self, node: str) -> None:
        if node not in self.parent:
            self.parent[node] = node
            self.rank[node] = 0

    def find(self, node: str) -> str:
        parent = self.parent[node]
        if parent != node:
            self.parent[node] = self.find(parent)
        return self.parent[node]

    def union(self, left: str, right: str) -> None:
        self.add(left)
        self.add(right)
        root_left = self.find(left)
        root_right = self.find(right)
        if root_left == root_right:
            return
        if self.rank[root_left] < self.rank[root_right]:
            root_left, root_right = root_right, root_left
        self.parent[root_right] = root_left
        if self.rank[root_left] == self.rank[root_right]:
            self.rank[root_left] += 1

    def connected(self, left: str, right: str) -> bool:
        return left in self.parent and right in self.parent and self.find(left) == self.find(right)


def normalize_project(raw: dict[str, Any]) -> dict[str, Any]:
    """把登记载荷整理成评价用项目结构，并执行基础校验。"""

    project_id = str(raw.get("project_id", "")).strip()
    corridor_id = str(raw.get("corridor_id", "")).strip()
    investments = raw.get("investments") or {}
    if not isinstance(investments, dict) or not investments:
        raise ValueError(f"项目 {project_id or '?'} 必须提供分年投资 investments")
    yearly: dict[str, float] = {}
    for year, amount in investments.items():
        amount = float(amount)
        if amount < 0:
            raise ValueError(f"项目 {project_id} 投资不能为负")
        yearly[str(year)] = amount
    duration = int(raw.get("duration_years", 1))
    if duration < 1:
        raise ValueError(f"项目 {project_id} 工期至少为 1 年")
    benefit_year = str(raw.get("benefit_year") or max(yearly, default=""))
    if not benefit_year:
        raise ValueError(f"项目 {project_id} 缺少效益年份")

    def as_str_set(value: Any) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list):
            value = [value]
        return sorted({str(item) for item in value})

    footprint = as_str_set(raw.get("footprint"))
    return {
        "project_id": project_id,
        "corridor_id": corridor_id,
        "name": str(raw.get("name", "")).strip(),
        "builder": str(raw.get("builder", "")).strip(),
        "direction": str(raw.get("direction", "")).strip(),
        "footprint": footprint,
        "bottleneck_ids": as_str_set(raw.get("bottleneck_ids")),
        "prerequisite_project_ids": as_str_set(raw.get("prerequisite_project_ids")),
        "service_areas": as_str_set(raw.get("service_areas")),
        "policy_goals": as_str_set(raw.get("policy_goals")),
        "investments": dict(sorted(yearly.items())),
        "duration_years": duration,
        "benefit_year": benefit_year,
        "target_capacity": float(raw.get("target_capacity", 0.0)),
        "priority": int(raw.get("priority", 0)),
    }


def normalize_corridor(raw: dict[str, Any]) -> dict[str, Any]:
    """整理走廊拓扑并校验端点、边和断点。"""

    corridor_id = str(raw.get("corridor_id", "")).strip()
    name = str(raw.get("name", "")).strip()
    direction = str(raw.get("direction", "")).strip()
    if direction not in DIRECTIONS:
        raise ValueError(f"走廊 {corridor_id} 的方向 {direction} 不受支持")
    nodes = {str(node) for node in raw.get("nodes", [])}
    edges_raw = raw.get("edges", [])
    edges: list[dict[str, str]] = []
    for edge in edges_raw:
        left = str(edge.get("from", "")).strip()
        right = str(edge.get("to", "")).strip()
        edge_id = str(edge.get("edge_id") or f"{left}--{right}").strip()
        if not left or not right:
            raise ValueError(f"走廊 {corridor_id} 存在缺少端点的边")
        nodes.update([left, right])
        edges.append({"edge_id": edge_id, "from": left, "to": right})
    bottlenecks: dict[str, dict[str, Any]] = {}
    bottleneck_items = raw.get("bottlenecks", [])
    if isinstance(bottleneck_items, dict):
        bottleneck_items = list(bottleneck_items.values())
    for raw_point in bottleneck_items:
        point_id = str(raw_point.get("bottleneck_id", "")).strip()
        if not point_id:
            raise ValueError(f"走廊 {corridor_id} 存在缺少编号的断点")
        gap = float(raw_point.get("capacity_gap", 0.0))
        if gap < 0:
            raise ValueError(f"断点 {point_id} 能力缺口不能为负")
        bottlenecks[point_id] = {
            "bottleneck_id": point_id,
            "edge_id": str(raw_point.get("edge_id", "")).strip() or None,
            "from_node": str(raw_point.get("from_node", "")).strip() or None,
            "to_node": str(raw_point.get("to_node", "")).strip() or None,
            "capacity_gap": gap,
            "target_capacity": float(raw_point.get("target_capacity", 0.0)),
        }
    gaps: dict[str, dict[str, Any]] = {}
    gap_items = raw.get("service_gaps", [])
    if isinstance(gap_items, dict):
        gap_items = list(gap_items.values())
    for raw_gap in gap_items:
        area_id = str(raw_gap.get("area_id", "")).strip()
        if not area_id:
            raise ValueError(f"走廊 {corridor_id} 存在缺少区域编号的服务缺口")
        kind = str(raw_gap.get("kind", "generic")).strip()
        gaps[area_id] = {"area_id": area_id, "kind": kind,
                         "population": float(raw_gap.get("population", 0.0))}
    backbone = []
    for pair in raw.get("backbone_pairs", []):
        if isinstance(pair, str):
            left, _, right = pair.partition("|")
        else:
            left, right = pair.get("from", ""), pair.get("to", "")
        left, right = str(left).strip(), str(right).strip()
        if left and right:
            backbone.append(left + "|" + right)
    return {
        "corridor_id": corridor_id,
        "name": name,
        "direction": direction,
        "nodes": sorted(nodes),
        "edges": edges,
        "bottlenecks": bottlenecks,
        "service_gaps": gaps,
        "backbone_pairs": backbone,
    }


def normalize_rules(raw: dict[str, Any]) -> dict[str, Any]:
    """整理评价规则，缺省权重均为 0。"""

    weights = raw.get("weights") or {}
    budgets = {str(year): float(amount) for year, amount in (raw.get("budgets") or {}).items()}
    if any(amount < 0 for amount in budgets.values()):
        raise ValueError("预算上限不能为负")
    approvers = [str(item) for item in raw.get("approvers", [])]
    if not approvers:
        raise ValueError("评价规则必须至少指定一名会签人")
    normalized = {
        "weights": {
            "backbone": float(weights.get("backbone", 0.0)),
            "connectivity": float(weights.get("connectivity", 0.0)),
            "border_coverage": float(weights.get("border_coverage", 0.0)),
            "rural_coverage": float(weights.get("rural_coverage", 0.0)),
            "bottleneck_relief": float(weights.get("bottleneck_relief", 0.0)),
            "policy_alignment": float(weights.get("policy_alignment", 0.0)),
        },
        "budgets": dict(sorted(budgets.items())),
        "approvers": approvers,
        "required_approvals": int(raw.get("required_approvals", len(approvers))),
        "overlap_penalty": float(raw.get("overlap_penalty", 1.0)),
        "dependency_penalty": float(raw.get("dependency_penalty", 1.0)),
        "horizon_years": sorted(budgets),
    }
    if normalized["required_approvals"] < 1 or normalized["required_approvals"] > len(approvers):
        raise ValueError("required_approvals 必须在 1 到会签人数量之间")
    return normalized


# ---------------------------------------------------------------------------
# 冲突识别
# ---------------------------------------------------------------------------


def detect_spatial_overlap(projects: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """识别同一走廊内共用断点或空间单元的项目（重复申报）。"""

    overlaps: list[dict[str, Any]] = []
    for index, current in enumerate(projects):
        for other in projects[index + 1:]:
            if current["corridor_id"] != other["corridor_id"]:
                continue
            shared_bottlenecks = sorted(set(current["bottleneck_ids"]) & set(other["bottleneck_ids"]))
            shared_footprint = sorted(set(current["footprint"]) & set(other["footprint"]))
            if shared_bottlenecks or shared_footprint:
                reasons = []
                if shared_bottlenecks:
                    reasons.append("shared_bottleneck")
                if shared_footprint:
                    reasons.append("shared_footprint")
                overlaps.append({
                    "project_ids": sorted([current["project_id"], other["project_id"]]),
                    "corridor_id": current["corridor_id"],
                    "shared_bottleneck_ids": shared_bottlenecks,
                    "shared_footprint": shared_footprint,
                    "reasons": reasons,
                })
    return overlaps


def detect_dependency_conflicts(projects: list[dict[str, Any]]) -> dict[str, list[Any]]:
    """识别缺失前置、自依赖、跨走廊依赖和依赖环。"""

    by_id = {project["project_id"]: project for project in projects}
    missing: list[dict[str, str]] = []
    cross_corridor: list[dict[str, str]] = []
    adjacency: dict[str, list[str]] = {project_id: [] for project_id in by_id}
    for project in projects:
        for prerequisite in project["prerequisite_project_ids"]:
            target = by_id.get(prerequisite)
            if target is None:
                missing.append({"project_id": project["project_id"], "missing_prerequisite_id": prerequisite})
                continue
            if prerequisite == project["project_id"]:
                missing.append({"project_id": project["project_id"], "missing_prerequisite_id": prerequisite})
                continue
            if target["corridor_id"] != project["corridor_id"]:
                cross_corridor.append({"project_id": project["project_id"],
                                       "prerequisite_project_id": prerequisite,
                                       "corridor_id": project["corridor_id"],
                                       "prerequisite_corridor_id": target["corridor_id"]})
            adjacency[project["project_id"]].append(prerequisite)
    cycles = _find_cycles(adjacency)
    return {"missing": missing, "cross_corridor": cross_corridor, "cycles": cycles}


def _find_cycles(adjacency: dict[str, list[str]]) -> list[list[str]]:
    """以 DFS 输出去重后的依赖环。"""

    state: dict[str, int] = {node: 0 for node in adjacency}
    stack: list[str] = []
    found: list[list[str]] = []

    def visit(node: str) -> None:
        state[node] = 1
        stack.append(node)
        for neighbor in adjacency.get(node, []):
            if state.get(neighbor, 0) == 0:
                visit(neighbor)
            elif state[neighbor] == 1:
                start = stack.index(neighbor)
                cycle = stack[start:] + [neighbor]
                found.append(cycle)
        stack.pop()
        state[node] = 2

    for node in sorted(adjacency):
        if state[node] == 0:
            visit(node)
    return _dedupe_cycles(found)


def _dedupe_cycles(cycles: list[list[str]]) -> list[list[str]]:
    seen: set[str] = set()
    result: list[list[str]] = []
    for cycle in cycles:
        body = cycle[:-1]
        rotation = min(range(len(body)), key=lambda i: body[i:] + body[:i])
        rotated = body[rotation:] + body[:rotation]
        key = canonical_json(rotated)
        if key not in seen:
            seen.add(key)
            result.append(rotated + [rotated[0]])
    return result


# ---------------------------------------------------------------------------
# 指标计算
# ---------------------------------------------------------------------------


def _build_network(corridors: list[dict[str, Any]],
                   relieved: set[str] | None = None
                   ) -> tuple[UnionFind, dict[str, set[str]], list[tuple[str, str]]]:
    """构建网络；未缓解断点所在的边视为中断，不接入连通。"""

    relieved = relieved if relieved is not None else set()
    nodes: list[str] = []
    for corridor in corridors:
        nodes.extend(corridor["nodes"])
    union_find = UnionFind(sorted(set(nodes)))
    adjacent: dict[str, set[str]] = {node: set() for node in union_find.parent}
    edges: list[tuple[str, str]] = []
    for corridor in corridors:
        blocked_edges = {point["edge_id"] for point in corridor["bottlenecks"].values()
                         if point["edge_id"] and point["bottleneck_id"] not in relieved}
        for edge in corridor["edges"]:
            left, right = edge["from"], edge["to"]
            adjacent.setdefault(left, set()).add(right)
            adjacent.setdefault(right, set()).add(left)
            edges.append((left, right))
            if edge["edge_id"] not in blocked_edges:
                union_find.union(left, right)
    return union_find, adjacent, edges


def _reachable(adjacent: dict[str, set[str]], sources: set[str]) -> set[str]:
    seen: set[str] = set()
    queue = deque(sorted(sources))
    while queue:
        node = queue.popleft()
        if node in seen:
            continue
        seen.add(node)
        queue.extend(adjacent.get(node, set()) - seen)
    return seen


def _baseline_metrics(corridors: list[dict[str, Any]]) -> dict[str, Any]:
    """所有断点未缓解时的基线：统计断点数量、缺口区域和骨架连通。"""

    union_find, _adjacent, _edges = _build_network(corridors)
    bottleneck_total = 0
    border_total = rural_total = 0
    for corridor in corridors:
        bottleneck_total += len(corridor["bottlenecks"])
        for gap in corridor["service_gaps"].values():
            if gap["kind"] == "border":
                border_total += 1
            elif gap["kind"] == "rural":
                rural_total += 1
    backbone_total = 0
    for corridor in corridors:
        for pair in corridor["backbone_pairs"]:
            left, right = pair.split("|", 1)
            backbone_total += 0 if union_find.connected(left, right) else 1
    return {
        "bottleneck_total": bottleneck_total,
        "backbone_missing_pairs": backbone_total,
        "border_gap_total": border_total,
        "rural_gap_total": rural_total,
    }


def compute_metrics(corridors: list[dict[str, Any]], projects: list[dict[str, Any]],
                    selected_ids: list[str], rules: dict[str, Any],
                    goal_codes: set[str] | None = None) -> dict[str, Any]:
    """对给定入选项目集合计算连通、覆盖、断点缓解和资金指标。"""

    selected = {project["project_id"]: project for project in projects
                if project["project_id"] in set(selected_ids)}
    corridor_by_id = {corridor["corridor_id"]: corridor for corridor in corridors}
    relieved: set[str] = set()
    for project in selected.values():
        corridor = corridor_by_id.get(project["corridor_id"])
        if corridor is None:
            continue
        relieved.update(bottleneck_id for bottleneck_id in project["bottleneck_ids"]
                        if bottleneck_id in corridor["bottlenecks"])
    union_find, adjacent, _edges = _build_network(corridors, relieved)
    # 只登记了节点对、没有落在边上的断点：缓解后直接连通两端。
    for project in selected.values():
        corridor = corridor_by_id.get(project["corridor_id"])
        if corridor is None:
            continue
        for bottleneck_id in project["bottleneck_ids"]:
            point = corridor["bottlenecks"].get(bottleneck_id)
            if point is None or point["edge_id"]:
                continue
            if point["from_node"] and point["to_node"]:
                union_find.union(point["from_node"], point["to_node"])

    backbone_total = backbone_connected = 0
    connected_pairs: list[str] = []
    for corridor in corridors:
        for pair in corridor["backbone_pairs"]:
            left, right = pair.split("|", 1)
            backbone_total += 1
            if union_find.connected(left, right):
                backbone_connected += 1
                connected_pairs.append(f"{corridor['corridor_id']}:{pair}")

    # 网络连通度：以边开放后的最大连通分量覆盖节点比例衡量。
    all_nodes = set(union_find.parent)
    largest = 0
    groups: dict[str, list[str]] = {}
    for node in all_nodes:
        groups.setdefault(union_find.find(node), []).append(node)
    if groups:
        largest = max(len(group) for group in groups.values())
    connectivity_ratio = largest / len(all_nodes) if all_nodes else 1.0

    served_areas: set[str] = set()
    for project in selected.values():
        served_areas.update(project["service_areas"])
    border_total = rural_total = 0
    border_served = rural_served = 0
    for corridor in corridors:
        for gap in corridor["service_gaps"].values():
            if gap["kind"] == "border":
                border_total += 1
                if gap["area_id"] in served_areas:
                    border_served += 1
            elif gap["kind"] == "rural":
                rural_total += 1
                if gap["area_id"] in served_areas:
                    rural_served += 1

    bottleneck_total = sum(len(corridor["bottlenecks"]) for corridor in corridors)
    declared_goals = {goal for project in selected.values() for goal in project["policy_goals"]}
    goal_codes = declared_goals if goal_codes is None else goal_codes
    goal_alignment = (len(declared_goals & goal_codes) / len(declared_goals)
                      if declared_goals else 0.0)

    yearly_investment: dict[str, float] = {year: 0.0 for year in rules["budgets"]}
    total_investment = 0.0
    for project in selected.values():
        for year, amount in project["investments"].items():
            yearly_investment[year] = yearly_investment.get(year, 0.0) + amount
            total_investment += amount
    budget_breaches = []
    for year, cap in rules["budgets"].items():
        spent = yearly_investment.get(year, 0.0)
        if spent > cap + 1e-9:
            budget_breaches.append({"year": year, "spent": round(spent, 2),
                                    "cap": cap, "over": round(spent - cap, 2)})

    weights = rules["weights"]
    score = (
        weights["backbone"] * (backbone_connected / backbone_total if backbone_total else 0.0)
        + weights["connectivity"] * connectivity_ratio
        + weights["border_coverage"] * (border_served / border_total if border_total else 0.0)
        + weights["rural_coverage"] * (rural_served / rural_total if rural_total else 0.0)
        + weights["bottleneck_relief"] * (len(relieved) / bottleneck_total if bottleneck_total else 0.0)
        + weights["policy_alignment"] * goal_alignment
    )

    return {
        "selected_project_ids": sorted(selected),
        "project_count": len(selected),
        "total_investment": round(total_investment, 2),
        "yearly_investment": {year: round(amount, 2) for year, amount in sorted(yearly_investment.items())},
        "budget_breaches": budget_breaches,
        "within_budget": not budget_breaches,
        "backbone_connected_pairs": backbone_connected,
        "backbone_total_pairs": backbone_total,
        "backbone_ratio": round(backbone_connected / backbone_total, 6) if backbone_total else 1.0,
        "newly_connected_pairs": sorted(connected_pairs),
        "connectivity_ratio": round(connectivity_ratio, 6),
        "border_areas_served": border_served,
        "border_gap_total": border_total,
        "border_coverage": round(border_served / border_total, 6) if border_total else 1.0,
        "rural_areas_served": rural_served,
        "rural_gap_total": rural_total,
        "rural_coverage": round(rural_served / rural_total, 6) if rural_total else 1.0,
        "bottlenecks_relieved": len(relieved),
        "bottleneck_total": bottleneck_total,
        "bottleneck_ratio": round(len(relieved) / bottleneck_total, 6) if bottleneck_total else 1.0,
        "relieved_bottleneck_ids": sorted(relieved),
        "policy_alignment": round(goal_alignment, 6),
        "score": round(score, 6),
    }


# ---------------------------------------------------------------------------
# 预算挤占与推荐时序
# ---------------------------------------------------------------------------


def detect_budget_competition(projects: list[dict[str, Any]], rules: dict[str, Any]) -> list[dict[str, Any]]:
    """找出同一预算年度内候选项目总额超过年度上限的年度。"""

    demands: dict[str, list[tuple[str, float]]] = {}
    for project in projects:
        for year, amount in project["investments"].items():
            demands.setdefault(year, []).append((project["project_id"], amount))
    result = []
    for year, entries in sorted(demands.items()):
        total = sum(amount for _id, amount in entries)
        cap = rules["budgets"].get(year)
        if cap is not None and total > cap + 1e-9:
            result.append({
                "year": year,
                "cap": cap,
                "demand": round(total, 2),
                "over": round(total - cap, 2),
                "project_ids": sorted(project_id for project_id, _ in entries),
            })
    return result


def _project_standalone_score(project: dict[str, Any], corridors_by_id: dict[str, dict[str, Any]],
                              rules: dict[str, Any]) -> float:
    corridor = corridors_by_id.get(project["corridor_id"])
    bottleneck_total = len(corridor["bottlenecks"]) if corridor else 0
    relieved = len([b for b in project["bottleneck_ids"]
                    if corridor and b in corridor["bottlenecks"]])
    border_total = len([g for g in (corridor["service_gaps"].values() if corridor else [])
                        if g["kind"] == "border"])
    rural_total = len([g for g in (corridor["service_gaps"].values() if corridor else [])
                       if g["kind"] == "rural"])
    border_hit = len([a for a in project["service_areas"]
                      if corridor and a in corridor["service_gaps"]
                      and corridor["service_gaps"][a]["kind"] == "border"])
    rural_hit = len([a for a in project["service_areas"]
                     if corridor and a in corridor["service_gaps"]
                     and corridor["service_gaps"][a]["kind"] == "rural"])
    weights = rules["weights"]
    value = (
        weights["bottleneck_relief"] * (relieved / bottleneck_total if bottleneck_total else 0.0)
        + weights["border_coverage"] * (border_hit / border_total if border_total else 0.0)
        + weights["rural_coverage"] * (rural_hit / rural_total if rural_total else 0.0)
        + weights["policy_alignment"] * (1.0 if project["policy_goals"] else 0.0)
    )
    total_cost = sum(project["investments"].values())
    return value + 1e-6 * project["priority"] - 1e-9 * total_cost


def recommend_schedule(corridors: list[dict[str, Any]], projects: list[dict[str, Any]],
                       rules: dict[str, Any], conflicts: dict[str, Any],
                       goal_codes: set[str] | None = None) -> dict[str, Any]:
    """生成可解释建设时序：按单位效益排序，逐年在预算内排入且尊重前置。"""

    corridors_by_id = {corridor["corridor_id"]: corridor for corridor in corridors}
    missing_targets = {entry["missing_prerequisite_id"] for entry in conflicts["dependency"]["missing"]}
    in_cycle: set[str] = set()
    for cycle in conflicts["dependency"]["cycles"]:
        in_cycle.update(cycle)
    blocked = missing_targets | in_cycle
    eligible = [p for p in projects if p["project_id"] not in blocked]
    ranked = sorted(eligible, key=lambda p: (-_project_standalone_score(p, corridors_by_id, rules),
                                             p["benefit_year"], p["project_id"]))

    selected: list[str] = []
    remaining_budget = dict(rules["budgets"])
    schedule: list[dict[str, Any]] = []
    deferred: dict[str, str] = {}
    pending = list(ranked)

    def prerequisites_ready(project: dict[str, Any]) -> bool:
        return all(prerequisite in selected for prerequisite in project["prerequisite_project_ids"])

    progress = True
    while progress and pending:
        progress = False
        rest: list[dict[str, Any]] = []
        for project in pending:
            if not prerequisites_ready(project):
                rest.append(project)
                continue
            affordable = True
            charge_years: dict[str, float] = {}
            for year, amount in project["investments"].items():
                if remaining_budget.get(year, 0.0) + 1e-9 < amount:
                    affordable = False
                    break
                charge_years[year] = amount
            if affordable:
                for year, amount in charge_years.items():
                    remaining_budget[year] = remaining_budget.get(year, 0.0) - amount
                selected.append(project["project_id"])
                schedule.append({
                    "project_id": project["project_id"],
                    "year": project["benefit_year"],
                    "reason": "score" if _project_standalone_score(project, corridors_by_id, rules) > 0
                    else "priority_fill",
                })
                progress = True
            else:
                deferred[project["project_id"]] = "budget_exhausted"
                rest.append(project)
        pending = rest

    for project in pending:
        deferred.setdefault(project["project_id"], "prerequisite_not_scheduled")
    for project_id in blocked:
        if project_id in {p["project_id"] for p in projects}:
            deferred.setdefault(project_id, "dependency_conflict")

    schedule.sort(key=lambda item: (item["year"], item["project_id"]))
    metrics = compute_metrics(corridors, projects, selected, rules, goal_codes)
    return {
        "decisions": [{"project_id": item["project_id"], "action": "schedule",
                       "year": item["year"], "reason": item["reason"]} for item in schedule]
        + [{"project_id": project_id, "action": "defer", "year": None, "reason": reason}
           for project_id, reason in sorted(deferred.items())],
        "schedule": schedule,
        "metrics": metrics,
        "remaining_budget": {year: round(amount, 2) for year, amount in sorted(remaining_budget.items())},
    }


def evaluate_snapshot(snapshot: dict[str, Any], rules: dict[str, Any],
                      selected_ids: list[str] | None = None) -> dict[str, Any]:
    """对一份冻结快照执行完整评价，是复算接口的统一入口。"""

    corridors = [normalize_corridor(item) for item in snapshot["corridors"]]
    projects = [normalize_project(item) for item in snapshot["projects"]]
    goals = snapshot.get("policy_goals", [])
    rules = normalize_rules(rules)
    goal_codes = {goal["goal_code"] for goal in goals}
    overlaps = detect_spatial_overlap(projects)
    dependency = detect_dependency_conflicts(projects)
    competition = detect_budget_competition(projects, rules)
    baseline = _baseline_metrics(corridors)
    if selected_ids is None:
        recommendation = recommend_schedule(corridors, projects, rules,
                                            {"dependency": dependency}, goal_codes)
        decisions = recommendation["decisions"]
        metrics = recommendation["metrics"]
    else:
        decisions = [{"project_id": project_id, "action": "schedule",
                      "year": next((p["benefit_year"] for p in projects
                                   if p["project_id"] == project_id), None),
                      "reason": "manual"} for project_id in selected_ids]
        metrics = compute_metrics(corridors, projects, selected_ids, rules, goal_codes)
        recommendation = None
    return {
        "rules_hash": digest(rules),
        "baseline": baseline,
        "spatial_overlaps": overlaps,
        "dependency_conflicts": dependency,
        "budget_competition": competition,
        "decisions": decisions,
        "metrics": metrics,
    }
