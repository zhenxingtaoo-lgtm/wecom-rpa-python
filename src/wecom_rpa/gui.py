from __future__ import annotations

import logging
import os
import queue
import subprocess
import threading
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .config import AppConfig, load_config
from .forward_flow import FlowResult, ForwardFlow
from .groups import limit_groups, load_groups_csv
from .models import TargetGroup, TargetStatus
from .powershell import terminate_active_powershell
from .safety import StopController
from .screen import ScreenInspector
from .storage import StateStore
from .wecom_window import WeComWindow

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class GuiRunOptions:
    config_path: Path
    groups_path: Path
    db_path: Path
    log_file: Path
    screenshot_dir: Path
    dry_run: bool = True
    max_total_send: int | None = None
    batch_size: int | None = None
    batch_interval_sec: float | None = None
    confirm_real_send: bool = False
    confirm_source_review: bool = False


@dataclass(frozen=True)
class RunInspection:
    config: AppConfig
    groups: list[TargetGroup]
    limited_groups: list[TargetGroup]
    original_count: int
    limited_count: int
    has_uncertain: bool
    uncertain_targets: list[str]
    ocr_warning: str | None = None


@dataclass(frozen=True)
class GuiLayout:
    width: int
    height: int
    min_width: int
    min_height: int
    base_font_size: int
    title_font_size: int


def configure_windows_dpi_awareness() -> None:
    if os.name != "nt":
        return
    try:
        import ctypes

        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception as exc:
        log.debug("设置 Windows DPI awareness 失败：%s", exc)


def compute_gui_layout(screen_width: int, screen_height: int, tk_scaling: float) -> GuiLayout:
    width_margin = 40
    height_margin = 40
    max_width = max(860, screen_width - width_margin)
    max_height = max(560, screen_height - height_margin)
    width = min(max(1100, int(screen_width * 0.86)), max_width)
    height = min(max(720, int(screen_height * 0.88)), max_height)
    min_width = min(980, width)
    min_height = min(620, height)
    base_font_size = 10 if tk_scaling >= 1.6 else 9
    if tk_scaling >= 2.3:
        base_font_size = 11
    return GuiLayout(
        width=width,
        height=height,
        min_width=min_width,
        min_height=min_height,
        base_font_size=base_font_size,
        title_font_size=base_font_size + 5,
    )


def validate_real_send_ready(*, dry_run: bool, confirm_send: bool, confirm_review: bool) -> None:
    if dry_run:
        return
    if not confirm_send or not confirm_review:
        raise ValueError("真实发送确认未完成：必须勾选两个真实发送确认项")


def _apply_config_overrides(config: AppConfig, options: GuiRunOptions) -> AppConfig:
    overrides: dict[str, Any] = {"dry_run": options.dry_run}
    if options.max_total_send is not None:
        overrides["max_total_send"] = int(options.max_total_send)
    if options.batch_size is not None:
        overrides["batch_size"] = int(options.batch_size)
    if options.batch_interval_sec is not None:
        overrides["batch_interval_sec"] = float(options.batch_interval_sec)
    updated = replace(config, **overrides)
    updated.validate(allow_real_send=not updated.dry_run)
    return updated


def inspect_run_setup(options: GuiRunOptions) -> RunInspection:
    validate_real_send_ready(
        dry_run=options.dry_run,
        confirm_send=options.confirm_real_send,
        confirm_review=options.confirm_source_review,
    )
    config = load_config(
        options.config_path,
        force_dry_run=options.dry_run,
        allow_real_send=not options.dry_run,
    )
    config = _apply_config_overrides(config, options)
    groups = load_groups_csv(options.groups_path)
    limited = limit_groups(groups, config.max_total_send)

    with StateStore(options.db_path) as store:
        statuses = store.get_statuses([group.group_name for group in limited])
    uncertain = [
        name
        for name, status in statuses.items()
        if status == TargetStatus.UNCERTAIN or status == str(TargetStatus.UNCERTAIN)
    ]

    ocr_warning = _check_ocr_model_warning(config, options.screenshot_dir)
    return RunInspection(
        config=config,
        groups=groups,
        limited_groups=limited,
        original_count=len(groups),
        limited_count=len(limited),
        has_uncertain=bool(uncertain),
        uncertain_targets=uncertain,
        ocr_warning=ocr_warning,
    )


