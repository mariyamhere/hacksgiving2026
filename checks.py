"""Inspect a repo and produce evidence. No AI in here: every finding is a fact.

A finding looks like:
  {"id": "todo-count", "severity": 1-3, "title": "...", "evidence": "...",
   "path": "src/x.py", "line": 12, "url": "https://github.com/...#L12", "fixable": True}
"""
import json
import os
import re
import shutil
import subprocess
import time
from datetime import datetime, timezone

import github

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", ".roastenv", "target", "dist", "build", "__pycache__", "vendor"}
JUNK = {".DS_Store", "Thumbs.db", ".env", "npm-debug.log"}
JUNK_DIRS = {"node_modules", "__pycache__", ".pytest_cache", ".idea"}
LAZY_MSG = re.compile(r"^(fix(es|ed)?|wip|update[sd]?|changes?|asdf+|test(ing)?|stuff|\.+|final.*|minor|temp|oops|idk|lol|commit|misc)\W*$", re.I)
CODE_EXT = {".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".rb", ".java", ".kt", ".c", ".cpp", ".h", ".cs", ".php", ".swift"}


class Inspection:
    def __init__(self, full, workdir, allow_exec=False, log=print):
        self.full = full
        self.workdir = workdir
        self.allow_exec = allow_exec
        self.log = log
        self.findings = []
        self.stats = {}
        self.meta = github.repo(full)
        self.ref = self.meta["default_branch"]
        self.sha = github.api(f"repos/{full}/commits/{self.ref}")["sha"]
        self.root = os.path.join(workdir, "src")
        self.envs = {}  # per-language env (e.g. python venv) so the fixer can re-run checks

    # ------------------------------------------------------------ helpers
    def url(self, path, line=None):
        u = f"https://github.com/{self.full}/blob/{self.sha}/{path}"
        return f"{u}#L{line}" if line else u

    def add(self, id, severity, title, evidence, path=None, line=None, fixable=False, **extra):
        self.findings.append({"id": id, "severity": severity, "title": title, "evidence": evidence,
                              "path": path, "line": line, "url": self.url(path, line) if path else None,
                              "fixable": fixable, **extra})

    def files(self):
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for f in filenames:
                full = os.path.join(dirpath, f)
                yield os.path.relpath(full, self.root), full

    def run(self, cmd, timeout=300, env=None):
        start = time.time()
        try:
            p = subprocess.run(cmd, cwd=self.root, shell=True, capture_output=True, text=True,
                               timeout=timeout, env={**_clean_env(), **(env or {})})
            return p.returncode, (p.stdout + p.stderr)[-6000:], time.time() - start
        except subprocess.TimeoutExpired:
            return 124, f"timed out after {timeout}s", time.time() - start

    # ------------------------------------------------------------ main
    def inspect(self):
        self.log(f"Downloading {self.full}@{self.ref}...")
        github.download(self.full, self.sha, self.root)
        _git_baseline(self.root)
        self.check_hygiene()
        self.check_readme()
        self.check_code()
        self.check_history()
        if self.allow_exec:
            self.check_execution()
        else:
            self.log("Skipping install/test/lint (pass --run to execute this repo's code).")
        self.findings.sort(key=lambda f: -f["severity"])
        return self

    # ------------------------------------------------------------ static checks
    def check_hygiene(self):
        names = set(os.listdir(self.root))
        lower = {n.lower() for n in names}
        if not any(n.startswith("readme") for n in lower):
            self.add("no-readme", 3, "No README", "There is no README at all.", fixable=True)
        if not any(n.startswith(("license", "licence", "copying")) for n in lower):
            self.add("no-license", 1, "No license", "No LICENSE file, so legally nobody can use this.")
        if ".gitignore" not in names:
            self.add("no-gitignore", 2, "No .gitignore", "No .gitignore in the repo root.", fixable=True)
        if not os.path.isdir(os.path.join(self.root, ".github", "workflows")):
            self.add("no-ci", 1, "No CI", "No GitHub Actions workflows. Tests run on vibes.")
        test_files = [p for p, _ in self.files() if re.search(r"(^|/)(tests?|spec|__tests__)/|(_test|\.test|\.spec|test_)[^/]*$|(^|/)tests?\.[a-z]+$", p)]
        self.stats["test_files"] = len(test_files)
        if not test_files:
            self.add("no-tests", 2, "No tests", "Could not find a single test file.")

        junk, big = [], []
        for rel, full in self.files():
            parts = rel.split("/")
            if parts[-1] in JUNK or parts[-1].endswith((".pyc", ".log")):
                junk.append(rel)
            try:
                size = os.path.getsize(full)
            except OSError:
                continue
            if size > 1_000_000:
                big.append((rel, size))
        for d, _, _ in os.walk(self.root):
            rel = os.path.relpath(d, self.root)
            if os.path.basename(d) in JUNK_DIRS:
                junk.append(rel + "/")
        if junk:
            secret = [j for j in junk if j.split("/")[-1] == ".env"]
            self.add("junk-files", 3 if secret else 2, "Committed junk",
                     f"{len(junk)} files that should never be committed: " + ", ".join(junk[:8]) +
                     (" (including a .env file, which may contain secrets: rotate them!)" if secret else ""),
                     path=junk[0].rstrip("/"), fixable=True, files=junk)
        if big:
            big.sort(key=lambda x: -x[1])
            self.add("big-files", 1, "Huge files", ", ".join(f"{p} ({s // 1_000_000} MB)" for p, s in big[:5]),
                     path=big[0][0])

    def check_readme(self):
        readme = next((n for n in os.listdir(self.root) if n.lower().startswith("readme")), None)
        if not readme:
            return
        text = open(os.path.join(self.root, readme), errors="replace").read()
        words = len(text.split())
        self.stats["readme_words"] = words
        if words < 40:
            self.add("tiny-readme", 2, "README is a haiku", f"README is {words} words long.", path=readme, fixable=True)
        # Commands in the README that point at scripts/targets that don't exist
        pkg = self._json("package.json")
        scripts = set((pkg or {}).get("scripts", {}))
        makefile = os.path.join(self.root, "Makefile")
        targets = set(re.findall(r"^([A-Za-z0-9_.-]+):", open(makefile).read(), re.M)) if os.path.exists(makefile) else None
        in_fence = False
        for n, raw_line in enumerate(text.splitlines(), 1):
            if raw_line.strip().startswith(("```", "~~~")):
                in_fence = not in_fence
                continue
            # Only look at code: fenced blocks, indented blocks, or `inline code`. Prose like "make this easy" is fine.
            if in_fence or raw_line.startswith(("    ", "\t", "$ ")):
                line = raw_line
            else:
                line = " ; ".join(re.findall(r"`([^`]+)`", raw_line))
            if not line:
                continue
            for m in re.finditer(r"\b(?:npm run|yarn|pnpm run?)\s+([a-z0-9:_-]+)", line):
                s = m.group(1)
                if pkg is not None and s not in scripts and s not in {"install", "add", "i", "dlx", "create"}:
                    self.add(f"readme-ghost-script-{s}", 3, "README lies",
                             f'README says to run `{m.group(0)}` but package.json has no "{s}" script.',
                             path=readme, line=n, fixable=True)
            for m in re.finditer(r"\bmake\s+([a-z0-9_-]+)", line):
                if targets is not None and m.group(1) not in targets:
                    self.add(f"readme-ghost-make-{m.group(1)}", 3, "README lies",
                             f"README says `make {m.group(1)}` but the Makefile has no such target.",
                             path=readme, line=n, fixable=True)
                elif targets is None and line.strip().lstrip("$ ").startswith("make "):
                    self.add("readme-ghost-makefile", 3, "README lies",
                             f"README says `make {m.group(1)}` but there is no Makefile.", path=readme, line=n)
            for m in re.finditer(r"\bpip install -r\s+(\S+)", line):
                if not os.path.exists(os.path.join(self.root, m.group(1))):
                    self.add("readme-ghost-reqs", 3, "README lies",
                             f"README says `{m.group(0)}` but {m.group(1)} doesn't exist.", path=readme, line=n, fixable=True)

    def check_code(self):
        todos, longest, total_lines, code_files = [], (None, 0), 0, 0
        for rel, full in self.files():
            if os.path.splitext(rel)[1] not in CODE_EXT:
                continue
            code_files += 1
            try:
                lines = open(full, errors="replace").read().splitlines()
            except OSError:
                continue
            total_lines += len(lines)
            if len(lines) > longest[1]:
                longest = (rel, len(lines))
            for i, l in enumerate(lines, 1):
                m = re.search(r"\b(TODO|FIXME|HACK|XXX)\b[:\s]*(.{0,80})", l)
                if m:
                    todos.append((rel, i, m.group(1), m.group(2).strip()))
        self.stats.update(code_files=code_files, code_lines=total_lines)
        if len(todos) >= 5:
            rel, i, kind, txt = todos[0]
            self.add("todo-count", 2 if len(todos) > 20 else 1, "TODO graveyard",
                     f"{len(todos)} TODO/FIXME/HACK comments. First one: {kind}: \"{txt}\"",
                     path=rel, line=i, examples=[f"{r}:{n} {k}: {t}" for r, n, k, t in todos[:5]])
        if longest[1] > 1000:
            self.add("god-file", 2, "God file", f"{longest[0]} is {longest[1]} lines long.", path=longest[0])

    def check_history(self):
        now = datetime.now(timezone.utc)
        try:
            cs = github.commits(self.full, 100)
        except github.GitHubError:
            cs = []
        msgs = [c["commit"]["message"].split("\n")[0].strip() for c in cs]
        lazy = [m for m in msgs if LAZY_MSG.match(m) or len(m) <= 3]
        self.stats["commits_sampled"] = len(msgs)
        if msgs and len(lazy) >= max(3, len(msgs) * 0.15):
            common = max(set(lazy), key=lazy.count)
            self.add("lazy-commits", 2, "Commit messages from the void",
                     f'{len(lazy)} of the last {len(msgs)} commit messages are things like "{common}" '
                     f"(used {lazy.count(common)} times).", examples=lazy[:8])
        if cs:
            last = datetime.fromisoformat(cs[0]["commit"]["committer"]["date"].replace("Z", "+00:00"))
            days = (now - last).days
            self.stats["days_since_commit"] = days
            if days > 365:
                self.add("abandoned", 2, "Abandoned", f"Last commit was {days} days ago.")
        try:
            issues = github.open_issues(self.full, 100)
        except github.GitHubError:
            issues = []
        ignored = []
        for i in issues:
            age = (now - datetime.fromisoformat(i["created_at"].replace("Z", "+00:00"))).days
            if i["comments"] == 0 and age > 60:
                ignored.append((age, i))
        if ignored:
            age, i = max(ignored, key=lambda x: x[0])
            self.add("ghosted-issues", 2, "Ghosting your users",
                     f'{len(ignored)} open issues with zero replies for 60+ days. Oldest: #{i["number"]} '
                     f'"{i["title"][:70]}" ({age} days, not a single reply).', issue_url=i["html_url"])

    # ------------------------------------------------------------ execution (sandboxed by the Sprite)
    def check_execution(self):
        r = self.root
        if os.path.exists(os.path.join(r, "package.json")) and shutil.which("npm"):
            self._node()
        if any(os.path.exists(os.path.join(r, f)) for f in ("pyproject.toml", "requirements.txt", "setup.py")):
            self._python()
        if os.path.exists(os.path.join(r, "go.mod")) and shutil.which("go"):
            self._go()
        if os.path.exists(os.path.join(r, "Cargo.toml")) and shutil.which("cargo"):
            self._rust()

    def _setup_result(self, label, code, out, secs, cmd):
        self.stats[f"{label}_setup_seconds"] = round(secs)
        if code != 0:
            self.add(f"{label}-setup-broken", 3, "Setup is broken",
                     f"`{cmd}` failed after {secs:.0f}s. Last lines:\n{_tail(out)}", fixable=True, log=out,
                     verify=cmd, lang=label)
            return False
        if secs > 180:
            self.add(f"{label}-setup-slow", 1, "Setup takes forever", f"`{cmd}` took {secs:.0f}s.")
        return True

    def _tests_result(self, label, code, out, secs, cmd, run_cmd=None):
        if code == 124:
            self.add(f"{label}-tests-hang", 3, "Tests hang", f"`{cmd}` timed out after {secs:.0f}s.")
        elif code != 0:
            self.add(f"{label}-tests-fail", 3, "Tests fail", f"`{cmd}` exited {code}. Last lines:\n{_tail(out)}",
                     fixable=True, log=out, verify=run_cmd or cmd, lang=label)
        else:
            self.stats[f"{label}_tests"] = "pass"

    def _node(self):
        self.log("Node: installing...")
        cmd = "npm ci --ignore-scripts" if os.path.exists(os.path.join(self.root, "package-lock.json")) else "npm install --ignore-scripts"
        if not self._setup_result("node", *self.run(cmd, 600), cmd):
            return
        scripts = (self._json("package.json") or {}).get("scripts", {})
        test = scripts.get("test", "")
        if test and "no test specified" not in test:
            self.log("Node: testing...")
            self.envs["node"] = {"CI": "1"}
            self._tests_result("node", *self.run("npm test --silent", 600, {"CI": "1"}), "npm test", "npm test --silent")
        if "lint" in scripts:
            self.log("Node: linting...")
            code, out, _ = self.run("npm run lint --silent", 300)
            if code != 0:
                self.add("node-lint", 2, "Lint fails", f"`npm run lint` fails. Last lines:\n{_tail(out)}", fixable=True, log=out)

    def _python(self):
        self.log("Python: creating venv and installing...")
        r = self.root
        if os.path.exists(os.path.join(r, "requirements.txt")):
            cmd = "pip install -r requirements.txt"
        else:
            cmd = "pip install -e ."
        venv = os.path.join(self.workdir, "venv")
        subprocess.run(["python3", "-m", "venv", venv], capture_output=True)
        env = {"PATH": f"{venv}/bin:" + os.environ.get("PATH", ""), "VIRTUAL_ENV": venv}
        self.envs["python"] = env
        if not self._setup_result("python", *self.run(cmd + " -q", 600, env), cmd):
            return
        if self.stats.get("test_files"):
            self.log("Python: testing...")
            self.run("pip install -q pytest", 300, env)
            pytest = "python -m pytest -q -x --no-header -p no:cacheprovider"
            self._tests_result("python", *self.run(pytest, 600, env), "pytest", pytest)
        ruff = shutil.which("ruff") or (self.run("pip install -q ruff", 300, env)[0] == 0 and f"{venv}/bin/ruff")
        if ruff:
            self.log("Python: linting with ruff...")
            code, out, _ = self.run(f"{ruff} check --output-format=json --no-cache .", 300, env)
            try:
                issues = json.loads(out[out.index("["):]) if "[" in out else []
            except ValueError:
                issues = []
            if issues:
                first = issues[0]
                path = os.path.relpath(first["filename"], self.root)
                fixable = sum(1 for i in issues if i.get("fix"))
                codes = {}
                for i in issues:
                    codes[i["code"]] = codes.get(i["code"], 0) + 1
                top = sorted(codes.items(), key=lambda x: -x[1])[:4]
                self.add("python-lint", 2 if len(issues) > 20 else 1, "Linter screaming",
                         f"ruff found {len(issues)} problems ({fixable} auto-fixable). Top: " +
                         ", ".join(f"{c} x{n}" for c, n in top) + f". First: {first['message']}",
                         path=path, line=first["location"]["row"], fixable=fixable > 0, tool="ruff",
                         count=len(issues), ruff=ruff, lang="python")

    def _go(self):
        self.log("Go: building...")
        if not self._setup_result("go", *self.run("go build ./...", 600), "go build ./..."):
            return
        code, out, _ = self.run("go vet ./...", 300)
        m = re.search(r"^(?:\./)?([^\s:]+\.go):(\d+)", out, re.M)
        if code != 0 and m:
            self.add("go-vet", 2, "go vet complaints", _tail(out, 4), path=m.group(1), line=int(m.group(2)))
        unformatted = self.run("gofmt -l .", 120)[1].split()
        if unformatted:
            self.add("go-fmt", 1, "Not gofmt'd", f"{len(unformatted)} files aren't gofmt'd: " + ", ".join(unformatted[:5]),
                     path=unformatted[0], fixable=True, tool="gofmt", files=unformatted)
        self._tests_result("go", *self.run("go test ./...", 600), "go test ./...")

    def _rust(self):
        self.log("Rust: building (grab a coffee)...")
        if not self._setup_result("rust", *self.run("cargo build -q", 900), "cargo build"):
            return
        self._tests_result("rust", *self.run("cargo test -q", 900), "cargo test")

    def _json(self, name):
        p = os.path.join(self.root, name)
        try:
            return json.load(open(p)) if os.path.exists(p) else None
        except ValueError:
            return None


def _tail(text, n=8):
    return "\n".join(text.strip().splitlines()[-n:])


def _clean_env():
    """Don't hand the repo's code anything secret from our environment."""
    keep = ("PATH", "HOME", "LANG", "TERM", "USER", "TMPDIR", "SSL_CERT_FILE", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY")
    return {k: v for k, v in os.environ.items() if k in keep}


def _git_baseline(root):
    """Turn the download into a git repo so the fixer can see exactly what it changed."""
    env = {**os.environ, "GIT_AUTHOR_NAME": "roast", "GIT_AUTHOR_EMAIL": "roast@localhost",
           "GIT_COMMITTER_NAME": "roast", "GIT_COMMITTER_EMAIL": "roast@localhost"}
    for cmd in (["git", "init", "-q"], ["git", "add", "-A", "-f"], ["git", "commit", "-q", "-m", "baseline", "--no-verify"]):
        subprocess.run(cmd, cwd=root, capture_output=True, env=env)
    # Build/install artifacts created by our own runs must never end up in a PR
    with open(os.path.join(root, ".git", "info", "exclude"), "a") as fh:
        fh.write("\nnode_modules/\n__pycache__/\n*.pyc\n*.egg-info/\n.pytest_cache/\n.ruff_cache/\n/target/\n.mypy_cache/\n")
