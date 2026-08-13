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

    print(f"{PASSED} passed, {len(FAILED)} failed")
    for line in FAILED:
        print(f"  FAIL  {line}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    if shutil.which("git") is None:
        print("git is not on PATH")
        raise SystemExit(1)
    raise SystemExit(main())
