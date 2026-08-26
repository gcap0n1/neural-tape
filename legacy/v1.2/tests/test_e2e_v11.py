#!/usr/bin/env python3
"""
Neural Tape v1.1 — End-to-End Tests
Tests all new features: BM25, dedup, privacy, decay, compression, enabled flag.
"""

import sys
import os
import hashlib
import tempfile
import shutil
from pathlib import Path

# Add parent dirs to path
sys.path.insert(0, str(Path(__file__).parent.parent / "lex"))

PASSED = 0
FAILED = 0


def test(name, condition, detail=""):
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  ✅ {name}")
    else:
        FAILED += 1
        print(f"  ❌ {name} — {detail}")


# ═══════════════════════════════════════════════════════════════════════
# 1. PRIVACY FILTER
# ═══════════════════════════════════════════════════════════════════════
print("\n🔒 Privacy Filter")
from log_parser import strip_secrets, SECRET_PATTERNS

test("OpenAI key redacted",
     "REDACTED" in strip_secrets("api_key=" + "sk-" + "abc123456789012345678901234567890"))

test("GitHub PAT redacted",
     "REDACTED" in strip_secrets("token=" + "ghp_" + "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefgh"))

test("Bearer token redacted",
     "REDACTED" in strip_secrets("Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.abcdef"))

test("AWS key redacted",
     "REDACTED" in strip_secrets("aws_key=" + "AKIA" + "IOSFODNN7EXAMPLE"))

test("Google API key redacted",
     "REDACTED" in strip_secrets("key=" + "AIza" + "SyDdI0hCZtE6vySjMm-WEfRq3CPzqKqqsHI"))

test("Private tag redacted",
     "REDACTED" in strip_secrets("config: <private>my_secret</private> end"))

test("Clean text passes through",
     strip_secrets("just a normal log line") == "just a normal log line")

test(f"13+ secret patterns loaded",
     len(SECRET_PATTERNS) >= 13)


# ═══════════════════════════════════════════════════════════════════════
# 2. SHA-256 DEDUP
# ═══════════════════════════════════════════════════════════════════════
print("\n🔄 SHA-256 Dedup")
from log_parser import DedupMap

dm = DedupMap(ttl_seconds=300)

test("First occurrence not duplicate",
     not dm.is_duplicate("s1", "trigger", "same content"))

test("Second occurrence IS duplicate",
     dm.is_duplicate("s1", "trigger", "same content"))

test("Different content NOT duplicate",
     not dm.is_duplicate("s1", "trigger", "different content"))

test("Different session NOT duplicate",
     not dm.is_duplicate("s2", "trigger", "same content"))

# Test cleanup
dm2 = DedupMap(ttl_seconds=0)  # instant expiry
dm2.is_duplicate("s1", "t", "c")
import time
time.sleep(0.01)
test("Expired entries cleaned up",
     not dm2.is_duplicate("s1", "t", "c"))


# ═══════════════════════════════════════════════════════════════════════
# 3. SYNTHETIC COMPRESSION
# ═══════════════════════════════════════════════════════════════════════
print("\n🗜️ Synthetic Compression")
from log_parser import compress_synthetic

bug_content = "Error: Traceback in /home/user/project/src/utils.py line 42, TypeError: 'NoneType'"
result = compress_synthetic(bug_content, "opencode_error", "bug_found")

test("Bug title generated",
     "opencode_error" in result["title"])

test("Error extracted in facts",
     any("Error:" in f for f in result["facts"]))

test("File extracted in facts",
     any("utils.py" in f for f in result["facts"]))

test("Narrative generated",
     len(result["narrative"]) > 10)

code_content = "Modified tool: compile_patterns in log_parser.py"
result2 = compress_synthetic(code_content, "code_change", "code_change")

test("Tool name extracted",
     any("compile_patterns" in f for f in result2["facts"]))


