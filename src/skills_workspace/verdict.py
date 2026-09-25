"""实验结论判定的纯函数模块。

该模块不依赖数据库，只负责把离线执行器上传的分片内容合并为逐用例视图，
依据固定证据规则判定 stable_failure / flaky / stable_pass / invalid，
并计算冻结输入摘要。冻结时与重放时调用的是同一份代码，因此“为何这样判定”
可以在任何时候从冻结快照离线复算。
"""

from __future__ import annotations

from typing import Any

from .audit import canonical_json, digest

PASS = "pass"
FAIL = "fail"

VERDICT_STABLE_FAILURE = "stable_failure"
VERDICT_FLAKY = "flaky"
VERDICT_STABLE_PASS = "stable_pass"
VERDICT_INVALID = "invalid"

# 判定规则按固定顺序求值，invalid 的 reason_code 取第一条失败规则。
RULE_SHARDS_COMPLETE = "shards_complete"
RULE_CASE_SET_EXACT = "case_set_exact"
RULE_ATTEMPT_EVIDENCE = "attempt_evidence_complete"
RULE_COVERAGE_EVIDENCE = "coverage_evidence_present"
RULE_ENVIRONMENT_MATCH = "environment_matches_declared"

INVALID_REASONS = (
    "shards_incomplete",
    "case_set_mismatch",
    "malformed_attempt_evidence",
    "coverage_evidence_missing",
    "environment_mismatch",
)


def signature_id(signature: str) -> str:
    """返回失败签名的短标识，便于展示而不泄露过长日志。"""

    return digest({"failure_signature": signature})[:12]


def _coverage_missing(case: dict[str, Any]) -> bool:
    coverage = case.get("coverage")
    if not isinstance(coverage, dict):
        return True
    evidence_ref = str(coverage.get("evidence_ref", "")).strip()
    if evidence_ref:
        return False
    covered_files = coverage.get("covered_files")
    return not (
        isinstance(covered_files, list)
        and bool(covered_files)
        and all(isinstance(item, str) and item.strip() for item in covered_files)
    )


def _observed_environment(shards: list[dict[str, Any]]) -> tuple[str | None, bool]:
    """汇总执行器观测到的环境摘要，返回(摘要, 是否自相矛盾)。"""

    observed: set[str] = set()
    for shard in shards:
        executor = shard.get("executor")
        if isinstance(executor, dict):
            value = str(executor.get("observed_environment_digest", "")).strip()
            if value:
                observed.add(value)
    if not observed:
        return None, False
    if len(observed) > 1:
        return sorted(observed)[0], True
    return next(iter(observed)), False


