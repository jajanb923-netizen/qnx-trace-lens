#!/usr/bin/env python3
"""qnx-trace-lens — scheduling analyzer for QNX 8.0 kernel traces.

Reads the text output of `traceprinter` (from a `tracelogger -w` capture),
computes per-thread scheduling statistics, detects priority inversions,
and emits a self-contained HTML report with an SVG timeline.

Usage:
    trace_lens.py check TRACE.txt [--html REPORT.html]
                                  [--max-ready-wait-ms FLOAT]   (default 5.0)
                                  [--max-inversions INT]        (default 0)
                                  [--min-block-ms FLOAT]        (default 1.0)
                                  [--cycles-per-sec INT]        (override header)

Exit status: 0 = no rule violated, 1 = rule violated (CI gate),
             2 = usage error or unparseable trace.

Design notes (written from scratch; not a copy of kevlint):
  * traceprinter format (QNX 8.0, wide mode `-w`):
        t:0x128a4c0787 CPU:01 THREAD          :THSEM     pid:725013 tid:4 priority:10 policy:2
    Header lines look like:
        TRACE_CYCLES_PER_SEC:: 2648529944
        TRACE_TRACELOGGER_ARGS:: tracelogger -w -s 3 -f /tmp/test.kev
    Wide mode is required: only it carries `priority:` and `policy:`.
    policy: 1 = SCHED_FIFO, 2 = SCHED_RR.
  * Priority inversion rule (classic, unbounded case): a thread H is
    blocked on a MUTEX (THMUTEX / THMUON_MUTEX) while a strictly
    lower-priority thread M runs on a CPU and a still lower-priority
    thread L — the presumed lock holder — sits merely READY, starved by
    M. The bounded case (L itself running its critical section while H
    waits) is NOT flagged: nothing is starved below the runner.
    Two exclusions keep real system traces honest:
    - THSEM / THCONDVAR waits are NOT lock contention: a server thread
      waiting on a semaphore/condvar for work is idle, not a victim of
      inversion, so those states never open an inversion.
    - A priority-0 thread is never the starved holder: in QNX the
      per-CPU idle threads (procnto, pid 1, prio 0) are READY whenever
      a CPU is idle, so counting them as "starved" flags every block
      that overlaps any idle CPU — a flood of false positives.
  * Times are CPU cycles; they are converted with TRACE_CYCLES_PER_SEC.

SPDX-License-Identifier: MIT
"""

import argparse
import html
import re
import sys
from collections import defaultdict

VERSION = "0.1.1"

# States in which a thread is waiting on something else (a lock, a message,
# a semaphore). THNANOSLEEP is deliberately excluded: sleeping is voluntary.
BLOCK_STATES = {
    "THSEM", "THMUTEX", "THMUON_MUTEX", "THRECEIVE", "THREPLY", "THSEND",
    "THSIGSUSPEND", "THSIGWAITINFO", "THWAITPAGE", "THCONDVAR",
}

# Subset of BLOCK_STATES that can be a priority inversion: waiting on a
# MUTEX. A semaphore/condvar wait is a wait for work or an event — a
# server thread idle on THSEM/THCONDVAR for the whole trace is not a
# victim of inversion — so only real lock waits open an inversion.
# Message-passing waits (THRECEIVE/THREPLY/THSEND) are normal IPC — an
# idle server waiting for a message is not a victim of inversion —
# so they are excluded from the rule as well.
INVERSION_STATES = {"THMUTEX", "THMUON_MUTEX"}

# States shown on the timeline.
STATE_COLORS = {
    "THRUNNING": "#2e9e5b",
    "THREADY":   "#e8a13a",
    "THSEM":     "#c0392b",
    "THMUTEX":   "#c0392b",
    "THRECEIVE": "#7d6bc4",
    "THREPLY":   "#7d6bc4",
    "THSEND":    "#7d6bc4",
    "THINTR":    "#8e44ad",
    "THCREATE":  "#95a5a6",
    "THDEAD":    "#333333",
}
DEFAULT_COLOR = "#b0b8c1"

