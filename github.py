"""GitHub via the Sprites connector, so the token never lives on the Sprite.

On a Sprite: GITHUB_BASE=https://api.sprites.dev/v1/gateway/github/<conn_id>
Locally:     leave GITHUB_BASE unset; uses api.github.com (+ GITHUB_TOKEN if set).
"""
import base64
import io
import json
import os
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = (os.environ.get("GITHUB_BASE") or "https://api.github.com").rstrip("/")
TOKEN = None if os.environ.get("GITHUB_BASE") else os.environ.get("GITHUB_TOKEN")


class GitHubError(RuntimeError):
    def __init__(self, status, msg):
        super().__init__(f"GitHub {status}: {msg}")
        self.status = status


def api(path, method="GET", body=None, raw=False, params=None):
    url = f"{BASE}/{path.lstrip('/')}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, json.dumps(body).encode() if body is not None else None, method=method)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "roast-my-repo")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    if TOKEN:
        req.add_header("Authorization", f"Bearer {TOKEN}")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            data = r.read()
            return data if raw else (json.loads(data) if data else {})
    except urllib.error.HTTPError as e:
        raise GitHubError(e.code, e.read().decode(errors="replace")[:300]) from None


# ---------------------------------------------------------------- reading
def repo(full):
    return api(f"repos/{full}")


def commits(full, n=100):
    return api(f"repos/{full}/commits", params={"per_page": min(n, 100)})


def open_issues(full, n=100):
    items = api(f"repos/{full}/issues", params={"state": "open", "per_page": n, "sort": "created", "direction": "asc"})
    return [i for i in items if "pull_request" not in i]


def branch_count(full):
    return len(api(f"repos/{full}/branches", params={"per_page": 100}))


def download(full, ref, dest):
    """Fetch the repo as a tarball (works for private repos via the connector) and extract it."""
    data = api(f"repos/{full}/tarball/{ref}", raw=True)
    os.makedirs(dest, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        members = tar.getmembers()
        prefix = members[0].name.split("/")[0] + "/"
        safe = []
        for m in members:
            if not m.name.startswith(prefix) or m.issym() or m.islnk() or ".." in m.name.split("/"):
                continue
            m.name = m.name[len(prefix):]
            if m.name:
                safe.append(m)
        try:
            tar.extractall(dest, members=safe, filter="data")
        except TypeError:  # Python < 3.12
            tar.extractall(dest, members=safe)
    return dest


def me():
    return api("user")


def my_repos():
    return api("user/repos", params={"per_page": 100, "affiliation": "owner,collaborator", "sort": "pushed"})


# ---------------------------------------------------------------- writing
def comment(full, number, body):
    return api(f"repos/{full}/issues/{number}/comments", "POST", {"body": body})


def react(full, number, content="eyes"):
    try:
        api(f"repos/{full}/issues/{number}/reactions", "POST", {"content": content})
    except GitHubError:
        pass


def open_pr(full, base_branch, changes, title, body, branch=None):
    """changes: {path: new_text or None to delete}. Commits via the contents API (no git push needed)."""
    branch = branch or f"roast-fix-{int(time.time())}"
    base_sha = api(f"repos/{full}/git/ref/heads/{base_branch}")["object"]["sha"]
    api(f"repos/{full}/git/refs", "POST", {"ref": f"refs/heads/{branch}", "sha": base_sha})
    for path, text in changes.items():
        qpath = urllib.parse.quote(path)
        sha = None
        try:
            sha = api(f"repos/{full}/contents/{qpath}", params={"ref": branch})["sha"]
        except GitHubError as e:
            if e.status != 404:
                raise
        msg = f"roast fix: {'remove' if text is None else 'update'} {path}"
        if text is None:
            if sha:
                api(f"repos/{full}/contents/{qpath}", "DELETE", {"message": msg, "sha": sha, "branch": branch})
        else:
            payload = {"message": msg, "branch": branch,
                       "content": base64.b64encode(text.encode()).decode()}
            if sha:
                payload["sha"] = sha
            api(f"repos/{full}/contents/{qpath}", "PUT", payload)
    return api(f"repos/{full}/pulls", "POST", {"title": title, "body": body, "head": branch, "base": base_branch})
