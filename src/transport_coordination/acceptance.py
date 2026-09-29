"""运行通道项目组合决策服务的离线端到端验收。

覆盖：基础登记、走廊拓扑/断点/缺口/项目登记、评审轮冻结、竞争提案与
可解释时序、多角色会签、服务重启后续签生效、历史版本复算、跨年度调整
沿版本保留影响且不倒改已批准组合。
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .clock import FixedClock
from .portfolio import PortfolioService
from .storage import Database


def _seed(service: PortfolioService) -> None:
    service.register_organization(request_id="req-org", actor_id="bootstrap",
                                  organization_id="org-001", name="国家综合交通网规划专班")
    service.register_actor(request_id="req-admin", actor_id="bootstrap", new_actor_id="admin-001",
                           display_name="系统管理员", role="admin", organization_id="org-001")
    service.register_actor(request_id="req-reviewer", actor_id="admin-001", new_actor_id="reviewer-001",
                           display_name="评审负责人", role="reviewer", organization_id="org-001")
    service.register_actor(request_id="req-operator", actor_id="admin-001", new_actor_id="operator-001",
                           display_name="申报负责人", role="operator", organization_id="org-001")
    nodes = [
        ("hub-east", "沿海主枢纽", "hub"),
        ("city-a", "沿海中心城市", "city"),
        ("port-border", "边境口岸", "border"),
        ("town-rural", "沿江乡镇", "rural"),
        ("gate-west", "出疆入藏门户", "city"),
    ]
    for index, (node_id, name, kind) in enumerate(nodes):
        service.register_node(request_id=f"req-node-{index}", actor_id="operator-001",
                              node_id=node_id, name=name, kind=kind)
    edges = [
        ("edge-1", "corridor-coast", "hub-east", "city-a", True, 20, 50),
        ("edge-2", "corridor-coast", "city-a", "port-border", False, 0, 30),
        ("edge-3", "corridor-west", "hub-east", "gate-west", False, 0, 40),
        ("edge-4", "corridor-river", "city-a", "town-rural", False, 0, 20),
    ]
    for index, edge in enumerate(edges):
        service.register_edge(request_id=f"req-edge-{index}", actor_id="operator-001",
                              edge_id=edge[0], corridor_id=edge[1], from_node=edge[2],
                              to_node=edge[3], exists=edge[4], current_capacity=edge[5],
                              required_capacity=edge[6])
    gaps = [
        ("gap-2", "corridor-coast", "edge-2", 0, 30),
        ("gap-3", "corridor-west", "edge-3", 0, 40),
        ("gap-4", "corridor-river", "edge-4", 0, 20),
    ]
    for index, gap in enumerate(gaps):
        service.register_gap(request_id=f"req-gap-{index}", actor_id="operator-001",
                             gap_id=gap[0], corridor_id=gap[1], edge_id=gap[2],
                             current_capacity=gap[3], required_capacity=gap[4])
    service.register_service_area(request_id="req-area-border", actor_id="operator-001",
                                  area_id="area-border", name="边境口岸覆盖片区",
                                  kind="border", node_ids=["port-border"])
    service.register_service_area(request_id="req-area-rural", actor_id="operator-001",
                                  area_id="area-rural", name="沿江乡村覆盖片区",
                                  kind="rural", node_ids=["town-rural"])
    service.register_policy_target(request_id="req-target", actor_id="operator-001",
                                   target_id="target-155", title="十五五主骨架贯通目标",
                                   goals={"skeleton_ratio": 1.0, "border_ratio": 1.0})
    projects = [
        ("proj-coast-upgrade", "沿海既有通道扩能", "corridor-coast", "沿海建设集团",
         ["edge-1"], [], [], {"2026": 100}),
        ("proj-coast-link", "沿海支线段贯通", "corridor-coast", "沿海建设集团",
         ["edge-2"], ["gap-2"], ["proj-coast-upgrade"], {"2026": 80}),
        ("proj-dup-link", "同瓶颈重复申报段", "corridor-coast", "口岸开发公司",
         ["edge-2"], ["gap-2"], [], {"2026": 90}),
        ("proj-west", "出疆入藏通道", "corridor-west", "西部建投",
         ["edge-3"], ["gap-3"], [], {"2026": 200}),
        ("proj-rural", "沿江通乡路", "corridor-river", "地方公路局",
         ["edge-4"], ["gap-4"], [], {"2026": 30}),
    ]
    for index, project in enumerate(projects):
        service.register_project(
            request_id=f"req-project-{index}", actor_id="operator-001", project_id=project[0],
            name=project[1], corridor_id=project[2], building_unit=project[3], edge_ids=project[4],
            gap_ids=project[5], prerequisite_project_ids=project[6],
            yearly_investment=project[7], policy_target_ids=["target-155"])


def run() -> dict[str, object]:
    """执行完整组合决策链并返回结果。"""

    with tempfile.TemporaryDirectory() as directory:
        db_path = Path(directory) / "acceptance.sqlite3"
        clock = FixedClock(datetime(2026, 9, 29, 8, 0, tzinfo=timezone.utc))
        database = Database(db_path)
        service = PortfolioService(database, clock)
        _seed(service)

        # 基础登记幂等性回归。
        first = service.record_domain_data(
            request_id="req-data", actor_id="operator-001", site_id=_site(service),
            category="network_registry", external_key="record-001", data={"frozen": True})
        replay = service.record_domain_data(
            request_id="req-data", actor_id="operator-001", site_id=_site(service),
            category="network_registry", external_key="record-001", data={"frozen": True})

        # 冻结首轮数据与评价规则。
        service.create_round(
            request_id="req-round", actor_id="reviewer-001", round_id="round-2026",
            title="十五五通道首评", plan_year=2026,
            rules={"budgets": {"2026": 320, "2027": 320}, "horizon": [2026, 2027],
                   "hubs": ["hub-east"]},
            target_id="target-155")
        frozen = service.freeze_round(actor_id="reviewer-001", round_id="round-2026")

        # 竞争方案 A：含重复申报项目，评价必须识别空间重叠。
        proposal_a = service.create_proposal(
            request_id="req-proposal-a", actor_id="reviewer-001", round_id="round-2026",
            proposal_id="proposal-a",
            items=[{"project_id": "proj-coast-upgrade"}, {"project_id": "proj-coast-link"},
                   {"project_id": "proj-dup-link"}, {"project_id": "proj-west"},
                   {"project_id": "proj-rural"}])
        eval_a = proposal_a["response"]["evaluation"]
        overlap_codes = {finding["code"] for finding in eval_a["findings"]}

        # 竞争方案 B：剔除重复申报，形成可执行时序。
        proposal_b = service.create_proposal(
            request_id="req-proposal-b", actor_id="reviewer-001", round_id="round-2026",
            proposal_id="proposal-b",
            items=[{"project_id": "proj-coast-upgrade"}, {"project_id": "proj-coast-link"},
                   {"project_id": "proj-west"}, {"project_id": "proj-rural"}])
        eval_b = proposal_b["response"]["evaluation"]
        schedule = {entry["project_id"]: entry["year"] for entry in eval_b["schedule"]}
        rural_reason = next(entry for entry in eval_b["schedule"]
                            if entry["project_id"] == "proj-rural")["reasons"]

        # 会签：admin 先签，随后“服务重启”，reviewer 重启后继续签署并生效。
        sign_admin = service.sign_proposal(
            request_id="req-sign-admin", actor_id="admin-001", proposal_id="proposal-b",
            decision="approve", comment="同意组合")
        database.close()

        restarted = Database(db_path)
        service = PortfolioService(restarted, clock)
        pending = service.get_proposal("proposal-b")
        sign_reviewer = service.sign_proposal(
            request_id="req-sign-reviewer", actor_id="reviewer-001", proposal_id="proposal-b",
            decision="approve", comment="复算通过")

        # 竞争审批只允许一个生效方案。
        competitor_blocked = False
        try:
            service.sign_proposal(request_id="req-sign-late", actor_id="admin-001",
                                  proposal_id="proposal-a", decision="approve")
        except Exception:
            competitor_blocked = True

        effective = service.get_proposal("proposal-b")
        competitor_status = service.get_proposal("proposal-a")["status"]
        recalculated = service.recalculate_proposal("proposal-b")

        # 跨年度调整：出疆通道延期，必须沿版本记录对已批准组合的影响。
        adjustment = service.adjust_project(
            request_id="req-adjust-west", actor_id="operator-001", project_id="proj-west",
            action="delay", target_year=2027, reason="用地预审跨年度顺延")
        impacts = adjustment["response"]["impacts"]
        versions = service.list_project_versions("proj-west")
        lineage = service.list_lineage("proj-west")

        # 调整后已批准组合仍按冻结版本复算，结果不得倒改。
        recalculated_after = service.recalculate_proposal("proposal-b")

        # 拆分沿版本谱系留痕。
        split = service.adjust_project(
            request_id="req-split-rural", actor_id="operator-001", project_id="proj-rural",
            action="split", reason="按两段分别实施", children=[
                {"project_id": "proj-rural-north", "name": "通乡路北段", "corridor_id": "corridor-river",
                 "building_unit": "地方公路局", "edge_ids": ["edge-4"], "gap_ids": ["gap-4"],
                 "prerequisite_project_ids": [], "yearly_investment": {"2026": 15}},
                {"project_id": "proj-rural-south", "name": "通乡路南段", "corridor_id": "corridor-river",
                 "building_unit": "地方公路局", "edge_ids": ["edge-4"], "gap_ids": ["gap-4"],
                 "prerequisite_project_ids": [], "yearly_investment": {"2026": 15}},
            ])

        valid, event_count = service.verify_audit()
        restarted.close()

    return {
        "status": "ok",
        "audit_valid": valid,
        "audit_events": event_count,
        "first_replayed": first.replayed,
        "second_replayed": replay.replayed,
        "records": 1,
        "frozen_projects": frozen["project_count"],
        "snapshot_hash": frozen["snapshot_hash"],
        "proposal_a_overlap": "spatial_overlap" in overlap_codes
        and "duplicate_gap" in overlap_codes,
        "proposal_b_within_budget": eval_b["within_budget"],
        "proposal_b_border_covered": eval_b["metrics"]["coverage"]["border_covered"],
        "proposal_b_rural_covered": eval_b["metrics"]["coverage"]["rural_covered"],
        "proposal_b_gaps_closed": eval_b["metrics"]["gaps_closed"],
        "schedule": schedule,
        "rural_covers_border_via_reason":
            rural_reason["newly_covered_rural"] == ["area-rural"],
        "sign_admin_pending": sign_admin["response"]["status"] == "submitted"
        and sign_admin["response"]["signoff"]["missing_roles"] == ["reviewer"],
        "signoff_resumed_after_restart": sign_reviewer["response"]["status"] == "effective"
        and pending["status"] == "submitted",
        "only_one_effective": competitor_blocked
        and effective["status"] == "effective"
        and competitor_status == "superseded",
        "recalculation_matches": recalculated["hash_matches"],
        "adjustment_version": adjustment["response"]["version"],
        "adjustment_impacts_round": [item["round_id"] for item in impacts] == ["round-2026"],
        "adjustment_recorded_in_lineage": len(lineage) >= 1,
        "west_versions": [item["version"] for item in versions],
        "approved_combo_immutable": recalculated_after["hash_matches"]
        and recalculated_after["hash_matches"] == recalculated["hash_matches"],
        "split_children": split["response"]["children"],
    }


def _site(service: PortfolioService) -> str:
    service.register_site(request_id="req-site", actor_id="admin-001", site_id="site-001",
                          organization_id="org-001", name="通道调度节点",
                          timezone_name="Asia/Shanghai")
    return "site-001"


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    checks = [key for key, value in result.items() if isinstance(value, bool)
              and key not in ("status", "first_replayed")]
    return 0 if (result["status"] == "ok" and result["audit_valid"]
                 and result["first_replayed"] is False
                 and all(result[key] for key in checks)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
