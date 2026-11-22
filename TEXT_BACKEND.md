# Text reply backend

`text_backend.py` reads the existing Codex model, HTTPS Responses endpoint, and
API-key configuration in memory. It does not copy credentials into this project.
Each request reads the approved local `persona.md`, or the file explicitly
selected with `WECHAT_PERSONA_PATH`. No private memory vault is selected by default.
The caller may pass up to twelve recent user/assistant turns. The module
does not import old conversations or persist chat history.

The provider receives this profile and the supplied conversational text. This
uses the existing configured provider; it is an independent text-model call,
not a continuation of the current Codex task. `store=false` is requested, but
this flag does not establish a third-party provider's retention policy.

There are no tools in the request. This module does not inspect windows, consume
the OCR queue, execute received commands, send WeChat messages, or run on a timer.
It performs one model request per direct invocation, without automatic retries.
HTTP redirects, incomplete replies, and unsupported tool outputs fail closed.

Run `python text_backend.py --self-test` for a synthetic text-only test.
Run `python -m unittest discover -v` for local tests.

The desktop event bridge remains disabled. Its rejected background Computer Use
access must not be replaced with another screenshot/input path. A supported,
authorized message transport and an actual new-message/reply exchange are still
required before enabling or claiming unattended WeChat replies. An incoming
message from another contact must not be given this owner's personal profile.
