# ADR-0133: Each commit answers for its own reading lane

- Status: Accepted (authorized by the repository owner, 2026-09-26)
- Date: 2026-09-26
- Related: ADR-0060, ADR-0107, ADR-0114
- Amends: ADR-0060 decision 2, which judged a whole range by its newest
  `Reading-Lane:` trailer
- Detailed design: `docs/plan/development-toolchain.md`

## Context

Under ADR-0060 the static CI job compared the newest `Reading-Lane:` trailer in
a pushed range with every path the range changed. Since ADR-0107 hosted
verification runs in two places, and both judge a whole promotion. The
requested `run_verify` pipeline on `dev` has no base revision, so its range
starts at `origin/main`. The `main` pipeline's base is the previous `main`
head.

A correct lane-B or lane-C commit at the tip of `dev` therefore failed the
promotion whenever earlier commits touched lane-A paths. On 2026-09-26 the
`run_verify` pipeline for commit c50c2df failed this way. That commit changed
only `clients/apple/` and declared B. The check reported 51 lane-A paths that
earlier commits had changed, each of them declaring A. The pipeline passed only
after another commit declared A. A replay of all 79 promotions merged into
`main` finds the same false failure on #91 and #104.

The replay also finds two promotions, #40 and #89, that each held a commit
whose own diff needed lane A but which declared B. The newest trailer in each
was A, so the rule passed them. Nothing on `dev` checks trailers when commits
are pushed.

## Decision

1. Each commit's declaration is compared with the floor of that commit's own
   diff. A commit without a trailer declares lane A. A merge commit answers only
   for the paths where its result differs from every parent: its conflict
   resolutions and anything it added itself. The work it merged answers through
   its own commits.
2. A range passes when every commit meets its own floor. It also passes when its
   newest declaration covers every path the range changed, which was ADR-0060's
   rule. That second test lets a new commit declaring the range's floor repair a
   commit that fell short, since a trailer already on `dev` cannot be amended
   without rewriting shared history.
3. A failing report names each commit below its own floor. When the range holds
   a single commit, the report reads as it did before. The job also prints the
   lane a repairing commit must declare. When a pass depends on per-commit
   floors alone, the job says so.
4. Changed paths are read with `-z`, so git never quotes them. A quoted path,
   such as one with a non-ASCII name, used to escape its floor.
5. The base revision is chosen as before.

## Consequences

- A promotion whose commits each declared their own lane passes, whatever lane
  its newest commit declares. Nobody has to add a lane-A commit to the tip of
  `dev` just for the check.
- A range that passed before still passes, unless a path git used to quote now
  raises its floor. In the replay, #91, #104 and c50c2df's pipeline pass. #64
  still fails: its newest trailer was C, and commit e66ae173 declared B while
  changing a gate test. The report now names that commit.
- An under-declared commit is still accepted when a newer declaration covers
  the whole range. That is the price of a repair that works without rewriting
  history.
- The check runs one `git diff-tree` per commit, and only when the newest
  declaration does not cover the range.

## Alternatives considered

- **Strict per-commit floors.** A newer declaration would never cover an
  under-declared commit. That catches #40 and #89, but one wrong trailer on
  `dev` would block every promotion until someone rewrote shared history or
  overrode `main`'s branch protection. The owner chose the repair path.
- **Document the newest-trailer rule.** The newest commit on `dev` would have to
  declare the floor of the whole promotion. The false failures stay, and the
  last person to push carries the burden.
- **Give a requested pipeline the previous `dev` revision as its base.** This
  narrows only the `run_verify` range. The `main` pipeline's base is the
  previous `main` head, so its merge would still judge the whole promotion by
  one trailer.
- **Judge a merge by its first-parent diff.** That diff contains every commit
  the merge brings in, so a merge declaring B would answer for other commits'
  lane-A work. It is the same false failure one level down.

## Verification

- `tests/unit/test_reading_lanes.py` covers:
  - per-commit floors, including the c50c2df case;
  - the repair by the newest declaration;
  - errors that name the commit;
  - a merge that changed nothing itself, and one that did;
  - git-quoted paths;
  - the lines `main` prints to explain its verdict.
