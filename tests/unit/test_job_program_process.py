"""Known synthetic job programs exercise the actual sandbox entry point locally.

These process tests do not claim kernel isolation or production broker access.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

import pytest


async def test_python_computes_a_result_without_needing_a_broker_or_model() -> None:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "agent_core.execution.job_runner",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={key: value for key, value in os.environ.items() if key != "AGENT_TOOL_BRIDGE_SOCKET"},
    )
    stdout, stderr = await asyncio.wait_for(
        process.communicate(
            json.dumps(
                {
                    "source": (
                        "def main(inputs, job):\n    print('Counting')\n"
                        "    return {'count': len(inputs['items'])}\n"
                    ),
                    "inputs": {"items": ["a", "b", "c"]},
                }
            ).encode()
        ),
        timeout=10,
    )
    assert process.returncode == 0, (stdout, stderr)
    assert json.loads(stdout) == {"ok": True, "value": {"count": 3}}
    assert stderr == b"Counting\n"


async def test_job_combines_http_python_model_and_feed_only_when_its_code_requests_them() -> None:
    import tempfile
    from pathlib import Path

    calls: list[dict[str, object]] = []
    responses: list[object] = [
        {"status": 200, "json": {"reviews": ["Add a regression test", "Looks good"]}},
        {"text": "One reviewer requests a regression test."},
        {"published": True},
        {"logged": True},
    ]

    async def respond(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            calls.append(json.loads(await reader.readline()))
            writer.write(
                json.dumps({"status": "succeeded", "result": responses.pop(0)}).encode() + b"\n"
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    source = """def main(inputs, job):
    response = job.http.request("GET", inputs["url"])
    reviews = response["json"]["reviews"]
    summary = job.model.complete("Summarize these reviews: " + " | ".join(reviews))
    job.feed.publish(inputs["feed"], {"count": len(reviews), "summary": summary},
                     expected_interval_seconds=300)
    job.log("Published the review summary.")
    return {"count": len(reviews), "summary": summary}
"""
    with tempfile.TemporaryDirectory(prefix="job-") as directory:
        socket_path = str(Path(directory) / "broker.sock")
        server = await asyncio.start_unix_server(respond, path=socket_path)
        async with server:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "agent_core.execution.job_runner",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**os.environ, "AGENT_TOOL_BRIDGE_SOCKET": socket_path},
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(
                        json.dumps(
                            {
                                "source": source,
                                "inputs": {
                                    "url": "https://api.example.test/reviews",
                                    "feed": "agent.reviews",
                                },
                            }
                        ).encode()
                    ),
                    timeout=10,
                )
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.wait()
    assert process.returncode == 0, (stdout, stderr)
    assert json.loads(stdout) == {
        "ok": True,
        "value": {
            "count": 2,
            "summary": "One reviewer requests a regression test.",
        },
    }
    assert [call["call"] for call in calls] == [
        "http.request",
        "model.complete",
        "feed.publish",
        "log",
    ]
    assert [call["ordinal"] for call in calls] == [0, 1, 2, 3]
    assert calls[1]["arguments"] == {
        "prompt": "Summarize these reviews: Add a regression test | Looks good",
        "max_output_tokens": 1024,
    }
    assert responses == []


@pytest.mark.parametrize(
    ("source", "error"),
    [
        (
            "def main(inputs, job):\n    raise ValueError('Missing review text')",
            "Missing review text",
        ),
        ("def main(inputs, job)\n    return 1", "SyntaxError"),
        ("value = 1", "main(inputs, job)"),
        ("def main(inputs, job):\n    return float('nan')", "Out of range float"),
        ("def main(inputs, job):\n    return 'x' * 65537", "result exceeds 64 KiB"),
        (
            "def main(inputs, job):\n    return job.model.complete('Summarize')",
            "broker is unavailable",
        ),
    ],
)
async def test_a_failed_job_returns_actionable_feedback(source: str, error: str) -> None:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "agent_core.execution.job_runner",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={key: value for key, value in os.environ.items() if key != "AGENT_TOOL_BRIDGE_SOCKET"},
    )
    try:
        stdout, _stderr = await asyncio.wait_for(
            process.communicate(
                json.dumps(
                    {
                        "source": source,
                        "inputs": {},
                    }
                ).encode()
            ),
            timeout=10,
        )
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    assert process.returncode == 1
    report = json.loads(stdout)
    assert report["ok"] is False
    assert error in report["error"]
