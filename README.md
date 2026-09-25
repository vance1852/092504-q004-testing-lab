# 软件测试实验运行与缺陷复现平台

在技能赛训协作基础服务（组织、人员、场所、资料登记、请求幂等、角色权限、SQLite
事务与哈希串联审计）之上，本项目为软件测试世赛项目提供**可重复实验**能力：把接口
契约对应的程序构建、用例包版本和运行环境声明登记为不可变制品，接收离线执行器分片
上传的逐用例结果、日志摘要与覆盖证据，齐套后冻结结论，并支持教师发起带截止时间的
复核、学生一次补充说明、复核员采信或重跑。所有统计只引用冻结输入，接口与命令行都能
重放某次实验为何被判为稳定失败、偶发或无效。

## 目录

- `src/skills_workspace/`
  - `service.py` / `storage.py` / `audit.py` / `clock.py` / `models.py` / `errors.py`：基础登记、事务、审计链；
  - `verdict.py`：**纯函数判定器**，合并分片并按证据规则判定，冻结与重放共用同一份代码；
  - `experiments.py`：不可变制品登记、分片合并与冻结、复核流程、统计、重放与恢复；
  - `api.py`：HTTP/JSON 边界；`cli.py`：命令行重放、恢复、统计与签名检索；
  - `acceptance.py` / `experiment_acceptance.py`：两条离线端到端验收。
- `tests/`：判定规则、服务事务、HTTP 路由、崩溃恢复与端到端验收测试。

## 环境

- Linux，Python 3.11+，运行时仅使用 Python 标准库和 SQLite。

## 测试与验收

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 -m skills_workspace.acceptance
PYTHONPATH=src python3 -m skills_workspace.experiment_acceptance
```

## 核心规则

### 不可变制品

- `POST /builds` 程序构建（`build_digest` 为 64 位 SHA-256）、
  `POST /case-packages` 用例包版本（含全部 `case_ids`）、
  `POST /environments` 运行环境声明（`environment_digest`）。
- 同一编号重复登记**相同摘要**返回原回执（幂等）；相同编号登记**不同摘要**返回
  `409 conflict`；相同摘要换编号登记同样冲突。制品一旦登记不可覆盖。

### 分片上传与齐套冻结

- `POST /runs` 创建运行时声明构建、用例包、环境与 `expected_shards`。
- `POST /runs/{run_id}/shards` 上传 `{shard_index, shard:{executor, cases}}`，
  分片允许乱序到达；服务端对规范分片体计算内容哈希，客户端可在 `content_hash`
  字段携带自算哈希，不一致直接拒绝。
- 同一分片编号重复上传**相同内容**幂等返回；上传**不同内容**返回 `409`，
  已到分片不能被覆盖。运行冻结后执行器重试只接受完全相同的分片。
- 收齐 `expected_shards` 个分片后，在同一事务内调用判定器并写入冻结快照
  （`frozen_inputs_digest` = 构建摘要 + 用例包摘要 + 环境摘要 + 每个分片内容哈希），
  随后分片与结论都不可变。

### 判定规则（`verdict.py`）

按固定顺序校验证据，任一不过即 `invalid` 并给出 `reason_code`：

1. `shards_complete`：分片序号恰为 `0..expected_shards-1`；
2. `case_set_exact`：用例集与用例包声明**完全一致**（无缺失、无多余、无重复）；
3. `attempt_evidence_complete`：每个失败尝试必须带 `failure_signature` 与
   `log_summary`，结果只能是 `pass`/`fail`；
4. `coverage_evidence_present`：每个用例必须有覆盖证据（`evidence_ref` 或
   非空 `covered_files`）；
5. `environment_matches_declared`：执行器各分片观测到的环境摘要一致且等于声明值。

全部通过后：

- 有用例在**每次尝试中都失败** → `stable_failure`；
- 同一用例同一输入下既有通过又有失败 → `flaky`（偶发）；
- 全部用例所有尝试均通过 → `stable_pass`。

多个用例命中同一 `failure_signature` 时进入 `repeated_signatures`，
`GET /signatures?signature=...` 可跨冻结运行聚类检索，用于缺陷复现。

### 复核流程

- 教师 `POST /reviews` 对已冻结运行发起复核，必须带未来的 `deadline`；
- 学生 `POST /reviews/{id}/supplement` 提交补充说明，**每个复核仅一次**，
  截止后拒绝提交；`POST /maintenance/resume` 或服务重启会把过期复核置为 `expired`；
- 复核员 `POST /reviews/{id}/decision` 给出 `accept`（采信，维持冻结结论）或
  `rerun`（重跑）：重跑自动以相同制品创建新运行编号，旧运行标记为 `superseded`，
  默认统计不再计入。

### 统计与重放

- `GET /statistics` 只汇总已冻结运行，每个数字都可追溯到对应
  `frozen_inputs_digest`；加 `?include_superseded=true` 可包含被重跑取代的运行。
- `GET /runs/{run_id}/replay` 从落库分片**重新计算**内容哈希与判定，比对冻结
  摘要与结论；若分片载荷被篡改，会在 `tampered_shards` 中列出。
- 命令行：

```bash
PYTHONPATH=src python3 -m skills_workspace.cli --database exp.sqlite3 replay run-001 --strict
PYTHONPATH=src python3 -m skills_workspace.cli --database exp.sqlite3 resume --stats
PYTHONPATH=src python3 -m skills_workspace.cli --database exp.sqlite3 stats
PYTHONPATH=src python3 -m skills_workspace.cli --database exp.sqlite3 signature SIG-NPE-42
PYTHONPATH=src python3 -m skills_workspace.cli --database exp.sqlite3 show run-001
```

`replay --strict` 在重算结果与冻结结论不一致时以退出码 2 结束，可用于审计脚本。

### 中断重启恢复

冻结与分片入库处于同一事务，因此不会出现“结论已写、证据缺失”。若进程在最后一个
分片入库后、冻结完成前崩溃，重启时（`api.py` 启动钩子或 `cli resume`、
`POST /maintenance/resume`）会扫描全部 `collecting` 运行，把已齐套者补做冻结，
重复执行幂等无副作用。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m skills_workspace.api --database skills_workspace.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查 `GET /health`；写接口通过 `X-Actor-Id` 标识操作者，所有写操作携带
`request_id` 实现幂等。服务重启后业务状态、冻结结论与审计链继续保留，
`/health` 会同时校验审计哈希链。
