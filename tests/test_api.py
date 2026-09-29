import unittest
from datetime import datetime, timezone

from transport_coordination.api import Services, route
from transport_coordination.clock import FixedClock
from transport_coordination.storage import Database


RULES = {
    "weights": {"backbone": 3.0, "connectivity": 1.0, "border_coverage": 2.0,
                "rural_coverage": 2.0, "bottleneck_relief": 2.0, "policy_alignment": 1.0},
    "budgets": {"2026": 70.0},
    "approvers": ["rv1", "rv2"],
}


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        clock = FixedClock(datetime(2026, 9, 29, tzinfo=timezone.utc))
        self.services = Services(self.database, clock)
        route(self.services, "POST", "/organizations",
              {"request_id": "org", "organization_id": "o1", "name": "专班"},
              {"X-Actor-Id": "bootstrap"})
        for rid, aid, role, name in [
                ("adm", "adm1", "admin", "管"),
                ("ra", "rv1", "reviewer", "甲"),
                ("rb", "rv2", "reviewer", "乙"),
                ("ro", "op1", "operator", "员")]:
            route(self.services, "POST", "/actors",
                  {"request_id": rid, "new_actor_id": aid, "display_name": name,
                   "role": role, "organization_id": "o1"},
                  {"X-Actor-Id": "bootstrap" if aid == "adm1" else "adm1"})

    def tearDown(self):
        self.database.close()

    def _request(self, method, path, body=None, actor="op1"):
        return route(self.services, method, path, body, {"X-Actor-Id": actor})

    def test_health_is_available_without_actor(self):
        status, payload = route(self.services, "GET", "/health", None)
        self.assertEqual(200, status)
        self.assertEqual("ok", payload["status"])

    def test_unknown_route_returns_404(self):
        status, payload = route(self.services, "GET", "/missing", None)
        self.assertEqual(404, status)
        self.assertEqual("route_not_found", payload["error"])

    def test_invalid_json_shape_returns_400(self):
        status, payload = route(self.services, "POST", "/organizations", {"request_id": "x"},
                                {"X-Actor-Id": "bootstrap"})
        self.assertEqual(400, status)
        self.assertEqual("invalid_request", payload["error"])

    def _seed_frozen_round(self):
        self._request("POST", "/rounds", {"request_id": "rd", "round_id": "rd1",
                                          "title": "首", "years": ["2026"]}, actor="rv1")
        status, _ = self._request("POST", "/corridors", {
            "request_id": "co", "corridor_id": "c1", "name": "沿海", "direction": "coastal",
            "nodes": ["A", "B", "C"],
            "edges": [{"edge_id": "e1", "from": "A", "to": "B"},
                      {"edge_id": "e2", "from": "B", "to": "C"}],
            "bottlenecks": [{"bottleneck_id": "b1", "edge_id": "e1", "capacity_gap": 30}],
            "service_gaps": [{"area_id": "g-b", "kind": "border"}],
            "backbone_pairs": [{"from": "A", "to": "C"}]})
        self.assertEqual(201, status)
        self._request("POST", "/projects", {
            "request_id": "p1", "project_id": "p1", "corridor_id": "c1", "name": "一段",
            "builder": "院", "investments": {"2026": 50.0}, "benefit_year": "2026",
            "bottleneck_ids": ["b1"], "service_areas": ["g-b"], "footprint": ["seg"]})
        status, payload = self._request("POST", "/rounds/freeze",
                                        {"request_id": "fz", "round_id": "rd1", "rules": RULES},
                                        actor="rv1")
        self.assertEqual(200, status)
        return payload

    def test_full_planning_flow_over_http(self):
        frozen = self._seed_frozen_round()
        self.assertEqual("v001", frozen["resource_id"])
        status, payload = self._request("GET", "/versions/v001/recompute", actor="rv1")
        self.assertEqual(200, status)
        self.assertTrue(payload["snapshot_hash_matches"])
        self.assertEqual(1, payload["evaluation"]["metrics"]["backbone_connected_pairs"])

        status, payload = self._request("POST", "/plans",
                                        {"request_id": "pl", "version_id": "v001",
                                         "origin": "recommended", "label": "推荐"}, actor="rv1")
        self.assertEqual(201, status)
        status, payload = self._request("GET", "/plans?round_id=rd1", actor="rv1")
        self.assertEqual(200, status)
        plan_id = payload["items"][0]["plan_id"]

        status, payload = self._request("POST", "/plans/sign",
                                        {"request_id": "s1", "plan_id": plan_id,
                                         "decision": "approve"}, actor="rv1")
        self.assertEqual(200, status)
        self.assertFalse(payload["became_effective"])
        status, payload = self._request("POST", "/plans/sign",
                                        {"request_id": "s2", "plan_id": plan_id,
                                         "decision": "approve"}, actor="rv2")
        self.assertEqual(200, status)
        self.assertTrue(payload["became_effective"])

        status, payload = self._request("GET", f"/plans/{plan_id}/recompute", actor="rv1")
        self.assertEqual(200, status)
        self.assertTrue(payload["evaluation_hash_matches"])

        status, payload = self._request("GET", "/rounds", actor="rv1")
        self.assertEqual(plan_id, payload["items"][0]["effective_plan_id"])

    def test_lineage_endpoint_records_split(self):
        self._request("POST", "/rounds", {"request_id": "rd", "round_id": "rd1",
                                          "title": "首", "years": ["2026"]}, actor="rv1")
        self._request("POST", "/corridors", {
            "request_id": "co", "corridor_id": "c1", "name": "出疆", "direction": "xinjiang_tibet",
            "nodes": ["U", "V"], "edges": [{"edge_id": "e1", "from": "U", "to": "V"}],
            "bottlenecks": [{"bottleneck_id": "b1", "edge_id": "e1", "capacity_gap": 10}],
            "service_gaps": [], "backbone_pairs": []})
        self._request("POST", "/projects", {
            "request_id": "big", "project_id": "big", "corridor_id": "c1", "name": "大",
            "builder": "联", "investments": {"2026": 80.0}, "benefit_year": "2026",
            "bottleneck_ids": ["b1"], "footprint": ["seg"]})
        status, _ = self._request("POST", "/projects/split", {
            "request_id": "sp", "project_id": "big", "splits": [
                {"project_id": "big-a", "name": "a", "builder": "院",
                 "investments": {"2026": 30.0}, "benefit_year": "2026",
                 "bottleneck_ids": ["b1"], "footprint": ["seg"]},
                {"project_id": "big-b", "name": "b", "builder": "院",
                 "investments": {"2026": 50.0}, "benefit_year": "2026",
                 "footprint": ["seg"]}]})
        self.assertEqual(200, status)
        status, payload = self._request("GET", "/projects/big/lineage", actor="rv1")
        self.assertEqual(200, status)
        self.assertEqual(["split", "split"], [e["relation"] for e in payload["lineage"]])


if __name__ == "__main__":
    unittest.main()
