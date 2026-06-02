from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .config import load_config
from .forward_flow import ForwardFlow
from .groups import limit_groups, load_groups_csv
from .storage import StateStore


def setup_logging(log_file: str | Path) -> None:
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(log_file, encoding="utf-8")],
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="企业微信桌面端批量转发 RPA（第一版安全 dry-run 骨架）")
    parser.add_argument("--config", default="config/config.example.yaml", help="配置 YAML 路径")
    parser.add_argument("--groups", default="data/groups.example.csv", help="目标群 CSV 路径，需包含 group_name 表头")
    parser.add_argument("--db", default="data/wecom_rpa.sqlite3", help="SQLite 状态库路径")
    parser.add_argument("--log-file", default="logs/wecom_rpa.log", help="日志文件路径")
    parser.add_argument("--screenshot-dir", default="screenshots", help="截图/占位文件目录")
    parser.add_argument("--yes", action="store_true", help="跳过人工确认（用于测试/cron；仍保持 dry-run）")
    parser.add_argument("--dry-run", dest="dry_run", action="store_true", default=None, help="强制 dry-run=true")
    parser.add_argument("--no-dry-run", dest="dry_run", action="store_false", help="请求真实发送；第一版会拒绝")
    parser.add_argument("--real-send", action="store_true", help="允许真实点击企业微信发送按钮")
    parser.add_argument(
        "--i-understand-this-will-send-messages",
        action="store_true",
        help="真实发送二次确认开关，必须和 --real-send 同时使用",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.log_file)
    log = logging.getLogger(__name__)
    try:
        allow_real_send = bool(args.real_send and args.i_understand_this_will_send_messages)
        config = load_config(args.config, force_dry_run=args.dry_run, allow_real_send=allow_real_send)
        groups = load_groups_csv(args.groups)
        limited = limit_groups(groups, config.max_total_send)
        log.info("读取群列表：原始=%s，去重/限额后=%s，batch_size=%s", len(groups), len(limited), config.batch_size)
        with StateStore(args.db) as store:
            result = ForwardFlow(
                config,
                store,
                screenshot_dir=args.screenshot_dir,
                yes=args.yes,
                real_send_allowed=allow_real_send,
            ).run(limited)
        log.info("运行完成：run_id=%s status=%s summary=%s", result.run_id, result.status, result.summary)
        print(f"run_id={result.run_id} status={result.status} summary={result.summary}")
        return 0
    except Exception as exc:
        log.exception("运行失败：%s", exc)
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
