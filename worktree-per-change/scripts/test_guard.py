#!/usr/bin/env python3
"""Checks for worktree_guard.py, against real git repositories in a temp directory.

Two categories, and the second is the one that matters:

  * **Denials that must happen.** Each is the protocol's actual content — if one of
    these stops firing, the rule is a suggestion.
  * **Allows that must keep happening.** Every false positive lands on ordinary work in
    a legitimate worktree and spends trust the guard has to keep. A guard that denies
    something reasonable is a guard someone deletes, and then it protects nothing.

    python test_guard.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

GUARD = Path(__file__).resolve().parent / "worktree_guard.py"

PASSED = 0
FAILED: list[str] = []


def git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def run(payload: dict, env: dict | None = None) -> dict | None:
    """Feed the guard a hook payload; return its JSON output, or None for silence."""
    environment = {**os.environ, "CLAUDE_WORKTREE_GATE": "on"}
    environment.pop("CLAUDE_INTEGRATION_BRANCH", None)
    environment.update(env or {})
    result = subprocess.run(
        [sys.executable, "-S", str(GUARD)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=environment,
    )
    body = result.stdout.strip()
    if not body:
        return None
    try:
        return json.loads(body)
    except ValueError:
        return {"unparseable": body}


def decision(output: dict | None) -> str:
    if output is None:
        return "allow"
    inner = output.get("hookSpecificOutput") or {}
    if output.get("decision") == "block":
        return "block"
    return inner.get("permissionDecision", "allow")


def check(name: str, got, want) -> None:
    global PASSED
    if got == want:
        PASSED += 1
    else:
        FAILED.append(f"{name}: expected {want!r}, got {got!r}")


def write(cwd: Path, path: str) -> dict:
    return {
        "session_id": "test",
        "hook_event_name": "PreToolUse",
        "cwd": str(cwd),
        "tool_name": "Write",
        "tool_input": {"file_path": path},
    }


def shell(cwd: Path, command: str) -> dict:
    return {
        "session_id": "test",
        "hook_event_name": "PreToolUse",
        "cwd": str(cwd),
        "tool_name": "Bash",
        "tool_input": {"command": command},
    }


# ------------------------------------------------------------------------ fixtures


def build(root: Path) -> tuple[Path, Path, Path]:
    """A repo with `development` as its integration branch, plus two worktrees."""
    main = root / "repo"
    main.mkdir()
    git(main, "init", "-b", "main")
    git(main, "config", "user.email", "t@example.com")
    git(main, "config", "user.name", "t")
    (main / "README.md").write_text("hello\n", encoding="utf-8")
    git(main, "add", "README.md")
    git(main, "commit", "-m", "init")
    git(main, "branch", "development")

    claude = main / ".claude"
    claude.mkdir()
    (claude / "worktree-per-change.json").write_text(
        json.dumps({"integrationBranch": "development"}), encoding="utf-8"
    )

    topic = main / ".claude" / "worktrees" / "topic"
    git(main, "worktree", "add", str(topic), "-b", "a-topic", "development")

    onbase = main / ".claude" / "worktrees" / "onbase"
    git(main, "worktree", "add", str(onbase), "development")

    return main, topic, onbase


# --------------------------------------------------------------------------- cases


def main() -> int:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        repo, topic, onbase = build(root)

        # --- denied in the main checkout ---------------------------------------
        check(
            "write in main checkout is denied",
            decision(run(write(repo, str(repo / "README.md")))),
            "deny",
        )
        check(
            "write to a nested path in the main checkout is denied",
            decision(run(write(repo, str(repo / "src" / "deep" / "a.ts")))),
            "deny",
        )
        check(
            "relative write in the main checkout is denied",
            decision(run(write(repo, "README.md"))),
            "deny",
        )
        for subcommand in ("commit -m x", "add .", "switch main", "reset --hard", "clean -fd"):
            check(
                f"`git {subcommand.split()[0]}` in the main checkout is denied",
                decision(run(shell(repo, f"git {subcommand}"))),
                "deny",
            )
        check(
            "a git call later in a chain is still seen",
            decision(run(shell(repo, "echo hi && git commit -m x"))),
            "deny",
        )

        # --- allowed in the main checkout --------------------------------------
        for command in ("git status", "git log --oneline", "git fetch origin", "git pull",
                        "git worktree add .claude/worktrees/x -b x development",
                        "git branch --show-current", "git diff", "gh pr list"):
            check(
                f"`{command}` in the main checkout is allowed",
                decision(run(shell(repo, command))),
                "allow",
            )
        check(
            "a write outside the repository is allowed from the main checkout",
            decision(run(write(repo, str(root / "elsewhere.txt")))),
            "allow",
        )
        check(
            "reads are not the guard's business",
            decision(
                run(
                    {
                        "session_id": "test",
                        "hook_event_name": "PreToolUse",
                        "cwd": str(repo),
                        "tool_name": "Read",
                        "tool_input": {"file_path": str(repo / "README.md")},
                    }
                )
            ),
            "allow",
        )

        # --- the worktree is where work happens --------------------------------
        check(
            "write in a topic worktree is allowed",
            decision(run(write(topic, str(topic / "README.md")))),
            "allow",
        )
        for command in ("git add README.md", "git commit -m x", "git push -u origin HEAD",
                        "gh pr create --base development --fill", "git switch -c another",
                        "git rebase origin/development"):
            check(
                f"`{command}` in a worktree is allowed",
                decision(run(shell(topic, command))),
                "allow",
            )
        check(
            "write in a worktree sitting on the integration branch is denied",
            decision(run(write(onbase, str(onbase / "README.md")))),
            "deny",
        )
        check(
            "the integration branch is configurable",
            decision(run(write(topic, str(topic / "README.md")), {"CLAUDE_INTEGRATION_BRANCH": "a-topic"})),
            "deny",
        )

        # --- stash, everywhere -------------------------------------------------
        check("`git stash` in a worktree is denied", decision(run(shell(topic, "git stash"))), "deny")
        check("`git stash push` is denied", decision(run(shell(topic, "git stash push -m x"))), "deny")
        check("`git stash` in the main checkout is denied", decision(run(shell(repo, "git stash"))), "deny")
        check("`git stash list` is allowed", decision(run(shell(topic, "git stash list"))), "allow")

        # --- spent worktrees ---------------------------------------------------
        check(
            "the worktree is not spent before the merge",
            decision(run(write(topic, str(topic / "README.md")))),
            "allow",
        )
        check(
            "`gh pr merge` is itself allowed",
            decision(run(shell(topic, "gh pr merge --squash"))),
            "allow",
        )
        check(
            "a write after `gh pr merge` is denied",
            decision(run(write(topic, str(topic / "README.md")))),
            "deny",
        )
        check(
            "the sibling worktree is unaffected by the other's merge",
            decision(run(write(onbase, str(root / "elsewhere.txt")))),
            "allow",
        )
        check(
            "the marker lives in the shared git dir, not the working tree",
            subprocess.run(
                ["git", "-C", str(topic), "status", "--porcelain"],
                capture_output=True,
                text=True,
            ).stdout.strip(),
            "",
        )

        # --- modes and fail-open ------------------------------------------------
        check(
            "`off` disables it",
            decision(run(write(repo, str(repo / "README.md")), {"CLAUDE_WORKTREE_GATE": "off"})),
            "allow",
        )
        check(
            "`warn` allows instead of denying",
            decision(run(write(repo, str(repo / "README.md")), {"CLAUDE_WORKTREE_GATE": "warn"})),
            "allow",
        )
        check(
            "`warn` still says what it would have denied",
            "worktree-per-change" in json.dumps(
                run(write(repo, str(repo / "README.md")), {"CLAUDE_WORKTREE_GATE": "warn"})
            ),
            True,
        )
        outside = root / "not-a-repo"
        outside.mkdir()
        check(
            "outside a git repository it stands down",
            decision(run(write(outside, str(outside / "a.txt")))),
            "allow",
        )
        check("a payload with no cwd is ignored", decision(run({"tool_name": "Write"})), "allow")
        check(
            "a payload with no tool_input is ignored",
            decision(run({"session_id": "t", "hook_event_name": "PreToolUse", "cwd": str(repo), "tool_name": "Write"})),
            "allow",
        )

        # --- the Stop hook ------------------------------------------------------
        clean = {"session_id": "s1", "hook_event_name": "Stop", "cwd": str(onbase)}
        check("Stop in a clean worktree does not block", decision(run(clean)), "allow")
        check(
            "Stop in the main checkout does not block",
            decision(run({"session_id": "s2", "hook_event_name": "Stop", "cwd": str(repo)})),
            "allow",
        )
        (topic / "dirty.txt").write_text("x", encoding="utf-8")
        check(
            "Stop blocks on uncommitted work in a worktree",
            decision(run({"session_id": "s3", "hook_event_name": "Stop", "cwd": str(topic)})),
            "block",
        )
        for _ in range(2):
            run({"session_id": "s4", "hook_event_name": "Stop", "cwd": str(topic)})
        check(
            "Stop gives up after two blocks rather than hanging the session",
            decision(run({"session_id": "s4", "hook_event_name": "Stop", "cwd": str(topic)})),
            "allow",
        )

        # --- SessionStart -------------------------------------------------------
        started = run({"session_id": "s5", "hook_event_name": "SessionStart", "cwd": str(repo)})
        check(
            "SessionStart states the protocol",
            "EnterWorktree" in json.dumps(started or {}) and "development" in json.dumps(started or {}),
            True,
        )

    print(f"{PASSED} passed, {len(FAILED)} failed")
    for line in FAILED:
        print(f"  FAIL  {line}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    if shutil.which("git") is None:
        print("git is not on PATH")
        raise SystemExit(1)
    raise SystemExit(main())
