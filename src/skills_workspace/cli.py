"""软件测试实验平台命令行入口。

支持：
- replay-run：重放某次运行为何判定为稳定失败、偶发或无效；
- recover：中断重启后继续未完成的分片合并；
- stats：生成只引用冻结输入的统计快照；
- experiment：查看实验及其全部尝试。
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .experiments import ExperimentService
from .storage import Database


def _print(value: dict[str, Any] | list[dict[str, Any]]) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="skills-workspace-experiments",
                                     description="软件测试实验运行与缺陷复现平台命令行")
    parser.add_argument("--database", default="service.sqlite3", help="SQLite 数据库路径")
    subparsers = parser.add_subparsers(dest="command", required=True)

    replay_parser = subparsers.add_parser("replay-run", help="重放某次运行的判定依据")
    replay_parser.add_argument("--run-id", required=True)
    replay_parser.add_argument("--actor-id", default=None,
                               help="可选：提供操作者编号时同时校验其有效性")

    subparsers.add_parser("recover", help="继续合并中断时未完成冻结的分片")

    stats_parser = subparsers.add_parser("stats", help="生成实验统计快照")
    stats_parser.add_argument("--experiment-id", required=True)
    stats_parser.add_argument("--actor-id", required=True)
    stats_parser.add_argument("--request-id", required=True,
                              help="幂等请求编号，重复执行返回原快照")

    exp_parser = subparsers.add_parser("experiment", help="查看实验及其尝试")
    exp_parser.add_argument("--experiment-id", required=True)

    args = parser.parse_args(argv)
    database = Database(args.database)
    service = ExperimentService(database)
    try:
        if args.command == "replay-run":
            _print(service.get_run(args.run_id, args.actor_id))
        elif args.command == "recover":
            _print({"items": service.recover_interrupted()})
        elif args.command == "stats":
            _print(service.stats_snapshot(request_id=args.request_id, actor_id=args.actor_id,
                                          experiment_id=args.experiment_id))
        elif args.command == "experiment":
            _print(service.get_experiment(args.experiment_id))
    finally:
        database.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
