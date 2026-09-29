import unittest

from transport_coordination.evaluation import bundle_hash, evaluate, normalize_rules


def _rules():
    return normalize_rules({
        "budgets": {"2026": 100, "2027": 100},
        "horizon": [2026, 2027],
        "hubs": ["h1"],
    })


def _bundle():
    edges = [
        {"edge_id": "e1", "corridor_id": "c1", "from_node": "h1", "to_node": "n2",
         "exists": True, "current_capacity": 5, "required_capacity": 10},
        {"edge_id": "e2", "corridor_id": "c1", "from_node": "n2", "to_node": "b1",
         "exists": False, "current_capacity": 0, "required_capacity": 10},
        {"edge_id": "e3", "corridor_id": "c2", "from_node": "n2", "to_node": "r1",
         "exists": False, "current_capacity": 0, "required_capacity": 10},
    ]
    projects = {
        "p1": {"project_id": "p1", "version": 1, "status": "registered", "name": "扩能",
               "corridor_id": "c1", "building_unit": "u1", "edge_ids": ["e1"], "gap_ids": [],
               "prerequisite_project_ids": [], "yearly_investment": {"2026": 40},
               "policy_target_ids": [], "total_investment": 40},
        "p2": {"project_id": "p2", "version": 1, "status": "registered", "name": "贯通",
               "corridor_id": "c1", "building_unit": "u1", "edge_ids": ["e2"],
               "gap_ids": ["g2"], "prerequisite_project_ids": ["p1"],
               "yearly_investment": {"2026": 30}, "policy_target_ids": [],
               "total_investment": 30},
        "pdup": {"project_id": "pdup", "version": 1, "status": "registered", "name": "重复",
                 "corridor_id": "c1", "building_unit": "u2", "edge_ids": ["e2"],
                 "gap_ids": ["g2"], "prerequisite_project_ids": [],
                 "yearly_investment": {"2026": 30}, "policy_target_ids": [],
                 "total_investment": 30},
        "p3": {"project_id": "p3", "version": 1, "status": "registered", "name": "通乡",
               "corridor_id": "c2", "building_unit": "u3", "edge_ids": ["e3"],
               "gap_ids": ["g3"], "prerequisite_project_ids": [],
               "yearly_investment": {"2026": 60}, "policy_target_ids": [],
               "total_investment": 60},
    }
    return {
        "nodes": [{"node_id": n, "name": n, "kind": "x"} for n in ("h1", "n2", "b1", "r1")],
        "edges": edges,
        "gaps": [
            {"gap_id": "g2", "corridor_id": "c1", "edge_id": "e2",
             "current_capacity": 0, "required_capacity": 10},
            {"gap_id": "g3", "corridor_id": "c2", "edge_id": "e3",
             "current_capacity": 0, "required_capacity": 10},
        ],
        "areas": [
            {"area_id": "a-border", "name": "边境", "kind": "border", "node_ids": ["b1"]},
            {"area_id": "a-rural", "name": "乡村", "kind": "rural", "node_ids": ["r1"]},
        ],
        "projects": projects,
        "rules": _rules(),
        "target": None,
    }


class EvaluationTest(unittest.TestCase):
    def test_deterministic_schedule_and_hash(self):
        bundle = _bundle()
        items = [{"project_id": pid, "version": 1} for pid in bundle["projects"]]
        first = evaluate(bundle, items)
        second = evaluate(bundle, items)
        self.assertEqual(first["metrics_hash"], second["metrics_hash"])
        self.assertEqual(
            [(e["project_id"], e["year"]) for e in first["schedule"]],
            [(e["project_id"], e["year"]) for e in second["schedule"]],
        )

    def test_detects_spatial_overlap_and_duplicate_gap(self):
        bundle = _bundle()
        items = [{"project_id": pid, "version": 1} for pid in ("p1", "p2", "pdup")]
        result = evaluate(bundle, items)
        codes = {finding["code"] for finding in result["findings"]}
        self.assertIn("spatial_overlap", codes)
        self.assertIn("duplicate_gap", codes)

    def test_dependency_conflict_when_prerequisite_excluded(self):
        bundle = _bundle()
        items = [{"project_id": "p2", "version": 1},
                 {"project_id": "p1", "version": 1, "action": "exit"}]
        result = evaluate(bundle, items)
        self.assertTrue(any(f["code"] == "dependency_conflict" for f in result["findings"]))
        self.assertIn({"project_id": "p2", "reason": "prerequisite_exited"}, result["excluded"])

    def test_budget_squeeze_defers_or_excludes(self):
        bundle = _bundle()
        # 单年投资抬高到 200，超过任何一年 100 的预算上限。
        bundle["projects"]["p3"]["yearly_investment"] = {"2026": 200}
        result = evaluate(bundle, [{"project_id": "p3", "version": 1}])
        self.assertFalse(result["feasible"])
        self.assertTrue(any(f["code"] == "budget_squeeze" for f in result["findings"]))
        self.assertIn({"project_id": "p3", "reason": "no_budget_window"}, result["excluded"])

    def test_coverage_and_gap_metrics(self):
        bundle = _bundle()
        items = [{"project_id": pid, "version": 1} for pid in ("p1", "p2", "pdup", "p3")]
        result = evaluate(bundle, items)
        coverage = result["metrics"]["coverage"]
        self.assertEqual(1, coverage["border_covered"])
        self.assertEqual(1, coverage["rural_covered"])
        self.assertEqual(2, result["metrics"]["gaps_closed"])
        scheduled = {entry["project_id"]: entry for entry in result["schedule"]}
        border_entries = [pid for pid, entry in scheduled.items()
                          if entry["reasons"]["newly_covered_border"] == ["a-border"]]
        self.assertEqual(1, len(border_entries))
        self.assertIn(border_entries[0], ("p2", "pdup"))
        gap_entries = [pid for pid, entry in scheduled.items()
                       if entry["reasons"]["gaps_addressed"] == ["g2"]]
        self.assertEqual(1, len(gap_entries))

    def test_prerequisite_orders_after_completion(self):
        bundle = _bundle()
        result = evaluate(bundle, [{"project_id": "p1", "version": 1},
                                   {"project_id": "p2", "version": 1}])
        years = {entry["project_id"]: entry["year"] for entry in result["schedule"]}
        self.assertLessEqual(years["p1"], years["p2"])

    def test_bundle_hash_stable(self):
        bundle = _bundle()
        self.assertEqual(bundle_hash(bundle), bundle_hash(_bundle()))

    def test_version_mismatch_rejected(self):
        bundle = _bundle()
        with self.assertRaises(ValueError):
            evaluate(bundle, [{"project_id": "p1", "version": 99}])


if __name__ == "__main__":
    unittest.main()
