"""Logging adapter for the vendored WhisperJAV code.

LOCAL REPLACEMENT (stt-to-subtitle). Upstream shipped a 109-line module that
installed its own stdout handler and set ``propagate = False``. Both are wrong
here: this code runs inside a worker whose stdout is captured and only shown
when the worker fails, and our services configure the root logger themselves.

Only ``logger`` is imported by the vendored modules, so this keeps that name
and lets the host application decide on handlers, level, and formatting.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("stt_to_subtitle.vendor.whisperjav")
