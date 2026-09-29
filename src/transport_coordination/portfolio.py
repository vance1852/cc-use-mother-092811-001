"""通道项目组合决策服务。

在基础服务的权限、幂等、事务与审计链之上，提供走廊拓扑、断点、服务缺口、
项目版本谱系、评审轮冻结、组合评价与竞争会签能力。所有评价只读入冻结快照，
因此历史版本可随时按相同规则复算。
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from .audit import append_event, canonical_json, digest
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .evaluation import bundle_hash, evaluate, normalize_rules
from .service import DomainService

TERMINAL_STATUSES = {"exited", "superseded_split", "superseded_merged"}
POOL_STATUSES = {"registered", "delayed"}
AREA_KINDS = {"border", "rural"}
ADJUST_ACTIONS = {"revise", "split", "merge", "delay", "exit"}
SIGN_DECISIONS = {"approve", "reject"}
SNAPSHOT_KINDS = ("nodes", "edges", "gaps", "areas", "projects", "target", "rules")


class PortfolioService(DomainService):
    """协调通道组合登记、冻结、评价与会签。"""

    # ------------------------------------------------------------------ 登记

    def register_node(self, *, request_id: str, actor_id: str, node_id: str,
                      name: str, kind: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "node_id": node_id, "name": name, "kind": kind}

        def create(connection) -> tuple[str, str, dict[str, Any]]:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            clean_node_id = self._identifier(node_id, "node_id")
            clean_name = self._text(name, "name")
            clean_kind = self._text(kind, "kind", 40)
            try:
                connection.execute(
                    "INSERT INTO corridor_nodes(node_id,name,kind,payload_hash,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (clean_node_id, clean_name, clean_kind,
                     digest({"name": clean_name, "kind": clean_kind}), actor_id, self._now()),
                )
            except Exception as exc:
                raise ConflictError("节点编号已经存在") from exc
            append_event(connection, actor_id=actor_id, action="node.registered",
                         resource_type="node", resource_id=clean_node_id,
                         detail={"name": clean_name, "kind": clean_kind}, occurred_at=self._now())
            return "node", clean_node_id, {"node_id": clean_node_id}

        with self.database.transaction(immediate=True) as connection:
            return self._receipt(connection, request_id=request_id, action="register_node",
                                 payload=payload, create=lambda: create(connection))

    def register_edge(self, *, request_id: str, actor_id: str, edge_id: str, corridor_id: str,
                      from_node: str, to_node: str, exists: bool,
                      current_capacity: float, required_capacity: float) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "edge_id": edge_id, "corridor_id": corridor_id,
                   "from_node": from_node, "to_node": to_node, "exists": exists,
                   "current_capacity": current_capacity, "required_capacity": required_capacity}

        def create(connection) -> tuple[str, str, dict[str, Any]]:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            clean_edge_id = self._identifier(edge_id, "edge_id")
            clean_corridor = self._identifier(corridor_id, "corridor_id")
            clean_from = self._identifier(from_node, "from_node")
            clean_to = self._identifier(to_node, "to_node")
            clean_current = self._capacity(current_capacity, "current_capacity")
            clean_required = self._capacity(required_capacity, "required_capacity")
            for node in (clean_from, clean_to):
                if connection.execute("SELECT 1 FROM corridor_nodes WHERE node_id=?", (node,)).fetchone() is None:
                    raise NotFoundError(f"节点 {node} 不存在")
            data = {"corridor_id": clean_corridor, "from_node": clean_from, "to_node": clean_to,
                    "exists": bool(exists), "current_capacity": clean_current,
                    "required_capacity": clean_required}
            try:
                connection.execute(
                    "INSERT INTO corridor_edges(edge_id,corridor_id,from_node,to_node,is_open,"
                    "current_capacity,required_capacity,payload_hash,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (clean_edge_id, clean_corridor, clean_from, clean_to, 1 if exists else 0,
                     clean_current, clean_required, digest(data), actor_id, self._now()),
                )
            except Exception as exc:
                raise ConflictError("区段编号已经存在") from exc
            append_event(connection, actor_id=actor_id, action="edge.registered",
                         resource_type="edge", resource_id=clean_edge_id,
                         detail={"corridor_id": clean_corridor, "from_node": clean_from,
                                 "to_node": clean_to, "exists": bool(exists)},
                         occurred_at=self._now())
            return "edge", clean_edge_id, {"edge_id": clean_edge_id}

        with self.database.transaction(immediate=True) as connection:
            return self._receipt(connection, request_id=request_id, action="register_edge",
                                 payload=payload, create=lambda: create(connection))

    def register_gap(self, *, request_id: str, actor_id: str, gap_id: str, corridor_id: str,
                     edge_id: str, current_capacity: float, required_capacity: float) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "gap_id": gap_id, "corridor_id": corridor_id,
                   "edge_id": edge_id, "current_capacity": current_capacity,
                   "required_capacity": required_capacity}

        def create(connection) -> tuple[str, str, dict[str, Any]]:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            clean_gap = self._identifier(gap_id, "gap_id")
            clean_corridor = self._identifier(corridor_id, "corridor_id")
            clean_edge = self._identifier(edge_id, "edge_id")
            clean_current = self._capacity(current_capacity, "current_capacity")
            clean_required = self._capacity(required_capacity, "required_capacity")
            edge = connection.execute("SELECT * FROM corridor_edges WHERE edge_id=?", (clean_edge,)).fetchone()
            if edge is None:
                raise NotFoundError("区段不存在")
            if edge["corridor_id"] != clean_corridor:
                raise ValidationError("断点所属走廊必须与区段一致")
            if clean_current >= clean_required:
                raise ValidationError("断点现有能力必须低于目标能力")
            data = {"corridor_id": clean_corridor, "edge_id": clean_edge,
                    "current_capacity": clean_current, "required_capacity": clean_required}
            try:
                connection.execute(
                    "INSERT INTO gaps(gap_id,corridor_id,edge_id,current_capacity,required_capacity,"
                    "payload_hash,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (clean_gap, clean_corridor, clean_edge, clean_current, clean_required,
                     digest(data), actor_id, self._now()),
                )
            except Exception as exc:
                raise ConflictError("断点编号已经存在") from exc
            append_event(connection, actor_id=actor_id, action="gap.registered",
                         resource_type="gap", resource_id=clean_gap,
                         detail={"corridor_id": clean_corridor, "edge_id": clean_edge},
                         occurred_at=self._now())
            return "gap", clean_gap, {"gap_id": clean_gap}

        with self.database.transaction(immediate=True) as connection:
            return self._receipt(connection, request_id=request_id, action="register_gap",
                                 payload=payload, create=lambda: create(connection))

    def register_service_area(self, *, request_id: str, actor_id: str, area_id: str,
                              name: str, kind: str, node_ids: list[str]) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "area_id": area_id, "name": name,
                   "kind": kind, "node_ids": node_ids}

        def create(connection) -> tuple[str, str, dict[str, Any]]:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            clean_area = self._identifier(area_id, "area_id")
            clean_name = self._text(name, "name")
            if kind not in AREA_KINDS:
                raise ValidationError("服务缺口类型必须是 border 或 rural")
            if not isinstance(node_ids, list) or not node_ids:
                raise ValidationError("node_ids 必须是非空数组")
            clean_nodes = []
            for raw_node in node_ids:
                node = self._identifier(raw_node, "node_ids")
                if connection.execute("SELECT 1 FROM corridor_nodes WHERE node_id=?", (node,)).fetchone() is None:
                    raise NotFoundError(f"节点 {node} 不存在")
                if node not in clean_nodes:
                    clean_nodes.append(node)
            data = {"name": clean_name, "kind": kind, "node_ids": clean_nodes}
            try:
                connection.execute(
                    "INSERT INTO service_areas(area_id,name,kind,node_ids_json,payload_hash,"
                    "created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                    (clean_area, clean_name, kind, canonical_json(clean_nodes), digest(data),
                     actor_id, self._now()),
                )
            except Exception as exc:
                raise ConflictError("服务区域编号已经存在") from exc
            append_event(connection, actor_id=actor_id, action="service_area.registered",
                         resource_type="service_area", resource_id=clean_area,
                         detail={"name": clean_name, "kind": kind, "node_ids": clean_nodes},
                         occurred_at=self._now())
            return "service_area", clean_area, {"area_id": clean_area}

        with self.database.transaction(immediate=True) as connection:
            return self._receipt(connection, request_id=request_id, action="register_service_area",
                                 payload=payload, create=lambda: create(connection))

    def register_policy_target(self, *, request_id: str, actor_id: str, target_id: str,
                               title: str, goals: dict[str, Any]) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "target_id": target_id, "title": title, "goals": goals}

        def create(connection) -> tuple[str, str, dict[str, Any]]:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            clean_target = self._identifier(target_id, "target_id")
            clean_title = self._text(title, "title")
            if not isinstance(goals, dict) or not goals:
                raise ValidationError("goals 必须是非空对象")
            data = {"title": clean_title, "goals": goals}
            try:
                connection.execute(
                    "INSERT INTO policy_targets(target_id,title,payload_json,payload_hash,"
                    "created_by,created_at) VALUES(?,?,?,?,?,?)",
                    (clean_target, clean_title, canonical_json(data), digest(data),
                     actor_id, self._now()),
                )
            except Exception as exc:
                raise ConflictError("政策目标编号已经存在") from exc
            append_event(connection, actor_id=actor_id, action="policy_target.registered",
                         resource_type="policy_target", resource_id=clean_target,
                         detail={"title": clean_title}, occurred_at=self._now())
            return "policy_target", clean_target, {"target_id": clean_target}

        with self.database.transaction(immediate=True) as connection:
            return self._receipt(connection, request_id=request_id, action="register_policy_target",
                                 payload=payload, create=lambda: create(connection))

    # ------------------------------------------------------------------ 项目

    def register_project(self, *, request_id: str, actor_id: str, project_id: str, name: str,
                         corridor_id: str, building_unit: str, edge_ids: list[str],
                         gap_ids: list[str], prerequisite_project_ids: list[str],
                         yearly_investment: dict[str, float],
                         policy_target_ids: list[str] | None = None) -> dict[str, Any]:
        policy_target_ids = policy_target_ids or []
        payload = {"actor_id": actor_id, "project_id": project_id, "name": name,
                   "corridor_id": corridor_id, "building_unit": building_unit,
                   "edge_ids": edge_ids, "gap_ids": gap_ids,
                   "prerequisite_project_ids": prerequisite_project_ids,
                   "yearly_investment": yearly_investment,
                   "policy_target_ids": policy_target_ids}

        def create(connection) -> tuple[str, str, dict[str, Any]]:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            clean_pid = self._identifier(project_id, "project_id")
            data = self._validate_project(
                connection, project_id=clean_pid, name=name, corridor_id=corridor_id,
                building_unit=building_unit, edge_ids=edge_ids, gap_ids=gap_ids,
                prerequisite_project_ids=prerequisite_project_ids,
                yearly_investment=yearly_investment, policy_target_ids=policy_target_ids)
            if connection.execute("SELECT 1 FROM projects WHERE project_id=?", (clean_pid,)).fetchone():
                raise ConflictError("项目编号已经存在，调整请使用项目版本接口")
            self._insert_project(connection, clean_pid, 1, "registered", data, actor_id)
            append_event(connection, actor_id=actor_id, action="project.registered",
                         resource_type="project", resource_id=clean_pid,
                         detail={"version": 1, "name": data["name"],
                                 "corridor_id": data["corridor_id"],
                                 "building_unit": data["building_unit"]},
                         occurred_at=self._now())
            return "project", clean_pid, {"project_id": clean_pid, "version": 1}

        with self.database.transaction(immediate=True) as connection:
            return self._receipt(connection, request_id=request_id, action="register_project",
                                 payload=payload, create=lambda: create(connection))

    def adjust_project(self, *, request_id: str, actor_id: str, project_id: str, action: str,
                       name: str | None = None, corridor_id: str | None = None,
                       building_unit: str | None = None, edge_ids: list[str] | None = None,
                       gap_ids: list[str] | None = None,
                       prerequisite_project_ids: list[str] | None = None,
                       yearly_investment: dict[str, float] | None = None,
                       policy_target_ids: list[str] | None = None,
                       target_year: int | None = None, reason: str = "",
                       children: list[dict[str, Any]] | None = None,
                       source_project_ids: list[str] | None = None,
                       child_project_id: str | None = None) -> dict[str, Any]:
        """登记项目拆分、合并、延期、退出或常规修订，全部沿版本留痕。"""

        payload = {"actor_id": actor_id, "project_id": project_id, "action": action,
                   "name": name, "corridor_id": corridor_id, "building_unit": building_unit,
                   "edge_ids": edge_ids, "gap_ids": gap_ids,
                   "prerequisite_project_ids": prerequisite_project_ids,
                   "yearly_investment": yearly_investment,
                   "policy_target_ids": policy_target_ids, "target_year": target_year,
                   "reason": reason, "children": children,
                   "source_project_ids": source_project_ids,
                   "child_project_id": child_project_id}
        if action not in ADJUST_ACTIONS:
            raise ValidationError(f"调整动作必须是 {'/'.join(sorted(ADJUST_ACTIONS))} 之一")

        def work(connection) -> tuple[str, str, dict[str, Any]]:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            clean_reason = self._text(reason or " ", "reason", 500).strip()
            updates = {key: value for key, value in (
                ("name", name), ("corridor_id", corridor_id),
                ("building_unit", building_unit), ("edge_ids", edge_ids),
                ("gap_ids", gap_ids),
                ("prerequisite_project_ids", prerequisite_project_ids),
                ("yearly_investment", yearly_investment),
                ("policy_target_ids", policy_target_ids),
            ) if value is not None}
            if action == "split":
                result = self._do_split(connection, actor_id, project_id, children or [], clean_reason)
            elif action == "merge":
                result = self._do_merge(connection, actor_id, project_id,
                                        source_project_ids or [], updates, clean_reason)
            else:
                result = self._do_revision(connection, actor_id, project_id, action,
                                           target_year=target_year, reason=clean_reason,
                                           updates=updates)
            return "project_adjustment", result["project_id"], result

        with self.database.transaction(immediate=True) as connection:
            return self._receipt(connection, request_id=request_id, action="adjust_project",
                                 payload=payload, create=lambda: work(connection))

    def _do_revision(self, connection, actor_id: str, project_id: str, action: str, *,
                     target_year: int | None, reason: str, updates: dict[str, Any]) -> dict[str, Any]:
        row = self._latest_row(connection, project_id)
        if row is None:
            raise NotFoundError("项目不存在")
        if row["status"] in TERMINAL_STATUSES:
            raise ConflictError(f"项目已处于终态 {row['status']}，不能继续调整")
        previous = json.loads(row["payload_json"])
        version = row["version"] + 1
        if action in ("revise", "delay"):
            data = {**previous, **updates}
            data = self._validate_project(connection, **data)
            status = "delayed" if action == "delay" else (
                "delayed" if row["status"] == "delayed" else "registered")
            if action == "delay":
                if not isinstance(target_year, int):
                    raise ValidationError("延期必须给出整数 target_year")
                data["target_year"] = target_year
            elif target_year is not None:
                data["target_year"] = target_year
        else:  # exit
            data = previous
            status = "exited"
        self._insert_project(connection, project_id, version, status, data, actor_id)
        impacts = self._record_impacts(connection, actor_id, project_id, row["version"],
                                       action, reason, target_year=target_year)
        connection.execute(
            "INSERT INTO project_lineage(lineage_id,relation,parent_project_id,parent_version,"
            "child_project_id,child_version,round_id,detail_json,impact_json,created_by,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, action, project_id, version - 1, project_id, version, None,
             canonical_json({"reason": reason, "status": status}), canonical_json(impacts),
             actor_id, self._now()),
        )
        append_event(connection, actor_id=actor_id, action=f"project.{action}",
                     resource_type="project", resource_id=project_id,
                     detail={"version": version, "status": status, "reason": reason,
                             "affected_rounds": [item["round_id"] for item in impacts]},
                     occurred_at=self._now())
        return {"project_id": project_id, "version": version, "status": status,
                "impacts": impacts}

    def _do_split(self, connection, actor_id: str, project_id: str,
                  children: list[dict[str, Any]], reason: str) -> dict[str, Any]:
        row = self._latest_row(connection, project_id)
        if row is None:
            raise NotFoundError("项目不存在")
        if len(children) < 2:
            raise ValidationError("拆分至少要给出两个子项目")
        if row["status"] in TERMINAL_STATUSES:
            raise ConflictError("项目已处于终态，不能继续调整")
        parent_version = row["version"] + 1
        parent_data = json.loads(row["payload_json"])
        self._insert_project(connection, project_id, parent_version,
                             "superseded_split", parent_data, actor_id)
        child_ids = []
        for spec in children:
            child_id = self._identifier(spec.get("project_id", ""), "children.project_id")
            if connection.execute("SELECT 1 FROM projects WHERE project_id=?", (child_id,)).fetchone():
                raise ConflictError(f"子项目编号 {child_id} 已经存在")
            data = self._validate_project(connection, **{k: v for k, v in spec.items()
                                                         if k != "project_id"})
            self._insert_project(connection, child_id, 1, "registered", data, actor_id)
            child_ids.append(child_id)
            connection.execute(
                "INSERT INTO project_lineage(lineage_id,relation,parent_project_id,parent_version,"
                "child_project_id,child_version,round_id,detail_json,impact_json,created_by,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (uuid.uuid4().hex, "split", project_id, parent_version, child_id, 1, None,
                 canonical_json({"reason": reason}), canonical_json([]), actor_id, self._now()),
            )
        impacts = self._record_impacts(connection, actor_id, project_id, row["version"],
                                       "split", reason, child_ids=child_ids)
        append_event(connection, actor_id=actor_id, action="project.split",
                     resource_type="project", resource_id=project_id,
                     detail={"version": parent_version, "children": child_ids,
                             "reason": reason,
                             "affected_rounds": [item["round_id"] for item in impacts]},
                     occurred_at=self._now())
        return {"project_id": project_id, "version": parent_version,
                "status": "superseded_split", "children": child_ids, "impacts": impacts}

    def _do_merge(self, connection, actor_id: str, new_project_id: str | None,
                  source_project_ids: list[str], spec: dict[str, Any],
                  reason: str) -> dict[str, Any]:
        if not new_project_id:
            raise ValidationError("合并必须给出新 project_id")
        new_project_id = self._identifier(new_project_id, "project_id")
        if len(source_project_ids) < 2:
            raise ValidationError("合并至少要给出两个源项目")
        if connection.execute("SELECT 1 FROM projects WHERE project_id=?", (new_project_id,)).fetchone():
            raise ConflictError("合并后的项目编号已经存在")
        sources = []
        for source in source_project_ids:
            source = self._identifier(source, "source_project_ids")
            row = self._latest_row(connection, source)
            if row is None:
                raise NotFoundError(f"源项目 {source} 不存在")
            if row["status"] in TERMINAL_STATUSES:
                raise ConflictError(f"源项目 {source} 已处于终态")
            sources.append((source, row))
        data = self._validate_project(
            connection, project_id=new_project_id, **spec)
        self._insert_project(connection, new_project_id, 1, "registered", data, actor_id)
        all_impacts = []
        for source, row in sources:
            new_version = row["version"] + 1
            self._insert_project(connection, source, new_version, "superseded_merged",
                                 json.loads(row["payload_json"]), actor_id)
            connection.execute(
                "INSERT INTO project_lineage(lineage_id,relation,parent_project_id,parent_version,"
                "child_project_id,child_version,round_id,detail_json,impact_json,created_by,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (uuid.uuid4().hex, "merge", source, new_version, new_project_id, 1, None,
                 canonical_json({"reason": reason, "sources": source_project_ids}),
                 canonical_json([]), actor_id, self._now()),
            )
            all_impacts.extend(self._record_impacts(
                connection, actor_id, source, row["version"], "merge", reason,
                child_ids=[new_project_id]))
        append_event(connection, actor_id=actor_id, action="project.merge",
                     resource_type="project", resource_id=new_project_id,
                     detail={"sources": [item[0] for item in sources], "reason": reason,
                             "affected_rounds": sorted({item["round_id"] for item in all_impacts})},
                     occurred_at=self._now())
        return {"project_id": new_project_id, "version": 1, "status": "registered",
                "sources": [item[0] for item in sources], "impacts": all_impacts}

    def _record_impacts(self, connection, actor_id: str, project_id: str, frozen_version: int,
                        relation: str, reason: str, *, target_year: int | None = None,
                        child_ids: list[str] | None = None) -> list[dict[str, Any]]:
        """对所有已生效组合复算该调整的影响，沿版本保留。"""

        child_ids = child_ids or []
        impacts = []
        rows = connection.execute(
            "SELECT p.proposal_id,p.round_id,p.items_json,p.metrics_json FROM proposals p "
            "JOIN rounds r ON r.round_id=p.round_id "
            "WHERE r.effective_proposal_id=p.proposal_id ORDER BY p.proposal_id"
        ).fetchall()
        for proposal in rows:
            items = json.loads(proposal["items_json"])
            hit = next((item for item in items if item["project_id"] == project_id), None)
            if hit is None or hit.get("version") != frozen_version:
                continue
            bundle = self._bundle_for_round(connection, proposal["round_id"])
            modified = json.loads(proposal["items_json"])
            for item in modified:
                if item["project_id"] != project_id:
                    continue
                if relation == "delay":
                    item["action"] = "delay"
                    item["target_year"] = target_year
                else:
                    item["action"] = "exit"
            rescored = evaluate(bundle, modified)
            original = json.loads(proposal["metrics_json"])
            original_entry = next((entry for entry in original["schedule"]
                                   if entry["project_id"] == project_id), None)
            new_entry = next((entry for entry in rescored["schedule"]
                              if entry["project_id"] == project_id), None)
            impacts.append({
                "round_id": proposal["round_id"],
                "proposal_id": proposal["proposal_id"],
                "relation": relation,
                "child_project_ids": child_ids,
                "frozen_version": frozen_version,
                "reason": reason,
                "original_entry": original_entry,
                "rescheduled_entry": new_entry,
                "score_before": original["total_score"],
                "score_after": rescored["total_score"],
                "score_delta": round(rescored["total_score"] - original["total_score"], 6),
                "metric_deltas": {
                    key: round(rescored["metrics"][key] - original["metrics"][key], 6)
                    for key in ("connectivity_ratio", "skeleton_ratio", "gap_ratio")
                    if isinstance(original["metrics"].get(key), (int, float))
                },
                "coverage_after": rescored["metrics"]["coverage"],
            })
        return impacts

    # ------------------------------------------------------------------ 评审轮

    def create_round(self, *, request_id: str, actor_id: str, round_id: str, title: str,
                     plan_year: int, rules: dict[str, Any],
                     target_id: str | None = None) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "round_id": round_id, "title": title,
                   "plan_year": plan_year, "rules": rules, "target_id": target_id}

        def create(connection) -> tuple[str, str, dict[str, Any]]:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "reviewer")
            clean_round = self._identifier(round_id, "round_id")
            clean_title = self._text(title, "title")
            if not isinstance(plan_year, int) or isinstance(plan_year, bool):
                raise ValidationError("plan_year 必须是整数年份")
            try:
                normalized = normalize_rules(rules)
            except ValueError as exc:
                raise ValidationError(str(exc)) from exc
            if target_id is not None and connection.execute(
                    "SELECT 1 FROM policy_targets WHERE target_id=?", (target_id,)).fetchone() is None:
                raise NotFoundError("政策目标不存在")
            try:
                connection.execute(
                    "INSERT INTO rounds(round_id,title,plan_year,status,rules_json,rules_hash,"
                    "target_id,frozen_at,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (clean_round, clean_title, plan_year, "open", canonical_json(normalized),
                     digest(normalized), target_id, None, actor_id, self._now()),
                )
            except Exception as exc:
                raise ConflictError("评审轮编号已经存在") from exc
            append_event(connection, actor_id=actor_id, action="round.created",
                         resource_type="round", resource_id=clean_round,
                         detail={"title": clean_title, "plan_year": plan_year,
                                 "rules_hash": digest(normalized)},
                         occurred_at=self._now())
            return "round", clean_round, {"round_id": clean_round, "status": "open",
                                          "rules_hash": digest(normalized)}

        with self.database.transaction(immediate=True) as connection:
            return self._receipt(connection, request_id=request_id, action="create_round",
                                 payload=payload, create=lambda: create(connection))

    def freeze_round(self, *, actor_id: str, round_id: str) -> dict[str, Any]:
        """冻结评审轮的全部登记数据与评价规则。"""

        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "reviewer")
            row = connection.execute("SELECT * FROM rounds WHERE round_id=?", (round_id,)).fetchone()
            if row is None:
                raise NotFoundError("评审轮不存在")
            if row["status"] != "open":
                raise ConflictError(f"评审轮已处于 {row['status']}，不能重复冻结")
            bundle = self._build_bundle(connection, round_id)
            snapshot_hash = bundle_hash(bundle)
            for kind in SNAPSHOT_KINDS:
                value = bundle.get(kind)
                if kind == "target":
                    items = [(row["target_id"], value)] if value else []
                elif kind == "rules":
                    items = [(round_id, value)]
                else:
                    items = [(item_id, item) for item_id, item in self._snapshot_items(kind, value)]
                for item_key, data in items:
                    connection.execute(
                        "INSERT INTO round_snapshots(round_id,kind,item_key,payload_json) "
                        "VALUES(?,?,?,?)",
                        (round_id, kind, str(item_key), canonical_json(data)),
                    )
            connection.execute(
                "UPDATE rounds SET status='frozen',snapshot_hash=?,frozen_at=? WHERE round_id=?",
                (snapshot_hash, self._now(), round_id),
            )
            append_event(connection, actor_id=actor_id, action="round.frozen",
                         resource_type="round", resource_id=round_id,
                         detail={"snapshot_hash": snapshot_hash,
                                 "nodes": len(bundle["nodes"]), "edges": len(bundle["edges"]),
                                 "gaps": len(bundle["gaps"]), "areas": len(bundle["areas"]),
                                 "projects": len(bundle["projects"])},
                         occurred_at=self._now())
            return {"round_id": round_id, "status": "frozen", "snapshot_hash": snapshot_hash,
                    "project_count": len(bundle["projects"]),
                    "edge_count": len(bundle["edges"]),
                    "gap_count": len(bundle["gaps"]),
                    "area_count": len(bundle["areas"])}

    # ------------------------------------------------------------------ 提案

    def create_proposal(self, *, request_id: str, actor_id: str, round_id: str,
                        proposal_id: str, items: list[dict[str, Any]]) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "round_id": round_id,
                   "proposal_id": proposal_id, "items": items}

        def create(connection) -> tuple[str, str, dict[str, Any]]:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "reviewer")
            clean_prop = self._identifier(proposal_id, "proposal_id")
            round_row = connection.execute("SELECT * FROM rounds WHERE round_id=?",
                                           (round_id,)).fetchone()
            if round_row is None:
                raise NotFoundError("评审轮不存在")
            if round_row["status"] == "open":
                raise ConflictError("评审轮尚未冻结，不能提交方案")
            if round_row["effective_proposal_id"]:
                raise ConflictError("本轮竞争审批已经产生生效方案")
            bundle = self._bundle_for_round(connection, round_id)
            clean_items = self._clean_items(items, bundle)
            try:
                result = evaluate(bundle, clean_items)
            except ValueError as exc:
                raise ValidationError(str(exc)) from exc
            try:
                connection.execute(
                    "INSERT INTO proposals(proposal_id,round_id,created_by,items_json,"
                    "schedule_json,metrics_json,status,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (clean_prop, round_id, actor_id, canonical_json(clean_items),
                     canonical_json(result["schedule"]), canonical_json(result),
                     "submitted", self._now()),
                )
            except Exception as exc:
                raise ConflictError("方案编号已经存在") from exc
            append_event(connection, actor_id=actor_id, action="proposal.submitted",
                         resource_type="proposal", resource_id=clean_prop,
                         detail={"round_id": round_id, "items": len(clean_items),
                                 "total_score": result["total_score"],
                                 "within_budget": result["within_budget"],
                                 "findings": len(result["findings"]),
                                 "metrics_hash": result["metrics_hash"]},
                         occurred_at=self._now())
            return "proposal", clean_prop, {"proposal_id": clean_prop, "status": "submitted",
                                            "evaluation": result}

        with self.database.transaction(immediate=True) as connection:
            return self._receipt(connection, request_id=request_id, action="create_proposal",
                                 payload=payload, create=lambda: create(connection))

    def sign_proposal(self, *, request_id: str, actor_id: str, proposal_id: str,
                      decision: str, comment: str = "") -> dict[str, Any]:
        payload = {"actor_id": actor_id, "proposal_id": proposal_id,
                   "decision": decision, "comment": comment}

        def create(connection) -> tuple[str, str, dict[str, Any]]:
            actor = self._actor(connection, actor_id)
            proposal = connection.execute("SELECT * FROM proposals WHERE proposal_id=?",
                                          (proposal_id,)).fetchone()
            if proposal is None:
                raise NotFoundError("方案不存在")
            round_row = connection.execute("SELECT * FROM rounds WHERE round_id=?",
                                           (proposal["round_id"],)).fetchone()
            required_roles = self._required_roles(round_row)
            self._require(actor, *required_roles)
            if decision not in SIGN_DECISIONS:
                raise ValidationError("decision 必须是 approve 或 reject")
            clean_comment = (comment or "").strip()
            if len(clean_comment) > 500:
                raise ValidationError("comment 不能超过 500 个字符")
            if proposal["status"] in ("effective", "rejected"):
                raise ConflictError(f"方案已处于 {proposal['status']}，不能再签署")
            if round_row["effective_proposal_id"]:
                raise ConflictError("本轮竞争审批已经产生生效方案")
            if connection.execute(
                    "SELECT 1 FROM signatures WHERE proposal_id=? AND actor_id=?",
                    (proposal_id, actor_id)).fetchone():
                raise ConflictError("同一操作者不能重复签署")
            connection.execute(
                "INSERT INTO signatures(proposal_id,actor_id,role,decision,comment,"
                "request_id,signed_at) VALUES(?,?,?,?,?,?,?)",
                (proposal_id, actor_id, actor.role, decision, clean_comment,
                 self._identifier(request_id, "request_id"), self._now()),
            )
            append_event(connection, actor_id=actor_id, action="proposal.signed",
                         resource_type="proposal", resource_id=proposal_id,
                         detail={"decision": decision, "role": actor.role},
                         occurred_at=self._now())
            if decision == "reject":
                connection.execute("UPDATE proposals SET status='rejected' WHERE proposal_id=?",
                                   (proposal_id,))
                append_event(connection, actor_id=actor_id, action="proposal.rejected",
                             resource_type="proposal", resource_id=proposal_id,
                             detail={}, occurred_at=self._now())
                return "signature", proposal_id, {"proposal_id": proposal_id,
                                                    "status": "rejected"}
            status = "submitted"
            signoff = self._signoff(connection, proposal_id, required_roles)
            if signoff["complete"]:
                # 竞争审批：原子认领，只有一个方案能生效。
                cursor = connection.execute(
                    "UPDATE rounds SET effective_proposal_id=?,status='closed' "
                    "WHERE round_id=? AND effective_proposal_id IS NULL",
                    (proposal_id, proposal["round_id"]),
                )
                if cursor.rowcount == 0:
                    raise ConflictError("竞争审批中已有其他方案先生效")
                connection.execute("UPDATE proposals SET status='effective' WHERE proposal_id=?",
                                   (proposal_id,))
                connection.execute(
                    "UPDATE proposals SET status='superseded' WHERE round_id=? "
                    "AND proposal_id<>? AND status='submitted'",
                    (proposal["round_id"], proposal_id),
                )
                status = "effective"
                append_event(connection, actor_id=actor_id, action="proposal.effective",
                             resource_type="proposal", resource_id=proposal_id,
                             detail={"round_id": proposal["round_id"]},
                             occurred_at=self._now())
            return "signature", proposal_id, {"proposal_id": proposal_id, "status": status,
                                               "signoff": signoff}

        with self.database.transaction(immediate=True) as connection:
            return self._receipt(connection, request_id=request_id, action="sign_proposal",
                                 payload=payload, create=lambda: create(connection))

    # ------------------------------------------------------------------ 查询

    def get_round(self, round_id: str) -> dict[str, Any]:
        row = self.database.connection.execute("SELECT * FROM rounds WHERE round_id=?",
                                               (round_id,)).fetchone()
        if row is None:
            raise NotFoundError("评审轮不存在")
        return {"round_id": row["round_id"], "title": row["title"], "plan_year": row["plan_year"],
                "status": row["status"], "rules": json.loads(row["rules_json"]),
                "rules_hash": row["rules_hash"], "target_id": row["target_id"],
                "snapshot_hash": row["snapshot_hash"], "frozen_at": row["frozen_at"],
                "effective_proposal_id": row["effective_proposal_id"],
                "created_at": row["created_at"]}

    def get_proposal(self, proposal_id: str) -> dict[str, Any]:
        row = self.database.connection.execute("SELECT * FROM proposals WHERE proposal_id=?",
                                               (proposal_id,)).fetchone()
        if row is None:
            raise NotFoundError("方案不存在")
        round_row = self.database.connection.execute(
            "SELECT rules_json FROM rounds WHERE round_id=?", (row["round_id"],)).fetchone()
        required_roles = normalize_rules(json.loads(round_row["rules_json"]))["required_roles"]
        return {"proposal_id": row["proposal_id"], "round_id": row["round_id"],
                "created_by": row["created_by"], "items": json.loads(row["items_json"]),
                "evaluation": json.loads(row["metrics_json"]),
                "status": row["status"], "created_at": row["created_at"],
                "signoff": self._signoff(self.database.connection, proposal_id, required_roles)}

    def recalculate_proposal(self, proposal_id: str, items: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """按冻结快照复算方案，校验历史评价哈希是否仍然一致。"""

        row = self.database.connection.execute("SELECT * FROM proposals WHERE proposal_id=?",
                                               (proposal_id,)).fetchone()
        if row is None:
            raise NotFoundError("方案不存在")
        bundle = self._bundle_for_round(self.database.connection, row["round_id"])
        frozen_hash = self.database.connection.execute(
            "SELECT snapshot_hash FROM rounds WHERE round_id=?",
            (row["round_id"],)).fetchone()["snapshot_hash"]
        snapshot_hash = bundle_hash(bundle)
        base_items = json.loads(row["items_json"])
        candidate = self._clean_items(items if items is not None else base_items, bundle)
        result = evaluate(bundle, candidate)
        stored = json.loads(row["metrics_json"])
        return {"proposal_id": proposal_id, "round_id": row["round_id"],
                "frozen_snapshot_hash": frozen_hash,
                "snapshot_hash": snapshot_hash,
                "snapshot_hash_matches": frozen_hash == snapshot_hash,
                "stored_metrics_hash": stored.get("metrics_hash"),
                "recalculated_metrics_hash": result["metrics_hash"],
                "hash_matches": (stored.get("metrics_hash") == result["metrics_hash"]
                                 and frozen_hash == snapshot_hash),
                "items_match": items is None or canonical_json(candidate) == canonical_json(
                    self._clean_items(base_items, bundle)),
                "evaluation": result}

    def list_project_versions(self, project_id: str) -> list[dict[str, Any]]:
        rows = self.database.connection.execute(
            "SELECT * FROM projects WHERE project_id=? ORDER BY version", (project_id,)).fetchall()
        if not rows:
            raise NotFoundError("项目不存在")
        return [{"project_id": row["project_id"], "version": row["version"],
                 "status": row["status"], "payload": json.loads(row["payload_json"]),
                 "created_by": row["created_by"], "created_at": row["created_at"]} for row in rows]

    def list_lineage(self, project_id: str | None = None) -> list[dict[str, Any]]:
        query = ("SELECT * FROM project_lineage "
                 + ("WHERE parent_project_id=? OR child_project_id=? " if project_id else "")
                 + "ORDER BY created_at, lineage_id")
        parameters = (project_id, project_id) if project_id else ()
        rows = self.database.connection.execute(query, parameters).fetchall()
        return [{"lineage_id": row["lineage_id"], "relation": row["relation"],
                 "parent_project_id": row["parent_project_id"], "parent_version": row["parent_version"],
                 "child_project_id": row["child_project_id"], "child_version": row["child_version"],
                 "detail": json.loads(row["detail_json"]),
                 "impacts": json.loads(row["impact_json"]),
                 "created_by": row["created_by"], "created_at": row["created_at"]} for row in rows]

    def list_round_proposals(self, round_id: str) -> dict[str, Any]:
        if self.database.connection.execute("SELECT 1 FROM rounds WHERE round_id=?",
                                            (round_id,)).fetchone() is None:
            raise NotFoundError("评审轮不存在")
        rows = self.database.connection.execute(
            "SELECT proposal_id,status,created_by,created_at,metrics_json FROM proposals "
            "WHERE round_id=? ORDER BY created_at", (round_id,)).fetchall()
        return {"round_id": round_id, "items": [
            {"proposal_id": row["proposal_id"], "status": row["status"],
             "created_by": row["created_by"], "created_at": row["created_at"],
             "total_score": json.loads(row["metrics_json"])["total_score"],
             "within_budget": json.loads(row["metrics_json"])["within_budget"]}
            for row in rows]}

    # ------------------------------------------------------------------ 辅助

    def _receipt(self, connection, *, request_id: str, action: str,
                 payload: dict[str, Any], create) -> dict[str, Any]:
        receipt = self._idempotent(connection, request_id=request_id, action=action,
                                   payload=payload, create=create)
        stored = connection.execute("SELECT response_json FROM request_receipts WHERE request_id=?",
                                    (receipt.request_id,)).fetchone()
        return {"request_id": receipt.request_id, "resource_type": receipt.resource_type,
                "resource_id": receipt.resource_id, "replayed": receipt.replayed,
                "response": json.loads(stored["response_json"])}

    @staticmethod
    def _capacity(value: Any, field: str) -> float:
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
            raise ValidationError(f"{field} 必须是非负数字")
        return float(value)

    def _validate_project(self, connection, *, project_id: str = "", name: str, corridor_id: str,
                          building_unit: str, edge_ids: list[str], gap_ids: list[str],
                          prerequisite_project_ids: list[str],
                          yearly_investment: dict[str, float],
                          policy_target_ids: list[str] | None = None,
                          target_year: int | None = None, **_: Any) -> dict[str, Any]:
        name = self._text(name, "name")
        corridor_id = self._identifier(corridor_id, "corridor_id")
        building_unit = self._text(building_unit, "building_unit")
        if not isinstance(edge_ids, list) or not edge_ids:
            raise ValidationError("edge_ids 必须是非空数组")
        clean_edges: list[str] = []
        for edge_id in edge_ids:
            edge_id = self._identifier(edge_id, "edge_ids")
            edge = connection.execute("SELECT * FROM corridor_edges WHERE edge_id=?",
                                      (edge_id,)).fetchone()
            if edge is None:
                raise NotFoundError(f"区段 {edge_id} 不存在")
            if edge["corridor_id"] != corridor_id:
                raise ValidationError(f"区段 {edge_id} 不属于走廊 {corridor_id}")
            if edge_id not in clean_edges:
                clean_edges.append(edge_id)
        clean_gaps: list[str] = []
        for gap_id in gap_ids or []:
            gap_id = self._identifier(gap_id, "gap_ids")
            gap = connection.execute("SELECT * FROM gaps WHERE gap_id=?", (gap_id,)).fetchone()
            if gap is None:
                raise NotFoundError(f"断点 {gap_id} 不存在")
            if gap["corridor_id"] != corridor_id:
                raise ValidationError(f"断点 {gap_id} 不属于走廊 {corridor_id}")
            if gap_id not in clean_gaps:
                clean_gaps.append(gap_id)
        prerequisites: list[str] = []
        for pre in prerequisite_project_ids or []:
            pre = self._identifier(pre, "prerequisite_project_ids")
            if pre == project_id:
                raise ValidationError("项目不能以前置工程引用自身")
            if pre not in prerequisites:
                prerequisites.append(pre)
            if connection.execute("SELECT 1 FROM projects WHERE project_id=?", (pre,)).fetchone() is None:
                raise NotFoundError(f"前置工程 {pre} 不存在")
        if not isinstance(yearly_investment, dict) or not yearly_investment:
            raise ValidationError("yearly_investment 必须是非空年度映射")
        clean_investment: dict[str, float] = {}
        for raw_year, amount in yearly_investment.items():
            try:
                year = int(raw_year)
            except (TypeError, ValueError) as exc:
                raise ValidationError("投资年度必须是整数") from exc
            if not isinstance(amount, (int, float)) or isinstance(amount, bool) or amount < 0:
                raise ValidationError("投资额必须是非负数字")
            clean_investment[str(year)] = float(amount)
        clean_targets: list[str] = []
        for target in policy_target_ids or []:
            target = self._identifier(target, "policy_target_ids")
            if connection.execute("SELECT 1 FROM policy_targets WHERE target_id=?",
                                  (target,)).fetchone() is None:
                raise NotFoundError(f"政策目标 {target} 不存在")
            if target not in clean_targets:
                clean_targets.append(target)
        data = {"name": name, "corridor_id": corridor_id, "building_unit": building_unit,
                "edge_ids": clean_edges, "gap_ids": clean_gaps,
                "prerequisite_project_ids": prerequisites,
                "yearly_investment": dict(sorted(clean_investment.items())),
                "policy_target_ids": clean_targets,
                "total_investment": round(sum(clean_investment.values()), 6)}
        if target_year is not None:
            if not isinstance(target_year, int):
                raise ValidationError("target_year 必须是整数")
            data["target_year"] = target_year
        return data

    def _insert_project(self, connection, project_id: str, version: int, status: str,
                        data: dict[str, Any], actor_id: str) -> None:
        connection.execute(
            "INSERT INTO projects(project_id,version,status,payload_json,payload_hash,"
            "created_by,created_at) VALUES(?,?,?,?,?,?,?)",
            (project_id, version, status, canonical_json(data), digest(data),
             actor_id, self._now()),
        )

    @staticmethod
    def _latest_row(connection, project_id: str):
        return connection.execute(
            "SELECT * FROM projects WHERE project_id=? ORDER BY version DESC LIMIT 1",
            (project_id,)).fetchone()

    @staticmethod
    def _required_roles(round_row) -> list[str]:
        rules = json.loads(round_row["rules_json"])
        return list(rules["required_roles"])

    def _signoff(self, connection, proposal_id: str, required_roles: list[str]) -> dict[str, Any]:
        rows = connection.execute(
            "SELECT actor_id,role,decision,comment,signed_at FROM signatures "
            "WHERE proposal_id=? ORDER BY signed_at", (proposal_id,)).fetchall()
        signatures = [{"actor_id": row["actor_id"], "role": row["role"],
                       "decision": row["decision"], "comment": row["comment"],
                       "signed_at": row["signed_at"]} for row in rows]
        approved_roles = {row["role"] for row in rows if row["decision"] == "approve"}
        return {"required_roles": required_roles, "signatures": signatures,
                "approved_roles": sorted(approved_roles),
                "missing_roles": [role for role in required_roles if role not in approved_roles],
                "complete": all(role in approved_roles for role in required_roles)}

    @staticmethod
    def _clean_items(items: list[dict[str, Any]], bundle: dict[str, Any]) -> list[dict[str, Any]]:
        if not isinstance(items, list) or not items:
            raise ValidationError("items 必须是非空数组")
        seen: set[str] = set()
        clean = []
        for item in items:
            if not isinstance(item, dict) or "project_id" not in item:
                raise ValidationError("每个方案条目必须包含 project_id")
            pid = str(item["project_id"])
            if pid in seen:
                raise ValidationError(f"项目 {pid} 在方案中重复出现")
            seen.add(pid)
            project = bundle["projects"].get(pid)
            if project is None:
                raise ValidationError(f"项目 {pid} 不在冻结快照中")
            entry = {"project_id": pid, "version": project["version"],
                     "action": item.get("action", "proceed")}
            if entry["action"] not in ("proceed", "delay", "exit"):
                raise ValidationError(f"项目 {pid} 的动作不支持")
            if "version" in item and int(item["version"]) != project["version"]:
                raise ValidationError(f"项目 {pid} 的版本与冻结快照不一致")
            if entry["action"] == "delay":
                year = item.get("target_year", project.get("target_year"))
                if year is None:
                    raise ValidationError(f"延期项目 {pid} 必须给出 target_year")
                entry["target_year"] = int(year)
            clean.append(entry)
        return clean

    @staticmethod
    def _snapshot_items(kind: str, value: Any) -> list[tuple[str, dict[str, Any]]]:
        if kind == "nodes":
            return [(item["node_id"], item) for item in value]
        if kind == "projects":
            return [(pid, item) for pid, item in sorted(value.items())]
        key = {"edges": "edge_id", "gaps": "gap_id", "areas": "area_id"}[kind]
        return [(item[key], item) for item in value]

    def _build_bundle(self, connection, round_id: str) -> dict[str, Any]:
        """从当前登记表组装数据束（冻结时刻调用）。"""

        nodes = [{"node_id": row["node_id"], "name": row["name"], "kind": row["kind"]}
                 for row in connection.execute("SELECT * FROM corridor_nodes ORDER BY node_id")]
        edges = [{"edge_id": row["edge_id"], "corridor_id": row["corridor_id"],
                  "from_node": row["from_node"], "to_node": row["to_node"],
                  "exists": bool(row["is_open"]), "current_capacity": row["current_capacity"],
                  "required_capacity": row["required_capacity"]}
                 for row in connection.execute("SELECT * FROM corridor_edges ORDER BY edge_id")]
        gaps = [{"gap_id": row["gap_id"], "corridor_id": row["corridor_id"],
                 "edge_id": row["edge_id"], "current_capacity": row["current_capacity"],
                 "required_capacity": row["required_capacity"]}
                for row in connection.execute("SELECT * FROM gaps ORDER BY gap_id")]
        areas = [{"area_id": row["area_id"], "name": row["name"], "kind": row["kind"],
                  "node_ids": json.loads(row["node_ids_json"])}
                 for row in connection.execute("SELECT * FROM service_areas ORDER BY area_id")]
        projects: dict[str, dict[str, Any]] = {}
        rows = connection.execute(
            "SELECT * FROM projects p WHERE version=(SELECT MAX(version) FROM projects "
            "WHERE project_id=p.project_id) AND status IN ('registered','delayed')"
        ).fetchall()
        for row in rows:
            data = json.loads(row["payload_json"])
            data = {"project_id": row["project_id"], "version": row["version"],
                    "status": row["status"], **data}
            projects[row["project_id"]] = data
        round_row = connection.execute("SELECT * FROM rounds WHERE round_id=?",
                                       (round_id,)).fetchone()
        rules = normalize_rules(json.loads(round_row["rules_json"]))
        target = None
        if round_row["target_id"]:
            target_row = connection.execute("SELECT * FROM policy_targets WHERE target_id=?",
                                            (round_row["target_id"],)).fetchone()
            if target_row:
                target = {"target_id": target_row["target_id"],
                          **json.loads(target_row["payload_json"])}
        return {"nodes": nodes, "edges": edges, "gaps": gaps, "areas": areas,
                "projects": projects, "rules": rules, "target": target}

    def _bundle_for_round(self, connection, round_id: str) -> dict[str, Any]:
        """从冻结快照还原数据束，供复算使用，绝不读取登记新数据。"""

        rows = connection.execute("SELECT * FROM round_snapshots WHERE round_id=? ORDER BY kind,item_key",
                                  (round_id,)).fetchall()
        grouped: dict[str, list[dict[str, Any]]] = {kind: [] for kind in SNAPSHOT_KINDS}
        for row in rows:
            grouped[row["kind"]].append(json.loads(row["payload_json"]))
        if not grouped["rules"]:
            raise ConflictError("评审轮尚未冻结")
        projects = {item["project_id"]: item for item in grouped["projects"]}
        # JSON 往返后年度键会变为字符串，重新规范化以恢复整数键与默认值。
        rules = normalize_rules(grouped["rules"][0])
        return {"nodes": grouped["nodes"], "edges": grouped["edges"], "gaps": grouped["gaps"],
                "areas": grouped["areas"], "projects": projects,
                "rules": rules,
                "target": grouped["target"][0] if grouped["target"] else None}
