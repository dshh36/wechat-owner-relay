# Ordinary WeChat arrival reminders

The existing local monitor detects changes in visible sidebar previews. The
notifier submits candidate IDs to the current desktop conversation only after
the bound phone relay and desktop are idle. That conversation verifies the
account, contact and incoming direction using approved Computer Use.

Only the matching completed final answer is sent to the existing owner's
official ClawBot connection. Dismissed candidates produce no phone message.
Unverified candidates produce one failure notice and pause. No ordinary
WeChat message is sent by this module.

## Operation

- `monitor_control.py --panel` opens the combined desktop panel with separate
  detector and ClawBot switches plus a relay reconnect button. Closing the panel
  preserves the selected process states.
- `monitor_control.py --off`, `--on` and `--status` control only the detector.
  Stopping waits for its current local operation to finish; starting preserves
  queue/deduplication history and never clears a paused or uncertain notification.
  These commands never stop or restart the phone relay.
- Temporary resize, minimization, unreadable sidebar, or capture timeout keeps
  the detector enabled in `waiting_for_window`, retrying locally every five
  seconds without submitting candidates while unreadable. Restore the calibrated
  window layout to resume; a new permanent layout still needs explicit
  recalibration. Account/avatar or process identity changes still stop detection.
  The panel shows waiting and fatal pause reasons instead of reporting both as off.
- The combined panel's `恢复提醒` button and
  `monitor_control.py --resume-notifications` recover only a confirmed
  `verification_unavailable` pause or an exactly correlated interrupted turn
  with no final answer and no possible notification send. Interrupted turns
  preserve new arrivals and retry only candidates still awaiting verification;
  the running notifier also recovers this case automatically. Other failures
  and uncertain sends remain paused. Verification-unavailable recoveries preserve queued candidates as a
  baseline and refuse uncertain sends, binding changes, or an unavailable owner
  relay. Turning detection off and on is not a notification recovery.
- `monitor_control.py --relay-on`, `--relay-off`, `--relay-status` and
  `--reconnect` control the existing phone relay process. Busy replies block
  stopping/reconnecting; starting validates the existing binding and refuses
  uncertain delivery records or an active built-in receiver. No cursor or
  delivery ledger is reset. Reconnecting does not remove the existing desktop
  thread availability requirement or rebind a changed PC WeChat account.
- `wechat_notifier.py --activate` validates the existing owner binding and
  baselines all current candidates before enabling future events.
- After a verification-unavailable notice was confirmed delivered, use
  `wechat_notifier.py --resume-after-verification-failure` to resume on future
  candidates. It preserves the failed attempt in recovery history and baselines
  all current queue entries without retrying them. It refuses uncertain
  ClawBot delivery states and any queue with an uncertain outgoing WeChat reply.
- `local_monitor.py --interval 5 --poll-interval 1` scans locally every five
  seconds and checks pending verification results every second. The two clocks
  are independent; OCR runtime is additional. Desktop idle must remain stable
  for two seconds before dispatch.
- Local JSON state replacement retries brief Windows permission locks a
  bounded number of times; persistent write failures still stop monitoring.
- `watcher.py --stop` stops this monitor and notifier. The phone relay has its
  separate stop signal and remains untouched.
- Set `wechat_notifier_config.json` enabled to false to disable notifications.
- Inspect `wechat_notifier_state.json` after a paused or uncertain operation.
  Never clear it and retry blindly: submission or delivery may have occurred.

The state stores correlation IDs, queue IDs and a binding hash, not credentials
or copied chat bodies. Credentials and the existing owner reply context are
loaded in memory from the existing connection.

Empty queues cause no desktop API calls and no model calls. While a verification
turn runs, scanning pauses to avoid racing its queue updates. Phone requests
take priority before dispatch. The desktop API has no atomic idle-only send;
an ownership race is detected and pauses the notifier without a retry.

Each verification reads only its selected queue entries through `watcher.py
--events ID...`. The compact prompt permits reuse of complete skill instructions
already in current context, keeps visual verification mandatory, and limits its
report to the submitted candidates so later arrivals are not reported twice.
Unchanged previews whose whitespace or trailing ellipsis varies at the same
displayed time are deduplicated locally. Explicit historical sidebar dates
(yesterday, weekdays, calendar dates) are baselined even when OCR text changes.
The scan can exclude a visible outgoing text preview when the exact selected
chat title, high-confidence OCR text and the latest green text bubble agree.
A clipped preview may end in a single OCR period as well as an ellipsis; this
requires a matching prefix of at least six characters and the same bubble evidence.
This does not introduce a contact cooldown or suppress different immediate replies.
This uses the existing capture, performs no input, and does not save additional
chat bodies. Other chats, unreadable evidence, different text and new clock
times remain candidates; there is no contact-wide cooldown that could swallow
an immediate reply. New text sharing a clipped prefix also remains a candidate
when its direction is unverified. This is conservative filtering, not reliable
direction detection for offscreen messages, images or custom themes.
Submission, reply observation and API acceptance timestamps support
later latency checks without storing message bodies or credentials.

This is a visible-sidebar detector, not a complete inbox subscription. It can
miss offscreen chats, repeated identical previews or rapid message bursts.
The desktop must remain logged in and the pinned WeChat window available at
its calibrated size. API acceptance alone does not prove phone delivery; a
real incoming-message test is required.
