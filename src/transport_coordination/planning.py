"""通道项目组合决策服务。

在基础服务的权限、幂等、事务与审计链边界之上，实现走廊与项目登记、
评审轮次冻结、版本快照、项目拆分/合并/延期/退出血缘，以及竞争会签。
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Callable

from .audit import append_event, canonical_json, digest
from .clock import Clock, SystemClock
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .portfolio import (
    evaluate_snapshot,
    normalize_corridor,
    normalize_project,
    normalize_rules,
)
from .storage import Database


SIGN_ROLES = ("admin", "reviewer")
EDIT_ROLES = ("admin", "operator")


class PlanningService:
    """协调通道组合的登记、冻结、评价和竞争审批规则。"""

    def __init__(self, database: Database, clock: Clock | None = None) -> None:
        self.database = database
        self.clock = clock or SystemClock()

    def _now(self) -> str:
        return self.clock.now().isoformat().replace("+00:00", "Z")

    # -- 基础辅助 -----------------------------------------------------------

    def _actor(self, connection, actor_id: str):
        row = connection.execute("SELECT * FROM actors WHERE actor_id=?", (actor_id,)).fetchone()
        if row is None:
            raise NotFoundError("操作者不存在")
        if not row["active"]:
            raise PermissionDenied("操作者已停用")
        return row

    def _require(self, actor, *roles: str) -> None:
        if actor["role"] not in roles:
            raise PermissionDenied("当前角色不能执行该动作")

    def _idempotent(self, connection, *, request_id: str, action: str,
                    payload: dict[str, Any], create: Callable[[], tuple[str, str, dict[str, Any]]]):
        row = connection.execute("SELECT * FROM request_receipts WHERE request_id=?", (request_id,)).fetchone()
        if row:
            if row["action"] != action or row["payload_hash"] != digest(payload):
                raise ConflictError("request_id 已被不同内容使用")
            return {"request_id": request_id, "resource_type": row["resource_type"],
                    "resource_id": row["resource_id"], "replayed": True}
        resource_type, resource_id, response = create()
        connection.execute(
            "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,resource_id,"
            "response_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (request_id, action, digest(payload), resource_type, resource_id,
             canonical_json(response), self._now()),
        )
        return {"request_id": request_id, "resource_type": resource_type,
                "resource_id": resource_id, "replayed": False}

    def _replay_if_seen(self, connection, *, request_id: str, action: str,
                        payload: dict[str, Any]) -> dict[str, Any] | None:
        """在状态机校验之前识别幂等重放，保证重启后重试返回原结果。"""

        row = connection.execute("SELECT * FROM request_receipts WHERE request_id=?",
                                 (request_id,)).fetchone()
        if row is None:
            return None
        if row["action"] != action or row["payload_hash"] != digest(payload):
            raise ConflictError("request_id 已被不同内容使用")
        return {"request_id": request_id, "resource_type": row["resource_type"],
                "resource_id": row["resource_id"], "replayed": True}

    def _open_round(self, connection):
        return connection.execute(
            "SELECT * FROM rounds WHERE status='open' ORDER BY created_at, round_id LIMIT 1"
        ).fetchone()

    def _require_open_round(self, connection):
        round_row = self._open_round(connection)
        if round_row is None:
            raise ConflictError("当前没有开放的评审轮次，登记与调整必须在开放轮次内进行")
        return round_row

    def _corridor_row(self, connection, corridor_id: str):
        row = connection.execute("SELECT * FROM corridors WHERE corridor_id=?", (corridor_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"走廊 {corridor_id} 不存在")
        return row

    def _project_row(self, connection, project_id: str):
        row = connection.execute("SELECT * FROM projects WHERE project_id=?", (project_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"项目 {project_id} 不存在")
        return row

    def _validate_against_corridor(self, project: dict[str, Any], corridor: dict[str, Any]) -> None:
        unknown_bottlenecks = sorted(set(project["bottleneck_ids"]) - set(corridor["bottlenecks"]))
        if unknown_bottlenecks:
            raise ValidationError(f"项目引用了走廊未登记的断点: {unknown_bottlenecks}")
        unknown_areas = sorted(set(project["service_areas"]) - set(corridor["service_gaps"]))
        if unknown_areas:
            raise ValidationError(f"项目引用了走廊未登记的服务区域: {unknown_areas}")

    # -- 政策目标 -----------------------------------------------------------

    def register_policy_goal(self, *, request_id: str, actor_id: str, goal_code: str,
                             name: str, description: str = "") -> dict[str, Any]:
        payload = {"actor_id": actor_id, "goal_code": goal_code, "name": name,
                   "description": description}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *EDIT_ROLES)
            name = str(name).strip()
            if not name:
                raise ValidationError("name 不能为空")
            goal_code = str(goal_code).strip()
            if not goal_code:
                raise ValidationError("goal_code 不能为空")

            def create():
                try:
                    connection.execute(
                        "INSERT INTO policy_goals(goal_code,name,description,created_by,created_at) "
                        "VALUES(?,?,?,?,?)",
                        (goal_code, name, description, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("政策目标编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="policy_goal.registered",
                             resource_type="policy_goal", resource_id=goal_code,
                             detail={"name": name}, occurred_at=self._now())
                return "policy_goal", goal_code, {"goal_code": goal_code}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_policy_goal", payload=payload, create=create)

    # -- 走廊登记 -----------------------------------------------------------

    def register_corridor(self, *, request_id: str, actor_id: str, corridor_id: str,
                          name: str, direction: str, nodes: list[str] | None = None,
                          edges: list[dict[str, Any]] | None = None,
                          bottlenecks: list[dict[str, Any]] | None = None,
                          service_gaps: list[dict[str, Any]] | None = None,
                          backbone_pairs: list[dict[str, str]] | None = None) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "corridor_id": corridor_id, "name": name,
                   "direction": direction, "nodes": nodes or [], "edges": edges or [],
                   "bottlenecks": bottlenecks or [], "service_gaps": service_gaps or [],
                   "backbone_pairs": backbone_pairs or []}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *EDIT_ROLES)
            round_row = self._require_open_round(connection)
            normalized = normalize_corridor(payload)
            if connection.execute("SELECT 1 FROM corridors WHERE corridor_id=?",
                                  (corridor_id,)).fetchone():
                raise ConflictError("走廊编号已经存在")

            def create():
                now = self._now()
                connection.execute(
                    "INSERT INTO corridors(corridor_id,name,direction,payload_json,payload_hash,"
                    "revision,created_by,created_at,updated_at) VALUES(?,?,?,?,?,1,?,?,?)",
                    (corridor_id, normalized["name"], direction,
                     canonical_json(normalized), digest(normalized), actor_id, now, now),
                )
                connection.execute(
                    "INSERT INTO corridor_revisions(corridor_id,revision,round_id,action,"
                    "payload_json,payload_hash,changed_by,changed_at) VALUES(?,1,?,'created',?,?,?,?)",
                    (corridor_id, round_row["round_id"], canonical_json(normalized),
                     digest(normalized), actor_id, now),
                )
                append_event(connection, actor_id=actor_id, action="corridor.registered",
                             resource_type="corridor", resource_id=corridor_id,
                             detail={"name": name, "direction": direction,
                                     "round_id": round_row["round_id"]}, occurred_at=now)
                return "corridor", corridor_id, {"corridor_id": corridor_id, "revision": 1}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_corridor", payload=payload, create=create)

    def update_corridor(self, *, request_id: str, actor_id: str, **fields: Any) -> dict[str, Any]:
        payload = {"actor_id": actor_id, **fields}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *EDIT_ROLES)
            round_row = self._require_open_round(connection)
            existing = self._corridor_row(connection, fields["corridor_id"])
            current = json.loads(existing["payload_json"])
            merged = {**current, **{key: value for key, value in fields.items()
                                    if key != "request_id"}}
            normalized = normalize_corridor(merged)

            def create():
                now = self._now()
                revision = existing["revision"] + 1
                connection.execute(
                    "UPDATE corridors SET name=?,direction=?,payload_json=?,payload_hash=?,"
                    "revision=?,updated_at=? WHERE corridor_id=?",
                    (normalized["name"], normalized["direction"], canonical_json(normalized),
                     digest(normalized), revision, now, fields["corridor_id"]),
                )
                connection.execute(
                    "INSERT INTO corridor_revisions(corridor_id,revision,round_id,action,"
                    "payload_json,payload_hash,changed_by,changed_at) VALUES(?,?,?,'updated',?,?,?,?)",
                    (fields["corridor_id"], revision, round_row["round_id"],
                     canonical_json(normalized), digest(normalized), actor_id, now),
                )
                append_event(connection, actor_id=actor_id, action="corridor.updated",
                             resource_type="corridor", resource_id=fields["corridor_id"],
                             detail={"revision": revision, "round_id": round_row["round_id"]},
                             occurred_at=now)
                return "corridor", fields["corridor_id"], {"corridor_id": fields["corridor_id"],
                                                           "revision": revision}

            return self._idempotent(connection, request_id=request_id,
                                    action="update_corridor", payload=payload, create=create)

    # -- 项目登记与演变 -----------------------------------------------------

    def _insert_project(self, connection, *, raw: dict[str, Any], actor_id: str,
                        round_id: str, action: str, note: str = "",
                        round_years: list[str] | None = None) -> dict[str, Any]:
        corridor_row = self._corridor_row(connection, raw["corridor_id"])
        corridor = json.loads(corridor_row["payload_json"])
        project = normalize_project(raw)
        if not project["name"]:
            raise ValidationError("项目 name 不能为空")
        if round_years is not None:
            outside = sorted(set(project["investments"]) - set(round_years))
            if outside:
                raise ValidationError(f"项目投资年度超出轮次范围: {outside}")
            if project["benefit_year"] not in round_years:
                raise ValidationError("效益年份必须落在轮次年度范围内")
        self._validate_against_corridor(project, corridor)
        now = self._now()
        connection.execute(
            "INSERT INTO projects(project_id,corridor_id,name,builder,status,payload_json,"
            "payload_hash,revision,created_by,created_at,updated_at) VALUES(?,?,?,?,'candidate',?,?,1,?,?,?)",
            (project["project_id"], project["corridor_id"], project["name"], project["builder"],
             canonical_json(project), digest(project), actor_id, now, now),
        )
        connection.execute(
            "INSERT INTO project_revisions(project_id,revision,round_id,action,note,payload_json,"
            "payload_hash,changed_by,changed_at) VALUES(?,1,?,?,'',?,?,?,?)",
            (project["project_id"], round_id, action, canonical_json(project),
             digest(project), actor_id, now),
        )
        append_event(connection, actor_id=actor_id, action=f"project.{action}",
                     resource_type="project", resource_id=project["project_id"],
                     detail={"corridor_id": project["corridor_id"], "round_id": round_id,
                             "note": note}, occurred_at=now)
        return project

    def register_project(self, *, request_id: str, actor_id: str, project_id: str,
                         corridor_id: str, name: str, builder: str,
                         investments: dict[str, float],
                         footprint: list[str] | None = None,
                         bottleneck_ids: list[str] | None = None,
                         prerequisite_project_ids: list[str] | None = None,
                         service_areas: list[str] | None = None,
                         policy_goals: list[str] | None = None,
                         duration_years: int = 1, benefit_year: str | None = None,
                         target_capacity: float = 0.0, priority: int = 0) -> dict[str, Any]:
        payload = {key: value for key, value in locals().items() if key not in ("self",)}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *EDIT_ROLES)
            round_row = self._require_open_round(connection)
            round_years = json.loads(round_row["years_json"])
            if connection.execute("SELECT 1 FROM projects WHERE project_id=?",
                                  (project_id,)).fetchone():
                raise ConflictError("项目编号已经存在")

            def create():
                project = self._insert_project(
                    connection,
                    raw={"project_id": project_id, "corridor_id": corridor_id, "name": name,
                         "builder": builder, "footprint": footprint or [],
                         "bottleneck_ids": bottleneck_ids or [],
                         "prerequisite_project_ids": prerequisite_project_ids or [],
                         "service_areas": service_areas or [], "policy_goals": policy_goals or [],
                         "investments": investments, "duration_years": duration_years,
                         "benefit_year": benefit_year, "target_capacity": target_capacity,
                         "priority": priority},
                    actor_id=actor_id, round_id=round_row["round_id"], action="registered",
                    round_years=round_years)
                return "project", project["project_id"], {"project_id": project["project_id"],
                                                          "revision": 1}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_project", payload=payload, create=create)

    def _add_revision(self, connection, *, project_row, action: str, project: dict[str, Any],
                      round_id: str, actor_id: str, note: str, now: str) -> int:
        revision = project_row["revision"] + 1
        connection.execute(
            "UPDATE projects SET name=?,builder=?,payload_json=?,payload_hash=?,revision=?,"
            "updated_at=? WHERE project_id=?",
            (project["name"], project["builder"], canonical_json(project), digest(project),
             revision, now, project["project_id"]),
        )
        connection.execute(
            "INSERT INTO project_revisions(project_id,revision,round_id,action,note,payload_json,"
            "payload_hash,changed_by,changed_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (project["project_id"], revision, round_id, action, note,
             canonical_json(project), digest(project), actor_id, now),
        )
        append_event(connection, actor_id=actor_id, action=f"project.{action}",
                     resource_type="project", resource_id=project["project_id"],
                     detail={"revision": revision, "round_id": round_id, "note": note},
                     occurred_at=now)
        return revision

    def _lineage(self, connection, *, round_id: str, ancestor_id: str | None,
                 descendant_id: str | None, relation: str, detail: dict[str, Any]) -> None:
        connection.execute(
            "INSERT INTO project_lineage(round_id,ancestor_id,descendant_id,relation,detail_json,"
            "created_at) VALUES(?,?,?,?,?,?)",
            (round_id, ancestor_id, descendant_id, relation,
             canonical_json(detail), self._now()),
        )

    def update_project(self, *, request_id: str, actor_id: str, project_id: str,
                       changes: dict[str, Any]) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "project_id": project_id, "changes": changes}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *EDIT_ROLES)
            round_row = self._require_open_round(connection)
            project_row = self._project_row(connection, project_id)
            if project_row["status"] != "candidate":
                raise ConflictError("已拆分、合并或退出的项目不能直接修改")
            current = json.loads(project_row["payload_json"])
            merged_payload = {**current, **changes, "project_id": project_id}
            project = normalize_project(merged_payload)
            round_years = json.loads(round_row["years_json"])
            outside = sorted(set(project["investments"]) - set(round_years))
            if outside:
                raise ValidationError(f"项目投资年度超出轮次范围: {outside}")
            corridor_row = self._corridor_row(connection, project["corridor_id"])
            self._validate_against_corridor(project, json.loads(corridor_row["payload_json"]))

            def create():
                revision = self._add_revision(connection, project_row=project_row, action="updated",
                                              project=project, round_id=round_row["round_id"],
                                              actor_id=actor_id,
                                              note=canonical_json(sorted(changes)), now=self._now())
                return "project", project_id, {"project_id": project_id, "revision": revision}

            return self._idempotent(connection, request_id=request_id,
                                    action="update_project", payload=payload, create=create)

    def split_project(self, *, request_id: str, actor_id: str, project_id: str,
                      splits: list[dict[str, Any]], note: str = "") -> dict[str, Any]:
        payload = {"actor_id": actor_id, "project_id": project_id, "splits": splits, "note": note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *EDIT_ROLES)
            round_row = self._require_open_round(connection)
            parent = self._project_row(connection, project_id)
            if parent["status"] != "candidate":
                raise ConflictError("只有候选项目可以拆分")
            if not splits or len(splits) < 2:
                raise ValidationError("拆分至少要产生两个新项目")
            round_years = json.loads(round_row["years_json"])
            parent_payload = json.loads(parent["payload_json"])
            new_ids = []
            for raw in splits:
                raw = {"corridor_id": parent_payload["corridor_id"], **raw}
                if not raw.get("project_id") or raw["project_id"] == project_id:
                    raise ValidationError("拆分产生的项目必须使用新的唯一编号")
                if connection.execute("SELECT 1 FROM projects WHERE project_id=?",
                                      (raw["project_id"],)).fetchone():
                    raise ConflictError(f"拆分目标编号 {raw['project_id']} 已存在")
                child = self._insert_project(connection, raw=raw, actor_id=actor_id,
                                             round_id=round_row["round_id"], action="split_created",
                                             round_years=round_years)
                new_ids.append(child["project_id"])
                self._lineage(connection, round_id=round_row["round_id"], ancestor_id=project_id,
                              descendant_id=child["project_id"], relation="split",
                              detail={"note": note})
            now = self._now()
            connection.execute("UPDATE projects SET status='split',updated_at=? WHERE project_id=?",
                               (now, project_id))
            connection.execute(
                "INSERT INTO project_revisions(project_id,revision,round_id,action,note,payload_json,"
                "payload_hash,changed_by,changed_at) VALUES(?,?,?,'split',?,?,?,?,?)",
                (project_id, parent["revision"] + 1, round_row["round_id"], note,
                 parent["payload_json"], parent["payload_hash"], actor_id, now),
            )
            append_event(connection, actor_id=actor_id, action="project.split",
                         resource_type="project", resource_id=project_id,
                         detail={"new_project_ids": new_ids, "round_id": round_row["round_id"]},
                         occurred_at=now)

            def create():
                return "project_split", project_id, {"project_id": project_id,
                                                      "new_project_ids": new_ids}

            return self._idempotent(connection, request_id=request_id,
                                    action="split_project", payload=payload, create=create)

    def merge_projects(self, *, request_id: str, actor_id: str, project_ids: list[str],
                       merged: dict[str, Any], note: str = "") -> dict[str, Any]:
        payload = {"actor_id": actor_id, "project_ids": project_ids, "merged": merged, "note": note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *EDIT_ROLES)
            round_row = self._require_open_round(connection)
            if not project_ids or len(project_ids) < 2:
                raise ValidationError("合并至少要选择两个项目")
            rows = [self._project_row(connection, project_id) for project_id in project_ids]
            if any(row["status"] != "candidate" for row in rows):
                raise ConflictError("只有候选项目可以合并")
            corridors = {row["corridor_id"] for row in rows}
            if len(corridors) != 1:
                raise ValidationError("只能合并同一走廊内的项目")
            new_id = str(merged.get("project_id", "")).strip()
            if not new_id or new_id in project_ids:
                raise ValidationError("合并项目必须使用新的唯一编号")
            if connection.execute("SELECT 1 FROM projects WHERE project_id=?", (new_id,)).fetchone():
                raise ConflictError(f"合并目标编号 {new_id} 已存在")
            child = self._insert_project(
                connection,
                raw={"corridor_id": next(iter(corridors)), **merged, "project_id": new_id},
                actor_id=actor_id, round_id=round_row["round_id"], action="merge_created",
                round_years=json.loads(round_row["years_json"]))
            now = self._now()
            for row in rows:
                connection.execute("UPDATE projects SET status='merged',updated_at=? WHERE project_id=?",
                                   (now, row["project_id"]))
                connection.execute(
                    "INSERT INTO project_revisions(project_id,revision,round_id,action,note,"
                    "payload_json,payload_hash,changed_by,changed_at) VALUES(?,?,?,'merged',?,?,?,?,?)",
                    (row["project_id"], row["revision"] + 1, round_row["round_id"], note,
                     row["payload_json"], row["payload_hash"], actor_id, now),
                )
                self._lineage(connection, round_id=round_row["round_id"],
                              ancestor_id=row["project_id"], descendant_id=new_id,
                              relation="merge", detail={"note": note})
            append_event(connection, actor_id=actor_id, action="project.merge",
                         resource_type="project", resource_id=new_id,
                         detail={"source_project_ids": list(project_ids),
                                 "round_id": round_row["round_id"]}, occurred_at=now)

            def create():
                return "project_merge", new_id, {"project_id": new_id,
                                                 "source_project_ids": list(project_ids)}

            return self._idempotent(connection, request_id=request_id,
                                    action="merge_projects", payload=payload, create=create)

    def defer_project(self, *, request_id: str, actor_id: str, project_id: str,
                      new_benefit_year: str, investments: dict[str, float],
                      note: str = "") -> dict[str, Any]:
        """跨年度延期：改写效益年份与分年投资，并沿版本保留影响。"""

        payload = {"actor_id": actor_id, "project_id": project_id,
                   "new_benefit_year": new_benefit_year, "investments": investments, "note": note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *EDIT_ROLES)
            round_row = self._require_open_round(connection)
            project_row = self._project_row(connection, project_id)
            if project_row["status"] != "candidate":
                raise ConflictError("只有候选项目可以延期")
            current = json.loads(project_row["payload_json"])
            before = {"benefit_year": current["benefit_year"],
                      "investments": current["investments"]}
            project = normalize_project({**current, "benefit_year": new_benefit_year,
                                         "investments": investments})

            def create():
                revision = self._add_revision(connection, project_row=project_row, action="deferred",
                                              project=project, round_id=round_row["round_id"],
                                              actor_id=actor_id, note=note, now=self._now())
                self._lineage(connection, round_id=round_row["round_id"], ancestor_id=project_id,
                              descendant_id=project_id, relation="defer",
                              detail={"before": before, "after": {"benefit_year": new_benefit_year,
                                                                  "investments": investments},
                                      "note": note})
                return "project", project_id, {"project_id": project_id, "revision": revision}

            return self._idempotent(connection, request_id=request_id,
                                    action="defer_project", payload=payload, create=create)

    def exit_project(self, *, request_id: str, actor_id: str, project_id: str,
                     reason: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "project_id": project_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *EDIT_ROLES)
            round_row = self._require_open_round(connection)
            project_row = self._project_row(connection, project_id)
            if project_row["status"] != "candidate":
                raise ConflictError("项目已经退出过组合")

            def create():
                now = self._now()
                connection.execute(
                    "UPDATE projects SET status='exited',updated_at=? WHERE project_id=?",
                    (now, project_id),
                )
                connection.execute(
                    "INSERT INTO project_revisions(project_id,revision,round_id,action,note,"
                    "payload_json,payload_hash,changed_by,changed_at) VALUES(?,?,?,'exited',?,?,?,?,?)",
                    (project_id, project_row["revision"] + 1, round_row["round_id"], reason,
                     project_row["payload_json"], project_row["payload_hash"], actor_id, now),
                )
                self._lineage(connection, round_id=round_row["round_id"], ancestor_id=project_id,
                              descendant_id=None, relation="exit", detail={"reason": reason})
                append_event(connection, actor_id=actor_id, action="project.exited",
                             resource_type="project", resource_id=project_id,
                             detail={"reason": reason, "round_id": round_row["round_id"]},
                             occurred_at=now)
                return "project", project_id, {"project_id": project_id, "status": "exited"}

            return self._idempotent(connection, request_id=request_id,
                                    action="exit_project", payload=payload, create=create)

    # -- 查询 ---------------------------------------------------------------

    def get_corridor(self, corridor_id: str) -> dict[str, Any]:
        row = self._corridor_row(self.database.connection, corridor_id)
        return {"corridor_id": row["corridor_id"], "name": row["name"],
                "direction": row["direction"], "revision": row["revision"],
                "payload": json.loads(row["payload_json"])}

    def list_projects(self, corridor_id: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM projects"
        parameters: list[Any] = []
        if corridor_id:
            query += " WHERE corridor_id=?"
            parameters.append(corridor_id)
        query += " ORDER BY corridor_id, project_id"
        result = []
        for row in self.database.connection.execute(query, parameters):
            result.append({"project_id": row["project_id"], "corridor_id": row["corridor_id"],
                           "name": row["name"], "builder": row["builder"],
                           "status": row["status"], "revision": row["revision"],
                           "payload": json.loads(row["payload_json"])})
        return result

    def project_lineage(self, project_id: str) -> dict[str, Any]:
        self._project_row(self.database.connection, project_id)
        rows = self.database.connection.execute(
            "SELECT * FROM project_lineage WHERE ancestor_id=? OR descendant_id=? "
            "ORDER BY lineage_id", (project_id, project_id),
        ).fetchall()
        events = [{"round_id": row["round_id"], "ancestor_id": row["ancestor_id"],
                   "descendant_id": row["descendant_id"], "relation": row["relation"],
                   "detail": json.loads(row["detail_json"]), "created_at": row["created_at"]}
                  for row in rows]
        revisions = self.database.connection.execute(
            "SELECT * FROM project_revisions WHERE project_id=? ORDER BY revision", (project_id,)
        ).fetchall()
        return {
            "project_id": project_id,
            "lineage": events,
            "revisions": [{"revision": row["revision"], "round_id": row["round_id"],
                           "action": row["action"], "note": row["note"],
                           "payload_hash": row["payload_hash"], "changed_by": row["changed_by"],
                           "changed_at": row["changed_at"]} for row in revisions],
        }

    # -- 轮次与冻结 ---------------------------------------------------------

    def create_round(self, *, request_id: str, actor_id: str, round_id: str, title: str,
                     years: list[str], parent_round_id: str | None = None) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "round_id": round_id, "title": title,
                   "years": years, "parent_round_id": parent_round_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "reviewer")
            years = [str(year) for year in years]
            if len(years) != len(set(years)) or years != sorted(years):
                raise ValidationError("years 必须是升序且不重复的年份列表")
            if self._open_round(connection) is not None:
                raise ConflictError("已有开放轮次，请先冻结后再开启新一轮")
            if parent_round_id is not None:
                parent = connection.execute("SELECT * FROM rounds WHERE round_id=?",
                                            (parent_round_id,)).fetchone()
                if parent is None:
                    raise NotFoundError("父轮次不存在")

            def create():
                now = self._now()
                connection.execute(
                    "INSERT INTO rounds(round_id,title,years_json,status,parent_round_id,"
                    "created_by,created_at) VALUES(?,?,?, 'open',?,?,?)",
                    (round_id, title, canonical_json(years), parent_round_id, actor_id, now),
                )
                append_event(connection, actor_id=actor_id, action="round.created",
                             resource_type="round", resource_id=round_id,
                             detail={"title": title, "years": years,
                                     "parent_round_id": parent_round_id}, occurred_at=now)
                return "round", round_id, {"round_id": round_id, "status": "open"}

            return self._idempotent(connection, request_id=request_id,
                                    action="create_round", payload=payload, create=create)

    def _snapshot(self, connection) -> dict[str, Any]:
        corridors = [json.loads(row["payload_json"])
                     for row in connection.execute(
                         "SELECT payload_json FROM corridors ORDER BY corridor_id")]
        projects = [json.loads(row["payload_json"])
                    for row in connection.execute(
                        "SELECT status,payload_json FROM projects WHERE status='candidate' "
                        "ORDER BY project_id")]
        goals = [{"goal_code": row["goal_code"], "name": row["name"],
                  "description": row["description"]}
                 for row in connection.execute(
                     "SELECT goal_code,name,description FROM policy_goals ORDER BY goal_code")]
        return {"corridors": corridors, "projects": projects, "policy_goals": goals}

    def _freeze_impact(self, connection, round_row, snapshot: dict[str, Any],
                       rules: dict[str, Any]) -> dict[str, Any]:
        """计算相对父版本的项目、投资与指标影响。"""

        parent_round_id = round_row["parent_round_id"]
        if parent_round_id is None:
            return {"parent_round_id": None, "project_changes": [], "investment_delta": {},
                    "metric_delta": {}, "lineage_events": []}
        parent_version = connection.execute(
            "SELECT * FROM versions WHERE round_id=?", (parent_round_id,)
        ).fetchone()
        if parent_version is None:
            return {"parent_round_id": parent_round_id, "project_changes": [],
                    "investment_delta": {}, "metric_delta": {}, "lineage_events": []}
        parent_snapshot = json.loads(parent_version["snapshot_json"])
        old_projects = {item["project_id"]: item for item in parent_snapshot["projects"]}
        new_projects = {item["project_id"]: item for item in snapshot["projects"]}
        changes = []
        for project_id in sorted(set(old_projects) | set(new_projects)):
            if project_id not in old_projects:
                changes.append({"project_id": project_id, "change": "added"})
            elif project_id not in new_projects:
                changes.append({"project_id": project_id, "change": "removed"})
            elif digest(old_projects[project_id]) != digest(new_projects[project_id]):
                changes.append({"project_id": project_id, "change": "changed"})
        old_corridors = {item["corridor_id"]: digest(item) for item in parent_snapshot["corridors"]}
        corridor_changes = sorted(
            item["corridor_id"] for item in snapshot["corridors"]
            if old_corridors.get(item["corridor_id"]) != digest(item))
        delta: dict[str, float] = {}
        for year in set().union(*[set(p["investments"]) for p in new_projects.values()],
                                *[set(p["investments"]) for p in old_projects.values()]):
            new_total = sum(p["investments"].get(year, 0.0) for p in new_projects.values())
            old_total = sum(p["investments"].get(year, 0.0) for p in old_projects.values())
            delta[year] = round(new_total - old_total, 2)
        lineage_events = [
            {"ancestor_id": row["ancestor_id"], "descendant_id": row["descendant_id"],
             "relation": row["relation"], "detail": json.loads(row["detail_json"])}
            for row in connection.execute(
                "SELECT * FROM project_lineage WHERE round_id=? ORDER BY lineage_id",
                (round_row["round_id"],))
        ]
        # 把父版本已批准组合放到新快照/新规则下复算，量化跨年调整影响。
        metric_delta: dict[str, Any] = {}
        parent_plan_id = connection.execute(
            "SELECT effective_plan_id FROM rounds WHERE round_id=?", (parent_round_id,)
        ).fetchone()[0]
        if parent_plan_id:
            parent_plan = connection.execute("SELECT * FROM plans WHERE plan_id=?",
                                             (parent_plan_id,)).fetchone()
            parent_decisions = json.loads(parent_plan["decisions_json"])
            selected = [item["project_id"] for item in parent_decisions
                        if item["action"] == "schedule" and item["project_id"] in new_projects]
            from .portfolio import compute_metrics
            old_eval = json.loads(parent_plan["evaluation_json"])
            replayed = compute_metrics(
                [normalize_corridor(item) for item in snapshot["corridors"]],
                [normalize_project(item) for item in snapshot["projects"]],
                selected, rules,
                {goal["goal_code"] for goal in snapshot.get("policy_goals", [])})
            for key in ("score", "backbone_ratio", "border_coverage", "rural_coverage",
                        "bottleneck_ratio", "total_investment", "within_budget"):
                metric_delta[key] = {"before": old_eval["metrics"].get(key),
                                     "after": replayed.get(key)}
        return {"parent_round_id": parent_round_id, "project_changes": changes,
                "corridor_changes": corridor_changes, "investment_delta": dict(sorted(delta.items())),
                "metric_delta": metric_delta, "lineage_events": lineage_events}

    def freeze_round(self, *, request_id: str, actor_id: str, round_id: str,
                     rules: dict[str, Any]) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "round_id": round_id, "rules": rules}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "reviewer")
            replay = self._replay_if_seen(connection, request_id=request_id,
                                          action="freeze_round", payload=payload)
            if replay is not None:
                return replay
            round_row = connection.execute("SELECT * FROM rounds WHERE round_id=?",
                                           (round_id,)).fetchone()
            if round_row is None:
                raise NotFoundError("轮次不存在")
            if round_row["status"] != "open":
                raise ConflictError("轮次已经冻结，不能倒改")
            normalized_rules = normalize_rules(rules)
            for approver in normalized_rules["approvers"]:
                approver_row = connection.execute(
                    "SELECT * FROM actors WHERE actor_id=?", (approver,)).fetchone()
                if approver_row is None or not approver_row["active"]:
                    raise ValidationError(f"会签人 {approver} 不存在或已停用")
                if approver_row["role"] not in SIGN_ROLES:
                    raise ValidationError(f"会签人 {approver} 不是评审角色")
            horizon = set(json.loads(round_row["years_json"]))
            outside = [year for year in normalized_rules["budgets"] if year not in horizon]
            if outside:
                raise ValidationError(f"预算年度超出轮次范围: {outside}")
            snapshot = self._snapshot(connection)
            evaluation = evaluate_snapshot(snapshot, normalized_rules)
            sequence_no = connection.execute("SELECT COUNT(*) AS c FROM versions").fetchone()["c"] + 1
            version_id = f"v{sequence_no:03d}"
            parent_version_row = None
            if round_row["parent_round_id"]:
                parent_version_row = connection.execute(
                    "SELECT version_id FROM versions WHERE round_id=?",
                    (round_row["parent_round_id"],)).fetchone()
            snapshot_json = canonical_json(snapshot)
            rules_json = canonical_json(normalized_rules)
            snapshot_hash = digest(snapshot)
            rules_hash = digest(normalized_rules)
            version_hash = digest({
                "version_id": version_id,
                "round_id": round_id,
                "parent_version_id": parent_version_row["version_id"] if parent_version_row else None,
                "snapshot_hash": snapshot_hash,
                "rules_hash": rules_hash,
            })
            impact = self._freeze_impact(connection, round_row, snapshot, normalized_rules)
            now = self._now()
            connection.execute(
                "INSERT INTO versions(version_id,round_id,sequence_no,parent_version_id,"
                "snapshot_json,snapshot_hash,rules_json,rules_hash,version_hash,impact_json,"
                "created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (version_id, round_id, sequence_no,
                 parent_version_row["version_id"] if parent_version_row else None,
                 snapshot_json, snapshot_hash, rules_json, rules_hash, version_hash,
                 canonical_json(impact), actor_id, now),
            )
            connection.execute("UPDATE rounds SET status='frozen',frozen_at=? WHERE round_id=?",
                               (now, round_id))
            append_event(connection, actor_id=actor_id, action="round.frozen",
                         resource_type="version", resource_id=version_id,
                         detail={"round_id": round_id, "snapshot_hash": snapshot_hash,
                                 "rules_hash": rules_hash, "version_hash": version_hash,
                                 "recommended_score": evaluation["metrics"]["score"]},
                         occurred_at=now)

            def create():
                return "version", version_id, {"version_id": version_id, "round_id": round_id,
                                                "snapshot_hash": snapshot_hash,
                                                "rules_hash": rules_hash,
                                                "version_hash": version_hash,
                                                "recommended_score": evaluation["metrics"]["score"],
                                                "conflicts": {
                                                    "spatial_overlaps": len(evaluation["spatial_overlaps"]),
                                                    "dependency": sum(
                                                        len(evaluation["dependency_conflicts"][k])
                                                        for k in ("missing", "cross_corridor", "cycles")),
                                                    "budget_competition": len(evaluation["budget_competition"]),
                                                }}

            return self._idempotent(connection, request_id=request_id,
                                    action="freeze_round", payload=payload, create=create)

    def list_rounds(self) -> list[dict[str, Any]]:
        rows = self.database.connection.execute(
            "SELECT * FROM rounds ORDER BY created_at, round_id").fetchall()
        return [{"round_id": row["round_id"], "title": row["title"],
                 "years": json.loads(row["years_json"]), "status": row["status"],
                 "parent_round_id": row["parent_round_id"],
                 "effective_version_id": row["effective_version_id"],
                 "effective_plan_id": row["effective_plan_id"],
                 "created_at": row["created_at"], "frozen_at": row["frozen_at"]} for row in rows]

    def get_version(self, version_id: str) -> dict[str, Any]:
        row = self.database.connection.execute("SELECT * FROM versions WHERE version_id=?",
                                               (version_id,)).fetchone()
        if row is None:
            raise NotFoundError("版本不存在")
        snapshot = json.loads(row["snapshot_json"])
        return {"version_id": row["version_id"], "round_id": row["round_id"],
                "sequence_no": row["sequence_no"],
                "parent_version_id": row["parent_version_id"],
                "snapshot_hash": row["snapshot_hash"], "rules_hash": row["rules_hash"],
                "version_hash": row["version_hash"],
                "snapshot_summary": {"corridors": len(snapshot["corridors"]),
                                     "projects": len(snapshot["projects"]),
                                     "policy_goals": len(snapshot["policy_goals"])},
                "rules": json.loads(row["rules_json"]),
                "impact": json.loads(row["impact_json"]),
                "created_by": row["created_by"], "created_at": row["created_at"]}

    def list_versions(self) -> list[dict[str, Any]]:
        rows = self.database.connection.execute(
            "SELECT version_id,round_id,sequence_no,parent_version_id,snapshot_hash,rules_hash,"
            "version_hash,created_at FROM versions ORDER BY sequence_no").fetchall()
        return [dict(row) for row in rows]

    def recompute_version(self, version_id: str) -> dict[str, Any]:
        """用冻结快照与规则原样复算推荐方案，并核对摘要一致性。"""

        row = self.database.connection.execute("SELECT * FROM versions WHERE version_id=?",
                                               (version_id,)).fetchone()
        if row is None:
            raise NotFoundError("版本不存在")
        snapshot = json.loads(row["snapshot_json"])
        rules = json.loads(row["rules_json"])
        evaluation = evaluate_snapshot(snapshot, rules)
        return {"version_id": version_id, "snapshot_hash": row["snapshot_hash"],
                "rules_hash": row["rules_hash"],
                "snapshot_hash_matches": digest(snapshot) == row["snapshot_hash"],
                "rules_hash_matches": digest(rules) == row["rules_hash"],
                "evaluation": evaluation}

    # -- 竞争方案与会签 -----------------------------------------------------

    def create_plan(self, *, request_id: str, actor_id: str, version_id: str, origin: str,
                    label: str, selected_project_ids: list[str] | None = None) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "version_id": version_id, "origin": origin,
                   "label": label, "selected_project_ids": selected_project_ids}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *SIGN_ROLES)
            version = connection.execute("SELECT * FROM versions WHERE version_id=?",
                                         (version_id,)).fetchone()
            if version is None:
                raise NotFoundError("版本不存在")
            snapshot = json.loads(version["snapshot_json"])
            rules = json.loads(version["rules_json"])
            if origin == "recommended":
                evaluation = evaluate_snapshot(snapshot, rules)
                decisions = evaluation["decisions"]
            elif origin == "manual":
                ids = selected_project_ids or []
                available = {item["project_id"] for item in snapshot["projects"]}
                unknown = sorted(set(ids) - available)
                if unknown:
                    raise ValidationError(f"方案引用了快照之外的项目: {unknown}")
                if len(ids) != len(set(ids)):
                    raise ValidationError("方案内项目不能重复")
                evaluation = evaluate_snapshot(snapshot, rules, selected_ids=ids)
                decisions = evaluation["decisions"]
            else:
                raise ValidationError("origin 只能是 recommended 或 manual")
            plan_id = "plan-" + uuid.uuid4().hex[:12]

            def create():
                now = self._now()
                connection.execute(
                    "INSERT INTO plans(plan_id,version_id,round_id,origin,label,decisions_json,"
                    "decisions_hash,evaluation_json,evaluation_hash,status,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?, 'proposed',?,?)",
                    (plan_id, version_id, version["round_id"], origin, label,
                     canonical_json(decisions), digest(decisions),
                     canonical_json(evaluation), digest(evaluation), actor_id, now),
                )
                append_event(connection, actor_id=actor_id, action="plan.proposed",
                             resource_type="plan", resource_id=plan_id,
                             detail={"version_id": version_id, "round_id": version["round_id"],
                                     "origin": origin, "score": evaluation["metrics"]["score"]},
                             occurred_at=now)
                return "plan", plan_id, {"plan_id": plan_id, "status": "proposed",
                                         "score": evaluation["metrics"]["score"],
                                         "within_budget": evaluation["metrics"]["within_budget"]}

            return self._idempotent(connection, request_id=request_id,
                                    action="create_plan", payload=payload, create=create)

    def _plan_view(self, row) -> dict[str, Any]:
        signatures = self.database.connection.execute(
            "SELECT * FROM plan_signatures WHERE plan_id=? ORDER BY signed_at, reviewer_id",
            (row["plan_id"],)).fetchall()
        version = self.database.connection.execute("SELECT rules_json FROM versions WHERE version_id=?",
                                                   (row["version_id"],)).fetchone()
        rules = json.loads(version["rules_json"])
        signers = [sig["reviewer_id"] for sig in signatures if sig["decision"] == "approve"]
        return {"plan_id": row["plan_id"], "version_id": row["version_id"],
                "round_id": row["round_id"], "origin": row["origin"], "label": row["label"],
                "status": row["status"], "decisions": json.loads(row["decisions_json"]),
                "evaluation": json.loads(row["evaluation_json"]),
                "created_by": row["created_by"], "created_at": row["created_at"],
                "required_approvals": rules["required_approvals"],
                "approvers": rules["approvers"],
                "approvals": signers,
                "pending_approvers": [a for a in rules["approvers"] if a not in signers],
                "signatures": [{"reviewer_id": sig["reviewer_id"], "decision": sig["decision"],
                                "comment": sig["comment"], "signed_at": sig["signed_at"]}
                               for sig in signatures]}

    def get_plan(self, plan_id: str) -> dict[str, Any]:
        row = self.database.connection.execute("SELECT * FROM plans WHERE plan_id=?",
                                               (plan_id,)).fetchone()
        if row is None:
            raise NotFoundError("方案不存在")
        return self._plan_view(row)

    def list_plans(self, round_id: str) -> list[dict[str, Any]]:
        rows = self.database.connection.execute(
            "SELECT * FROM plans WHERE round_id=? ORDER BY created_at, plan_id", (round_id,)).fetchall()
        return [self._plan_view(row) for row in rows]

    def recompute_plan(self, plan_id: str) -> dict[str, Any]:
        """复算方案评价并核对与冻结时评价摘要一致。"""

        row = self.database.connection.execute("SELECT * FROM plans WHERE plan_id=?",
                                               (plan_id,)).fetchone()
        if row is None:
            raise NotFoundError("方案不存在")
        version = self.database.connection.execute("SELECT * FROM versions WHERE version_id=?",
                                                   (row["version_id"],)).fetchone()
        snapshot = json.loads(version["snapshot_json"])
        rules = json.loads(version["rules_json"])
        decisions = json.loads(row["decisions_json"])
        selected = [item["project_id"] for item in decisions if item["action"] == "schedule"]
        evaluation = evaluate_snapshot(snapshot, rules,
                                       selected_ids=selected if row["origin"] == "manual" else None)
        return {"plan_id": plan_id,
                "evaluation_hash_matches": digest(evaluation) == row["evaluation_hash"],
                "decisions_hash_matches": digest(decisions) == row["decisions_hash"],
                "evaluation": evaluation}

    def sign_plan(self, *, request_id: str, actor_id: str, plan_id: str,
                  decision: str, comment: str = "") -> dict[str, Any]:
        payload = {"actor_id": actor_id, "plan_id": plan_id, "decision": decision,
                   "comment": comment}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *SIGN_ROLES)
            replay = self._replay_if_seen(connection, request_id=request_id,
                                          action="sign_plan", payload=payload)
            if replay is not None:
                return replay
            plan = connection.execute("SELECT * FROM plans WHERE plan_id=?", (plan_id,)).fetchone()
            if plan is None:
                raise NotFoundError("方案不存在")
            version = connection.execute("SELECT * FROM versions WHERE version_id=?",
                                         (plan["version_id"],)).fetchone()
            rules = json.loads(version["rules_json"])
            if actor_id not in rules["approvers"]:
                raise PermissionDenied("操作者不是该轮次指定的会签人")
            if decision not in ("approve", "reject"):
                raise ValidationError("decision 只能是 approve 或 reject")
            existing = connection.execute(
                "SELECT * FROM plan_signatures WHERE plan_id=? AND reviewer_id=?",
                (plan_id, actor_id)).fetchone()
            if existing is not None:
                raise ConflictError("会签人已经签署，不能重复表决")
            if plan["status"] in ("rejected", "superseded"):
                raise ConflictError("方案已经终结，不能再签署")
            now = self._now()
            sign_result: dict[str, Any] = {}

            def create():
                connection.execute(
                    "INSERT INTO plan_signatures(plan_id,reviewer_id,decision,comment,signed_at) "
                    "VALUES(?,?,?,?,?)", (plan_id, actor_id, decision, comment, now))
                new_status = plan["status"]
                effective = False
                if decision == "reject":
                    if plan["status"] == "effective":
                        raise ConflictError("已生效方案不能被否决")
                    new_status = "rejected"
                    connection.execute("UPDATE plans SET status='rejected' WHERE plan_id=?",
                                       (plan_id,))
                else:
                    approvals = connection.execute(
                        "SELECT COUNT(*) AS c FROM plan_signatures WHERE plan_id=? AND decision='approve'",
                        (plan_id,)).fetchone()["c"]
                    if plan["status"] == "effective":
                        new_status = "effective"
                        effective = True
                    elif approvals >= rules["required_approvals"]:
                        existing_effective = connection.execute(
                            "SELECT plan_id FROM plans WHERE round_id=? AND status='effective'",
                            (plan["round_id"],)).fetchone()
                        if existing_effective is not None and existing_effective["plan_id"] != plan_id:
                            raise ConflictError("该轮次已有生效方案，竞争审批只能产生一个生效方案")
                        self._assert_effectivable(connection, plan, json.loads(version["snapshot_json"]),
                                                  rules)
                        new_status = "effective"
                        effective = True
                        connection.execute(
                            "UPDATE plans SET status='effective' WHERE plan_id=?", (plan_id,))
                        connection.execute(
                            "UPDATE plans SET status='superseded' WHERE round_id=? AND plan_id<>?",
                            (plan["round_id"], plan_id))
                        connection.execute(
                            "UPDATE rounds SET effective_version_id=?,effective_plan_id=? "
                            "WHERE round_id=?",
                            (plan["version_id"], plan_id, plan["round_id"]))
                append_event(connection, actor_id=actor_id,
                             action="plan.rejected" if decision == "reject" else "plan.approved",
                             resource_type="plan", resource_id=plan_id,
                             detail={"status": new_status, "became_effective": effective,
                                     "round_id": plan["round_id"], "comment": comment},
                             occurred_at=now)
                response = {"plan_id": plan_id, "status": new_status,
                            "became_effective": effective}
                sign_result.update(response)
                return "plan_signature", plan_id, response

            receipt = self._idempotent(connection, request_id=request_id,
                                       action="sign_plan", payload=payload, create=create)
            if receipt["replayed"]:
                current = connection.execute("SELECT status FROM plans WHERE plan_id=?",
                                             (plan_id,)).fetchone()
                receipt.update({"status": current["status"],
                                "became_effective": current["status"] == "effective"})
            else:
                receipt.update(sign_result)
            return receipt

    def _assert_effectivable(self, connection, plan, snapshot: dict[str, Any],
                             rules: dict[str, Any]) -> None:
        """生效前复核资金硬约束与入选项目间的依赖冲突。"""

        evaluation = json.loads(plan["evaluation_json"])
        metrics = evaluation["metrics"]
        if not metrics["within_budget"]:
            raise ConflictError("方案突破年度预算上限，不能生效")
        decisions = json.loads(plan["decisions_json"])
        selected = {item["project_id"] for item in decisions if item["action"] == "schedule"}
        projects = {item["project_id"]: item for item in snapshot["projects"]}
        for project_id in selected:
            project = projects[project_id]
            for prerequisite in project.get("prerequisite_project_ids", []):
                if prerequisite in projects and prerequisite not in selected:
                    raise ConflictError(f"项目 {project_id} 的前置工程 {prerequisite} 未纳入方案")
        dep = evaluation["dependency_conflicts"]
        selected_cycles = [cycle for cycle in dep["cycles"] if set(cycle) & selected]
        if selected_cycles:
            raise ConflictError("入选项目存在依赖环，不能生效")
