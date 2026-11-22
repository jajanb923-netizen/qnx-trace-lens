// rt-pulse.c — deterministic RT workload for qnx-trace-lens.
//
// Three SCHED_FIFO threads sharing one mutex:
//   LOW  (prio 10)  — holds the mutex for a long critical section.
//   MED  (prio 20)  — burns CPU in a loop, never touches the mutex.
//   HIGH (prio 30)  — periodically tries to take the mutex.
//
// Classic priority inversion (default mode, --no-pi): while LOW holds the
// mutex, MED preempts LOW, so HIGH waits far longer than the critical
// section itself. With --pi the mutex uses PTHREAD_PRIO_INHERIT, so LOW is
// boosted to HIGH's priority while HIGH waits and the inversion shrinks.
//
// Build (QNX 8.0 host):  qcc -Vgcc_ntox86_64 -static -o rt-pulse rt-pulse.c
//   (static link: the minimal target tree currently lacks libgcc_s.so.1;
//    see BUILD-NOTES.md — dynamic link is the goal once the runtime lib
//    package is installed.)
// Run in the QEMU guest:  ./rt-pulse [--seconds N] [--pi|--no-pi]
// Then capture: tracelogger -w -s <N+2> -f /tmp/rt-pulse.kev
//
// SPDX-License-Identifier: MIT

#include <errno.h>
#include <pthread.h>
#include <sched.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#define LOW_PRIO   10
#define MED_PRIO   20
#define HIGH_PRIO  30

static pthread_mutex_t g_lock;
static volatile int    g_stop;
static volatile long   g_low_holds;    // critical sections completed by LOW
static volatile long   g_high_waits;   // mutex acquisitions completed by HIGH
static volatile long   g_med_spins;    // work units completed by MED

static void die(const char *what, int err)
{
    fprintf(stderr, "rt-pulse: %s: %s\n", what, strerror(err));
    exit(1);
}

static void set_fifo(pthread_t th, int prio)
{
    struct sched_param p;
    memset(&p, 0, sizeof(p));
    p.sched_priority = prio;
    int rc = pthread_setschedparam(th, SCHED_FIFO, &p);
    if (rc != 0)
        die("pthread_setschedparam", rc);
}

/* Busy-work that the compiler cannot optimise away. */
static void burn_ms(long ms)
{
    struct timespec t0, now;
    clock_gettime(CLOCK_MONOTONIC, &t0);
    volatile uint64_t x = 0;
    for (;;) {
        for (int i = 0; i < 1000; i++)
            x += (uint64_t)(i * 2654435761u) ^ (x >> 13);
        clock_gettime(CLOCK_MONOTONIC, &now);
        long elapsed = (now.tv_sec - t0.tv_sec) * 1000
                     + (now.tv_nsec - t0.tv_nsec) / 1000000;
        if (elapsed >= ms)
            break;
    }
    (void)x;
}

static void *low_thread(void *arg)
{
    (void)arg;
    set_fifo(pthread_self(), LOW_PRIO);
    while (!g_stop) {
        pthread_mutex_lock(&g_lock);
        /* Critical section: 40 ms of work while holding the lock. */
        burn_ms(40);
        g_low_holds++;
        pthread_mutex_unlock(&g_lock);
        /* Short breather so HIGH gets a chance to queue on the lock. */
        struct timespec ts = { 0, 5 * 1000 * 1000 };
        nanosleep(&ts, NULL);
    }
    return NULL;
}

static void *med_thread(void *arg)
{
    (void)arg;
    set_fifo(pthread_self(), MED_PRIO);
    while (!g_stop) {
        /* CPU hog: 25 ms of work, never touches the mutex. */
        burn_ms(25);
        g_med_spins++;
        struct timespec ts = { 0, 2 * 1000 * 1000 };
        nanosleep(&ts, NULL);
    }
    return NULL;
}

static void *high_thread(void *arg)
{
    (void)arg;
    set_fifo(pthread_self(), HIGH_PRIO);
    while (!g_stop) {
        /* Periodic 100 ms: try to take the mutex, do 2 ms of work. */
        struct timespec ts = { 0, 100 * 1000 * 1000 };
        nanosleep(&ts, NULL);
        if (g_stop)
            break;
        pthread_mutex_lock(&g_lock);
        burn_ms(2);
        g_high_waits++;
        pthread_mutex_unlock(&g_lock);
    }
    return NULL;
}

int main(int argc, char **argv)
{
    int seconds = 10;
    bool use_pi = false;

    for (int i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--pi") == 0)
            use_pi = true;
        else if (strcmp(argv[i], "--no-pi") == 0)
            use_pi = false;
        else if (strcmp(argv[i], "--seconds") == 0 && i + 1 < argc)
            seconds = atoi(argv[++i]);
        else if (strncmp(argv[i], "--seconds=", 10) == 0)
            seconds = atoi(argv[i] + 10);
        else {
            fprintf(stderr,
                "usage: %s [--seconds N] [--pi|--no-pi]\n", argv[0]);
            return 2;
        }
    }

    pthread_mutexattr_t attr;
    pthread_mutexattr_init(&attr);
    pthread_mutexattr_setprotocol(&attr, use_pi ? PTHREAD_PRIO_INHERIT
                                                : PTHREAD_PRIO_NONE);
    pthread_mutex_init(&g_lock, &attr);

    printf("rt-pulse: %d seconds, mutex protocol=%s\n",
           seconds, use_pi ? "PRIO_INHERIT" : "NONE (inversion expected)");
    printf("rt-pulse: LOW=%d MED=%d HIGH=%d (SCHED_FIFO)\n",
           LOW_PRIO, MED_PRIO, HIGH_PRIO);
    fflush(stdout);

    pthread_t low, med, high;
    int rc;
    if ((rc = pthread_create(&low, NULL, low_thread, NULL)) != 0)
        die("pthread_create low", rc);
    if ((rc = pthread_create(&med, NULL, med_thread, NULL)) != 0)
        die("pthread_create med", rc);
    if ((rc = pthread_create(&high, NULL, high_thread, NULL)) != 0)
        die("pthread_create high", rc);

    sleep((unsigned)seconds);
    g_stop = 1;

    pthread_join(low, NULL);
    pthread_join(med, NULL);
    pthread_join(high, NULL);

    printf("rt-pulse: done. low_holds=%ld high_waits=%ld med_spins=%ld\n",
           g_low_holds, g_high_waits, g_med_spins);
    return 0;
}
