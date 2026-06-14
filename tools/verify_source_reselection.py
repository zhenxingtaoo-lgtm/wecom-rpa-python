from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from wecom_rpa.config import build_runtime_config
from wecom_rpa.forward_flow import ForwardFlow


def _points_payload(points: list[tuple[float, float]]) -> list[list[float]]:
    return [[round(x, 4), round(y, 4)] for x, y in points]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="验证源消息重新勾选；不会打开收件人弹窗或点击发送按钮。"
    )
    parser.add_argument("--expected-count", type=int, default=4)
    parser.add_argument("--reselect", action="store_true")
    parser.add_argument(
        "--result",
        default="logs/source_reselection_probe.json",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    result_path = Path(args.result)
    result_path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "mode": "reselect" if args.reselect else "inspect_only",
        "expected_count": args.expected_count,
        "status": "started",
    }

    try:
        config = build_runtime_config(
            dry_run=False,
            allow_real_send=True,
        )
        flow = ForwardFlow(
            config,
            screenshot_dir="screenshots",
            real_send_allowed=True,
            install_stop_hotkey=False,
        )
        rect = flow.window.locate()
        if rect is None:
            raise RuntimeError("未找到企业微信窗口")

        before_shot = flow.screen.save_checkpoint(
            "live_source_reselection_before",
            region=None,
        )
        before_points = flow._source_selected_checkbox_ratios_in_window(
            before_shot,
            rect,
        )
        payload.update(
            {
                "before_screenshot": str(before_shot),
                "before_points": _points_payload(before_points),
                "before_count": len(before_points),
            }
        )
        if len(before_points) != args.expected_count:
            raise RuntimeError(
                f"当前源消息蓝勾数量不是 {args.expected_count} 个：actual={len(before_points)}"
            )

        if not args.reselect:
            payload["status"] = "inspection_passed"
            return 0

        source_points = sorted(before_points, key=lambda item: item[1], reverse=True)
        flow.source_checkbox_x_ratio = sum(x for x, _y in source_points) / len(source_points)
        flow.source_checkbox_y_ratios = [y for _x, y in source_points]

        # 关闭当前多选工具栏，回到普通聊天状态。此坐标是工具栏最右侧的 X，
        # 与任何转发或发送按钮分离。
        if not flow.window.click_relative(rect, 0.771, 0.900):
            raise RuntimeError("无法关闭当前源消息多选工具栏")
        time.sleep(0.8)

        flow._reselect_source_messages(rect)
        after_shot = flow.screen.save_checkpoint(
            "live_source_reselection_after",
            region=None,
        )
        after_points = flow._source_selected_checkbox_ratios_in_window(
            after_shot,
            rect,
        )
        payload.update(
            {
                "after_screenshot": str(after_shot),
                "after_points": _points_payload(after_points),
                "after_count": len(after_points),
            }
        )
        if len(after_points) != args.expected_count:
            raise RuntimeError(
                f"重新进入多选后蓝勾数量不是 {args.expected_count} 个：actual={len(after_points)}"
            )
        payload["status"] = "reselection_passed"
        return 0
    except Exception as exc:
        payload["status"] = "failed"
        payload["error"] = str(exc)
        logging.exception("源消息无发送验证失败")
        return 1
    finally:
        result_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


if __name__ == "__main__":
    raise SystemExit(main())
