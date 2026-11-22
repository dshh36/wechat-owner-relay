"""Thread-bound relay transport using the desktop's packaged MCP server."""
from __future__ import annotations

import atexit
import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import time
from uuid import UUID

from watcher import ROOT, load

_client = None


def approved_thread(thread_id):
    UUID(thread_id)
    if thread_id != load(ROOT / "clawbot_relay_config.json", {}).get("thread_id"):
        raise RuntimeError("Relay destination does not match the approved thread")


def runtime_paths():
    root = Path.home() / ".codex/plugins/cache/openai-bundled/codex-app-tools"
    servers = sorted(root.glob("*/server.mjs"), key=lambda p: p.parent.name, reverse=True)
    nodes = [
        Path(os.environ[name]) for name in (
            "CODEX_MCP_NODE_PATH", "CODEX_BROWSER_USE_NODE_PATH"
        ) if os.environ.get(name)
    ]
    nodes += list((Path.home() / "AppData/Local/OpenAI/Codex/runtimes/cua_node").glob(
        "*/bin/node.exe"))
    node = next((path for path in nodes if path.is_file()), None)
    pipes = [
        name for name in os.listdir("\\\\.\\pipe\\")
        if name.startswith("codex-app-tools-")
    ]
    if not servers or node is None or len(pipes) != 1:
        raise RuntimeError("A unique desktop app-tools connection is unavailable")
    return node, servers[0], "\\\\.\\pipe\\" + pipes[0]


class AppToolsClient:
    def __init__(self):
        node, server, pipe = runtime_paths()
        self.pipe = pipe
        self.next_id = 0
        self.lock = threading.Lock()
        self.responses = queue.Queue()
        self.process = subprocess.Popen(
            [str(node), str(server)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
            env={**os.environ, "CODEX_APP_TOOLS_PIPE_PATH": pipe,
                 "CODEX_APP_TOOLS_DEFERRED": "0"},
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        threading.Thread(target=self._read, daemon=True).start()
        try:
            self.rpc("initialize", {
                "protocolVersion": "2024-11-05",
                "clientInfo": {"name": "owner-phone-relay", "version": "1.0"},
                "capabilities": {},
            })
            self._write({"jsonrpc": "2.0", "method": "notifications/initialized"})
            catalog = self.rpc("tools/list", {})
            names = {tool.get("name") for tool in catalog.get("tools", [])}
            if not {"read_thread", "send_message_to_thread"} <= names:
                raise RuntimeError("Required desktop tools are unavailable")
        except Exception:
            self.close()
            raise

    def _write(self, message):
        self.process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        self.process.stdin.flush()

    def _read(self):
        try:
            for line in self.process.stdout:
                self.responses.put(json.loads(line))
        except (ValueError, OSError):
            pass
        finally:
            self.responses.put(None)

    def rpc(self, method, params):
        with self.lock:
            self.next_id += 1
            request_id = self.next_id
            self._write({"jsonrpc": "2.0", "id": request_id,
                         "method": method, "params": params})
            deadline = time.monotonic() + 20
            while True:
                try:
                    response = self.responses.get(timeout=max(
                        0.01, deadline - time.monotonic()))
                except queue.Empty:
                    raise RuntimeError("Desktop tool timed out; submission is not retried") from None
                if response is None:
                    raise RuntimeError("Desktop tools disconnected")
                if response.get("id") != request_id:
                    if time.monotonic() >= deadline:
                        raise RuntimeError("Desktop tool timed out")
                    continue
                if "error" in response:
                    raise RuntimeError("Desktop tool rejected the request")
                return response["result"]

    def tool(self, thread_id, name, arguments):
        approved_thread(thread_id)
        # Identify the real bound conversation; do not fabricate an executor turn.
        result = self.rpc("tools/call", {
            "name": name, "arguments": arguments,
            "_meta": {"threadId": thread_id},
        })
        if result.get("isError"):
            raise RuntimeError("Desktop tool could not complete the request")
        texts = [item["text"] for item in result.get("content", [])
                 if item.get("type") == "text"]
        if len(texts) != 1:
            raise RuntimeError("Unexpected desktop tool response")
        return json.loads(texts[0])

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        self.process.stdin.close()
        self.process.stdout.close()


def normalize_view(result, thread_id):
    thread = result.get("thread", {})
    if thread.get("id") != thread_id or thread.get("hostId") != "local":
        raise RuntimeError("Unexpected desktop thread or host")
    turns = []
    for turn in result.get("turns", []):
        items = []
        for item in turn.get("items", []):
            kind = item.get("type")
            if kind in ("userMessage", "steeringUserMessage"):
                content = item.get("content", [])
                text = "\n".join(part.get("text", "") for part in content
                                 if part.get("type") == "text")
                if item.get("textTruncated") or any(
                    part.get("textTruncated") for part in content
                ):
                    raise RuntimeError("Relay correlation text was truncated")
                items.append({"type": kind, "text": text})
            elif kind == "agentMessage":
                if item.get("textTruncated"):
                    raise RuntimeError("Desktop answer was truncated")
                items.append({key: item.get(key) for key in ("type", "phase", "text")})
            elif (kind == "functionCallOutput"
                  and item.get("name") == "send_message_to_thread"
                  and item.get("namespace") == "codex_app"):
                output = item.get("output", {})
                text = output.get("text", "")
                prefix = (
                    "<codex_delegation>\n  <source_thread_id>" + thread_id
                    + "</source_thread_id>\n  <input>"
                )
                suffix = "</input>\n</codex_delegation>"
                if output.get("truncated"):
                    raise RuntimeError("Desktop relay input was truncated")
                if text.startswith(prefix) and text.endswith(suffix):
                    payload = text[len(prefix):-len(suffix)]
                    if payload.startswith("[ClawBot relay "):
                        items.append({"type": "userMessage", "text": payload})
        turns.append({"turnId": turn.get("id"), "status": turn.get("status"),
                      "error": turn.get("error"), "items": items})
    status = thread.get("status", {})
    flags = status.get("activeFlags", [])
    busy = bool(flags) or any(turn["status"] not in (
        "completed", "interrupted", "failed"
    ) for turn in turns)
    # An empty or unrecognized snapshot cannot establish an idle destination.
    live = status.get("type") in ("active", "idle") and bool(turns)
    return {"threadId": thread_id, "live": live,
            "runtime": "active" if busy else "idle", "requests": flags,
            "turns": turns, "transport": "desktop_app_tools"}


def close():
    global _client
    if _client is not None:
        _client.close()
        _client = None


def request(thread_id, action=None, text=None):
    global _client
    approved_thread(thread_id)
    if action not in (None, "messages"):
        raise ValueError("Unsupported relay desktop operation")
    if _client is not None and _client.process.poll() is not None:
        close()
    if _client is None:
        _client = AppToolsClient()
    if action is None:
        return normalize_view(_client.tool(thread_id, "read_thread", {
            "threadId": thread_id, "hostId": "local", "turnLimit": 10,
            "includeOutputs": True, "maxOutputCharsPerItem": 20000,
        }), thread_id)
    if not isinstance(text, str) or not text.strip() or len(text) > 20000:
        raise ValueError("Invalid relay prompt")
    result = _client.tool(thread_id, "send_message_to_thread", {
        "threadId": thread_id, "hostId": "local", "prompt": text,
    })
    if result.get("threadId") != thread_id:
        raise RuntimeError("Desktop did not acknowledge the bound destination")
    return {"kind": "start", "background": False, "transport": "desktop_app_tools"}


atexit.register(close)
