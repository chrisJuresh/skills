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
from install import content_hash  # noqa: E402

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


def fresh(root: Path, name: str) -> Path:
    repo = root / name
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    return repo


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
        check("and it names the file that exists", str(outside / "hooks") in registered, True)
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

    print(f"{PASSED} passed, {len(FAILED)} failed")
    for line in FAILED:
        print(f"  FAIL  {line}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    if shutil.which("git") is None:
        print("git is not on PATH")
        raise SystemExit(1)
    raise SystemExit(main())
