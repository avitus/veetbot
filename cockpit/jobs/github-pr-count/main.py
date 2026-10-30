def main(inputs, job):
    repositories = inputs["repositories"]
    if not isinstance(repositories, list) or not 1 <= len(repositories) <= 20:
        raise ValueError("Expected between 1 and 20 repositories")
    counts = {}
    for repository in repositories:
        if not isinstance(repository, str):
            raise ValueError("Repository must be an owner/name string")
        parts = repository.split("/")
        if len(parts) != 2 or any(part in ("", ".", "..") for part in parts):
            raise ValueError("Repository must be an owner/name string")
        owner_chars = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-"
        repo_chars = owner_chars + "_."
        if any(c not in owner_chars for c in parts[0]) or any(c not in repo_chars for c in parts[1]):
            raise ValueError("Invalid repository characters")
        if repository in counts:
            raise ValueError("Duplicate repository")
        seen = set()
        for page in range(1, 11):
            url = "https://api.github.com/repos/" + repository + "/pulls?state=open&per_page=100&page=" + str(page)
            response = job.http.request("GET", url)
            status = response.get("status")
            if status != 200:
                raise RuntimeError("GitHub HTTP " + str(status) + " for " + repository)
            pulls = response.get("json")
            if not isinstance(pulls, list):
                raise RuntimeError("Expected a GitHub pull request array")
            for pull in pulls:
                if not isinstance(pull, dict) or not isinstance(pull.get("number"), int) or pull.get("state") != "open":
                    raise RuntimeError("Unexpected pull request data")
                seen.add(pull["number"])
            if len(pulls) < 100:
                counts[repository] = len(seen)
                break
        else:
            raise RuntimeError("Pagination limit reached; refusing to return an incomplete count")
    return {"counts": counts}
