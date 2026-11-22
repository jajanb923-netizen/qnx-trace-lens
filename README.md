# qnx-trace-lens

A scheduling analyzer for QNX 8.0 kernel traces, plus `rt-pulse`, a small
real-time workload that produces interesting traces to analyze.

## What it does

`trace_lens.py` reads the text output of `traceprinter` (from a
`tracelogger -w` capture — wide mode, which carries each thread's priority
and policy) and:

- computes per-thread CPU time, ready-queue wait, and blocking time, with
  worst-case ready-wait and worst-case block per thread;
- detects **priority inversions** (classic unbounded case: a high-priority
  thread waits on a lock while a middle-priority thread runs and the
  low-priority lock holder starves in the ready queue);
- emits a self-contained HTML report (inline SVG timeline + tables) and a
  text summary;
- exits non-zero when a rule is violated, so it can gate CI.

Python 3, standard library only. No third-party dependencies.

## Capturing a real trace (QNX 8.0 QEMU guest)

```sh
# In the guest (over SSH, localhost:2222). tracelogger runs in the
# background while rt-pulse runs in the foreground:
tracelogger -w -s 12 -f /tmp/rt-pulse.kev &
./rt-pulse --seconds 10            # --pi for the priority-inheritance run
wait

# On the host:
scp -P 2222 root@localhost:/tmp/rt-pulse.kev .
source ~/qnx800/qnxsdp-env.sh
traceprinter -f rt-pulse.kev > rt-pulse.txt
python3 trace_lens.py check rt-pulse.txt --html rt-pulse.html
```

Always use `tracelogger -w`: only wide mode records priorities. Note
`traceprinter` needs `-f <file>` — a bare filename argument is ignored
(it defaults to the live `/dev/shmem/tracebuffer.kev` and fails).

## Real-trace validation (QNX 8.0 QEMU guest, 22 Nov 2026)

Validated against genuine guest captures in `traces-real/` (12 s
`tracelogger -w` each; QNX 8.0.0, QEMU x86_64, 256 MB RAM; guest built
with `mkqnximage --type=qemu --arch=x86_64 --ram=256M`). `trace_lens.py`
v0.1.1.

**The 1-CPU guest is the honest demo configuration.** With `-smp 1` the
classic inversion is fully visible; with `-smp 2` LOW and MED run on
separate CPUs, so the inversion is bounded by one 40 ms critical section
in both modes.

| trace (1 CPU, `-smp 1`) | inversions | HIGH worst mutex block | LOW CPU / ready-wait |
|---|---|---|---|
| `trace-1cpu-nopi` (`--no-pi`) | **40** | 55.4 ms | 687 ms / 8,764 ms |
| `trace-1cpu-pi` (`--pi`) | **0** | 36.2 ms | 1,967 ms / 7,427 ms |

Without priority inheritance, LOW is starved in the ready queue for
8.8 s of the 12 s trace while MED burns CPU and HIGH waits on the mutex
— 40 detected inversions, worst single HIGH block 55.4 ms. With
`PTHREAD_PRIO_INHERIT`, LOW inherits HIGH's priority while it holds the
mutex, so it is never ready-starved while HIGH waits: **0 inversions**,
LOW's CPU share nearly triples (687 → 1,967 ms), and HIGH's worst block
drops to 36.2 ms. MED pays for it — its ready-wait rises 282 → 1,717 ms —
which is the inheritance mechanism working as designed.

| trace (2 CPUs, `-smp 2`) | inversions | HIGH worst mutex block |
|---|---|---|
| `trace-nopi` (`--no-pi`) | 25 | 47.7 ms |
| `trace-pi` (`--pi`) | 11 | 48.3 ms |

On two CPUs the inversion is bounded by the ~40 ms critical section in
both modes, so the contrast is smaller — the tool still detects the
difference (25 vs 11), but the 1-CPU pair is the demonstration.

Reproduce the numbers:

```sh
python3 trace_lens.py check traces-real/trace-1cpu-nopi.txt
python3 trace_lens.py check traces-real/trace-1cpu-pi.txt
```

(Exit status is 1 for both: the default 5 ms worst-ready-wait gate also
counts system idle threads, whose ready waits are long by design. The
inversion rule — the core check — is 40 vs 0.)

## Usage

```sh
python3 trace_lens.py check TRACE.txt [--html REPORT.html]
                                      [--max-ready-wait-ms 5.0]
                                      [--max-inversions 0]
                                      [--min-block-ms 1.0]
```

Exit status: `0` = all rules pass, `1` = a rule was violated,
`2` = usage error or unparseable trace.

## rt-pulse

`rt-pulse/` is a deterministic SCHED_FIFO workload (LOW prio 10 holds a
mutex for 40 ms, MED prio 20 burns CPU, HIGH prio 30 waits on the mutex).
Default mode (`--no-pi`) shows a real priority inversion; `--pi` enables
`PTHREAD_PRIO_INHERIT` on the mutex so the inversion shrinks — run both
and compare the reports.

```sh
source ~/qnx800/qnxsdp-env.sh
qcc -Vgcc_ntox86_64 -o rt-pulse rt-pulse.c        # dynamic link
qcc -Vgcc_ntox86_64 -static -o rt-pulse rt-pulse.c  # static fallback
```

## Tests

`tests/` contains synthetic traceprinter-format fixtures (clearly labelled
synthetic — they model the rt-pulse scenario for parser testing only) and
a self-test:

```sh
python3 tests/test_trace_lens.py
```

Real-trace validation is done against genuine guest captures; see
"Real-trace validation" above and `BUILD-NOTES.md` in the parent
directory.

## Reproducing the QEMU guest

QNX SDP 8.0 (host toolchain + target packages installed via the QNX
Software Center). The guest image is built with the stock `mkqnximage`
QEMU template plus two local template fixes (both documented in
`BUILD-NOTES.md`): the RSA host-key line removed from `system.build`
(QNX 8.0's sshd segfaults loading any RSA host key) and `UsePAM no`
(the boot-time sshd, running as the secpol sshd user, fails PAM
initialisation — password auth is already off, so PAM is unused).

```sh
source ~/qnx800/qnxsdp-env.sh
mkqnximage --type=qemu --arch=x86_64 --ram=256M \
  --ssh-ident=$HOME/.ssh/id_ed25519.pub --build --noprompt
# Boot: qemu-system-x86_64 -smp 1 --cpu max -m 256M ... (see BUILD-NOTES.md)
# SSH: ssh -i ~/.ssh/id_ed25519 -p 2222 root@localhost
```

## How this differs from kevlint

kevlint (same author as the tutorial this project follows) is a pass/fail
rule checker over traceprinter text. trace-lens is an analyzer and
visualizer: per-thread latency statistics, worst-case numbers, an
interactive HTML timeline, and CI-gateable exit codes — a different
purpose, with its own parser written from scratch.

## License

MIT. See LICENSE.
