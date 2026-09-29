"""评价引擎的纯函数测试。"""

import unittest

from transport_coordination.portfolio import (
    UnionFind,
    compute_metrics,
    detect_budget_competition,
    detect_dependency_conflicts,
    detect_spatial_overlap,
    evaluate_snapshot,
    normalize_corridor,
    normalize_project,
    normalize_rules,
    recommend_schedule,
)


RULES = normalize_rules({
    "weights": {"backbone": 3.0, "connectivity": 1.0, "border_coverage": 2.0,
                "rural_coverage": 2.0, "bottleneck_relief": 2.0, "policy_alignment": 1.0},
    "budgets": {"2026": 70.0, "2027": 100.0},
    "approvers": ["r1", "r2"],
})

CORRIDOR_RAW = {
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


def project(project_id, **overrides):
    base = {"project_id": project_id, "corridor_id": "c1", "name": project_id,
            "builder": "院", "investments": {"2026": 50.0}, "benefit_year": "2026",
            "bottleneck_ids": ["b1"], "footprint": ["seg1"],
            "service_areas": ["g-b"], "policy_goals": ["g1"], "priority": 1}
    base.update(overrides)
    return normalize_project(base)


class EngineTest(unittest.TestCase):
    def setUp(self):
        self.corridor = normalize_corridor(CORRIDOR_RAW)

    def test_union_find_transitivity(self):
        uf = UnionFind(["a", "b", "c"])
        uf.union("a", "b")
        uf.union("b", "c")
        self.assertTrue(uf.connected("a", "c"))
        self.assertFalse(uf.connected("a", "x") if "x" in uf.parent else False)

    def test_spatial_overlap_detects_duplicate_bottleneck(self):
        overlaps = detect_spatial_overlap([project("p1"), project("p2")])
        self.assertEqual(1, len(overlaps))
        self.assertEqual(["p1", "p2"], overlaps[0]["project_ids"])
        self.assertEqual(["b1"], overlaps[0]["shared_bottleneck_ids"])

    def test_different_corridors_do_not_overlap(self):
        other = project("p2", corridor_id="c2")
        self.assertEqual([], detect_spatial_overlap([project("p1"), other]))

    def test_dependency_cycle_is_found(self):
        conflicts = detect_dependency_conflicts([
            project("p1", prerequisite_project_ids=["p2"]),
            project("p2", prerequisite_project_ids=["p1"]),
        ])
        self.assertEqual(1, len(conflicts["cycles"]))

    def test_missing_prerequisite_is_reported(self):
        conflicts = detect_dependency_conflicts([project("p1", prerequisite_project_ids=["ghost"])])
        self.assertEqual("ghost", conflicts["missing"][0]["missing_prerequisite_id"])

    def test_budget_competition_flags_over_cap_year(self):
        competition = detect_budget_competition(
            [project("p1", investments={"2026": 50.0}),
             project("p2", investments={"2026": 60.0})], RULES)
        self.assertEqual("2026", competition[0]["year"])
        self.assertEqual(110.0, competition[0]["demand"])

    def test_metrics_connect_backbone_when_bottleneck_relieved(self):
        corridor = normalize_corridor({
            "corridor_id": "c1", "name": "沿海通道", "direction": "coastal",
            "nodes": ["A", "B"],
            "edges": [{"edge_id": "e1", "from": "A", "to": "B"}],
            "bottlenecks": [{"bottleneck_id": "b1", "edge_id": "e1", "capacity_gap": 30}],
            "service_gaps": [{"area_id": "g-b", "kind": "border"}],
            "backbone_pairs": [{"from": "A", "to": "B"}],
        })
        before = compute_metrics([corridor], [project("p1")], [], RULES)
        after = compute_metrics([corridor], [project("p1")], ["p1"], RULES)
        self.assertEqual(0, before["backbone_connected_pairs"])
        self.assertEqual(1, after["backbone_connected_pairs"])
        self.assertEqual(1.0, after["border_coverage"])

    def test_budget_breach_recorded(self):
        metrics = compute_metrics(
            [self.corridor],
            [project("p1", investments={"2026": 90.0})], ["p1"], RULES)
        self.assertFalse(metrics["within_budget"])
        self.assertEqual(20.0, metrics["budget_breaches"][0]["over"])

    def test_recommend_respects_budget_and_explains_deferral(self):
        projects = [project("p1", investments={"2026": 50.0}, priority=5),
                    project("p2", investments={"2026": 60.0}, priority=1)]
        result = recommend_schedule(
            [self.corridor], projects, RULES,
            {"dependency": detect_dependency_conflicts(projects)})
        scheduled = [d for d in result["decisions"] if d["action"] == "schedule"]
        deferred = [d for d in result["decisions"] if d["action"] == "defer"]
        self.assertEqual(["p1"], [d["project_id"] for d in scheduled])
        self.assertEqual("budget_exhausted", deferred[0]["reason"])
        self.assertTrue(result["metrics"]["within_budget"])

    def test_prerequisite_orders_schedule(self):
        projects = [project("p1", investments={"2027": 10.0}, benefit_year="2027",
                            prerequisite_project_ids=["p2"], bottleneck_ids=[]),
                    project("p2", investments={"2026": 10.0}, benefit_year="2026",
                            bottleneck_ids=["b1"])]
        result = recommend_schedule(
            [self.corridor], projects, RULES,
            {"dependency": detect_dependency_conflicts(projects)})
        order = [d["project_id"] for d in result["decisions"] if d["action"] == "schedule"]
        self.assertLess(order.index("p2"), order.index("p1"))

    def test_evaluate_snapshot_is_deterministic(self):
        snapshot = {"corridors": [CORRIDOR_RAW],
                    "projects": [{**project("p1")}],
                    "policy_goals": [{"goal_code": "g1", "name": "出疆"}]}
        first = evaluate_snapshot(snapshot, RULES)
        second = evaluate_snapshot(snapshot, RULES)
        self.assertEqual(first["metrics"], second["metrics"])
        self.assertEqual(first["rules_hash"], second["rules_hash"])


if __name__ == "__main__":
    unittest.main()
