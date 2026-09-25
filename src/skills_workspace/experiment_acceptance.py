"""软件测试实验平台的离线端到端验收。

覆盖：不可变制品登记、分片乱序上传与防覆盖、齐套冻结、稳定失败/偶发判定、
重复失败签名、冻结输入重放、复核（学生一次补充 + 采信/重跑）、统计引用冻结
摘要，以及关闭数据库重新打开后的分片合并恢复。
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .audit import digest
from .clock import FixedClock
from .errors import ConflictError
from .experiments import ExperimentService
from .storage import Database


def _case(case_id: str, outcomes, signature: str = "") -> dict:
    attempts = []
    for outcome in outcomes:
        attempt = {"outcome": outcome}
        if outcome == "fail":
            attempt["failure_signature"] = signature or f"sig-{case_id}"
            attempt["log_summary"] = f"{case_id} 失败日志摘要"
        attempts.append(attempt)
    return {
        "case_id": case_id,
        "coverage": {"evidence_ref": f"coverage/{case_id}.lcov", "line_rate": 0.8},
        "attempts": attempts,
    }


def _shard(index: int, cases, env_digest: str) -> dict:
    return {
        "executor": {"executor_id": f"executor-{index}", "observed_environment_digest": env_digest},
        "cases": cases,
    }


def run() -> dict[str, object]:
    with tempfile.TemporaryDirectory() as directory:
        db_path = Path(directory) / "experiment_acceptance.sqlite3"
        clock = FixedClock(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc))
        database = Database(db_path)
        service = ExperimentService(database, clock)

        # 组织与角色：管理员、教师(operator)、复核员、学生。
        service.register_organization(request_id="org", actor_id="bootstrap",
                                      organization_id="org-1", name="软件测试教研组")
        service.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="adm",
                               display_name="管理员", role="admin", organization_id="org-1")
        service.register_actor(request_id="teacher", actor_id="adm", new_actor_id="tea",
                               display_name="指导教师", role="operator", organization_id="org-1")
        service.register_actor(request_id="reviewer", actor_id="adm", new_actor_id="rev",
                               display_name="复核员", role="reviewer", organization_id="org-1")
        service.register_actor(request_id="student", actor_id="adm", new_actor_id="stu",
                               display_name="参赛学生", role="student", organization_id="org-1")
        service.register_site(request_id="site", actor_id="tea", site_id="lab-1",
                              organization_id="org-1", name="软件测试世赛集训室",
                              timezone_name="Asia/Shanghai")

        # 不可变制品：程序构建摘要、用例包版本、运行环境声明。
        build_manifest = {"commit": "a1b2c3d", "compiler": "gcc 13", "options": ["-O2"]}
        build_digest = digest(build_manifest)
        package_manifest = {"version": "case-pack-2026.09", "suite": "regression"}
        package_digest = digest(package_manifest)
        env_manifest = {"os": "linux-6.6", "python": "3.11", "image": "runner:2026.09"}
        env_digest = digest(env_manifest)
        case_ids = ["case-login", "case-cart", "case-pay", "case-search"]

        service.register_build(request_id="build", actor_id="tea", build_id="build-9",
                               source_ref="repo@a1b2c3d", build_digest=build_digest,
                               manifest=build_manifest)
        service.register_case_package(request_id="pkg", actor_id="tea", package_id="pack-3",
                                      package_digest=package_digest, case_ids=case_ids,
                                      manifest=package_manifest)
        service.register_environment(request_id="env", actor_id="tea", environment_id="env-7",
                                     environment_digest=env_digest, manifest=env_manifest)

        # 同编号重复登记同内容幂等、异内容冲突。
        again = service.register_build(request_id="build", actor_id="tea", build_id="build-9",
                                       source_ref="repo@a1b2c3d", build_digest=build_digest,
                                       manifest=build_manifest)
        try:
            service.register_build(request_id="build-x", actor_id="tea", build_id="build-9",
                                   source_ref="repo@a1b2c3d",
                                   build_digest="f" * 64, manifest={"tampered": True})
            raise AssertionError("异内容覆盖必须被拒绝")
        except ConflictError:
            pass

        # 实验一：偶发结果。分片乱序到达（先 1 后 0）。
        service.create_run(request_id="run-flaky", actor_id="tea", run_id="run-flaky",
                           site_id="lab-1", build_id="build-9", package_id="pack-3",
                           environment_id="env-7", expected_shards=2)
        up1 = service.upload_shard(actor_id="tea", run_id="run-flaky", shard_index=1,
                                   shard=_shard(1, [
                                       _case("case-search", ["pass", "pass"]),
                                       _case("case-pay", ["pass", "pass"]),
                                   ], env_digest))
        assert up1["frozen"] is False and up1["received_shards"] == 1
        up0 = service.upload_shard(actor_id="tea", run_id="run-flaky", shard_index=0,
                                   shard=_shard(0, [
                                       _case("case-login", ["fail", "pass"], "SIG-TIMEOUT"),
                                       _case("case-cart", ["pass", "pass"]),
                                   ], env_digest))
        assert up0["frozen"] is True and up0["verdict"] == "flaky"

        # 同片重传：同内容幂等、异内容冲突；冻结后拒绝新分片。
        replay = service.upload_shard(actor_id="tea", run_id="run-flaky", shard_index=0,
                                      shard=_shard(0, [
                                          _case("case-login", ["fail", "pass"], "SIG-TIMEOUT"),
                                          _case("case-cart", ["pass", "pass"]),
                                      ], env_digest))
        assert replay["replayed"] is True
        try:
            service.upload_shard(actor_id="tea", run_id="run-flaky", shard_index=0,
                                 shard=_shard(0, [_case("case-login", ["fail", "fail"], "SIG-TIMEOUT")], env_digest))
            raise AssertionError("不同内容不能覆盖同一分片")
        except ConflictError:
            pass

        # 实验二：稳定失败，两个用例命中同一失败签名（缺陷复现聚类）。
        service.create_run(request_id="run-stable", actor_id="tea", run_id="run-stable",
                           site_id="lab-1", build_id="build-9", package_id="pack-3",
                           environment_id="env-7", expected_shards=2)
        service.upload_shard(actor_id="tea", run_id="run-stable", shard_index=0,
                             shard=_shard(0, [
                                 _case("case-pay", ["fail", "fail"], "SIG-NPE-42"),
                                 _case("case-cart", ["fail", "fail"], "SIG-NPE-42"),
                             ], env_digest))
        stable_up = service.upload_shard(actor_id="tea", run_id="run-stable", shard_index=1,
                                         shard=_shard(1, [
                                             _case("case-login", ["pass", "pass"]),
                                             _case("case-search", ["pass", "pass"]),
                                         ], env_digest))
        assert stable_up["verdict"] == "stable_failure"
        stable_run = service.get_run("run-stable")
        repeated = stable_run["decision"]["result"]["repeated_signatures"]
        assert len(repeated) == 1 and repeated[0]["case_count"] == 2

        # 重放判定理由，且摘要/结论必须与冻结时一致。
        replay_stable = service.replay_run("run-stable")
        assert replay_stable["matches"] is True
        assert "全部尝试中均失败" in replay_stable["explanation"]["summary"]
        replay_flaky = service.replay_run("run-flaky")
        assert replay_flaky["matches"] is True and "偶发" in replay_flaky["explanation"]["summary"]

        # 失败签名跨运行检索。
        clustered = service.search_signature("SIG-NPE-42")
        assert clustered["match_count"] == 2

        # 复核流程：教师发起带截止时间的复核，学生提交一次补充，复核员采信。
        deadline = (clock.now() + timedelta(days=2)).isoformat().replace("+00:00", "Z")
        service.open_review(request_id="review-1", actor_id="tea", run_id="run-stable",
                            deadline=deadline, note="请说明稳定失败是否为环境问题")
        review_id = service.get_run("run-stable")["review_id"]
        service.submit_supplement(request_id="sup-1", actor_id="stu", review_id=review_id,
                                  content="已在本机复现，空购物车触发空指针，疑似候选程序缺陷")
        try:
            service.submit_supplement(request_id="sup-2", actor_id="stu", review_id=review_id,
                                      content="再补充一次")
            raise AssertionError("学生只能提交一次补充说明")
        except ConflictError:
            pass
        service.decide_review(request_id="dec-1", actor_id="rev", review_id=review_id,
                              decision="accept", decision_note="补充证据采信，维持稳定失败")
        accepted = service.get_review(review_id)
        assert accepted["status"] == "accepted" and len(accepted["supplements"]) == 1

        # 第二条复核走重跑：旧运行被新运行取代。
        deadline2 = (clock.now() + timedelta(days=1)).isoformat().replace("+00:00", "Z")
        service.open_review(request_id="review-2", actor_id="tea", run_id="run-flaky",
                            deadline=deadline2, note="偶发结论请重跑确认")
        review2 = service.get_run("run-flaky")["review_id"]
        rerun = service.decide_review(request_id="dec-2", actor_id="rev", review_id=review2,
                                      decision="rerun", decision_note="重跑确认偶发率")
        assert rerun.resource_id == review2
        rerun_id = service.get_review(review2)["rerun_run_id"]
        assert service.get_run("run-flaky")["status"] == "superseded"
        assert service.get_run(rerun_id)["status"] == "collecting"

        # 统计只引用冻结输入摘要。
        stats = service.statistics()
        assert stats["frozen_run_count"] == 1
        assert all(item["frozen_inputs_digest"] for item in stats["runs"])

        audit_valid, audit_events = service.verify_audit()
        database.close()

        # 中断重启恢复：新进程打开同一数据库，重放仍一致；resume 不破坏状态。
        restarted = Database(db_path)
        service2 = ExperimentService(restarted, clock)
        resumed = service2.resume_pending()
        assert resumed == []
        assert service2.replay_run("run-stable")["matches"] is True
        restarted.close()

        return {
            "status": "ok",
            "audit_valid": audit_valid,
            "audit_events": audit_events,
            "flaky_verdict": up0["verdict"],
            "stable_verdict": stable_up["verdict"],
            "repeated_signature_cases": repeated[0]["case_count"],
            "replay_matches": replay_stable["matches"] and replay_flaky["matches"],
            "signature_cluster_size": clustered["match_count"],
            "rerun_run_id": rerun_id,
            "frozen_runs_in_stats": stats["frozen_run_count"],
        }


def main() -> int:
    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ok" and result["audit_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