EVENT_RE = re.compile(
    r"^t:0x([0-9a-fA-F]+)\s+CPU:(\d+)\s+(\S+)\s+:(\S+)"
    r"(?:\s+pid:(\d+))?(?:\s+tid:(\d+))?"
    r"(?:\s+priority:(\d+))?(?:\s+policy:(\d+))?"
)
HEADER_RE = re.compile(r"^\s*TRACE_([A-Z_]+)::\s*(.*)$")
PID_LINE_RE = re.compile(r"^\s*pid:(\d+)\s*$")
NAME_RE = re.compile(r"^\s*name:\s*(.*)$")


class TraceEvent:
    __slots__ = ("t", "cpu", "cls", "event", "pid", "tid",
                 "priority", "policy")

    def __init__(self, t, cpu, cls, event, pid, tid, priority, policy):
        self.t = t
        self.cpu = cpu
        self.cls = cls
        self.event = event
        self.pid = pid
        self.tid = tid
        self.priority = priority
        self.policy = policy


class Interval:
    __slots__ = ("thread", "state", "start", "end", "priority")

    def __init__(self, thread, state, start, end, priority):
        self.thread = thread
        self.state = state
        self.start = start
        self.end = end
        self.priority = priority

    @property
    def duration(self):
        return self.end - self.start


def parse_trace(path):
    """Parse a traceprinter text file.

    Returns (cycles_per_sec, events, names) where names maps
    (pid, tid or None) -> display name.
    """
    cycles_per_sec = None
    events = []
    names = {}
    pending_pid = None
    pending_kind = None

    with open(path, "r", errors="replace") as fh:
        for line in fh:
            line = line.rstrip("\n")
            m = HEADER_RE.match(line)
            if m:
                if m.group(1) == "CYCLES_PER_SEC":
                    try:
                        cycles_per_sec = int(m.group(2))
                    except ValueError:
                        pass
                pending_pid = None
                continue

            m = EVENT_RE.match(line)
            if m:
                t = int(m.group(1), 16)
                events.append(TraceEvent(
                    t=t,
                    cpu=int(m.group(2)),
                    cls=m.group(3),
                    event=m.group(4),
                    pid=int(m.group(5)) if m.group(5) else None,
                    tid=int(m.group(6)) if m.group(6) else None,
                    priority=int(m.group(7)) if m.group(7) else None,
                    policy=int(m.group(8)) if m.group(8) else None,
                ))
                # Name events: PROCCREATE_NAME / PROCTHREAD_NAME carry the
                # pid on a following indented line, then the name.
                if m.group(3) in ("PROCCREATE_NAME", "PROCTHREAD_NAME"):
                    pending_kind = m.group(3)
                    pending_pid = None
                else:
                    pending_kind = None
                    pending_pid = None
                continue

            if pending_kind is not None:
                pm = PID_LINE_RE.match(line)
                if pm:
                    pending_pid = int(pm.group(1))
                    continue
                nm = NAME_RE.match(line)
                if nm and pending_pid is not None:
                    key = (pending_pid, None if pending_kind == "PROCCREATE_NAME"
                           else None)
                    names[key] = nm.group(1).strip()
                    pending_kind = None
                    pending_pid = None
                    continue

    if cycles_per_sec is None:
        raise ValueError("no TRACE_CYCLES_PER_SEC header — is this a "
                         "traceprinter text file?")
    if not events:
        raise ValueError("no trace events found")
    # traceprinter output is normally time-ordered; sort defensively so a
    # stray out-of-order line cannot corrupt the span or the intervals.
    events.sort(key=lambda e: e.t)
    return cycles_per_sec, events, names


