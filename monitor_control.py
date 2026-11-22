"""User-operated detector and existing ClawBot process controls."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
import queue
import subprocess
import sys
import threading
import time

from watcher import ROOT, STOP, load, now, save
import clawbot_relay as relay
import wechat_notifier as notifier

MONITOR_STATUS = ROOT / "local_monitor.json"
NOTIFIER_STATE = ROOT / "wechat_notifier_state.json"
MONITOR_LOCK = ROOT / "local_monitor.lock"
CONTROL_LOCK = ROOT / "monitor_control.lock"


class ControlError(RuntimeError):
    pass


@contextmanager
def file_lock(path):
    import msvcrt

    with path.open("a+b") as stream:
        stream.seek(0, 2)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        try:
            yield
        finally:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


def monitor_running():
    return process_running(MONITOR_LOCK)


def notification_resume_available():
    state = load(NOTIFIER_STATE, {})
    queue = load(notifier.QUEUE, [])
    return (
        state.get("status") == "paused"
        and (
            (notifier.uncertain_notification(state) and load(notifier.CONFIG, {}).get("enabled"))
            or (state.get("last_result") == "verification_unavailable"
             and bool(state.get("notification_accepted_at"))
             and bool(state.get("reply_seen_at")))
            or (state.get("reason") == "RuntimeError"
                and bool(state.get("marker")) and bool(state.get("ids"))
                and "chunk" not in state and not state.get("turn_id")
                and state.get("failure_stage") != "before_send"
                and bool(state.get("submitted_at"))
                and state.get("notification_accepted_at", "") < state["submitted_at"])
        )
        and not any(event.get("state") == "reply_started" for event in queue)
    )


def resume_notifications():
    with file_lock(CONTROL_LOCK):
        if not notification_resume_available():
            raise ControlError("当前提醒状态不能安全恢复，请先核查未确认操作")
        try:
            state = load(NOTIFIER_STATE, {})
            if notifier.uncertain_notification(state):
                return notifier.resume_after_uncertain_notification()
            if state.get("last_result") != "verification_unavailable":
                return notifier.resume_interrupted_verification()
            return notifier.resume_after_verification_failure()
        except RuntimeError as error:
            raise ControlError(str(error)) from error


def process_running(path):
    try:
        with file_lock(path):
            return False
    except OSError:
        return True


def relay_status():
    running = process_running(relay.LOCK)
    record = load(relay.STATUS, {})
    return {
        "enabled": running and not relay.STOP.exists(),
        "running": running,
        "status": record.get("status", "starting") if running else (
            "paused" if not relay.STOP.exists()
            and record.get("status") == "paused" else "off"),
        "reason": record.get("reason"),
        "busy": running and bool(record.get("has_pending_message")),
    }


def validate_desktop_connection(config):
    try:
        view = relay.desktop_request(config["thread_id"])
    except Exception as error:
        raise ControlError("无法连接电脑会话，未改动未完成消息") from error
    if view.get("threadId") != config["thread_id"]:
        raise ControlError("电脑会话不匹配，未改动未完成消息")
    if not view.get("live"):
        raise ControlError("桌面会话通信未连接，未改动未完成消息")


def relay_start_state():
    config = load(relay.CONFIG, {})
    settings, _, _, _, identity = relay.binding(config)
    if not config.get("enabled"):
        raise ControlError("接力配置未启用，需要先检查配置")
    if settings.get("weixinConnectEnabled"):
        raise ControlError("请先在管理器停止原生微信连接")
    state = load(relay.STATE, {})
    if state and state.get("binding_fingerprint") != identity:
        raise ControlError("绑定账号已改变，需要重新核对")
    relay.ClawBotClient(settings)
    return config, state, identity


def validate_relay_start(discard_pending=False):
    config, state, identity = relay_start_state()
    if discard_pending:
        if process_running(relay.LOCK):
            raise ControlError("请先等待接力退出，再放弃旧回复")
        pending = [
            event for event in state.get("events", {}).values()
            if event.get("stage") not in relay.TERMINAL_EVENT_STAGES
        ]
        if any(event.get("stage") not in {
                "before_post", "awaiting_reply", "before_send"} for event in pending):
            raise ControlError("存在未知消息状态，需要先核查")
        for event in pending:
            event.update(
                discarded_from_stage=event["stage"],
                stage="discarded", discarded_at=now(),
                discard_reason="owner_authorized_reconnect_no_retry",
            )
        if pending:
            save(relay.STATE, state)
    try:
        relay.recover_interrupted_replies(config, state, identity, allow_resume=True)
    except relay.RelayError as error:
        raise ControlError("未完成消息无法安全恢复，需要先核查") from error
    if any(event.get("stage") not in relay.TERMINAL_EVENT_STAGES | {"awaiting_reply"}
           for event in state.get("events", {}).values()):
        raise ControlError("有未确认完成的消息，需要先核查")


def discard_relay_event(event_id):
    with file_lock(CONTROL_LOCK):
        if relay_status()["running"]:
            raise ControlError("请先停止接力，再放弃未完成消息")
        config = load(relay.CONFIG, {})
        if not config.get("enabled"):
            raise ControlError("接力配置未启用")
        settings, _, _, _, identity = relay.binding(config)
        if settings.get("weixinConnectEnabled"):
            raise ControlError("请先在管理器停止原生微信连接")
        state = load(relay.STATE, {})
        if state and state.get("binding_fingerprint") != identity:
            raise ControlError("绑定账号已改变，需要重新核对")
        events = state.get("events", {})
        event = events.get(event_id)
        if not event or event.get("stage") != "awaiting_reply":
            raise ControlError("只能放弃指定的等待回复事件")
        pending = [
            key for key, item in events.items()
            if item.get("stage") not in relay.TERMINAL_EVENT_STAGES
        ]
        if pending != [event_id]:
            raise ControlError("未完成事件不止一条，拒绝批量放弃")
        event.update(stage="discarded", discarded_at=now(),
                     discard_reason="user_confirmed")
        save(relay.STATE, state)
        return {"discarded": True, "remaining_pending": 0}


def stop_relay():
    current = relay_status()
    if current["busy"]:
        raise ControlError("正在处理手机消息，请回复结束后再切换")
    relay.STOP.touch()
    deadline = time.monotonic() + 40
    while process_running(relay.LOCK):
        if time.monotonic() >= deadline:
            raise ControlError("仍在等待接力退出，请稍后重试")
        time.sleep(0.2)


def set_relay_enabled(enabled, reconnect=False):
    with file_lock(CONTROL_LOCK):
        if not enabled:
            stop_relay()
            return relay_status()
        current = relay_status()
        if current["enabled"] and not reconnect:
            return current
        if current["busy"]:
            raise ControlError("正在处理手机消息，请回复结束后再重连")
        config, _, _ = relay_start_state()
        # Check the destination before stopping a writer or abandoning old replies.
        validate_desktop_connection(config)
        if reconnect:
            if current["running"]:
                stop_relay()
            validate_relay_start(discard_pending=True)
        else:
            validate_relay_start()
            if current["running"]:
                stop_relay()
        relay.STOP.unlink(missing_ok=True)
        subprocess.Popen(
            [sys.executable, str(ROOT / "clawbot_relay.py"), "--run"],
            cwd=ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        deadline = time.monotonic() + 10
        while not process_running(relay.LOCK):
            if time.monotonic() >= deadline:
                raise ControlError("接力未能启动，请检查连接")
            time.sleep(0.1)
        return relay_status()


def status():
    running = monitor_running()
    record = load(MONITOR_STATUS, {})
    return {
        "enabled": running and not STOP.exists(),
        "running": running,
        "status": "stopping" if running and STOP.exists() else (
            record.get("status", "starting") if running else (
                "paused" if not STOP.exists()
                and record.get("status") == "paused" else "off")),
        "reason": record.get("reason"),
        "notifier_status": load(NOTIFIER_STATE, {}).get("status", "not_initialized"),
        "notifier_resume_available": notification_resume_available(),
        "pid": record.get("pid") if running else None,
    }


def detector_label(current):
    if current["status"] == "waiting_for_window":
        if current.get("reason") == "RuntimeError: Pinned WeChat window is missing or minimized":
            return "检测已开启，已绑定的微信窗口不可用"
        return "检测已开启，等待微信恢复原窗口大小和聊天列表"
    if current["status"] == "paused":
        return "检测已暂停：" + str(current.get("reason") or "请检查微信账号和窗口")
    if current["status"] == "starting":
        return "正在启动，等待首次扫描"
    if current["status"] == "stopping":
        return "正在关闭"
    if current["enabled"]:
        if current["notifier_status"] in {"paused", "before_post", "before_send"}:
            return "检测运行中，提醒已暂停"
        return "检测运行中"
    return "已关闭"


def relay_label(current):
    if current["status"] == "paused":
        return "连接已暂停：" + str(current.get("reason") or "请检查桌面会话连接")
    return {
        "listening": "已连接",
        "awaiting_reply": "正在回复",
        "waiting_for_desktop": "等待电脑会话空闲",
        "waiting_for_desktop_connection": "桌面会话通信未连接",
        "off": "已断开",
    }.get(current["status"], "正在连接")


def wait_until_stopped(timeout=15):
    deadline = time.monotonic() + timeout
    while monitor_running():
        if time.monotonic() >= deadline:
            raise TimeoutError("Detector is still finishing its current local operation")
        time.sleep(0.1)


def set_enabled(enabled):
    with file_lock(CONTROL_LOCK):
        if not enabled:
            STOP.touch()
            wait_until_stopped()
            return status()
        if STOP.exists():
            # Do not clear a stop request before the previous process exits.
            wait_until_stopped()
        elif monitor_running():
            return status()
        STOP.unlink(missing_ok=True)
        subprocess.Popen(
            [sys.executable, str(ROOT / "local_monitor.py"),
            "--interval", "5", "--poll-interval", "1"],
            cwd=ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        deadline = time.monotonic() + 10
        while not monitor_running():
            if time.monotonic() >= deadline:
                STOP.touch()
                raise TimeoutError("Detector did not acquire its local lock")
            time.sleep(0.1)
        return status()


def panel():
    import tkinter as tk
    from tkinter import ttk

    window = tk.Tk()
    window.title("微信提醒开关")
    window.geometry("420x360")
    window.resizable(False, False)
    frame = ttk.Frame(window, padding=24)
    frame.pack(fill="both", expand=True)
    selected = tk.BooleanVar()
    relay_selected = tk.BooleanVar()
    label = tk.StringVar(value="正在读取状态")
    relay_text = tk.StringVar(value="正在读取状态")
    events = queue.Queue()
    busy = False
    errors = {}

    def change(target="detector", reconnect=False):
        nonlocal busy
        if busy:
            return
        busy = True
        errors.pop(target, None)
        for widget in (checkbox, relay_checkbox, reconnect_button, resume_button):
            widget.state(["disabled"])
        is_relay = target == "relay"
        output = relay_text if is_relay else label
        if target == "notifier":
            output.set("正在恢复提醒")
        else:
            enabled = True if reconnect else (
                relay_selected.get() if is_relay else selected.get())
            output.set("正在重新连接" if reconnect else (
                "正在开启" if enabled else "正在关闭"))

        def worker():
            try:
                result = (resume_notifications() if target == "notifier" else
                          set_relay_enabled(enabled, reconnect) if is_relay else
                          set_enabled(enabled))
                events.put(("done", target, result))
            except Exception as error:
                message = str(error) if isinstance(error, ControlError) else "操作未完成，请稍后重试"
                events.put(("error", target, message))

        threading.Thread(target=worker).start()

    checkbox = ttk.Checkbutton(
        frame, text="启用本地消息检测", variable=selected, command=lambda: change(),
    )
    checkbox.pack(anchor="w", pady=(4, 18))
    ttk.Label(frame, textvariable=label, wraplength=360).pack(anchor="w")
    resume_button = ttk.Button(
        frame, text="恢复提醒", command=lambda: change("notifier"),
    )
    resume_button.pack(anchor="e", pady=(4, 0))
    ttk.Separator(frame).pack(fill="x", pady=14)
    relay_checkbox = ttk.Checkbutton(
        frame, text="启用 ClawBot 手机接力", variable=relay_selected,
        command=lambda: change("relay"),
    )
    relay_checkbox.pack(anchor="w", pady=(0, 8))
    relay_row = ttk.Frame(frame)
    relay_row.pack(fill="x")
    ttk.Label(relay_row, textvariable=relay_text, wraplength=240).pack(side="left")
    reconnect_button = ttk.Button(
        relay_row, text="重新连接", command=lambda: change("relay", reconnect=True),
    )
    reconnect_button.pack(side="right")
    ttk.Button(frame, text="关闭窗口", command=window.destroy).pack(anchor="e", pady=(12, 0))

    def refresh():
        nonlocal busy
        try:
            while True:
                kind, target, value = events.get_nowait()
                busy = False
                for widget in (checkbox, relay_checkbox, reconnect_button):
                    widget.state(["!disabled"])
                if kind == "error":
                    errors[target] = value
                    (relay_text if target == "relay" else label).set(value)
        except queue.Empty:
            pass
        if not busy:
            try:
                current = status()
                selected.set(current["enabled"])
                if "notifier" in errors:
                    label.set(errors["notifier"])
                elif "detector" not in errors:
                    label.set(detector_label(current))
                resume_button.state(
                    ["!disabled"] if current["notifier_resume_available"]
                    else ["disabled"])
                phone = relay_status()
                relay_selected.set(phone["enabled"])
                if "relay" not in errors:
                    relay_text.set(relay_label(phone))
                for widget in (relay_checkbox, reconnect_button):
                    widget.state(["disabled"] if phone["busy"] else ["!disabled"])
            except Exception:
                label.set("暂时无法读取状态")
        window.after(1000, refresh)

    refresh()
    window.mainloop()


if __name__ == "__main__":
    if sys.stdout is not None:
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--on", action="store_true")
    action.add_argument("--off", action="store_true")
    action.add_argument("--status", action="store_true")
    action.add_argument("--panel", action="store_true")
    action.add_argument("--resume-notifications", action="store_true")
    action.add_argument("--relay-on", action="store_true")
    action.add_argument("--relay-off", action="store_true")
    action.add_argument("--relay-status", action="store_true")
    action.add_argument("--relay-discard-event", metavar="EVENT_ID")
    action.add_argument("--reconnect", action="store_true")
    args = parser.parse_args()
    if args.resume_notifications:
        print(json.dumps(resume_notifications()))
    elif args.relay_discard_event:
        print(json.dumps(discard_relay_event(args.relay_discard_event)))
    elif args.relay_status:
        print(json.dumps(relay_status()))
    elif args.relay_on or args.relay_off or args.reconnect:
        print(json.dumps(set_relay_enabled(not args.relay_off, reconnect=args.reconnect)))
    elif args.panel or not (args.on or args.off or args.status):
        panel()
    else:
        print(json.dumps(status() if args.status else set_enabled(args.on)))
