"""Fix the single worst fixable offence, prove the fix works, and hand back the changes.

Strategy, cheapest and most reliable first:
  1. Deterministic tools (ruff --fix, gofmt, .gitignore + removing junk)
  2. Claude Code (preinstalled on every Sprite) for real fixes like failing tests
  3. One-shot LLM edit of a single small file (fallback)
"""
import os
import re
import shutil
import subprocess

import llm

FIX_AGENT = os.environ.get("FIX_AGENT", "auto")  # auto | claude | llm | none
MAX_DELETES = 40

GITIGNORE = {
    "python": ["__pycache__/", "*.pyc", ".venv/", "venv/", ".pytest_cache/", "*.egg-info/", "dist/", "build/"],
    "node": ["node_modules/", "npm-debug.log*", "dist/", "coverage/"],
    "go": ["/bin/", "*.test", "*.out"],
    "rust": ["/target/"],
    "common": [".DS_Store", "Thumbs.db", ".env", ".env.*", "*.log", ".idea/", ".vscode/"],
}

# Lower number = try first. Deterministic and high-impact fixes win.
PRIORITY = [("junk-files", 1), ("no-gitignore", 2), ("readme-ghost", 3), ("python-lint", 4), ("go-fmt", 5),
            ("tests-fail", 6), ("setup-broken", 7), ("node-lint", 8), ("tiny-readme", 9), ("no-readme", 10)]


class FixResult:
    def __init__(self, finding, changes, summary, verified, how):
        self.finding, self.changes, self.summary, self.verified, self.how = finding, changes, summary, verified, how


def pick(findings):
    cands = []
    for f in findings:
        if not f.get("fixable"):
            continue
        for key, rank in PRIORITY:
            if key in f["id"]:
                cands.append((rank, -f["severity"], f))
                break
    return [f for _, _, f in sorted(cands, key=lambda x: (x[0], x[1]))]


def fix(insp, log=print):
    """Try candidates in order until one fix is made and verified."""
    for f in pick(insp.findings)[:3]:
        log(f"Trying to fix: {f['title']} ({f['id']})")
        try:
            result = _attempt(insp, f, log)
        except Exception as e:
            log(f"  fix attempt crashed: {e}")
            result = None
        if result and result.changes and result.verified:
            return result
        _reset(insp.root)
        log("  no verified fix, trying next")
    return None


# ------------------------------------------------------------------ attempts
def _attempt(insp, f, log):
    fid = f["id"]
    if fid in ("junk-files", "no-gitignore"):
        return _fix_gitignore(insp, f)
    if fid == "python-lint":
        before = f.get("count", 0)
        subprocess.run(f"{f['ruff']} check --fix --no-cache .", cwd=insp.root, shell=True, capture_output=True,
                       env={**os.environ, **insp.envs.get("python", {})})
        after = _ruff_count(insp, f)
        return FixResult(f, _changes(insp.root), f"Ran `ruff check --fix`: lint problems went from {before} to {after}.",
                         after < before, "ruff")
    if fid == "go-fmt":
        subprocess.run(["gofmt", "-w", *f.get("files", [])], cwd=insp.root, capture_output=True)
        left = subprocess.run("gofmt -l .", cwd=insp.root, shell=True, capture_output=True, text=True).stdout.split()
        return FixResult(f, _changes(insp.root), f"Ran `gofmt -w` on {len(f.get('files', []))} files.", not left, "gofmt")

    # Everything else needs a brain
    agent = _agent()
    if agent == "none":
        return None
    if agent == "claude":
        _claude_fix(insp, f, log)
        how = "Claude Code"
    else:
        if not _llm_fix(insp, f):
            return None
        how = "LLM edit"
    ok = _verify(insp, f)
    changes = _changes(insp.root)
    return FixResult(f, changes, f"{how} fixed **{f['title']}**" + (" and I re-ran the check to prove it." if ok else "."),
                     ok and bool(changes), how)


def _fix_gitignore(insp, f):
    root = insp.root
    path = os.path.join(root, ".gitignore")
    existing = open(path).read().splitlines() if os.path.exists(path) else []
    langs = ["common"]
    if os.path.exists(os.path.join(root, "package.json")):
        langs.append("node")
    if any(os.path.exists(os.path.join(root, p)) for p in ("pyproject.toml", "requirements.txt", "setup.py")):
        langs.append("python")
    if os.path.exists(os.path.join(root, "go.mod")):
        langs.append("go")
    if os.path.exists(os.path.join(root, "Cargo.toml")):
        langs.append("rust")
    add = [p for l in langs for p in GITIGNORE[l] if p not in existing]
    if add:
        with open(path, "a") as fh:
            if existing and existing[-1].strip():
                fh.write("\n")
            fh.write("# Added by Roast My Repo\n" + "\n".join(add) + "\n")
    # Remove committed junk (if it's a manageable number of files)
    junk = [j for j in f.get("files", [])]
    to_delete = []
    for j in junk:
        p = os.path.join(root, j.rstrip("/"))
        if os.path.isdir(p):
            for dp, _, fns in os.walk(p):
                to_delete += [os.path.join(dp, n) for n in fns]
        elif os.path.exists(p):
            to_delete.append(p)
    removed = 0
    if 0 < len(to_delete) <= MAX_DELETES:
        for p in to_delete:
            os.remove(p)
        removed = len(to_delete)
    summary = f"Added {len(add)} patterns to `.gitignore`"
    summary += f" and removed {removed} committed junk files." if removed else "."
    if any(j.split("/")[-1] == ".env" for j in junk):
        summary += " ⚠️ A `.env` was committed: removing it doesn't erase it from git history. Rotate any secrets in it."
    return FixResult(f, _changes(root), summary, bool(add or removed), "deterministic")


