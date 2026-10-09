# Reactor vs TensorScale benchmark

A local page that sends one prompt to Reactor and TensorScale at the same instant,
plays both videos side by side, and times every phase. It is a developer tool, not
part of the agent: nothing here is composed into the application.

## Run it

```sh
make env-pull          # or export REACTOR_API_KEY and TENSORSCALE_API_KEY
make media-benchmark   # then open http://127.0.0.1:8765/
```

`REACTOR_API_KEY` is a Reactor dashboard key; `TENSORSCALE_API_KEY` needs the
`MiniMax-H3-Fast` scope for the like-for-like pairing, or `ltx2-5-fast` for the
LTX alternative. With only one key the page runs that side alone.

## What it compares

- **Reactor** `reactor/fast-h3`: MiniMax H3 Fast as a real-time WebRTC stream.
- **TensorScale** `MiniMax-H3-Fast` FL2VA (same model, buffered MP4), or LTX-2.5
  Fast through its streaming endpoint (a different model, marked as such).

Both get the same prompt, length, aspect ratio and seed. Each run records time
to first frame, setup, generation, delivery, finished, generation speed
(× real time), resolution, frame rate shown, dropped frames, data received,
bitrate, and estimated cost. Reactor runs also record WebRTC round-trip time,
jitter, packet loss and freezes, and TensorScale runs record `Server-Timing`.
Repeated runs give medians and p90s; history exports as CSV or JSON. The page's
"How each number is measured" section defines each measure.

## Why there is a server

TensorScale's API sends no CORS headers, so a browser cannot call it directly.
The server binds 127.0.0.1 only and reads both keys from `.env` or the
environment. It mints a Reactor token scoped to one `fast-h3` session, and it
relays TensorScale generations to fixed endpoints. Neither key reaches the page.
Cross-origin posts and foreign `Host` headers are refused.

The page loads Reactor's browser SDK (`@reactor-team/js-sdk@3.0.2`) and its
dependencies from cdn.jsdelivr.net at the exact versions pinned in
`static/index.html`. Run history stays in the browser's local storage.
