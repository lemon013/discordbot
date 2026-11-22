"""Small crash/host diagnostics. Never read message bodies, environment or cmdlines."""
import asyncio
import faulthandler
import logging
import os
from pathlib import Path
import signal
import sys
import threading
import time
import uuid

log = logging.getLogger(__name__)
ACTIVE = None


def process_memory(proc=Path('/proc'), pid=None):
    """RSS sum is approximate (shared pages can be counted more than once)."""
    pid = os.getpid() if pid is None else pid
    rows = {}
    try:
        directories = list(proc.iterdir())
    except OSError:
        return None, None, None
    for directory in directories:
        if not directory.name.isdigit():
            continue
        try:
            fields = dict(line.split(':', 1) for line in (directory / 'status').read_text().splitlines() if ':' in line)
            rows[int(directory.name)] = (int(fields['PPid']), int(fields.get('VmRSS', '0').split()[0]) / 1024)
        except (OSError, ValueError, KeyError, IndexError):
            continue
    descendants = {pid}
    while True:
        found = {child for child, (parent, _) in rows.items() if parent in descendants}
        if found <= descendants:
            break
        descendants.update(found)
    if pid not in rows:
        return None, None, None
    return rows[pid][1], sum(rows[p][1] for p in descendants if p in rows), len(descendants) - 1


class RuntimeProbe:
    def __init__(self):
        self.boot = uuid.uuid4().hex[:12]
        self.started = time.monotonic()
        self.tick = None
        self.beat_task = None
        self.stop_event = threading.Event()
        self.last_wall, self.last_cpu = self.started, time.process_time()
        self.thread = threading.Thread(target=self.watch, name='runtime-probe', daemon=True)

    def sample(self):
        now, cpu = time.monotonic(), time.process_time()
        cpu_pct = 100 * (cpu - self.last_cpu) / max(.001, now - self.last_wall)
        self.last_wall, self.last_cpu = now, cpu
        rss, tree_rss, children = process_memory()
        lag = max(0, now - self.tick - 1) if self.tick is not None else None
        log.info('Runtime sample (boot=%s, uptime_s=%.0f, cpu_pct=%.1f, rss_mib=%s, process_tree_rss_mib=%s, children=%s, loop_lag_s=%s)',
                 self.boot, now - self.started, cpu_pct, rss, tree_rss, children,
                 round(lag, 1) if lag is not None else None)
        if lag is not None and lag >= 5:
            # Frame identifiers only: no locals, source lines, URLs or exception bodies.
            frame = sys._current_frames().get(threading.main_thread().ident)
            stack = []
            while frame is not None and len(stack) < 12:
                stack.append(f'{Path(frame.f_code.co_filename).name}:{frame.f_lineno}:{frame.f_code.co_name}')
                frame = frame.f_back
            del frame
            log.warning('Event loop delayed (boot=%s, lag_s=%.1f, main_stack=%s)', self.boot, lag, ' <- '.join(stack))

    def watch(self):
        while not self.stop_event.wait(15):
            try:
                self.sample()
            except Exception as exc:
                log.warning('Runtime sample unavailable (error_type=%s)', type(exc).__name__)

    async def beat(self):
        try:
            while True:
                self.tick = time.monotonic()
                await asyncio.sleep(1)
        finally:
            self.tick = None

    def bind(self):
        if self.beat_task is None or self.beat_task.done():
            self.beat_task = asyncio.create_task(self.beat(), name='runtime-heartbeat')

    def signal_handler(self, signum, frame, previous):
        os.write(2, f'Runtime termination signal (boot={self.boot}, signal={signum}, uptime_s={time.monotonic() - self.started:.0f})\n'.encode())
        if callable(previous):
            previous(signum, frame)
        elif previous == signal.SIG_DFL:
            # Preserve the original signal exit; do not restart or swallow termination.
            signal.signal(signum, signal.SIG_DFL)
            os.kill(os.getpid(), signum)


def install():
    global ACTIVE
    if ACTIVE is not None:
        return ACTIVE
    probe = ACTIVE = RuntimeProbe()
    try:
        faulthandler.enable(all_threads=True)
    except (OSError, RuntimeError, AttributeError):
        log.warning('Native fault trace unavailable')
    if os.name != 'nt':
        for signum in (signal.SIGTERM, signal.SIGHUP):
            previous = signal.getsignal(signum)
            if previous != signal.SIG_IGN:
                signal.signal(signum, lambda sig, frame, old=previous: probe.signal_handler(sig, frame, old))
    log.info('Runtime boot (boot=%s, pid=%s)', probe.boot, os.getpid())
    probe.thread.start()
    return probe
