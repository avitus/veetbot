"""The real-browser delivery partition, shared by routing and reporting."""

BROWSER_MODULES = frozenset(
    {
        "test_browser_playwright.py",
        "test_browser_observation_pages.py",
        "test_browser_image_upload.py",
        "test_browser_rich_text.py",
        "test_browser_expansion_real_chromium.py",
        "test_browser_readiness_real_chromium.py",
        "test_browser_completion_real_chromium.py",
        "test_browser_regions_real_chromium.py",
        "test_browser_extraction_real_chromium.py",
        "test_browser_text_real_chromium.py",
        "test_browser_focus_real_chromium.py",
        "test_browser_synthetic_tasks.py",
        "test_browser_runtime_real_chromium.py",
        "test_browser_grant_hostile_pages.py",
        "test_browser_grant_hostile_constructs.py",
        "test_device_handoff_real_chromium.py",
        "test_browser_task_grant_live_runtime.py",
    }
)
