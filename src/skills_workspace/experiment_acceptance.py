"""软件测试实验平台的离线端到端验收。

在临时 SQLite 数据库中走完：不可变输入登记 → 乱序分片（同内容幂等、
异内容拒绝覆盖）→ 齐套冻结与稳定失败/偶发/无效判定 → 带截止时间的
复核与重跑 → 只引用冻结输入的统计 → 模拟中断后重启继续合并。
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .audit import digest
from .clock import FixedClock
from .experiments import ExperimentService
from .service import DomainService
from .storage import Database


def _passed(case_id: str) -> dict:
    return {"case_id": case_id, "outcome": "passed"}


def _failed(case_id: str, message: str) -> dict:
    return {"case_id": case_id, "outcome": "failed",
            "failure": {"type": "AssertionError", "message": message},
            "log_digest": "a" * 64}


def _shard(svc, request_id, run_id, index, cases, coverage_digest, build_digest, env_digest):
    content = {"build_digest": build_digest, "environment_digest": env_digest,
               "coverage_digest": coverage_digest, "cases": cases}
    return svc.upload_shard(request_id=request_id, actor_id="t1", run_id=run_id,
                            shard_index=index, content_hash=digest(content),
                            build_digest=build_digest, environment_digest=env_digest,
                            cases=cases, coverage_digest=coverage_digest)


def run() -> dict[str, object]:
    """执行实验平台完整链路并返回验收结果。"""

    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "experiments.sqlite3")
        clock = FixedClock(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc))
        base = DomainService(database, clock)
        svc = ExperimentService(database, clock)

        base.register_organization(request_id="org", actor_id="bootstrap",
                                   organization_id="o1", name="世赛软件测试教研组")
        base.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                            display_name="管理员", role="admin", organization_id="o1")
        base.register_actor(request_id="teacher", actor_id="a1", new_actor_id="t1",
                            display_name="教师", role="teacher", organization_id="o1")
        base.register_actor(request_id="student", actor_id="a1", new_actor_id="s1",
                            display_name="学生", role="student", organization_id="o1")
        base.register_actor(request_id="reviewer", actor_id="a1", new_actor_id="r1",
                            display_name="复核员", role="reviewer", organization_id="o1")

        # 1) 不可变输入登记。
        build_digest = digest({"commit": "9f1c", "files": ["tax.py"]})
        env_digest_value = digest({"os": "linux", "python": "3.11"})
        svc.register_build(request_id="build", actor_id="t1", build_id="build-1",
                           program_name="tax-calculator", build_digest=build_digest,
                           manifest={"commit": "9f1c", "files": ["tax.py"]})
        svc.register_case_package(request_id="pkg", actor_id="t1", package_id="pkg-1",
                                  version="2026.1", package_digest=digest({"spec": 1}),
                                  case_ids=["t1", "t2"])
        svc.register_environment(request_id="env", actor_id="t1", environment_id="env-1",
                                 declaration={"os": "linux", "python": "3.11"})
        svc.register_experiment(request_id="exp", actor_id="t1", experiment_id="exp-1",
                                build_id="build-1", package_id="pkg-1",
                                package_version="2026.1", environment_id="env-1")

        # 2) 第一次尝试：t1 通过、t2 失败（首次观察，稳定失败候选）。
        run1 = svc.open_run(request_id="run1", actor_id="t1", experiment_id="exp-1",
                            expected_shards=1)
        _shard(svc, "r1s", run1["run_id"], 0,
               [_passed("t1"), _failed("t2", "税额应为 100 实际 90")],
               "1" * 64, build_digest, env_digest_value)
        replay1 = svc.get_run(run1["run_id"])
        first_conclusion = replay1["conclusion"]

        # 3) 第二次尝试乱序两片：同签名失败再次出现 -> 稳定失败。
        run2 = svc.open_run(request_id="run2", actor_id="t1", experiment_id="exp-1",
                            expected_shards=2)
        late = _shard(svc, "r2s2", run2["run_id"], 1,
                      [_failed("t2", "税额应为 100 实际 88")],
                      "3" * 64, build_digest, env_digest_value)
        early = _shard(svc, "r2s1", run2["run_id"], 0, [_passed("t1")],
                       "2" * 64, build_digest, env_digest_value)
        stable_conclusion = early["conclusion"]
        replayed = _shard(svc, "r2s2", run2["run_id"], 1,
                          [_failed("t2", "税额应为 100 实际 88")],
                          "3" * 64, build_digest, env_digest_value)

        # 4) 复核：教师发起、学生一次说明、复核员决定重跑。
        deadline = (clock.now() + timedelta(days=1)).isoformat().replace("+00:00", "Z")
        review = svc.open_review(request_id="review", actor_id="t1", run_id=run2["run_id"],
                                 student_actor_id="s1", deadline=deadline)
        svc.submit_explanation(request_id="note", actor_id="s1",
                               review_id=review["review_id"], explanation="怀疑浮点取整差异")
        decision = svc.decide_review(request_id="decide", actor_id="r1",
                                     review_id=review["review_id"], decision="rerun")
        rerun_id = decision["rerun_run_id"]

        # 5) 重跑尝试中 t2 通过 -> 结果跨尝试不一致 -> 后续失败判偶发。
        #    重跑继承 run2 的两片声明。
        _shard(svc, "r3s1", rerun_id, 0, [_passed("t1")],
               "4" * 64, build_digest, env_digest_value)
        _shard(svc, "r3s2", rerun_id, 1, [_passed("t2")],
               "7" * 64, build_digest, env_digest_value)
        run4 = svc.open_run(request_id="run4", actor_id="t1", experiment_id="exp-1",
                            expected_shards=1)
        flaky_receipt = _shard(svc, "r4s", run4["run_id"], 0,
                               [_passed("t1"), _failed("t2", "税额应为 100 实际 91")],
                               "5" * 64, build_digest, env_digest_value)

        # 6) 统计快照只引用冻结输入。
        stats = svc.stats_snapshot(request_id="stats", actor_id="t1", experiment_id="exp-1")

        # 7) 模拟中断：直接落一片齐套分片但保留 collecting，重启后恢复合并。
        interrupted = svc.open_run(request_id="run5", actor_id="t1", experiment_id="exp-1",
                                   expected_shards=1)
        content = {"build_digest": build_digest, "environment_digest": env_digest_value,
                   "coverage_digest": "6" * 64,
                   "cases": [_passed("t1"), _passed("t2")]}
        database.connection.execute(
            "INSERT INTO shards(run_id,shard_index,content_hash,build_digest,"
            "environment_digest,payload_json,uploaded_by,received_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (interrupted["run_id"], 0, digest(content), build_digest, env_digest_value,
             json.dumps(content, sort_keys=True, separators=(",", ":")), "t1",
             "2026-09-25T08:00:00Z"))
        database.close()

        restarted = Database(Path(directory) / "experiments.sqlite3")
        svc2 = ExperimentService(restarted, clock)
        recovered = svc2.recover_interrupted()
        audit_valid, audit_events = DomainService(restarted, clock).verify_audit()
        replayed_recovery = svc2.recover_interrupted()
        replay4 = svc2.get_run(run4["run_id"])
        restarted.close()

        checks = {
            "first_observation_is_stable_fail_candidate": first_conclusion == "stable_fail",
            "out_of_order_freeze": late["frozen"] is False and early["frozen"] is True,
            "stable_fail_confirmed": stable_conclusion == "stable_fail",
            "shard_idempotent_replay": replayed["replayed"] is True,
            "rerun_is_attempt_3": decision["rerun_attempt"] == 3,
            "flaky_detected": flaky_receipt["conclusion"] == "flaky",
            "replay_explains_flaky":
                any(item["rule"] == "flaky_passed_before"
                    for item in replay4["judgement_evidence"]),
            "stats_reference_only_frozen":
                stats["frozen_run_count"] == 4 and len(stats["frozen_inputs"]) == 4
                and all(item["inputs_hash"] for item in stats["frozen_inputs"]),
            "restart_resumes_merge": len(recovered) == 1
                and recovered[0]["run_id"] == interrupted["run_id"]
                and recovered[0]["conclusion"] == "pass",
            "recovery_is_idempotent": replayed_recovery == [],
            "audit_chain_valid": audit_valid,
        }
        return {"status": "ok" if all(checks.values()) else "failed",
                "checks": checks, "audit_events": audit_events,
                "conclusions": {"run1": first_conclusion, "run2": stable_conclusion,
                                "run4": flaky_receipt["conclusion"]},
                "stats_conclusion_counts": stats["conclusion_counts"]}


def main() -> int:
    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
