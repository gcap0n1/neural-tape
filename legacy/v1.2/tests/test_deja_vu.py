#!/usr/bin/env python3
"""Tests for deja_vu.py"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "lex"))

from deja_vu import DejaVu, Config


class FakeConfigDejaVu:
    def __init__(self):
        self.paths = {"neural_tape_root": "/tmp/neural-tape"}
        self.deja_vu = {
            "similarity_threshold": 0.75,
            "normalization": [
                {"pattern": r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}", "replacement": "IP_ADDR"},
                {"pattern": r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", "replacement": "DATETIME"},
            ]
        }

    def get_archive_dir(self):
        return Path(self.paths["neural_tape_root"]) / "tape" / "archive"

    def get_staging_dir(self):
        return Path(self.paths["neural_tape_root"]) / "tape" / "staging"


def test_similarity_identical():
    config = FakeConfigDejaVu()
    dv = DejaVu(config)
    a = "SSH error: connection refused to 192.168.1.1"
    b = "SSH error: connection refused to 192.168.1.1"
    sim = dv.similarity(a, b)
    assert sim > 0.95, f"Expected >0.95, got {sim}"
    print("PASS: test_similarity_identical")


def test_similarity_similar():
    config = FakeConfigDejaVu()
    dv = DejaVu(config)
    a = "SSH error: connection refused to 192.168.1.1"
    b = "SSH error: connection refused to 10.0.0.5"
    sim = dv.similarity(a, b)
    assert sim > 0.75, f"Expected >0.75, got {sim}"
    print("PASS: test_similarity_similar")


def test_similarity_different():
    config = FakeConfigDejaVu()
    dv = DejaVu(config)
    a = "SSH error: connection refused"
    b = "Context too long, compacting memory"
    sim = dv.similarity(a, b)
    assert sim < 0.5, f"Expected <0.5, got {sim}"
    print("PASS: test_similarity_different")


def test_normalization():
    config = FakeConfigDejaVu()
    dv = DejaVu(config)
    text = "Error at 2026-06-07 12:00:00 on host 192.168.1.1"
    norm = dv._normalize(text)
    assert "DATETIME" in norm
    assert "IP_ADDR" in norm
    assert "192.168.1.1" not in norm
    print("PASS: test_normalization")


if __name__ == "__main__":
    test_similarity_identical()
    test_similarity_similar()
    test_similarity_different()
    test_normalization()
    print("
All deja_vu tests passed.")
