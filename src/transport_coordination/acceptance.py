"""运行基础服务与通道组合决策的离线端到端验收。"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .api import Services
from .clock import FixedClock
from .storage import Database


def _bootstrap(services: Services) -> None:
    domain = services.domain
    domain.register_organization(request_id="req-org", actor_id="bootstrap",
                                 organization_id="org-001", name="国家综合交通网规划专班")
    domain.register_actor(request_id="req-admin", actor_id="bootstrap", new_actor_id="admin-001",
                          display_name="系统管理员", role="admin", organization_id="org-001")
    domain.register_actor(request_id="req-reviewer-1", actor_id="admin-001", new_actor_id="rev-001",
                          display_name="沿海评审人", role="reviewer", organization_id="org-001")
    domain.register_actor(request_id="req-reviewer-2", actor_id="admin-001", new_actor_id="rev-002",
                          display_name="沿边评审人", role="reviewer", organization_id="org-001")
    domain.register_actor(request_id="req-operator", actor_id="admin-001", new_actor_id="operator-001",
                          display_name="规划负责人", role="operator", organization_id="org-001")
    domain.register_site(request_id="req-site", actor_id="operator-001", site_id="site-001",
                         organization_id="org-001", name="一号交通节点", timezone_name="Asia/Shanghai")
    first = domain.record_domain_data(request_id="req-data", actor_id="operator-001", site_id="site-001",
                                      category="operator_profile", external_key="record-001",
                                      data={"name": "基础资料", "enabled": True})
    replay = domain.record_domain_data(request_id="req-data", actor_id="operator-001", site_id="site-001",
                                       category="operator_profile", external_key="record-001",
                                       data={"name": "基础资料", "enabled": True})
    return first, replay


CORRIDORS = [
    {
        "corridor_id": "cor-coast", "name": "沿海主通道", "direction": "coastal",
        "nodes": ["C0", "C1", "C2"],
        "edges": [{"edge_id": "ce1", "from": "C0", "to": "C1"},
                  {"edge_id": "ce2", "from": "C1", "to": "C2"}],
        "bottlenecks": [{"bottleneck_id": "cb1", "edge_id": "ce1", "capacity_gap": 40},
                        {"bottleneck_id": "cb2", "edge_id": "ce2", "capacity_gap": 25}],
        "service_gaps": [{"area_id": "ca1", "kind": "rural", "population": 80000}],
        "backbone_pairs": [{"from": "C0", "to": "C2"}],
    },
    {
        "corridor_id": "cor-border", "name": "沿边主通道", "direction": "border",
        "nodes": ["B0", "B1"],
        "edges": [{"edge_id": "be1", "from": "B0", "to": "B1"}],
        "bottlenecks": [{"bottleneck_id": "bb1", "edge_id": "be1", "capacity_gap": 30}],
        "service_gaps": [{"area_id": "ba1", "kind": "border", "population": 12000}],
        "backbone_pairs": [{"from": "B0", "to": "B1"}],
    },
    {
        "corridor_id": "cor-river", "name": "沿江主通道", "direction": "riverine",
        "nodes": ["R0", "R1"],
        "edges": [{"edge_id": "re1", "from": "R0", "to": "R1"}],
        "bottlenecks": [{"bottleneck_id": "rb1", "edge_id": "re1", "capacity_gap": 20}],
        "service_gaps": [],
        "backbone_pairs": [{"from": "R0", "to": "R1"}],
    },
    {
        "corridor_id": "cor-xz", "name": "出疆入藏主通道", "direction": "xinjiang_tibet",
        "nodes": ["X0", "X1"],
        "edges": [{"edge_id": "xe1", "from": "X0", "to": "X1"}],
        "bottlenecks": [{"bottleneck_id": "xb1", "edge_id": "xe1", "capacity_gap": 50}],
        "service_gaps": [{"area_id": "xa1", "kind": "border", "population": 5000}],
        "backbone_pairs": [{"from": "X0", "to": "X1"}],
    },
]

RULES = {
    "weights": {"backbone": 3.0, "connectivity": 1.0, "border_coverage": 2.0,
                "rural_coverage": 2.0, "bottleneck_relief": 2.0, "policy_alignment": 1.0},
    "budgets": {"2026": 120.0, "2027": 160.0},
    "approvers": ["rev-001", "rev-002"],
    "required_approvals": 2,
}


def _project(services: Services, request_id: str, project_id: str, corridor_id: str,
             name: str, builder: str, *, investments, bottleneck_ids, year,
             service_areas=None, footprint=None, policy_goals=None, priority=1,
             prerequisite_project_ids=None):
    return services.planning.register_project(
        request_id=request_id, actor_id="operator-001", project_id=project_id,
        corridor_id=corridor_id, name=name, builder=builder, investments=investments,
        footprint=footprint or ["seg"], bottleneck_ids=bottleneck_ids,
        prerequisite_project_ids=prerequisite_project_ids or [],
        service_areas=service_areas or [], policy_goals=policy_goals or ["backbone-2035"],
        benefit_year=year, priority=priority)


def run() -> dict[str, object]:
    """执行完整登记、冻结、评价、竞争会签与重启续签链并返回结果。"""

    with tempfile.TemporaryDirectory() as directory:
        db_path = Path(directory) / "acceptance.sqlite3"
        clock = FixedClock(datetime(2026, 9, 29, 8, 0, tzinfo=timezone.utc))
        database = Database(db_path)
        services = Services(database, clock)
        first, replay = _bootstrap(services)
        planning = services.planning
        planning.register_policy_goal(request_id="goal-1", actor_id="operator-001",
                                      goal_code="backbone-2035",
                                      name="国家综合立体交通网主骨架贯通")
        planning.create_round(request_id="round-1", actor_id="rev-001", round_id="round-2026",
                              title="十五五通道第一轮", years=["2026", "2027"])
        for index, corridor in enumerate(CORRIDORS):
            planning.register_corridor(request_id=f"corridor-{index}",
                                       actor_id="operator-001", **corridor)

        # 同一断点 cb1 被两家建设单位重复申报；出疆入藏项目与前置工程形成依赖。
        _project(services, "p-coast-a", "prj-coast-a", "cor-coast", "沿海扩能A段", "铁建一院",
                 investments={"2026": 60.0}, bottleneck_ids=["cb1"], year="2026",
                 service_areas=["ca1"], priority=8)
        _project(services, "p-coast-b", "prj-coast-b", "cor-coast", "沿海扩能重复申报", "港航二院",
                 investments={"2026": 70.0}, bottleneck_ids=["cb1"], year="2026",
                 service_areas=["ca1"], priority=2)
        _project(services, "p-border", "prj-border", "cor-border", "沿边口岸扩能", "路桥集团",
                 investments={"2026": 45.0}, bottleneck_ids=["bb1"], year="2026",
                 service_areas=["ba1"], priority=6)
        _project(services, "p-river", "prj-river", "cor-river", "沿江航道整治", "航道局",
                 investments={"2027": 30.0}, bottleneck_ids=["rb1"], year="2027", priority=4)
        _project(services, "p-xz-pre", "prj-xz-pre", "cor-xz", "出疆入藏先导工程", "藏建公司",
                 investments={"2026": 40.0}, bottleneck_ids=[], year="2026", priority=7)
        _project(services, "p-xz-main", "prj-xz-main", "cor-xz", "出疆入藏主体工程", "疆建公司",
                 investments={"2027": 90.0}, bottleneck_ids=["xb1"], year="2027",
                 service_areas=["xa1"], prerequisite_project_ids=["prj-xz-pre"], priority=9)

        frozen = planning.freeze_round(request_id="freeze-1", actor_id="rev-001",
                                       round_id="round-2026", rules=RULES)
        version_id = frozen["resource_id"]
        frozen_version_hash = planning.get_version(version_id)["version_hash"]
        recomputed = planning.recompute_version(version_id)
        evaluation = recomputed["evaluation"]
        recommended = planning.create_plan(request_id="plan-rec", actor_id="rev-001",
                                           version_id=version_id, origin="recommended",
                                           label="系统推荐时序")
        recommended_id = recommended["resource_id"]
        # 竞争方案：把重复申报也纳入，突破年度预算，应被生效校验拦截。
        planning.create_plan(request_id="plan-all", actor_id="rev-001", version_id=version_id,
                             origin="manual", label="全部上马方案",
                             selected_project_ids=["prj-coast-a", "prj-coast-b", "prj-border",
                                                   "prj-river", "prj-xz-pre", "prj-xz-main"])
        plans = {p["label"]: p["plan_id"] for p in planning.list_plans("round-2026")}
        all_plan_id = plans["全部上马方案"]
        planning.sign_plan(request_id="sign-all-1", actor_id="rev-001", plan_id=all_plan_id,
                           decision="approve", comment="先提交竞争方案")
        try:
            planning.sign_plan(request_id="sign-all-2", actor_id="rev-002", plan_id=all_plan_id,
                               decision="approve")
            blocked_over_budget = False
        except Exception:
            blocked_over_budget = True
        planning.sign_plan(request_id="sign-rec-1", actor_id="rev-001", plan_id=recommended_id,
                           decision="approve", comment="同意推荐时序")
        database.close()

        # 服务重启：会签状态持久化，第二位评审人继续完成会签。
        restarted_db = Database(db_path)
        restarted = Services(restarted_db, clock)
        plan_view = restarted.planning.get_plan(recommended_id)
        sign_result = restarted.planning.sign_plan(request_id="sign-rec-2", actor_id="rev-002",
                                                   plan_id=recommended_id, decision="approve",
                                                   comment="复算指标后同意")
        rounds = restarted.planning.list_rounds()
        round_2026_plans = restarted.planning.list_plans("round-2026")
        effective_plan_id = rounds[0]["effective_plan_id"]
        only_one_effective = len([p for p in round_2026_plans
                                  if p["status"] == "effective"]) == 1

        # 跨年度调整：第二轮把沿江项目延期到 2028 年之外不可行（超出年度范围），
        # 改为在轮次年度内重新排布资金，并核对对已批准组合的影响。
        restarted.planning.create_round(request_id="round-2", actor_id="rev-001",
                                        round_id="round-2027", title="十五五通道第二轮",
                                        years=["2027", "2028"], parent_round_id="round-2026")
        restarted.planning.defer_project(request_id="defer-river", actor_id="operator-001",
                                         project_id="prj-river", new_benefit_year="2028",
                                         investments={"2028": 32.0}, note="年度资金后移")
        restarted.planning.freeze_round(request_id="freeze-2", actor_id="rev-001",
                                        round_id="round-2027",
                                        rules={**RULES,
                                               "budgets": {"2027": 160.0, "2028": 160.0}})
        impact = restarted.planning.get_version("v002")["impact"]
        approved_version_hash = restarted.planning.get_version("v001")["version_hash"]
        audit_valid, audit_events = restarted.domain.verify_audit()
        restarted_db.close()

        scheduled = [d["project_id"] for d in evaluation["decisions"] if d["action"] == "schedule"]
        result = {
            "status": "ok",
            "records": 1,
            "audit_events": audit_events,
            "audit_valid": audit_valid,
            "first_replayed": first.replayed,
            "second_replayed": replay.replayed,
            "frozen_version": version_id,
            "snapshot_hash_matches": recomputed["snapshot_hash_matches"],
            "rules_hash_matches": recomputed["rules_hash_matches"],
            "spatial_overlaps": len(evaluation["spatial_overlaps"]),
            "budget_competition_years": [item["year"] for item in evaluation["budget_competition"]],
            "recommended_scheduled": scheduled,
            "recommended_score": evaluation["metrics"]["score"],
            "recommended_within_budget": evaluation["metrics"]["within_budget"],
            "blocked_over_budget_plan": blocked_over_budget,
            "effective_plan_id": effective_plan_id,
            "only_one_effective": only_one_effective,
            "countersign_completed_after_restart": sign_result["became_effective"],
            "pending_after_restart": plan_view["pending_approvers"],
            "cross_year_investment_delta": impact["investment_delta"],
            "approved_version_unchanged": approved_version_hash == frozen_version_hash,
        }
        return result


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    ok = (result["status"] == "ok" and result["audit_valid"]
          and result["snapshot_hash_matches"] and result["rules_hash_matches"]
          and result["blocked_over_budget_plan"] and result["only_one_effective"]
          and result["countersign_completed_after_restart"]
          and result["approved_version_unchanged"])
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
