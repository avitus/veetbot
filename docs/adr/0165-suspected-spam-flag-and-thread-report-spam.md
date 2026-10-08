# ADR-0165: Suspected-spam flag and thread-level Report spam

- Status: Accepted (authorized by the repository owner, 2026-10-08; amends
  ADR-0112, Milestone 31)
- Date: 2026-10-08
- Related: ADR-0095, ADR-0096, ADR-0112, ADR-0116, ADR-0126
- Amends: ADR-0112
- Design: [Email unsubscribe assistance](../plan/email-unsubscribe.md),
  section "Suspected spam"

## Context

On 2026-10-08 the owner asked whether Veetbot could flag spam while it
processes email. Gmail's own filter already holds back most spam, and refresh
excludes the Spam label, so what remains is junk that reached the Inbox
anyway. Nothing in the platform recognized it. The importance assessment
carries a `bulk` verdict, which lowers priority and, since ADR-0116, keeps a
thread from forming memory, but it puts newsletters the owner wants beside
phishing and scams. Milestone 31 offered Report spam only for a sender in the
unsubscribe census, and its design left open whether a thread with no
subscription could be reported at all.

Two ways were offered: flag suspected spam inside Veetbot and let the owner
confirm, or move it to Spam automatically. The owner chose the first and
asked that it extend Milestone 31.

## Decisions

1. **Suspected spam is a flag, never a mailbox action.** The flag removes a
   thread from the priority view, from automatic drafts, from Chat's priority
   context, and from memory formation, and tags it in the other views. It
   never changes Gmail and never creates a consent. Milestone 26's exclusion
   of automatic mailbox actions and ADR-0112's exclusion of standing rules
   stand unchanged.
2. **The verdict is one assessment field.** `EmailAssessment` gains `spam`,
   defined in the prompt as unsolicited mail that is deceptive or unwanted
   from a party with no relationship to the owner. Bulk mail the owner signed
   up for is not spam, and a real person's first contact about a real matter
   never is. The prompt and both assessment revisions change, so retained mail
   is re-assessed in the existing bounded foreground slices at provider cost.
   This revises the prompt that ADR-0116 decision 3 and ADR-0126 left
   unchanged; the reason is a new verdict, not a narrower People clause.
3. **Gmail's sender check informs the verdict and limits protection.** The
   read server projects one closed per-message field, `sender_check`, from the
   DMARC result in Gmail's own topmost `Authentication-Results` header, parsed
   as the census parses DKIM. It is evidence for the model, not a verdict:
   DMARC failures are common among legitimate senders with careless
   configuration, including the cold introductions the owner most wants to
   see. It is mailbox metadata that changes neither the content revision nor
   the source fingerprint.
4. **A known correspondent is never flagged on content alone.** A sender the
   owner wrote to inside the window, or one the assessment tied to an admitted
   relationship memory, is protected, unless the newest received message
   failed the sender check. A rejected or ungrounded assessment never flags.
5. **The owner clears a flag per thread.** Not spam on a flagged thread
   clears it locally, restores the thread's importance at once, and keeps
   later assessments from flagging it again. It changes no mailbox and adds no
   owner-feedback judgment, so the pinned `email.feedback` tool, the learning
   inputs, and the stored feedback rows are untouched.
6. **Report spam on a thread is Archive's gesture with another delta.** The
   owner names one thread; the server derives the account, provider thread,
   and labels. Report spam adds `SPAM` and removes `INBOX`; Not spam, for a
   thread in Spam, adds `INBOX`, removes `SPAM`, and clears the flag. The
   consent, its expiry, exact-argument approval, the effect-boundary recheck,
   replay, and the uncertainty rule are ADR-0095's, reused rather than copied;
   the consent and the thread's operation gain one `spam` marker, and a plain
   archive's consent digest is unchanged. This answers the design's third
   open question. Gate 11 still governs the sender-level label actions, whose
   threads come from the run's own search; this action's single target is the
   thread the owner named, as Archive's is.
7. **The extension rides the Milestone 31 flag.** Without
   `AGENT_EMAIL_UNSUBSCRIBE_ENABLED` no thread is flagged and both new routes
   are unmounted. No new flag, scope, OAuth permission, knob, or table is
   added.
8. **Three new hard gates.** `gate.email.spam_flag`,
   `gate.email.spam_sender_check`, and `gate.email.spam_thread_gesture` join
   Milestone 31, the release-evidence gate becomes gate 23, and the native gate
   covers the new surfaces. The milestone declares twenty-three gates.

## Scope admission and consequences

- Milestone 31's authorized scope gains the suspected-spam flag, the sender
  check, two routes under `/v1/email/threads`, and the native tag and thread
  actions. Its deferred scope is unchanged.
- The first refresh after deployment begins re-assessing retained mail, four
  threads per refresh within the automatic-email allowance.
- Mail cached before the sender check existed reads `none` until Gmail sends
  it again in a changed thread; the flag still rests on the model's verdict.
- Editing the assessment's domain, runtime, and application modules changes
  the email-semantic and email-People implementation digests, as every edit
  to them does.
- A false positive costs the owner one tap and is counted: the false-flag rate
  is a tracked metric.
- The owner's real-mailbox smoke grows by one thread report reversed by Not
  spam.

## Alternatives considered

- **Move suspected spam to Spam automatically:** rejected by the owner. It
  would reverse Milestone 26's exclusion of automatic mailbox actions, and a
  false positive on genuine mail is deleted after thirty days.
- **Flag on a failed sender check alone:** rejected because careless but
  legitimate senders fail DMARC, and those include founders writing through
  outreach tools.
- **Reuse the `bulk` verdict:** rejected because bulk mail the owner signed up
  for is wanted, and demoting it to spam would hide it.
- **A `not_spam` owner-feedback judgment:** rejected because the judgment set
  is pinned in a Chat tool and in stored rows, and the feedback projection
  would read an unknown judgment as "no reply needed".
- **A second, spam-only copy of the Archive machinery:** rejected as two
  implementations of one consent model.

## Acceptance status

The owner asked for the capability and chose flag-and-confirm on 2026-10-08,
then directed implementation the same day. Implementation proceeds under the
repository's red-green rule behind the existing Milestone 31 flag. Pull
request creation, merge, production delivery, and any live mailbox write
retain their explicit authorization boundaries; the owner's real-mailbox smoke
remains the milestone's release evidence.
