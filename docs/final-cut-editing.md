# Edit a reel in Veetbot Mac chat

Open a new Veetbot chat on the Mac running Final Cut and send this request.

> Select my footage folder on this Mac and make a 60-second highlight reel. Open with the best moment, keep the natural sound, skip repeated takes, and show me the rough cut.

Choose the source directory in the Mac picker. Choose **Local only** to
keep transcripts and audiovisual descriptions on the Mac. **Share notes**
also sends those descriptions and timecoded speech to the configured chat
model. Both modes keep source media, contact sheets, proxies and renders on
the Mac. Review the ordinary Veetbot approval cards as they appear.

Open the preview link in the Mac chat. Ask, “Hold the opening shot two
seconds longer and cut the last interview answer.” Each revision returns
a new preview. If a preview is damaged, ask to render the same edit again.
Review and approve the replacement version before delivery. Once satisfied, say, “Deliver this version and open its
project in Final Cut.” Veetbot returns the local movie and editable XML links.
The movie uses the approved preview's exact bytes.
There is no Final Cut Share click. The version's new library links to
the originals by file path. Keep the originals in place and unchanged through
import and reconciliation. Final Cut resolves links when it opens the project.

To confirm the import, select the project in Final Cut, choose **File >
Export XML**, and save it. Tell Veetbot “Verify this version against my
Final Cut export” and choose that file in the Mac picker. An unconfirmed
import stays unconfirmed across retries. If the project never appeared,
check the version's library first, then manually open its `timeline.fcpxml`
once. A playable movie can be delivered even if the import is unconfirmed.

## Connect the editing Mac

Install this branch's Veetbot package on the API and interactive worker.
On the Apple silicon editing Mac, install Final Cut Pro, FFmpeg with H.264
and AAC encoders, and the locked project environment. Prepare the local
speech and audiovisual models once, before opening personal footage.

```sh
brew install ffmpeg openai-whisper
uv sync --frozen
uv tool install --python 3.13 mlx-vlm==0.8.1
"$(brew --prefix openai-whisper)/libexec/bin/python" -c 'import whisper; whisper.load_model("base", device="cpu")'
uvx --from huggingface_hub hf download mlx-community/gemma-3n-E2B-it-4bit
```

Start the worker while signed in to the Mac. Pass the local snapshot
directory printed by the download command.

```sh
uv run python -m final_cut.worker serve --model /absolute/path/to/model/snapshot
```

The default state directory is `~/Library/Application Support/Veetbot/Edit`.
Its mode is 0700. The command socket is `editor.sock` inside it. Playback
binds to 127.0.0.1 on port 59381. Keep the worker running while editing or
playing links. To revoke access immediately from another terminal, run
`uv run python -m final_cut.worker revoke`. Prior version files stay on disk.

For a core running on this Mac, set `AGENT_FINAL_CUT_SOCKET` on the API
and interactive worker to the absolute path of that socket. For a hosted
core, provision a private remote directory accessible only to the Veetbot
service account and connect the Mac through the operator's authenticated
SSH connection. For example, with an authorized host alias and remote
directory already provisioned, run the following on the Mac.

```sh
ssh -NT -o ExitOnForwardFailure=yes \
  -R "/run/veetbot-editor/editor.sock:$HOME/Library/Application Support/Veetbot/Edit/editor.sock" \
  veetbot-editor
```

Set `AGENT_FINAL_CUT_SOCKET=/run/veetbot-editor/editor.sock` for those
two services. Restart them and open a new chat to pin the tool catalog.
Access to either socket grants the caller the authority of this paired owner.
Limit the hosted socket and its parent directory to the Veetbot service account.
When changing Macs, revoke the old folder grant and disconnect its tunnel
before restarting both services with the replacement socket.
No raw media travels through this connection. The Mac worker reclaims its
own stale socket on restart. If the SSH tunnel leaves a stale socket at
the remote end, confirm it has no listener before removing it and reconnecting.

## Supported footage and timing

Use progressive, constant-rate, square-pixel SDR MOV, MP4 or M4V originals
with one video track, up to one aligned audio track and at most one million frames per clip. A cut uses one
source frame rate and raster size, at most 64 shots and up to ten minutes.
A source range uses a zero-based first frame and an exclusive end frame.
Media without an audio track contributes silence. Source timecode is
added when writing FCPXML. The worker validates version 1.10 against the
DTD bundled with Final Cut. Excerpts include up to 15 seconds of audio
and eight labeled frames. More excerpts can inspect a longer clip.

The repeatable timing fixture generates three synthetic 29.97 fps sources
with drop-frame timecode, visible frame codes and distinct timed audio
cues. Generate them in a new folder using `uv run python
scripts/final_cut_fixture.py generate "/absolute/new/folder"`. After editing
those sources, run `uv run python scripts/final_cut_fixture.py verify
"/absolute/version/directory/movie.mp4"` to check the encoded frame identities
and cue windows against the saved plan.

## Tested Mac workflow

In a native Veetbot Mac chat connected to an isolated development backend,
the assistant inspected audiovisual excerpts from all three synthetic originals.
Its rough cut used 599 frames at 30000/1001 fps. A second chat message asked
to shorten only the end of the opener by 15 frames and deliver the revision.
The earlier cut remained playable. The new cut had 584 frames. Final Cut Pro
13.0 opened the new library and displayed Take 3, Take 1 and Take 2 on the
timeline. Its own XML export resolved all three original files and matched
each source range, timeline offset and the total duration. Decoding the
delivered movie matched all 584 visible frame codes and 360 audio cue windows.
This verifies the editing and delivery path. These synthetic takes use similar
motion, so they do not establish the quality of creative ranking on real footage.
