#!/usr/bin/env python3
"""Checks against install.py, run against real repositories in a temp dir.

Separate from test_guard.py because it exercises a different thing: that suite asks
what the hook decides, this one asks what the installer leaves on disk. The two
failures it exists for are both silent — a settings file that lost a registration,
and a config file that lost the record of where the guard came from — and neither
shows up as an error at install time. They show up later, as a hook that is not
running or a copy nobody can date.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
INSTALL = HERE / "install.py"
GUARD_SOURCE = HERE / "worktree_guard.py"

# Imported rather than reimplemented: the question these ask is whether the installer
# and its reader agree on what "the same file" means, and a second copy of the rule here
# could only ever agree with itself.
sys.path.insert(0, str(HERE))
from install import SHIM, content_hash  # noqa: E402

PASSED = 0
FAILED: list[str] = []


def check(name: str, got, want) -> None:
    global PASSED
    if got == want:
        PASSED += 1
    else:
        FAILED.append(f"{name}: expected {want!r}, got {got!r}")


def install(repo: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(INSTALL), "--repo", str(repo), "--branch", "development",
         "--no-skill", *extra],
        capture_output=True,
        text=True,
    )


def config_of(repo: Path) -> dict:
    path = repo / ".claude" / "worktree-per-change.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def permissions_of(repo: Path) -> list[str]:
    path = repo / ".claude" / "settings.json"
    if not path.is_file():
        return []
    settings = json.loads(path.read_text(encoding="utf-8"))
    allow = (settings.get("permissions") or {}).get("allow")
    return allow if isinstance(allow, list) else []


def registrations(repo: Path) -> list[str]:
    path = repo / ".claude" / "settings.json"
    if not path.is_file():
        return []
    settings = json.loads(path.read_text(encoding="utf-8"))
    found = []
    for event, matchers in (settings.get("hooks") or {}).items():
        for matcher in matchers:
            for hook in matcher.get("hooks") or []:
                if "worktree-guard.py" in " ".join(str(p) for p in hook.get("args") or []):
                    found.append(event)
    return sorted(found)


def owner_registrations(repo: Path, name: str = "settings.json") -> list[str]:
    path = repo / ".claude" / name
    if not path.is_file():
        return []
    settings = json.loads(path.read_text(encoding="utf-8"))
    found = []
    for event, matchers in (settings.get("hooks") or {}).items():
        for matcher in matchers:
            for hook in matcher.get("hooks") or []:
                if "worktree-owner.py" in " ".join(str(p) for p in hook.get("args") or []):
                    found.append(event)
    return sorted(found)


def hooks_of(repo: Path, script: str = "worktree-guard.py",
             name: str = "settings.json") -> dict[str, list[dict]]:
    """Every registration naming `script`, by event, exactly as the settings file has it."""
    path = repo / ".claude" / name
    settings = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    found: dict[str, list[dict]] = {}
    for event, matchers in (settings.get("hooks") or {}).items():
        for matcher in matchers:
            for hook in matcher.get("hooks") or []:
                if script in " ".join(str(p) for p in hook.get("args") or []):
                    found.setdefault(event, []).append(hook)
    return found


def to_direct(repo: Path) -> None:
    """Rewrite every registration of ours into the shape installers wrote before the shim.

    `python -S <script>` and nothing else, which is what every repository installed before
    it still has, and what a resync has to recognise and replace.
    """
    path = repo / ".claude" / "settings.json"
    settings = json.loads(path.read_text(encoding="utf-8"))
    for matchers in (settings.get("hooks") or {}).values():
        for matcher in matchers:
            for hook in matcher.get("hooks") or []:
                args = [str(arg) for arg in hook.get("args") or []]
                if "-c" in args:
                    hook["args"] = ["-S", args[-1]]
    path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")


def run_hook(hook: dict, project: Path, payload: dict) -> subprocess.CompletedProcess:
    """Run a registration the way Claude Code does: exec form, the project dir substituted.

    No shell, each `args` element one argument exactly as written, and `python` read as the
    interpreter running these tests, because a committed install names `python` and this
    machine's may not answer to it.
    """
    command = sys.executable if hook["command"] == "python" else hook["command"]
    args = [str(arg).replace("${CLAUDE_PROJECT_DIR}", str(project))
            for arg in hook.get("args") or []]
    return subprocess.run([command, *args], input=json.dumps(payload), capture_output=True,
                          text=True, cwd=str(project))


def fresh(root: Path, name: str) -> Path:
    repo = root / name
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    return repo


def git_in(tree: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(tree), *args], capture_output=True, text=True)
    return result.stdout.strip()


def remoted(root: Path, name: str) -> Path:
    """A repository with a bare `origin` holding one commit on `main`, as a real one has.

    `fresh` repositories have no remote, so every install into one is written in place —
    which is the right behaviour there and exactly why it cannot test the default.
    """
    remote = root / f"{name}.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    repo = fresh(root, name)
    for args in (("checkout", "-q", "-b", "main"),
                 ("config", "user.name", "install test"),
                 ("config", "user.email", "install-test@example.invalid"),
                 ("config", "commit.gpgsign", "false")):
        subprocess.run(["git", "-C", str(repo), *args], check=True)
    (repo / "README").write_bytes(b"a repository" + bytes([10]))
    # Captured, not checked for silence: `core.autocrlf` warns about the README on Windows.
    subprocess.run(["git", "-C", str(repo), "add", "README"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "first"], check=True,
                   capture_output=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", str(remote)], check=True)
    subprocess.run(["git", "-C", str(repo), "push", "-q", "-u", "origin", "main"],
                   check=True, capture_output=True)
    return repo


def skill_clone(root: Path, name: str) -> tuple[Path, Path]:
    """A clone carrying this skill's scripts, and a second clone that can move its upstream.

    The installer judges whether it is current by the directory it runs from, so the only
    honest test runs a COPY of it from a checkout whose history the test controls. Returns
    the clone the installer runs from and the one that pushes past it.
    """
    upstream = root / f"{name}.git"
    subprocess.run(["git", "init", "-q", "--bare", str(upstream)], check=True)
    seed = remoted_from(root / f"{name}-seed", upstream)
    scripts = seed / "worktree-per-change" / "scripts"
    scripts.mkdir(parents=True)
    for script in ("install.py", "worktree_guard.py", "worktree_owner.py", "land.py"):
        shutil.copy2(HERE / script, scripts / script)
    for args in (("add", "worktree-per-change"), ("commit", "-q", "-m", "the skill"),
                 ("push", "-q", "-u", "origin", "main")):
        subprocess.run(["git", "-C", str(seed), *args], check=True, capture_output=True)
    clone = root / f"{name}-clone"
    subprocess.run(["git", "clone", "-q", "-b", "main", str(upstream), str(clone)], check=True,
                   capture_output=True)
    return clone, seed


def remoted_from(repo: Path, upstream: Path) -> Path:
    """An empty repository on `main` with `upstream` as its origin, ready to commit."""
    repo.mkdir(parents=True)
    for args in (("init", "-q", str(repo)),
                 ("-C", str(repo), "checkout", "-q", "-b", "main"),
                 ("-C", str(repo), "config", "user.name", "install test"),
                 ("-C", str(repo), "config", "user.email", "install-test@example.invalid"),
                 ("-C", str(repo), "config", "commit.gpgsign", "false"),
                 ("-C", str(repo), "remote", "add", "origin", str(upstream))):
        subprocess.run(["git", *args], check=True, capture_output=True)
    return repo


def checkout_bytes(repo: Path) -> dict:
    """Every file in the checkout by path, leaving out `.git` and the worktrees under it."""
    found = {}
    for path in repo.rglob("*"):
        relative = path.relative_to(repo).as_posix()
        if relative.split("/")[0] == ".git" or relative.startswith(".claude/worktrees/"):
            continue
        if path.is_file():
            found[relative] = path.read_bytes()
    return found


def install_branches(repo: Path) -> list[str]:
    return git_in(repo, "for-each-ref", "--format=%(refname:short)",
                  "refs/heads/install-worktree-per-change*").splitlines()


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)

        # --- a first install ------------------------------------------------------
        repo = fresh(root, "first")
        result = install(repo)
        check("it installs cleanly", result.returncode, 0)
        check("all three events are registered", registrations(repo),
              ["PreToolUse", "SessionStart", "Stop"])
        check("the guard is copied in", (repo / ".claude" / "hooks" / "worktree-guard.py").is_file(), True)
        check("the branch is recorded", config_of(repo).get("integrationBranch"), "development")
        # No `origin`, so nothing to cut a worktree from or land into: written where it was
        # asked for, and saying so rather than leaving the operator to work out which.
        check("a repo with no origin is installed in place, and told so",
              "in place" in result.stdout and "no `origin` remote" in result.stdout, True)

        # --- the provenance of the copy -------------------------------------------
        # A committed guard is a fork the moment the skill moves, and a stale one still
        # denies confidently. The record is what lets a repo's gate ask whether its copy
        # has drifted, so an install that does not write one leaves nothing to check.
        guard = config_of(repo).get("guard") or {}
        check("the copy records where it came from", bool(guard.get("source")), True)
        check(
            "it records the hash of the file it actually wrote",
            guard.get("sha256"),
            content_hash((repo / ".claude" / "hooks" / "worktree-guard.py").read_bytes()),
        )
        # The record crosses platforms, so it cannot be a hash of the bytes on disk. A
        # repo pinning `eol=lf` hands out LF everywhere; one leaving it to `core.autocrlf`
        # hands out CRLF on Windows — so a working-copy hash is true on the machine that
        # installed and false on the Linux runner meant to check it, reporting drift in a
        # file nobody touched.
        body = GUARD_SOURCE.read_bytes().replace(b"\r\n", b"\n")
        check("the hash ignores line endings", content_hash(body), content_hash(body.replace(b"\n", b"\r\n")))
        check(
            "so the record matches the copy however git checked it out",
            guard.get("sha256"),
            content_hash(body.replace(b"\n", b"\r\n")),
        )

        head = subprocess.run(["git", "-C", str(HERE), "rev-parse", "HEAD"],
                              capture_output=True, text=True)
        if head.returncode == 0:
            check("it records the upstream commit", guard.get("syncedFrom"), head.stdout.strip())
        else:  # a tarball rather than a checkout — no commit to name, and saying so is honest
            check("no commit is claimed when there is none", "syncedFrom" in guard, False)

        # --- a resync ------------------------------------------------------------
        # The failure this pair exists for: the config is the repo's file, and a resync is
        # exactly when a replace would drop everything the repo put beside the branch —
        # including the provenance the resync itself is meant to update.
        path = repo / ".claude" / "worktree-per-change.json"
        blob = config_of(repo)
        blob["ticketPrefix"] = "PORT"
        path.write_text(json.dumps(blob, indent=2), encoding="utf-8")
        check("resync succeeds", install(repo).returncode, 0)
        # A repo install must not drop a `.bak` beside a file git already versions. Small
        # mess, real cost: it is untracked, so it turns up in the `git status` of whoever
        # installs or resyncs next, beside the change they are trying to read. Measured
        # 2026-08-15 — this check exists because the rule was written down, applied to the
        # uninstall path, and missed on this one, which is the path everyone takes.
        check("a resync leaves no .bak beside a committed settings.json",
              sorted(p.name for p in (repo / ".claude").glob("*.bak")), [])
        check("a key the repo added survives it", config_of(repo).get("ticketPrefix"), "PORT")
        check("the provenance survives it", bool((config_of(repo).get("guard") or {}).get("sha256")), True)
        check("it does not double-register", registrations(repo),
              ["PreToolUse", "SessionStart", "Stop"])

        # The record only earns its keep if editing the copy breaks it — that is the whole
        # of what an offline gate can detect, and the copy is not the repo's to edit.
        installed = repo / ".claude" / "hooks" / "worktree-guard.py"
        installed.write_bytes(b"# edited in place\n")
        check(
            "an edit in place stops matching the record",
            config_of(repo)["guard"]["sha256"] == content_hash(installed.read_bytes()),
            False,
        )
        # ...and a resync is what puts the two back in agreement, on the new file.
        install(repo)
        check("a resync restores the copy and re-records it",
              config_of(repo)["guard"]["sha256"],
              content_hash(GUARD_SOURCE.read_bytes()))
        check("which is the file now on disk",
              content_hash(installed.read_bytes()),
              content_hash(GUARD_SOURCE.read_bytes()))

        # --- dry run writes nothing ----------------------------------------------
        untouched = fresh(root, "dry")
        out = install(untouched, "--dry-run")
        check("dry run exits clean", out.returncode, 0)
        check("dry run shows the record it would write", "syncedFrom" in out.stdout or
              "guard" in out.stdout, True)
        check("dry run writes no config", (untouched / ".claude" / "worktree-per-change.json").exists(), False)
        check("dry run writes no guard", (untouched / ".claude" / "hooks").exists(), False)

        # --- uninstall -------------------------------------------------------------
        gone = fresh(root, "gone")
        install(gone)
        check("uninstall exits clean", install(gone, "--uninstall").returncode, 0)
        check("uninstall clears the registrations", registrations(gone), [])
        check("uninstall removes the guard", (gone / ".claude" / "hooks" / "worktree-guard.py").exists(), False)
        check("uninstall removes the config", (gone / ".claude" / "worktree-per-change.json").exists(), False)

        # --- the integration branch is asked for, never assumed --------------------
        # The setting that is silently wrong. A guard installed against the wrong branch
        # denies nothing and breaks nothing; it just aims every future PR at a branch
        # nobody merges. So an install with no answer available has to stop, and the one
        # thing it must never do is pick.
        asked = fresh(root, "asked")
        blind = subprocess.run(
            [sys.executable, str(INSTALL), "--repo", str(asked), "--no-skill"],
            capture_output=True, text=True, stdin=subprocess.DEVNULL,
        )
        check("an install with no answer refuses", blind.returncode, 2)
        check("and says how to answer it", "--branch" in blind.stderr, True)
        check("and installs nothing", (asked / ".claude" / "settings.json").exists(), False)
        # An answer down a pipe is an answer. `isatty()` is false there, and under Git Bash
        # on Windows it is *true* for stdin redirected from /dev/null — so what decides is
        # whether a line arrives, not what stdin claims to be.
        piped = subprocess.run(
            [sys.executable, str(INSTALL), "--repo", str(asked), "--no-skill"],
            capture_output=True, text=True, input="queue\n",
        )
        check("an answer down a pipe is taken", piped.returncode, 0)
        check("and is what gets recorded", config_of(asked).get("integrationBranch"), "queue")
        # ...and having answered once, the repository is not asked again. The commonest run
        # of this installer is a RESYNC, and re-asking there is how a recorded decision gets
        # overwritten: the list offered names the *default* branch as the obvious one, and
        # for every repository this protocol is built for that is the wrong answer. Both
        # known consumers integrate through something other than their default branch.
        resync = subprocess.run(
            [sys.executable, str(INSTALL), "--repo", str(asked), "--no-skill"],
            capture_output=True, text=True, stdin=subprocess.DEVNULL,
        )
        check("a resync with no --branch does not have to ask", resync.returncode, 0)
        check("and keeps the branch the repo recorded",
              config_of(asked).get("integrationBranch"), "queue")
        check("a repo that changes its mind still says so with --branch",
              config_of(asked) is not None
              and install(asked, "--branch", "trunk").returncode == 0
              and config_of(asked).get("integrationBranch"),
              "trunk")

        # --- a resync keeps every decision the repository made by hand -------------
        # The keys below are hand edits by design: the repo that needs them has already
        # written its protocol down somewhere. That only holds if a resync leaves them
        # alone — an installer that dropped them would make its own defaults the answer
        # to a question the repository had already settled.
        opinionated = fresh(root, "opinionated")
        install(opinionated)
        blob = config_of(opinionated)
        blob["delivery"] = {"command": "pnpm feature land", "enterWorktree": False}
        blob["protectedMergeTargets"] = ["main"]
        blob["mergeIntegrationBeforeLanding"] = True
        (opinionated / ".claude" / "worktree-per-change.json").write_text(
            json.dumps(blob, indent=2), encoding="utf-8")
        install(opinionated)
        kept = config_of(opinionated)
        check("a resync keeps a hand-written delivery block",
              (kept.get("delivery") or {}).get("command"), "pnpm feature land")
        check("and the branches the repo will not merge into",
              kept.get("protectedMergeTargets"), ["main"])
        check("and its answer about merging the base down first",
              kept.get("mergeIntegrationBeforeLanding"), True)
        # And it says so, because a mechanism that exists is not the same as this
        # repository having used it, and the gap is silent everywhere else.
        told = subprocess.run(
            [sys.executable, str(INSTALL), "--repo", str(opinionated), "--status"],
            capture_output=True, text=True, cwd=str(opinionated),
        ).stdout
        check("--status names the repo's own delivery command",
              "pnpm feature land" in told, True)
        check("and its protected merge targets", "protected merge targets" in told, True)
        plain = subprocess.run(
            [sys.executable, str(INSTALL), "--repo", str(asked), "--status"],
            capture_output=True, text=True, cwd=str(asked),
        ).stdout
        check("a repo that declared nothing is told that, not left to wonder",
              "no other declarations" in plain, True)

        # --- the allowlist ---------------------------------------------------------
        # Two different denials stop this protocol, and only one of them is the guard. A
        # permission layer that stops `git status` teaches an agent to stop asking, and one
        # that stops `gh pr merge` leaves the change on the disk it was made on.
        allowed = permissions_of(repo)
        # A rule is `Bash(<command>)`, tool name and all. A bare `git status:*` in the
        # allow list is not a narrower rule, it is one that matches nothing — and it fails
        # silently, because the file looks right and every command it covers is still
        # stopped. So the spelling is asserted, not just the contents.
        check("read-only git is allowed", "Bash(git status:*)" in allowed, True)
        check("read-only gh is allowed", "Bash(gh pr view:*)" in allowed, True)
        check("the lander is allowed",
              "Bash(python .claude/scripts/land.py:*)" in allowed, True)
        check("every entry is a Bash rule",
              all(r.startswith("Bash(") and r.endswith(")") for r in allowed), True)

        # The entries are prefix matches on the whole command, so a bare verb is a much
        # larger grant than it looks. These three are the ones that would give away the
        # protocol itself, `git config`, and every write the token can reach.
        check("`git branch` is not allowed bare", "Bash(git branch:*)" in allowed, False)
        check("`git config` is not allowed bare", "Bash(git config:*)" in allowed, False)
        check("`gh api` is not allowed at all",
              any(r.startswith("Bash(gh api") for r in allowed), False)
        # ...and the one that would make the narrow lander pointless.
        check("`gh pr merge` is not allowed wholesale",
              any(r.startswith("Bash(gh pr merge") for r in allowed), False)

        # A resync must not duplicate them, for the same reason it must not double-register
        # the hooks: the file is read every session and grows every install.
        install(repo)
        check("a resync does not duplicate entries",
              len(permissions_of(repo)), len(set(permissions_of(repo))))

        # The lander is copied in and dated, because it is committed and so it is a fork
        # the moment this skill moves — the same failure the guard's own record exists for.
        check("the lander is copied in", (repo / ".claude" / "scripts" / "land.py").is_file(), True)
        check("and its provenance is recorded",
              bool((config_of(repo).get("land") or {}).get("sha256")), True)

        # --- an operator's own decisions survive ------------------------------------
        # The permissions block is the repo's, not this installer's. Somebody allowed
        # `npm test` here; an uninstall that took the whole block would silently reverse a
        # decision it was never asked about.
        settings_file = repo / ".claude" / "settings.json"
        blob = json.loads(settings_file.read_text(encoding="utf-8"))
        blob["permissions"]["allow"].append("Bash(npm test:*)")
        settings_file.write_text(json.dumps(blob, indent=2), encoding="utf-8")
        install(repo)
        check("a rule the operator added survives a resync",
              "Bash(npm test:*)" in permissions_of(repo), True)
        install(repo, "--uninstall")
        check("uninstall drops ours", "Bash(git status:*)" in permissions_of(repo), False)
        check("and leaves theirs", "Bash(npm test:*)" in permissions_of(repo), True)

        # --- opting out ------------------------------------------------------------
        bare = fresh(root, "bare")
        install(bare, "--no-permissions")
        check("--no-permissions writes none", permissions_of(bare), [])
        check("but still registers the hooks", registrations(bare),
              ["PreToolUse", "SessionStart", "Stop"])

        # --- the two files git will not carry for the protocol ---------------------
        # Both are silent when absent. An unignored `.claude/worktrees/` gets a live
        # checkout committed into the repository as a gitlink no clone can resolve; a
        # worktree with no `settings.local.json` falls back to the default permission
        # mode and starts refusing the protocol's own writes for no visible reason. The
        # installer is what puts worktrees under `.claude/worktrees/` in the first place,
        # so it is the installer that owes the repository both entries.
        carried = fresh(root, "carried")
        # Written as bytes so the fixture really has LF endings: `write_text` would
        # translate them on Windows, and the assertion below would be about the
        # fixture rather than about what the installer appended.
        (carried / ".gitignore").write_bytes(b"node_modules/" + bytes([10]))
        install(carried)
        ignored = (carried / ".gitignore").read_text(encoding="utf-8")
        check("the worktree directory is ignored", ".claude/worktrees/" in ignored, True)
        check("and this machine's permission mode with it",
              ".claude/settings.local.json" in ignored, True)
        check("what was already in the file is left where it was",
              ignored.startswith("node_modules/\n"), True)
        check(
            "the permission mode is copied into every new worktree instead",
            ".claude/settings.local.json"
            in (carried / ".worktreeinclude").read_text(encoding="utf-8"),
            True,
        )
        # Appended as bytes, because `write_text` would translate every existing LF on
        # Windows and turn a two-line addition into a whole-file diff.
        check("and the file's existing line endings survive the append",
              (carried / ".gitignore").read_bytes().count(bytes([13])), 0)

        unchanged = (carried / ".gitignore").read_bytes()
        install(carried)
        check("a second install does not append them again",
              (carried / ".gitignore").read_bytes(), unchanged)

        install(carried, "--uninstall")
        check(
            # Un-ignoring `.claude/worktrees/` is how a stale checkout gets committed, and
            # that outlives the guard. The uninstaller says it left them rather than acting.
            "uninstalling leaves both files as it found them",
            ".claude/worktrees/" in (carried / ".gitignore").read_text(encoding="utf-8"),
            True,
        )

        # The question is whether the REPOSITORY carries the rule, so the machine's own
        # ignores cannot answer it. Measured on the machine this was written on, whose
        # global ignore already names `**/.claude/settings.local.json`: `check-ignore` said
        # it was covered, the installer wrote nothing, and the repo went out to everybody
        # else without the entry — right where it was installed, false where it travels.
        elsewhere = fresh(root, "elsewhere")
        # Named `.gitignore`, as a global one often is (`~/.gitignore`): the file's name is
        # not what makes it the repository's, being tracked in it is.
        machine_ignore = root / "home" / ".gitignore"
        machine_ignore.parent.mkdir()
        machine_ignore.write_text(".claude/\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(elsewhere), "config",
                        "core.excludesFile", str(machine_ignore)], check=True)
        install(elsewhere)
        check(
            "a machine-local ignore does not stand in for the repository's own",
            ".claude/worktrees/" in (elsewhere / ".gitignore").read_text(encoding="utf-8"),
            True,
        )

        # `.git/info/exclude` is the same kind of answer: it lives in the git directory,
        # is never committed, and is true only in this clone. Measured 2026-09-23 on a
        # repo whose exclude named `.claude/worktrees/` and whose .gitignore did not: the
        # installer appended its note and `settings.local.json`, and left out the one
        # entry that keeps a live worktree from being committed as a gitlink.
        excluded = fresh(root, "excluded")
        (excluded / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(excluded), "add", ".gitignore"], check=True)
        with (excluded / ".git" / "info" / "exclude").open("a", encoding="utf-8") as handle:
            handle.write(".claude/worktrees/\n.claude/settings.local.json\n")
        install(excluded)
        ignored = (excluded / ".gitignore").read_text(encoding="utf-8").splitlines()
        check("an info/exclude rule does not stand in for the repository's own",
              ".claude/worktrees/" in ignored, True)
        check("for either entry", ".claude/settings.local.json" in ignored, True)

        # The other half: a TRACKED .gitignore that covers them under a broader pattern
        # is the repository answering, and it collects no second line.
        covered = fresh(root, "covered")
        (covered / ".gitignore").write_text("/.claude/worktrees/\n**/settings.local.json\n",
                                            encoding="utf-8")
        subprocess.run(["git", "-C", str(covered), "add", ".gitignore"], check=True)
        install(covered)
        check("a broader pattern in a tracked .gitignore is an answer",
              (covered / ".gitignore").read_text(encoding="utf-8").splitlines(),
              ["/.claude/worktrees/", "**/settings.local.json"])

        # A repo that already ignores them keeps its own spelling: this is the repo's file
        # and a second literal line saying the same thing is noise.
        broad = fresh(root, "broad")
        (broad / ".gitignore").write_text(
            ".claude/worktrees/\n.claude/settings.local.json\n", encoding="utf-8")
        install(broad)
        check(
            "an entry the repo already ignores is not repeated",
            (broad / ".gitignore").read_text(encoding="utf-8").count(".claude/worktrees/"),
            1,
        )

        # --- a predecessor guard is replaced, not left beside ----------------------
        legacy = fresh(root, "legacy")
        (legacy / ".claude").mkdir()
        (legacy / ".claude" / "settings.json").write_text(json.dumps({"hooks": {"PreToolUse": [
            {"matcher": "Write", "hooks": [{"type": "command", "command": "python",
                                            "args": ["-S", ".claude/hooks/parallel-guard.py"]}]}
        ]}}, indent=2), encoding="utf-8")
        out = install(legacy)
        check("a predecessor guard is reported", "predecessor" in out.stdout, True)
        check(
            "and is gone from the settings",
            "parallel-guard" in (legacy / ".claude" / "settings.json").read_text(encoding="utf-8"),
            False,
        )
        check("while ours is registered", registrations(legacy),
              ["PreToolUse", "SessionStart", "Stop"])

        # --- a missing script is not a block ----------------------------------------
        # `python -S <script>` exits 2 when the file is not there, and Claude Code reads
        # exit 2 from PreToolUse as a block — so a deleted or not-yet-pulled guard refused
        # every Bash, PowerShell, Edit and Write call in the session. Measured 2026-10-16:
        # stale copies of the hook files could not safely be deleted mid-session, and a
        # person had to. So every registration goes through a shim that exits 0 instead.
        # Status is asked with an empty user scope, so this machine's own settings cannot
        # answer for the repository under test.
        nobody = {**__import__("os").environ, "CLAUDE_CONFIG_DIR": str(root / "nobody")}
        shimmed = fresh(root, "shimmed")
        install(shimmed, "--session-ownership")
        guard_hooks = hooks_of(shimmed)
        check("each event registers the guard once", sorted(guard_hooks),
              ["PreToolUse", "SessionStart", "Stop"])
        check("through the shim, with the event and the script as its arguments",
              {event: hooks[0].get("args") for event, hooks in guard_hooks.items()},
              {event: ["-S", "-c", SHIM, event,
                       "${CLAUDE_PROJECT_DIR}/.claude/hooks/worktree-guard.py"]
               for event in ("PreToolUse", "SessionStart", "Stop")})
        check("still under the plain interpreter a committed file can carry",
              {hooks[0].get("command") for hooks in guard_hooks.values()}, {"python"})
        check("the ownership hook goes through it too",
              {event: hooks[0].get("args") for event, hooks in
               hooks_of(shimmed, "worktree-owner.py").items()},
              {event: ["-S", "-c", SHIM, event,
                       "${CLAUDE_PROJECT_DIR}/.claude/hooks/worktree-owner.py"]
               for event in ("PreToolUse", "SessionStart")})
        # The shim must not name either script, or recognising ours by file name — which
        # is how a resync, an uninstall and `--status` all find them — would misfire.
        check("the shim names neither script",
              any(name in SHIM for name in ("worktree-guard", "worktree_guard",
                                            "worktree-owner", "worktree_owner")), False)

        # Present, it behaves exactly as `python -S <script>` does: same output, same
        # exit code, for a call the guard denies.
        main_write = {"session_id": "t", "hook_event_name": "PreToolUse", "cwd": str(shimmed),
                      "tool_name": "Write",
                      "tool_input": {"file_path": str(shimmed / "a.txt")}}
        script = shimmed / ".claude" / "hooks" / "worktree-guard.py"
        through = run_hook(guard_hooks["PreToolUse"][0], shimmed, main_write)
        direct = subprocess.run([sys.executable, "-S", str(script)], input=json.dumps(main_write),
                                capture_output=True, text=True, cwd=str(shimmed))
        check("a present guard still denies a write in the main checkout",
              '"permissionDecision": "deny"' in through.stdout, True)
        check("...exactly as running it directly does",
              (through.returncode, through.stdout, through.stderr),
              (direct.returncode, direct.stdout, direct.stderr))

        # ...down to what the script itself can see. A probe in the guard's place reports
        # its argv, its module name, its own path, its sys.path[0] and its stdin, then
        # writes to stderr and exits 3 — each of which the shim must hand over untouched.
        probed = fresh(root, "probed")
        install(probed)
        probe = probed / ".claude" / "hooks" / "worktree-guard.py"
        probe.write_text(
            "import json, sys\n"
            "print(json.dumps({'argv': sys.argv, 'name': __name__, 'file': __file__,\n"
            "                  'path0': sys.path[0], 'stdin': sys.stdin.read()}))\n"
            "print('to stderr', file=sys.stderr)\n"
            "sys.exit(3)\n", encoding="utf-8")
        payload = {"hook_event_name": "PreToolUse", "cwd": str(probed)}
        through = run_hook(hooks_of(probed)["PreToolUse"][0], probed, payload)
        direct = subprocess.run([sys.executable, "-S", str(probe)], input=json.dumps(payload),
                                capture_output=True, text=True, cwd=str(probed))
        seen, expected = json.loads(through.stdout or "{}"), json.loads(direct.stdout or "{}")
        check("the script's exit code passes through", through.returncode, 3)
        check("...and its stderr", through.stderr, direct.stderr)
        check("it reads the payload from stdin as before", seen.get("stdin"), json.dumps(payload))
        check("it runs as __main__", seen.get("name"), "__main__")
        check("with argv shifted to just the script",
              [Path(arg).resolve() for arg in seen.get("argv") or []],
              [Path(arg).resolve() for arg in expected.get("argv") or []])
        check("knowing its own path", Path(seen.get("file") or ".").resolve(), probe.resolve())
        # Not the cwd: `-c` puts the cwd there, the cwd is the repository, and a `json.py`
        # at its root would otherwise be imported in place of the stdlib's.
        check("with its own directory first on sys.path, not the repository",
              Path(seen.get("path0") or ".").resolve(),
              Path(expected.get("path0") or "..").resolve())
        (probed / "json.py").write_text("raise SystemExit('the repository shadowed json')\n",
                                        encoding="utf-8")
        check("so a module at the repository's root cannot stand in for the stdlib",
              run_hook(hooks_of(probed)["PreToolUse"][0], probed, payload).returncode, 3)

        # Missing, it exits 0 — silently on the two events that run on every call, and
        # out loud on SessionStart, whose stdout reaches the session as context.
        script.unlink()
        main_add = {"session_id": "t", "hook_event_name": "PreToolUse", "cwd": str(shimmed),
                    "tool_name": "Bash", "tool_input": {"command": "git add -A"}}
        blocked = run_hook(guard_hooks["PreToolUse"][0], shimmed, main_add)
        check("a missing guard does not block PreToolUse",
              (blocked.returncode, blocked.stdout, blocked.stderr), (0, "", ""))
        stopped = run_hook(guard_hooks["Stop"][0], shimmed,
                           {"session_id": "t", "hook_event_name": "Stop", "cwd": str(shimmed)})
        check("...nor Stop", (stopped.returncode, stopped.stdout, stopped.stderr), (0, "", ""))
        started = run_hook(guard_hooks["SessionStart"][0], shimmed,
                           {"session_id": "t", "hook_event_name": "SessionStart",
                            "cwd": str(shimmed)})
        check("SessionStart exits 0 too", started.returncode, 0)
        check("...and says, on one line, that nothing is enforced",
              "worktree-guard.py is registered as a hook but missing at" in started.stdout
              and "nothing it enforces is enforced" in started.stdout
              and len(started.stdout.strip().splitlines()) == 1, True)
        # As a path, not a string: `${CLAUDE_PROJECT_DIR}/...` mixes separators on Windows.
        named = started.stdout.split("missing at ", 1)[-1].split(", so nothing", 1)[0]
        check("...naming where it looked", Path(named).resolve(), script.resolve())
        told = subprocess.run([sys.executable, str(INSTALL), "--repo", str(shimmed), "--status"],
                              capture_output=True, text=True, env=nobody).stdout
        check("--status says the registered script is missing",
              "is registered but missing, so nothing it enforces is enforced" in told, True)
        # The shape this replaces, against the same missing file: the block it existed for.
        to_direct(shimmed)
        before = run_hook(hooks_of(shimmed)["PreToolUse"][0], shimmed, main_add)
        check("the old shape blocks every call when the file is missing", before.returncode, 2)

        # --- the old shape is upgraded, never doubled -------------------------------
        # Every repository installed before the shim has `python -S <script>`. A resync has
        # to replace those registrations rather than add the new ones beside them, keep
        # anything the repo registered itself, and leave the file's key order alone.
        upgraded = fresh(root, "upgraded")
        install(upgraded, "--session-ownership")
        to_direct(upgraded)
        settings_file = upgraded / ".claude" / "settings.json"
        blob = json.loads(settings_file.read_text(encoding="utf-8"))
        blob["hooks"]["PreToolUse"].append(
            {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo", "args": ["x"]}]})
        settings_file.write_text(json.dumps(blob, indent=2) + "\n", encoding="utf-8")
        told = subprocess.run([sys.executable, str(INSTALL), "--repo", str(upgraded), "--status"],
                              capture_output=True, text=True, env=nobody).stdout
        check("--status recognises the old shape as installed",
              "settings.json  ->  installed (PreToolUse, SessionStart, Stop)" in told, True)
        check("...and says what it costs", "registered as `python -S <script>`" in told, True)
        keys = list(json.loads(settings_file.read_text(encoding="utf-8")))
        check("a resync over the old shape succeeds", install(upgraded).returncode, 0)
        check("it leaves one guard registration per event",
              {event: len(hooks) for event, hooks in hooks_of(upgraded).items()},
              {"PreToolUse": 1, "SessionStart": 1, "Stop": 1})
        check("...and one ownership registration per event",
              {event: len(hooks) for event, hooks in
               hooks_of(upgraded, "worktree-owner.py").items()},
              {"PreToolUse": 1, "SessionStart": 1})
        check("all of them in the new shape",
              all("-c" in hook["args"]
                  for found in (hooks_of(upgraded), hooks_of(upgraded, "worktree-owner.py"))
                  for hooks in found.values() for hook in hooks), True)
        check("the repo's own hook beside them survives",
              any(hook.get("command") == "echo"
                  for matcher in json.loads(settings_file.read_text(encoding="utf-8"))
                  ["hooks"]["PreToolUse"] for hook in matcher.get("hooks") or []), True)
        check("the file's key order survives the upgrade",
              list(json.loads(settings_file.read_text(encoding="utf-8"))), keys)
        told = subprocess.run([sys.executable, str(INSTALL), "--repo", str(upgraded), "--status"],
                              capture_output=True, text=True, env=nobody).stdout
        check("--status stops warning once it is upgraded",
              "registered as `python -S <script>`" in told, False)

        # --- uninstall takes either shape -------------------------------------------
        for shape in ("new", "old"):
            removed = fresh(root, f"uninstall-{shape}")
            install(removed, "--session-ownership")
            if shape == "old":
                to_direct(removed)
            install(removed, "--uninstall")
            check(f"uninstall clears the {shape} shape's guard registrations",
                  hooks_of(removed), {})
            check(f"...and the {shape} shape's ownership ones",
                  hooks_of(removed, "worktree-owner.py"), {})

        # --- installing where nothing may be committed ----------------------------
        # A committed guard changes what a COLLEAGUE's session may do in their own working
        # directory. Where that is not wanted, the install still has to be a real install:
        # hooks that fire, an allowlist that matches, and a record that can be dated.
        local = fresh(root, "uncommitted")
        outside = root / "tooling" / ".claude"
        outside.mkdir(parents=True)
        out = install(local, "--settings-file", "settings.local.json",
                      "--guard-root", str(outside), "--worktrees-root", "../trees")
        check("a local install succeeds", out.returncode, 0)
        check("nothing is written to the committed settings file",
              (local / ".claude" / "settings.json").exists(), False)
        local_settings = json.loads(
            (local / ".claude" / "settings.local.json").read_text(encoding="utf-8"))
        check("all three events are registered in the local file",
              sorted(local_settings.get("hooks") or {}),
              ["PreToolUse", "SessionStart", "Stop"])
        check("the guard is not copied into the repository",
              (local / ".claude" / "hooks" / "worktree-guard.py").exists(), False)
        check("it is written to the guard root instead",
              (outside / "hooks" / "worktree-guard.py").is_file(), True)
        # `${CLAUDE_PROJECT_DIR}` resolves to the tree the session is in, which is exactly
        # where this file is not. An install that kept it would register a hook that never
        # runs — and a hook that never runs is indistinguishable from one with nothing to
        # deny.
        registered = json.dumps(local_settings)
        check("the hook is referenced absolutely, not through the project dir",
              "CLAUDE_PROJECT_DIR" in registered, False)
        # Compared against the parsed args, not the serialised text: `json.dumps` escapes
        # every backslash, so a substring test for a Windows path passes only on POSIX.
        local_hooks = hooks_of(local, name="settings.local.json")
        check("--guard-root registers through the same shim",
              {event: hooks[0].get("args")[:4] for event, hooks in local_hooks.items()},
              {event: ["-S", "-c", SHIM, event] for event in ("PreToolUse", "SessionStart", "Stop")})
        check("...with the absolute path as the script it runs",
              {Path(hooks[0].get("args")[-1]) for hooks in local_hooks.values()},
              {outside / "hooks" / "worktree-guard.py"})
        check("and it names the file that exists",
              any(Path(arg) == outside / "hooks" / "worktree-guard.py"
                  for event in (local_settings.get("hooks") or {}).values()
                  for matcher in event
                  for hook in matcher.get("hooks", [])
                  for arg in hook.get("args", [])),
              True)
        # The allowlist is a prefix match on the command string, so `python` in the entry
        # and `python3` in the call is an entry that matches nothing — and says nothing.
        entries = (local_settings.get("permissions") or {}).get("allow") or []
        landers = [e for e in entries if "land.py" in e]
        check("land.py gets exactly one entry", len(landers), 1)
        check("spelled with an interpreter that exists here", sys.executable in landers[0], True)
        check("and with the absolute path it will be called by",
              str(outside / "scripts" / "land.py") in landers[0], True)
        check("the worktrees root is recorded", config_of(local).get("worktreesRoot"), "../trees")
        check("the branch is still recorded in the repo, where both readers look",
              config_of(local).get("integrationBranch"), "development")
        # The one failure mode of this install shape, said out loud rather than found out:
        # a worktree holds tracked files only, so an untracked settings file is in none of
        # them.
        check("and the install says the worktrees will not have it",
              "does NOT exist in a fresh worktree" in out.stdout, True)

        out = subprocess.run(
            [sys.executable, str(INSTALL), "--repo", str(local), "--status"],
            capture_output=True, text=True,
        )
        check("--status finds a local install rather than reporting none",
              "settings.local.json  ->  installed" in out.stdout, True)

        # An uninstall is frequently run without the flags the install had. Leaving a
        # grant behind is worse than leaving a hook behind: the hook announces itself, and
        # an allowlist entry for a script that is gone is a grant nobody remembers making.
        install(local, "--settings-file", "settings.local.json", "--uninstall")
        left = json.loads(
            (local / ".claude" / "settings.local.json").read_text(encoding="utf-8"))
        check("an uninstall without the original flags still clears the allowlist",
              (left.get("permissions") or {}).get("allow"), None)
        check("and clears the hooks", left.get("hooks"), None)

        # ... but a rule the operator NARROWED by hand is a decision, and an uninstaller
        # that swept it up would silently reverse it.
        narrowed = fresh(root, "narrowed")
        install(narrowed)
        settings_file = narrowed / ".claude" / "settings.json"
        blob = json.loads(settings_file.read_text(encoding="utf-8"))
        blob["permissions"]["allow"].append("Bash(python .claude/scripts/land.py --dry-run:*)")
        settings_file.write_text(json.dumps(blob, indent=2), encoding="utf-8")
        install(narrowed, "--uninstall")
        check("a hand-narrowed land.py rule survives the uninstall",
              permissions_of(narrowed), ["Bash(python .claude/scripts/land.py --dry-run:*)"])

        # --- session ownership ----------------------------------------------------
        # The second hook. Opt-in, separate from the guard on disk and in the settings
        # file, and sticky across a resync — the last of those is the one that matters,
        # because a resync that quietly retires a rule people rely on looks exactly like a
        # successful resync.
        owned = fresh(root, "owned")
        install(owned)
        check("it is off unless asked for", owner_registrations(owned), [])
        check("...and recorded as off", config_of(owned).get("sessionOwnership"), False)
        check("no ownership hook is copied",
              (owned / ".claude" / "hooks" / "worktree-owner.py").exists(), False)

        out = install(owned, "--session-ownership")
        check("--session-ownership succeeds", out.returncode, 0)
        check("it registers on PreToolUse and SessionStart only",
              owner_registrations(owned), ["PreToolUse", "SessionStart"])
        # Not Stop: a session ending is not a write, and the hook has nothing to decide
        # there. Asserted rather than assumed, because the cost of getting it wrong is one
        # interpreter start per stop, forever, for nothing.
        check("and not on Stop", "Stop" in owner_registrations(owned), False)
        check("the guard's own three are untouched", registrations(owned),
              ["PreToolUse", "SessionStart", "Stop"])
        check("the script is copied in",
              (owned / ".claude" / "hooks" / "worktree-owner.py").is_file(), True)
        check("it is recorded, so a resync keeps it",
              config_of(owned).get("sessionOwnership"), True)
        check("with a provenance that can date the copy",
              content_hash((HERE / "worktree_owner.py").read_bytes()),
              (config_of(owned).get("owner") or {}).get("sha256"))

        install(owned)
        check("a resync without the flag keeps the rule",
              owner_registrations(owned), ["PreToolUse", "SessionStart"])

        out = install(owned, "--no-session-ownership")
        check("--no-session-ownership clears the registration",
              owner_registrations(owned), [])
        check("...and removes the script, so no later resync mistakes it for a decision",
              (owned / ".claude" / "hooks" / "worktree-owner.py").exists(), False)
        check("...and records the answer", config_of(owned).get("sessionOwnership"), False)
        check("...and drops the provenance with it", "owner" in config_of(owned), False)
        check("the guard survives all of that", registrations(owned),
              ["PreToolUse", "SessionStart", "Stop"])

        # An uninstall run without the flag the install had: the hook announces itself on
        # every tool call, so one left behind pointing at a deleted script is noisier than
        # the guard entry it sits beside.
        install(owned, "--session-ownership")
        install(owned, "--uninstall")
        check("an uninstall without the flag still clears the ownership hook",
              owner_registrations(owned), [])
        check("...and its script", (owned / ".claude" / "hooks" / "worktree-owner.py").exists(), False)

        # Never at user scope. Claims are keyed off a repository's common git dir, so a
        # user-scope registration would run it against every repo on the machine.
        user = root / "user-scope"
        (user / ".claude").mkdir(parents=True)
        out = subprocess.run(
            [sys.executable, str(INSTALL), "--branch", "development", "--no-skill",
             "--session-ownership", "--dry-run"],
            capture_output=True, text=True, env={**__import__("os").environ,
                                                 "CLAUDE_CONFIG_DIR": str(user / ".claude")},
        )
        check("a user-scope install ignores --session-ownership",
              "worktree_owner.py" in out.stdout, False)

        # --- a committed install is a change, and is made as one ---------------------
        # It used to be written into the main checkout with "commit these" printed after
        # it. Measured 2026-10-16: the guard it had just registered denied that `git add`
        # in the same session, and the files had to be cleared out by hand before the main
        # checkout would pull again. So it is cut, written and committed where every change
        # is, and the main checkout is not touched at all.
        landed = remoted(root, "landed")
        untouched = checkout_bytes(landed)
        out = install(landed, "--branch", "main", "--session-ownership")
        check("an install into a repo with an origin succeeds", out.returncode, 0)
        made = install_branches(landed)
        check("it makes exactly one branch of its own", len(made), 1)
        topic = made[0] if made else "install-worktree-per-change-missing"
        tree = landed / ".claude" / "worktrees" / topic
        check("in a worktree of the same name under the worktrees root",
              (tree / ".git").is_file(), True)
        check("the main checkout is left byte for byte as it was",
              checkout_bytes(landed) == untouched, True)
        check("so it holds no guard yet", (landed / ".claude" / "hooks").exists(), False)
        check("and is still on its own branch",
              git_in(landed, "rev-parse", "--abbrev-ref", "HEAD"), "main")
        # Until the install lands nothing in the main checkout ignores the worktrees, and
        # its status would show `?? .claude/worktrees/` — the one line a `git add -A` turns
        # into a gitlink, at the one moment no guard is there to stop it. This clone's
        # exclude closes that window; the tracked .gitignore is still what carries it.
        check("its status stays empty while the install is in flight",
              git_in(landed, "status", "--porcelain"), "")
        check("because this clone excludes the worktrees root",
              "/.claude/worktrees/" in (landed / ".git" / "info" / "exclude")
              .read_text(encoding="utf-8").splitlines(), True)
        check("the branch commits exactly the files the install wrote",
              sorted(git_in(landed, "diff", "--name-only", "origin/main", topic).splitlines()),
              [".claude/hooks/worktree-guard.py", ".claude/hooks/worktree-owner.py",
               ".claude/scripts/land.py", ".claude/settings.json",
               ".claude/worktree-per-change.json", ".gitignore", ".worktreeinclude"])
        check("in one commit on top of the fetched base",
              git_in(landed, "rev-list", "--count", f"origin/main..{topic}"), "1")
        check("leaving nothing uncommitted in the worktree", git_in(tree, "status", "--porcelain"), "")
        check("and nothing pushed — landing is land.py's job",
              git_in(landed, "ls-remote", "--heads", "origin", topic), "")
        synced = (config_of(tree).get("guard") or {}).get("syncedFrom")
        if synced:
            check("the commit records where the copy came from",
                  f"syncedFrom: {synced}" in git_in(landed, "log", "-1", "--format=%B", topic),
                  True)
        check("the one next step is land.py, from inside that worktree",
              "land.py" in out.stdout and topic in out.stdout.split("Next:", 1)[-1], True)
        # land.py reads the record from the MAIN checkout, which has none until this lands,
        # and falls back to the one this worktree carries — so the step is the plain
        # command the allowlist entry matches, with no prefix in front of it.
        check("the step is the command the allowlist matches",
              "run `python .claude/scripts/land.py`" in out.stdout, True)
        check("...with no override in front of it",
              "CLAUDE_INTEGRATION_BRANCH" in out.stdout, False)

        # Refused, never reused or overwritten: a second install in flight writes the same
        # files and conflicts with the first, and a half-landed one is its owner's question.
        again = install(landed, "--branch", "main")
        check("a second install while the first stands is refused", again.returncode, 2)
        check("...naming what is standing", topic in again.stderr, True)
        check("...and cutting nothing", install_branches(landed), [topic])
        squatted = remoted(root, "squatted")
        subprocess.run(["git", "-C", str(squatted), "branch", "install-worktree-per-change"],
                       check=True)
        check("an install branch that is already there is refused",
              install(squatted, "--branch", "main").returncode, 2)
        check("...with no worktree cut beside it",
              (squatted / ".claude" / "worktrees").exists(), False)
        # A directory as well: `git worktree remove` deregisters before it deletes, and
        # keeps the deregistration when the delete fails.
        leftover = remoted(root, "leftover")
        (leftover / ".claude" / "worktrees" / "install-worktree-per-change-old").mkdir(parents=True)
        out = install(leftover, "--branch", "main")
        check("a leftover install directory is refused too", out.returncode, 2)
        check("...and named", "install-worktree-per-change-old" in out.stderr, True)

        # --- a resync reads the record the integration branch carries --------------
        # Land it the way a merged PR would, and leave the main checkout un-pulled — which
        # under this protocol is its ordinary state. A resync that read the record from
        # there would find none, drop the ownership hook, and have something to commit.
        subprocess.run(["git", "-C", str(landed), "push", "-q", "origin", f"{topic}:main"],
                       check=True, capture_output=True)
        subprocess.run(["git", "-C", str(landed), "worktree", "remove", "--force", str(tree)],
                       check=True)
        subprocess.run(["git", "-C", str(landed), "branch", "-D", topic], check=True,
                       capture_output=True)
        out = install(landed, "--branch", "main")
        check("a resync of what already landed has nothing to land", out.returncode, 0)
        check("...says so", "nothing to land" in out.stdout, True)
        check("...and leaves no branch", install_branches(landed), [])
        check("...or worktree behind", any((landed / ".claude" / "worktrees").iterdir()), False)
        out = install(landed, "--branch", "main", "--no-session-ownership")
        check("a resync that changes something commits it", out.returncode, 0)
        retired = install_branches(landed)
        check("...including a file it removed, as a deletion",
              "D\t.claude/hooks/worktree-owner.py"
              in git_in(landed, "diff", "--name-status", "origin/main",
                        retired[0] if retired else "main").splitlines(), True)
        check("...needing no override, since the main checkout has pulled no record",
              "CLAUDE_INTEGRATION_BRANCH" in out.stdout, False)
        check("...and the un-pulled main checkout is still untouched",
              checkout_bytes(landed) == untouched, True)

        # --- a change of branch still needs the override -----------------------------
        # land.py falls back to the worktree's record only where the main checkout has
        # none. One that records the OLD branch still wins, which is what keeps every
        # worktree agreeing — so moving the branch is the one install whose step says it.
        moving = remoted(root, "moving")
        subprocess.run(["git", "-C", str(moving), "push", "-q", "origin", "main:queue"],
                       check=True, capture_output=True)
        (moving / ".claude").mkdir(exist_ok=True)
        (moving / ".claude" / "worktree-per-change.json").write_text(
            json.dumps({"integrationBranch": "main"}), encoding="utf-8")
        out = install(moving, "--branch", "queue")
        check("an install that moves the branch succeeds", out.returncode, 0)
        check("...and names the new one for land.py",
              "CLAUDE_INTEGRATION_BRANCH=queue python .claude/scripts/land.py" in out.stdout, True)
        check("...saying what the main checkout records instead", "records `main`" in out.stdout,
              True)

        # --- in place: asked for, or where a worktree makes no sense -----------------
        placed = remoted(root, "placed")
        out = install(placed, "--branch", "main", "--in-place")
        check("--in-place succeeds", out.returncode, 0)
        check("...writes into the checkout itself",
              (placed / ".claude" / "hooks" / "worktree-guard.py").is_file(), True)
        check("...with no branch", install_branches(placed), [])
        check("...or worktree", (placed / ".claude" / "worktrees").exists(), False)
        check("...and says so", "in place: asked for with --in-place" in out.stdout, True)
        # A resync of the same install rewrites the settings file with the same bytes. It
        # used to swap the file's two blocks on the first resync, which is a diff of
        # nothing that somebody then has to commit, review and land.
        written = (placed / ".claude" / "settings.json").read_bytes()
        install(placed, "--branch", "main", "--in-place")
        check("a resync keeps the settings file's key order",
              (placed / ".claude" / "settings.json").read_bytes() == written, True)

        # Nothing committed, so nothing to land: a local install on a repo with an origin.
        localised = remoted(root, "localised")
        out = install(localised, "--branch", "main", "--settings-file", "settings.local.json",
                      "--guard-root", str(root / "tooling-local" / ".claude"))
        check("a local install is written in place even with an origin",
              (localised / ".claude" / "settings.local.json").is_file(), True)
        check("...with no branch of its own", install_branches(localised), [])
        check("...and says why", "nothing this install writes is committed" in out.stdout, True)

        allowlisted = remoted(root, "allowlisted")
        out = install(allowlisted, "--permissions-only")
        check("--permissions-only is written in place",
              bool(permissions_of(allowlisted)) and not install_branches(allowlisted), True)
        check("...and says so", "in place: --permissions-only" in out.stdout, True)

        reported = remoted(root, "reported")
        subprocess.run([sys.executable, str(INSTALL), "--repo", str(reported), "--status"],
                       capture_output=True, text=True)
        check("--status cuts nothing", install_branches(reported), [])

        scoped = root / "user-scope-real" / ".claude"
        scoped.mkdir(parents=True)
        out = subprocess.run(
            [sys.executable, str(INSTALL), "--branch", "development", "--no-skill"],
            capture_output=True, text=True, cwd=str(root),
            env={**__import__("os").environ, "CLAUDE_CONFIG_DIR": str(scoped)},
        )
        check("a user-scope install is in place, and says so",
              "in place: a user-scope install" in out.stdout, True)

        # --- the dry run shows the worktree and writes nothing ------------------------
        rehearsed = remoted(root, "rehearsed")
        untouched = checkout_bytes(rehearsed)
        out = install(rehearsed, "--branch", "main", "--dry-run")
        check("a worktree dry run exits clean", out.returncode, 0)
        check("it names the worktree it would cut",
              "would cut" in out.stdout and ".claude" in out.stdout
              and "install-worktree-per-change-" in out.stdout, True)
        check("...the branch, and the fetched base it would cut it from",
              "from origin/main (fetched)" in out.stdout, True)
        for name in ("worktree-guard.py", "land.py", "settings.json",
                     "worktree-per-change.json", ".gitignore", ".worktreeinclude"):
            check(f"it lists {name} among the files it would write there", name in out.stdout, True)
        check("it says it would commit them", "would commit exactly those files" in out.stdout, True)
        check("and that it would exclude the worktrees root",
              "would exclude /.claude/worktrees/" in out.stdout, True)
        check("it cuts no branch", install_branches(rehearsed), [])
        check("...and no worktree", (rehearsed / ".claude").exists(), False)
        check("...and leaves the checkout as it was", checkout_bytes(rehearsed) == untouched, True)
        check("...including this clone's excludes",
              "/.claude/worktrees/" in (rehearsed / ".git" / "info" / "exclude")
              .read_text(encoding="utf-8"), False)

        # --- an install from a stale clone says so, and still installs ---------------
        # `syncedFrom` is whatever HEAD the skill's own checkout is at, and nothing asked
        # whether that was current. Measured 2026-10-16: a clone one commit behind GitHub
        # installed without a word, and its record named the stale commit. The answer comes
        # from the last-fetched refs — the installer never reaches the network itself.
        clone, seed = skill_clone(root, "skill")
        cloned = clone / "worktree-per-change" / "scripts" / "install.py"

        def from_clone(target: Path, *extra: str) -> subprocess.CompletedProcess:
            return subprocess.run([sys.executable, str(cloned), "--repo", str(target),
                                   "--branch", "development", "--no-skill", *extra],
                                  capture_output=True, text=True)

        out = from_clone(fresh(root, "from-current"))
        check("a current clone installs without a warning", "behind" in out.stdout, False)
        check("...and its status says it is current, and as of when",
              "up to date with origin/main as of its last fetch"
              in from_clone(root / "from-current", "--status").stdout, True)

        (seed / "NEWS").write_text("a change that landed upstream\n", encoding="utf-8")
        for args in (("add", "NEWS"), ("commit", "-q", "-m", "landed"), ("push", "-q")):
            subprocess.run(["git", "-C", str(seed), *args], check=True, capture_output=True)
        out = from_clone(fresh(root, "unfetched"))
        check("with nothing fetched it cannot know, and does not go and ask",
              "behind" in out.stdout, False)

        subprocess.run(["git", "-C", str(clone), "fetch", "-q"], check=True, capture_output=True)
        stale_target = fresh(root, "from-stale")
        out = from_clone(stale_target)
        check("a clone behind its last-fetched upstream warns",
              "1 commit(s) behind origin/main as of its last fetch" in out.stdout, True)
        check("...before the rest of the report, so it is not lost under it",
              out.stdout.startswith("warning:"), True)
        check("...and does not refuse", out.returncode, 0)
        check("...so the guard is still installed",
              (stale_target / ".claude" / "hooks" / "worktree-guard.py").is_file(), True)
        check("...recording the commit it actually came from",
              (config_of(stale_target).get("guard") or {}).get("syncedFrom"),
              git_in(clone, "rev-parse", "HEAD"))
        check("the dry run warns too",
              "behind origin/main" in from_clone(fresh(root, "stale-dry"), "--dry-run").stdout,
              True)
        check("--status says the same",
              "1 commit(s) behind origin/main as of its last fetch; `git pull --ff-only`"
              in from_clone(stale_target, "--status").stdout, True)
        check("--uninstall has nothing to warn about",
              "behind" in from_clone(stale_target, "--uninstall").stdout, False)

        subprocess.run(["git", "-C", str(clone), "pull", "-q", "--ff-only"], check=True,
                       capture_output=True)
        check("pulling clears it", "behind" in from_clone(fresh(root, "pulled")).stdout, False)

        # A branch with no upstream, and a directory that is not a checkout at all, have
        # nothing to compare with — and say that in --status rather than claiming currency.
        subprocess.run(["git", "-C", str(clone), "switch", "-q", "-c", "local-only"], check=True,
                       capture_output=True)
        check("a branch with no upstream installs without a warning",
              "behind" in from_clone(fresh(root, "no-upstream")).stdout, False)
        check("...and its status says there is nothing to compare with",
              "no upstream to compare it with"
              in from_clone(root / "no-upstream", "--status").stdout, True)
        unpacked = root / "unpacked" / "worktree-per-change"
        shutil.copytree(clone / "worktree-per-change", unpacked)
        out = subprocess.run([sys.executable, str(unpacked / "scripts" / "install.py"),
                              "--repo", str(fresh(root, "from-unpacked")), "--branch",
                              "development", "--no-skill"], capture_output=True, text=True)
        check("a skill that is not a checkout installs without a warning",
              (out.returncode, "behind" in out.stdout), (0, False))
        out = subprocess.run([sys.executable, str(unpacked / "scripts" / "install.py"),
                              "--repo", str(root / "from-unpacked"), "--status"],
                             capture_output=True, text=True)
        check("...and its status says how current it is cannot be said",
              "not a git checkout, so how current it is cannot be said" in out.stdout, True)

    print(f"{PASSED} passed, {len(FAILED)} failed")
    for line in FAILED:
        print(f"  FAIL  {line}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    if shutil.which("git") is None:
        print("git is not on PATH")
        raise SystemExit(1)
    raise SystemExit(main())
