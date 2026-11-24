# ClawBot Relay

This relay uses the user's existing official ClawBot login and the desktop's
packaged codex-app-tools MCP server. It does not access the ordinary WeChat PC account,
start another Codex CLI process, reset the conversation, or change permissions.

## Scope

- Bind only the one existing ClawBot peer whose native thread mapping exactly
  matches `clawbot_relay_config.json`.
- Accept newly received plain text after first activation. Attachments, slash
  commands, other peers and historical messages are ignored.
- Keep the target conversation open in desktop Codex and keep Codex++ running.
- The desktop upgrade from 26.1111.2228.0 to 26.1117.3838.0 removed the old
  Follower connection used by Codex++ 2.8.0. Historical desktop logs show the
  old IPC router running before restart; the current desktop has no such
  endpoint. The relay now uses the desktop's own `read_thread` and
  `send_message_to_thread` tools through its packaged MCP server, identifying
  the actual bound conversation without inventing an executor turn.
- Only one active desktop app-tools endpoint is accepted. A missing or
  ambiguous endpoint stops dispatch. No app package, tool approval settings,
  permissions, model settings, account binding or receive cursor is rewritten.
- Read at most ten recent turns. Unknown or empty status, pending input,
  truncated text and mismatched destinations cannot establish safe delivery.
  An uncertain submission is never retried or redirected to background execution.
- The desktop records `send_message_to_thread` input as a `codex_app`
  function-call output with a delegation envelope, rather than a user-message
  item. Only an exact same-thread envelope from that tool is eligible for
  correlation; unrelated command/tool outputs are discarded.
- A verified `awaiting_reply` event with exactly one matching running/completed
  turn and no send attempt may resume when the original inbound update is
  replayed from the preserved cursor. It reuses the original marker and reply
  context without submitting the prompt again. Uncertain submissions remain
  blocked; uncertain sends are isolated as described below.
- Wait for the desktop conversation to become idle; never intentionally steer
  an active turn.
- Find the reply by the unique marker in the submitted user message and a new
  turn ID. Return only that turn's completed `final_answer`.
- No model calls while waiting for messages. Tencent's getupdates request is a
  network long poll, not a model request.
- A failed or uncertain model submission still pauses the relay. A reply-send
  transport failure is recorded as `send_uncertain`, retaining its chunk index,
  turn ID, timestamp and sanitized exception type. It is never retried, but the
  relay continues with subsequent inbound messages and advances the receive
  cursor normally. A partially delivered reply can remain incomplete.
- On startup, an old `before_send` record is also isolated as `send_uncertain`
  after verifying the enabled account/peer binding and disabled native receiver.
  This does not claim delivery succeeded or send any old content. Binding
  changes, explicit stops, ledger failures and uncertain model submissions
  still stop the relay.
- A uniquely correlated interrupted turn with no final answer and no send
  attempt is recorded as `interrupted`. The relay can then accept new messages
  without replaying that request or sending its partial output. The normal
  start switch also applies this guarded recovery to older paused records.
- A single `awaiting_reply` event may be marked `discarded` only after the
  owner explicitly chooses to abandon it. This preserves its ledger entry and
  receive cursor. Events at `before_post` remain blocked because submission may
  be uncertain; `before_send` records use the isolation rule above.
- The owner-authorized "Reconnect" action (`monitor_control.py --reconnect`)
  abandons all old `before_post`, `awaiting_reply`, and `before_send` events
  after the previous relay process exits. It records their original stage and
  discard time, preserves the cursor and deduplication ledger, and never
  resubmits or resends them. A partially delivered reply may remain incomplete.
  Active replies, changed bindings, and unknown stages still block this action.
  Ordinary start and automatic network retries retain their existing behavior.
- Start and explicit reconnect check that the exact bound desktop thread is
  live before stopping a writer or abandoning pending replies. A disconnected
  desktop leaves the existing ledger unchanged. Waiting for a disconnected
  desktop is displayed separately from waiting for a busy conversation.
- Credentials and reply context tokens are read from the existing native
  settings/state in memory. No credential or message-body copies are logged.

## Switch

1. Run the read-only check with the existing wx4-skill Python:
   `clawbot_relay.py --check`.
2. Enable this relay's configuration and launch
   `clawbot_relay.py --run --wait-for-switch` in a hidden background process.
   It acquires an exclusive process lock and waits locally.
3. In the Codex++ manager, stop the built-in WeChat connection and wait until its
   interface shows stopped. Do not log out, reset the session or quit Codex++.
   There is no exposed stop action in the authenticated remote-control REST API;
   the native receiver is held in the manager process.
4. After the switch, send a fresh plain-text test from the bound ClawBot peer.
   Only a real matching response returned to ClawBot verifies the full exchange.

`clawbot_relay.py --notify-ready` sends one fixed setup-progress notice to the
already-bound peer using its existing reply context. This does not claim the
relay is working and does not fetch messages.

## Stop And Roll Back

Run `clawbot_relay.py --stop`. Wait for the relay to leave its listening state
and its process to exit. Disable `clawbot_relay_config.json` before restarting
the built-in receiver. Do not run both receivers together.

The relay deliberately keeps its own receive cursor and deduplication ledger;
it does not rewrite the built-in receiver's state. Before returning to the
built-in receiver after real relay exchanges, reconcile its cursor and processed
IDs with this relay's ledger, or it may see older messages again. Do not reset
the thread mapping as a shortcut.

To discard one confirmed `awaiting_reply` event, pass its event key to
`monitor_control.py --relay-discard-event EVENT_ID`, then use the normal
protected `--relay-on` path. The command refuses while the relay is running,
when bindings differ, or when more than one event is unresolved.

## Continuous Conversation Coverage

Sidebar previews are triggers, not a complete inbox. Verification now reads the
same contact's continuous visible segment from the last verified anchor (at most
50 messages), including intermediate text without its own preview candidate.
Candidate batches stay within one contact and run oldest first; the scan interval
is unchanged. Missing context must be reported rather than guessed.

`conversation_context.py --event HASH --contact NAME` reads locally retained
unanswered text for that exact candidate contact and current owner binding.
After Computer Use verifies the actual chat, `--incoming-file FILE` accepts a
JSON array of `{ "anchor": "visible date/time plus stable position", "text": "..." }`.
It does not read WeChat, execute the text, or send messages. Reusing the same
anchor/text deduplicates it; identical text at distinct positions stays separate.
Only explicitly supplied `--answered HASH ...` entries are resolved after a
corresponding reply completes. Presenting a draft or replying to a sticker does
not clear unanswered text. Do not record OCR previews as verified context.

The private `wechat_conversation_context.json` stays in the runtime directory
and is excluded by the repository's root JSON ignore rule. Context is bounded
to 500 entries per contact; reaching the limit requires local review rather
than silently dropping unanswered messages. These instructions improve coverage
but cannot recover unseen messages or guarantee semantic completeness.

## Files

- `clawbot_relay_status.json`: status and sanitized failure reason.
- `clawbot_relay_state.json`: receive cursor, identity fingerprint, message
  hashes, correlation markers, stages and native-format deduplication IDs.
- `CLAWBOT_STOP`: explicit local stop signal.
- `clawbot_relay_notice.json`: at-most-once setup-progress notice record.

No startup registration or scheduled task is installed. Program preparation,
local tests, desktop availability, API acceptance and a real phone exchange
are separate verification stages.
