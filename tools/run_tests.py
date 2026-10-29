"""Every test in the repo, in one go: the engines' and the relay's Python
tests (test_*.py under ocr/) and the dashboard's and overlays' JS tests
(test_*.js under dashboard/ and overlay/). Each runs on its own, from the
repo root; a test passes when it exits 0.

    python tools/run_tests.py            everything
    python tools/run_tests.py -k lobby   only the tests whose path has "lobby"
    python tools/run_tests.py --py       only the Python ones (--js: only JS)

Exits 1 when any test fails, so CI (and a deploy gated on it) can trust
the exit code. A JS test whose library is missing (jsdom: npm install
jsdom) is reported as skipped, not failed.
"""
import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TIMEOUT = 300


def find_tests():
    py = sorted(p for p in (ROOT / "ocr").rglob("test_*.py") if "node_modules" not in p.parts)
    js = sorted(p for d in ("dashboard", "overlay") for p in (ROOT / d).glob("test_*.js"))
    return py, js


def run(path):
    cmd = [sys.executable, str(path)] if path.suffix == ".py" else ["node", str(path)]
    started = time.time()
    try:
        out = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=TIMEOUT,
                             encoding="utf-8", errors="replace",
                             env=dict(os.environ, PYTHONIOENCODING="utf-8"))
        text = (out.stdout or "") + (out.stderr or "")
        if out.returncode != 0 and "npm install jsdom" in text:
            status = "skip"
        else:
            status = "pass" if out.returncode == 0 else "FAIL"
    except subprocess.TimeoutExpired as e:
        text = (e.stdout or "") if isinstance(e.stdout, str) else ""
        status = "FAIL"
        text += "\n(timed out after %ds)" % TIMEOUT
    return status, time.time() - started, text


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("-k", default="", help="only tests whose path contains this")
    ap.add_argument("--py", action="store_true", help="only the Python tests")
    ap.add_argument("--js", action="store_true", help="only the JS tests")
    ap.add_argument("-v", action="store_true", help="print every test's output, not just failures'")
    args = ap.parse_args()

    py, js = find_tests()
    tests = (py if not args.js else []) + (js if not args.py else [])
    tests = [t for t in tests if args.k.lower() in str(t.relative_to(ROOT)).lower()]
    if not tests:
        print("no tests match")
        return 1

    results = []
    for t in tests:
        rel = t.relative_to(ROOT).as_posix()
        status, secs, text = run(t)
        results.append((rel, status))
        print("%-4s  %5.1fs  %s" % (status, secs, rel), flush=True)
        if status == "FAIL" or args.v:
            tail = "\n".join(text.strip().splitlines()[-25:])
            print("\n".join("        " + line for line in tail.splitlines()))

    failed = [r for r, s in results if s == "FAIL"]
    skipped = [r for r, s in results if s == "skip"]
    print("\n%d tests: %d passed, %d failed, %d skipped" % (
        len(results), len(results) - len(failed) - len(skipped), len(failed), len(skipped)))
    for r in failed:
        print("  FAILED  " + r)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
