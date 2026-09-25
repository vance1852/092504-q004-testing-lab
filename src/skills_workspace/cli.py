"""实验平台的命令行入口：重放判定、恢复合并、查看统计。

示例：
    PYTHONPATH=src python3 -m skills_workspace.cli --database exp.sqlite3 replay run-001
    PYTHONPATH=src python3 -m skills_workspace.cli --database exp.sqlite3 resume
    PYTHONPATH=src python3 -m skills_workspace.cli --database exp.sqlite3 stats
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .experiments import ExperimentService
from .storage import Database


def _print(value: dict[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="软件测试实验运行与缺陷复现平台命令行")
    parser.add_argument("--database", default="service.sqlite3", help="SQLite 数据库路径")
    sub = parser.add_subparsers(dest="command", required=True)

    replay_parser = sub.add_parser("replay", help="从冻结输入重放某次实验的判定理由")
    replay_parser.add_argument("run_id")
    replay_parser.add_argument("--strict", action="store_true",
                               help="重算结果与冻结结论不一致时以退出码 2 结束")

    resume_parser = sub.add_parser("resume", help="重启后继续未完成的分片合并并过期复核")
    resume_parser.add_argument("--stats", action="store_true", help="恢复后顺带输出统计")

    stats_parser = sub.add_parser("stats", help="输出只引用冻结输入的统计")
    stats_parser.add_argument("--include-superseded", action="store_true")

    signature_parser = sub.add_parser("signature", help="按失败签名聚类检索冻结运行")
    signature_parser.add_argument("signature")

    show_parser = sub.add_parser("show", help="查看某次运行的当前状态")
    show_parser.add_argument("run_id")

    args = parser.parse_args(argv)
    database = Database(args.database)
    service = ExperimentService(database)
    try:
        if args.command == "replay":
            result = service.replay_run(args.run_id)
            _print(result)
            if args.strict and not result["matches"]:
                return 2
            return 0
        if args.command == "resume":
            result: dict[str, Any] = {"frozen_runs": service.resume_pending(),
                                      "expired_reviews": service.expire_due_reviews()}
            if args.stats:
                result["statistics"] = service.statistics()
            _print(result)
            return 0
        if args.command == "stats":
            _print(service.statistics(include_superseded=args.include_superseded))
            return 0
        if args.command == "signature":
            _print(service.search_signature(args.signature))
            return 0
        if args.command == "show":
            _print(service.get_run(args.run_id))
            return 0
    finally:
        database.close()
    return 1


if __name__ == "__main__":
    sys.exit(main())
