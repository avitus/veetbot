# ADR-0152: Hosted CI measures the browser image's own requests

- Status: Accepted (authorized by the repository owner, 2026-10-05); implements
  the owner's 2026-10-03 direction
- Date: 2026-10-05
- Related: ADR-0107, ADR-0114, ADR-0145
- Amends: ADR-0146 (the measurement it asked a person to repeat after raising
  Playwright now runs in hosted CI)
- Detailed design: `docs/plan/development-toolchain.md`,
  `docs/plan/browser-automation.md`

## Context

ADR-0146 stops hosted Chromium's own requests with settings that belong to the
browser: four internal feature names, a push-messaging check-in address, and a
managed policy in the directory each Chromium build reads. A Playwright release
brings its own Chromium build, and any of these can stop working with it. A
renamed feature stops matching, the policy directory can move, and a new build
can add a request that no setting covers. Chromium reports none of this.

Two static tests check that the settings are present: the launch test compares
the repeated feature list with the installed Playwright driver, and a toolchain
test finds the policy in both directories. Neither shows that the settings
work. The real-browser test that observes the requests skips where Chromium is
not installed, which includes every CI job. ADR-0146 therefore asked a person
to repeat its built-image measurement after raising Playwright, and nothing
enforced that.

## Evidence

Measured on 2026-10-05 in images built for arm64 and, emulated on Apple
silicon, for x86_64, through a refusing relay, on an internal network:

- On both architectures, an image built from the commit before ADR-0146 asked
  for the network time, `www.google.com`, the autofill host, the
  push-messaging check-in and the spelling dictionary, as well as the account
  listing. The current image asked for the account listing alone.
- The spelling dictionary needs more than a focused field. The runtime's type
  action alone never fetched it. Neither did a click and typing into the fields
  of the sign-in and address page, in four runs. A click into a lone text field
  on its own page, then typing, fetched it in each of the thirteen runs that
  followed, on both architectures; an identical earlier batch had fetched it
  in none of seven, which this ADR does not explain.
- With the dictionary policy removed from the current Dockerfile, the probe
  failed on `redirector.gvt1.com:443` alone, in both runs on arm64.
- One measurement took about two and a half minutes once the image was built.

## Decision

1. A test lane, `make test-browser-image`, builds the browser-profile service
   image and starts a container from it. The container has the production
   compose limits: the service's user, a read-only root with the same `/tmp`
   mount, its process, memory, CPU and shared-memory limits, no capabilities,
   `no-new-privileges` and an init process. Its only network is an internal
   Docker network with no route out. Every key of the compose service is either
   translated into the container's options or named as deliberately ignored, so
   a new limit cannot be left out silently.
2. Inside the container, a probe repeats ADR-0146's scenario through the
   production runtime's actions, headed on the runtime's own display. On a page
   with a sign-in form and an address form it clicks into and types in two
   fields and submits the sign-in form. On a second page it clicks into a lone
   text field and types. It stays idle for 120 seconds, then types into that
   field once more and waits ten seconds. A relay serves the
   synthetic site and refuses and records every other request, which is how
   ADR-0145 measured. The probe runs the image's own installed code and imports
   nothing the image lacks.
3. The lane fails if the browser asked for anything except the page and the
   one account request to `accounts.google.com:443`, or if the site saw a
   headless user agent.
4. The CI job `browser-image` runs the lane on an x86_64 machine executor, the
   architecture production runs, and on an arm64 one, because Playwright
   installs a different Chromium build there. Release packaging requires both.
5. Each job writes a CircleCI cache record, after the lane passes, keyed by
   architecture and by its inputs: the image's Dockerfile, the production
   compose file, the Playwright version in `uv.lock`, the runtime's
   vendor-request launch arguments, startup/context configuration, and check code. A job that finds
   its record halts successfully after checkout, on every branch. Any change to
   the inputs runs the lane in full. The record's key carries a version prefix;
   raising it forces a new measurement.
6. Startup and context configuration are the semantic bodies of `start` and
   `_context_options`; prose, unrelated runtime operations and other locked
   packages are excluded. Static launch tests keep guarding the switches.

## Consequences

- Raising Playwright, editing the image, or changing a vendor-request switch
  measures the shipped image on both architectures before it can be released.
  The manual measurement step in the deployment guide is gone.
- Most pipelines pay only for two machine executors to check out and halt. A
  measured run builds the image without a layer cache, then measures for about
  two and a half minutes; the build's hosted duration is not yet measured.
- Branch protection does not require the two contexts yet; release packaging
  does, so a failure still stops delivery. Requiring them for a merge is the
  owner's setting.
- The check sees what reaches the proxy. A request that bypassed it would not
  be counted; ADR-0146 found none, and the internal network gives one nowhere
  to go. A request that starts after two idle minutes is outside the window.
- The dictionary request depends on how a field takes focus, and one batch of
  runs never made it. A run that misses it passes; a later run of the same
  inputs is skipped by the record. Raising the record's version prefix measures
  again.
- The image's system packages are not pinned, so a later build of the same
  inputs can differ. Raising the record's version prefix measures it again.

## Alternatives rejected

- **Running the lane in every pipeline.** It would add two machine executors
  and about ten minutes to each, while the browser's requests change only with
  the inputs above.
- **CircleCI path filtering.** It needs a dynamic setup workflow for the whole
  configuration and compares commits, not contents. A content key also lets the
  pull request's verified inputs skip on `main`, as ADR-0114 does for trees.
- **Keying on the whole of `uv.lock` and the runtime module.** In the three
  weeks before this decision the runtime module changed in 32 commits; the
  Dockerfile changed in two.
- **Installing Chromium in an ordinary CI job.** It would test a browser built
  outside the image, without its policy, limits or display.
- **Running the probe under pytest inside the image.** The image has no test
  dependencies, and adding them changes the image under test.

## Verification

- `tests/unit/test_browser_image_inputs.py`: the inputs change with the
  Dockerfile, the compose limits, the Playwright version and the launch
  switches, and not with the rest of the runtime or another locked package.
- `tests/security/test_browser_image_traffic.py`: the measurement in the built
  image. Against an image built from the commit before ADR-0146 it fails and
  names five vendor requests; without the dictionary policy it fails on the
  dictionary alone; against the current image it passes.
- `tests/unit/test_toolchain.py`: both architectures run the job, release
  packaging requires both, and a job halts only on its own record.
