#!/usr/bin/env python3
"""Generate SYNTHETIC traceprinter-format traces for testing trace_lens.py.

These traces model the rt-pulse scenario (LOW prio 10 holds a mutex,
MED prio 20 burns CPU, HIGH prio 30 waits on the mutex) in the documented
traceprinter text format. They are TEST FIXTURES ONLY — not real captures.
Real validation uses a genuine `tracelogger -w` capture from the QEMU guest
once the target tree is complete (see BUILD-NOTES.md).

Cycle 1 (inversion): HIGH blocks on the mutex while MED runs and LOW
starves in THREADY  -> must be flagged.
Cycle 2 (bounded):   HIGH blocks while LOW itself runs its critical
section -> must NOT be flagged.

SPDX-License-Identifier: MIT
"""

import sys

CPS = 2648529944  # cycles/sec, same order as the tutorial's example
PID = 100
LOW, MED, HIGH = 1, 2, 3


def cyc(ms):
    return int(ms * CPS / 1000)


def ev(t_ms, cpu, tid, state, prio, policy=1):
    return ("t:0x%x CPU:%02d THREAD          :%-16s pid:%d tid:%d "
            "priority:%d policy:%d"
            % (cyc(t_ms), cpu, state, PID, tid, prio, policy))


def gen_inversion(path):
    lines = [
        "  TRACE_CYCLES_PER_SEC:: %d" % CPS,
        " TRACE_TRACELOGGER_ARGS:: tracelogger -w -s 3 -f /tmp/rt-pulse.kev",
        "  TRACE_DATE:: 2026-11-22",
        "",
    ]
    # Thread creation dump at trace start.
    for tid, prio in ((LOW, 10), (MED, 20), (HIGH, 30)):
        lines.append(ev(0, 0, tid, "THCREATE", prio))
    # Cycle 1 — inversion (0-46 ms).
    lines.append(ev(0, 0, LOW, "THRUNNING", 10))    # LOW runs, takes mutex
    lines.append(ev(2, 0, HIGH, "THMUTEX", 30))     # HIGH wakes, blocks
    lines.append(ev(2, 0, LOW, "THREADY", 10))      # LOW preempted -> ready
    lines.append(ev(2, 1, MED, "THRUNNING", 20))    # MED runs (the problem)
    lines.append(ev(42, 1, MED, "THRECEIVE", 20))   # MED sleeps
    lines.append(ev(42, 0, LOW, "THRUNNING", 10))   # LOW resumes, releases
    lines.append(ev(44, 0, LOW, "THRECEIVE", 10))   # LOW done, sleeps
    lines.append(ev(44, 0, HIGH, "THRUNNING", 30))  # HIGH gets mutex, runs
    lines.append(ev(46, 0, HIGH, "THRECEIVE", 30))  # HIGH done
    # Cycle 2 — bounded, no inversion (50-92 ms).
    lines.append(ev(50, 0, LOW, "THRUNNING", 10))   # LOW runs w/ mutex
    lines.append(ev(52, 0, HIGH, "THMUTEX", 30))    # HIGH blocks
    lines.append(ev(90, 0, LOW, "THRECEIVE", 10))   # LOW releases
    lines.append(ev(90, 0, HIGH, "THRUNNING", 30))  # HIGH runs
    lines.append(ev(92, 0, HIGH, "THRECEIVE", 30))  # HIGH done
    # A background system thread, mostly idle.
    lines.append("t:0x%x CPU:01 THREAD          :THRECEIVE        "
                 "pid:1 tid:1 priority:255 policy:1" % cyc(0))
    open(path, "w").write("\n".join(lines) + "\n")
    print("wrote %s (%d events)" % (path, len(lines)))


def gen_clean(path):
    """Same as cycle 2 only: HIGH waits while LOW runs -> no inversion."""
    lines = [
        "  TRACE_CYCLES_PER_SEC:: %d" % CPS,
        " TRACE_TRACELOGGER_ARGS:: tracelogger -w -s 3 -f /tmp/rt-pulse.kev",
        "",
    ]
    for tid, prio in ((LOW, 10), (MED, 20), (HIGH, 30)):
        lines.append(ev(0, 0, tid, "THCREATE", prio))
    lines.append(ev(50, 0, LOW, "THRUNNING", 10))
    lines.append(ev(52, 0, HIGH, "THMUTEX", 30))
    lines.append(ev(90, 0, LOW, "THRECEIVE", 10))
    lines.append(ev(90, 0, HIGH, "THRUNNING", 30))
    lines.append(ev(92, 0, HIGH, "THRECEIVE", 30))
    open(path, "w").write("\n".join(lines) + "\n")
    print("wrote %s (%d events)" % (path, len(lines)))


if __name__ == "__main__":
    gen_inversion(sys.argv[1] if len(sys.argv) > 1
                  else "test-inversion.txt")
    gen_clean(sys.argv[2] if len(sys.argv) > 2 else "test-clean.txt")
