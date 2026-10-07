"""Scripted tasks check website effects through model-sized observations."""

from __future__ import annotations

import asyncio
import html as html_module
import json
from time import monotonic
from typing import Any
from uuid import UUID

import pytest
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from agent_core.domain.browser import BrowserAction, BrowserActionKind, BrowserObservationExpansion
from agent_core.domain.browser_projection import browser_context_projection
from agent_core.domain.messages import FileReferencePart, TextPart
from agent_core.domain.tool_output import content_bytes
from agent_core.ports.browser import expand_browser_observation
from tests.browser_tasks import OUTCOMES, SCENARIOS, Scenario, TaskOutcome
from tests.unit.test_browser_runtime_real_chromium import browsing, html


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda scenario: scenario.id)
async def test_scripted_browser_task(scenario: Scenario) -> None:
    started = monotonic()
    effects: list[dict[str, Any]] = []
    completed = False
    operations = 0
    label = html_module.escape(scenario.target, quote=True)
    button = f'<button id="commit">{label}</button>'
    body = button
    script = ""
    if scenario.fixture == "dense":
        body = "".join(f"<button>Decoy {i}</button>" for i in range(300)) + button
    elif scenario.fixture == "hidden":
        body = "<button hidden>Hidden</button>" * 5000 + button
    elif scenario.fixture == "long-text":
        body = "<p>" + "Lesson context 雪. " * 2000 + "</p>" + button
    elif scenario.fixture == "shadow":
        body = '<div id="host"></div>'
        script = (
            "document.querySelector('#host').attachShadow({mode:'open'}).innerHTML="
            + json.dumps(button)
            + ";"
        )
    elif scenario.fixture == "text":
        body = '<label>Answer<input id="answer"></label><button id="commit">Submit</button>'
    elif scenario.fixture == "select-check":
        body = (
            '<label for="answer">Answer</label><select id="answer"><option value="">Choose</option>'
            '<option value="yes">Yes</option></select>'
            '<label>Confirm<input id="confirm" type="checkbox"></label>'
            '<button id="commit">Submit</button>'
        )
    elif scenario.fixture == "slow":
        body = "<main>Loading</main>"
        script = (
            f"await fetch('/ready'); document.querySelector('main').innerHTML={json.dumps(button)};"
        )
    # The server owns the completion predicate; a click alone is never success.
    script = (
        "(async()=>{"
        + script
        + """
      const root = document.querySelector('#host')?.shadowRoot || document;
      root.querySelector('#commit').addEventListener('click', async () => {
        const answer = document.querySelector('#answer')?.value || null;
        const confirmed = document.querySelector('#confirm')?.checked || false;
        const response = await fetch('/effect', {method:'POST',
          headers:{'Content-Type':'application/json'}, body:JSON.stringify({answer, confirmed})});
        const state = await response.json();
        document.body.dataset.progress = String(state.count);
      });
    })();"""
    )

    async def home(request: Request) -> Response:
        del request
        return html(body, script=script)

    async def ready(request: Request) -> Response:
        del request
        await asyncio.sleep(0.3)
        return JSONResponse({"ready": True})

    async def effect(request: Request) -> Response:
        effects.append(await request.json())
        return JSONResponse({"count": len(effects)})

    try:
        async with browsing(
            [
                Route("/", home),
                Route("/ready", ready),
                Route("/effect", effect, methods=["POST"]),
            ]
        ) as (site, runtime):
            observation = await runtime.navigate(site.url("/"))
            operations += 1

            async def choose(name: str, kind: BrowserActionKind, **values: Any) -> None:
                nonlocal observation, operations
                for _ in range(128):
                    projected = browser_context_projection(
                        observation.model_dump(mode="json"),
                        budget=scenario.budget,
                        reference=FileReferencePart(
                            artifact_id=UUID(int=1), media_type="application/json"
                        ),
                    )
                    assert projected is not None and isinstance(projected[0], TextPart)
                    assert len(content_bytes(projected)) <= scenario.budget
                    payload = json.loads(projected[0].text)
                    found = next((e for e in payload["elements"] if e["name"] == name), None)
                    if found is not None:
                        observation = await runtime.act(
                            BrowserAction(
                                kind=kind,
                                ref=found["ref"],
                                expected_revision=payload["revision"],
                                **values,
                            )
                        )
                        operations += 1
                        return
                    assert "next_observe" in payload, (
                        "target undiscoverable in bounded observations"
                    )
                    observation = await expand_browser_observation(
                        runtime,
                        BrowserObservationExpansion.model_validate(payload["next_observe"]),
                    )
                    operations += 1
                pytest.fail("synthetic task exceeded bounded discovery operations")

            if scenario.fixture == "text":
                await choose("Answer", BrowserActionKind.TYPE, value="yes")
                await choose("Submit", BrowserActionKind.CLICK)
            elif scenario.fixture == "select-check":
                await choose("Answer", BrowserActionKind.SELECT, value="yes")
                await choose("Confirm", BrowserActionKind.CHECK)
                await choose("Submit", BrowserActionKind.CLICK)
            else:
                for _ in range(scenario.expected_effects):
                    await choose(scenario.target, BrowserActionKind.CLICK)
            assert len(effects) == scenario.expected_effects
            if scenario.fixture in {"text", "select-check"}:
                assert effects[0]["answer"] == "yes"
            if scenario.fixture == "select-check":
                assert effects[0]["confirmed"] is True
            completed = True
    finally:
        OUTCOMES[scenario.id] = TaskOutcome(
            completed=completed,
            operations=operations,
            observed_effects=len(effects),
            duration_seconds=monotonic() - started,
        )
