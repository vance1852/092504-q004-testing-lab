# 软件测试实验运行与缺陷复现平台

软件测试首次成为世赛项目后，教研组需要把接口契约、测试数据和候选程序版本组合成可重复的实验，
而不是只收一份通过率截图。本项目在原有技能赛训协作基础服务之上，提供一套**实验运行与缺陷复现
平台**：登记不可变的程序构建摘要、用例包版本和运行环境声明，接收离线执行器上传的逐用例结果、
日志摘要与覆盖证据，齐套后才冻结结论，并支持带截止时间的复核流程与判定依据重放。

运行时仅使用 Python 标准库和 SQLite。

## 核心规则

- **不可变输入登记**：`builds`（程序构建 SHA-256 摘要 + 清单）、`case_packages`（用例包版本 +
  完整用例集合）、`environments`（环境声明，摘要由声明内容规范哈希自证派生）；相同业务键绑定不同
  摘要一律 `409 conflict`，不可覆盖。
- **实验组合**：一个实验 = 一个构建 × 一个用例包版本 × 一个环境，三元组唯一。
- **分片接收**：同一 `run_id` 的分片允许**乱序到达**；`content_hash` 必须与分片内容自证绑定；
  相同 `(run_id, shard_index)` 同内容重传幂等，异内容 `409` 且**绝不覆盖**；分片携带的构建/环境
  摘要必须与实验登记输入一致，否则拒绝。
- **齐套冻结**：所有 `expected_shards` 到齐后在单个 SQLite 事务内完成用例覆盖校验、失败签名归一化
  与结论判定，并计算 `inputs_hash`；冻结后不再接收分片。
- **结论判定**（跨同一实验此前**已冻结**的尝试逐用例比对）：
  - `pass`：全部通过；
  - `invalid`：缺用例/多用例/重复用例、缺覆盖证据、失败缺日志或失败详情等；
  - `stable_fail`：失败签名在此前冻结尝试中重复出现，或首次尝试即失败（稳定失败候选）；
  - `flaky`：本次失败用例在此前尝试中曾通过（结果跨尝试不一致），或失败未在历史中复现。
  失败签名对失败类型与消息中的地址、数字、空白做归一化，使同一缺陷跨尝试可匹配。
- **复核流程**：教师对冻结运行发起复核并设定截止时间；学生**只能提交一次**补充说明且逾期拒收；
  复核员决定 `accept`（采信）或 `rerun`（重跑，自动开启同一实验下一次尝试）。
- **统计只引用冻结输入**：统计快照内每个运行都带 `inputs_hash`，收集态运行绝不计入；快照自身带
  `content_hash`，幂等请求返回同一快照编号。
- **重放**：`GET /runs/{id}/replay` 与 CLI `replay-run` 重新从冻结表计算并逐条列出判定证据
  （`judgement_evidence`），同时核对重算的输入哈希与冻结时刻一致（`inputs_hash_matches`）。
- **中断续传**：服务启动时自动调用 `recover_interrupted`；也可通过 `POST /runs/recover` 或 CLI
  `recover` 手动触发，把已齐套但仍处收集态的运行冻结。
- **审计**：所有状态变更写入与基础服务共用的哈希串联审计链，可离线校验。

## 目录

- `src/skills_workspace/`
  - `experiments.py`：实验平台领域服务（登记、上传、冻结、判定、复核、统计、重放、恢复）；
  - `experiment_acceptance.py`：实验平台离线端到端验收；
  - `cli.py`：判定重放、恢复合并、统计、实验视图命令行；
  - `api.py`：在原 HTTP 边界上挂载实验平台路由；
  - 其余为基础服务（领域资料登记、权限、存储、审计链、时钟）。
- `tests/`：服务规则、HTTP 路由、CLI、端到端验收测试（共 32 个）。

## 环境与测试

- Linux，Python 3.11+，仅标准库 + SQLite。

