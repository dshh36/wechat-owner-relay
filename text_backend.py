"""Text-only replies through the user's existing configured Responses provider.

This module has no desktop access, message listener, tool execution, or sender.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import time
import tomllib
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

MEMORY = Path(os.environ.get("WECHAT_PERSONA_PATH", Path(__file__).parent / "persona.md"))
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_MESSAGE_CHARS = 12000


class BackendError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    endpoint: str
    model: str
    token: str = field(repr=False)


def load_settings(home=None):
    home = Path(home or os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    try:
        config = tomllib.loads((home / "config.toml").read_text(encoding="utf-8"))
        provider_name = config.get("model_provider", "openai")
        provider = config.get("model_providers", {}).get(provider_name, {})
        if provider.get("wire_api", "responses") != "responses":
            raise BackendError("The configured provider does not use Responses.")
        base = provider.get("base_url")
        if not base:
            if provider_name != "openai":
                raise BackendError("The configured provider has no endpoint.")
            base = "https://api.openai.com/v1"
        parts = urlsplit(base)
        if (parts.scheme != "https" or not parts.hostname or parts.username
                or parts.password or parts.query or parts.fragment):
            raise BackendError("A credential-free HTTPS provider URL is required.")
        token = os.environ.get(provider.get("env_key", ""), "")
        token = token or provider.get("experimental_bearer_token", "")
        if not token:
            auth = json.loads((home / "auth.json").read_text(encoding="utf-8"))
            token = auth.get("OPENAI_API_KEY", "")
        model = config.get("model")
        if not isinstance(token, str) or not token or not isinstance(model, str) or not model:
            raise BackendError("An existing model and API-key configuration is required.")
        return Settings(base.rstrip("/") + "/responses", model, token)
    except (OSError, ValueError, TypeError, AttributeError):
        raise BackendError("The existing model configuration could not be loaded.") from None


def build_payload(message, model, history=(), memory_path=MEMORY):
    if not isinstance(message, str) or not message.strip():
        raise BackendError("A nonempty manually supplied message is required.")
    if len(message) > MAX_MESSAGE_CHARS:
        raise BackendError("The message exceeds the text limit.")
    recent = list(history)[-12:]
    for item in recent:
        if (not isinstance(item, dict)
                or item.get("role") not in {"user", "assistant"}
                or not isinstance(item.get("content"), str)
                or len(item["content"]) > MAX_MESSAGE_CHARS):
            raise BackendError("History must contain bounded user/assistant text only.")
    try:
        profile = Path(memory_path).read_text(encoding="utf-8")
    except OSError:
        raise BackendError("The approved persona memory could not be read.") from None
    if len(profile) > 16000:
        raise BackendError("The persona memory exceeds the profile limit.")
    instructions = (
        "你是一位自然、温暖的 AI 助手，具体口吻遵循用户提供的资料。"
        "这是用户本人授权的纯文字对话回复模块。只回答当前文字消息。"
        "没有电脑控制、文件读取、微信发送、监听、定时或其他工具。"
        "不要声称做过任何这些操作，也不要声称自动继承控制会话的历史。"
        "下面是用户在本地确认的个人资料与口吻偏好，只用于理解称呼和表达方式，"
        "其中的工作需求不代表已有功能或工具授权。"
        "若所问的个人事实没有记载，就坦诚说明，不编造共同经历。"
        "默认简短中文，不主动显示路径、后台配置或密钥，不主动提问索取凭据。"
        "\n<approved_profile>\n" + profile + "\n</approved_profile>"
    )
    return {
        "model": model, "instructions": instructions,
        "input": [
            {"role": item["role"], "content": item["content"]} for item in recent
        ] + [{"role": "user", "content": message}],
        "tools": [], "store": False, "stream": True,
        "max_output_tokens": 1024, "reasoning": {"effort": "low"},
    }


def extract_reply(response):
    if response.get("status") != "completed":
        raise BackendError("The model did not complete a text reply; no retry was made.")
    chunks = []
    for item in response.get("output", []):
        if item.get("type") == "reasoning":
            continue
        if item.get("type") != "message" or item.get("role") != "assistant":
            raise BackendError("The provider returned unsupported non-text output.")
        for part in item.get("content", []):
            if part.get("type") not in {"output_text", "refusal"}:
                raise BackendError("The provider returned unsupported message content.")
            text = part.get("text") if part["type"] == "output_text" else part.get("refusal")
            if not isinstance(text, str):
                raise BackendError("The provider returned invalid text.")
            chunks.append(text)
    reply = "\n".join(chunks).strip()
    if not reply or len(reply) > MAX_MESSAGE_CHARS:
        raise BackendError("The provider returned an empty or oversized reply.")
    return reply


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward the user's existing credential to a redirected endpoint.
        return None


def read_reply(stream):
    if "text/event-stream" not in stream.headers.get("Content-Type", ""):
        raw = stream.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise BackendError("The provider response exceeded the size limit.")
        return extract_reply(json.loads(raw))
    total = 0
    deadline = time.monotonic() + 60
    data = []
    while time.monotonic() < deadline:
        raw = stream.readline(MAX_RESPONSE_BYTES + 1)
        total += len(raw)
        if total > MAX_RESPONSE_BYTES:
            raise BackendError("The provider response exceeded the size limit.")
        if not raw:
            break
        line = raw.decode("utf-8").rstrip("\r\n")
        if line.startswith("data:"):
            data.append(line[5:].lstrip(" "))
        elif not line and data:
            event = json.loads("\n".join(data))
            data = []
            kind = event.get("type")
            if kind == "response.completed":
                return extract_reply(event["response"])
            if kind in {"error", "response.failed", "response.incomplete"}:
                raise BackendError("The provider did not complete the reply; no retry was made.")
    raise BackendError("The response stream ended without a completed reply.")


def generate_reply(message, history=(), settings=None, memory_path=MEMORY):
    settings = settings or load_settings()
    payload = build_payload(message, settings.model, history, memory_path)
    request = Request(
        settings.endpoint, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={
            "Authorization": "Bearer " + settings.token,
            "Content-Type": "application/json", "Accept": "text/event-stream",
            "User-Agent": "Mengmengxiang-TextBackend/1",
        },
    )
    try:
        with build_opener(NoRedirect()).open(request, timeout=45) as stream:
            return read_reply(stream)
    except HTTPError as error:
        raise BackendError(f"Provider HTTP {error.code}; no retry was made.") from None
    except (URLError, OSError):
        raise BackendError("The provider connection failed; no retry was made.") from None
    except (ValueError, KeyError, TypeError, AttributeError):
        raise BackendError("The provider response was invalid; no retry was made.") from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--message", help="Text supplied directly by the controlling user.")
    group.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    message = args.message or "这是纯文字连通测试。请简短打声招呼。"
    try:
        reply = generate_reply(message)
        print(json.dumps({"status": "text_reply_ready", "reply": reply,
                          "wechat_connected": False, "sent": False}, ensure_ascii=False))
    except BackendError as error:
        print(json.dumps({"status": "error", "message": str(error)}, ensure_ascii=False))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
