# ADR-0359: Edit selected footage on the Final Cut Mac

- Status: Proposed with implementation requested by the owner
- Date: 2026-11-28
- Related: ADR-0021, ADR-0030, ADR-0255 and ADR-0356

Veetbot needs to inspect moving footage and natural sound, revise a rough
cut and return an editable Final Cut project and a movie. The existing Mac
folder capability authorizes creating and replacing files. It conveys no
authority to read media. Its acknowledgement also carries no inspection
results. Core artifact delivery stores file bytes on the server.

A foreground, operator-configured MCP server named `final_cut` connects
the ordinary registry and approval pipeline to a private Unix socket on
the editing Mac. The editing prompt enters the session's existing MCP
skill catalog. The operator provisions a private SSH socket forward when
the core is remote. This choice avoids a public media endpoint, an Apple
client update and a change to the SMS and folder acknowledgement protocol.
The socket and its enclosing directories are accessible only to the operator
on each end. The operator pairs one Mac with the configured owner's core.

Folder selection on the Mac grants read access to direct regular movie
files. Changing or removing the random grant invalidates clip handles and
playback links. Only the Mac resolves source names. Operations accept
typed ranges and version handles. The worker creates derived files and a
new Final Cut library under its private output directory. A loopback player
supports byte-range requests. The editing Mac opens its own playback URLs.

FFmpeg and FFprobe decode and render the media. On Apple silicon, local
Whisper supplies speech timing and MLX-VLM interprets sampled frames
together with the excerpt's audio. Model weights are installed separately.
With local-only review the Mac compares observations and returns numeric
selections. An explicit choice on the Mac permits sending transcript text
and descriptions to the configured chat model for that selection.

Both renderers consume the same ordered source-frame plan. The full-quality
preview is decoded and checked before publication. Delivery checks its digest
and hard-links the approved bytes as the movie. Before requesting Open
Document the worker durably claims the version. An uncertain handoff is
reconciled by comparing an owner-selected Final Cut XML export against
the source identities, ranges, offsets and total duration. The owner can
inspect the version's library before taking any manual recovery action.
