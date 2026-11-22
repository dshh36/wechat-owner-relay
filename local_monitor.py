"""Scan locally; optionally resume the approved session on new preview events."""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

from watcher import ROOT, STATUS, STOP, WINDOW_RECOVERY_AMBIGUOUS, WINDOW_UNAVAILABLE, load, now, save
from event_bridge import CONFIG, maybe_dispatch
from desktop_bridge import CONFIG as DESKTOP_CONFIG, maybe_dispatch as desktop_dispatch
from wechat_notifier import CONFIG as NOTIFIER_CONFIG, tick as notifier_tick, in_flight

MONITOR_STATUS = ROOT / "local_monitor.json"
LOCK = ROOT / "local_monitor.lock"
RECOVERABLE_SCAN_REASONS = {
    "Window session or size changed; recalibration required",
    f"RuntimeError: {WINDOW_UNAVAILABLE}",
    f"RuntimeError: {WINDOW_RECOVERY_AMBIGUOUS}",
    "RuntimeError: WeChat window is too small for this calibration",
    "RuntimeError: Window-only capture failed",
    "RuntimeError: Window capture is blank",
    "No reliable sidebar previews; OCR/calibration not verified",
    "Local scan timed out",
}


def wait_for_stop(seconds):
    deadline = time.monotonic() + seconds
    while not STOP.exists():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.5, remaining))
    return True


def scan_once():
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    try:
        result = subprocess.run(
            [sys.executable, str(ROOT / "watcher.py"), "--scan"],
            cwd=ROOT, env=env, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=45,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        return {"status": "paused", "reason": "Local scan timed out"}
    status = load(STATUS, {})
    if result.returncode != 0 or status.get("status") != "scan_ok":
        return {
            "status": "paused",
            "reason": status.get("reason", "Local scan did not complete normally"),
        }
    return status


def monitor_loop(interval, cycles, state, record, poll_interval=1):
    next_scan = 0
    while not STOP.exists():
        # Scan and result polling use separate clocks. Queue writes stay serial.
        if not in_flight() and time.monotonic() >= next_scan:
            status = scan_once()
            if status["status"] != "scan_ok":
                reason = status.get("reason", "Scan unavailable")
                if reason in RECOVERABLE_SCAN_REASONS:
                    record("waiting_for_window", reason=reason)
                    next_scan = time.monotonic() + interval
                    if wait_for_stop(interval):
                        break
                    continue
                record("paused", reason=status.get("reason", "Scan unavailable"))
                return
            state["scans"] += 1
            next_scan = time.monotonic() + interval
            record(
                "running", reason=None, last_scan_at=status["checked_at"],
                pending=status.get("pending", 0),
                new_candidates=status.get("new_candidates", 0),
            )
            bridge = maybe_dispatch()
            desktop = desktop_dispatch()
            state.update(bridge_status=bridge["status"], desktop_bridge_status=desktop["status"])
        notification = notifier_tick()
        record("running", notifier_status=notification["status"])
        if cycles and state["scans"] >= cycles:
            record("completed")
            return
        if wait_for_stop(poll_interval):
            break
    record("stopped")


def run(interval, cycles, poll_interval=1):
    import msvcrt

    # The OS releases this lock even if the process is terminated unexpectedly.
    with LOCK.open("a+b") as lock:
        lock.seek(0, 2)
        if lock.tell() == 0:
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise SystemExit("Local monitor is already running")
        state = {
            "pid": os.getpid(), "started_at": now(), "interval_seconds": interval,
            "result_poll_seconds": poll_interval,
            "idle_model_calls": False,
            "event_model_enabled": bool(load(CONFIG, {}).get("enabled")),
            "desktop_notifications_enabled": bool(load(DESKTOP_CONFIG, {}).get("enabled")),
            "clawbot_notifications_enabled": bool(load(NOTIFIER_CONFIG, {}).get("enabled")),
            "automatic_sending": False, "scans": 0,
        }

        def record(status, **fields):
            state.update(status=status, checked_at=now(), **fields)
            save(MONITOR_STATUS, state)

        try:
            record("starting")
            monitor_loop(interval, cycles, state, record, poll_interval)
        except Exception as error:
            record("paused", reason=f"{type(error).__name__}: {error}")
            raise
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval", type=float, default=5)
    parser.add_argument("--poll-interval", type=float, default=1)
    parser.add_argument("--cycles", type=int, default=0)
    args = parser.parse_args()
    if args.interval < 5 or args.cycles < 0 or not 0.5 <= args.poll_interval <= args.interval:
        parser.error("Scan interval >= 5; 0.5 <= poll interval <= scan interval; cycles >= 0")
    run(args.interval, args.cycles, args.poll_interval)
