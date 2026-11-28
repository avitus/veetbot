"""Local owner process. The assistant has no operation that accepts a path or command."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import re
import socket
import socketserver
import stat
import subprocess
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from final_cut.editor import Editor, choose_folder
from final_cut.media import run
from final_cut.protocol import (
    REQUEST,
    Cut,
    Excerpt,
    Inventory,
    Recommend,
    Request,
    Revision,
    Select,
)


def dispatch(editor: Editor, request: Request) -> dict[str, Any]:
    if isinstance(request, Select):
        folder = choose_folder()
        answer = run(
            [
                "/usr/bin/osascript",
                "-e",
                "button returned of (display dialog "
                '"Allow timecoded transcripts and audiovisual descriptions of this folder '
                'to be sent to Veetbot chat and its model? Media stays on this Mac." '
                'buttons {"Local only", "Share notes"} default button "Local only")',
            ]
        )
        return editor.select(folder, answer.strip() == b"Share notes")
    if isinstance(request, Inventory):
        return editor.inventory()
    if isinstance(request, Excerpt):
        return editor.excerpt(request.grant, request.shot, request.analyze)
    if isinstance(request, Recommend):
        return editor.recommend(request.grant, request.evidence, request.seconds, request.brief)
    if isinstance(request, Cut):
        return editor.cut(request.grant, request.shots)
    if isinstance(request, Revision):
        return editor.revise(request.grant, request.version, request.changes)
    if request.operation == "deliver":
        return editor.deliver(request.grant, request.version)
    return editor.reconcile(request.grant, request.version)


def clear_stale_socket(endpoint: Path) -> None:
    try:
        entry = endpoint.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(entry.st_mode) or entry.st_uid != os.getuid():
        raise ValueError("The editor socket path is occupied. Check the private state directory.")
    with socket.socket(socket.AF_UNIX) as probe:
        probe.settimeout(1)
        try:
            probe.connect(str(endpoint))
        except ConnectionRefusedError:
            endpoint.unlink()
        else:
            raise ValueError("An editing worker is already running.")


@contextlib.contextmanager
def worker_slot(endpoint: Path) -> Iterator[None]:
    with endpoint.with_suffix(".lock").open("a+b") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("An editing worker is already running.") from exc
        clear_stale_socket(endpoint)
        yield


def playback_handler(editor: Editor) -> type[BaseHTTPRequestHandler]:
    class Playback(BaseHTTPRequestHandler):
        def log_message(self, *_args: object) -> None:
            pass

        def do_HEAD(self) -> None:
            self.do_GET()

        def do_GET(self) -> None:
            match = re.fullmatch(
                r"/([a-f0-9]{32})/([a-f0-9]{32})/"
                r"(preview\.mp4|movie\.mp4|excerpt\.mp4|sheet\.jpg|timeline\.fcpxml)",
                self.path,
            )
            if self.headers.get("Host") != f"127.0.0.1:{editor.port}" or not match:
                self.send_error(404)
                return
            try:
                editor.active(match[1])
                path = editor.state.joinpath(*match.groups())
                file = path.open("rb")
            except (ValueError, OSError):
                self.send_error(404)
                return
            with file:
                size = os.fstat(file.fileno()).st_size
                first, end = 0, size
                span = self.headers.get("Range")
                if span:
                    part = re.fullmatch(r"bytes=(\d+)-(\d*)", span)
                    if part:
                        first = int(part[1])
                        end = min(size, int(part[2]) + 1) if part[2] else size
                    if not part or not 0 <= first < end:
                        self.send_error(416)
                        return
                self.send_response(206 if span else 200)
                self.send_header(
                    "Content-Type",
                    {
                        ".mp4": "video/mp4",
                        ".jpg": "image/jpeg",
                        ".fcpxml": "application/xml",
                    }[path.suffix],
                )
                if path.suffix == ".fcpxml":
                    self.send_header(
                        "Content-Disposition", f'attachment; filename="Veetbot-{match[2]}.fcpxml"'
                    )
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(end - first))
                if span:
                    self.send_header("Content-Range", f"bytes {first}-{end - 1}/{size}")
                self.end_headers()
                file.seek(first)
                remaining = end - first if self.command != "HEAD" else 0
                self.connection.settimeout(2)
                while remaining:
                    try:
                        with editor.grant_lock(match[1]):
                            chunk = file.read(min(remaining, 65536))
                            if not chunk:
                                return
                            self.wfile.write(chunk)
                    except (ValueError, OSError):
                        return
                    remaining -= len(chunk)

    return Playback


def serve(editor: Editor, endpoint: Path) -> None:
    class RPC(socketserver.StreamRequestHandler):
        def handle(self) -> None:
            self.connection.settimeout(300)
            try:
                line = self.rfile.readline(65537)
                if not line:
                    return
                if len(line) > 65536:
                    raise ValueError("Request is too large.")
                request = REQUEST.validate_json(line)
                result = dispatch(editor, request)
                grant = getattr(request, "grant", None) or result.get("grant")
                if grant:
                    with editor.grant_lock(grant):
                        self.connection.settimeout(2)
                        self.wfile.write(json.dumps(result).encode() + b"\n")
                    return
            except FileNotFoundError:
                result = {"error": "Missing source or Mac tool. Restore it and inspect again."}
            except (ValueError, OSError, subprocess.SubprocessError) as exc:
                # Input validation and system exceptions can contain paths or arguments.
                message = (
                    str(exc)
                    if type(exc) is ValueError
                    else "The Mac declined or could not complete this operation."
                )
                result = {"error": message}
            self.wfile.write(json.dumps(result).encode() + b"\n")

    with (
        worker_slot(endpoint),
        ThreadingHTTPServer(("127.0.0.1", editor.port), playback_handler(editor)) as http,
        socketserver.UnixStreamServer(str(endpoint), RPC) as rpc,
    ):
        os.chmod(endpoint, 0o600)
        threading.Thread(target=http.serve_forever, daemon=True).start()
        try:
            rpc.serve_forever()
        finally:
            http.shutdown()
            endpoint.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("serve", "select", "revoke"))
    parser.add_argument(
        "--state", type=Path, default=Path.home() / "Library/Application Support/Veetbot/Edit"
    )
    parser.add_argument("--port", type=int, default=59381)
    parser.add_argument("--model", type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    editor = Editor(args.state, args.port, args.model)
    if args.action == "revoke":
        editor.revoke()
    elif args.action == "select":
        dispatch(editor, Select(operation="select"))
    else:
        if sys.platform != "darwin":
            parser.error("Run the editor on the Mac with Final Cut installed.")
        serve(editor, editor.state / "editor.sock")


if __name__ == "__main__":
    main()
