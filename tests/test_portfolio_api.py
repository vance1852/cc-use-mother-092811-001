import unittest

from transport_coordination.api import route
from transport_coordination.portfolio import PortfolioService
from transport_coordination.storage import Database


class PortfolioApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.service = PortfolioService(self.database)
        self._seed()

    def tearDown(self):
        self.database.close()

    def _call(self, method, path, body=None, actor="admin"):
        return route(self.service, method, path, body or {},
                     {"X-Actor-Id": actor})

    def _seed(self):
        s = self.service
        s.register_organization(request_id="org", actor_id="bootstrap",
                                organization_id="o1", name="专班")
        s.register_actor(request_id="adm", actor_id="bootstrap", new_actor_id="admin",
                         display_name="管理员", role="admin", organization_id="o1")
        s.register_actor(request_id="rev", actor_id="admin", new_actor_id="reviewer",
                         display_name="评审", role="reviewer", organization_id="o1")
        s.register_actor(request_id="op", actor_id="admin", new_actor_id="operator",
                         display_name="申报", role="operator", organization_id="o1")

    def _network_and_round(self):
        self._call("POST", "/nodes",
                   {"request_id": "n1", "node_id": "h1", "name": "枢纽", "kind": "hub"}, "operator")
        self._call("POST", "/nodes",
                   {"request_id": "n2", "node_id": "b1", "name": "口岸", "kind": "border"}, "operator")
        self._call("POST", "/edges",
                   {"request_id": "e1", "edge_id": "e1", "corridor_id": "c1", "from_node": "h1",
                    "to_node": "b1", "exists": False, "current_capacity": 0,
                    "required_capacity": 10}, "operator")
        self._call("POST", "/gaps",
                   {"request_id": "g1", "gap_id": "g1", "corridor_id": "c1", "edge_id": "e1",
                    "current_capacity": 0, "required_capacity": 10}, "operator")
        self._call("POST", "/service-areas",
                   {"request_id": "ab", "area_id": "ab", "name": "边境", "kind": "border",
                    "node_ids": ["b1"]}, "operator")
        self._call("POST", "/projects",
                   {"request_id": "p1", "project_id": "p1", "name": "贯通", "corridor_id": "c1",
                    "building_unit": "u1", "edge_ids": ["e1"], "gap_ids": ["g1"],
                    "prerequisite_project_ids": [], "yearly_investment": {"2026": 30}}, "operator")
        status, _ = self._call("POST", "/rounds",
                               {"request_id": "r1", "round_id": "r1", "title": "评审",
                                "plan_year": 2026,
                                "rules": {"budgets": {"2026": 100, "2027": 100},
                                          "horizon": [2026, 2027], "hubs": ["h1"]}}, "reviewer")
        self.assertEqual(201, status)
        status, body = self._call("POST", "/rounds/r1/freeze", {}, "reviewer")
        self.assertEqual(200, status)
        self.assertEqual(64, len(body["snapshot_hash"]))

    def test_full_http_flow(self):
        self._network_and_round()
        status, body = self._call("POST", "/proposals",
                                  {"request_id": "pr1", "round_id": "r1", "proposal_id": "pr1",
                                   "items": [{"project_id": "p1"}]}, "reviewer")
        self.assertEqual(201, status)
        self.assertTrue(body["response"]["evaluation"]["within_budget"])

        status, body = self._call("GET", "/proposals/pr1")
        self.assertEqual(200, status)
        self.assertEqual(["admin", "reviewer"], body["signoff"]["required_roles"])

        status, body = self._call("POST", "/proposals/pr1/recalculate", {})
        self.assertEqual(200, status)
        self.assertTrue(body["hash_matches"])

        status, body = self._call("POST", "/proposals/pr1/sign",
                                  {"request_id": "s1", "decision": "approve", "comment": "同意"})
        self.assertEqual(200, status)
        self.assertEqual("submitted", body["response"]["status"])
        status, body = self._call("POST", "/proposals/pr1/sign",
                                  {"request_id": "s2", "decision": "approve"}, "reviewer")
        self.assertEqual(200, status)
        self.assertEqual("effective", body["response"]["status"])

        status, body = self._call("GET", "/rounds/r1")
        self.assertEqual(200, status)
        self.assertEqual("closed", body["status"])
        self.assertEqual("pr1", body["effective_proposal_id"])

        status, body = self._call("GET", "/rounds/r1/proposals")
        self.assertEqual(200, status)
        self.assertEqual("effective", body["items"][0]["status"])

    def test_project_versions_and_lineage_routes(self):
        self._network_and_round()
        status, _ = self._call("POST", "/project-adjustments",
                               {"request_id": "d1", "project_id": "p1", "action": "delay",
                                "target_year": 2027, "reason": "顺延"}, "operator")
        self.assertEqual(201, status)
        status, body = self._call("GET", "/projects/p1/versions")
        self.assertEqual(200, status)
        self.assertEqual([1, 2], [item["version"] for item in body["items"]])
        status, body = self._call("GET", "/lineage?project_id=p1")
        self.assertEqual(200, status)
        self.assertEqual("delay", body["items"][0]["relation"])

    def test_missing_actor_is_rejected_on_protected_route(self):
        status, body = route(self.service, "POST", "/nodes",
                             {"request_id": "x1", "node_id": "h1", "name": "x", "kind": "k"},
                             {"X-Actor-Id": ""})
        self.assertEqual(404, status)


if __name__ == "__main__":
    unittest.main()
