"""Collapses the proxy's repeating log lines.

Protect pushes the same handful of settings messages at a camera forever --
ChangeVideoSettings every fifteen seconds, GetRequest every five or so -- and
unifi-cam-proxy logs each one at INFO. Over a day that is tens of thousands of
identical lines, and it buries the ones that matter: a detection, a dropped
connection, a stack trace.

Repeats of a line are held back and counted. When the window is up the next one
goes through carrying the tally, so nothing is hidden -- it just stops being
said twenty times a minute. The first occurrence of anything always passes
immediately, so a message that has not been seen before is never delayed.
"""

from __future__ import annotations

import logging
import os
import time

# 0 turns the whole thing off, for when the raw stream is what you want.
WINDOW = float(os.environ.get("UNIFI_LOG_REPEAT_SECONDS", "300"))


class RepeatFilter(logging.Filter):
    """Passes a given message at most once per window, with a count of the rest."""

    def __init__(self, window: float = WINDOW):
        super().__init__()
        self.window = window
        self._last: dict[str, float] = {}
        self._held: dict[str, int] = {}

    def key(self, record: logging.LogRecord) -> str:
        try:
            return str(record.msg) % record.args if record.args else str(record.msg)
        except Exception:  # noqa: BLE001 - a bad format string must not lose the line
            return str(record.msg)

    def filter(self, record: logging.LogRecord) -> bool:
        if self.window <= 0:
            return True
        # Anything above INFO is a problem, and problems are never collapsed:
        # two warnings in a row are two separate things going wrong.
        if record.levelno > logging.INFO:
            return True

        key = self.key(record)
        now = time.monotonic()
        last = self._last.get(key)
        if last is not None and now - last < self.window:
            self._held[key] = self._held.get(key, 0) + 1
            return False

        held = self._held.pop(key, 0)
        self._last[key] = now
        if held:
            # Rewrite rather than append to the format string: the arguments have
            # already been folded into the key, and reusing them would be wrong.
            record.msg = f"{key} (and {held} more like it in the last " \
                         f"{int(self.window)}s)"
            record.args = ()
        return True


def install(*logger_names: str, window: float = WINDOW) -> RepeatFilter | None:
    """Attach one shared filter to each named logger. Returns it, or None if off."""
    if window <= 0:
        return None
    log_filter = RepeatFilter(window)
    for name in logger_names:
        logging.getLogger(name).addFilter(log_filter)
    return log_filter
