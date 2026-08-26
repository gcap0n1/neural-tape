#!/usr/bin/env python3
"""Tests for log_parser.py"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "lex"))

from log_parser import LogParser, Insight, Config


class FakeConfig:
    def __init__(self):
        self.paths = {"kimi_logs": "/tmp", "neural_tape_root": "/tmp/neural-tape"}
        self.log_parser = {
            "patterns": {
                "shell_error": {
                    "regex": r"ERROR.*Shell command execution failed: (.*)",
                    "category": "bug_found",
                    "confidence": "high",
                },
                "context_compaction": {
                    "regex": r"Context too long, compacting",
                    "category": "warning",
                    "confidence": "high",
                },
            }
        }
        self.assistants = {}

    def get_log_path(self, assistant="kimi"):
        return Path(self.paths["kimi_logs"])

    def get_staging_dir(self):
        return Path(self.paths["neural_tape_root"]) / "tape" / "staging"

    def get_patterns(self, assistant="kimi"):
        return self.log_parser["patterns"]


def test_shell_error_pattern():
    config = FakeConfig()
    parser = LogParser(config)
    line = "2026-06-06 08:24:20.378 | ERROR | kimi_cli.tools.shell:__call__:130 | fec8e159-b87b-4b56-916c-a0fc00eaeefd - Shell command execution failed: ssh -i ~/.ssh/id_rsa user@host"
    insight = parser.process_line(line)
    assert insight is not None
    assert insight.type == "bug_found"
    assert insight.confidence == "high"
    assert "ssh" in insight.content
    print("PASS: test_shell_error_pattern")


def test_context_compaction_pattern():
    config = FakeConfig()
    parser = LogParser(config)
    line = "2026-06-06 08:49:59.115 | INFO | kimi_cli.soul.kimisoul:_agent_loop:899 | fec8e159-b87b-4b56-916c-a0fc00eaeefd - Context too long, compacting..."
    insight = parser.process_line(line)
    assert insight is not None
    assert insight.type == "warning"
    assert "compaction" in insight.content.lower() or "compacting" in insight.content.lower()
    print("PASS: test_context_compaction_pattern")


def test_no_match():
    config = FakeConfig()
    parser = LogParser(config)
    line = "2026-06-06 08:24:20.378 | INFO | Some random log line"
    insight = parser.process_line(line)
    assert insight is None
    print("PASS: test_no_match")


if __name__ == "__main__":
    test_shell_error_pattern()
    test_context_compaction_pattern()
    test_no_match()
    print("
All log_parser tests passed.")