# ═══════════════════════════════════════════════════════════════════════
# 4. EBBINGHAUS DECAY
# ═══════════════════════════════════════════════════════════════════════
print("\n📉 Ebbinghaus Decay")
from pre_load import decay_strength, is_below_threshold
from datetime import datetime, timedelta

now = datetime.now().isoformat()
test("Fresh insight (now) has full strength",
     decay_strength(now) >= 0.99)

one_year_ago = (datetime.now() - timedelta(days=365)).isoformat()
old_strength = decay_strength(one_year_ago)
test(f"1-year-old insight decayed (strength={old_strength:.2f})",
     old_strength < 0.5)

test("Decay strength is bounded [0.1, 1.0]",
     0.1 <= decay_strength(one_year_ago) <= 1.0)

test("is_below_threshold works",
     is_below_threshold(0.05) and not is_below_threshold(0.5))


# ═══════════════════════════════════════════════════════════════════════
# 5. BM25 SEARCH
# ═══════════════════════════════════════════════════════════════════════
print("\n🔍 BM25 Search")
from pre_load import BM25Ranker, _tokenize, bm25_score

# Tokenization
test("Tokenize lowercase + split",
     _tokenize("Hello World!") == ["hello", "world"])

test("Tokenize alphanumeric extraction",
     "regex" in _tokenize("error in regex compilation"))

# BM25 scoring
docs = [
    {"content": "error in regex compilation", "type": "bug_found", "strength": 0.8},
    {"content": "BM25 ranking for insights", "type": "eureka", "strength": 0.9},
    {"content": "dedup with SHA-256 hashing", "type": "code_change", "strength": 0.6},
    {"content": "regex error in pattern matching", "type": "bug_found", "strength": 0.7},
]

ranker = BM25Ranker()
ranker.index(docs, text_key="content")

results = ranker.search("error regex", top_k=2)
test("BM25 returns correct top result",
     "error" in results[0]["content"].lower())

test("BM25 top-2 contains both error docs",
     all("error" in r["content"].lower() for r in results))

results2 = ranker.search("dedup SHA", top_k=1)
test("BM25 dedup query matches",
     "dedup" in results2[0]["content"].lower())

# Blended ranking (70% BM25 + 30% decay)
results3 = ranker.search("error", top_k=4)
test("BM25 returns all matching docs",
     len(results3) == 4)

# Empty query fallback
results4 = ranker.search("", top_k=2)
test("Empty query returns top docs",
     len(results4) == 2)


# ═══════════════════════════════════════════════════════════════════════
# 6. CONFIG: ENABLED FLAG
# ═══════════════════════════════════════════════════════════════════════
print("\n⚙️ Config: enabled flag")
from log_parser import Config

config_path = Path(__file__).parent.parent / "config.yaml"
if config_path.exists():
    config = Config(config_path)

    test("is_pattern_enabled returns True for default",
         config.is_pattern_enabled("nonexistent_pattern", "kimi"))

    # Check ZCode disabled patterns
    zcode_patterns = config.assistants.get("zcode", {}).get("patterns", {})
    disabled_zcode = [name for name, spec in zcode_patterns.items()
                      if spec.get("enabled") is False]
    test(f"Found {len(disabled_zcode)} disabled ZCode patterns",
         len(disabled_zcode) > 0)

    # Check Kimi disabled patterns
    kimi_patterns = config.assistants.get("kimi", {}).get("patterns", {})
    disabled_kimi = [name for name, spec in kimi_patterns.items()
                     if spec.get("enabled") is False]
    test(f"Found {len(disabled_kimi)} disabled Kimi patterns",
         len(disabled_kimi) > 0)
else:
    test("Config file exists", False, "config.yaml not found")


# ═══════════════════════════════════════════════════════════════════════
# 7. LOG PARSER: SKIP DISABLED PATTERNS
# ═══════════════════════════════════════════════════════════════════════
print("\n🔧 Log Parser: Pattern compilation")