def _claude_fix(insp, f, log):
    prompt = (
        f"You are fixing one specific problem in this repository, found by an automated review.\n"
        f"PROBLEM: {f['title']}\nEVIDENCE:\n{f['evidence']}\n"
        + (f"FILE: {f['path']} line {f['line']}\n" if f.get("path") else "")
        + (f"VERIFY BY RUNNING: {f['verify']}\n" if f.get("verify") else "")
        + "Make the smallest correct change that fixes it. Don't refactor anything else, don't add "
          "dependencies unless the fix requires it, don't touch unrelated files. "
          "Do not run git commands. When done, reply with one sentence describing the fix."
    )
    log("  handing it to Claude Code...")
    p = subprocess.run(["claude", "-p", prompt, "--permission-mode", "acceptEdits", "--max-turns", "25"],
                       cwd=insp.root, capture_output=True, text=True, timeout=900)
    log("  Claude Code: " + (p.stdout.strip().splitlines() or ["(no output)"])[-1][:200])


def _llm_fix(insp, f):
    """One-shot: rewrite a single small file."""
    path = f.get("path")
    if not path:
        return False
    full = os.path.join(insp.root, path)
    if not os.path.isfile(full):
        return False
    text = open(full, errors="replace").read()
    if text.count("\n") > 400:
        return False
    extra = ""
    pkg = os.path.join(insp.root, "package.json")
    if "readme" in f["id"] and os.path.exists(pkg):
        extra = "\npackage.json:\n" + open(pkg).read()[:3000]
    reply = llm.chat(
        "You fix one problem in one file. Reply with ONLY the complete corrected file inside a single "
        "``` fenced block. Change as little as possible.",
        f"PROBLEM: {f['title']}\nEVIDENCE: {f['evidence']}\nLINE: {f.get('line')}\n{extra}\n\nFILE {path}:\n```\n{text}\n```",
        max_tokens=8000)
    m = re.search(r"```[^\n]*\n(.*)```", reply, re.S)
    if not m or m.group(1).strip() == text.strip():
        return False
    open(full, "w").write(m.group(1))
    return True


# ------------------------------------------------------------------ helpers
def _agent():
    if FIX_AGENT != "auto":
        return FIX_AGENT
    return "claude" if shutil.which("claude") else "llm"


def _verify(insp, f):
    """Re-run whatever check found the problem."""
    if f.get("verify"):
        code, _, _ = insp.run(f["verify"], 600, insp.envs.get(f.get("lang"), {}))
        return code == 0
    if f["id"].startswith("readme-ghost"):
        # Re-run the README check on the modified tree
        old = insp.findings
        insp.findings = []
        insp.check_readme()
        still = any(x["id"] == f["id"] for x in insp.findings)
        insp.findings = old
        return not still
    return True  # README rewrites etc: nothing mechanical to check


def _ruff_count(insp, f):
    import json
    p = subprocess.run(f"{f['ruff']} check --output-format=json --no-cache .", cwd=insp.root, shell=True,
                       capture_output=True, text=True, env={**os.environ, **insp.envs.get("python", {})})
    try:
        return len(json.loads(p.stdout or "[]"))
    except ValueError:
        return f.get("count", 0)


def _changes(root):
    """{path: new_text or None for deleted}, read from git against the baseline."""
    out = subprocess.run(["git", "status", "--porcelain", "-uall"], cwd=root, capture_output=True, text=True).stdout
    changes = {}
    for line in out.splitlines():
        status, path = line[:2], line[3:].strip().strip('"')
        if " -> " in path:
            path = path.split(" -> ")[1]
        full = os.path.join(root, path)
        if "D" in status or not os.path.exists(full):
            changes[path] = None
        else:
            try:
                changes[path] = open(full, encoding="utf-8").read()
            except UnicodeDecodeError:
                continue  # skip binaries
    return changes


def _reset(root):
    subprocess.run(["git", "checkout", "-q", "--", "."], cwd=root, capture_output=True)
    subprocess.run(["git", "clean", "-fdq"], cwd=root, capture_output=True)
