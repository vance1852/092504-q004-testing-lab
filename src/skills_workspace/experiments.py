"""软件测试实验运行与缺陷复现的领域服务。

在基础登记服务之上增加：
- 不可变程序构建、用例包版本、运行环境声明的登记；
- 实验运行创建与离线执行器分片上传（乱序、防覆盖、齐套冻结）；
- 稳定失败 / 偶发 / 无效结论的确定性判定与冻结快照；
- 教师发起带截止时间的复核、学生一次补充说明、复核员采信或重跑；
- 只引用冻结输入的统计，以及从冻结快照重放判定理由。
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from .audit import append_event, canonical_json, digest
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .service import DomainService
from . import verdict as verdict_rules

HEX_DIGEST = re.compile(r"^[a-f0-9]{64}$")
COLLECTING = "collecting"
FROZEN = "frozen"
SUPERSEDED = "superseded"


class ExperimentService(DomainService):
    """协调实验制品登记、分片合并、冻结与复核。"""

    # ---- 不可变制品登记 -------------------------------------------------

    def _digest_field(self, value: str, field: str) -> str:
        value = str(value).strip().lower()
        if not HEX_DIGEST.fullmatch(value):
            raise ValidationError(f"{field} 必须是 64 位小写十六进制摘要")
        return value

    def _immutable_register(self, connection, *, table: str, key_field: str, key_value: str,
                            digest_field: str, digest_value: str, extra_columns: dict[str, Any],
                            actor_id: str) -> tuple[bool, str]:
        """同编号同摘要幂等、同编号异摘要冲突、同摘要异编号冲突。"""

        same_key = connection.execute(
            f"SELECT * FROM {table} WHERE {key_field}=?", (key_value,)
        ).fetchone()
        if same_key is not None:
            if same_key[digest_field] != digest_value:
                raise ConflictError(f"{key_field} 已登记不同内容，不可变制品不能被覆盖")
            return True, key_value
        same_digest = connection.execute(
            f"SELECT {key_field} AS k FROM {table} WHERE {digest_field}=?", (digest_value,)
        ).fetchone()
        if same_digest is not None:
            raise ConflictError(f"相同摘要已以编号 {same_digest['k']} 登记")
        columns = [key_field, digest_field, *extra_columns.keys()]
        placeholders = ", ".join("?" for _ in columns)
        connection.execute(
            f"INSERT INTO {table}({', '.join(columns)}) VALUES({placeholders})",
            (key_value, digest_value, *extra_columns.values()),
        )
        return False, key_value

    def register_build(self, *, request_id: str, actor_id: str, build_id: str,
                       source_ref: str, build_digest: str, manifest: dict[str, Any]) -> Any:
        if not isinstance(manifest, dict) or not manifest:
            raise ValidationError("manifest 必须是非空对象")
        payload = {"actor_id": actor_id, "build_id": build_id, "source_ref": source_ref,
                   "build_digest": build_digest, "manifest": manifest}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            build_id = self._identifier(build_id, "build_id")
            source_ref = self._text(source_ref, "source_ref")
            build_digest = self._digest_field(build_digest, "build_digest")

            def create() -> tuple[str, str, dict[str, Any]]:
                replayed, _ = self._immutable_register(
                    connection, table="builds", key_field="build_id", key_value=build_id,
                    digest_field="build_digest", digest_value=build_digest,
                    extra_columns={"source_ref": source_ref,
                                   "manifest_json": canonical_json(manifest),
                                   "registered_by": actor_id, "created_at": self._now()},
                    actor_id=actor_id,
                )
                append_event(connection, actor_id=actor_id, action="build.registered",
                             resource_type="build", resource_id=build_id,
                             detail={"build_digest": build_digest, "source_ref": source_ref},
                             occurred_at=self._now())
                return "build", build_id, {"build_id": build_id, "replayed": replayed}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_build", payload=payload, create=create)

    def register_case_package(self, *, request_id: str, actor_id: str, package_id: str,
                              package_digest: str, case_ids: list[str],
                              manifest: dict[str, Any]) -> Any:
        if not isinstance(manifest, dict) or not manifest:
            raise ValidationError("manifest 必须是非空对象")
        if not isinstance(case_ids, list) or not case_ids:
            raise ValidationError("case_ids 必须是非空数组")
        cleaned: list[str] = []
        for case_id in case_ids:
            cleaned.append(self._identifier(case_id, "case_id"))
        if len(set(cleaned)) != len(cleaned):
            raise ValidationError("case_ids 不能重复")
        payload = {"actor_id": actor_id, "package_id": package_id, "package_digest": package_digest,
                   "case_ids": cleaned, "manifest": manifest}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            package_id = self._identifier(package_id, "package_id")
            package_digest = self._digest_field(package_digest, "package_digest")

            def create() -> tuple[str, str, dict[str, Any]]:
                replayed, _ = self._immutable_register(
                    connection, table="case_packages", key_field="package_id", key_value=package_id,
                    digest_field="package_digest", digest_value=package_digest,
                    extra_columns={"case_count": len(cleaned),
                                   "case_ids_json": canonical_json(cleaned),
                                   "manifest_json": canonical_json(manifest),
                                   "registered_by": actor_id, "created_at": self._now()},
                    actor_id=actor_id,
                )
                append_event(connection, actor_id=actor_id, action="case_package.registered",
                             resource_type="case_package", resource_id=package_id,
                             detail={"package_digest": package_digest, "case_count": len(cleaned)},
                             occurred_at=self._now())
                return "case_package", package_id, {"package_id": package_id, "replayed": replayed}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_case_package", payload=payload, create=create)

    def register_environment(self, *, request_id: str, actor_id: str, environment_id: str,
                             environment_digest: str, manifest: dict[str, Any]) -> Any:
        if not isinstance(manifest, dict) or not manifest:
            raise ValidationError("manifest 必须是非空对象")
        payload = {"actor_id": actor_id, "environment_id": environment_id,
                   "environment_digest": environment_digest, "manifest": manifest}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            environment_id = self._identifier(environment_id, "environment_id")
            environment_digest = self._digest_field(environment_digest, "environment_digest")

            def create() -> tuple[str, str, dict[str, Any]]:
                replayed, _ = self._immutable_register(
                    connection, table="environments", key_field="environment_id", key_value=environment_id,
                    digest_field="environment_digest", digest_value=environment_digest,
                    extra_columns={"manifest_json": canonical_json(manifest),
                                   "registered_by": actor_id, "created_at": self._now()},
                    actor_id=actor_id,
                )
                append_event(connection, actor_id=actor_id, action="environment.registered",
                             resource_type="environment", resource_id=environment_id,
                             detail={"environment_digest": environment_digest},
                             occurred_at=self._now())
                return "environment", environment_id, {"environment_id": environment_id, "replayed": replayed}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_environment", payload=payload, create=create)

    # ---- 实验运行与分片 -------------------------------------------------

    def create_run(self, *, request_id: str, actor_id: str, run_id: str, site_id: str,
                   build_id: str, package_id: str, environment_id: str,
                   expected_shards: int) -> Any:
        payload = {"actor_id": actor_id, "run_id": run_id, "site_id": site_id,
                   "build_id": build_id, "package_id": package_id,
                   "environment_id": environment_id, "expected_shards": expected_shards}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            run_id = self._identifier(run_id, "run_id")
            if not isinstance(expected_shards, int) or isinstance(expected_shards, bool) or expected_shards < 1:
                raise ValidationError("expected_shards 必须是不小于 1 的整数")
            site = connection.execute("SELECT * FROM sites WHERE site_id=?", (site_id,)).fetchone()
            if site is None:
                raise NotFoundError("场所不存在")
            if actor.organization_id != site["organization_id"] and actor.role != "admin":
                raise PermissionDenied("不能在其他组织的场所创建实验")
            for table, column, value, label in (
                ("builds", "build_id", build_id, "程序构建"),
                ("case_packages", "package_id", package_id, "用例包"),
                ("environments", "environment_id", environment_id, "运行环境"),
            ):
                if connection.execute(
                    f"SELECT 1 FROM {table} WHERE {column}=?", (value,)
                ).fetchone() is None:
                    raise NotFoundError(f"{label}不存在")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO experiments(run_id,site_id,build_id,package_id,environment_id,"
                        "expected_shards,status,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                        (run_id, site_id, build_id, package_id, environment_id,
                         expected_shards, COLLECTING, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("运行编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="run.created",
                             resource_type="experiment", resource_id=run_id,
                             detail={"site_id": site_id, "build_id": build_id, "package_id": package_id,
                                     "environment_id": environment_id, "expected_shards": expected_shards},
                             occurred_at=self._now())
                return "experiment", run_id, {"run_id": run_id, "status": COLLECTING}

            return self._idempotent(connection, request_id=request_id,
                                    action="create_run", payload=payload, create=create)

    @staticmethod
    def _shard_canonical(shard: dict[str, Any]) -> dict[str, Any]:
        """提取参与内容哈希与判定的规范分片体。"""

        executor = shard.get("executor", {})
        if not isinstance(executor, dict):
            raise ValidationError("executor 必须是对象")
        cases = shard.get("cases")
        if not isinstance(cases, list):
            raise ValidationError("cases 必须是数组")
        return {"executor": executor, "cases": cases}

    def upload_shard(self, *, actor_id: str, run_id: str, shard_index: int,
                     shard: dict[str, Any]) -> dict[str, Any]:
        """接收一个分片；同片同内容幂等，同片异内容冲突。"""

        run_id = self._identifier(run_id, "run_id")
        if not isinstance(shard_index, int) or isinstance(shard_index, bool) or shard_index < 0:
            raise ValidationError("shard_index 必须是非负整数")
        body = self._shard_canonical(shard)
        content_hash = digest(body)
        client_hash = str(shard.get("content_hash", "")).strip().lower()
        if client_hash and client_hash != content_hash:
            raise ValidationError("content_hash 与分片内容不一致")
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            run = connection.execute("SELECT * FROM experiments WHERE run_id=?", (run_id,)).fetchone()
            if run is None:
                raise NotFoundError("实验运行不存在")
            if run["status"] != COLLECTING:
                # 冻结后执行器可能重试送达：只接受完全相同的分片作为重放，其余一律拒绝。
                existing = connection.execute(
                    "SELECT content_hash FROM run_shards WHERE run_id=? AND shard_index=?",
                    (run_id, shard_index),
                ).fetchone()
                if existing is not None and existing["content_hash"] == content_hash:
                    return {"run_id": run_id, "shard_index": shard_index,
                            "content_hash": content_hash, "replayed": True,
                            "received_shards": self._shard_count(connection, run_id),
                            "frozen": True, "verdict": run["verdict"]}
                raise ConflictError(f"运行已 {run['status']}，不能再上传新分片")
            if shard_index >= run["expected_shards"]:
                raise ValidationError("shard_index 超出声明的分片范围")
            existing = connection.execute(
                "SELECT content_hash FROM run_shards WHERE run_id=? AND shard_index=?",
                (run_id, shard_index),
            ).fetchone()
            replayed = False
            if existing is not None:
                if existing["content_hash"] != content_hash:
                    raise ConflictError("同一分片编号已上传不同内容，分片内容不可覆盖")
                replayed = True
            else:
                connection.execute(
                    "INSERT INTO run_shards(run_id,shard_index,content_hash,payload_json,"
                    "uploaded_by,uploaded_at) VALUES(?,?,?,?,?,?)",
                    (run_id, shard_index, content_hash, canonical_json(body),
                     actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="shard.uploaded",
                             resource_type="shard", resource_id=f"{run_id}:{shard_index}",
                             detail={"run_id": run_id, "shard_index": shard_index,
                                     "content_hash": content_hash},
                             occurred_at=self._now())
            frozen = self._freeze_if_complete(connection, run_id)
            return {"run_id": run_id, "shard_index": shard_index, "content_hash": content_hash,
                    "replayed": replayed,
                    "received_shards": self._shard_count(connection, run_id),
                    "frozen": frozen is not None,
                    "verdict": frozen["verdict"] if frozen else None}

    def _shard_count(self, connection, run_id: str) -> int:
        return connection.execute(
            "SELECT COUNT(*) AS count FROM run_shards WHERE run_id=?", (run_id,)
        ).fetchone()["count"]

    def _load_freeze_inputs(self, connection, run: Any) -> dict[str, Any]:
        package = connection.execute(
            "SELECT * FROM case_packages WHERE package_id=?", (run["package_id"],)
        ).fetchone()
        environment = connection.execute(
            "SELECT * FROM environments WHERE environment_id=?", (run["environment_id"],)
        ).fetchone()
        build = connection.execute(
            "SELECT * FROM builds WHERE build_id=?", (run["build_id"],)
        ).fetchone()
        shard_rows = connection.execute(
            "SELECT * FROM run_shards WHERE run_id=? ORDER BY shard_index", (run["run_id"],)
        ).fetchall()
        shards: list[dict[str, Any]] = []
        shard_hashes: list[tuple[int, str]] = []
        for row in shard_rows:
            payload = json.loads(row["payload_json"])
            # 重放时重新计算载荷摘要，检测落库后被篡改的分片内容。
            actual_hash = digest(payload)
            if actual_hash != row["content_hash"]:
                shard_hashes.append((row["shard_index"], actual_hash))
            else:
                shard_hashes.append((row["shard_index"], row["content_hash"]))
            shards.append({"shard_index": row["shard_index"], **payload})
        return {
            "build": build, "package": package, "environment": environment,
            "case_ids": json.loads(package["case_ids_json"]),
            "shards": shards,
            "shard_hashes": shard_hashes,
        }

    def _freeze_if_complete(self, connection, run_id: str) -> dict[str, Any] | None:
        """齐套后在当前事务内冻结结论；未齐套返回 None。"""

        run = connection.execute("SELECT * FROM experiments WHERE run_id=?", (run_id,)).fetchone()
        if run is None or run["status"] != COLLECTING:
            return None
        if self._shard_count(connection, run_id) != run["expected_shards"]:
            return None
        inputs = self._load_freeze_inputs(connection, run)
        result = verdict_rules.classify(
            inputs["case_ids"], inputs["shards"],
            expected_shards=run["expected_shards"],
            declared_environment_digest=inputs["environment"]["environment_digest"],
        )
        frozen_digest = verdict_rules.frozen_inputs_digest(
            build_digest=inputs["build"]["build_digest"],
            package_digest=inputs["package"]["package_digest"],
            environment_digest=inputs["environment"]["environment_digest"],
            shard_hashes=inputs["shard_hashes"],
        )
        decision = {
            "run_id": run_id,
            "frozen_inputs_digest": frozen_digest,
            "inputs": {
                "build_id": run["build_id"], "build_digest": inputs["build"]["build_digest"],
                "package_id": run["package_id"], "package_digest": inputs["package"]["package_digest"],
                "environment_id": run["environment_id"],
                "environment_digest": inputs["environment"]["environment_digest"],
                "case_ids": inputs["case_ids"],
                "shards": [{"shard_index": index, "content_hash": content_hash}
                           for index, content_hash in inputs["shard_hashes"]],
            },
            "result": result,
        }
        frozen_at = self._now()
        connection.execute(
            "UPDATE experiments SET status=?, frozen_inputs_digest=?, verdict=?, reason_code=?, "
            "decision_json=?, frozen_at=? WHERE run_id=?",
            (FROZEN, frozen_digest, result["verdict"], result["reason_code"],
             canonical_json(decision), frozen_at, run_id),
        )
        append_event(connection, actor_id=run["created_by"], action="run.frozen",
                     resource_type="experiment", resource_id=run_id,
                     detail={"frozen_inputs_digest": frozen_digest,
                             "verdict": result["verdict"], "reason_code": result["reason_code"],
                             "repeated_signatures": len(result["repeated_signatures"]),
                             "flaky_cases": result["counts"]["flaky_cases"]},
                     occurred_at=frozen_at)
        return result

    def resume_pending(self) -> list[dict[str, Any]]:
        """中断重启后继续未完成的分片合并：把已齐套的收集中运行冻结。"""

        frozen_runs: list[dict[str, Any]] = []
        with self.database.transaction(immediate=True) as connection:
            rows = connection.execute(
                "SELECT run_id FROM experiments WHERE status=? ORDER BY created_at, run_id",
                (COLLECTING,),
            ).fetchall()
            for row in rows:
                result = self._freeze_if_complete(connection, row["run_id"])
                if result is not None:
                    frozen_runs.append({"run_id": row["run_id"], "verdict": result["verdict"]})
        return frozen_runs

    # ---- 查询与重放 -----------------------------------------------------

    def get_run(self, run_id: str) -> dict[str, Any]:
        row = self.database.connection.execute(
            "SELECT * FROM experiments WHERE run_id=?", (run_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("实验运行不存在")
        decision = json.loads(row["decision_json"]) if row["decision_json"] else None
        return {
            "run_id": row["run_id"], "site_id": row["site_id"],
            "build_id": row["build_id"], "package_id": row["package_id"],
            "environment_id": row["environment_id"], "expected_shards": row["expected_shards"],
            "received_shards": self._shard_count(self.database.connection, run_id),
            "status": row["status"], "verdict": row["verdict"], "reason_code": row["reason_code"],
            "frozen_inputs_digest": row["frozen_inputs_digest"], "frozen_at": row["frozen_at"],
            "superseded_by_run_id": row["superseded_by_run_id"], "review_id": row["review_id"],
            "decision": decision,
        }

    def replay_run(self, run_id: str) -> dict[str, Any]:
        """从冻结输入重算判定，并解释为何得到该结论。"""

        stored = self.get_run(run_id)
        if stored["status"] not in (FROZEN, SUPERSEDED) or not stored["decision"]:
            raise ConflictError("运行尚未冻结，没有可重放的结论")
        with self.database.transaction() as connection:
            run = connection.execute("SELECT * FROM experiments WHERE run_id=?", (run_id,)).fetchone()
            inputs = self._load_freeze_inputs(connection, run)
        recomputed_result = verdict_rules.classify(
            inputs["case_ids"], inputs["shards"],
            expected_shards=run["expected_shards"],
            declared_environment_digest=inputs["environment"]["environment_digest"],
        )
        recomputed_digest = verdict_rules.frozen_inputs_digest(
            build_digest=inputs["build"]["build_digest"],
            package_digest=inputs["package"]["package_digest"],
            environment_digest=inputs["environment"]["environment_digest"],
            shard_hashes=inputs["shard_hashes"],
        )
        stored_decision = stored["decision"]
        digest_matches = recomputed_digest == stored["frozen_inputs_digest"]
        verdict_matches = recomputed_result["verdict"] == stored_decision["result"]["verdict"]
        frozen_shard_hashes = {
            item["shard_index"]: item["content_hash"]
            for item in stored_decision["inputs"]["shards"]
        }
        tampered_shards = [
            {"shard_index": index, "frozen_hash": frozen_shard_hashes.get(index),
             "current_hash": current_hash}
            for index, current_hash in inputs["shard_hashes"]
            if frozen_shard_hashes.get(index) != current_hash
        ]
        return {
            "run_id": run_id,
            "frozen_inputs_digest": stored["frozen_inputs_digest"],
            "recomputed_inputs_digest": recomputed_digest,
            "digest_matches": digest_matches,
            "verdict_matches": verdict_matches,
            "matches": digest_matches and verdict_matches and not tampered_shards,
            "tampered_shards": tampered_shards,
            "stored_verdict": stored_decision["result"]["verdict"],
            "recomputed_verdict": recomputed_result["verdict"],
            "explanation": self._explain(recomputed_result),
            "trace": recomputed_result,
        }

    @staticmethod
    def _explain(result: dict[str, Any]) -> dict[str, Any]:
        verdict = result["verdict"]
        lines: list[str] = []
        if verdict == verdict_rules.VERDICT_INVALID:
            failed = [check for check in result["checks"] if not check["ok"]]
            lines.append("证据校验未通过：" + "；".join(check["rule"] for check in failed))
        elif verdict == verdict_rules.VERDICT_STABLE_FAILURE:
            cases = [c["case_id"] for c in result["cases"] if c["classification"] == "stable_failure"]
            lines.append(f"{len(cases)} 个用例在全部尝试中均失败：{', '.join(cases)}")
        elif verdict == verdict_rules.VERDICT_FLAKY:
            cases = [c["case_id"] for c in result["cases"] if c["classification"] == "flaky"]
            lines.append(f"{len(cases)} 个用例同一输入下出现通过与失败两种结果（偶发）：{', '.join(cases)}")
        else:
            lines.append("全部用例在所有尝试中通过")
        if result["repeated_signatures"]:
            repeated = "; ".join(
                f"{item['signature_id']} 命中 {item['case_count']} 个用例"
                for item in result["repeated_signatures"]
            )
            lines.append("重复失败签名：" + repeated)
        return {"summary": " ".join(lines), "lines": lines}

    def search_signature(self, signature: str) -> dict[str, Any]:
        """在所有冻结运行中查找具有相同失败签名的用例，用于缺陷复现聚类。"""

        signature = str(signature).strip()
        if not signature:
            raise ValidationError("signature 不能为空")
        matches: list[dict[str, Any]] = []
        rows = self.database.connection.execute(
            "SELECT run_id, decision_json FROM experiments WHERE status IN (?, ?) "
            "AND decision_json IS NOT NULL ORDER BY frozen_at, run_id",
            (FROZEN, SUPERSEDED),
        ).fetchall()
        for row in rows:
            decision = json.loads(row["decision_json"])
            for case in decision["result"]["cases"]:
                if signature in case.get("failure_signatures", []):
                    matches.append({"run_id": row["run_id"], "case_id": case["case_id"],
                                    "frozen_inputs_digest": decision["frozen_inputs_digest"]})
        return {"signature_id": verdict_rules.signature_id(signature),
                "signature": signature, "matches": matches, "match_count": len(matches)}

    def statistics(self, *, include_superseded: bool = False) -> dict[str, Any]:
        """汇总统计；每个数字都附带其引用的冻结输入摘要。"""

        statuses = (FROZEN, SUPERSEDED) if include_superseded else (FROZEN,)
        placeholders = ", ".join("?" for _ in statuses)
        rows = self.database.connection.execute(
            f"SELECT run_id, verdict, reason_code, frozen_inputs_digest, frozen_at "
            f"FROM experiments WHERE status IN ({placeholders}) ORDER BY frozen_at, run_id",
            statuses,
        ).fetchall()
        by_verdict: dict[str, int] = {verdict_rules.VERDICT_STABLE_FAILURE: 0,
                                      verdict_rules.VERDICT_FLAKY: 0,
                                      verdict_rules.VERDICT_STABLE_PASS: 0,
                                      verdict_rules.VERDICT_INVALID: 0}
        runs: list[dict[str, Any]] = []
        for row in rows:
            by_verdict[row["verdict"]] = by_verdict.get(row["verdict"], 0) + 1
            runs.append({"run_id": row["run_id"], "verdict": row["verdict"],
                         "reason_code": row["reason_code"],
                         "frozen_inputs_digest": row["frozen_inputs_digest"],
                         "frozen_at": row["frozen_at"]})
        return {"frozen_run_count": len(rows), "by_verdict": by_verdict,
                "runs": runs, "include_superseded": include_superseded}

    # ---- 复核流程 -------------------------------------------------------

    def _parse_deadline(self, deadline: str) -> str:
        value = str(deadline).strip()
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValidationError("deadline 必须是 ISO 8601 带时区时间") from exc
        if parsed.tzinfo is None:
            raise ValidationError("deadline 必须包含时区")
        return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    def expire_due_reviews(self) -> list[dict[str, Any]]:
        """把截止时间已过且仍开放的复核标记为过期（幂等，可在重启后调用）。"""

        expired: list[dict[str, Any]] = []
        now = self._now()
        with self.database.transaction(immediate=True) as connection:
            rows = connection.execute(
                "SELECT review_id, run_id FROM reviews WHERE status='open' AND deadline<=?",
                (now,),
            ).fetchall()
            for row in rows:
                connection.execute("UPDATE reviews SET status='expired' WHERE review_id=?", (row["review_id"],))
                append_event(connection, actor_id="system", action="review.expired",
                             resource_type="review", resource_id=row["review_id"],
                             detail={"run_id": row["run_id"]}, occurred_at=now)
                expired.append({"review_id": row["review_id"], "run_id": row["run_id"]})
        return expired

    def open_review(self, *, request_id: str, actor_id: str, run_id: str,
                    deadline: str, note: str) -> Any:
        run_id = self._identifier(run_id, "run_id")
        note = self._text(note, "note", 1000)
        deadline_text = self._parse_deadline(deadline)
        if datetime.fromisoformat(deadline_text.replace("Z", "+00:00")) <= self.clock.now():
            raise ValidationError("复核截止时间必须晚于当前时间")
        payload = {"actor_id": actor_id, "run_id": run_id, "deadline": deadline_text, "note": note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            run = connection.execute("SELECT * FROM experiments WHERE run_id=?", (run_id,)).fetchone()
            if run is None:
                raise NotFoundError("实验运行不存在")
            if run["status"] not in (FROZEN, SUPERSEDED):
                raise ConflictError("只能对已冻结的运行发起复核")

            def create() -> tuple[str, str, dict[str, Any]]:
                existing = connection.execute(
                    "SELECT review_id, status FROM reviews WHERE run_id=?", (run_id,)
                ).fetchone()
                if existing is not None:
                    raise ConflictError(f"该运行已存在复核 {existing['review_id']}（{existing['status']}）")
                review_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO reviews(review_id,run_id,opened_by,opened_at,deadline,note,status) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (review_id, run_id, actor_id, self._now(), deadline_text, note, "open"),
                )
                connection.execute("UPDATE experiments SET review_id=? WHERE run_id=?", (review_id, run_id))
                append_event(connection, actor_id=actor_id, action="review.opened",
                             resource_type="review", resource_id=review_id,
                             detail={"run_id": run_id, "deadline": deadline_text},
                             occurred_at=self._now())
                return "review", review_id, {"review_id": review_id, "run_id": run_id, "status": "open"}

            return self._idempotent(connection, request_id=request_id,
                                    action="open_review", payload=payload, create=create)

    def _load_open_review(self, connection, run_id: str):
        review = connection.execute("SELECT * FROM reviews WHERE run_id=?", (run_id,)).fetchone()
        if review is None:
            raise NotFoundError("复核不存在")
        if review["status"] != "open":
            raise ConflictError(f"复核已 {review['status']}")
        return review

    def _before_deadline(self, review: Any) -> None:
        deadline = datetime.fromisoformat(review["deadline"].replace("Z", "+00:00"))
        if self.clock.now() > deadline:
            raise ConflictError("已超过复核截止时间")

    def submit_supplement(self, *, request_id: str, actor_id: str, review_id: str,
                          content: str) -> Any:
        review_id = self._identifier(review_id, "review_id")
        content = self._text(content, "content", 4000)
        payload = {"actor_id": actor_id, "review_id": review_id, "content": content}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "student")
            review = connection.execute("SELECT * FROM reviews WHERE review_id=?", (review_id,)).fetchone()
            if review is None:
                raise NotFoundError("复核不存在")
            if review["status"] != "open":
                raise ConflictError(f"复核已 {review['status']}")
            self._before_deadline(review)
            run = connection.execute(
                "SELECT * FROM experiments WHERE run_id=?", (review["run_id"],)
            ).fetchone()
            site = connection.execute(
                "SELECT organization_id FROM sites WHERE site_id=?", (run["site_id"],)
            ).fetchone()
            if actor.organization_id != site["organization_id"]:
                raise PermissionDenied("只能为本组织的实验提交补充说明")

            def create() -> tuple[str, str, dict[str, Any]]:
                count = connection.execute(
                    "SELECT COUNT(*) AS count FROM review_supplements WHERE review_id=?",
                    (review_id,),
                ).fetchone()["count"]
                if count:
                    raise ConflictError("该复核已经收到补充说明，只能提交一次")
                try:
                    connection.execute(
                        "INSERT INTO review_supplements(review_id,submitted_by,content,submitted_at) "
                        "VALUES(?,?,?,?)",
                        (review_id, actor_id, content, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("你已经提交过补充说明") from exc
                append_event(connection, actor_id=actor_id, action="review.supplement_submitted",
                             resource_type="review", resource_id=review_id,
                             detail={"run_id": review["run_id"]}, occurred_at=self._now())
                return "review_supplement", review_id, {"review_id": review_id, "submitted": True}

            return self._idempotent(connection, request_id=request_id,
                                    action="submit_supplement", payload=payload, create=create)

    def decide_review(self, *, request_id: str, actor_id: str, review_id: str,
                      decision: str, decision_note: str = "") -> Any:
        review_id = self._identifier(review_id, "review_id")
        decision_note = str(decision_note or "").strip()
        if len(decision_note) > 2000:
            raise ValidationError("decision_note 不能超过 2000 个字符")
        if decision not in ("accept", "rerun"):
            raise ValidationError("decision 必须是 accept 或 rerun")
        payload = {"actor_id": actor_id, "review_id": review_id, "decision": decision,
                   "decision_note": decision_note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "reviewer")
            review = connection.execute("SELECT * FROM reviews WHERE review_id=?", (review_id,)).fetchone()
            if review is None:
                raise NotFoundError("复核不存在")
            if review["status"] != "open":
                raise ConflictError(f"复核已 {review['status']}")
            run = connection.execute(
                "SELECT * FROM experiments WHERE run_id=?", (review["run_id"],)
            ).fetchone()

            def create() -> tuple[str, str, dict[str, Any]]:
                decided_at = self._now()
                if decision == "accept":
                    connection.execute(
                        "UPDATE reviews SET status=?, decided_by=?, decided_at=?, decision_note=? "
                        "WHERE review_id=?",
                        ("accepted", actor_id, decided_at, decision_note, review_id),
                    )
                    append_event(connection, actor_id=actor_id, action="review.decided",
                                 resource_type="review", resource_id=review_id,
                                 detail={"run_id": run["run_id"], "decision": "accept",
                                         "frozen_inputs_digest": run["frozen_inputs_digest"]},
                                 occurred_at=decided_at)
                    return "review", review_id, {"review_id": review_id, "decision": "accept"}

                new_run_id = f"{run['run_id']}-r{uuid.uuid4().hex[:8]}"
                connection.execute(
                    "INSERT INTO experiments(run_id,site_id,build_id,package_id,environment_id,"
                    "expected_shards,status,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (new_run_id, run["site_id"], run["build_id"], run["package_id"],
                     run["environment_id"], run["expected_shards"], COLLECTING,
                     run["created_by"], decided_at),
                )
                connection.execute(
                    "UPDATE experiments SET status=?, superseded_by_run_id=? WHERE run_id=?",
                    (SUPERSEDED, new_run_id, run["run_id"]),
                )
                connection.execute(
                    "UPDATE reviews SET status=?, decided_by=?, decided_at=?, decision_note=?, "
                    "rerun_run_id=? WHERE review_id=?",
                    ("rerun", actor_id, decided_at, decision_note, new_run_id, review_id),
                )
                append_event(connection, actor_id=actor_id, action="review.decided",
                             resource_type="review", resource_id=review_id,
                             detail={"run_id": run["run_id"], "decision": "rerun",
                                     "rerun_run_id": new_run_id},
                             occurred_at=decided_at)
                return "review", review_id, {"review_id": review_id, "decision": "rerun",
                                             "rerun_run_id": new_run_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="decide_review", payload=payload, create=create)

    def get_review(self, review_id: str) -> dict[str, Any]:
        row = self.database.connection.execute(
            "SELECT * FROM reviews WHERE review_id=?", (review_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("复核不存在")
        supplements = [
            {"submitted_by": item["submitted_by"], "content": item["content"],
             "submitted_at": item["submitted_at"]}
            for item in self.database.connection.execute(
                "SELECT * FROM review_supplements WHERE review_id=? ORDER BY submitted_at",
                (review_id,),
            )
        ]
        return {
            "review_id": row["review_id"], "run_id": row["run_id"],
            "opened_by": row["opened_by"], "opened_at": row["opened_at"],
            "deadline": row["deadline"], "note": row["note"], "status": row["status"],
            "decided_by": row["decided_by"], "decided_at": row["decided_at"],
            "decision_note": row["decision_note"], "rerun_run_id": row["rerun_run_id"],
            "supplements": supplements,
        }
