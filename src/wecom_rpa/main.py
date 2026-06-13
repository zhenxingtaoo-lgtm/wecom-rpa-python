from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from .config import load_config
from .forward_flow import ForwardFlow
from .screen import ScreenInspector


def setup_logging(log_file: str | Path) -> None:
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(log_file, encoding="utf-8")],
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="企业微信桌面端批量转发 RPA")
    parser.add_argument("--config", default="config/config.example.yaml", help="配置 YAML 路径")
    parser.add_argument("--send-count", type=int, help="本次计划从会话列表底部发送的会话数量")
    parser.add_argument("--log-file", default="logs/wecom_rpa.log", help="日志文件路径")
    parser.add_argument("--screenshot-dir", default="screenshots", help="截图/占位文件目录")
    parser.add_argument("--yes", action="store_true", help="跳过人工确认（用于测试/cron；仍保持 dry-run）")
    parser.add_argument("--dry-run", dest="dry_run", action="store_true", default=None, help="强制 dry-run=true")
    parser.add_argument("--no-dry-run", dest="dry_run", action="store_false", help="关闭 dry-run，进入真实发送模式")
    parser.add_argument("--real-send", action="store_true", help="允许真实点击企业微信发送按钮")
    parser.add_argument(
        "--i-understand-this-will-send-messages",
        action="store_true",
        help="真实发送二次确认开关，必须和 --real-send 同时使用",
    )
    parser.add_argument("--check-ocr-models", action="store_true", help="只检查 PaddleOCR 离线模型和依赖是否可初始化")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.log_file)
    log = logging.getLogger(__name__)
    try:
        allow_real_send = bool(args.real_send and args.i_understand_this_will_send_messages)
        config = load_config(args.config, force_dry_run=args.dry_run, allow_real_send=allow_real_send)
        if args.check_ocr_models:
            inspector = ScreenInspector(
                args.screenshot_dir,
                template_threshold=config.vision.template_threshold,
                ocr_engine=config.ocr.engine,
                ocr_lang=config.ocr.lang,
                ocr_fallback=config.ocr.fallback,
                paddle_model_root=config.ocr.model_root,
            )
            kwargs = inspector.paddleocr_model_kwargs()
            from paddleocr import PaddleOCR  # type: ignore

            try:
                PaddleOCR(lang=config.ocr.lang, use_textline_orientation=True, **kwargs)
            except TypeError:
                PaddleOCR(lang=config.ocr.lang, **kwargs)
            print(f"ocr_status=ok model_dirs={kwargs}")
            return 0
        if args.send_count is None or args.send_count <= 0:
            raise ValueError("--send-count 必须 > 0")
        snapshot_dir = Path(args.log_file).parent / "run_snapshots"
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        snapshot_path = snapshot_dir / f"run_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.json"
        snapshot_path.write_text(
            json.dumps(
                {
                    "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "send_count": args.send_count,
                    "config_path": str(Path(args.config).resolve()),
                    "effective_config": asdict(config),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        log.info("本次运行参数快照已保存：%s", snapshot_path)
        result = ForwardFlow(
            config,
            screenshot_dir=args.screenshot_dir,
            yes=args.yes,
            real_send_allowed=allow_real_send,
        ).run(args.send_count)
        log.info("运行完成：status=%s summary=%s", result.status, result.summary)
        print(f"status={result.status} summary={result.summary}")
        return 0
    except Exception as exc:
        log.exception("运行失败：%s", exc)
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
