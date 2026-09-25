"""软件测试实验运行与缺陷复现领域服务。

负责不可变输入登记（程序构建摘要、用例包版本、运行环境声明）、
分片结果的乱序接收与齐套冻结、稳定失败/偶发/无效判定、
带截止时间的复核流程，以及只引用冻结输入的统计与判定重放。
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from typing import Any, Callable

from .audit import append_event, canonical_json, digest
from .clock import Clock, SystemClock
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .models import Actor
from .storage import Database


_HEX_RUN = re.compile(r"0x[0-9a-fA-F]+")
_NUMBER_RUN = re.compile(r"\d+")
_WHITESPACE_RUN = re.compile(r"\s+")

OUTCOMES = frozenset({"passed", "failed", "errored"})
FAIL_OUTCOMES = frozenset({"failed", "errored"})
CONCLUSIONS = ("pass", "stable_fail", "flaky", "invalid")
WRITE_ROLES = frozenset({"admin", "operator", "teacher"})

IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")


def normalize_signature(failure_type: str, message: str) -> str:
    """把失败信息归一化为可跨运行重复识别的签名。

    去除地址、数字与空白差异，使同一缺陷在不同尝试中产生相同签名。
    """

    text = str(message).lower().strip()
    text = _HEX_RUN.sub("0x?", text)
    text = _NUMBER_RUN.sub("#", text)
    text = _WHITESPACE_RUN.sub(" ", text).strip()[-200:]
    material = str(failure_type).strip().lower() + "|" + text
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def parse_instant(value: str, field: str) -> str:
    """校验并归一化 ISO-8601 时间字符串。"""

    try:
        from datetime import datetime
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{field} 必须是 ISO-8601 时间") from exc
    if parsed.tzinfo is None:
        raise ValidationError(f"{field} 必须包含时区")
    return _to_utc(parsed)


def _to_utc(parsed) -> str:
    from datetime import timezone
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class ExperimentService:
    """协调实验登记、分片合并、判定、复核与统计。"""

    def __init__(self, database: Database, clock: Clock | None = None) -> None:
        self.database = database
        self.clock = clock or SystemClock()

    # ----- 基础工具 -------------------------------------------------

    def _now(self) -> str:
        return self.clock.now().isoformat().replace("+00:00", "Z")

    def _now_dt(self):
        return self.clock.now()

    def _identifier(self, value: str, field: str) -> str:
        value = str(value).strip()
        if not IDENTIFIER.fullmatch(value):
            raise ValidationError(f"{field} 格式无效")
        return value

    def _digest_field(self, value: str, field: str) -> str:
        value = str(value).strip().lower()
        if not SHA256.fullmatch(value):
            raise ValidationError(f"{field} 必须是 64 位十六进制 SHA-256 摘要")
        return value

    def _actor(self, connection, actor_id: str) -> Actor:
        row = connection.execute("SELECT * FROM actors WHERE actor_id=?", (actor_id,)).fetchone()
        if row is None:
            raise NotFoundError("操作者不存在")
        actor = Actor(row["actor_id"], row["display_name"], row["role"],
                      row["organization_id"], bool(row["active"]))
        if not actor.active:
            raise PermissionDenied("操作者已停用")
        return actor

    def _idempotent(self, connection, *, request_id: str, action: str,
                    payload: dict[str, Any],
                    create: Callable[[], tuple[str, str, dict[str, Any]]]) -> dict[str, Any]:
        request_id = self._identifier(request_id, "request_id")
        payload_hash = digest(payload)
        row = connection.execute(
            "SELECT * FROM request_receipts WHERE request_id=?", (request_id,)
        ).fetchone()
        if row:
            if row["action"] != action or row["payload_hash"] != payload_hash:
                raise ConflictError("request_id 已被不同内容使用")
            return {**json.loads(row["response_json"]), "request_id": request_id,
                    "resource_type": row["resource_type"], "resource_id": row["resource_id"],
                    "replayed": True}
        resource_type, resource_id, response = create()
        connection.execute(
            "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,"
            "resource_id,response_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (request_id, action, payload_hash, resource_type, resource_id,
             canonical_json(response), self._now()),
        )
        return {**response, "request_id": request_id, "resource_type": resource_type,
                "resource_id": resource_id, "replayed": False}

    # ----- 不可变输入登记 -------------------------------------------

    def register_build(self, *, request_id: str, actor_id: str, build_id: str,
                       program_name: str, build_digest: str,
                       manifest: dict[str, Any]) -> dict[str, Any]:
        """登记不可变程序构建摘要。manifest 可含文件清单、构建参数等。"""

        if not isinstance(manifest, dict) or not manifest:
            raise ValidationError("manifest 必须是非空对象")
        payload = {"actor_id": actor_id, "build_id": build_id, "program_name": program_name,
                   "build_digest": build_digest, "manifest": manifest}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            if actor.role not in WRITE_ROLES:
                raise PermissionDenied("当前角色不能登记程序构建")
            build_id = self._identifier(build_id, "build_id")
            program_name = self._text(program_name, "program_name", 120)
            build_digest = self._digest_field(build_digest, "build_digest")

            def create() -> tuple[str, str, dict[str, Any]]:
                existing = connection.execute(
                    "SELECT digest FROM builds WHERE build_id=?", (build_id,)
                ).fetchone()
                if existing:
                    if existing["digest"] != build_digest:
                        raise ConflictError("构建编号已经绑定不同摘要，构建不可变")
                    return "build", build_id, {"build_id": build_id, "replayed_business_key": True}
                try:
                    connection.execute(
                        "INSERT INTO builds(build_id,program_name,digest,manifest_json,created_by,created_at) "
                        "VALUES(?,?,?,?,?,?)",
                        (build_id, program_name, build_digest, canonical_json(manifest),
                         actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("同一程序名与摘要已经登记") from exc
                append_event(connection, actor_id=actor_id, action="build.registered",
                             resource_type="build", resource_id=build_id,
                             detail={"program_name": program_name, "digest": build_digest},
                             occurred_at=self._now())
                return "build", build_id, {"build_id": build_id}

            return self._idempotent(connection, request_id=request_id, action="register_build",
                                    payload=payload, create=create)

    def register_case_package(self, *, request_id: str, actor_id: str, package_id: str,
                              version: str, package_digest: str,
                              case_ids: list[str]) -> dict[str, Any]:
        """登记不可变用例包版本，case_ids 为该版本覆盖的完整用例集合。"""

        if not isinstance(case_ids, list) or not case_ids:
            raise ValidationError("case_ids 必须是非空数组")
        if any(not isinstance(c, str) or not IDENTIFIER.fullmatch(c.strip()) for c in case_ids):
            raise ValidationError("case_ids 中存在无效用例编号")
        if len(set(case_ids)) != len(case_ids):
            raise ValidationError("case_ids 不能重复")
        manifest = {"case_ids": list(case_ids)}
        payload = {"actor_id": actor_id, "package_id": package_id, "version": version,
                   "package_digest": package_digest, "case_ids": case_ids}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            if actor.role not in WRITE_ROLES:
                raise PermissionDenied("当前角色不能登记用例包")
            package_id = self._identifier(package_id, "package_id")
            version = self._identifier(version, "version")
            package_digest = self._digest_field(package_digest, "package_digest")

            def create() -> tuple[str, str, dict[str, Any]]:
                existing = connection.execute(
                    "SELECT digest FROM case_packages WHERE package_id=? AND version=?",
                    (package_id, version),
                ).fetchone()
                if existing:
                    if existing["digest"] != package_digest:
                        raise ConflictError("用例包版本已经绑定不同摘要，版本不可变")
                    return "case_package", f"{package_id}@{version}", {
                        "package_id": package_id, "version": version,
                        "replayed_business_key": True}
                try:
                    connection.execute(
                        "INSERT INTO case_packages(package_id,version,digest,manifest_json,created_by,created_at) "
                        "VALUES(?,?,?,?,?,?)",
                        (package_id, version, package_digest, canonical_json(manifest),
                         actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("用例包版本写入冲突") from exc
                append_event(connection, actor_id=actor_id, action="case_package.registered",
                             resource_type="case_package", resource_id=f"{package_id}@{version}",
                             detail={"package_id": package_id, "version": version,
                                     "digest": package_digest, "case_count": len(case_ids)},
                             occurred_at=self._now())
                return "case_package", f"{package_id}@{version}", {
                    "package_id": package_id, "version": version}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_case_package", payload=payload, create=create)

    def register_environment(self, *, request_id: str, actor_id: str, environment_id: str,
                             declaration: dict[str, Any]) -> dict[str, Any]:
        """登记运行环境声明；摘要由声明内容派生，保证声明与摘要一致。"""

        if not isinstance(declaration, dict) or not declaration:
            raise ValidationError("declaration 必须是非空对象")
        environment_digest = digest(declaration)
        payload = {"actor_id": actor_id, "environment_id": environment_id,
                   "environment_digest": environment_digest, "declaration": declaration}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            if actor.role not in WRITE_ROLES:
                raise PermissionDenied("当前角色不能登记运行环境")
            environment_id = self._identifier(environment_id, "environment_id")

            def create() -> tuple[str, str, dict[str, Any]]:
                existing = connection.execute(
                    "SELECT digest FROM environments WHERE environment_id=?", (environment_id,)
                ).fetchone()
                if existing:
                    if existing["digest"] != environment_digest:
                        raise ConflictError("环境编号已经绑定不同声明，环境不可变")
                    return "environment", environment_id, {
                        "environment_id": environment_id, "replayed_business_key": True}
                try:
                    connection.execute(
                        "INSERT INTO environments(environment_id,digest,declaration_json,created_by,created_at) "
                        "VALUES(?,?,?,?,?)",
                        (environment_id, environment_digest, canonical_json(declaration),
                         actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("相同环境声明已经以其他编号登记") from exc
                append_event(connection, actor_id=actor_id, action="environment.registered",
                             resource_type="environment", resource_id=environment_id,
                             detail={"digest": environment_digest}, occurred_at=self._now())
                return "environment", environment_id, {"environment_id": environment_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_environment", payload=payload, create=create)

    def register_experiment(self, *, request_id: str, actor_id: str, experiment_id: str,
                            build_id: str, package_id: str, package_version: str,
                            environment_id: str) -> dict[str, Any]:
        """把不可变三元组组合成一个可重复实验。"""

        payload = {"actor_id": actor_id, "experiment_id": experiment_id, "build_id": build_id,
                   "package_id": package_id, "package_version": package_version,
                   "environment_id": environment_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            if actor.role not in WRITE_ROLES:
                raise PermissionDenied("当前角色不能登记实验")
            experiment_id = self._identifier(experiment_id, "experiment_id")
            build_id = self._identifier(build_id, "build_id")
            package_id = self._identifier(package_id, "package_id")
            package_version = self._identifier(package_version, "package_version")
            environment_id = self._identifier(environment_id, "environment_id")

            def create() -> tuple[str, str, dict[str, Any]]:
                refs = {
                    "build": connection.execute("SELECT 1 FROM builds WHERE build_id=?", (build_id,)).fetchone(),
                    "package": connection.execute(
                        "SELECT 1 FROM case_packages WHERE package_id=? AND version=?",
                        (package_id, package_version)).fetchone(),
                    "environment": connection.execute(
                        "SELECT 1 FROM environments WHERE environment_id=?", (environment_id,)).fetchone(),
                }
                missing = [name for name, row in refs.items() if row is None]
                if missing:
                    raise NotFoundError(f"引用对象不存在: {','.join(missing)}")
                existing = connection.execute(
                    "SELECT experiment_id FROM experiments WHERE build_id=? AND package_id=? "
                    "AND package_version=? AND environment_id=?",
                    (build_id, package_id, package_version, environment_id),
                ).fetchone()
                if existing:
                    if existing["experiment_id"] != experiment_id:
                        raise ConflictError("相同输入三元组已经登记为其他实验编号")
                    return "experiment", experiment_id, {
                        "experiment_id": experiment_id, "replayed_business_key": True}
                try:
                    connection.execute(
                        "INSERT INTO experiments(experiment_id,build_id,package_id,package_version,"
                        "environment_id,created_at) VALUES(?,?,?,?,?,?)",
                        (experiment_id, build_id, package_id, package_version,
                         environment_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("实验编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="experiment.registered",
                             resource_type="experiment", resource_id=experiment_id,
                             detail={"build_id": build_id, "package_id": package_id,
                                     "package_version": package_version,
                                     "environment_id": environment_id},
                             occurred_at=self._now())
                return "experiment", experiment_id, {"experiment_id": experiment_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_experiment", payload=payload, create=create)

    # ----- 运行与分片 ------------------------------------------------

    def open_run(self, *, request_id: str, actor_id: str, experiment_id: str,
                 expected_shards: int) -> dict[str, Any]:
        """为实验开启一次新尝试，尝试序号按既有最大值递增。"""

        payload = {"actor_id": actor_id, "experiment_id": experiment_id,
                   "expected_shards": expected_shards}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            if actor.role not in WRITE_ROLES:
                raise PermissionDenied("当前角色不能开启实验运行")
            experiment_id = self._identifier(experiment_id, "experiment_id")
            if not isinstance(expected_shards, int) or expected_shards < 1:
                raise ValidationError("expected_shards 必须是不小于 1 的整数")
            if connection.execute("SELECT 1 FROM experiments WHERE experiment_id=?",
                                  (experiment_id,)).fetchone() is None:
                raise NotFoundError("实验不存在")

            def create() -> tuple[str, str, dict[str, Any]]:
                row = connection.execute(
                    "SELECT COALESCE(MAX(attempt),0) AS max_attempt FROM runs WHERE experiment_id=?",
                    (experiment_id,)).fetchone()
                attempt = row["max_attempt"] + 1
                run_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO runs(run_id,experiment_id,attempt,expected_shards,status,"
                    "invalid_reasons_json,opened_by,opened_at) VALUES(?,?,?,?,'collecting','[]',?,?)",
                    (run_id, experiment_id, attempt, expected_shards, actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="run.opened",
                             resource_type="run", resource_id=run_id,
                             detail={"experiment_id": experiment_id, "attempt": attempt,
                                     "expected_shards": expected_shards},
                             occurred_at=self._now())
                return "run", run_id, {"run_id": run_id, "experiment_id": experiment_id,
                                       "attempt": attempt, "expected_shards": expected_shards}

            return self._idempotent(connection, request_id=request_id, action="open_run",
                                    payload=payload, create=create)

    def upload_shard(self, *, request_id: str, actor_id: str, run_id: str, shard_index: int,
                     content_hash: str, build_digest: str, environment_digest: str,
                     cases: list[dict[str, Any]],
                     coverage_digest: str) -> dict[str, Any]:
        """接收一个结果分片。

        - 同运行编号下分片可乱序到达；
        - content_hash 必须与分片内容自证绑定；
        - 相同 (run_id, shard_index) 重传同内容幂等，异内容冲突且绝不覆盖；
        - 最后一片齐套时在同一事务内冻结结论。
        """

        if not isinstance(cases, list) or not cases:
            raise ValidationError("cases 必须是非空数组")
        shard_content = {"build_digest": build_digest, "environment_digest": environment_digest,
                         "coverage_digest": coverage_digest, "cases": cases}
        payload = {"actor_id": actor_id, "run_id": run_id, "shard_index": shard_index,
                   "content_hash": content_hash, "shard_content": shard_content}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            run = connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if run is None:
                raise NotFoundError("运行不存在")
            if not isinstance(shard_index, int) or shard_index < 0:
                raise ValidationError("shard_index 必须是非负整数")
            if actor.role not in WRITE_ROLES and actor_id != run["opened_by"]:
                raise PermissionDenied("只有开启者或教师可以上传分片")
            if run["status"] != "collecting":
                # 冻结后重传：同 request_id 同内容回放原回执，异内容拒绝。
                request_id = self._identifier(request_id, "request_id")
                receipt_row = connection.execute(
                    "SELECT * FROM request_receipts WHERE request_id=?", (request_id,)
                ).fetchone()
                if receipt_row and receipt_row["action"] == "upload_shard" \
                        and receipt_row["payload_hash"] == digest(payload):
                    return {**json.loads(receipt_row["response_json"]),
                            "request_id": request_id,
                            "resource_type": receipt_row["resource_type"],
                            "resource_id": receipt_row["resource_id"], "replayed": True}
                raise ConflictError("运行已经冻结，不能再接收分片")
            if shard_index >= run["expected_shards"]:
                raise ValidationError("shard_index 超出声明的分片范围")
            content_hash = self._digest_field(content_hash, "content_hash")
            build_digest = self._digest_field(build_digest, "build_digest")
            environment_digest = self._digest_field(environment_digest, "environment_digest")
            coverage_digest = self._digest_field(coverage_digest, "coverage_digest")
            if digest(shard_content) != content_hash:
                raise ValidationError("分片内容与 content_hash 不一致")
            experiment = connection.execute(
                "SELECT * FROM experiments WHERE experiment_id=?", (run["experiment_id"],)
            ).fetchone()
            build = connection.execute("SELECT digest FROM builds WHERE build_id=?",
                                       (experiment["build_id"],)).fetchone()
            environment = connection.execute(
                "SELECT digest FROM environments WHERE environment_id=?",
                (experiment["environment_id"],)).fetchone()
            if build["digest"] != build_digest:
                raise ConflictError("分片的程序构建摘要与实验登记输入不一致")
            if environment["digest"] != environment_digest:
                raise ConflictError("分片的运行环境摘要与实验登记输入不一致")
            for case in cases:
                self._validate_case_shape(case)

            def create() -> tuple[str, str, dict[str, Any]]:
                existing = connection.execute(
                    "SELECT content_hash FROM shards WHERE run_id=? AND shard_index=?",
                    (run_id, shard_index),
                ).fetchone()
                duplicate = False
                if existing:
                    # 自然键冲突：同内容幂等，异内容拒绝，保证不可覆盖。
                    if existing["content_hash"] != content_hash:
                        raise ConflictError("同一分片编号已经存在不同内容，禁止覆盖")
                    duplicate = True
                else:
                    connection.execute(
                        "INSERT INTO shards(run_id,shard_index,content_hash,build_digest,"
                        "environment_digest,payload_json,uploaded_by,received_at) "
                        "VALUES(?,?,?,?,?,?,?,?)",
                        (run_id, shard_index, content_hash, build_digest, environment_digest,
                         canonical_json(shard_content), actor_id, self._now()),
                    )
                    append_event(connection, actor_id=actor_id, action="shard.uploaded",
                                 resource_type="shard", resource_id=f"{run_id}:{shard_index}",
                                 detail={"run_id": run_id, "shard_index": shard_index,
                                         "content_hash": content_hash, "case_count": len(cases)},
                                 occurred_at=self._now())
                count = connection.execute(
                    "SELECT COUNT(*) AS count FROM shards WHERE run_id=?", (run_id,)
                ).fetchone()["count"]
                response: dict[str, Any] = {
                    "run_id": run_id, "shard_index": shard_index, "duplicate": duplicate,
                    "received_shards": count, "expected_shards": run["expected_shards"],
                    "frozen": False}
                if count == run["expected_shards"]:
                    replay = self._freeze_run(connection, run)
                    response["frozen"] = True
                    response["conclusion"] = replay["conclusion"]
                return "shard", f"{run_id}:{shard_index}", response

            return self._idempotent(connection, request_id=request_id, action="upload_shard",
                                    payload=payload, create=create)

    def _validate_case_shape(self, case: dict[str, Any]) -> None:
        if not isinstance(case, dict):
            raise ValidationError("用例结果必须是对象")
        case_id = str(case.get("case_id", "")).strip()
        if not IDENTIFIER.fullmatch(case_id):
            raise ValidationError("用例编号格式无效")
        outcome = case.get("outcome")
        if outcome not in OUTCOMES:
            raise ValidationError(f"用例 {case_id} 的 outcome 非法")
        if outcome in FAIL_OUTCOMES:
            failure = case.get("failure")
            if not isinstance(failure, dict):
                raise ValidationError(f"用例 {case_id} 失败时必须提供 failure")
            if not self._text(failure.get("type", ""), "failure.type", 120, optional=True) \
                    or not self._text(failure.get("message", ""), "failure.message", 2000, optional=True):
                raise ValidationError(f"用例 {case_id} 的 failure.type/message 不能为空")
            if not SHA256.fullmatch(str(case.get("log_digest", "")).strip()):
                raise ValidationError(f"用例 {case_id} 失败时必须提供日志摘要 log_digest")

    def _text(self, value: str, field: str, limit: int = 2000, optional: bool = False) -> str:
        value = str(value or "").strip()
        if not value:
            if optional:
                return value
            raise ValidationError(f"{field} 不能为空且不能超过 {limit} 个字符")
        if len(value) > limit:
            raise ValidationError(f"{field} 不能超过 {limit} 个字符")
        return value

    def _load_package_case_ids(self, connection, experiment) -> list[str]:
        row = connection.execute(
            "SELECT manifest_json FROM case_packages WHERE package_id=? AND version=?",
            (experiment["package_id"], experiment["package_version"]),
        ).fetchone()
        return json.loads(row["manifest_json"])["case_ids"]

    def _freeze_run(self, connection, run) -> dict[str, Any]:
        """齐套后在当前事务内完成校验、判定与冻结。"""

        run_id = run["run_id"]
        experiment = connection.execute(
            "SELECT * FROM experiments WHERE experiment_id=?", (run["experiment_id"],)
        ).fetchone()
        build = connection.execute("SELECT * FROM builds WHERE build_id=?",
                                   (experiment["build_id"],)).fetchone()
        package = connection.execute(
            "SELECT * FROM case_packages WHERE package_id=? AND version=?",
            (experiment["package_id"], experiment["package_version"]),
        ).fetchone()
        environment = connection.execute(
            "SELECT * FROM environments WHERE environment_id=?",
            (experiment["environment_id"],),
        ).fetchone()
        shard_rows = connection.execute(
            "SELECT * FROM shards WHERE run_id=? ORDER BY shard_index", (run_id,)).fetchall()

        shard_digests: list[dict[str, Any]] = []
        cases: list[dict[str, Any]] = []
        invalid_reasons: list[dict[str, Any]] = []
        seen_case_ids: set[str] = set()
        for shard in shard_rows:
            content = json.loads(shard["payload_json"])
            if digest(content) != shard["content_hash"]:
                raise ConflictError(f"分片 {shard['shard_index']} 内容与登记摘要不符，拒绝冻结")
            shard_digests.append({"shard_index": shard["shard_index"],
                                  "content_hash": shard["content_hash"],
                                  "coverage_digest": content["coverage_digest"]})
            if not content.get("coverage_digest"):
                invalid_reasons.append({"reason": "missing_coverage",
                                        "shard_index": shard["shard_index"]})
            for position, case in enumerate(content["cases"]):
                case_id = case["case_id"]
                if case_id in seen_case_ids:
                    invalid_reasons.append({"reason": "duplicate_case", "case_id": case_id})
                seen_case_ids.add(case_id)
                cases.append({"case_id": case_id, "outcome": case["outcome"],
                              "shard_index": shard["shard_index"], "position": position,
                              "failure": case.get("failure"),
                              "log_digest": case.get("log_digest"),
                              "coverage_digest": case.get("coverage_digest")})

        expected_cases = self._load_package_case_ids(connection, experiment)
        for case_id in expected_cases:
            if case_id not in seen_case_ids:
                invalid_reasons.append({"reason": "missing_case", "case_id": case_id})
        for case_id in seen_case_ids:
            if case_id not in set(expected_cases):
                invalid_reasons.append({"reason": "unexpected_case", "case_id": case_id})
        for case in cases:
            if case["outcome"] in FAIL_OUTCOMES:
                failure = case["failure"] or {}
                if not failure.get("type") or not failure.get("message"):
                    invalid_reasons.append({"reason": "missing_failure_detail",
                                            "case_id": case["case_id"]})
                if not case.get("log_digest"):
                    invalid_reasons.append({"reason": "missing_log",
                                            "case_id": case["case_id"]})

        # 落冻结用例与失败签名出现记录；重复用例只保留首条（重复已记入无效原因）。
        connection.execute("DELETE FROM frozen_cases WHERE run_id=?", (run_id,))
        connection.execute("DELETE FROM signature_occurrences WHERE run_id=?", (run_id,))
        failed_now: list[dict[str, Any]] = []
        inserted_case_ids: set[str] = set()
        for case in cases:
            signature = None
            if case["outcome"] in FAIL_OUTCOMES and case["failure"]:
                signature = normalize_signature(case["failure"]["type"],
                                                case["failure"]["message"])
                if case["case_id"] not in inserted_case_ids:
                    connection.execute(
                        "INSERT INTO signature_occurrences(signature,run_id,attempt,case_id,"
                        "failure_type) VALUES(?,?,?,?,?)",
                        (signature, run_id, run["attempt"], case["case_id"],
                         case["failure"]["type"]),
                    )
            if case["case_id"] not in inserted_case_ids:
                connection.execute(
                    "INSERT INTO frozen_cases(run_id,case_id,outcome,failure_signature,log_digest,"
                    "coverage_digest,evidence_json) VALUES(?,?,?,?,?,?,?)",
                    (run_id, case["case_id"], case["outcome"], signature,
                     case.get("log_digest"), case.get("coverage_digest"),
                     canonical_json({"shard_index": case["shard_index"], "position": case["position"],
                                     "failure": case["failure"]})),
                )
                inserted_case_ids.add(case["case_id"])
            if case["outcome"] in FAIL_OUTCOMES:
                failed_now.append({"case_id": case["case_id"], "signature": signature,
                                   "failure_type": (case["failure"] or {}).get("type")})

        prior_runs = connection.execute(
            "SELECT run_id,attempt,conclusion FROM runs WHERE experiment_id=? AND status='frozen' "
            "AND attempt<? ORDER BY attempt",
            (run["experiment_id"], run["attempt"]),
        ).fetchall()
        prior = [dict(row) for row in prior_runs]
        conclusion, evidence = self._judge(connection, run, prior, failed_now, invalid_reasons)

        inputs_material = {
            "build_id": experiment["build_id"], "build_digest": build["digest"],
            "package_id": experiment["package_id"], "package_version": experiment["package_version"],
            "package_digest": package["digest"],
            "environment_id": experiment["environment_id"],
            "environment_digest": environment["digest"],
            "shards": shard_digests,
        }
        inputs_hash = digest(inputs_material)
        connection.execute(
            "UPDATE runs SET status='frozen',conclusion=?,invalid_reasons_json=?,inputs_hash=?,"
            "frozen_at=? WHERE run_id=?",
            (conclusion, canonical_json(invalid_reasons), inputs_hash, self._now(), run_id),
        )
        append_event(connection, actor_id=run["opened_by"], action="run.frozen",
                     resource_type="run", resource_id=run_id,
                     detail={"experiment_id": run["experiment_id"], "attempt": run["attempt"],
                             "conclusion": conclusion, "inputs_hash": inputs_hash,
                             "invalid_reason_count": len(invalid_reasons),
                             "case_count": len(cases), "failed_count": len(failed_now)},
                     occurred_at=self._now())
        return self._build_replay(connection, run_id, inputs_material=inputs_material,
                                  inputs_hash=inputs_hash, invalid_reasons=invalid_reasons,
                                  conclusion=conclusion, evidence=evidence, prior=prior,
                                  shard_digests=shard_digests)

    def _judge(self, connection, run, prior: list[dict[str, Any]],
               failed_now: list[dict[str, Any]],
               invalid_reasons: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
        """根据冻结输入与历史冻结尝试判定结论，evidence 记录每条触发依据。"""

        evidence: list[dict[str, Any]] = []
        if invalid_reasons:
            evidence.append({"rule": "invalid_inputs",
                             "detail": f"{len(invalid_reasons)} 条无效输入原因",
                             "reasons": invalid_reasons})
            return "invalid", evidence

        if not failed_now:
            evidence.append({"rule": "all_cases_passed",
                             "detail": "本次全部用例通过"})
            return "pass", evidence

        prior_run_ids = [row["run_id"] for row in prior]
        prior_placeholders = ",".join("?" for _ in prior_run_ids)
        prior_signatures: set[str] = set()
        prior_passed_cases: set[str] = set()
        if prior_run_ids:
            for row in connection.execute(
                f"SELECT DISTINCT signature FROM signature_occurrences WHERE run_id IN "
                f"({prior_placeholders})", prior_run_ids):
                prior_signatures.add(row["signature"])
            for row in connection.execute(
                f"SELECT DISTINCT case_id FROM frozen_cases WHERE run_id IN "
                f"({prior_placeholders}) AND outcome='passed'", prior_run_ids):
                prior_passed_cases.add(row["case_id"])

        flaky_cases = [item for item in failed_now if item["case_id"] in prior_passed_cases]
        recurring = [item for item in failed_now if item["signature"] in prior_signatures]
        evidence.append({
            "rule": "cross_attempt_comparison",
            "detail": "与同实验此前冻结尝试逐用例比对",
            "prior_attempts": [{"run_id": row["run_id"], "attempt": row["attempt"],
                                "conclusion": row["conclusion"]} for row in prior],
            "failed_now": failed_now,
            "recurring_signatures": recurring,
            "flaky_cases": flaky_cases,
        })

        if flaky_cases:
            evidence.append({"rule": "flaky_passed_before",
                             "detail": "本次失败用例在此前尝试中曾经通过，结果跨尝试不一致",
                             "case_ids": [item["case_id"] for item in flaky_cases]})
            return "flaky", evidence
        if recurring:
            evidence.append({"rule": "repeated_failure_signature",
                             "detail": "失败签名在此前冻结尝试中重复出现",
                             "signatures": sorted({item["signature"] for item in recurring}),
                             "case_ids": [item["case_id"] for item in recurring]})
            return "stable_fail", evidence
        if prior:
            evidence.append({"rule": "failure_not_reproduced",
                             "detail": "本次失败签名均未在此前冻结尝试中出现，按偶发失败处理"})
            return "flaky", evidence
        evidence.append({"rule": "first_observation_failure",
                         "detail": "首次尝试即失败且无历史尝试，按稳定失败候选登记，等待复核重跑确认"})
        return "stable_fail", evidence

    def recover_interrupted(self) -> list[dict[str, Any]]:
        """中断重启后继续未完成的分片合并：冻结所有已齐套但仍在收集的运行。"""

        recovered: list[dict[str, Any]] = []
        with self.database.transaction(immediate=True) as connection:
            rows = connection.execute(
                "SELECT r.* FROM runs r WHERE r.status='collecting' AND "
                "(SELECT COUNT(*) FROM shards s WHERE s.run_id=r.run_id)=r.expected_shards"
            ).fetchall()
            for row in rows:
                replay = self._freeze_run(connection, row)
                recovered.append({"run_id": row["run_id"], "conclusion": replay["conclusion"]})
        return recovered

    # ----- 重放 ------------------------------------------------------

    def get_run(self, run_id: str, actor_id: str | None = None) -> dict[str, Any]:
        """重放某次运行为何判定为稳定失败、偶发或无效。"""

        with self.database.transaction() as connection:
            if actor_id is not None:
                self._actor(connection, actor_id)
            return self._replay(connection, run_id)

    def _replay(self, connection, run_id: str) -> dict[str, Any]:
        run = connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if run is None:
            raise NotFoundError("运行不存在")
        experiment = connection.execute(
            "SELECT * FROM experiments WHERE experiment_id=?", (run["experiment_id"],)
        ).fetchone()
        build = connection.execute("SELECT * FROM builds WHERE build_id=?",
                                   (experiment["build_id"],)).fetchone()
        package = connection.execute(
            "SELECT * FROM case_packages WHERE package_id=? AND version=?",
            (experiment["package_id"], experiment["package_version"]),
        ).fetchone()
        environment = connection.execute(
            "SELECT * FROM environments WHERE environment_id=?",
            (experiment["environment_id"],),
        ).fetchone()
        shard_rows = connection.execute(
            "SELECT * FROM shards WHERE run_id=? ORDER BY shard_index", (run_id,)).fetchall()
        shard_digests = [{"shard_index": row["shard_index"], "content_hash": row["content_hash"],
                          "coverage_digest": json.loads(row["payload_json"])["coverage_digest"]}
                         for row in shard_rows]
        inputs_material = {
            "build_id": experiment["build_id"], "build_digest": build["digest"],
            "package_id": experiment["package_id"], "package_version": experiment["package_version"],
            "package_digest": package["digest"],
            "environment_id": experiment["environment_id"],
            "environment_digest": environment["digest"],
            "shards": shard_digests,
        }
        recomputed_inputs_hash = digest(inputs_material)
        frozen_cases = []
        failed_now: list[dict[str, Any]] = []
        for row in connection.execute(
                "SELECT * FROM frozen_cases WHERE run_id=? ORDER BY case_id", (run_id,)):
            item = {"case_id": row["case_id"], "outcome": row["outcome"],
                    "failure_signature": row["failure_signature"],
                    "log_digest": row["log_digest"], "coverage_digest": row["coverage_digest"],
                    "evidence": json.loads(row["evidence_json"])}
            frozen_cases.append(item)
            if row["outcome"] in FAIL_OUTCOMES:
                failed_now.append({"case_id": row["case_id"],
                                   "signature": row["failure_signature"]})
        invalid_reasons = json.loads(run["invalid_reasons_json"])
        prior = [dict(row) for row in connection.execute(
            "SELECT run_id,attempt,conclusion FROM runs WHERE experiment_id=? AND status='frozen' "
            "AND attempt<? ORDER BY attempt",
            (run["experiment_id"], run["attempt"])).fetchall()]
        if run["status"] == "frozen":
            _, evidence = self._judge(connection, run, prior, failed_now, invalid_reasons)
        else:
            evidence = [{"rule": "not_frozen",
                         "detail": "运行尚未齐套冻结，暂无结论；cases 为已接收的部分结果"}]
        review_row = connection.execute(
            "SELECT * FROM reviews WHERE run_id=?", (run_id,)).fetchone()
        review = None
        if review_row:
            review = {"review_id": review_row["review_id"], "status": review_row["status"],
                      "student_actor_id": review_row["student_actor_id"],
                      "opened_by": review_row["opened_by"], "opened_at": review_row["opened_at"],
                      "deadline": review_row["deadline"],
                      "explanation": review_row["explanation"],
                      "explanation_at": review_row["explanation_at"],
                      "decision": review_row["decision"],
                      "decided_by": review_row["decided_by"],
                      "decided_at": review_row["decided_at"],
                      "rerun_run_id": review_row["rerun_run_id"]}
        return {
            "run_id": run_id,
            "experiment_id": run["experiment_id"],
            "attempt": run["attempt"],
            "status": run["status"],
            "conclusion": run["conclusion"],
            "expected_shards": run["expected_shards"],
            "received_shards": len(shard_rows),
            "opened_by": run["opened_by"],
            "opened_at": run["opened_at"],
            "frozen_at": run["frozen_at"],
            "inputs": {
                "build": {"build_id": build["build_id"], "program_name": build["program_name"],
                          "digest": build["digest"]},
                "case_package": {"package_id": package["package_id"],
                                 "version": package["version"], "digest": package["digest"]},
                "environment": {"environment_id": environment["environment_id"],
                                "digest": environment["digest"],
                                "declaration": json.loads(environment["declaration_json"])},
                "shards": shard_digests,
            },
            "frozen_inputs_hash": run["inputs_hash"],
            "recomputed_inputs_hash": recomputed_inputs_hash,
            "inputs_hash_matches": run["inputs_hash"] == recomputed_inputs_hash,
            "invalid_reasons": invalid_reasons,
            "cases": frozen_cases,
            "judgement_evidence": evidence,
            "review": review,
        }

    def _build_replay(self, connection, run_id: str, *, inputs_material: dict[str, Any],
                      inputs_hash: str, invalid_reasons: list[dict[str, Any]], conclusion: str,
                      evidence: list[dict[str, Any]], prior: list[dict[str, Any]],
                      shard_digests: list[dict[str, Any]]) -> dict[str, Any]:
        # 冻结后直接复用只读重放，保证两条路径产出一致。
        return self._replay(connection, run_id)

    def get_experiment(self, experiment_id: str, actor_id: str | None = None) -> dict[str, Any]:
        with self.database.transaction() as connection:
            if actor_id is not None:
                self._actor(connection, actor_id)
            experiment = connection.execute(
                "SELECT * FROM experiments WHERE experiment_id=?", (experiment_id,)).fetchone()
            if experiment is None:
                raise NotFoundError("实验不存在")
            runs = []
            for row in connection.execute(
                    "SELECT run_id,attempt,status,conclusion,expected_shards,inputs_hash,"
                    "opened_at,frozen_at FROM runs WHERE experiment_id=? ORDER BY attempt",
                    (experiment_id,)):
                runs.append(dict(row))
            return {"experiment_id": experiment_id, "build_id": experiment["build_id"],
                    "package_id": experiment["package_id"],
                    "package_version": experiment["package_version"],
                    "environment_id": experiment["environment_id"],
                    "created_at": experiment["created_at"], "runs": runs}

    # ----- 复核流程 --------------------------------------------------

    def open_review(self, *, request_id: str, actor_id: str, run_id: str,
                    student_actor_id: str, deadline: str) -> dict[str, Any]:
        """教师针对冻结运行发起带截止时间的复核。"""

        deadline_iso = parse_instant(deadline, "deadline")
        payload = {"actor_id": actor_id, "run_id": run_id,
                   "student_actor_id": student_actor_id, "deadline": deadline_iso}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            if actor.role not in {"admin", "teacher"}:
                raise PermissionDenied("只有教师可以发起复核")
            student = self._actor(connection, student_actor_id)
            if student.role != "student":
                raise ValidationError("复核对象必须是学生角色")
            run = connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if run is None:
                raise NotFoundError("运行不存在")
            if run["status"] != "frozen":
                raise ConflictError("只能对已冻结运行发起复核")
            if self._now_dt() >= _parse(deadline_iso):
                raise ValidationError("截止时间必须晚于当前时间")

            def create() -> tuple[str, str, dict[str, Any]]:
                if connection.execute("SELECT 1 FROM reviews WHERE run_id=?", (run_id,)).fetchone():
                    raise ConflictError("该运行已经存在复核")
                review_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO reviews(review_id,run_id,student_actor_id,opened_by,opened_at,"
                    "deadline,status) VALUES(?,?,?,?,?,?,'open')",
                    (review_id, run_id, student_actor_id, actor_id, self._now(), deadline_iso),
                )
                append_event(connection, actor_id=actor_id, action="review.opened",
                             resource_type="review", resource_id=review_id,
                             detail={"run_id": run_id, "student_actor_id": student_actor_id,
                                     "deadline": deadline_iso},
                             occurred_at=self._now())
                return "review", review_id, {"review_id": review_id, "run_id": run_id,
                                             "deadline": deadline_iso}

            return self._idempotent(connection, request_id=request_id, action="open_review",
                                    payload=payload, create=create)

    def submit_explanation(self, *, request_id: str, actor_id: str, review_id: str,
                           explanation: str) -> dict[str, Any]:
        """学生提交且只能提交一次补充说明，逾期拒收。"""

        explanation = self._text(explanation, "explanation", 4000)
        payload = {"actor_id": actor_id, "review_id": review_id, "explanation": explanation}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            review = connection.execute("SELECT * FROM reviews WHERE review_id=?",
                                        (review_id,)).fetchone()
            if review is None:
                raise NotFoundError("复核不存在")
            if actor.actor_id != review["student_actor_id"]:
                raise PermissionDenied("只有被指定的学生可以提交说明")
            if review["status"] != "open":
                raise ConflictError("复核已经作出决定")
            if review["explanation"] is not None:
                raise ConflictError("补充说明只能提交一次")
            if self._now_dt() > _parse(review["deadline"]):
                raise ConflictError("已经超过复核截止时间，说明不再接收")

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "UPDATE reviews SET explanation=?, explanation_at=? WHERE review_id=?",
                    (explanation, self._now(), review_id),
                )
                append_event(connection, actor_id=actor_id, action="review.explanation_submitted",
                             resource_type="review", resource_id=review_id,
                             detail={"run_id": review["run_id"], "length": len(explanation)},
                             occurred_at=self._now())
                return "review", review_id, {"review_id": review_id, "submitted": True}

            return self._idempotent(connection, request_id=request_id,
                                    action="submit_explanation", payload=payload, create=create)

    def decide_review(self, *, request_id: str, actor_id: str, review_id: str,
                      decision: str) -> dict[str, Any]:
        """复核员给出采信或重跑决定；重跑会开启同一实验的下一次尝试。"""

        if decision not in {"accept", "rerun"}:
            raise ValidationError("decision 必须是 accept 或 rerun")
        payload = {"actor_id": actor_id, "review_id": review_id, "decision": decision}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            if actor.role not in {"admin", "reviewer"}:
                raise PermissionDenied("只有复核员可以作出复核决定")
            review = connection.execute("SELECT * FROM reviews WHERE review_id=?",
                                        (review_id,)).fetchone()
            if review is None:
                raise NotFoundError("复核不存在")
            if review["status"] != "open":
                raise ConflictError("复核已经作出决定")
            run = connection.execute("SELECT * FROM runs WHERE run_id=?",
                                     (review["run_id"],)).fetchone()

            def create() -> tuple[str, str, dict[str, Any]]:
                rerun_run_id = None
                attempt = None
                if decision == "rerun":
                    row = connection.execute(
                        "SELECT COALESCE(MAX(attempt),0) AS max_attempt FROM runs "
                        "WHERE experiment_id=?", (run["experiment_id"],)).fetchone()
                    attempt = row["max_attempt"] + 1
                    rerun_run_id = uuid.uuid4().hex
                    connection.execute(
                        "INSERT INTO runs(run_id,experiment_id,attempt,expected_shards,status,"
                        "invalid_reasons_json,opened_by,opened_at) VALUES(?,?,?,?,'collecting','[]',?,?)",
                        (rerun_run_id, run["experiment_id"], attempt, run["expected_shards"],
                         actor_id, self._now()),
                    )
                    append_event(connection, actor_id=actor_id, action="run.rerun_opened",
                                 resource_type="run", resource_id=rerun_run_id,
                                 detail={"experiment_id": run["experiment_id"], "attempt": attempt,
                                         "origin_run_id": run["run_id"]},
                                 occurred_at=self._now())
                connection.execute(
                    "UPDATE reviews SET status='decided',decision=?,decided_by=?,decided_at=?,"
                    "rerun_run_id=? WHERE review_id=?",
                    (decision, actor_id, self._now(), rerun_run_id, review_id),
                )
                append_event(connection, actor_id=actor_id,
                             action=f"review.decided_{decision}",
                             resource_type="review", resource_id=review_id,
                             detail={"run_id": review["run_id"], "rerun_run_id": rerun_run_id},
                             occurred_at=self._now())
                return "review", review_id, {"review_id": review_id, "decision": decision,
                                             "rerun_run_id": rerun_run_id, "rerun_attempt": attempt}

            return self._idempotent(connection, request_id=request_id, action="decide_review",
                                    payload=payload, create=create)

    # ----- 统计（只引用冻结输入）------------------------------------

    def stats_snapshot(self, *, request_id: str, actor_id: str,
                       experiment_id: str) -> dict[str, Any]:
        """生成实验统计快照；所有计数只取自 status=frozen 的运行。"""

        payload = {"actor_id": actor_id, "experiment_id": experiment_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            if actor.role not in {"admin", "operator", "teacher", "reviewer", "auditor"}:
                raise PermissionDenied("当前角色不能查看统计")
            experiment_id = self._identifier(experiment_id, "experiment_id")

            def create() -> tuple[str, str, dict[str, Any]]:
                experiment = connection.execute(
                    "SELECT * FROM experiments WHERE experiment_id=?", (experiment_id,)).fetchone()
                if experiment is None:
                    raise NotFoundError("实验不存在")
                run_rows = connection.execute(
                    "SELECT * FROM runs WHERE experiment_id=? AND status='frozen' ORDER BY attempt",
                    (experiment_id,)).fetchall()
                frozen_refs = []
                conclusion_counts = {key: 0 for key in CONCLUSIONS}
                case_outcomes: dict[str, dict[str, int]] = {}
                repeated_signatures: dict[str, list[int]] = {}
                invalid_total = 0
                for run in run_rows:
                    frozen_refs.append({"run_id": run["run_id"], "attempt": run["attempt"],
                                        "conclusion": run["conclusion"],
                                        "inputs_hash": run["inputs_hash"]})
                    conclusion_counts[run["conclusion"]] = \
                        conclusion_counts.get(run["conclusion"], 0) + 1
                    invalid_total += len(json.loads(run["invalid_reasons_json"]))
                    for case in connection.execute(
                            "SELECT case_id,outcome,failure_signature FROM frozen_cases "
                            "WHERE run_id=?", (run["run_id"],)):
                        bucket = case_outcomes.setdefault(
                            case["case_id"], {"passed": 0, "failed": 0, "errored": 0})
                        bucket[case["outcome"]] += 1
                        if case["failure_signature"]:
                            repeated_signatures.setdefault(
                                case["failure_signature"], set()).add(run["attempt"])
                flaky_cases = sorted(
                    case_id for case_id, bucket in case_outcomes.items()
                    if bucket["passed"] > 0 and bucket["failed"] + bucket["errored"] > 0)
                repeated = sorted(
                    ({"signature": signature, "attempts": sorted(attempts)}
                     for signature, attempts in repeated_signatures.items() if len(attempts) >= 2),
                    key=lambda item: item["signature"])
                open_reviews = connection.execute(
                    "SELECT COUNT(*) AS count FROM reviews r JOIN runs u ON r.run_id=u.run_id "
                    "WHERE u.experiment_id=? AND r.status='open'", (experiment_id,)).fetchone()["count"]
                decided_reviews = connection.execute(
                    "SELECT COUNT(*) AS count FROM reviews r JOIN runs u ON r.run_id=u.run_id "
                    "WHERE u.experiment_id=? AND r.status='decided'",
                    (experiment_id,)).fetchone()["count"]
                content = {
                    "experiment_id": experiment_id,
                    "frozen_run_count": len(run_rows),
                    "frozen_inputs": frozen_refs,
                    "conclusion_counts": conclusion_counts,
                    "case_count": len(case_outcomes),
                    "flaky_cases": flaky_cases,
                    "repeated_failure_signatures": repeated,
                    "invalid_reason_total": invalid_total,
                    "reviews": {"open": open_reviews, "decided": decided_reviews},
                    "generated_at": self._now(),
                }
                content_hash = digest(content)
                snapshot_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO stats_snapshots(snapshot_id,experiment_id,content_json,"
                    "content_hash,created_by,created_at) VALUES(?,?,?,?,?,?)",
                    (snapshot_id, experiment_id, canonical_json(content), content_hash,
                     actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="stats.snapshot_created",
                             resource_type="stats_snapshot", resource_id=snapshot_id,
                             detail={"experiment_id": experiment_id, "content_hash": content_hash,
                                     "frozen_run_count": len(run_rows)},
                             occurred_at=self._now())
                return "stats_snapshot", snapshot_id, {"snapshot_id": snapshot_id, **content,
                                                        "content_hash": content_hash}

            return self._idempotent(connection, request_id=request_id, action="stats_snapshot",
                                    payload=payload, create=create)


def _parse(value: str):
    from datetime import datetime
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
