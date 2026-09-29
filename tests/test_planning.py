"""通道组合决策服务的规则测试。"""

import os
import tempfile
import unittest
from datetime import datetime, timezone

from transport_coordination.clock import FixedClock
from transport_coordination.errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from transport_coordination.planning import PlanningService
from transport_coordination.service import DomainService
from transport_coordination.storage import Database


RULES = {
    "weights": {"backbone": 3.0, "connectivity": 1.0, "border_coverage": 2.0,
                "rural_coverage": 2.0, "bottleneck_relief": 2.0, "policy_alignment": 1.0},
    "budgets": {"2026": 70.0, "2027": 100.0},
    "approvers": ["rv1", "rv2"],
    "required_approvals": 2,
}

CORRIDOR = {
    "corridor_id": "c1", "name": "沿海通道", "direction": "coastal",
    "nodes": ["A", "B", "C"],
    "edges": [{"edge_id": "e1", "from": "A", "to": "B"},
              {"edge_id": "e2", "from": "B", "to": "C"}],
    "bottlenecks": [{"bottleneck_id": "b1", "edge_id": "e1", "capacity_gap": 30},
                    {"bottleneck_id": "b2", "edge_id": "e2", "capacity_gap": 20}],
    "service_gaps": [{"area_id": "g-b", "kind": "border"},
                     {"area_id": "g-r", "kind": "rural"}],
    "backbone_pairs": [{"from": "A", "to": "C"}],
}


class PlanningTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        clock = FixedClock(datetime(2026, 9, 29, tzinfo=timezone.utc))
        self.domain = DomainService(self.database, clock)
        self.service = PlanningService(self.database, clock)
        self.domain.register_organization(request_id="org", actor_id="bootstrap",
                                          organization_id="o1", name="专班")
        self.domain.register_actor(request_id="adm", actor_id="bootstrap", new_actor_id="adm1",
                                   display_name="管理员", role="admin", organization_id="o1")
        for rid, aid, name, role in [
                ("ra", "rv1", "评审甲", "reviewer"),
                ("rb", "rv2", "评审乙", "reviewer"),
                ("ro", "op1", "规划员", "operator"),
                ("rau", "au1", "审计员", "auditor")]:
            self.domain.register_actor(request_id=rid, actor_id="adm1", new_actor_id=aid,
                                       display_name=name, role=role, organization_id="o1")
        self.service.create_round(request_id="round", actor_id="rv1", round_id="rd1",
                                  title="首轮", years=["2026", "2027"])
        self.service.register_corridor(request_id="corridor", actor_id="op1", **CORRIDOR)

    def tearDown(self):
        self.database.close()

    def _project(self, request_id, project_id, **overrides):
        payload = {"request_id": request_id, "actor_id": "op1", "project_id": project_id,
                   "corridor_id": "c1", "name": project_id, "builder": "院",
                   "investments": {"2026": 50.0}, "benefit_year": "2026",
                   "bottleneck_ids": ["b1"], "footprint": ["seg"],
                   "service_areas": ["g-b"], "policy_goals": ["g1"], "priority": 1}
        payload.update(overrides)
        return self.service.register_project(**payload)

    def _freeze(self, rules=None, round_id="rd1", request_id="freeze"):
        return self.service.freeze_round(request_id=request_id, actor_id="rv1",
                                         round_id=round_id, rules=rules or RULES)

    def test_registration_requires_open_round(self):
        self._freeze()
        with self.assertRaises(ConflictError):
            self._project("late", "late")

    def test_auditor_cannot_register(self):
        with self.assertRaises(PermissionDenied):
            self.service.register_corridor(request_id="x", actor_id="au1", corridor_id="c9",
                                           name="x", direction="coastal")

    def test_project_must_reference_registered_bottleneck(self):
        with self.assertRaises(ValidationError):
            self._project("bad", "bad", bottleneck_ids=["nope"])

    def test_investment_year_must_fit_round_horizon(self):
        with self.assertRaises(ValidationError):
            self._project("bad", "bad", investments={"2030": 1.0}, benefit_year="2030")

    def test_operator_cannot_freeze(self):
        with self.assertRaises(PermissionDenied):
            self.service.freeze_round(request_id="x", actor_id="op1", round_id="rd1", rules=RULES)

    def test_freeze_is_idempotent_and_immutable(self):
        first = self._freeze()
        self.assertFalse(first["replayed"])
        replay = self._freeze()
        self.assertTrue(replay["replayed"])
        self.assertEqual(first["resource_id"], replay["resource_id"])
        with self.assertRaises(ConflictError):
            self.service.freeze_round(request_id="freeze-again", actor_id="rv1",
                                      round_id="rd1", rules=RULES)

    def test_version_recompute_matches_frozen_hashes(self):
        self._project("p1", "p1")
        self._freeze()
        recomputed = self.service.recompute_version("v001")
        self.assertTrue(recomputed["snapshot_hash_matches"])
        self.assertTrue(recomputed["rules_hash_matches"])

    def test_frozen_recommendation_flags_duplicate_application(self):
        self._project("p1", "p1")
        self._project("p2", "p2", service_areas=["g-r"])
        self._freeze()
        evaluation = self.service.recompute_version("v001")["evaluation"]
        self.assertEqual(1, len(evaluation["spatial_overlaps"]))
        self.assertEqual(1, len(evaluation["budget_competition"]))

    def test_split_merge_exit_preserve_lineage(self):
        self._project("big", "big")
        self.service.split_project(request_id="split", actor_id="op1", project_id="big", splits=[
            {"project_id": "big-a", "name": "a", "builder": "院", "investments": {"2026": 20.0},
             "benefit_year": "2026", "bottleneck_ids": ["b1"], "footprint": ["seg"]},
            {"project_id": "big-b", "name": "b", "builder": "院", "investments": {"2026": 30.0},
             "benefit_year": "2026", "footprint": ["seg"]}], note="分段")
        self.service.merge_projects(request_id="merge", actor_id="op1",
                                    project_ids=["big-a", "big-b"],
                                    merged={"project_id": "fused", "name": "合", "builder": "联营",
                                            "investments": {"2026": 40.0}, "benefit_year": "2026",
                                            "bottleneck_ids": ["b1"], "footprint": ["seg"]})
        self.service.exit_project(request_id="exit", actor_id="op1", project_id="fused",
                                  reason="暂缓")
        lineage = self.service.project_lineage("big")
        relations = [event["relation"] for event in lineage["lineage"]]
        self.assertEqual(["split", "split"], relations)
        statuses = {p["project_id"]: p["status"] for p in self.service.list_projects()}
        self.assertEqual("split", statuses["big"])
        self.assertEqual("merged", statuses["big-a"])
        self.assertEqual("exited", statuses["fused"])
        # 拆分后的父项目不能再修改
        with self.assertRaises(ConflictError):
            self.service.update_project(request_id="upd", actor_id="op1", project_id="big",
                                        changes={"name": "新"})

    def test_cross_year_round_records_impact_without_revising_approved(self):
        self._project("p1", "p1", investments={"2026": 50.0}, benefit_year="2026")
        self._freeze()
        self.service.create_plan(request_id="plan", actor_id="rv1", version_id="v001",
                                 origin="recommended", label="推荐")
        plan_id = self.service.list_plans("rd1")[0]["plan_id"]
        self.service.sign_plan(request_id="s1", actor_id="rv1", plan_id=plan_id, decision="approve")
        self.service.sign_plan(request_id="s2", actor_id="rv2", plan_id=plan_id, decision="approve")
        self.service.create_round(request_id="round2", actor_id="rv1", round_id="rd2",
                                  title="二轮", years=["2027", "2028"], parent_round_id="rd1")
        self.service.defer_project(request_id="defer", actor_id="op1", project_id="p1",
                                   new_benefit_year="2027", investments={"2027": 55.0},
                                   note="资金后移")
        self.service.freeze_round(request_id="freeze2", actor_id="rv1", round_id="rd2", rules={
            "weights": RULES["weights"],
            "budgets": {"2027": 100.0, "2028": 100.0},
            "approvers": ["rv1", "rv2"]})
        impact = self.service.get_version("v002")["impact"]
        self.assertEqual([{"project_id": "p1", "change": "changed"}],
                         impact["project_changes"])
        self.assertEqual(-50.0, impact["investment_delta"]["2026"])
        self.assertEqual(55.0, impact["investment_delta"]["2027"])
        self.assertIn("score", impact["metric_delta"])
        # 已批准的 v001 组合不被倒改
        v1 = self.service.get_version("v001")
        self.assertEqual("v001", v1["version_id"])
        self.assertEqual("rd1", self.service.list_rounds()[0]["round_id"])

    def test_competitive_approval_allows_only_one_effective_plan(self):
        self._project("p1", "p1", investments={"2026": 50.0}, benefit_year="2026")
        self._project("p2", "p2", investments={"2026": 60.0}, benefit_year="2026",
                      service_areas=["g-r"], priority=1)
        self._freeze()
        self.service.create_plan(request_id="plan-a", actor_id="rv1", version_id="v001",
                                 origin="recommended", label="推荐方案")
        plan_a = self.service.list_plans("rd1")[0]["plan_id"]
        self.service.create_plan(request_id="plan-b", actor_id="rv1", version_id="v001",
                                 origin="manual", label="全选方案",
                                 selected_project_ids=["p1", "p2"])
        plan_b = [p for p in self.service.list_plans("rd1")
                  if p["label"] == "全选方案"][0]["plan_id"]
        # 超预算方案即使会签也不能生效
        self.service.sign_plan(request_id="b1", actor_id="rv1", plan_id=plan_b,
                               decision="approve")
        with self.assertRaises(ConflictError):
            self.service.sign_plan(request_id="b2", actor_id="rv2", plan_id=plan_b,
                                   decision="approve")
        self.service.sign_plan(request_id="a1", actor_id="rv1", plan_id=plan_a,
                               decision="approve")
        result = self.service.sign_plan(request_id="a2", actor_id="rv2", plan_id=plan_a,
                                        decision="approve")
        self.assertTrue(result["became_effective"])
        statuses = {p["label"]: p["status"] for p in self.service.list_plans("rd1")}
        self.assertEqual("effective", statuses["推荐方案"])
        self.assertEqual("superseded", statuses["全选方案"])
        self.assertEqual(1, len([p for p in self.service.list_plans("rd1")
                                 if p["status"] == "effective"]))
        self.assertEqual(plan_a, self.service.list_rounds()[0]["effective_plan_id"])

    def test_plan_with_unscheduled_prerequisite_cannot_become_effective(self):
        self._project("p1", "p1", bottleneck_ids=[], investments={"2026": 1.0},
                      benefit_year="2026", prerequisite_project_ids=["p2"])
        self._project("p2", "p2", bottleneck_ids=["b1"], investments={"2026": 1.0},
                      benefit_year="2026", service_areas=[])
        self._freeze(rules={
            "weights": {"backbone": 1.0}, "budgets": {"2026": 100.0, "2027": 100.0},
            "approvers": ["rv1", "rv2"]})
        self.service.create_plan(request_id="plan", actor_id="rv1", version_id="v001",
                                 origin="manual", label="仅p1", selected_project_ids=["p1"])
        plan_id = self.service.list_plans("rd1")[0]["plan_id"]
        self.service.sign_plan(request_id="s1", actor_id="rv1", plan_id=plan_id,
                               decision="approve")
        with self.assertRaises(ConflictError):
            self.service.sign_plan(request_id="s2", actor_id="rv2", plan_id=plan_id,
                                   decision="approve")

    def test_non_approver_cannot_sign_and_signature_is_unique(self):
        self._project("p1", "p1")
        self._freeze()
        self.service.create_plan(request_id="plan", actor_id="rv1", version_id="v001",
                                 origin="recommended", label="推荐")
        plan_id = self.service.list_plans("rd1")[0]["plan_id"]
        with self.assertRaises(PermissionDenied):
            self.service.sign_plan(request_id="x", actor_id="op1", plan_id=plan_id,
                                   decision="approve")
        self.service.sign_plan(request_id="s1", actor_id="rv1", plan_id=plan_id,
                               decision="approve")
        with self.assertRaises(ConflictError):
            self.service.sign_plan(request_id="s1b", actor_id="rv1", plan_id=plan_id,
                                   decision="approve")

    def test_signatures_survive_restart_and_countersign_continues(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "planning.sqlite3")
            database = Database(path)
            clock = FixedClock(datetime(2026, 9, 29, tzinfo=timezone.utc))
            domain = DomainService(database, clock)
            service = PlanningService(database, clock)
            domain.register_organization(request_id="org", actor_id="bootstrap",
                                         organization_id="o1", name="专班")
            domain.register_actor(request_id="adm", actor_id="bootstrap", new_actor_id="adm1",
                                  display_name="管", role="admin", organization_id="o1")
            domain.register_actor(request_id="ra", actor_id="adm1", new_actor_id="rv1",
                                  display_name="甲", role="reviewer", organization_id="o1")
            domain.register_actor(request_id="rb", actor_id="adm1", new_actor_id="rv2",
                                  display_name="乙", role="reviewer", organization_id="o1")
            domain.register_actor(request_id="ro", actor_id="adm1", new_actor_id="op1",
                                  display_name="员", role="operator", organization_id="o1")
            service.create_round(request_id="rd", actor_id="rv1", round_id="rd1",
                                 title="首", years=["2026", "2027"])
            service.register_corridor(request_id="co", actor_id="op1", **CORRIDOR)
            service.register_project(request_id="p1", actor_id="op1", project_id="p1",
                                     corridor_id="c1", name="p1", builder="院",
                                     investments={"2026": 50.0}, benefit_year="2026",
                                     bottleneck_ids=["b1"], service_areas=["g-b"])
            service.freeze_round(request_id="fz", actor_id="rv1", round_id="rd1", rules=RULES)
            service.create_plan(request_id="pl", actor_id="rv1", version_id="v001",
                                origin="recommended", label="推荐")
            plan_id = service.list_plans("rd1")[0]["plan_id"]
            service.sign_plan(request_id="s1", actor_id="rv1", plan_id=plan_id,
                              decision="approve")
            database.close()

            restarted_db = Database(path)
            restarted = PlanningService(restarted_db, clock)
            view = restarted.get_plan(plan_id)
            self.assertEqual(["rv1"], view["approvals"])
            self.assertEqual(["rv2"], view["pending_approvers"])
            result = restarted.sign_plan(request_id="s2", actor_id="rv2", plan_id=plan_id,
                                         decision="approve")
            self.assertTrue(result["became_effective"])
            self.assertEqual(plan_id, restarted.list_rounds()[0]["effective_plan_id"])
            restarted_db.close()


if __name__ == "__main__":
    unittest.main()
