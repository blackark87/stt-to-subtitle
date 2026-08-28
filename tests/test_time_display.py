import logging
import unittest

from stt_to_subtitle.time_display import (
    KSTLogFormatter,
    format_kst_iso,
    format_kst_timestamp,
)


class KSTTimeDisplayTests(unittest.TestCase):
    def test_formats_epoch_as_kst_for_ui_and_api(self) -> None:
        self.assertEqual(
            format_kst_timestamp(0),
            "1970-01-01 09:00:00",
        )
        self.assertEqual(
            format_kst_iso(0),
            "1970-01-01T09:00:00.000+09:00",
        )

    def test_formats_log_record_time_as_kst(self) -> None:
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="message",
            args=(),
            exc_info=None,
        )
        record.created = 0
        formatter = KSTLogFormatter(
            "%(asctime)s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

        self.assertEqual(
            formatter.format(record),
            "1970-01-01 09:00:00 message",
        )


if __name__ == "__main__":
    unittest.main()
