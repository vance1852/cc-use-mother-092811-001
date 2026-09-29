"""提供不依赖第三方框架的 HTTP/JSON 边界。"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .errors import DomainError, ValidationError
from .planning import PlanningService
from .service import DomainService
from .storage import Database


def route(services: "Services", method: str, path: str, body: dict[str, Any] | None,
          headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    """把一个 HTTP 语义请求分派到领域服务。"""

    headers = headers or {}
    body = body or {}
    parsed = urlparse(path)
    query = parse_qs(parsed.query)
    actor_id = headers.get("X-Actor-Id", "")
    service = services.domain
    planning = services.planning
    parts = [segment for segment in parsed.path.split("/") if segment]
    try:
        if method == "GET" and parsed.path == "/health":
            valid, count = service.verify_audit()
            return 200, {"status": "ok", "audit_valid": valid, "audit_events": count}
        if method == "POST" and parsed.path == "/organizations":
            receipt = service.register_organization(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/actors":
            receipt = service.register_actor(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/sites":
            receipt = service.register_site(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/domain-records":
            receipt = service.record_domain_data(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "GET" and parsed.path == "/domain-records":
            site_id = query.get("site_id", [""])[0]
            if not site_id:
                raise ValidationError("site_id 不能为空")
            category = query.get("category", [None])[0]
            return 200, {"items": [item.__dict__ for item in service.list_domain_data(site_id, category)]}
        if method == "GET" and parsed.path == "/audit-events":
            after = int(query.get("after_sequence", ["0"])[0])
            return 200, {"items": service.audit_events(after)}

        # -- 通道组合决策 --------------------------------------------------
        if method == "POST" and parsed.path == "/policy-goals":
            receipt = planning.register_policy_goal(
                actor_id=actor_id,
                **_fields(body, "request_id", "goal_code", "name", "description"))
            return 200 if receipt["replayed"] else 201, receipt
        if method == "POST" and parsed.path == "/corridors":
            receipt = planning.register_corridor(
                actor_id=actor_id,
                **_fields(body, "request_id", "corridor_id", "name", "direction", "nodes",
                          "edges", "bottlenecks", "service_gaps", "backbone_pairs"))
            return 200 if receipt["replayed"] else 201, receipt
        if method == "POST" and parsed.path == "/corridors/update":
            receipt = planning.update_corridor(actor_id=actor_id, **body)
            return 200 if receipt["replayed"] else 200, receipt
        if method == "GET" and len(parts) == 2 and parts[0] == "corridors":
            return 200, planning.get_corridor(parts[1])
        if method == "POST" and parsed.path == "/projects":
            receipt = planning.register_project(
                actor_id=actor_id,
                **_fields(body, "request_id", "project_id", "corridor_id", "name", "builder",
                          "investments", "footprint", "bottleneck_ids",
                          "prerequisite_project_ids", "service_areas", "policy_goals",
                          "duration_years", "benefit_year", "target_capacity", "priority"))
            return 200 if receipt["replayed"] else 201, receipt
        if method == "POST" and parsed.path == "/projects/update":
            receipt = planning.update_project(actor_id=actor_id, request_id=body["request_id"],
                                              project_id=body["project_id"],
                                              changes=body.get("changes", {}))
            return 200, receipt
        if method == "POST" and parsed.path == "/projects/split":
            receipt = planning.split_project(actor_id=actor_id, request_id=body["request_id"],
                                             project_id=body["project_id"],
                                             splits=body["splits"], note=body.get("note", ""))
            return 200, receipt
        if method == "POST" and parsed.path == "/projects/merge":
            receipt = planning.merge_projects(actor_id=actor_id, request_id=body["request_id"],
                                              project_ids=body["project_ids"],
                                              merged=body["merged"], note=body.get("note", ""))
            return 200, receipt
        if method == "POST" and parsed.path == "/projects/defer":
            receipt = planning.defer_project(actor_id=actor_id, request_id=body["request_id"],
                                             project_id=body["project_id"],
                                             new_benefit_year=body["new_benefit_year"],
                                             investments=body["investments"],
                                             note=body.get("note", ""))
            return 200, receipt
        if method == "POST" and parsed.path == "/projects/exit":
            receipt = planning.exit_project(actor_id=actor_id, request_id=body["request_id"],
                                            project_id=body["project_id"], reason=body["reason"])
            return 200, receipt
        if method == "GET" and parsed.path == "/projects":
            return 200, {"items": planning.list_projects(query.get("corridor_id", [None])[0])}
        if method == "GET" and len(parts) == 3 and parts[0] == "projects" and parts[2] == "lineage":
            return 200, planning.project_lineage(parts[1])
        if method == "POST" and parsed.path == "/rounds":
            receipt = planning.create_round(
                actor_id=actor_id,
                **_fields(body, "request_id", "round_id", "title", "years", "parent_round_id"))
            return 200 if receipt["replayed"] else 201, receipt
        if method == "POST" and parsed.path == "/rounds/freeze":
            receipt = planning.freeze_round(actor_id=actor_id, request_id=body["request_id"],
                                            round_id=body["round_id"], rules=body["rules"])
            return 200, receipt
        if method == "GET" and parsed.path == "/rounds":
            return 200, {"items": planning.list_rounds()}
        if method == "GET" and parsed.path == "/versions":
            return 200, {"items": planning.list_versions()}
        if method == "GET" and len(parts) == 2 and parts[0] == "versions":
            return 200, planning.get_version(parts[1])
        if method == "GET" and len(parts) == 3 and parts[0] == "versions" and parts[2] == "recompute":
            return 200, planning.recompute_version(parts[1])
        if method == "POST" and parsed.path == "/plans":
            receipt = planning.create_plan(
                actor_id=actor_id,
                **_fields(body, "request_id", "version_id", "origin", "label",
                          "selected_project_ids"))
            return 200 if receipt["replayed"] else 201, receipt
        if method == "GET" and parsed.path == "/plans":
            round_id = query.get("round_id", [""])[0]
            if not round_id:
                raise ValidationError("round_id 不能为空")
            return 200, {"items": planning.list_plans(round_id)}
        if method == "POST" and parsed.path == "/plans/sign":
            receipt = planning.sign_plan(actor_id=actor_id, request_id=body["request_id"],
                                         plan_id=body["plan_id"], decision=body["decision"],
                                         comment=body.get("comment", ""))
            return 200, receipt
        if method == "GET" and len(parts) == 2 and parts[0] == "plans":
            return 200, planning.get_plan(parts[1])
        if method == "GET" and len(parts) == 3 and parts[0] == "plans" and parts[2] == "recompute":
            return 200, planning.recompute_plan(parts[1])
        return 404, {"error": "route_not_found", "message": "接口不存在"}
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError, KeyError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


def _fields(body: dict[str, Any], *names: str) -> dict[str, Any]:
    return {name: body[name] for name in names if name in body}


class Services:
    """聚合基础登记服务与通道组合决策服务。"""

    def __init__(self, database: Database, clock: Any = None) -> None:
        self.domain = DomainService(database, clock)
        self.planning = PlanningService(database, clock)


class Handler(BaseHTTPRequestHandler):
    """把标准库 HTTP 请求转换为路由调用。"""

    services: Services

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write(400, {"error": "invalid_json", "message": "请求体必须是 UTF-8 JSON"})
            return
        status, payload = route(self.services, self.command, self.path, body,
                                {"X-Actor-Id": self.headers.get("X-Actor-Id", "")})
        self._write(status, payload)

    def _write(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> int:
    """启动本地 HTTP 服务。"""

    parser = argparse.ArgumentParser(description="启动综合交通通道项目组合决策服务")
    parser.add_argument("--database", default="service.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    database = Database(args.database)
    Handler.services = Services(database)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
