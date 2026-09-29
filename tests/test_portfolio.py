import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from transport_coordination.clock import FixedClock
from transport_coordination.errors import ConflictError, PermissionDenied, ValidationError
from transport_coordination.portfolio import PortfolioService
from transport_coordination.storage import Database


class PortfolioServiceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "portfolio.sqlite3"
        self.database = Database(self.db_path)
        self.service = PortfolioService(
            self.database, FixedClock(datetime(2026, 9, 29, tzinfo=timezone.utc)))
        self._bootstrap()
        self._network()

    def tearDown(self):
        self.database.close()
        self.temp.cleanup()

    def _bootstrap(self):
        s = self.service
        s.register_organization(request_id="org", actor_id="bootstrap",
                                organization_id="o1", name="专班")
        s.register_actor(request_id="adm", actor_id="bootstrap", new_actor_id="admin",
                         display_name="管理员", role="admin", organization_id="o1")
        s.register_actor(request_id="rev", actor_id="admin", new_actor_id="reviewer",
                         display_name="评审", role="reviewer", organization_id="o1")
        s.register_actor(request_id="op", actor_id="admin", new_actor_id="operator",
                         display_name="申报", role="operator", organization_id="o1")

    def _network(self):
        s = self.service
        for node in ("h1", "n2", "b1"):
            s.register_node(request_id="node-" + node, actor_id="operator", node_id=node,
                            name=node, kind="hub" if node == "h1" else "x")
        s.register_edge(request_id="e1", actor_id="operator", edge_id="e1", corridor_id="c1",
                        from_node="h1", to_node="n2", exists=True,
                        current_capacity=5, required_capacity=10)
        s.register_edge(request_id="e2", actor_id="operator", edge_id="e2", corridor_id="c1",
                        from_node="n2", to_node="b1", exists=False,
                        current_capacity=0, required_capacity=10)
        s.register_gap(request_id="g2", actor_id="operator", gap_id="g2", corridor_id="c1",
                       edge_id="e2", current_capacity=0, required_capacity=10)
        s.register_service_area(request_id="ab", actor_id="operator", area_id="ab",
                                name="边境", kind="border", node_ids=["b1"])
        s.register_project(request_id="p1", actor_id="operator", project_id="p1", name="扩能",
                           corridor_id="c1", building_unit="u1", edge_ids=["e1"], gap_ids=[],
                           prerequisite_project_ids=[], yearly_investment={"2026": 40})
        s.register_project(request_id="p2", actor_id="operator", project_id="p2", name="贯通",
                           corridor_id="c1", building_unit="u1", edge_ids=["e2"], gap_ids=["g2"],
                           prerequisite_project_ids=["p1"], yearly_investment={"2026": 30})

    def _round(self, round_id="r1"):
        self.service.create_round(
            request_id="round-" + round_id, actor_id="reviewer", round_id=round_id,
            title="评审", plan_year=2026,
            rules={"budgets": {"2026": 100, "2027": 100}, "horizon": [2026, 2027],
                   "hubs": ["h1"]})
        return self.service.freeze_round(actor_id="reviewer", round_id=round_id)

    def test_freeze_is_idempotent_snapshot_and_recalc_matches(self):
        frozen = self._round()
        proposal = self.service.create_proposal(
            request_id="prop", actor_id="reviewer", round_id="r1", proposal_id="pr1",
            items=[{"project_id": "p1"}, {"project_id": "p2"}])
        self.assertTrue(proposal["response"]["evaluation"]["within_budget"])
        recalculated = self.service.recalculate_proposal("pr1")
        self.assertTrue(recalculated["hash_matches"])
        self.assertTrue(recalculated["snapshot_hash_matches"])
        self.assertEqual(frozen["snapshot_hash"], recalculated["frozen_snapshot_hash"])
        self.assertEqual(64, len(frozen["snapshot_hash"]))

    def test_cannot_freeze_twice_or_submit_before_freeze(self):
        self.service.create_round(
            request_id="round-r2", actor_id="reviewer", round_id="r2", title="评审",
            plan_year=2026, rules={"horizon": [2026], "hubs": ["h1"]})
        with self.assertRaises(ConflictError):
            self.service.create_proposal(
                request_id="early", actor_id="reviewer", round_id="r2", proposal_id="px",
                items=[{"project_id": "p1"}])
        self.service.freeze_round(actor_id="reviewer", round_id="r2")
        with self.assertRaises(ConflictError):
            self.service.freeze_round(actor_id="reviewer", round_id="r2")

    def test_operator_cannot_create_round(self):
        with self.assertRaises(PermissionDenied):
            self.service.create_round(
                request_id="denied", actor_id="operator", round_id="r9", title="x",
                plan_year=2026, rules={"horizon": [2026], "hubs": ["h1"]})

    def test_delayed_project_carries_version_and_impacts_approved_combo(self):
        self._round()
        self.service.create_proposal(
            request_id="prop", actor_id="reviewer", round_id="r1", proposal_id="pr1",
            items=[{"project_id": "p1"}, {"project_id": "p2"}])
        self.service.sign_proposal(request_id="s1", actor_id="admin",
                                   proposal_id="pr1", decision="approve")
        self.service.sign_proposal(request_id="s2", actor_id="reviewer",
                                   proposal_id="pr1", decision="approve")
        before = self.service.recalculate_proposal("pr1")
        adjustment = self.service.adjust_project(
            request_id="delay-p2", actor_id="operator", project_id="p2",
            action="delay", target_year=2027, reason="跨年度")
        self.assertEqual(2, adjustment["response"]["version"])
        impacts = adjustment["response"]["impacts"]
        self.assertEqual(["r1"], [item["round_id"] for item in impacts])
        # 已批准组合按冻结版本复算，不受调整影响。
        after = self.service.recalculate_proposal("pr1")
        self.assertEqual(before["recalculated_metrics_hash"],
                         after["recalculated_metrics_hash"])
        versions = self.service.list_project_versions("p2")
        self.assertEqual([1, 2], [item["version"] for item in versions])

    def test_exit_project_versions_and_explains_gap_retained(self):
        self._round()
        self.service.create_proposal(
            request_id="prop", actor_id="reviewer", round_id="r1", proposal_id="pr1",
            items=[{"project_id": "p1"}, {"project_id": "p2"}])
        self.service.sign_proposal(request_id="s1", actor_id="admin",
                                   proposal_id="pr1", decision="approve")
        self.service.sign_proposal(request_id="s2", actor_id="reviewer",
                                   proposal_id="pr1", decision="approve")
        exited = self.service.adjust_project(
            request_id="exit-p2", actor_id="operator", project_id="p2",
            action="exit", reason="退出")
        self.assertEqual("exited", exited["response"]["status"])
        with self.assertRaises(ConflictError):
            self.service.adjust_project(
                request_id="again", actor_id="operator", project_id="p2",
                action="delay", target_year=2027, reason="再调")

    def test_split_and_merge_lineage(self):
        split = self.service.adjust_project(
            request_id="split", actor_id="operator", project_id="p2", action="split",
            reason="拆分", children=[
                {"project_id": "p2a", "name": "贯通A", "corridor_id": "c1",
                 "building_unit": "u1", "edge_ids": ["e2"], "gap_ids": ["g2"],
                 "prerequisite_project_ids": ["p1"], "yearly_investment": {"2026": 15}},
                {"project_id": "p2b", "name": "贯通B", "corridor_id": "c1",
                 "building_unit": "u1", "edge_ids": ["e2"], "gap_ids": ["g2"],
                 "prerequisite_project_ids": ["p1"], "yearly_investment": {"2026": 15}},
            ])
        self.assertEqual(["p2a", "p2b"], split["response"]["children"])
        merged = self.service.adjust_project(
            request_id="merge", actor_id="operator", project_id="p-merged", action="merge",
            reason="合并", source_project_ids=["p1", "p2a"],
            name="合并段", corridor_id="c1", building_unit="u1", edge_ids=["e1", "e2"],
            gap_ids=["g2"], prerequisite_project_ids=[],
            yearly_investment={"2026": 55})
        self.assertEqual("registered", merged["response"]["status"])
        relations = {item["relation"] for item in self.service.list_lineage()}
        self.assertIn("split", relations)
        self.assertIn("merge", relations)

    def test_only_one_proposal_becomes_effective(self):
        self._round()
        self.service.create_proposal(
            request_id="req-a", actor_id="reviewer", round_id="r1", proposal_id="pa",
            items=[{"project_id": "p1"}, {"project_id": "p2"}])
        self.service.create_proposal(
            request_id="req-b", actor_id="reviewer", round_id="r1", proposal_id="pb",
            items=[{"project_id": "p1"}])
        self.service.sign_proposal(request_id="sa1", actor_id="admin",
                                   proposal_id="pa", decision="approve")
        self.service.sign_proposal(request_id="sb1", actor_id="admin",
                                   proposal_id="pb", decision="approve")
        self.service.sign_proposal(request_id="sb2", actor_id="reviewer",
                                   proposal_id="pb", decision="approve")
        self.assertEqual("effective", self.service.get_proposal("pb")["status"])
        # pa 已被竞争胜出者盖过，不能再生效。
        with self.assertRaises(ConflictError):
            self.service.sign_proposal(request_id="sa2", actor_id="reviewer",
                                       proposal_id="pa", decision="approve")
        self.assertEqual("superseded", self.service.get_proposal("pa")["status"])
        round_view = self.service.list_round_proposals("r1")
        self.assertEqual(1, sum(1 for item in round_view["items"]
                                if item["status"] == "effective"))

    def test_reject_closes_proposal_and_allows_competitor(self):
        self._round()
        self.service.create_proposal(
            request_id="req-a", actor_id="reviewer", round_id="r1", proposal_id="pa",
            items=[{"project_id": "p1"}, {"project_id": "p2"}])
        self.service.create_proposal(
            request_id="req-b", actor_id="reviewer", round_id="r1", proposal_id="pb",
            items=[{"project_id": "p1"}])
        self.service.sign_proposal(request_id="ra", actor_id="reviewer",
                                   proposal_id="pa", decision="reject", comment="不通过")
        self.assertEqual("rejected", self.service.get_proposal("pa")["status"])
        self.service.sign_proposal(request_id="sb1", actor_id="admin",
                                   proposal_id="pb", decision="approve")
        result = self.service.sign_proposal(request_id="sb2", actor_id="reviewer",
                                            proposal_id="pb", decision="approve")
        self.assertEqual("effective", result["response"]["status"])

    def test_signoff_resumes_after_service_restart(self):
        self._round()
        self.service.create_proposal(
            request_id="req-a", actor_id="reviewer", round_id="r1", proposal_id="pa",
            items=[{"project_id": "p1"}, {"project_id": "p2"}])
        self.service.sign_proposal(request_id="s1", actor_id="admin",
                                   proposal_id="pa", decision="approve")
        self.database.close()
        restarted_db = Database(self.db_path)
        restarted = PortfolioService(
            restarted_db, FixedClock(datetime(2026, 9, 29, 9, 0, tzinfo=timezone.utc)))
        pending = restarted.get_proposal("pa")
        self.assertEqual(["reviewer"], pending["signoff"]["missing_roles"])
        result = restarted.sign_proposal(request_id="s2", actor_id="reviewer",
                                         proposal_id="pa", decision="approve")
        self.assertEqual("effective", result["response"]["status"])
        restarted_db.close()

    def test_duplicate_signature_same_actor_rejected(self):
        self._round()
        self.service.create_proposal(
            request_id="req-a", actor_id="reviewer", round_id="r1", proposal_id="pa",
            items=[{"project_id": "p1"}])
        self.service.sign_proposal(request_id="s1", actor_id="admin",
                                   proposal_id="pa", decision="approve")
        with self.assertRaises(ConflictError):
            self.service.sign_proposal(request_id="s1b", actor_id="admin",
                                       proposal_id="pa", decision="approve")


if __name__ == "__main__":
    unittest.main()
