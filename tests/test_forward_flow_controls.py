from __future__ import annotations

from unittest import mock
import unittest

from wecom_rpa.config import AppConfig
from wecom_rpa.forward_flow import ForwardFlow
from wecom_rpa.safety import StopController


class ForwardFlowControlsTest(unittest.TestCase):
    def test_accepts_external_stop_controller(self):
        stop = StopController("ctrl+alt+q")
        flow = ForwardFlow(
            AppConfig(max_total_send=1),
            mock.Mock(),
            stop_controller=stop,
            install_stop_hotkey=False,
        )

        self.assertIs(flow.stop, stop)
        stop.request_stop()
        self.assertTrue(flow.stop.should_stop())

    def test_confirm_callback_can_cancel_flow_prompt(self):
        flow = ForwardFlow(
            AppConfig(max_total_send=1),
            mock.Mock(),
            confirm_callback=lambda prompt: False,
            install_stop_hotkey=False,
        )

        with self.assertRaisesRegex(RuntimeError, "用户未确认"):
            flow._confirm("确认测试")

    def test_progress_callback_receives_batch_events_in_dry_run(self):
        events = []
        store = mock.Mock()
        store.get_status.return_value = None

        flow = ForwardFlow(
            AppConfig(max_total_send=1),
            store,
            progress_callback=events.append,
            install_stop_hotkey=False,
        )

        flow.screen.save_checkpoint = mock.Mock()

        flow._run_batch(1, [], total_batches=1)

        self.assertTrue(any(event.get("event") == "batch_started" for event in events))
        self.assertTrue(any(event.get("event") == "batch_finished" for event in events))


if __name__ == "__main__":
    unittest.main()
