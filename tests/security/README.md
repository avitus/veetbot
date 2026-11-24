# Security tests

Sandbox and browser-image isolation tests live here. Each carries the
`integration` marker with `sandbox` or `browser_image`, so the integration job
deselects them and the `sandbox` and `browser-image` jobs, which have a Docker
daemon, run them. Pure structural security gates remain in `tests/gates` and
run in the static partition.
