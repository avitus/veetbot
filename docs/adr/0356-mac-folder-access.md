# ADR-0356: Write to a folder selected on a Mac

- Status: Proposed
- Date: 2026-11-27

The owner requested one configurable folder per Mac and authorized creating and
replacing files there after selection. Section 29.4 of the engineering plan
places the operation on the target device. This extends the device channel
introduced by ADR-0081.

Mac Settings offers Choose folder, Change folder and Remove access. The app
stores an app-scoped bookmark locally and assigns a fresh random grant identity
on selection. Its registered capability includes that identity. Each Mac has
its own tool, named for the grant and described by the registered computer
name. Removing or replacing a grant invalidates outstanding writes against it.

The agent supplies a single visible filename and UTF-8 text or base64 file
content. The Mac atomically installs the bytes as a regular file directly in
the selected directory. The operation grants no read, list, execute or delete
tool. The cloud workspace has no mount of the owner's directory. Switching
accounts clears the local selection.

The device channel carries the exact grant and a stable invocation ID. The
folder grant authorizes ordinary writes through the external-write policy
for this device capability. Hardline rules and the untrusted-input overlay
still apply. An offline Mac produces a bounded failure. Success requires a
receipt from the target Mac after writing. The folder capability can be
enabled independently of SMS.
