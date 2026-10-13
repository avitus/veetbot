# ADR-0201: Signing evidence for the reviewed foundation split

- Status: Accepted by the owner on 2026-10-13; required signing evidence verified
- Date: 2026-10-13
- Related: ADR-0074, ADR-0107
- Scope: PR #159 only; no general change to the exact-revision release gate

## Problem

The owner approved splitting the memory recall release into foundation PR #159
and remainder PR #158 because CodeRabbit cannot review more than 300 files.
PR #159 uses `codex/memory-recall-foundation`; the remainder uses `dev`.
The signing smoke runs only on `dev`, and its restricted context admits only
`dev` and `main`. The release contract nevertheless requires a signing pass on
the exact revision proposed for `main`.

CircleCI's [current pipeline-trigger API](https://circleci.com/docs/api/v2/)
accepts a branch or tag, not a historical
commit together with the `dev` branch and `run_verify` parameter. Rewinding `dev`
or expanding credential access would introduce unrelated release risk.

## One-time decision

For PR #159 at `794f80c539eeefc3ece2114b1753d1af4adbc3fa` only, accept a
successful hosted `apple-signing-smoke` on the already reviewed `dev` revision
`c6cdc3f124c5440416be2445faed99d70bac40ec` if the following conditions hold:

1. The owner explicitly approves this exception before the foundation merge.
2. Every file and executable mode under `clients/apple`, the complete
   `.circleci/config.yml`, and `scripts/build_macos_testflight_package.sh` is
   identical at both revisions. The comparison covers 167 files. The SHA-256 of
   the sorted-key, compact JSON manifest is
   `81e8c1e23b4827d12704fee452259d690b5a35338bce54ad7a443f029d94f513`.
   The checked-in Xcode project has no external package or custom script phase
   that introduces an additional repository input to this archive.
3. The smoke uses the existing committed workflow, Xcode executor, managed
   application signing bundle, and restricted installer-signing context. It
   must verify the application and installer signatures successfully.
4. Record the actual tested revision, pipeline and job identities without
   retagging the pass as a test of the foundation commit. Preserve the input
   manifest with the release evidence and recheck equality immediately before
   merging. The generated build number belongs to the smoke pipeline; the
   foundation's subsequent publication receives its own pipeline number.
5. Every other required hosted check passes on the foundation's exact head, all
   CodeRabbit findings remain resolved, and its production pipeline verifies,
   deploys and signs the actual merge revision normally.

Any changed signing input or either changed source revision voids this exception.
No branch protection, context restriction, workflow filter, credential location,
or publication authority changes. PR #158 retains the ordinary exact-head
signing requirement. ADR-0199 concerns recall measurements and is not authority
for this separate signing exception.

## Alternative

Keep the foundation merge blocked until an independently approved workflow can
run the smoke on its exact commit. A passing unsigned Apple test, an unexecuted
signing job, or a later deployment attempt does not satisfy the existing gate.

## Evidence

[Pipeline 1098, signing job 5416](https://circleci.com/gh/avitus/veetbot/5416)
passed on the named `dev` revision in 67.761 seconds. The 167-file comparison was
rechecked before the foundation merge. All ten foundation verification jobs
passed in pipeline 1095, and both CodeRabbit conversations were resolved. The
[release record on PR #159](https://github.com/avitus/veetbot/pull/159#issuecomment-6154719587)
preserves the tested revision and approved exception. The foundation merged as
`81062c5baf52070c032939bba38d74abd8cd4810`. Production pipeline 1099 completed
successfully: application, documentation, website and TestFlight upload jobs passed.
All three public release identities matched `20261013-204914-81062c5`. This
confirms delivery of the foundation; it does not certify the remaining PR #158 or
Apple's subsequent asynchronous processing and device installation.
