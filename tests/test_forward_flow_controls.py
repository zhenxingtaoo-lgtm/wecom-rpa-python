from unittest import mock
import unittest

from wecom_rpa.config import AppConfig
from wecom_rpa.forward_flow import ForwardFlow
from wecom_rpa.safety import StopController


class ForwardFlowControlsTest(unittest.TestCase):
    def test_accepts_external_stop_controller(self):
        stop = StopController("ctrl+alt+q")
        flow = ForwardFlow(AppConfig(), stop_controller=stop, install_stop_hotkey=False)
        self.assertIs(flow.stop, stop)

    def test_confirm_callback_can_cancel_flow_prompt(self):
        flow = ForwardFlow(AppConfig(), confirm_callback=lambda _prompt: False, install_stop_hotkey=False)
        with self.assertRaises(RuntimeError):
            flow._confirm("confirm")

    def test_split_send_count(self):
        flow = ForwardFlow(AppConfig(batch_size=9), install_stop_hotkey=False)
        self.assertEqual(flow._split_send_count(20), [9, 9, 2])

    def test_progress_callback_receives_batch_events_in_dry_run(self):
        events = []
        flow = ForwardFlow(AppConfig(batch_size=9), progress_callback=events.append, install_stop_hotkey=False)
        flow.screen.save_checkpoint = mock.Mock()
        flow._run_batch(1, 3, total_batches=1)
        self.assertTrue(any(event.get("event") == "batch_started" for event in events))
        self.assertTrue(any(event.get("event") == "batch_finished" for event in events))


if __name__ == "__main__":
    unittest.main()