```bash
python3 -m compileall -q src tests
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 离线验收

```bash
PYTHONPATH=src python3 -m skills_workspace.acceptance             # 基础服务
PYTHONPATH=src python3 -m skills_workspace.experiment_acceptance  # 实验平台
```

实验平台验收会在临时库中走完：不可变登记 → 乱序分片（同内容幂等/异内容拒覆盖）→ 稳定失败确认 →
带截止时间复核与重跑 → 偶发判定 → 只引用冻结输入的统计 → 模拟中断后重启继续合并，输出一行
`status` 为 `ok` 的 JSON。

## HTTP 接口

启动：

```bash
PYTHONPATH=src python3 -m skills_workspace.api --database skills.sqlite3 --host 127.0.0.1 --port 8080
```

所有写入接口通过 `X-Actor-Id` 标识操作者，并要求 `request_id` 保证幂等。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/builds` | 登记不可变程序构建摘要 |
| POST | `/case-packages` | 登记不可变用例包版本 |
| POST | `/environments` | 登记运行环境声明（摘要自证派生） |
| POST | `/experiments` | 组合构建×用例包×环境为实验 |
| POST | `/runs/open` | 开启实验的下一次尝试 |
| POST | `/runs/shards` | 上传一个结果分片（齐套即冻结） |
| POST | `/runs/recover` | 中断后续传合并 |
| GET | `/runs/{run_id}/replay` | 重放判定依据 |
| GET | `/experiments/{id}` | 查看实验及全部尝试 |
| POST | `/reviews` | 教师发起带截止时间的复核 |
| POST | `/reviews/explanation` | 学生提交一次补充说明 |
| POST | `/reviews/decision` | 复核员采信/重跑 |
| POST | `/stats/snapshots` | 生成只引用冻结输入的统计快照 |

### 分片上传示例

```json
{
  "request_id": "executor-upload-0001",
  "run_id": "…",
  "shard_index": 0,
  "content_hash": "<sha256(分片内容规范JSON)>",
  "build_digest": "<登记的构建摘要>",
  "environment_digest": "<登记的环境摘要>",
  "coverage_digest": "<sha256 覆盖证据摘要>",
  "cases": [
    {"case_id": "t001", "outcome": "passed"},
    {"case_id": "t002", "outcome": "failed",
     "failure": {"type": "AssertionError", "message": "expected 100 got 90"},
     "log_digest": "<sha256 日志摘要>"}
  ]
}
```

`content_hash` 的输入是 `{build_digest, environment_digest, coverage_digest, cases}` 的规范 JSON
（键排序、无空白、UTF-8）。失败用例必须随附 `failure` 与 `log_digest`。

## 命令行

```bash
PYTHONPATH=src python3 -m skills_workspace.cli --database skills.sqlite3 replay-run --run-id <id>
PYTHONPATH=src python3 -m skills_workspace.cli --database skills.sqlite3 recover
PYTHONPATH=src python3 -m skills_workspace.cli --database skills.sqlite3 stats \
    --experiment-id exp-1 --actor-id t1 --request-id stat-001
PYTHONPATH=src python3 -m skills_workspace.cli --database skills.sqlite3 experiment --experiment-id exp-1
```

## 角色

基础服务新增 `teacher`、`student` 两个角色：构建/用例包/环境/实验登记与分片上传允许
`admin/operator/teacher`；复核发起为 `admin/teacher`，决定为 `admin/reviewer`，补充说明仅被指定的
`student` 本人；统计对教师、复核员、审计员等只读角色开放。

## 数据可追溯性

冻结结论只依赖冻结表（`runs`、`shards`、`frozen_cases`、`signature_occurrences`），重放会重新执行
判定规则并核对输入哈希；每次登记、上传、冻结、复核、重跑、统计都进入哈希串联审计链，服务重启后
SQLite 中的业务状态、冻结结论与审计链继续保留。
