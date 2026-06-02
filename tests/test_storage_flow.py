from pathlib import Path
import tempfile
import unittest

from PIL import Image

from wecom_rpa.config import AppConfig, RecipientSelectionConfig, SentinelConfig
from wecom_rpa.forward_flow import ForwardFlow, SelectedRecipient
from wecom_rpa.groups import load_groups_csv
from wecom_rpa.models import TargetGroup
from wecom_rpa.screen import OcrLine
from wecom_rpa.storage import StateStore
from wecom_rpa.wecom_window import WindowRect


class StorageFlowTest(unittest.TestCase):
    def test_dry_run_flow_marks_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            groups_csv = root / "groups.csv"
            groups_csv.write_text("group_name\nA\nB\nC\n", encoding="utf-8")
            groups = load_groups_csv(groups_csv)
            cfg = AppConfig(
                max_total_send=3,
                batch_size=2,
                batch_interval_sec=0,
                dry_run=True,
                require_confirm_before_start=False,
                require_confirm_first_batch=False,
            )
            with StateStore(root / "state.sqlite3") as store:
                result = ForwardFlow(cfg, store, screenshot_dir=str(root / "screenshots"), yes=True).run(groups)
                self.assertEqual(result.status, "completed")
                self.assertEqual(result.summary, {"skipped": 3})
                self.assertEqual(store.get_status("A"), "skipped")

    def test_real_send_flow_requires_runtime_permission(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            groups_csv = root / "groups.csv"
            groups_csv.write_text("group_name\nA\n", encoding="utf-8")
            groups = load_groups_csv(groups_csv)
            cfg = AppConfig(
                max_total_send=1,
                batch_size=1,
                batch_interval_sec=0,
                dry_run=False,
                require_confirm_before_start=False,
                require_confirm_first_batch=False,
            )

            class NoopRealFlow(ForwardFlow):
                def _run_batch(self, batch_no, targets):
                    for target in targets:
                        self.store.set_status(target.group_name, "sent", batch_no=batch_no)

            with StateStore(root / "state.sqlite3") as store:
                with self.assertRaisesRegex(RuntimeError, "真实发送"):
                    flow = NoopRealFlow(cfg, store, screenshot_dir=str(root / "screenshots"), yes=True)
                    flow.window.locate = lambda: None
                    flow.run(groups)

            with StateStore(root / "state2.sqlite3") as store:
                flow = NoopRealFlow(
                    cfg,
                    store,
                    screenshot_dir=str(root / "screenshots2"),
                    yes=True,
                    real_send_allowed=True,
                )
                flow.window.locate = lambda: None
                result = flow.run(groups)
                self.assertEqual(result.summary, {"sent": 1})

    def test_real_send_batches_after_first_reselect_source_messages(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            groups_csv = root / "groups.csv"
            groups_csv.write_text("group_name\nA\nB\nC\n", encoding="utf-8")
            groups = load_groups_csv(groups_csv)
            cfg = AppConfig(
                max_total_send=3,
                batch_size=2,
                batch_interval_sec=0,
                dry_run=False,
                require_confirm_before_start=False,
                require_confirm_first_batch=False,
            )

            class CountingRealFlow(ForwardFlow):
                def __init__(self, *args, **kwargs):
                    super().__init__(*args, **kwargs)
                    self.reselect_calls = 0

                def _run_real_bottom_picker_batch(self, batch_no, targets):
                    if batch_no > 1:
                        self.reselect_calls += 1
                    for target in targets:
                        self.store.set_status(target.group_name, "sent", batch_no=batch_no)

            with StateStore(root / "state.sqlite3") as store:
                flow = CountingRealFlow(
                    cfg,
                    store,
                    screenshot_dir=str(root / "screenshots"),
                    yes=True,
                    real_send_allowed=True,
                )
                flow.window.locate = lambda: None
                result = flow.run(groups)
                self.assertEqual(result.summary, {"sent": 3})
                self.assertEqual(flow.reselect_calls, 1)

    def test_resume_skips_completed_targets_and_blocks_uncertain(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            groups = [TargetGroup("A"), TargetGroup("B"), TargetGroup("C")]
            cfg = AppConfig(
                max_total_send=3,
                batch_size=3,
                batch_interval_sec=0,
                dry_run=True,
                require_confirm_before_start=False,
                require_confirm_first_batch=False,
            )

            class RecordingFlow(ForwardFlow):
                def __init__(self, *args, **kwargs):
                    super().__init__(*args, **kwargs)
                    self.processed = []

                def _run_batch(self, batch_no, targets):
                    self.processed.extend(target.group_name for target in targets)
                    for target in targets:
                        self.store.set_status(target.group_name, "skipped", batch_no=batch_no)

            with StateStore(root / "state.sqlite3") as store:
                store.upsert_targets(groups)
                store.set_status("A", "sent", batch_no=1)
                store.set_status("B", "skipped", batch_no=1)
                flow = RecordingFlow(cfg, store, screenshot_dir=str(root / "screenshots"), yes=True)
                result = flow.run(groups)
                self.assertEqual(flow.processed, ["C"])
                self.assertEqual(result.summary, {"sent": 1, "skipped": 2})

            with StateStore(root / "state_uncertain.sqlite3") as store:
                store.upsert_targets(groups)
                store.set_status("A", "uncertain", batch_no=1, error="发送后状态不明确")
                flow = RecordingFlow(cfg, store, screenshot_dir=str(root / "screenshots2"), yes=True)
                with self.assertRaisesRegex(RuntimeError, "uncertain"):
                    flow.run(groups)

    def test_partial_real_batch_uses_bottom_recipient_rows(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cfg = AppConfig(
                max_total_send=2,
                batch_size=9,
                batch_interval_sec=0,
                dry_run=False,
                require_confirm_before_start=False,
                require_confirm_first_batch=False,
            )

            class FakeWindow:
                def __init__(self):
                    self.clicks = []

                def locate(self):
                    from wecom_rpa.wecom_window import WindowRect
                    return WindowRect(0, 0, 1000, 1000)

                def click_relative(self, _rect, x_ratio, y_ratio):
                    self.clicks.append((round(x_ratio, 3), round(y_ratio, 3)))
                    return True

                def mouse_wheel_relative(self, *_args):
                    return True

                def send_keys(self, _keys):
                    return True

                def right_click_relative(self, *_args):
                    return True

                def send_keys(self, *_args):
                    return True

            with StateStore(root / "state.sqlite3") as store:
                flow = ForwardFlow(cfg, store, screenshot_dir=str(root / "screenshots"), yes=True, real_send_allowed=True)
                fake = FakeWindow()
                flow.window = fake
                flow._assert_exact_source_selection = lambda *_args, **_kwargs: None
                flow._open_recipient_picker_from_source = lambda *_args, **_kwargs: None
                flow._click_recipient_checkbox_until_selected = lambda rect, x, y, _index: flow.window.click_relative(rect, x, y)
                flow._run_real_bottom_picker_batch(1, [TargetGroup("A"), TargetGroup("B")])
                recipient_clicks = [click for click in fake.clicks if click[0] == 0.260]
                self.assertEqual(recipient_clicks, [(0.260, 0.819), (0.260, 0.757)])

    def test_sentinel_trim_keeps_recipients_below_boundary(self):
        cfg = AppConfig(
            max_total_send=9,
            batch_size=9,
            dry_run=True,
            recipient_selection=RecipientSelectionConfig(
                sentinel=SentinelConfig(enabled=True, names=["员工1", "员工2", "员工3"])
            ),
        )
        flow = ForwardFlow(cfg, store=None)  # type: ignore[arg-type]
        selected = [
            SelectedRecipient("群A"),
            SelectedRecipient("群B"),
            SelectedRecipient("员工1"),
            SelectedRecipient("员工2"),
            SelectedRecipient("员工3"),
        ]

        plan = flow._build_sentinel_trim_plan(selected)

        self.assertTrue(plan.boundary_reached)
        self.assertEqual([item.name for item in plan.send], ["群A", "群B"])
        self.assertEqual([item.name for item in plan.remove], ["员工1", "员工2", "员工3"])

    def test_sentinel_dry_run_stops_after_boundary_batch(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            groups_csv = root / "groups.csv"
            groups_csv.write_text("group_name\n群A\n群B\n员工1\n员工2\n员工3\n群C\n", encoding="utf-8")
            groups = load_groups_csv(groups_csv)
            cfg = AppConfig(
                max_total_send=6,
                batch_size=6,
                batch_interval_sec=0,
                dry_run=True,
                require_confirm_before_start=False,
                require_confirm_first_batch=False,
                recipient_selection=RecipientSelectionConfig(
                    sentinel=SentinelConfig(enabled=True, names=["员工1", "员工2", "员工3"])
                ),
            )

            with StateStore(root / "state.sqlite3") as store:
                flow = ForwardFlow(cfg, store, screenshot_dir=str(root / "screenshots"), yes=True)
                result = flow.run(groups)
                self.assertEqual(result.status, "completed")
                self.assertTrue(flow.boundary_reached)
                self.assertEqual(store.get_status("群A"), "skipped")
                self.assertEqual(store.get_status("群B"), "skipped")
                self.assertEqual(store.get_status("员工1"), "skipped")
                self.assertEqual(store.get_status("群C"), "skipped")

    def test_boundary_marks_later_batches_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cfg = AppConfig(
                max_total_send=5,
                batch_size=2,
                batch_interval_sec=0,
                dry_run=True,
                require_confirm_before_start=False,
                require_confirm_first_batch=False,
            )
            groups = [TargetGroup(f"slot{i}") for i in range(1, 6)]

            with StateStore(root / "state.sqlite3") as store:
                flow = ForwardFlow(cfg, store, screenshot_dir=str(root / "screenshots"), yes=True)
                flow.window.locate = lambda: None

                def stop_after_first_batch(batch_no, targets):
                    self.assertEqual(batch_no, 1)
                    for target in targets:
                        store.set_status(target.group_name, "skipped", batch_no=batch_no, error="first")
                    flow.boundary_reached = True

                flow._run_batch = stop_after_first_batch
                flow.run(groups)

                self.assertEqual(store.get_status("slot1"), "skipped")
                self.assertEqual(store.get_status("slot2"), "skipped")
                self.assertEqual(store.get_status("slot3"), "skipped")
                self.assertEqual(store.get_status("slot4"), "skipped")
                self.assertEqual(store.get_status("slot5"), "skipped")

    def test_real_sentinel_trim_removes_boundary_and_sends_lower_items(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cfg = AppConfig(
                max_total_send=5,
                batch_size=5,
                batch_interval_sec=0,
                dry_run=False,
                require_confirm_before_start=False,
                require_confirm_first_batch=False,
                recipient_selection=RecipientSelectionConfig(
                    sentinel=SentinelConfig(enabled=True, names=["员工1", "员工2", "员工3"])
                ),
            )

            class FakeWindow:
                def __init__(self):
                    self.clicks = []
                    self.keys = []

                def locate(self):
                    from wecom_rpa.wecom_window import WindowRect
                    return WindowRect(0, 0, 1000, 1000)

                def click_relative(self, _rect, x_ratio, y_ratio):
                    self.clicks.append((round(x_ratio, 3), round(y_ratio, 3)))
                    return True

                def mouse_wheel_relative(self, *_args):
                    return True

                def send_keys(self, _keys):
                    return True

                def right_click_relative(self, *_args):
                    return True

                def send_keys(self, keys):
                    self.keys.append(keys)
                    return True

            class FakeScreen:
                def save_checkpoint(self, *_args, **_kwargs):
                    return root / "fake.png"

                def ocr_lines(self, *_args, **_kwargs):
                    return []

            with StateStore(root / "state.sqlite3") as store:
                targets = [TargetGroup("slot1"), TargetGroup("slot2"), TargetGroup("slot3"), TargetGroup("slot4"), TargetGroup("slot5")]
                store.upsert_targets(targets)
                flow = ForwardFlow(cfg, store, screenshot_dir=str(root / "screenshots"), yes=True, real_send_allowed=True)
                flow.window = FakeWindow()
                flow.screen = FakeScreen()
                flow._assert_exact_source_selection = lambda *_args, **_kwargs: None
                flow._open_recipient_picker_from_source = lambda *_args, **_kwargs: None
                flow._click_recipient_checkbox_until_selected = lambda rect, x, y, _index: flow.window.click_relative(rect, x, y)
                flow._read_left_selected_recipients = lambda _rect, _rows: [
                    SelectedRecipient("群A", 0.819),
                    SelectedRecipient("群B", 0.757),
                    SelectedRecipient("员工1", 0.694),
                    SelectedRecipient("员工2", 0.632),
                    SelectedRecipient("员工3", 0.569),
                ]
                flow._click_recipient_checkbox_until_unselected = lambda rect, x, y, _name: flow.window.click_relative(rect, x, y)
                flow._verify_left_selection_count = lambda _rect, rows, expected: len(rows) == expected

                flow._run_real_bottom_picker_batch(1, targets)

                recipient_clicks = [click for click in flow.window.clicks if click[0] == 0.260]
                remove_clicks = recipient_clicks[5:]
                self.assertEqual(remove_clicks, [(0.260, 0.694), (0.260, 0.632), (0.260, 0.569)])
                self.assertIn((0.574, 0.801), flow.window.clicks)
                self.assertEqual(store.get_status("slot1"), "sent")
                self.assertEqual(store.get_status("slot2"), "sent")
                self.assertEqual(store.get_status("slot3"), "skipped")
                self.assertTrue(flow.boundary_reached)

    def test_real_sentinel_trim_stops_when_post_trim_verification_fails(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cfg = AppConfig(
                max_total_send=5,
                batch_size=5,
                batch_interval_sec=0,
                dry_run=False,
                require_confirm_before_start=False,
                require_confirm_first_batch=False,
                recipient_selection=RecipientSelectionConfig(
                    sentinel=SentinelConfig(enabled=True, names=["员工1", "员工2", "员工3"])
                ),
            )

            class FakeWindow:
                def __init__(self):
                    self.clicks = []

                def locate(self):
                    from wecom_rpa.wecom_window import WindowRect
                    return WindowRect(0, 0, 1000, 1000)

                def click_relative(self, _rect, x_ratio, y_ratio):
                    self.clicks.append((round(x_ratio, 3), round(y_ratio, 3)))
                    return True

                def mouse_wheel_relative(self, *_args):
                    return True

                def send_keys(self, _keys):
                    return True

            class FakeScreen:
                def save_checkpoint(self, *_args, **_kwargs):
                    return root / "fake.png"

            with StateStore(root / "state.sqlite3") as store:
                targets = [TargetGroup("slot1"), TargetGroup("slot2"), TargetGroup("slot3"), TargetGroup("slot4"), TargetGroup("slot5")]
                store.upsert_targets(targets)
                flow = ForwardFlow(cfg, store, screenshot_dir=str(root / "screenshots"), yes=True, real_send_allowed=True)
                flow.window = FakeWindow()
                flow.screen = FakeScreen()
                flow._assert_exact_source_selection = lambda *_args, **_kwargs: None
                flow._open_recipient_picker_from_source = lambda *_args, **_kwargs: None
                flow._click_recipient_checkbox_until_selected = lambda rect, x, y, _index: flow.window.click_relative(rect, x, y)
                flow._read_left_selected_recipients = lambda _rect, _rows: [
                    SelectedRecipient("群A", 0.819),
                    SelectedRecipient("群B", 0.757),
                    SelectedRecipient("员工1", 0.694),
                    SelectedRecipient("员工2", 0.632),
                    SelectedRecipient("员工3", 0.569),
                ]
                flow._click_recipient_checkbox_until_unselected = lambda rect, x, y, _name: flow.window.click_relative(rect, x, y)
                flow._verify_left_selection_count = lambda _rect, rows, expected: False

                with self.assertRaisesRegex(RuntimeError, "截断后"):
                    flow._run_real_bottom_picker_batch(1, targets)

                self.assertNotIn((0.574, 0.801), flow.window.clicks)
                self.assertEqual(store.get_status("slot1"), "uncertain")
                self.assertEqual(store.get_status("slot2"), "uncertain")

    def test_real_sentinel_boundary_without_sendable_items_cancels(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cfg = AppConfig(
                max_total_send=2,
                batch_size=2,
                batch_interval_sec=0,
                dry_run=False,
                require_confirm_before_start=False,
                require_confirm_first_batch=False,
                recipient_selection=RecipientSelectionConfig(
                    sentinel=SentinelConfig(enabled=True, names=["员工1"])
                ),
            )

            class FakeWindow:
                def __init__(self):
                    self.clicks = []
                    self.keys = []

                def locate(self):
                    from wecom_rpa.wecom_window import WindowRect
                    return WindowRect(0, 0, 1000, 1000)

                def click_relative(self, _rect, x_ratio, y_ratio):
                    self.clicks.append((round(x_ratio, 3), round(y_ratio, 3)))
                    return True

                def mouse_wheel_relative(self, *_args):
                    return True

                def send_keys(self, _keys):
                    return True

                def send_keys(self, keys):
                    self.keys.append(keys)
                    return True

            class FakeScreen:
                def save_checkpoint(self, *_args, **_kwargs):
                    return root / "fake.png"

                def ocr_lines(self, *_args, **_kwargs):
                    return []

            with StateStore(root / "state.sqlite3") as store:
                targets = [TargetGroup("slot1"), TargetGroup("slot2")]
                store.upsert_targets(targets)
                flow = ForwardFlow(cfg, store, screenshot_dir=str(root / "screenshots"), yes=True, real_send_allowed=True)
                flow.window = FakeWindow()
                flow.screen = FakeScreen()
                flow._assert_exact_source_selection = lambda *_args, **_kwargs: None
                flow._open_recipient_picker_from_source = lambda *_args, **_kwargs: None
                flow._click_recipient_checkbox_until_selected = lambda rect, x, y, _index: flow.window.click_relative(rect, x, y)
                flow._read_left_selected_recipients = lambda _rect, _rows: [SelectedRecipient("员工1", 0.819), SelectedRecipient("群A", 0.757)]

                flow._run_real_bottom_picker_batch(1, targets)

                self.assertIn("{ESC}", flow.window.keys)
                self.assertNotIn((0.574, 0.801), flow.window.clicks)
                self.assertEqual(store.get_status("slot1"), "skipped")
                self.assertEqual(store.get_status("slot2"), "skipped")

    def test_real_sentinel_stops_when_left_selection_cannot_be_read(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cfg = AppConfig(
                max_total_send=2,
                batch_size=2,
                batch_interval_sec=0,
                dry_run=False,
                require_confirm_before_start=False,
                require_confirm_first_batch=False,
                recipient_selection=RecipientSelectionConfig(
                    sentinel=SentinelConfig(enabled=True, names=["大小尘"])
                ),
            )

            class FakeWindow:
                def __init__(self):
                    self.clicks = []

                def locate(self):
                    from wecom_rpa.wecom_window import WindowRect
                    return WindowRect(0, 0, 1000, 1000)

                def click_relative(self, _rect, x_ratio, y_ratio):
                    self.clicks.append((round(x_ratio, 3), round(y_ratio, 3)))
                    return True

                def mouse_wheel_relative(self, *_args):
                    return True

                def send_keys(self, _keys):
                    return True

            class FakeScreen:
                def save_checkpoint(self, *_args, **_kwargs):
                    return root / "fake.png"

            with StateStore(root / "state.sqlite3") as store:
                targets = [TargetGroup("slot1"), TargetGroup("slot2")]
                store.upsert_targets(targets)
                flow = ForwardFlow(cfg, store, screenshot_dir=str(root / "screenshots"), yes=True, real_send_allowed=True)
                flow.window = FakeWindow()
                flow.screen = FakeScreen()
                flow._assert_exact_source_selection = lambda *_args, **_kwargs: None
                flow._open_recipient_picker_from_source = lambda *_args, **_kwargs: None
                flow._click_recipient_checkbox_until_selected = lambda rect, x, y, _index: flow.window.click_relative(rect, x, y)
                flow._read_left_selected_recipients = lambda _rect, _rows: []

                with self.assertRaisesRegex(RuntimeError, "左侧已勾选"):
                    flow._run_real_bottom_picker_batch(1, targets)

                self.assertNotIn((0.574, 0.801), flow.window.clicks)
                self.assertEqual(store.get_status("slot1"), "pending")
                self.assertEqual(store.get_status("slot2"), "pending")

    def test_real_sentinel_stops_on_partially_visible_left_selection(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cfg = AppConfig(
                max_total_send=3,
                batch_size=3,
                batch_interval_sec=0,
                dry_run=False,
                require_confirm_before_start=False,
                require_confirm_first_batch=False,
                recipient_selection=RecipientSelectionConfig(
                    sentinel=SentinelConfig(enabled=True, names=["大小尘"])
                ),
            )

            class FakeWindow:
                def __init__(self):
                    self.clicks = []

                def locate(self):
                    return WindowRect(0, 0, 1000, 1000)

                def click_relative(self, _rect, x_ratio, y_ratio):
                    self.clicks.append((round(x_ratio, 3), round(y_ratio, 3)))
                    return True

                def mouse_wheel_relative(self, *_args):
                    return True

                def send_keys(self, _keys):
                    return True

            class FakeScreen:
                def save_checkpoint(self, *_args, **_kwargs):
                    return root / "fake.png"

            with StateStore(root / "state.sqlite3") as store:
                targets = [TargetGroup("slot1"), TargetGroup("slot2"), TargetGroup("slot3")]
                store.upsert_targets(targets)
                flow = ForwardFlow(cfg, store, screenshot_dir=str(root / "screenshots"), yes=True, real_send_allowed=True)
                flow.window = FakeWindow()
                flow.screen = FakeScreen()
                flow._assert_exact_source_selection = lambda *_args, **_kwargs: None
                flow._open_recipient_picker_from_source = lambda *_args, **_kwargs: None
                flow._click_recipient_checkbox_until_selected = lambda rect, x, y, _index: flow.window.click_relative(rect, x, y)
                flow._read_left_selected_recipients = lambda _rect, _rows: [
                    SelectedRecipient("群A", 0.819),
                    SelectedRecipient("群B", 0.757),
                ]

                with self.assertRaisesRegex(RuntimeError, "数量与预期不一致"):
                    flow._run_real_bottom_picker_batch(1, targets)

                self.assertNotIn((0.574, 0.801), flow.window.clicks)
                self.assertEqual(store.get_status("slot1"), "uncertain")
                self.assertEqual(store.get_status("slot2"), "uncertain")
                self.assertEqual(store.get_status("slot3"), "uncertain")

    def test_sentinel_does_not_fall_back_to_right_selected_list_when_left_is_clipped(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cfg = AppConfig(
                max_total_send=9,
                batch_size=9,
                batch_interval_sec=0,
                dry_run=False,
                require_confirm_before_start=False,
                require_confirm_first_batch=False,
                recipient_selection=RecipientSelectionConfig(
                    sentinel=SentinelConfig(enabled=True, names=["大小尘"])
                ),
            )

            with StateStore(root / "state.sqlite3") as store:
                flow = ForwardFlow(cfg, store, screenshot_dir=str(root / "screenshots"), yes=True, real_send_allowed=True)
                flow._read_left_selected_recipients = lambda _rect, _rows: [
                    SelectedRecipient("大小尘", 0.819),
                    SelectedRecipient("酒稚一家", 0.757),
                ]
                flow._read_selected_recipients = lambda _rect, _expected: [
                    SelectedRecipient("1", 0.290),
                    SelectedRecipient("大小尘", 0.360),
                    SelectedRecipient("酒稚一家", 0.430),
                ]

                selected = flow._read_ordered_selected_recipients(WindowRect(0, 0, 1000, 1000), [0.819] * 9, 9)
                plan = flow._build_sentinel_trim_plan(selected)

                self.assertEqual([item.name for item in selected], ["大小尘", "酒稚一家"])
                self.assertEqual([item.name for item in plan.send], [])
                self.assertEqual([item.name for item in plan.remove], ["大小尘", "酒稚一家"])

    def test_left_checkbox_detection_ignores_blue_member_tags(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cfg = AppConfig(max_total_send=1, batch_size=1, dry_run=True)

            class FakeScreen:
                def save_checkpoint(self, *_args, **_kwargs):
                    return root / "fake.png"

                def find_selected_checkbox_ratios(self, *_args, **_kwargs):
                    return [
                        (0.206, 0.880),  # real checkbox column
                        (0.319, 0.863),  # blue "全员" tag; must be ignored
                    ]

            with StateStore(root / "state.sqlite3") as store:
                flow = ForwardFlow(cfg, store, screenshot_dir=str(root / "screenshots"), yes=True)
                flow.screen = FakeScreen()

                selected_y = flow._left_selected_checkbox_y_ratios(WindowRect(0, 0, 1600, 900), "probe")

                self.assertEqual(len(selected_y), 1)
                self.assertAlmostEqual(selected_y[0], 0.909, places=2)

    def test_reselect_source_messages_requires_visible_selected_checkboxes(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cfg = AppConfig(
                max_total_send=2,
                batch_size=1,
                batch_interval_sec=0,
                dry_run=False,
                require_confirm_before_start=False,
                require_confirm_first_batch=False,
            )

            class FakeWindow:
                def __init__(self):
                    self.clicks = []

                def locate(self):
                    return None

                def click_relative(self, _rect, x_ratio, y_ratio):
                    self.clicks.append((round(x_ratio, 3), round(y_ratio, 3)))
                    return True

                def click_screen(self, *_args):
                    return True

                def right_click_relative(self, *_args):
                    return True

                def send_keys(self, *_args):
                    return True

            class FakeScreen:
                def save_checkpoint(self, *_args, **_kwargs):
                    return root / "source.png"

                def find_selected_checkbox_ratios(self, *_args, **_kwargs):
                    return [(0.100, 0.100)]

                def ocr_lines(self, *_args, **_kwargs):
                    return [OcrLine("多选", 10, 10, 40, 20)]

            with StateStore(root / "state.sqlite3") as store:
                flow = ForwardFlow(cfg, store, screenshot_dir=str(root / "screenshots"), yes=True, real_send_allowed=True)
                flow.window = FakeWindow()
                flow.screen = FakeScreen()
                flow.source_checkbox_x_ratio = 0.260
                flow.source_checkbox_y_ratios = [0.500, 0.600]

                with self.assertRaisesRegex(RuntimeError, "源消息多选"):
                    flow._reselect_source_messages(WindowRect(0, 0, 1000, 1000))

    def test_reselect_source_messages_right_clicks_recorded_source_row_not_midpoint(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cfg = AppConfig(
                max_total_send=2,
                batch_size=1,
                batch_interval_sec=0,
                dry_run=False,
                require_confirm_before_start=False,
                require_confirm_first_batch=False,
            )

            class FakeWindow:
                def __init__(self):
                    self.right_clicks = []
                    self.clicks = []
                    self.screen_clicks = []

                def locate(self):
                    return None

                def click_relative(self, _rect, x_ratio, y_ratio):
                    self.clicks.append((round(x_ratio, 3), round(y_ratio, 3)))
                    return True

                def click_screen(self, x, y):
                    self.screen_clicks.append((x, y))
                    return True

                def right_click_relative(self, _rect, x_ratio, y_ratio):
                    self.right_clicks.append((round(x_ratio, 3), round(y_ratio, 3)))
                    return True

                def send_keys(self, *_args):
                    return True

            class FakeScreen:
                def save_checkpoint(self, *_args, **_kwargs):
                    return root / "source.png"

                def find_selected_checkbox_ratios(self, *_args, **_kwargs):
                    return [(0.403, 0.645), (0.403, 0.887)]

                def ocr_lines(self, *_args, **_kwargs):
                    return [OcrLine("多选", 100, 200, 60, 20)]

            with StateStore(root / "state.sqlite3") as store:
                flow = ForwardFlow(cfg, store, screenshot_dir=str(root / "screenshots"), yes=True, real_send_allowed=True)
                flow.window = FakeWindow()
                flow.screen = FakeScreen()
                flow.source_checkbox_x_ratio = 0.403
                flow.source_checkbox_y_ratios = [0.887, 0.645]

                flow._reselect_source_messages(WindowRect(0, 0, 1000, 1000))

                self.assertEqual(flow.window.right_clicks, [(0.65, 0.52)])
                self.assertEqual(flow.window.screen_clicks, [(130, 210), (403, 887)])
                self.assertEqual(flow.window.clicks, [])

    def test_context_menu_match_allows_paddle_prefix_noise_on_right_menu(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            menu_image = root / "menu.png"
            Image.new("RGB", (1600, 900), "white").save(menu_image)

            cfg = AppConfig(max_total_send=1, batch_size=1, require_confirm_before_start=False, require_confirm_first_batch=False)

            class FakeScreen:
                def ocr_lines(self, *_args, **_kwargs):
                    return [
                        OcrLine("这里也提到了多选但不是菜单", 100, 100, 500, 30),
                        OcrLine("三多选", 1518, 680, 70, 23),
                    ]

            with StateStore(root / "state.sqlite3") as store:
                flow = ForwardFlow(cfg, store, screenshot_dir=str(root / "screenshots"), yes=True, real_send_allowed=True)
                flow.screen = FakeScreen()

                line = flow._find_context_menu_line(menu_image, "多选")

                self.assertIsNotNone(line)
                self.assertEqual(line.left, 1518)

    def test_source_selection_rejects_extra_selected_checkbox(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cfg = AppConfig(
                max_total_send=2,
                batch_size=1,
                batch_interval_sec=0,
                dry_run=False,
                require_confirm_before_start=False,
                require_confirm_first_batch=False,
            )

            class FakeScreen:
                def find_selected_checkbox_ratios(self, *_args, **_kwargs):
                    return [(0.403, 0.887), (0.403, 0.645), (0.403, 0.402)]

            with StateStore(root / "state.sqlite3") as store:
                flow = ForwardFlow(cfg, store, screenshot_dir=str(root / "screenshots"), yes=True, real_send_allowed=True)
                flow.screen = FakeScreen()
                flow.source_checkbox_y_ratios = [0.887, 0.645]

                with self.assertRaisesRegex(RuntimeError, "数量不是 2 个"):
                    flow._record_exact_source_selection(root / "source.png")

    def test_real_send_marks_uncertain_when_post_send_evidence_missing(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cfg = AppConfig(
                max_total_send=1,
                batch_size=1,
                batch_interval_sec=0,
                dry_run=False,
                require_confirm_before_start=False,
                require_confirm_first_batch=False,
            )

            class FakeWindow:
                def __init__(self):
                    self.clicks = []

                def locate(self):
                    from wecom_rpa.wecom_window import WindowRect
                    return WindowRect(0, 0, 1000, 1000)

                def click_relative(self, _rect, x_ratio, y_ratio):
                    self.clicks.append((round(x_ratio, 3), round(y_ratio, 3)))
                    return True

                def mouse_wheel_relative(self, *_args):
                    return True

                def send_keys(self, _keys):
                    return True

            class FakeScreen:
                def save_checkpoint(self, name, **_kwargs):
                    if "post_send" in name:
                        return root / "post_send.placeholder.txt"
                    return root / "fake.png"

            with StateStore(root / "state.sqlite3") as store:
                targets = [TargetGroup("slot1")]
                store.upsert_targets(targets)
                flow = ForwardFlow(cfg, store, screenshot_dir=str(root / "screenshots"), yes=True, real_send_allowed=True)
                flow.window = FakeWindow()
                flow.screen = FakeScreen()
                flow._assert_exact_source_selection = lambda *_args, **_kwargs: None
                flow._open_recipient_picker_from_source = lambda *_args, **_kwargs: None
                flow._click_recipient_checkbox_until_selected = lambda rect, x, y, _index: flow.window.click_relative(rect, x, y)

                with self.assertRaisesRegex(RuntimeError, "发送后截图"):
                    flow._run_real_bottom_picker_batch(1, targets)

                self.assertIn((0.574, 0.801), flow.window.clicks)
                self.assertEqual(store.get_status("slot1"), "uncertain")


if __name__ == "__main__":
    unittest.main()