def build_intervals(events):
    """Turn THREAD state-change events into per-thread state intervals.

    Returns (intervals_by_thread, t_start, t_end). The last open interval
    of each thread is closed at the last event timestamp.
    """
    intervals = defaultdict(list)
    current = {}  # (pid, tid) -> (state, start_cycles, priority)
    t_start = events[0].t
    t_end = events[-1].t

    for ev in events:
        if ev.cls != "THREAD" or ev.pid is None or ev.tid is None:
            continue
        key = (ev.pid, ev.tid)
        state = ev.event
        if key in current:
            old_state, old_start, old_prio = current[key]
            if ev.t > old_start:
                intervals[key].append(
                    Interval(key, old_state, old_start, ev.t, old_prio))
        current[key] = (state, ev.t, ev.priority)
    for key, (state, start, prio) in current.items():
        if t_end > start:
            intervals[key].append(Interval(key, state, start, t_end, prio))
    return intervals, t_start, t_end


def analyze(intervals, cycles_per_sec, min_block_ms):
    """Compute per-thread stats and detect priority inversions."""
    stats = {}
    running = []   # (start, end, priority, thread) for THRUNNING intervals
    for thread, ivs in intervals.items():
        cpu_s = 0
        ready_s = 0
        blocked_s = 0
        worst_ready = 0.0
        worst_block = 0.0
        counts = defaultdict(int)
        for iv in ivs:
            counts[iv.state] += 1
            dur_s = iv.duration / cycles_per_sec
            if iv.state == "THRUNNING":
                cpu_s += dur_s
                running.append((iv.start, iv.end, iv.priority, thread))
            elif iv.state == "THREADY":
                ready_s += dur_s
                worst_ready = max(worst_ready, dur_s)
            elif iv.state in BLOCK_STATES:
                blocked_s += dur_s
                worst_block = max(worst_block, dur_s)
        stats[thread] = {
            "cpu_s": cpu_s, "ready_s": ready_s, "blocked_s": blocked_s,
            "worst_ready_ms": worst_ready * 1000,
            "worst_block_ms": worst_block * 1000,
            "counts": dict(counts),
            "intervals": len(ivs),
        }

    # Priority inversions (classic definition, unbounded case): thread H is
    # blocked while a middle-priority thread M runs AND a lower-priority
    # thread L — the presumed lock holder — sits merely READY (starved by
    # M). The bounded case (L itself running its critical section while H
    # waits) is NOT flagged: nothing is starved below the runner.
    # For each blocked interval of H we find the (M, L) pair with the
    # longest triple overlap.
    inversions = []
    min_block_cycles = min_block_ms / 1000.0 * cycles_per_sec
    running = []   # (start, end, priority, thread)
    ready = []     # (start, end, priority, thread)
    for thread, ivs in intervals.items():
        for iv in ivs:
            if iv.state == "THRUNNING":
                running.append((iv.start, iv.end, iv.priority, thread))
            elif iv.state == "THREADY":
                ready.append((iv.start, iv.end, iv.priority, thread))
    running.sort()
    ready.sort()
    for thread, ivs in intervals.items():
        for iv in ivs:
            if iv.state not in INVERSION_STATES:
                continue
            if iv.duration < min_block_cycles or iv.priority is None:
                continue
            best = None  # (overlap_cycles, M_thread, M_prio, L_thread, L_prio)
            for (rs, re, rprio, rthread) in running:
                if rs >= iv.end:
                    break
                if re <= iv.start or rprio is None or rprio >= iv.priority:
                    continue
                for (ds, de, dprio, dthread) in ready:
                    if ds >= min(re, iv.end):
                        break
                    if de <= max(rs, iv.start):
                        continue
                    if dprio is None or dprio >= rprio:
                        continue
                    # A priority-0 thread is never the starved holder:
                    # QNX idle threads (procnto, pid 1, prio 0) are
                    # READY whenever a CPU is idle, so they would match
                    # every block that overlaps any idle CPU.
                    if dprio == 0:
                        continue
                    lo = max(rs, ds, iv.start)
                    hi = min(re, de, iv.end)
                    if hi > lo and (best is None or hi - lo > best[0]):
                        best = (hi - lo, rthread, rprio, dthread, dprio)
            if best is not None:
                overlap, m_thread, m_prio, l_thread, l_prio = best
                inversions.append({
                    "thread": thread,
                    "priority": iv.priority,
                    "state": iv.state,
                    "start": iv.start,
                    "end": iv.end,
                    "duration_ms": iv.duration / cycles_per_sec * 1000,
                    "blocker": m_thread,
                    "blocker_priority": m_prio,
                    "holder": l_thread,
                    "holder_priority": l_prio,
                    "overlap_ms": overlap / cycles_per_sec * 1000,
                })
    inversions.sort(key=lambda d: d["start"])
    return stats, inversions


