# ADR-0273: Diagnose a widget from owner chat

- Status: Proposed design for the owner-requested implementation, 2026-10-31.
- Scope: Cockpit under ADR-0240 and ADR-0270.

When a widget looks wrong, the assistant needs to compare the data it read with
the output of the component. A successful build establishes that a synthetic
fixture rendered within bounds. It does not establish that the right values
appeared or that a particular device loaded the bundle.

1. **Reproduce the display privately.** The existing check tool builds and
   screens the submitted package in the offline widget sandbox. On preview it
   renders one chosen synthetic fixture at one declared size through the real
   host and realm in WebKit. The response names the fixture and size, includes
   bounded visible text, and references a private PNG artifact of the render.
   Each call can select a different case. Rejections carry the ordinary build
   diagnostics. Preview output is untrusted. Publication and approval stay on
   the existing paths.
2. **Read the evidence for a widget.** The Cockpit reader offers an on-demand
   diagnostic operation by slug and optional revision. It returns the owned
   revision's build findings, recent rejected imports, queued publication
   attempts and next retry, and device failure observations. Last-error
   categories exclude upstream response text. Feed data and timestamps remain
   available through the existing feed reader.
3. **Report device failures.** Native code sends a closed failure code and
   revision through an authenticated Cockpit write route. It checks the
   displayed revision before sending. The service verifies that the revision
   is live for that owner's widget and records the first observation of each
   kind per revision. Retries converge on the same process event. Quarantine
   identifies the watchdog's suspect, not a proven culprit. Reports carry no
   screen text, exception strings, stack traces, settings or feed values.

Acceptance requires a private preview that shows the fixture's expected
content, diagnostic reads for rejected and queued changes, and a client
failure observable through the reader. Foreign or stale reports and arbitrary
payloads must be refused. A repeat report must not create another event or
cause the board to refresh. Production verification requires a released
builder, server and Apple client.
