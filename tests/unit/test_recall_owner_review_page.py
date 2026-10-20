"""The shorter review starts blank and exports only actual scoped assessments."""

import json

from playwright.async_api import Route, async_playwright

from scripts.build_recall_owner_review import render
from tests.real_browser_support import require_real_browser


async def test_scoped_packet_empty_draft_restore_and_accurate_export() -> None:
    require_real_browser()
    selected = [f"synthetic-{n}" for n in range(37)]
    groups = {f"History {n}": [str(n) * 64] for n in range(4)}
    scope = {
        "proposed_individual_scenarios": selected,
        "actual_obsolete_answer_scenarios": selected[:9],
        "original_strict_regression_scenarios": {cid: ["automatic"] for cid in selected[9:29]},
        "interruption_summary_groups": groups,
        "omitted_required_scenarios": [f"omitted-{n}" for n in range(270)],
        "cost_summary": [],
    }
    data = {
        "observations_sha256": "a" * 64,
        "original_review_items_sha256": "b" * 64,
        "review_items": [
            {"original_item_sha256": hashes[0], "item": {"kind": label}}
            for label, hashes in groups.items()
        ],
        "cases": [
            {
                "case": {
                    "id": cid,
                    "task": {"question": "Synthetic </script><script>window.injected=1</script>"},
                    "history": [],
                    "expected_action": "answer",
                    "expected_values": [],
                },
                "rows": [],
            }
            for cid in selected
        ],
    }
    page_html = render(data, scope)

    async def serve(route: Route) -> None:
        await route.fulfill(content_type="text/html", body=page_html)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page()
        await page.route("**/*", serve)
        await page.goto("https://synthetic-review.test/")
        try:
            assert await page.locator("[data-finding]").count() == 42
            assert await page.locator("input:checked").count() == 0
            assert await page.locator("#reviewer").input_value() == ""
            assert await page.evaluate("typeof window.injected") == "undefined"
            await page.locator("#export").click()
            assert "Complete the assessment" in await page.locator("#error").inner_text()
            await page.locator("details").first.click()
            await page.locator(".verdict").first.select_option("acceptable")
            await page.reload()
            assert await page.locator(".verdict").first.input_value() == "acceptable"
            assert "1 of 42" in await page.locator("#progress").inner_text()
            # Only this synthetic page is filled; the owner's packet stays blank.
            await page.evaluate(
                """() => {
                    document.querySelectorAll('details').forEach(el => el.open = true);
                }"""
            )
            for field in await page.locator(".verdict").all():
                await field.select_option("acceptable")
            await page.locator("#reviewer").fill("Synthetic reviewer")
            await page.locator("#decision").select_option("accept_bounded_rollout")
            await page.locator("#findings").fill(
                "Synthetic complete review for browser verification."
            )
            await page.locator("#risk").fill(
                "Synthetic residual risk finding for the omitted cases."
            )
            await page.locator("#human").check()
            await page.locator("#independent").check()
            async with page.expect_download() as pending:
                await page.locator("#export").click()
            downloaded = await pending.value
            path = await downloaded.path()
            assert path is not None
            submission = json.loads(path.read_text())
            assert submission["version"] == "recall-owner-review-submission@2"
            assert submission["reviewed_scenarios"] == selected
            assert submission["omitted_scenarios"] == scope["omitted_required_scenarios"]
            assert set(submission["scenario_findings"]) == set(selected)
            assert submission["reviewed_item_groups"] == groups
            assert "reviewed_items" not in submission
            await page.locator(".verdict").first.select_option("unacceptable")
            await page.locator(".notes").first.fill("Synthetic unacceptable answer.")
            await page.locator("#export").click()
            assert "requires changes or rejection" in await page.locator("#error").inner_text()
            await page.locator("#decision").select_option("reject")
            async with page.expect_download() as pending_rejection:
                await page.locator("#export").click()
            rejection_path = await (await pending_rejection.value).path()
            assert rejection_path is not None
            assert json.loads(rejection_path.read_text())["decision"] == "reject"
        finally:
            await browser.close()
