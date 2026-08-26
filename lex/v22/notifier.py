"""Notifier — desktop notification when insights are captured.

Uses Linux notify-send when available, macOS osascript as fallback,
and always prints to stdout (visible in cron logs).
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys

log = logging.getLogger("neural-tape-v22")


class Notifier:
    """Cross-platform desktop notification (best-effort, never raises)."""

    def notify(self, title: str, message: str) -> None:
        # Always log
        log.info("NOTIFY: %s — %s", title, message)
        print(f"[{title}] {message}", flush=True)

        try:
            if shutil.which("notify-send"):
                subprocess.run(
                    ["notify-send", "-i", "dialog-information", "-t", "8000", title, message],
                    check=False,
                    timeout=5,
                )
            elif sys.platform == "darwin":
                # Escape double quotes for AppleScript
                safe_msg = message.replace('"', '\\"')
                safe_title = title.replace('"', '\\"')
                subprocess.run(
                    [
                        "osascript",
                        "-e",
                        f'display notification "{safe_msg}" with title "{safe_title}"',
                    ],
                    check=False,
                    timeout=5,
                )
        except Exception as e:
            # Notification is best-effort — never fail the pipeline on it
            log.debug("Notification delivery skipped: %s", e)