def thread_label(thread, names):
    pid, tid = thread
    proc = names.get((pid, None))
    if proc:
        return "%s pid:%d tid:%d" % (proc, pid, tid)
    return "pid:%d tid:%d" % (pid, tid)


def fmt_ms(ms):
    return "%.3f" % ms


def text_summary(stats, inversions, cycles_per_sec, t_start, t_end,
                 max_ready_wait_ms, max_inversions):
    total_s = (t_end - t_start) / cycles_per_sec
    lines = []
    lines.append("qnx-trace-lens %s" % VERSION)
    lines.append("trace span: %.3f s (%d threads)"
                 % (total_s, len(stats)))
    lines.append("")
    lines.append("%-28s %10s %10s %10s %12s %12s" % (
        "thread", "cpu_ms", "ready_ms", "blocked_ms",
        "worst_ready", "worst_block"))
    for thread in sorted(stats, key=lambda k: -stats[k]["cpu_s"]):
        s = stats[thread]
        lines.append("%-28s %10s %10s %10s %12s %12s" % (
            thread_label(thread, {})[:28],
            fmt_ms(s["cpu_s"] * 1000), fmt_ms(s["ready_s"] * 1000),
            fmt_ms(s["blocked_s"] * 1000),
            fmt_ms(s["worst_ready_ms"]), fmt_ms(s["worst_block_ms"])))
    lines.append("")
    worst_ready = max((s["worst_ready_ms"] for s in stats.values()),
                      default=0.0)
    lines.append("rule: worst-ready-wait <= %.3f ms : %s (worst %.3f ms)" % (
        max_ready_wait_ms,
        "PASS" if worst_ready <= max_ready_wait_ms else "FAIL",
        worst_ready))
    lines.append("rule: priority-inversion <= %d : %s (%d found)" % (
        max_inversions,
        "PASS" if len(inversions) <= max_inversions else "FAIL",
        len(inversions)))
    for inv in inversions:
        lines.append(
            "  inversion: pid:%d tid:%d prio:%d blocked %s for %s ms "
            "while pid:%d tid:%d (prio %s) ran and pid:%d tid:%d (prio %s) "
            "starved ready (overlap %s ms)"
            % (inv["thread"][0], inv["thread"][1], inv["priority"],
               inv["state"], fmt_ms(inv["duration_ms"]),
               inv["blocker"][0], inv["blocker"][1],
               inv["blocker_priority"],
               inv["holder"][0], inv["holder"][1],
               inv["holder_priority"], fmt_ms(inv["overlap_ms"])))
    failed = (worst_ready > max_ready_wait_ms
              or len(inversions) > max_inversions)
    lines.append("")
    lines.append("RESULT: %s" % ("FAIL" if failed else "PASS"))
    return "\n".join(lines), failed


