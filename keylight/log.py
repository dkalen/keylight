"""Logging to the platform's normal place.

- Interactive terminal: plain stderr.
- Under systemd (JOURNAL_STREAM set): stderr without timestamps; journald adds them.
- Otherwise (launchd on macOS): syslog(3), which macOS routes into the unified log.
"""

from __future__ import annotations

import logging
import os
import sys
import syslog

_SYSLOG_PRIORITY = {
    logging.DEBUG: syslog.LOG_DEBUG,
    # NOTICE, not INFO: macOS doesn't persist syslog INFO in the unified log, so
    # `log show` would never see Session events. journald treats both alike.
    logging.INFO: syslog.LOG_NOTICE,
    logging.WARNING: syslog.LOG_WARNING,
    logging.ERROR: syslog.LOG_ERR,
    logging.CRITICAL: syslog.LOG_CRIT,
}


class SyslogHandler(logging.Handler):
    def __init__(self, ident: str):
        super().__init__()
        syslog.openlog(ident, syslog.LOG_PID, syslog.LOG_USER)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            syslog.syslog(_SYSLOG_PRIORITY.get(record.levelno, syslog.LOG_INFO), self.format(record))
        except Exception:
            self.handleError(record)


def setup(ident: str, verbose: bool = False) -> logging.Logger:
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    if sys.stderr.isatty():
        handler: logging.Handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))
    elif os.environ.get("JOURNAL_STREAM"):
        handler = logging.StreamHandler()
        # sd-daemon level prefixes so journald records priorities correctly
        handler.setFormatter(_JournalFormatter())
    else:
        handler = SyslogHandler(ident)
        handler.setFormatter(logging.Formatter(f"{ident}: %(levelname)s %(message)s"))
    root.addHandler(handler)
    return logging.getLogger(ident)


class _JournalFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return f"<{_SYSLOG_PRIORITY.get(record.levelno, 6)}>{super().format(record)}"
