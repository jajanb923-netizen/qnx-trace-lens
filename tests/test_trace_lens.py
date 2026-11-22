#!/usr/bin/env python3
"""Self-test for qnx-trace-lens on synthetic traces.

Run from the qnx-trace-lens directory:
    python3 tests/test_trace_lens.py

Checks (all on SYNTHETIC fixtures — see tests/gen_synth.py):
  1. Inversion trace -> exit 1, exactly 1 inversion, blocker MED (tid 2),
     starved holder LOW (tid 1), overlap ~40 ms; HIGH worst block ~40 ms.
  2. Clean trace     -> exit 0, 0 inversions.
  3. HTML report is written, self-contained, contains <svg> and the
     thread rows.
  4. Bad input (missing file / no header) -> exit 2.
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(HERE, "trace_lens.py")
TESTS = os.path.join(HERE, "tests")
INV = os.path.join(TESTS, "test-inversion.txt")
CLEAN = os.path.join(TESTS, "test-clean.txt")
HTML = os.path.join(TESTS, "test-report.html")

failures = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (" — " + detail if detail and not cond else ""))
    if not cond:
        failures.append(name)


def run(*args):
    return subprocess.run([sys.executable, TOOL, "check", *args],
                          capture_output=True, text=True)


def main():
    subprocess.run([sys.executable, os.path.join(TESTS, "gen_synth.py"),
                    INV, CLEAN], check=True)

    r = run(INV, "--html", HTML, "--max-ready-wait-ms", "100")
    check("inversion trace exits 1", r.returncode == 1,
          "got %d: %s" % (r.returncode, r.stdout[-200:]))
    check("exactly 1 inversion reported", "1 found" in r.stdout or
          r.stdout.count("inversion:") == 1, r.stdout)
    check("blocker is MED tid 2", "pid:100 tid:2 (prio 20) ran" in r.stdout,
          r.stdout)
    check("starved holder is LOW tid 1",
          "pid:100 tid:1 (prio 10)" in r.stdout and "starved ready" in r.stdout,
          r.stdout)
    check("overlap ~40 ms", "overlap 40.000 ms" in r.stdout, r.stdout)
    check("HIGH worst block 42 ms",
          "pid:100 tid:3" in r.stdout and "42.000" in r.stdout, r.stdout)
    check("html written", os.path.exists(HTML) and os.path.getsize(HTML) > 1000)
    html = open(HTML).read() if os.path.exists(HTML) else ""
    check("html contains svg timeline", "<svg" in html and "THRUNNING" in html)
    check("html marks FAIL", ">FAIL<" in html)

    r2 = run(CLEAN, "--max-ready-wait-ms", "100")
    check("clean trace exits 0", r2.returncode == 0,
          "got %d: %s" % (r2.returncode, r2.stdout[-200:]))
    check("clean trace: 0 inversions", "0 found" in r2.stdout, r2.stdout)

    r3 = run(os.path.join(TESTS, "does-not-exist.txt"))
    check("missing file exits 2", r3.returncode == 2, "got %d" % r3.returncode)
    bad = os.path.join(TESTS, "test-bad.txt")
    open(bad, "w").write("hello world\nno header here\n")
    r4 = run(bad)
    check("no-header file exits 2", r4.returncode == 2, "got %d" % r4.returncode)

    print()
    if failures:
        print("FAILED: %d (%s)" % (len(failures), ", ".join(failures)))
        return 1
    print("ALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
