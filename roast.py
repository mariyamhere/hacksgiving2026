#!/usr/bin/env python3
"""Roast My Repo: an agent that actually runs your code, then roasts you with receipts.

  python3 roast.py owner/repo            roast once, print it, save roast-owner-repo.md
  python3 roast.py owner/repo --run      also install/test/lint the code (do this on a Sprite)
  python3 roast.py owner/repo --run --pr also fix the worst offence and open a PR (needs push access)
  python3 roast.py serve                 watch your repos for issues titled "roast me" and reply
"""
import argparse
import json
import os
import shutil
import sys
import tempfile
import time

import checks
import fixer
import github
import roaster

STATE = os.environ.get("ROAST_STATE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json"))
POLL = int(os.environ.get("POLL_SECONDS", "60"))


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


def can_push(meta):
    return bool(meta.get("permissions", {}).get("push"))


def roast_repo(full, run=False, pr=False, keep=False):
    """Full pipeline. Returns (markdown, pr_url or None)."""
    workdir = tempfile.mkdtemp(prefix="roast-")
    try:
        insp = checks.Inspection(full, workdir, allow_exec=run, log=log)
        # Executing code from a repo you don't control is how you get owned. Only do it on request.
        insp.inspect()
        log(f"{len(insp.findings)} findings: " + ", ".join(f["id"] for f in insp.findings))
        log("Writing the roast...")
        md = roaster.roast(insp)

        pr_url = None
        if pr:
            if not can_push(insp.meta):
                log("No push access to this repo, so no PR. (I don't spam strangers.)")
            else:
                result = fixer.fix(insp, log=log)
                if result:
                    body = (f"### 🔥 Roast My Repo fixed something\n\n"
                            f"**Problem:** {result.finding['title']}\n\n{result.finding['evidence'][:800]}\n\n"
                            f"**Fix:** {result.summary}\n\n"
                            f"**Verified:** {'yes, I re-ran the check ✅' if result.verified else 'no'}  \n"
                            f"**How:** {result.how}\n\n"
                            f"Files: " + ", ".join(f"`{p}`" for p in list(result.changes)[:15]) +
                            "\n\n<sub>You're welcome. Generated on a Fly.io Sprite.</sub>")
                    p = github.open_pr(full, insp.ref, result.changes,
                                       f"🔥 Roast fix: {result.finding['title']}", body)
                    pr_url = p["html_url"]
                    md += f"\n\n---\n**I fixed the worst one for you:** {pr_url}. {result.summary}"
                    log(f"Opened {pr_url}")
                else:
                    md += "\n\n---\n_I tried to fix something myself and failed. We're both going through a lot._"
        return md, pr_url
    finally:
        if keep:
            log(f"Kept workdir: {workdir}")
        else:
            shutil.rmtree(workdir, ignore_errors=True)


# ---------------------------------------------------------------- serve mode
def load_state():
    try:
        return json.load(open(STATE))
    except (OSError, ValueError):
        return {"done": []}


def save_state(s):
    json.dump(s, open(STATE, "w"))


def watched_repos(cache={"at": 0, "repos": []}):
    if os.environ.get("ROAST_REPOS"):
        return [r.strip() for r in os.environ["ROAST_REPOS"].split(",") if r.strip()]
    if time.time() - cache["at"] > 600:
        cache["repos"] = [r["full_name"] for r in github.my_repos() if can_push(r) and not r.get("archived")]
        cache["at"] = time.time()
    return cache["repos"]


def serve(run):
    state = load_state()
    owner = github.me()["login"]
    log(f"Roast My Repo is open for business as @{owner}. Open an issue titled 'roast me' on any watched repo.")
    while True:
        try:
            for full in watched_repos():
                for issue in github.open_issues(full, 30):
                    key = f"{full}#{issue['number']}"
                    if key in state["done"] or "roast me" not in issue["title"].lower():
                        continue
                    log(f"Roast requested: {key} by @{issue['user']['login']}")
                    github.react(full, issue["number"], "eyes")
                    state["done"].append(key)
                    save_state(state)
                    try:
                        md, _ = roast_repo(full, run=run, pr=run)
                    except Exception as e:
                        md = f"I tried to roast this repo but it roasted me first: `{str(e)[:200]}`"
                        log(f"Failed: {e}")
                    github.comment(full, issue["number"], md)
                    github.react(full, issue["number"], "rocket")
        except Exception as e:
            log(f"error: {e}")
        time.sleep(POLL)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("repo", help="owner/repo, or 'serve'")
    ap.add_argument("--run", action="store_true", help="install, test and lint the code (use a Sprite!)")
    ap.add_argument("--pr", action="store_true", help="fix the worst offence and open a PR (needs push access)")
    ap.add_argument("--post", type=int, metavar="ISSUE", help="post the roast as a comment on this issue number")
    ap.add_argument("--keep", action="store_true", help="keep the working directory for debugging")
    a = ap.parse_args()

    if a.repo == "serve":
        return serve(run=a.run)
    full = a.repo.replace("https://github.com/", "").strip("/")
    md, _ = roast_repo(full, run=a.run, pr=a.pr, keep=a.keep)
    out = f"roast-{full.replace('/', '-')}.md"
    open(out, "w").write(md)
    print(md)
    log(f"Saved {out}")
    if a.post:
        github.comment(full, a.post, md)
        log(f"Posted to {full}#{a.post}")


if __name__ == "__main__":
    main()
