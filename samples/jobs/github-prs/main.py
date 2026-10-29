"""Collect open pull requests with ordinary code; never call a model."""

from datetime import UTC, datetime

QUERY = """
query PullRequests($owner: String!, $name: String!, $after: String) {
  repository(owner: $owner, name: $name) {
    pullRequests(states: OPEN, first: 50, after: $after,
      orderBy: {field: UPDATED_AT, direction: DESC}) {
      nodes {
        number title url isDraft updatedAt reviewDecision
        commits(last: 1) { nodes { commit { statusCheckRollup { state } } } }
      }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""


def main(inputs, job):
    pulls = []
    for repository in inputs["repositories"]:
        owner, name = repository.split("/", 1)
        cursor = None
        while True:
            reply = job.http.request(
                "POST",
                "https://api.github.com/graphql",
                json={
                    "query": QUERY,
                    "variables": {"owner": owner, "name": name, "after": cursor},
                },
            )
            body = reply["json"]
            if reply["status"] != 200 or body.get("errors"):
                raise ValueError(
                    "GitHub could not return pull requests for an approved repository."
                )
            result = body.get("data", {}).get("repository")
            if result is None:
                raise ValueError(
                    "The GitHub reader installation cannot read a selected repository."
                )
            page = result["pullRequests"]
            for pull in page["nodes"]:
                commits = pull["commits"]["nodes"]
                rollup = commits[-1]["commit"].get("statusCheckRollup") if commits else None
                pulls.append(
                    {
                        "repository": repository,
                        "number": pull["number"],
                        "title": pull["title"],
                        "url": pull["url"],
                        "draft": pull["isDraft"],
                        "review": pull["reviewDecision"] or "REVIEW_REQUIRED",
                        "checks": rollup["state"] if rollup else "UNKNOWN",
                        "updated_at": pull["updatedAt"],
                    }
                )
            if not page["pageInfo"]["hasNextPage"]:
                break
            next_cursor = page["pageInfo"]["endCursor"]
            if not next_cursor or next_cursor == cursor:
                raise ValueError("GitHub returned an invalid pagination cursor.")
            cursor = next_cursor
    pulls.sort(key=lambda pull: (pull["repository"], pull["number"]))
    snapshot = {"updated_at": datetime.now(UTC).isoformat(), "pull_requests": pulls}
    job.feed.publish(
        "agent.github-prs",
        snapshot,
        expected_interval_seconds=inputs.get("interval_seconds", 300),
        schema={
            "type": "object",
            "properties": {
                "updated_at": {"type": "string"},
                "pull_requests": {"type": "array", "items": {"type": "object"}},
            },
            "required": ["updated_at", "pull_requests"],
            "additionalProperties": False,
        },
    )
    return {"summary": f"Updated {len(pulls)} open pull requests.", "count": len(pulls)}