def classify(
    expected_case_ids: list[str],
    shards: list[dict[str, Any]],
    *,
    expected_shards: int | None = None,
    declared_environment_digest: str | None = None,
) -> dict[str, Any]:
    """对一组已到齐的分片执行确定性判定。

    参数 shards 中的每个元素为 ``{"shard_index": int, "executor": dict, "cases": list}``。
    返回结构化判定轨迹，冻结与重放都使用该结果。
    """

    ordered = sorted(shards, key=lambda item: int(item.get("shard_index", 0)))
    violations: dict[str, list[Any]] = {
        "case_set_mismatch": [],
        "malformed_attempt_evidence": [],
        "coverage_evidence_missing": [],
        "environment_mismatch": [],
    }

    indices = sorted(int(item.get("shard_index", -1)) for item in ordered)
    shards_complete = True
    if expected_shards is not None:
        shards_complete = indices == list(range(expected_shards))

    # case_id -> [(shard_index, case_obj), ...]，正常情况只会出现一次。
    appearances: dict[str, list[tuple[int, dict[str, Any]]]] = {}
    unknown_cases: set[str] = set()
    for shard in ordered:
        shard_index = int(shard.get("shard_index", -1))
        cases = shard.get("cases")
        if not isinstance(cases, list):
            violations["case_set_mismatch"].append(
                {"shard_index": shard_index, "problem": "cases_not_list"}
            )
            continue
        for case in cases:
            if not isinstance(case, dict):
                violations["case_set_mismatch"].append(
                    {"shard_index": shard_index, "problem": "case_not_object"}
                )
                continue
            case_id = str(case.get("case_id", "")).strip()
            if not case_id:
                violations["case_set_mismatch"].append(
                    {"shard_index": shard_index, "problem": "case_id_missing"}
                )
                continue
            if case_id not in expected_case_ids:
                unknown_cases.add(case_id)
            appearances.setdefault(case_id, []).append((shard_index, case))

    expected_set = set(expected_case_ids)
    seen_set = set(appearances)
    missing_cases = sorted(expected_set - seen_set)
    duplicate_cases = sorted(case_id for case_id, items in appearances.items() if len(items) > 1)
    if missing_cases or unknown_cases or duplicate_cases:
        violations["case_set_mismatch"].append(
            {
                "missing": missing_cases,
                "unexpected": sorted(unknown_cases),
                "duplicate": duplicate_cases,
            }
        )

    case_views: list[dict[str, Any]] = []
    failed_signature_cases: dict[str, set[str]] = {}
    failed_attempts_total = 0

    for case_id in expected_case_ids:
        items = appearances.get(case_id)
        if not items or len(items) != 1:
            continue
        shard_index, case = items[0]
        attempts = case.get("attempts")
        passed = 0
        failed = 0
        signatures: list[str] = []
        if not isinstance(attempts, list) or not attempts:
            violations["malformed_attempt_evidence"].append(
                {"case_id": case_id, "problem": "attempts_missing_or_empty"}
            )
        else:
            for attempt_index, attempt in enumerate(attempts):
                if not isinstance(attempt, dict):
                    violations["malformed_attempt_evidence"].append(
                        {"case_id": case_id, "attempt_index": attempt_index, "problem": "attempt_not_object"}
                    )
                    continue
                outcome = str(attempt.get("outcome", "")).strip()
                if outcome not in (PASS, FAIL):
                    violations["malformed_attempt_evidence"].append(
                        {"case_id": case_id, "attempt_index": attempt_index, "problem": "outcome_invalid",
                         "outcome": outcome}
                    )
                    continue
                if outcome == PASS:
                    passed += 1
                else:
                    failed += 1
                    failed_attempts_total += 1
                    signature = str(attempt.get("failure_signature", "")).strip()
                    log_summary = str(attempt.get("log_summary", "")).strip()
                    if not signature or not log_summary:
                        violations["malformed_attempt_evidence"].append(
                            {"case_id": case_id, "attempt_index": attempt_index,
                             "problem": "failure_evidence_incomplete"}
                        )
                    if signature:
                        signatures.append(signature)
                        failed_signature_cases.setdefault(signature, set()).add(case_id)
        if _coverage_missing(case):
            coverage = case.get("coverage")
            violations["coverage_evidence_missing"].append(
                {"case_id": case_id, "has_coverage_field": isinstance(coverage, dict)}
            )
        total = passed + failed
        if total == 0:
            classification = "no_evidence"
        elif failed and passed:
            classification = VERDICT_FLAKY
        elif failed:
            classification = VERDICT_STABLE_FAILURE
        else:
            classification = VERDICT_STABLE_PASS
        case_views.append(
            {
                "case_id": case_id,
                "shard_index": shard_index,
                "attempts": total,
                "passed": passed,
                "failed": failed,
                "classification": classification,
                "failure_signatures": sorted(set(signatures)),
            }
        )

    observed_digest, env_conflict = _observed_environment(ordered)
    if env_conflict:
        violations["environment_mismatch"].append({"problem": "shards_report_different_environments"})
    elif observed_digest and declared_environment_digest and observed_digest != declared_environment_digest:
        violations["environment_mismatch"].append(
            {"declared": declared_environment_digest, "observed": observed_digest}
        )

    checks = [
        {"rule": RULE_SHARDS_COMPLETE, "ok": shards_complete,
         "detail": {"expected": expected_shards, "received_indices": indices}},
        {"rule": RULE_CASE_SET_EXACT, "ok": not violations["case_set_mismatch"],
         "detail": violations["case_set_mismatch"]},
        {"rule": RULE_ATTEMPT_EVIDENCE, "ok": not violations["malformed_attempt_evidence"],
         "detail": violations["malformed_attempt_evidence"]},
        {"rule": RULE_COVERAGE_EVIDENCE, "ok": not violations["coverage_evidence_missing"],
         "detail": violations["coverage_evidence_missing"]},
        {"rule": RULE_ENVIRONMENT_MATCH, "ok": not violations["environment_mismatch"],
         "detail": violations["environment_mismatch"]},
    ]

    failed_check = next((check for check in checks if not check["ok"]), None)
    if failed_check is not None:
        reason_map = {
            RULE_SHARDS_COMPLETE: "shards_incomplete",
            RULE_CASE_SET_EXACT: "case_set_mismatch",
            RULE_ATTEMPT_EVIDENCE: "malformed_attempt_evidence",
            RULE_COVERAGE_EVIDENCE: "coverage_evidence_missing",
            RULE_ENVIRONMENT_MATCH: "environment_mismatch",
        }
        verdict = VERDICT_INVALID
        reason_code = reason_map[failed_check["rule"]]
    else:
        stable_failures = [view for view in case_views if view["classification"] == VERDICT_STABLE_FAILURE]
        flaky_cases = [view for view in case_views if view["classification"] == VERDICT_FLAKY]
        if stable_failures:
            verdict = VERDICT_STABLE_FAILURE
            reason_code = "deterministic_failures"
        elif flaky_cases:
            verdict = VERDICT_FLAKY
            reason_code = "mixed_outcomes"
        else:
            verdict = VERDICT_STABLE_PASS
            reason_code = "all_cases_passed"

    repeated_signatures = [
        {
            "signature": signature,
            "signature_id": signature_id(signature),
            "cases": sorted(case_ids),
            "case_count": len(case_ids),
        }
        for signature, case_ids in sorted(failed_signature_cases.items())
        if len(case_ids) >= 2
    ]

    return {
        "verdict": verdict,
        "reason_code": reason_code,
        "checks": checks,
        "violations": {key: value for key, value in violations.items() if value},
        "cases": case_views,
        "repeated_signatures": repeated_signatures,
        "observed_environment_digest": observed_digest,
        "counts": {
            "expected_cases": len(expected_case_ids),
            "stable_failure_cases": sum(1 for v in case_views if v["classification"] == VERDICT_STABLE_FAILURE),
            "flaky_cases": sum(1 for v in case_views if v["classification"] == VERDICT_FLAKY),
            "passed_cases": sum(1 for v in case_views if v["classification"] == VERDICT_STABLE_PASS),
            "failed_attempts": failed_attempts_total,
        },
    }


def frozen_inputs_digest(
    *,
    build_digest: str,
    package_digest: str,
    environment_digest: str,
    shard_hashes: list[tuple[int, str]],
) -> str:
    """计算冻结输入摘要：构建、用例包、环境与每个分片内容的哈希组合。"""

    material = {
        "build_digest": build_digest,
        "case_package_digest": package_digest,
        "environment_digest": environment_digest,
        "shards": [
            {"shard_index": index, "content_hash": content_hash}
            for index, content_hash in sorted(shard_hashes)
        ],
    }
    return digest(material)


def canonical_payload(value: Any) -> str:
    """暴露规范 JSON 序列化，保持服务层一致。"""

    return canonical_json(value)
