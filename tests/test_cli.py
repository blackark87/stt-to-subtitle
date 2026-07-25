import threading
import unittest
from unittest.mock import Mock, patch

from stt_to_subtitle.cli import _format_elapsed, _log_stage, _report_progress


class FormatElapsedTests(unittest.TestCase):
    def test_formats_elapsed_time_beyond_one_hour(self) -> None:
        self.assertEqual(_format_elapsed(3661.9), "01:01:01")


class ReportProgressTests(unittest.TestCase):
    def test_reports_elapsed_time_until_stopped(self) -> None:
        stop_event = Mock(spec=threading.Event)
        stop_event.wait.side_effect = [False, True]

        with (
            patch(
                "stt_to_subtitle.cli.time.monotonic",
                return_value=65.0,
            ),
            patch("builtins.print") as print_mock,
        ):
            _report_progress(stop_event, "[2/3] Transcription", 0.0, 30.0)

        print_mock.assert_called_once_with(
            "[2/3] Transcription still running (elapsed 00:01:05)",
            flush=True,
        )


class LogStageTests(unittest.TestCase):
    def test_logs_stage_start_and_completion(self) -> None:
        with (
            patch(
                "stt_to_subtitle.cli.time.monotonic",
                side_effect=[10.0, 12.9],
            ),
            patch("stt_to_subtitle.cli.threading.Thread") as thread_factory,
            patch("builtins.print") as print_mock,
        ):
            with _log_stage(
                "[1/3] Audio extraction",
                detail="/output/sample.wav",
            ):
                pass

        thread_factory.return_value.start.assert_called_once_with()
        thread_factory.return_value.join.assert_called_once_with()
        self.assertEqual(
            print_mock.call_args_list,
            [
                unittest.mock.call(
                    "[1/3] Audio extraction started: /output/sample.wav",
                    flush=True,
                ),
                unittest.mock.call(
                    "[1/3] Audio extraction completed in 00:00:02",
                    flush=True,
                ),
            ],
        )