def html_report(stats, inversions, intervals, names, cycles_per_sec,
                t_start, t_end, max_ready_wait_ms, max_inversions):
    total_s = (t_end - t_start) / cycles_per_sec
    worst_ready = max((s["worst_ready_ms"] for s in stats.values()),
                      default=0.0)
    failed = (worst_ready > max_ready_wait_ms
              or len(inversions) > max_inversions)

    threads = sorted(stats, key=lambda k: -stats[k]["cpu_s"])
    width, row_h, label_w = 900, 26, 220
    height = 40 + len(threads) * row_h + 30
    span = max(t_end - t_start, 1)

    def x(cycles):
        return label_w + (cycles - t_start) / span * (width - label_w - 20)

    svg = ['<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d">'
           % (width, height)]
    svg.append('<rect x="0" y="0" width="%d" height="%d" fill="#ffffff"/>'
               % (width, height))
    # Time gridlines every 10% of the span.
    for i in range(11):
        gx = label_w + i / 10.0 * (width - label_w - 20)
        ms = (t_start + span * i / 10.0 - t_start) / cycles_per_sec * 1000
        svg.append('<line x1="%.1f" y1="30" x2="%.1f" y2="%d" stroke="#e5e5e5"/>'
                   % (gx, gx, 30 + len(threads) * row_h))
        svg.append('<text x="%.1f" y="20" font-size="10" fill="#666" '
                   'text-anchor="middle">%.0f ms</text>' % (gx, ms))
    for r, thread in enumerate(threads):
        y = 40 + r * row_h
        label = html.escape(thread_label(thread, names))
        svg.append('<text x="%d" y="%d" font-size="11" fill="#222">%s</text>'
                   % (label_w - 8, y + 4, label))
        for iv in intervals[thread]:
            x0, x1 = x(iv.start), x(iv.end)
            if x1 - x0 < 0.5:
                x1 = x0 + 0.5
            color = STATE_COLORS.get(iv.state, DEFAULT_COLOR)
            title = "%s %.3f ms" % (iv.state,
                                     iv.duration / cycles_per_sec * 1000)
            svg.append('<rect x="%.1f" y="%d" width="%.1f" height="14" '
                       'fill="%s"><title>%s</title></rect>'
                       % (x0, y - 7, x1 - x0, color,
                          html.escape(title)))
    # Legend.
    lx = label_w
    for state, color in STATE_COLORS.items():
        svg.append('<rect x="%d" y="%d" width="12" height="12" fill="%s"/>'
                   % (lx, height - 20, color))
        svg.append('<text x="%d" y="%d" font-size="10" fill="#333">%s</text>'
                   % (lx + 15, height - 10, state))
        lx += 15 + len(state) * 6 + 18
    svg.append("</svg>")

    rows = []
    for thread in threads:
        s = stats[thread]
        rows.append("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td>"
                    "<td>%s</td><td>%s</td></tr>" % (
                        html.escape(thread_label(thread, names)),
                        fmt_ms(s["cpu_s"] * 1000),
                        fmt_ms(s["ready_s"] * 1000),
                        fmt_ms(s["blocked_s"] * 1000),
                        fmt_ms(s["worst_ready_ms"]),
                        fmt_ms(s["worst_block_ms"])))
    inv_rows = []
    for inv in inversions:
        inv_rows.append(
            "<tr><td>pid:%d tid:%d (prio %d)</td><td>%s</td>"
            "<td>%s ms</td><td>pid:%d tid:%d (prio %s)</td>"
            "<td>pid:%d tid:%d (prio %s)</td>"
            "<td>%s ms</td></tr>" % (
                inv["thread"][0], inv["thread"][1], inv["priority"],
                inv["state"], fmt_ms(inv["duration_ms"]),
                inv["blocker"][0], inv["blocker"][1],
                inv["blocker_priority"],
                inv["holder"][0], inv["holder"][1],
                inv["holder_priority"], fmt_ms(inv["overlap_ms"])))

    return """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>qnx-trace-lens report</title>
<style>body{font-family:system-ui,sans-serif;margin:24px;color:#222}
h1{font-size:20px}h2{font-size:16px;margin-top:28px}
table{border-collapse:collapse}td,th{border:1px solid #ccc;padding:4px 8px;
font-size:13px;text-align:right}td:first-child,th:first-child{text-align:left}
.pass{color:#1e7d32;font-weight:700}.fail{color:#b71c1c;font-weight:700}
svg{border:1px solid #ddd}</style></head><body>
<h1>qnx-trace-lens report</h1>
<p>Trace span %.3f s &middot; %d threads &middot; cycles/sec %d</p>
<p>Worst ready-wait: %.3f ms (limit %.3f ms) &mdash;
<span class="%s">%s</span></p>
<p>Priority inversions: %d (limit %d) &mdash;
<span class="%s">%s</span></p>
<h2>Timeline</h2>
%s
<h2>Per-thread statistics (ms)</h2>
<table><tr><th>thread</th><th>cpu</th><th>ready-wait</th><th>blocked</th>
<th>worst ready</th><th>worst block</th></tr>
%s</table>
<h2>Priority inversions</h2>
<table><tr><th>blocked thread</th><th>state</th><th>blocked for</th>
<th>ran meanwhile (middle prio)</th><th>starved holder (low prio)</th>
<th>overlap</th></tr>
%s</table>
<p><small>Generated by qnx-trace-lens %s. Times are wall time from the
trace; on QEMU (TCG) use them for practice, not as measurements.</small></p>
</body></html>""" % (
        total_s, len(stats), cycles_per_sec,
        worst_ready, max_ready_wait_ms,
        "pass" if worst_ready <= max_ready_wait_ms else "fail",
        "PASS" if worst_ready <= max_ready_wait_ms else "FAIL",
        len(inversions), max_inversions,
        "pass" if len(inversions) <= max_inversions else "fail",
        "PASS" if len(inversions) <= max_inversions else "FAIL",
        "\n".join(svg), "\n".join(rows),
        "\n".join(inv_rows) if inv_rows else
        "<tr><td colspan=6>none detected</td></tr>",
        VERSION)


