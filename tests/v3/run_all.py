"""Test harness per NeuralTape v3 Fase 0.

Run:
    python tests/v3/run_all.py
    python tests/v3/run_all.py -v
    python tests/v3/run_all.py --keep-db   # non cancellare i DB temporanei

No pytest: ogni test_*.py espone funzioni test_*(). Il runner le scopre e le chiama,
raccoglie failures, ritorna exit code.
"""

from __future__ import annotations

import argparse
import importlib.util
import logging
import sys
import tempfile
import traceback
from pathlib import Path

log = logging.getLogger("nt-v3-tests")

HERE = Path(__file__).resolve().parent
LEX_V3_DIR = HERE.parent.parent / "lex" / "v3"


def _load_test_module(name: str):
    """Load a test_*.py module with lex/v3 importable as package."""
    # Make 'lex.v3' importable. We add NeuralTape/ root to sys.path and inject
    # lex as a namespace package.
    nt_root = LEX_V3_DIR.parent.parent
    if str(nt_root) not in sys.path:
        sys.path.insert(0, str(nt_root))
    # Ensure 'lex' is importable even though NeuralTape has a hyphen-free path
    # (NeuralTape/ folder itself — we import from inside it).
    # Strategy: register lex/v3 as top-level module 'nt_v3' for test files.
    if "nt_v3" not in sys.modules:
        # Build a synthetic package pointing at lex/v3.
        import types
        pkg = types.ModuleType("nt_v3")
        pkg.__path__ = [str(LEX_V3_DIR)]
        sys.modules["nt_v3"] = pkg
    spec = importlib.util.spec_from_file_location(f"nt_v3_tests.{name}", HERE / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--keep-db", action="store_true", help="non cancellare i DB temporanei")
    ap.add_argument("--module", help="esegui solo un test_*.py (senza estensione)")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    test_files = sorted(p.stem for p in HERE.glob("test_*.py"))
    if args.module:
        if args.module not in test_files:
            log.error("Module %s not found in %s", args.module, test_files)
            return 2
        test_files = [args.module]

    total = 0
    passed = 0
    failed = 0
    failures: list[tuple[str, str, str]] = []

    print(f"NeuralTape v3 — Fase 0 test suite ({len(test_files)} module(s))")
    print("=" * 60)

    # Make a tmpdir available to test modules via env var (so they don't pollute).
    tmp_root = Path(tempfile.mkdtemp(prefix="nt-v3-tests-"))

    for tf in test_files:
        print(f"\n[{tf}]")
        try:
            mod = _load_test_module(tf)
        except Exception:
            print("  IMPORT FAILED:")
            traceback.print_exc()
            failed += 1
            failures.append((tf, "<module-import>", traceback.format_exc()))
            continue

        # Each test_*.py may read TEST_TMP_ROOT for a shared scratch dir.
        if hasattr(mod, "TEST_TMP_ROOT"):
            mod.TEST_TMP_ROOT = tmp_root
        if hasattr(mod, "KEEP_DB"):
            mod.KEEP_DB = args.keep_db

        for attr in sorted(dir(mod)):
            if not attr.startswith("test_"):
                continue
            fn = getattr(mod, attr)
            if not callable(fn):
                continue
            total += 1
            try:
                fn()
                print(f"  ✓ {attr}")
                passed += 1
            except Exception as e:
                print(f"  ✗ {attr}: {e}")
                failed += 1
                failures.append((tf, attr, traceback.format_exc()))

    print("\n" + "=" * 60)
    print(f"Total: {total}  Passed: {passed}  Failed: {failed}")

    if failed:
        print("\n--- FAILURES ---")
        for tf, attr, tb in failures:
            print(f"\n[{tf}::{attr}]")
            print(tb)
        return 1

    if not args.keep_db:
        try:
            import shutil
            shutil.rmtree(tmp_root, ignore_errors=True)
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
