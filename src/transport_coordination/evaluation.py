"""通道项目组合评价引擎。

本模块只依赖入参的冻结数据束（bundle），不访问数据库与时钟，
因此对同一冻结版本必然得到同一结果，支持任一历史版本的离线复算。
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from .audit import canonical_json, digest

DEFAULT_WEIGHTS = {
    "connectivity": 0.30,
    "skeleton": 0.30,
    "coverage": 0.30,
    "gap": 0.10,
}
DEFAULT_BORDER_WEIGHT = 1.0
DEFAULT_RURAL_WEIGHT = 0.6


def _numbers(value: Any, field: str) -> dict[int, float]:
    if not isinstance(value, dict) or not value:
        raise ValueError(f"{field} 必须是非空的年度映射")
    result: dict[int, float] = {}
    for raw_year, amount in value.items():
        try:
            year = int(raw_year)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field} 的年度必须是整数") from exc
        if not isinstance(amount, (int, float)) or isinstance(amount, bool) or amount < 0:
            raise ValueError(f"{field} 的投资额必须是非负数字")
        result[year] = float(amount)
    return dict(sorted(result.items()))


def normalize_rules(raw: dict[str, Any] | None) -> dict[str, Any]:
    """补齐并校验评价规则。"""

    raw = raw or {}
    budgets = _numbers(raw.get("budgets", {}), "budgets") if raw.get("budgets") else {}
    horizon = raw.get("horizon") or sorted(budgets)
    if not isinstance(horizon, list) or not horizon:
        raise ValueError("horizon 必须是非空年度序列")
    horizon = sorted(int(year) for year in horizon)
    weights = dict(DEFAULT_WEIGHTS)
    given_weights = raw.get("weights") or {}
    if not isinstance(given_weights, dict):
        raise ValueError("weights 必须是对象")
    for key in weights:
        if key in given_weights:
            value = given_weights[key]
            if not isinstance(value, (int, float)) or value < 0:
                raise ValueError(f"weights.{key} 必须是非负数字")
            weights[key] = float(value)
    hubs = raw.get("hubs") or []
    if not isinstance(hubs, list) or not all(isinstance(node, str) and node for node in hubs):
        raise ValueError("hubs 必须是非空节点编号数组")
    required_roles = raw.get("required_roles") or ["admin", "reviewer"]
    if not isinstance(required_roles, list) or not required_roles:
        raise ValueError("required_roles 必须是非空角色数组")
    return {
        "budgets": budgets,
        "horizon": horizon,
        "weights": weights,
        "hubs": list(hubs),
        "required_roles": [str(role) for role in required_roles],
        "border_weight": float(raw.get("border_weight", DEFAULT_BORDER_WEIGHT)),
        "rural_weight": float(raw.get("rural_weight", DEFAULT_RURAL_WEIGHT)),
    }


def _components(open_edges: list[dict[str, Any]]) -> dict[str, int]:
    adjacency: dict[str, set[str]] = defaultdict(set)
    nodes: set[str] = set()
    for edge in open_edges:
        a, b = edge["from_node"], edge["to_node"]
        nodes.update((a, b))
        adjacency[a].add(b)
        adjacency[b].add(a)
    component_of: dict[str, int] = {}
    next_id = 0
    for start in sorted(nodes):
        if start in component_of:
            continue
        stack = [start]
        members: list[str] = []
        while stack:
            current = stack.pop()
            if current in component_of:
                continue
            component_of[current] = next_id
            members.append(current)
            stack.extend(adjacency[current] - component_of.keys())
        next_id += 1
    return component_of


def _graph_state(bundle: dict[str, Any], built_projects: set[str]):
    """返回给定建成项目集合下的图状态。"""

    edges = bundle["edges"]
    upgraded_edges: set[str] = set()
    closed_gaps: set[str] = set()
    for pid in built_projects:
        project = bundle["projects"].get(pid)
        if not project:
            continue
        upgraded_edges.update(project.get("edge_ids", []))
        closed_gaps.update(project.get("gap_ids", []))
    open_edges: list[dict[str, Any]] = []
    standard_edges: set[str] = set()
    for edge in edges:
        upgraded = edge["edge_id"] in upgraded_edges
        is_open = bool(edge["exists"]) or upgraded
        if is_open:
            open_edges.append(edge)
        already_standard = bool(edge["exists"]) and edge["current_capacity"] >= edge["required_capacity"]
        if already_standard or upgraded:
            standard_edges.add(edge["edge_id"])
    return open_edges, standard_edges, closed_gaps, upgraded_edges


def _metrics(bundle: dict[str, Any], built_projects: set[str]) -> dict[str, Any]:
    open_edges, standard_edges, closed_gaps, _ = _graph_state(bundle, built_projects)
    component_of = _components(open_edges)
    rules = bundle["rules"]
    hubs = [node for node in rules["hubs"] if node in component_of]

    # 枢纽间连通：无向图中可互相到达的枢纽对占比。
    if len(rules["hubs"]) >= 2:
        total_pairs = 0
        reachable_pairs = 0
        present = rules["hubs"]
        for i, left in enumerate(present):
            for right in present[i + 1:]:
                total_pairs += 1
                if component_of.get(left) is not None and component_of.get(left) == component_of.get(right):
                    reachable_pairs += 1
        connectivity_ratio = reachable_pairs / total_pairs if total_pairs else 0.0
    elif hubs:
        connectivity_ratio = 1.0
    else:
        connectivity_ratio = 0.0

    # 主骨架：逐走廊判定是否全程贯通（所有边开放且端点同属一个连通分量）并达标。
    corridor_edges: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for edge in bundle["edges"]:
        corridor_edges[edge["corridor_id"]].append(edge)
    open_edge_ids = {edge["edge_id"] for edge in open_edges}
    through_corridors = 0
    standard_edge_total = 0
    for corridor_id, edges in corridor_edges.items():
        corridor_open = [edge for edge in edges if edge["edge_id"] in open_edge_ids]
        connected = _components(corridor_open)
        endpoints = {node for edge in edges for node in (edge["from_node"], edge["to_node"])}
        fully_open = len(corridor_open) == len(edges) and bool(edges)
        single_component = bool(endpoints) and all(node in connected for node in endpoints) and len(
            {connected[node] for node in endpoints}) == 1
        all_standard = all(edge["edge_id"] in standard_edges for edge in edges)
        if fully_open and single_component and all_standard:
            through_corridors += 1
        standard_edge_total += sum(1 for edge in edges if edge["edge_id"] in standard_edges)
    skeleton_ratio = through_corridors / len(corridor_edges) if corridor_edges else 0.0
    standard_edge_ratio = standard_edge_total / len(bundle["edges"]) if bundle["edges"] else 0.0

    # 边境与乡村覆盖：区域内全部节点与枢纽同属一个连通分量。
    hub_components = {component_of[hub] for hub in hubs}
    covered = {"border": [], "rural": [], "other": []}
    for area in bundle["areas"]:
        nodes = area.get("node_ids", [])
        is_covered = bool(nodes) and all(component_of.get(node) in hub_components for node in nodes)
        if is_covered:
            covered.setdefault(area.get("kind", "other"), []).append(area["area_id"])
    totals = {"border": sum(1 for a in bundle["areas"] if a.get("kind") == "border"),
              "rural": sum(1 for a in bundle["areas"] if a.get("kind") == "rural")}
    coverage = {
        "border_covered": len(covered["border"]),
        "border_total": totals["border"],
        "border_ratio": len(covered["border"]) / totals["border"] if totals["border"] else 0.0,
        "rural_covered": len(covered["rural"]),
        "rural_total": totals["rural"],
        "rural_ratio": len(covered["rural"]) / totals["rural"] if totals["rural"] else 0.0,
        "covered_area_ids": sorted(covered["border"] + covered["rural"] + covered["other"]),
    }

    gap_total = len(bundle["gaps"])
    closed_gap_ids = closed_gaps & {gap["gap_id"] for gap in bundle["gaps"]}
    return {
        "connectivity_ratio": round(connectivity_ratio, 6),
        "skeleton_ratio": round(skeleton_ratio, 6),
        "standard_edge_ratio": round(standard_edge_ratio, 6),
        "through_corridors": through_corridors,
        "corridor_total": len(corridor_edges),
        "coverage": coverage,
        "gaps_closed": len(closed_gap_ids),
        "closed_gap_ids": sorted(closed_gap_ids),
        "gap_total": gap_total,
        "gap_ratio": round(len(closed_gap_ids) / gap_total, 6)
        if gap_total else 0.0,
        "reachable_hubs": sorted(hubs),
    }


def _coverage_score(metrics: dict[str, Any], rules: dict[str, Any]) -> float:
    coverage = metrics["coverage"]
    border = coverage["border_ratio"] * rules["border_weight"]
    rural = coverage["rural_ratio"] * rules["rural_weight"]
    denominator = rules["border_weight"] + rules["rural_weight"]
    return round((border + rural) / denominator, 6)


def _weighted_score(bundle: dict[str, Any], built: set[str]) -> tuple[float, dict[str, Any]]:
    metrics = _metrics(bundle, built)
    rules = bundle["rules"]
    weights = rules["weights"]
    parts = {
        "connectivity": metrics["connectivity_ratio"],
        "skeleton": round((metrics["skeleton_ratio"] + metrics["standard_edge_ratio"]) / 2, 6),
        "coverage": _coverage_score(metrics, rules),
        "gap": metrics["gap_ratio"],
    }
    return round(sum(weights[key] * parts[key] for key in weights), 6), parts


def _project_costs(project: dict[str, Any], start_year: int) -> dict[int, float]:
    years = sorted(int(year) for year in project["yearly_investment"])
    values = [project["yearly_investment"][str(year)] if str(year) in project["yearly_investment"]
              else project["yearly_investment"][year] for year in years]
    shift = start_year - years[0]
    return {years[0] + shift + index: amount for index, amount in enumerate(values)}


def _completion_year(project: dict[str, Any], start_year: int) -> int:
    return max(_project_costs(project, start_year))


def evaluate(bundle: dict[str, Any], items: list[dict[str, Any]]) -> dict[str, Any]:
    """对一份组合方案执行完整评价并返回可解释结果。

    items 元素：{project_id, version, action(proceed|delay|exit), target_year?}
    """

    rules = bundle["rules"]
    horizon = rules["horizon"]
    findings: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []

    by_project = {item["project_id"]: item for item in items}
    candidate: dict[str, dict[str, Any]] = {}
    for item in items:
        pid = item["project_id"]
        project = bundle["projects"].get(pid)
        if project is None:
            raise ValueError(f"项目 {pid} 不在冻结快照中")
        if project["version"] != item.get("version"):
            raise ValueError(f"项目 {pid} 的版本与冻结快照不一致")
        action = item.get("action", "proceed")
        if action == "exit":
            excluded.append({"project_id": pid, "reason": "declared_exit"})
            continue
        if action not in ("proceed", "delay"):
            raise ValueError(f"项目 {pid} 的动作 {action} 不支持")
        candidate[pid] = item

    # 空间重叠：不同建设单位就同一区段/瓶颈重复申报。
    edge_owners: dict[str, list[str]] = defaultdict(list)
    gap_owners: dict[str, list[str]] = defaultdict(list)
    for pid, item in candidate.items():
        for edge_id in bundle["projects"][pid].get("edge_ids", []):
            edge_owners[edge_id].append(pid)
        for gap_id in bundle["projects"][pid].get("gap_ids", []):
            gap_owners[gap_id].append(pid)
    seen_pairs: set[tuple[str, str]] = set()
    for edge_id, owners in sorted(edge_owners.items()):
        if len(owners) > 1:
            pair = tuple(sorted(owners))
            if pair not in seen_pairs:
                seen_pairs.add(pair)
                findings.append({"code": "spatial_overlap", "severity": "high", "edges": [edge_id],
                                 "projects": list(pair),
                                 "message": f"项目 {'、'.join(pair)} 对区段 {edge_id} 重复申报"})
    for gap_id, owners in sorted(gap_owners.items()):
        if len(owners) > 1:
            findings.append({"code": "duplicate_gap", "severity": "medium", "gaps": [gap_id],
                             "projects": sorted(owners),
                             "message": f"同一断点 {gap_id} 被 {len(owners)} 个项目重复申报"})

    # 依赖冲突：前置工程退出、未入选或时序无法满足。
    feasible: dict[str, dict[str, Any]] = {}
    for pid, item in candidate.items():
        missing = [pre for pre in bundle["projects"][pid].get("prerequisite_project_ids", [])
                   if pre not in candidate]
        exited = [pre for pre in bundle["projects"][pid].get("prerequisite_project_ids", [])
                  if pre in by_project and by_project[pre].get("action") == "exit"]
        if exited:
            findings.append({"code": "dependency_conflict", "severity": "high", "projects": [pid],
                             "prerequisites": exited,
                             "message": f"项目 {pid} 的前置工程 {'、'.join(exited)} 已退出"})
            excluded.append({"project_id": pid, "reason": "prerequisite_exited"})
        elif missing:
            findings.append({"code": "dependency_conflict", "severity": "high", "projects": [pid],
                             "prerequisites": missing,
                             "message": f"项目 {pid} 的前置工程 {'、'.join(missing)} 未纳入组合"})
            excluded.append({"project_id": pid, "reason": "prerequisite_missing"})
        else:
            feasible[pid] = item

    baseline_metrics = _metrics(bundle, set())
    baseline_score, _ = _weighted_score(bundle, set())

    # 贪心排序：每轮在前置已排定的候选中选边际贡献最大者，保证可解释、可复算。
    scheduled: dict[str, int] = {}
    excluded_ids: set[str] = set()
    yearly_spend: dict[int, float] = defaultdict(float)
    built: set[str] = set()
    ordered: list[dict[str, Any]] = []
    while len(built) + len(excluded_ids) < len(feasible):
        progress = False
        # 前置已被排除的项目级联排除。
        for pid in sorted(feasible):
            if pid in built or pid in excluded_ids:
                continue
            blocked_pre = [pre for pre in bundle["projects"][pid].get("prerequisite_project_ids", [])
                           if pre in excluded_ids]
            if blocked_pre:
                findings.append({"code": "dependency_conflict", "severity": "high", "projects": [pid],
                                 "prerequisites": blocked_pre,
                                 "message": f"项目 {pid} 的前置工程 {'、'.join(blocked_pre)} 无法安排"})
                excluded.append({"project_id": pid, "reason": "prerequisite_unschedulable"})
                excluded_ids.add(pid)
                progress = True
        if progress:
            continue
        choices = []
        for pid, item in feasible.items():
            if pid in built or pid in excluded_ids:
                continue
            prerequisites = bundle["projects"][pid].get("prerequisite_project_ids", [])
            if any(pre not in built for pre in prerequisites):
                continue
            before, _ = _weighted_score(bundle, built)
            after, _ = _weighted_score(bundle, built | {pid})
            choices.append((round(after - before, 9), pid, item))
        if not choices:
            # 剩余项目互为前置依赖形成环。
            for pid in sorted(set(feasible) - built - excluded_ids):
                findings.append({"code": "dependency_cycle", "severity": "high", "projects": [pid],
                                 "message": f"项目 {pid} 的前置关系形成环"})
                excluded.append({"project_id": pid, "reason": "dependency_cycle"})
            break
        for _, pid, item in sorted(choices, key=lambda choice: (-choice[0], choice[1])):
            project = bundle["projects"][pid]
            prerequisites = project.get("prerequisite_project_ids", [])
            earliest = horizon[0]
            if prerequisites:
                earliest = max(_completion_year(bundle["projects"][pre], scheduled[pre])
                               for pre in prerequisites)
            if item.get("action") == "delay":
                target_year = item.get("target_year")
                if target_year is None:
                    raise ValueError(f"延期项目 {pid} 必须给出 target_year")
                earliest = max(earliest, int(target_year))
            assigned = None
            for start_year in range(earliest, horizon[-1] + 1):
                costs = _project_costs(project, start_year)
                if any(year not in horizon for year in costs):
                    continue
                if all(yearly_spend[year] + amount <= rules["budgets"].get(year, float("inf"))
                       for year, amount in costs.items()):
                    assigned = (start_year, costs)
                    break
            if assigned is None:
                findings.append({"code": "budget_squeeze", "severity": "high", "projects": [pid],
                                 "message": f"项目 {pid} 在规划年度与预算约束内无法安排"})
                excluded.append({"project_id": pid, "reason": "no_budget_window"})
                excluded_ids.add(pid)
                break
            start_year, costs = assigned
            for year, amount in costs.items():
                yearly_spend[year] += amount
            before_metrics = _metrics(bundle, built)
            built.add(pid)
            after_metrics = _metrics(bundle, built)
            reasons = _reasons(bundle, pid, before_metrics, after_metrics, prerequisites, scheduled)
            after, _ = _weighted_score(bundle, built)
            before, _ = _weighted_score(bundle, built - {pid})
            entry = {
                "project_id": pid,
                "name": project.get("name", pid),
                "corridor_id": project.get("corridor_id"),
                "building_unit": project.get("building_unit"),
                "year": start_year,
                "investment": {str(year): round(amount, 6) for year, amount in costs.items()},
                "score": round(after - before, 6),
                "reasons": reasons,
            }
            scheduled[pid] = start_year
            ordered.append(entry)
            break

    for year, budget in sorted(rules["budgets"].items()):
        spend = round(yearly_spend.get(year, 0.0), 6)
        if spend > budget + 1e-9:
            findings.append({"code": "budget_squeeze", "severity": "high", "year": year,
                             "budget": budget, "spend": spend,
                             "message": f"{year} 年度资金 {spend} 超出预算 {budget}"})

    final_metrics = _metrics(bundle, set(scheduled))
    final_score, score_parts = _weighted_score(bundle, set(scheduled))

    # 退出项目造成的缺口与覆盖损失说明。
    for item in items:
        if item.get("action") != "exit":
            continue
        pid = item["project_id"]
        project = bundle["projects"][pid]
        findings.append({"code": "exit_impact", "severity": "info", "projects": [pid],
                         "gaps": list(project.get("gap_ids", [])),
                         "message": f"项目 {pid} 退出后断点 {('、'.join(project.get('gap_ids', [])) or '无')} 保留"})

    total_investment = round(sum(cost for entry in ordered
                                 for cost in entry["investment"].values()), 6)
    schedule = sorted(ordered, key=lambda entry: (entry["year"], entry["project_id"]))
    within_budget = all(
        yearly_spend.get(year, 0.0) <= budget + 1e-9 for year, budget in rules["budgets"].items())
    blocking = [finding for finding in findings if finding["severity"] == "high"]
    result = {
        "baseline": baseline_metrics,
        "baseline_score": baseline_score,
        "metrics": final_metrics,
        "score_parts": score_parts,
        "total_score": final_score,
        "total_score_gain": round(final_score - baseline_score, 6),
        "schedule": schedule,
        "excluded": sorted(excluded, key=lambda item: item["project_id"]),
        "findings": findings,
        "yearly_spend": {str(year): round(yearly_spend.get(year, 0.0), 6)
                         for year in rules["horizon"]},
        "total_investment": total_investment,
        "within_budget": within_budget,
        "feasible": not blocking,
    }
    result["metrics_hash"] = digest({
        "metrics": result["metrics"],
        "schedule": [{"project_id": e["project_id"], "year": e["year"],
                      "score": e["score"]} for e in schedule],
        "within_budget": within_budget,
    })
    return result


def _reasons(bundle, pid, before, after, prerequisites, scheduled) -> dict[str, Any]:
    project = bundle["projects"][pid]
    new_areas = sorted(set(after["coverage"]["covered_area_ids"])
                       - set(before["coverage"]["covered_area_ids"]))
    area_kinds = {area["area_id"]: area.get("kind") for area in bundle["areas"]}
    corridor_before_through = before["through_corridors"]
    corridor_after_through = after["through_corridors"]
    newly_closed = sorted(set(after.get("closed_gap_ids", []))
                         - set(before.get("closed_gap_ids", [])))
    reasons = {
        "gaps_addressed": newly_closed,
        "declared_gap_ids": list(project.get("gap_ids", [])),
        "edges_upgraded": list(project.get("edge_ids", [])),
        "newly_covered_areas": new_areas,
        "newly_covered_border": [a for a in new_areas if area_kinds.get(a) == "border"],
        "newly_covered_rural": [a for a in new_areas if area_kinds.get(a) == "rural"],
        "completes_skeleton_corridor": corridor_after_through > corridor_before_through,
        "connectivity_gain": round(after["connectivity_ratio"] - before["connectivity_ratio"], 6),
        "prerequisites": [{"project_id": pre, "year": scheduled[pre]} for pre in prerequisites],
    }
    return reasons


def bundle_hash(bundle: dict[str, Any]) -> str:
    """计算冻结数据束与评价规则的联合摘要。"""

    material = {
        "nodes": bundle["nodes"],
        "edges": bundle["edges"],
        "gaps": bundle["gaps"],
        "areas": bundle["areas"],
        "target": bundle.get("target"),
        "projects": bundle["projects"],
        "rules": bundle["rules"],
    }
    return digest(canonical_json(material))