if config_path.exists():
    from log_parser import LogParser

    # Test with zcode (has disabled patterns)
    lp = LogParser(config, assistant="zcode")

    # Count how many patterns were actually compiled
    compiled_count = len(lp.patterns)
    zcode_total = config.assistants.get("zcode", {}).get("patterns", {})
    total_patterns = len(zcode_total)
    disabled_count = sum(1 for spec in zcode_total.values()
                         if spec.get("enabled") is False)

    test(f"Compiled {compiled_count}/{total_patterns} ZCode patterns ({disabled_count} disabled)",
         compiled_count == total_patterns - disabled_count)

    # Verify specific disabled patterns are NOT compiled
    test("zcode_info_tool_call NOT compiled (disabled)",
         "zcode_info_tool_call" not in lp.patterns)

    test("zcode_info_turn_completed NOT compiled (disabled)",
         "zcode_info_turn_completed" not in lp.patterns)

    # Verify enabled patterns ARE compiled
    test("zcode_error_startup IS compiled (enabled)",
         "zcode_error_startup" in lp.patterns)


# ═══════════════════════════════════════════════════════════════════════
# 8. POST CAPTURE: CONFIDENCE FILTER
# ═══════════════════════════════════════════════════════════════════════
print("\n📋 Post Capture: Confidence filter")
from post_capture import PostCapture, Config as PCConfig
import tempfile

if config_path.exists():
    pc_config = PCConfig(config_path)
    pc = PostCapture(pc_config)

    # Create temp staging files to test confidence extraction
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        
        # High confidence file
        high_file = tmpdir / "high.md"
        high_file.write_text("---\ntype: bug_found\nconfidence: high\n---\n# High insight\n\nContent")
        
        # Low confidence file
        low_file = tmpdir / "low.md"
        low_file.write_text("---\ntype: insight\nconfidence: low\n---\n# Low insight\n\nContent")
        
        # No frontmatter file
        nofm_file = tmpdir / "nofm.md"
        nofm_file.write_text("No frontmatter here")
        
        # Medium confidence
        med_file = tmpdir / "med.md"
        med_file.write_text("---\ntype: warning\nconfidence: medium\n---\n# Medium insight\n\nContent")
        
        test("High confidence extracted",
             pc._get_confidence(high_file) == "high")
        
        test("Low confidence extracted",
             pc._get_confidence(low_file) == "low")
        
        test("No frontmatter returns empty",
             pc._get_confidence(nofm_file) == "")
        
        test("Medium confidence is high value",
             pc._is_high_value(med_file))
        
        test("High confidence is high value",
             pc._is_high_value(high_file))
        
        test("Low confidence is NOT high value",
             not pc._is_high_value(low_file))
else:
    test("Post capture config loaded", False, "config.yaml not found")


# ═══════════════════════════════════════════════════════════════════════
# 9. PRE-LOAD: FULL PIPELINE
# ═══════════════════════════════════════════════════════════════════════
print("\n🚀 Pre-Load: Full pipeline")
from pre_load import PreLoad
from pre_load import Config as PLConfig

if config_path.exists():
    pl_config = PLConfig(config_path)
    pl = PreLoad(pl_config)

    insights = pl._read_insights("default", lookback_days=30)
    test(f"Read {len(insights)} insights from archive",
         isinstance(insights, list))

    if insights:
        ranked = pl._rank_insights(insights, query="error", top_k=5)
        test("BM25 ranking returns subset",
             len(ranked) <= len(insights))

        no_query = pl._rank_insights(insights, query=None, top_k=5)
        test("Decay ranking returns subset",
             len(no_query) <= len(insights))


# ═══════════════════════════════════════════════════════════════════════
# SUMMARY
# ═══════════════════════════════════════════════════════════════════════
print("\n" + "═" * 60)
print(f"RESULTS: {PASSED} passed, {FAILED} failed")
print("═" * 60)

if FAILED > 0:
    sys.exit(1)
else:
    print("🎯 All tests passed!")
    sys.exit(0)
