"""Publish the existing PR widget schema through the approved GitHub reader."""

from datetime import UTC, datetime

QUERY = """
query WidgetPullRequests($owner: String!, $name: String!, $after: String) {
  repository(owner: $owner, name: $name) {
    pullRequests(states: OPEN, first: 10, after: $after,
      orderBy: {field: CREATED_AT, direction: DESC}) {
      totalCount
      nodes {
        number title author { login } isDraft mergeable
        latestOpinionatedReviews(first: 50) {
          nodes { state }
          pageInfo { hasNextPage }
        }
        reviewRequests { totalCount }
        commits(last: 1) { nodes { commit { statusCheckRollup { state } } } }
      }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""


def review_state(pull):
    """Summarize decisive reviews only when the complete reviewer set was returned."""
    reviews = pull.get("latestOpinionatedReviews")
    if not reviews or reviews["pageInfo"]["hasNextPage"]:
        return "unknown"
    states = {item["state"] for item in reviews["nodes"]}
    if "CHANGES_REQUESTED" in states:
        return "changes_requested"
    if "APPROVED" in states:
        return "approved"
    requests = pull.get("reviewRequests")
    if requests is None:
        return "unknown"
    return "pending" if requests["totalCount"] else "none"


def check_state(pull):
    """Use the current head's combined checks and statuses, never an older commit."""
    commits = pull["commits"]["nodes"]
    if not commits:
        return "unknown"
    rollup = commits[-1]["commit"]["statusCheckRollup"]
    if rollup is None:
        return "none"
    return {
        "SUCCESS": "passing",
        "ERROR": "failing",
        "FAILURE": "failing",
        "PENDING": "pending",
        "EXPECTED": "pending",
    }.get(rollup["state"], "unknown")


def main(inputs, job):
    """Acquire complete bounded JSON before publishing; an error keeps the old feed stale."""
    interval = inputs.get("refresh_seconds", 3600)
    repositories = []
    for name in inputs["repositories"]:
        owner, repository = name.split("/", 1)
        pulls = {}
        cursor = None
        seen_cursors = set()
        expected_count = None
        while True:
            reply = job.http.request(
                "POST",
                "https://api.github.com/graphql",
                json={
                    "query": QUERY,
                    "variables": {
                        "owner": owner,
                        "name": repository,
                        "after": cursor,
                    },
                },
            )
            body = reply["json"]
            if reply["status"] != 200 or body.get("errors"):
                raise ValueError("GitHub refused the authenticated PR read; no snapshot published.")
            result = (body.get("data") or {}).get("repository")
            if result is None:
                raise ValueError("The GitHub reader cannot access a selected repository.")
            page = result["pullRequests"]
            if expected_count is None:
                expected_count = page["totalCount"]
            elif page["totalCount"] != expected_count:
                raise ValueError("GitHub pagination changed; retry before publishing a count.")
            for pull in page["nodes"]:
                number = pull["number"]
                if number in pulls:
                    raise ValueError("GitHub pagination changed; retry before publishing a count.")
                pulls[number] = {
                    "number": number,
                    "title": pull["title"][:500],
                    "author": (pull.get("author") or {}).get("login", "")[:100],
                    "draft": pull["isDraft"],
                    "review": review_state(pull),
                    "checks": check_state(pull),
                    "merge": {"MERGEABLE": "mergeable", "CONFLICTING": "conflicting"}.get(
                        pull["mergeable"], "unknown"
                    ),
                }
            if not page["pageInfo"]["hasNextPage"]:
                if len(pulls) != expected_count:
                    raise ValueError("GitHub pagination is incomplete; no snapshot published.")
                break
            cursor = page["pageInfo"]["endCursor"]
            if not cursor or cursor in seen_cursors:
                raise ValueError("GitHub returned an invalid pagination cursor.")
            seen_cursors.add(cursor)
        repositories.append(
            {
                "name": name,
                "status": "ok",
                "message": "Verified via authenticated GitHub API.",
                "totalOpen": len(pulls),
                "pullRequests": list(pulls.values()),
            }
        )
    snapshot = {
        "observedAt": datetime.now(UTC).isoformat(),
        "expectedIntervalSec": interval,
        "repositories": repositories,
    }
    job.feed.publish("agent.github-pull-requests", snapshot, expected_interval_seconds=interval)
    return {"summary": "Updated the pull request widget from authenticated GitHub data."}
