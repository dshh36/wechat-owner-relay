"""Read-only, local OCR of visible WeChat conversation previews.

No input injection, outgoing messages, network calls, or model credentials.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import ctypes
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from project_settings import owner_contact

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "state.json"
QUEUE = ROOT / "pending.json"
STATUS = ROOT / "status.json"
STOP = ROOT / "STOP"
SYSTEM_CHATS = {"微信团队", "腾讯新闻", "文件传输助手", "公众号", "服务通知", "WeChat Team"}
WINDOW_UNAVAILABLE = "Pinned WeChat window is missing or minimized"
WINDOW_RECOVERY_AMBIGUOUS = "Multiple matching WeChat windows; waiting for a unique window"


def now():
    return datetime.now(timezone.utc).isoformat()


def save(path, value):
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
        for attempt in range(8):
            try:
                os.replace(name, path)
                return
            except PermissionError:
                if attempt == 7:
                    raise
                time.sleep(min(0.1 * (2 ** attempt), 1.0))
    finally:
        if os.path.exists(name):
            os.unlink(name)


def load(path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def window_identity(hwnd):
    import win32api
    import win32gui
    import win32process
    _, pid = win32process.GetWindowThreadProcessId(hwnd)
    process = win32api.OpenProcess(0x1000, False, pid)
    try:
        exe = win32process.GetModuleFileNameEx(process, 0)
    finally:
        win32api.CloseHandle(process)
    return pid, {
        "exe": os.path.normcase(exe),
        "class": win32gui.GetClassName(hwnd),
        "title": win32gui.GetWindowText(hwnd),
    }


def recover_window(state):
    import pywintypes
    import win32gui

    expected = state.get("window_identity")
    if not expected:
        raise RuntimeError(WINDOW_UNAVAILABLE)
    matches = []

    def inspect(hwnd, _):
        try:
            if (win32gui.IsWindowVisible(hwnd) and not win32gui.IsIconic(hwnd)
                    and win32gui.GetClassName(hwnd) == expected["class"]
                    and win32gui.GetWindowText(hwnd) == expected["title"]
                    and window_identity(hwnd)[1] == expected):
                matches.append(hwnd)
        except pywintypes.error:
            # A window can disappear between enumeration and inspection.
            pass
        return True

    win32gui.EnumWindows(inspect, None)
    if not matches:
        raise RuntimeError(WINDOW_UNAVAILABLE)
    if len(matches) > 1:
        raise RuntimeError(WINDOW_RECOVERY_AMBIGUOUS)
    return matches[0]


def capture(hwnd):
    """Capture only the selected WeChat window; never capture the whole desktop."""
    import win32gui
    import win32ui
    from PIL import Image, ImageStat

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except OSError:
        pass
    if not win32gui.IsWindow(hwnd) or win32gui.IsIconic(hwnd):
        raise RuntimeError(WINDOW_UNAVAILABLE)
    pid, identity = window_identity(hwnd)
    exe = identity["exe"]
    if Path(exe).name.lower() not in {"weixin.exe", "wechat.exe"}:
        raise RuntimeError("Pinned window no longer belongs to WeChat")
    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    w, h = right - left, bottom - top
    if w < 650 or h < 450:
        raise RuntimeError("WeChat window is too small for this calibration")
    dc = win32gui.GetWindowDC(hwnd)
    src = win32ui.CreateDCFromHandle(dc)
    dst = src.CreateCompatibleDC()
    bitmap = win32ui.CreateBitmap()
    bitmap.CreateCompatibleBitmap(src, w, h)
    old = dst.SelectObject(bitmap)
    try:
        ok = ctypes.windll.user32.PrintWindow(hwnd, dst.GetSafeHdc(), 2)
        if not ok:
            raise RuntimeError("Window-only capture failed")
        image = Image.frombuffer(
            "RGB", (w, h), bitmap.GetBitmapBits(True), "raw", "BGRX", 0, 1
        ).copy()
    finally:
        dst.SelectObject(old)
        win32gui.DeleteObject(bitmap.GetHandle())
        dst.DeleteDC()
        src.DeleteDC()
        win32gui.ReleaseDC(hwnd, dc)
    if max(ImageStat.Stat(image).stddev) < 5:
        raise RuntimeError("Window capture is blank")
    return image, pid


def scan_window(hwnd, state, auto_recover):
    import win32gui
    import win32process

    if auto_recover and (not win32gui.IsWindow(hwnd)
                         or win32process.GetWindowThreadProcessId(hwnd)[1] != state["pid"]):
        hwnd = recover_window(state)
    image, pid = capture(hwnd)
    current_pid, identity = window_identity(hwnd)
    if current_pid != pid:
        raise RuntimeError(WINDOW_UNAVAILABLE)
    return hwnd, image, pid, identity


def avatar_signature(image, scale):
    # Pin the current user-approved session; a changed avatar pauses monitoring.
    box = tuple(round(v * scale) for v in (14, 42, 48, 76))
    thumb = image.crop(box).resize((8, 8)).convert("RGB")
    return list(thumb.tobytes())


def avatar_matches(expected, actual):
    if len(expected) != len(actual) or not expected:
        return False
    return sum(abs(a - b) for a, b in zip(expected, actual)) / len(expected) < 12


def read_ocr(image):
    import numpy as np
    from rapidocr_onnxruntime import RapidOCR

    engine = RapidOCR(
        intra_op_num_threads=2, inter_op_num_threads=1,
    )
    result, _ = engine(np.asarray(image))
    lines = []
    for box, text, score in result or []:
        xs, ys = zip(*box)
        lines.append({
            "text": text, "score": float(score),
            "x": (min(xs) + max(xs)) / 2,
            "y": (min(ys) + max(ys)) / 2,
        })
    return lines


def parse_previews(lines, width, height, scale):
    """Fixed baseline geometry; resizing requires explicit recalibration."""
    rows = defaultdict(list)
    for line in lines:
        x, y = line["x"] / scale, line["y"] / scale
        if 112 <= x < 294 and 80 <= y < height / scale - 20:
            row = int((y - 80) // 66)
            rows[row].append((y - (80 + row * 66), line))
    previews = []
    for row, parts in sorted(rows.items()):
        names = [p for dy, p in parts if 5 <= dy <= 29 and p["x"] / scale < 230]
        texts = [p for dy, p in parts if 30 <= dy <= 57]
        stamps = [
            p["text"] for dy, p in parts
            if 5 <= dy <= 29 and p["x"] / scale >= 230
        ]
        if len(names) != 1 or names[0]["score"] < 0.85 or not texts:
            continue
        if names[0]["text"] in SYSTEM_CHATS:
            continue
        text = " ".join(p["text"] for p in sorted(texts, key=lambda p: p["x"]))
        if min(p["score"] for p in texts) < 0.75:
            continue
        previews.append({
            "contact_candidate": names[0]["text"],
            "preview": text,
            "displayed_time": " ".join(stamps),
            "row": row,
            "source": "sidebar_preview",
            "needs_visual_verification": True,
            "direction": "unknown",
        })
    return previews


def preview_key(item):
    return digest(json.dumps(
        [item["contact_candidate"], item["preview"], item["displayed_time"]],
        ensure_ascii=False,
    ))


def fresh_candidates(previews, state):
    # Previously visible history and identical repeats are never queued again.
    seen = set(state["seen"])
    previous = state.setdefault("last_previews", {})
    fresh = []
    for item in previews:
        contact = item["contact_candidate"]
        old = previous.get(contact)
        stamp = re.sub(r"\s+", "", item["displayed_time"])
        old_stamp = re.sub(r"\s+", "", old["displayed_time"]) if old else ""
        # Midnight relabels the same sidebar entry without receiving a message.
        aged = (
            old is not None
            and old["preview"] == item["preview"]
            and re.fullmatch(r"\d{1,2}:\d{2}", old_stamp) is not None
            and stamp == "昨天" + old_stamp
        )
        # OCR sometimes varies only the number of dots in a clipped preview.
        ellipsis_jitter = (
            old is not None and stamp == old_stamp
            and old["preview"] != item["preview"]
            and re.search(r"(?:\.{2,}|…+)$", old["preview"]) is not None
            and re.search(r"(?:\.{2,}|…+)$", item["preview"]) is not None
            and re.sub(r"(?:\.{2,}|…+)$", "", old["preview"])
            == re.sub(r"(?:\.{2,}|…+)$", "", item["preview"])
        )
        key = preview_key(item)
        if key not in seen:
            if not aged and not ellipsis_jitter:
                fresh.append(item)
            state["seen"].append(key)
            seen.add(key)
        previous[contact] = {
            "preview": item["preview"], "displayed_time": item["displayed_time"],
        }
    state["seen"] = state["seen"][-2000:]
    return fresh


def rebase_previews(previews, state):
    """Re-establish the visible sidebar baseline after a window rebound."""
    state["last_previews"] = {
        item["contact_candidate"]: {
            "preview": item["preview"],
            "displayed_time": item["displayed_time"],
        }
        for item in previews
    }
    state["seen"] = list(dict.fromkeys(
        state.get("seen", []) + [preview_key(item) for item in previews]
    ))[-2000:]


def report(status, **fields):
    value = {"status": status, "checked_at": now(), **fields}
    save(STATUS, value)
    print(json.dumps(value, ensure_ascii=False))


def selected_events(ids):
    wanted = set(ids)
    events = [event for event in load(QUEUE, []) if event["id"] in wanted]
    if {event["id"] for event in events} != wanted:
        raise ValueError("Unknown candidate ID")
    return events


def reply_transition(event_id, sent=False):
    config = load(ROOT / "wechat_notifier_config.json", {})
    queue = load(QUEUE, [])
    matches = [event for event in queue if event["id"] == event_id]
    if len(matches) != 1:
        raise ValueError("Expected one known candidate")
    event = matches[0]
    if sent:
        if event.get("state") != "reply_started":
            raise ValueError("No registered reply to complete")
        event.update(state="replied", replied_at=now())
    else:
        since = config.get("carry_replies_since")
        if (not config.get("enabled") or config.get("carry_replies") is not True
                or STOP.exists() or not since
                or event.get("detected_at", "") <= since
                or event.get("contact_candidate") != owner_contact(config)
                or event.get("state") != "needs_verification"):
            raise ValueError("Candidate is outside the enabled carry reply scope")
        if any(item.get("state") == "reply_started" for item in queue):
            raise ValueError("An earlier reply has an uncertain result")
        event.update(state="reply_started", reply_started_at=now())
    save(QUEUE, queue)
    return {"state": event["state"]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--initialize", action="store_true")
    parser.add_argument("--recalibrate", action="store_true")
    parser.add_argument("--rebind", action="store_true")
    parser.add_argument("--hwnd", type=int)
    parser.add_argument("--scale", type=float, default=1.0)
    parser.add_argument("--scan", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--stop", action="store_true")
    parser.add_argument("--snapshot", action="store_true")
    parser.add_argument("--events", nargs="+", metavar="EVENT_ID")
    parser.add_argument("--mark-presented", nargs="+", metavar="EVENT_ID")
    parser.add_argument("--dismiss", nargs="+", metavar="EVENT_ID")
    reply = parser.add_mutually_exclusive_group()
    reply.add_argument("--begin-reply", metavar="EVENT_ID")
    reply.add_argument("--reply-sent", metavar="EVENT_ID")
    args = parser.parse_args()
    if args.begin_reply or args.reply_sent:
        print(json.dumps(reply_transition(
            args.begin_reply or args.reply_sent, sent=bool(args.reply_sent))))
        return
    if args.events:
        print(json.dumps(selected_events(args.events), ensure_ascii=False))
        return
    if args.mark_presented or args.dismiss:
        queue = load(QUEUE, [])
        wanted = set(args.mark_presented or args.dismiss)
        for event in queue:
            if event["id"] in wanted:
                if event.get("state") in {"reply_started", "replied"}:
                    raise ValueError("Reply records cannot be dismissed or presented")
                event["state"] = "presented" if args.mark_presented else "dismissed"
        save(QUEUE, queue)
        print(json.dumps({"updated": len(wanted)}))
        return
    if args.stop:
        STOP.touch()
        report("stopped")
        return
    if args.status:
        print(json.dumps(load(STATUS, {"status": "not_initialized"}), ensure_ascii=False))
        return
    if not (args.initialize or args.recalibrate or args.scan or args.rebind):
        parser.error("Choose --initialize, --recalibrate, --rebind, --scan, --status, or --stop")
    if args.rebind and (not STOP.exists() or not args.hwnd or args.initialize
                        or args.recalibrate or args.scan or args.snapshot):
        parser.error("--rebind requires a stopped monitor and an explicitly verified --hwnd")
    if STOP.exists() and not args.rebind:
        report("stopped", reason="Local STOP file is present")
        return
    state = load(STATE, {})
    if args.rebind and not state.get("avatar"):
        raise RuntimeError("No approved baseline to rebind")
    hwnd = args.hwnd if args.initialize or args.rebind else state.get("hwnd")
    if not hwnd:
        raise RuntimeError("Initialize with the user-approved window handle first")
    auto_recover = args.scan and not (args.initialize or args.recalibrate or args.rebind)
    hwnd, image, pid, identity = scan_window(hwnd, state, auto_recover)
    if args.snapshot:
        image.save(ROOT / "diagnostic.png")
    scale = args.scale if args.initialize else state["scale"]
    signature = avatar_signature(image, scale)
    if not args.initialize:
        if (not args.rebind and (pid != state["pid"] or hwnd != state["hwnd"])
                and (not auto_recover or identity != state.get("window_identity"))):
            report("paused", reason="Window session changed; identity verification required")
            return
        if list(image.size) != state["size"] and not (args.recalibrate or args.rebind):
            report("paused", reason="Window session or size changed; recalibration required")
            return
        if not avatar_matches(state["avatar"], signature):
            report("paused", reason="Account avatar changed; identity verification required")
            return
    lines = read_ocr(image)
    previews = parse_previews(lines, *image.size, scale)
    if not previews:
        report("paused", reason="No reliable sidebar previews; OCR/calibration not verified")
        return
    if args.rebind:
        state.update(hwnd=hwnd, pid=pid, size=list(image.size), window_identity=identity)
        save(STATE, state)
        report("baseline_rebound", visible_rows=len(previews))
        return
    if args.recalibrate:
        state.update(size=list(image.size), window_identity=identity)
        save(STATE, state)
    if args.initialize:
        state = {
            "hwnd": hwnd, "pid": pid, "size": list(image.size), "scale": scale,
            "avatar": signature, "initialized_at": now(), "window_identity": identity,
            "seen": list(dict.fromkeys(preview_key(item) for item in previews)),
        }
        save(STATE, state)
        if not QUEUE.exists():
            save(QUEUE, [])
        report("baseline_ready", visible_rows=len(previews), previews=previews)
        return
    rebound = (
        hwnd != state["hwnd"]
        or pid != state["pid"]
        or state.pop("rebase_on_next_scan", False)
    )
    state.update(hwnd=hwnd, pid=pid)
    state.setdefault("window_identity", identity)
    if rebound:
        rebase_previews(previews, state)
        queue = load(QUEUE, [])
        save(QUEUE, queue)
        save(STATE, state)
        report(
            "scan_ok", new_candidates=0, window_rebound=True,
            pending=sum(e["state"] == "needs_verification" for e in queue),
        )
        return
    fresh = fresh_candidates(previews, state)
    queue = load(QUEUE, [])
    ids = {event["id"] for event in queue}
    for item in fresh:
        key = preview_key(item)
        if key not in ids:
            queue.append({
                "id": key, "detected_at": now(), "state": "needs_verification", **item,
            })
            ids.add(key)
    save(QUEUE, queue)
    save(STATE, state)
    if fresh:
        image.save(ROOT / "diagnostic.png")
    report(
        "scan_ok", new_candidates=len(fresh), window_rebound=rebound,
        pending=sum(e["state"] == "needs_verification" for e in queue),
    )


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        main()
    except Exception as error:
        if str(error) == WINDOW_UNAVAILABLE:
            state = load(STATE, {})
            state["rebase_on_next_scan"] = True
            save(STATE, state)
        report("paused", reason=f"{type(error).__name__}: {error}")
        raise SystemExit(1)
