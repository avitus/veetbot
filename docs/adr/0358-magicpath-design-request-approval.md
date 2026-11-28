# ADR-0358: One approval for a MagicPath design request

- Status: Proposed
- Date: 2026-11-27
- Amends ADR-0255 and the tool-name request approval in ADR-0342.

The owner's design request raised nine cards. The signed-in MagicPath server
classifies all its tools as external writes. Two project lookups, two canvas
reads, four shape creations and one sandbox script each needed a decision.

Offer **Allow for this request** on those four MagicPath methods in a top-level
owner chat. The offer describes project discovery, canvas reads and shape
creation on one project, and sandbox scripts for the request. The client
echoes the displayed method. Approve once keeps its existing meaning.

Use the tool-name offer and echo already supported by the Apple app. Record a
separate MagicPath grant event when resolving the card. Each covered use cites
the original approval. Check the exact remote method on the pinned catalog,
the official endpoint, and the same owner's current sign-in. Match the policy
and agent versions, and use the existing request lifetime of two hours or the
next consumed owner message, whichever comes first. A new run needs its own
approval.

A card showing a canvas operation binds its project immediately. A project
lookup can precede the choice of canvas. In that case the first covered canvas
operation records its project under a unique event derivation key for this
approval. Concurrent attempts and retries receive the original binding.
Other projects and methods retain their ordinary approval decisions. A
script start qualifies only for the existing outside-content escalation, and
each call over its bridge goes through the same policy and coverage checks.

The in-process runner assigns each attempt a sandbox epoch when it starts or
resumes. Cleanup retires that epoch. A sandbox script after an approval
therefore starts with a fresh workspace, as it does under a durable worker.
