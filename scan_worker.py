"""One bounded, reusable OCR process; no notification dispatch in the worker."""
from __future__ import annotations

import contextlib
import ctypes
import multiprocessing
import os
import time


def worker_main(connection):
    # Set these before importing NumPy/ONNX; inherited machine defaults may be high.
    for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
                 "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
        os.environ[name] = "1"
    if os.name == "nt":
        ctypes.windll.kernel32.SetPriorityClass(
            ctypes.windll.kernel32.GetCurrentProcess(), 0x00004000)
    import watcher

    try:
        with open(os.devnull, "w", encoding="utf-8") as sink:
            while connection.recv() == "scan":
                wall, cpu = time.monotonic(), time.process_time()
                try:
                    with contextlib.redirect_stdout(sink):
                        watcher.main(["--scan"])
                    status = watcher.load(watcher.STATUS, {})
                except Exception as error:
                    if str(error) == watcher.WINDOW_UNAVAILABLE:
                        state = watcher.load(watcher.STATE, {})
                        state["rebase_on_next_scan"] = True
                        watcher.save(watcher.STATE, state)
                    status = {"status": "paused",
                              "reason": f"{type(error).__name__}: {error}"}
                    with contextlib.redirect_stdout(sink):
                        watcher.report("paused", reason=status["reason"])
                    status = watcher.load(watcher.STATUS, status)
                status["scan_metrics"] = {
                    "wall_seconds": round(time.monotonic() - wall, 3),
                    "cpu_seconds": round(time.process_time() - cpu, 3),
                    "worker_pid": os.getpid(),
                }
                connection.send(status)
    except (EOFError, BrokenPipeError):
        pass
    finally:
        connection.close()


class ScanWorker:
    def __init__(self, max_scans=500):
        self.process = None
        self.connection = None
        self.scans = 0
        self.max_scans = max_scans

    def close(self):
        if self.process is not None:
            if self.process.is_alive():
                self.process.terminate()
            self.process.join(timeout=2)
            if self.process.is_alive():
                self.process.kill()
                self.process.join(timeout=2)
            self.process.close()
            self.process = None
        if self.connection is not None:
            self.connection.close()
            self.connection = None
        self.scans = 0

    def scan(self, timeout=45, should_stop=None):
        if should_stop is not None and should_stop():
            self.close()
            return {"status": "stopped"}
        if (self.process is None or not self.process.is_alive()
                or self.scans >= self.max_scans):
            self.close()
            context = multiprocessing.get_context("spawn")
            self.connection, child = context.Pipe()
            self.process = context.Process(target=worker_main, args=(child,), daemon=True)
            try:
                self.process.start()
            except Exception:
                self.process.close()
                self.process = None
                self.connection.close()
                self.connection = None
                raise
            finally:
                child.close()
        try:
            self.connection.send("scan")
            deadline = time.monotonic() + timeout
            while True:
                if should_stop is not None and should_stop():
                    self.close()
                    return {"status": "stopped"}
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.close()
                    return {"status": "paused", "reason": "Local scan timed out"}
                if self.connection.poll(min(0.2, remaining)):
                    break
            status = self.connection.recv()
            self.scans += 1
            return status
        except (EOFError, BrokenPipeError, OSError):
            self.close()
            return {"status": "paused", "reason": "Local scanner exited unexpectedly"}