def cmd_check(args):
    try:
        cycles_per_sec, events, names = parse_trace(args.trace)
    except (OSError, ValueError) as exc:
        print("qnx-trace-lens: %s" % exc, file=sys.stderr)
        return 2
    if args.cycles_per_sec:
        cycles_per_sec = args.cycles_per_sec
    intervals, t_start, t_end = build_intervals(events)
    stats, inversions = analyze(intervals, cycles_per_sec,
                                args.min_block_ms)
    text, failed = text_summary(stats, inversions, cycles_per_sec,
                                t_start, t_end,
                                args.max_ready_wait_ms,
                                args.max_inversions)
    print(text)
    if args.html:
        report = html_report(stats, inversions, intervals, names,
                             cycles_per_sec, t_start, t_end,
                             args.max_ready_wait_ms, args.max_inversions)
        with open(args.html, "w") as fh:
            fh.write(report)
        print("html report: %s" % args.html)
    return 1 if failed else 0


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="trace_lens.py",
        description="Analyze a QNX 8.0 traceprinter text file.")
    ap.add_argument("--version", action="version",
                    version="qnx-trace-lens %s" % VERSION)
    sub = ap.add_subparsers(dest="cmd", required=True)
    ck = sub.add_parser("check", help="check a trace against the rules")
    ck.add_argument("trace", help="traceprinter text file")
    ck.add_argument("--html", help="write self-contained HTML report")
    ck.add_argument("--max-ready-wait-ms", type=float, default=5.0)
    ck.add_argument("--max-inversions", type=int, default=0)
    ck.add_argument("--min-block-ms", type=float, default=1.0)
    ck.add_argument("--cycles-per-sec", type=int, default=0)
    args = ap.parse_args(argv)
    if args.cmd == "check":
        return cmd_check(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