def _check_ocr_model_warning(config: AppConfig, screenshot_dir: Path) -> str | None:
    if config.ocr.engine != "paddleocr" or not config.ocr.model_root:
        return None
    inspector = ScreenInspector(
        screenshot_dir,
        template_threshold=config.vision.template_threshold,
        ocr_engine=config.ocr.engine,
        ocr_lang=config.ocr.lang,
        ocr_fallback=config.ocr.fallback,
        paddle_model_root=config.ocr.model_root,
    )
    try:
        inspector.paddleocr_model_kwargs()
    except Exception as exc:
        return f"OCR 离线模型检查提示：{exc}"
    return None


class QueueLogHandler(logging.Handler):
    def __init__(self, ui_queue: "queue.Queue[tuple[str, Any]]"):
        super().__init__(level=logging.INFO)
        self.ui_queue = ui_queue
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.ui_queue.put(("log", self.format(record)))
        except Exception:
            pass


class WeComRpaApp:
    def __init__(self) -> None:
        configure_windows_dpi_awareness()
        import tkinter as tk
        import tkinter.font as tkfont
        from tkinter import ttk

        self.tk = tk
        self.tkfont = tkfont
        self.ttk = ttk
        self.root = tk.Tk()
        self.root.title("企业微信批量转发 RPA")
        self.layout = compute_gui_layout(
            self.root.winfo_screenwidth(),
            self.root.winfo_screenheight(),
            float(self.root.tk.call("tk", "scaling")),
        )
        self._configure_fonts()
        self.root.geometry(f"{self.layout.width}x{self.layout.height}")
        self.root.minsize(self.layout.min_width, self.layout.min_height)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.ttk.Style().configure("Danger.TButton", foreground="#b00020")
        self.dpi_info = (
            "GUI DPI 信息："
            f"screen={self.root.winfo_screenwidth()}x{self.root.winfo_screenheight()} "
            f"tk_scaling={float(self.root.tk.call('tk', 'scaling')):.3f} "
            f"window={self.layout.width}x{self.layout.height} "
            f"min={self.layout.min_width}x{self.layout.min_height}"
        )
        log.info(
            "%s",
            self.dpi_info,
        )

        self.ui_queue: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.worker: threading.Thread | None = None
        self.stop_controller: StopController | None = None
        self.current_inspection: RunInspection | None = None
        self.last_check_passed = False
        self.queue_log_handler: QueueLogHandler | None = None
        self.file_log_handler: logging.FileHandler | None = None

        self.status_var = tk.StringVar(value="未启动")
        self.config_var = tk.StringVar(value=str(self._default_config_path()))
        self.groups_var = tk.StringVar(value="data/groups.example.csv")
        self.db_var = tk.StringVar(value="data/wecom_rpa.sqlite3")
        self.log_file_var = tk.StringVar(value="logs/wecom_rpa.log")
        self.screenshot_dir_var = tk.StringVar(value="screenshots")
        self.mode_var = tk.StringVar(value="dry_run")
        self.max_total_var = tk.StringVar(value="")
        self.batch_size_var = tk.StringVar(value="")
        self.batch_interval_var = tk.StringVar(value="")
        self.confirm_real_var = tk.BooleanVar(value=False)
        self.confirm_review_var = tk.BooleanVar(value=False)
        self.source_ready_var = tk.BooleanVar(value=False)
        self.summary_var = tk.StringVar(value="尚未检查环境")
        self.progress_var = tk.StringVar(value="当前批次: -    已发送: 0    跳过: 0    uncertain: 0")
        self.latest_screenshot_var = tk.StringVar(value="最近截图: -")

        self._build_ui()
        self._append_log(self.dpi_info)
        self._set_running(False)
        self.root.after(100, self._process_ui_queue)

    def _configure_fonts(self) -> None:
        base_size = self.layout.base_font_size
        title_size = self.layout.title_font_size
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont", "TkCaptionFont", "TkSmallCaptionFont"):
            try:
                font = self.tkfont.nametofont(name)
                current_size = abs(int(font.cget("size")))
                if current_size < base_size:
                    font.configure(size=base_size)
            except Exception:
                continue
        self.title_font = ("", title_size, "bold")

    def _default_config_path(self) -> Path:
        preferred = Path("config/real_send_until_daxiaochen.yaml")
        if preferred.exists():
            return preferred
        return Path("config/config.example.yaml")

    def _build_ui(self) -> None:
        tk = self.tk
        ttk = self.ttk

        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=0)
        self.root.rowconfigure(2, weight=1)

        header = ttk.Frame(self.root, padding=(12, 10))
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="企业微信批量转发 RPA", font=self.title_font).grid(row=0, column=0, sticky="w")
        ttk.Label(header, textvariable=self.status_var).grid(row=0, column=1, sticky="e")

        middle = ttk.Frame(self.root, padding=(12, 0, 12, 8))
        middle.grid(row=1, column=0, sticky="nsew")
        middle.columnconfigure(0, weight=3)
        middle.columnconfigure(1, weight=2)
        middle.rowconfigure(0, weight=1)

        params = ttk.LabelFrame(middle, text="参数设置", padding=10)
        params.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        params.columnconfigure(1, weight=1)

        self._file_row(params, 0, "配置文件", self.config_var, self._browse_config)
        self._file_row(params, 1, "群列表 CSV", self.groups_var, self._browse_groups)
        self._file_row(params, 2, "状态库 DB", self.db_var, self._browse_db)
        self._file_row(params, 3, "日志文件", self.log_file_var, self._browse_log)
        self._file_row(params, 4, "截图目录", self.screenshot_dir_var, self._browse_screenshot_dir)

        mode_frame = ttk.Frame(params)
        mode_frame.grid(row=5, column=0, columnspan=3, sticky="w", pady=(10, 4))
        ttk.Label(mode_frame, text="运行模式").grid(row=0, column=0, padx=(0, 12))
        ttk.Radiobutton(mode_frame, text="Dry-run 自检", variable=self.mode_var, value="dry_run", command=self._invalidate_check).grid(row=0, column=1)
        ttk.Radiobutton(mode_frame, text="真实发送", variable=self.mode_var, value="real_send", command=self._invalidate_check).grid(row=0, column=2, padx=(12, 0))

        overrides = ttk.LabelFrame(params, text="覆盖参数", padding=8)
        overrides.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(8, 4))
        for column in range(6):
            overrides.columnconfigure(column, weight=1)
        ttk.Label(overrides, text="max_total_send").grid(row=0, column=0, sticky="w")
        ttk.Entry(overrides, textvariable=self.max_total_var, width=8).grid(row=0, column=1, sticky="w")
        ttk.Label(overrides, text="batch_size").grid(row=0, column=2, sticky="w")
        ttk.Entry(overrides, textvariable=self.batch_size_var, width=8).grid(row=0, column=3, sticky="w")
        ttk.Label(overrides, text="间隔秒").grid(row=0, column=4, sticky="w")
        ttk.Entry(overrides, textvariable=self.batch_interval_var, width=8).grid(row=0, column=5, sticky="w")

        confirm = ttk.LabelFrame(params, text="真实发送确认", padding=8)
        confirm.grid(row=7, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        ttk.Checkbutton(confirm, text="我理解这会真实发送企业微信消息", variable=self.confirm_real_var, command=self._invalidate_check).grid(row=0, column=0, sticky="w")
        ttk.Checkbutton(confirm, text="我已人工确认源消息和哨兵配置正确", variable=self.confirm_review_var, command=self._invalidate_check).grid(row=1, column=0, sticky="w")

        right = ttk.LabelFrame(middle, text="运行摘要 / 准备检查", padding=10)
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)
        right.rowconfigure(0, weight=1)
        ttk.Label(right, textvariable=self.summary_var, justify="left", anchor="nw").grid(row=0, column=0, sticky="nsew")
        ttk.Separator(right).grid(row=1, column=0, sticky="ew", pady=8)
        ttk.Checkbutton(right, text="已确认企业微信窗口可见，源消息已手动勾选", variable=self.source_ready_var, command=self._invalidate_check).grid(row=2, column=0, sticky="w")
        self.check_button = ttk.Button(right, text="检查环境", command=self._check_environment)
        self.check_button.grid(row=3, column=0, sticky="w", pady=(10, 0))

        log_frame = ttk.LabelFrame(self.root, text="运行日志", padding=8)
        log_frame.grid(row=2, column=0, sticky="nsew", padx=12, pady=(0, 8))
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(2, weight=1)
        ttk.Label(log_frame, textvariable=self.progress_var).grid(row=0, column=0, sticky="w")
        ttk.Label(log_frame, textvariable=self.latest_screenshot_var).grid(row=1, column=0, sticky="w")
        self.log_text = tk.Text(log_frame, height=10, wrap="word", state="disabled")
        self.log_text.grid(row=2, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        scrollbar.grid(row=2, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scrollbar.set)

        footer = ttk.Frame(self.root, padding=(12, 0, 12, 12))
        footer.grid(row=3, column=0, sticky="ew")
        footer.columnconfigure(4, weight=1)
        self.start_button = ttk.Button(footer, text="启动运行", command=self._start_run)
        self.start_button.grid(row=0, column=0, padx=(0, 8))
        self.stop_button = ttk.Button(footer, text="立即停止", command=self._request_stop, style="Danger.TButton")
        self.stop_button.grid(row=0, column=1, padx=(0, 8))
        ttk.Button(footer, text="打开日志目录", command=lambda: self._open_path(Path(self.log_file_var.get()).parent)).grid(row=0, column=2, padx=(0, 8))
        ttk.Button(footer, text="打开截图目录", command=lambda: self._open_path(Path(self.screenshot_dir_var.get()))).grid(row=0, column=3, padx=(0, 8))
        ttk.Button(footer, text="重新加载参数", command=self._check_environment).grid(row=0, column=5, padx=(0, 8))
        self.exit_button = ttk.Button(footer, text="退出", command=self._on_close)
        self.exit_button.grid(row=0, column=6)

        for var in (
            self.config_var,
            self.groups_var,
            self.db_var,
            self.log_file_var,
            self.screenshot_dir_var,
            self.max_total_var,
            self.batch_size_var,
            self.batch_interval_var,
        ):
            var.trace_add("write", lambda *_args: self._invalidate_check())

    def _file_row(self, parent: Any, row: int, label: str, variable: Any, command: Any) -> None:
        ttk = self.ttk
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=2)
        ttk.Entry(parent, textvariable=variable).grid(row=row, column=1, sticky="ew", padx=(8, 6), pady=2)
        ttk.Button(parent, text="浏览", command=command, width=6).grid(row=row, column=2, sticky="e", pady=2)

    def _browse_config(self) -> None:
        from tkinter import filedialog

        path = filedialog.askopenfilename(filetypes=[("YAML", "*.yaml *.yml"), ("All files", "*.*")])
        if path:
            self.config_var.set(path)

    def _browse_groups(self) -> None:
        from tkinter import filedialog

        path = filedialog.askopenfilename(filetypes=[("CSV", "*.csv"), ("All files", "*.*")])
        if path:
            self.groups_var.set(path)

    def _browse_db(self) -> None:
        from tkinter import filedialog

        path = filedialog.asksaveasfilename(defaultextension=".sqlite3", filetypes=[("SQLite", "*.sqlite3 *.db"), ("All files", "*.*")])
        if path:
            self.db_var.set(path)

    def _browse_log(self) -> None:
        from tkinter import filedialog

        path = filedialog.asksaveasfilename(defaultextension=".log", filetypes=[("Log", "*.log"), ("All files", "*.*")])
        if path:
            self.log_file_var.set(path)

    def _browse_screenshot_dir(self) -> None:
        from tkinter import filedialog

        path = filedialog.askdirectory()
        if path:
            self.screenshot_dir_var.set(path)

    def _options_from_form(self) -> GuiRunOptions:
        return GuiRunOptions(
            config_path=Path(self.config_var.get()),
            groups_path=Path(self.groups_var.get()),
            db_path=Path(self.db_var.get()),
            log_file=Path(self.log_file_var.get()),
            screenshot_dir=Path(self.screenshot_dir_var.get()),
            dry_run=self.mode_var.get() == "dry_run",
            max_total_send=self._optional_int(self.max_total_var.get()),
            batch_size=self._optional_int(self.batch_size_var.get()),
            batch_interval_sec=self._optional_float(self.batch_interval_var.get()),
            confirm_real_send=bool(self.confirm_real_var.get()),
            confirm_source_review=bool(self.confirm_review_var.get()),
        )

    def _optional_int(self, value: str) -> int | None:
        value = value.strip()
        if not value:
            return None
        try:
            return int(value)
        except ValueError as exc:
            raise ValueError(f"覆盖参数必须是整数：{value}") from exc

    def _optional_float(self, value: str) -> float | None:
        value = value.strip()
        if not value:
            return None
        try:
            return float(value)
        except ValueError as exc:
            raise ValueError(f"覆盖参数必须是数字：{value}") from exc

    def _check_environment(self) -> None:
        from tkinter import messagebox

        try:
            options = self._options_from_form()
            inspection = inspect_run_setup(options)
            window_found = WeComWindow(inspection.config.window.title_keyword, anchors=inspection.config.window.anchors).locate() is not None
            if not options.dry_run and not window_found:
                raise RuntimeError("真实发送前必须能找到企业微信窗口")
            if not self.source_ready_var.get():
                raise RuntimeError("请先勾选：已确认企业微信窗口可见，源消息已手动勾选")
            self.current_inspection = inspection
            self.last_check_passed = not inspection.has_uncertain
            self._render_summary(inspection, window_found=window_found)
            self._refresh_start_button()
            if inspection.has_uncertain:
                messagebox.showwarning("存在 uncertain", "状态库存在 uncertain 目标，请人工复查后再运行。")
            elif inspection.ocr_warning:
                messagebox.showwarning("OCR 检查提示", inspection.ocr_warning)
        except Exception as exc:
            self.current_inspection = None
            self.last_check_passed = False
            self.summary_var.set(f"检查失败：{exc}")
            self._refresh_start_button()
            messagebox.showerror("检查失败", str(exc))

    def _render_summary(self, inspection: RunInspection, *, window_found: bool) -> None:
        sentinel = inspection.config.recipient_selection.sentinel
        sentinel_text = "未启用"
        if sentinel.enabled:
            sentinel_text = "已启用 " + "、".join(sentinel.names)
        uncertain = "无" if not inspection.uncertain_targets else "、".join(inspection.uncertain_targets)
        ocr_text = inspection.ocr_warning or "无"
        self.summary_var.set(
            "\n".join(
                [
                    "配置状态: 已加载",
                    f"群数量: 去重后 {inspection.original_count} / 本次 {inspection.limited_count}",
                    f"运行模式: {'Dry-run 自检' if inspection.config.dry_run else '真实发送'}",
                    f"本次上限: {inspection.config.max_total_send}",
                    f"批次大小: {inspection.config.batch_size}",
                    f"批次间隔: {inspection.config.batch_interval_sec} 秒",
                    f"哨兵: {sentinel_text}",
                    f"uncertain: {uncertain}",
                    f"企业微信窗口: {'已找到' if window_found else '未找到'}",
                    f"OCR 提示: {ocr_text}",
                ]
            )
        )

    def _invalidate_check(self) -> None:
        self.last_check_passed = False
        self.current_inspection = None
        self._refresh_start_button()

    def _refresh_start_button(self) -> None:
        if self.worker and self.worker.is_alive():
            self.start_button.configure(state="disabled")
            return
        state = "normal" if self.last_check_passed and self.current_inspection and not self.current_inspection.has_uncertain else "disabled"
        self.start_button.configure(state=state)

    def _start_run(self) -> None:
        from tkinter import messagebox, simpledialog

        if not self.current_inspection or not self.last_check_passed:
            messagebox.showerror("尚未检查", "请先点击“检查环境”。")
            return
        options = self._options_from_form()
        if options.dry_run:
            if not messagebox.askyesno("启动 dry-run", "dry-run 不会点击最终发送按钮。确认启动？"):
                return
        else:
            typed = simpledialog.askstring("真实发送确认", "请输入 SEND 以启动真实发送：", show=None)
            if typed != "SEND":
                messagebox.showerror("确认失败", "未输入 SEND，真实发送已取消。")
                return

        self._clear_log()
        self.stop_controller = StopController(self.current_inspection.config.stop_hotkey)
        self._set_running(True)
        self.worker = threading.Thread(target=self._run_worker, args=(options, self.current_inspection, self.stop_controller), daemon=True)
        self.worker.start()

    def _run_worker(self, options: GuiRunOptions, inspection: RunInspection, stop_controller: StopController) -> None:
        self._install_logging(options.log_file)
        result: FlowResult | None = None
        try:
            with StateStore(options.db_path) as store:
                result = ForwardFlow(
                    inspection.config,
                    store,
                    screenshot_dir=str(options.screenshot_dir),
                    yes=False,
                    real_send_allowed=not inspection.config.dry_run,
                    stop_controller=stop_controller,
                    install_stop_hotkey=False,
                    confirm_callback=self._confirm_from_worker,
                    progress_callback=lambda event: self.ui_queue.put(("progress", event)),
                ).run(inspection.limited_groups)
            self.ui_queue.put(("finished", result))
        except Exception as exc:
            self.ui_queue.put(("failed", exc))
        finally:
            self._uninstall_logging()

    def _install_logging(self, log_file: Path) -> None:
        root_logger = logging.getLogger()
        root_logger.setLevel(logging.INFO)
        log_file.parent.mkdir(parents=True, exist_ok=True)
        self.queue_log_handler = QueueLogHandler(self.ui_queue)
        self.file_log_handler = logging.FileHandler(log_file, encoding="utf-8")
        self.file_log_handler.setLevel(logging.INFO)
        self.file_log_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root_logger.addHandler(self.queue_log_handler)
        root_logger.addHandler(self.file_log_handler)
        log.info("%s", self.dpi_info)

    def _uninstall_logging(self) -> None:
        root_logger = logging.getLogger()
        for handler in (self.queue_log_handler, self.file_log_handler):
            if handler is None:
                continue
            root_logger.removeHandler(handler)
            handler.close()
        self.queue_log_handler = None
        self.file_log_handler = None

    def _confirm_from_worker(self, prompt: str) -> bool:
        done = threading.Event()
        payload: dict[str, Any] = {"prompt": prompt, "answer": False, "done": done}
        self.ui_queue.put(("confirm", payload))
        if not done.wait(timeout=300):
            log.error("GUI 确认等待超时，流程停止：%s", prompt)
            return False
        return bool(payload["answer"])

    def _request_stop(self) -> None:
        self.status_var.set("状态: 正在强制停止")
        if self.stop_controller is not None:
            self.stop_controller.request_stop()
        terminate_active_powershell()
        os._exit(130)

    def _process_ui_queue(self) -> None:
        while True:
            try:
                kind, payload = self.ui_queue.get_nowait()
            except queue.Empty:
                break
            if kind == "log":
                self._append_log(str(payload))
            elif kind == "progress":
                self._handle_progress(payload)
            elif kind == "confirm":
                self._handle_confirm(payload)
            elif kind == "finished":
                self._handle_finished(payload)
            elif kind == "failed":
                self._handle_failed(payload)
        self.root.after(100, self._process_ui_queue)

    def _handle_progress(self, event: dict[str, Any]) -> None:
        name = event.get("event")
        summary = event.get("summary") or {}
        if name == "run_started":
            self.status_var.set("状态: 运行中")
        elif name in {"batch_started", "batch_finished"}:
            batch_no = event.get("batch_no", "-")
            total_batches = event.get("total_batches", "-")
            sent = summary.get("sent", 0)
            skipped = summary.get("skipped", 0)
            uncertain = summary.get("uncertain", 0)
            self.progress_var.set(f"当前批次: {batch_no} / {total_batches}    已发送: {sent}    跳过: {skipped}    uncertain: {uncertain}")

    def _handle_confirm(self, payload: dict[str, Any]) -> None:
        from tkinter import messagebox

        payload["answer"] = messagebox.askyesno("运行确认", f"{payload['prompt']}\n\n确认继续？")
        payload["done"].set()

    def _handle_finished(self, result: FlowResult) -> None:
        from tkinter import messagebox

        self._set_running(False)
        self.status_var.set(f"状态: {result.status}")
        self.progress_var.set(f"运行完成: run_id={result.run_id} summary={result.summary}")
        messagebox.showinfo("运行完成", f"run_id={result.run_id}\nstatus={result.status}\nsummary={result.summary}")

    def _handle_failed(self, exc: Exception) -> None:
        from tkinter import messagebox

        self._set_running(False)
        self.status_var.set("状态: 失败")
        messagebox.showerror("运行失败", str(exc))

    def _set_running(self, running: bool) -> None:
        state = "disabled" if running else "normal"
        self.check_button.configure(state=state)
        self.stop_button.configure(state="normal" if running else "disabled")
        self.exit_button.configure(state="disabled" if running else "normal")
        if running:
            self.start_button.configure(state="disabled")
        else:
            self._refresh_start_button()

    def _append_log(self, line: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", line + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")
        if "screenshots" in line:
            self.latest_screenshot_var.set(f"最近截图: {line}")

    def _clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _open_path(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True) if path.suffix == "" else path.parent.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            os.startfile(path)  # type: ignore[attr-defined]
            return
        opener = "open" if os.uname().sysname == "Darwin" else "xdg-open"
        subprocess.Popen([opener, str(path)])

    def _on_close(self) -> None:
        from tkinter import messagebox

        if self.worker and self.worker.is_alive():
            if messagebox.askyesno("正在运行", "任务仍在运行，是否请求停止？"):
                self._request_stop()
            return
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def main() -> int:
    app = WeComRpaApp()
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
