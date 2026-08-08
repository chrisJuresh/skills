#!/usr/bin/env python3
"""Exercise the guard's decisions against real git repositories in a temp directory.

Every case here is a failure someone actually hit, or a false positive that would
make someone delete the hook. Run it after any change to `parallel_guard.py`:

    python scripts/test_guard.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

GUARD = Path(__file__).resolve().parent / "parallel_guard.py"
REGISTRY = "claude-parallel-sessions"

failures: list[str] = []


def run(payload: dict, env: dict | None = None) -> dict:
    environment = {**os.environ, "CLAUDE_PARALLEL_GUARD": ""}
    environment.pop("CLAUDE_PARALLEL_GUARD")
    environment.update(env or {})
    result = subprocess.run(
        [sys.executable, str(GUARD)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=environment,
    )
    if result.returncode != 0:
        raise AssertionError(f"guard exited {result.returncode}: {result.stderr}")
    if not result.stdout.strip():
        return {}
    return json.loads(result.stdout)


def decision(out: dict) -> str:
    return ((out.get("hookSpecificOutput") or {}).get("permissionDecision")) or "allow"


def check(name: str, got, want) -> None:
    if got == want:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}: got {got!r}, want {want!r}")
        failures.append(name)


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def make_repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "t@t.t")
    git(root, "config", "user.name", "t")
    (root / "seed.txt").write_text("seed", encoding="utf-8")
    git(root, "add", "seed.txt")
    git(root, "commit", "-qm", "seed")
    return root


def claim(repo: Path, session: str, tree: Path, paths: list[Path], age: float = 0.0) -> None:
    """Write another session's claim directly, as the guard would have."""
    now = time.time() - age
    registry = repo / ".git" / REGISTRY
    registry.mkdir(parents=True, exist_ok=True)
    (registry / f"{session}.json").write_text(
        json.dumps(
            {
                "session_id": session,
                "tree_root": os.path.normcase(os.path.normpath(str(tree))),
                "cwd": str(tree),
                "transcript_path": None,
                "since": now,
                "last_write_at": now,
                "paths": {os.path.normcase(os.path.normpath(str(p))): now for p in paths},
            }
        ),
        encoding="utf-8",
    )


def pre(cwd: Path, tool: str, tool_input: dict, session: str = "mine") -> dict:
    return {
        "session_id": session,
        "hook_event_name": "PreToolUse",
        "cwd": str(cwd),
        "tool_name": tool,
        "tool_input": tool_input,
    }


def main() -> int:
    with tempfile.TemporaryDirectory() as temporary:
        base = Path(temporary)

        print("\n[A] a lone writer is never blocked")
        repo = make_repo(base / "alone")
        check("edit", decision(run(pre(repo, "Edit", {"file_path": str(repo / "a.ts")}))), "allow")
        check("switch", decision(run(pre(repo, "Bash", {"command": "git switch -c feat"}))), "allow")
        check("add -A", decision(run(pre(repo, "Bash", {"command": "git add -A && git commit -m x"}))), "allow")
        check("stash", decision(run(pre(repo, "Bash", {"command": "git stash"}))), "allow")
        check(
            "claim was recorded",
            (repo / ".git" / REGISTRY / "mine.json").is_file(),
            True,
        )

        print("\n[B] a second live writer — the three silent failures are denied")
        repo = make_repo(base / "contended")
        claim(repo, "other", repo, [repo / "auth.ts", repo / "user.ts"])
        check("floor-mover: git switch", decision(run(pre(repo, "Bash", {"command": "git switch feat"}))), "deny")
        check("floor-mover: git checkout", decision(run(pre(repo, "Bash", {"command": "git checkout main"}))), "deny")
        check("floor-mover: git reset --hard", decision(run(pre(repo, "Bash", {"command": "git reset --hard"}))), "deny")
        check("floor-mover: git rebase", decision(run(pre(repo, "Bash", {"command": "git rebase origin/main"}))), "deny")
        check("blind stage: git add -A", decision(run(pre(repo, "Bash", {"command": "git add -A"}))), "deny")
        check("blind stage: git add .", decision(run(pre(repo, "Bash", {"command": "git add ."}))), "deny")
        check("blind stage: git commit -am", decision(run(pre(repo, "Bash", {"command": 'git commit -am "x"'}))), "deny")
        check("stash", decision(run(pre(repo, "Bash", {"command": "git stash push -m wip"}))), "deny")
        check("amend", decision(run(pre(repo, "Bash", {"command": "git commit --amend --no-edit"}))), "deny")
        check("same file", decision(run(pre(repo, "Edit", {"file_path": str(repo / "auth.ts")}))), "deny")

        print("\n[C] and everything else still works")
        check("different file", decision(run(pre(repo, "Edit", {"file_path": str(repo / "billing.ts")}))), "allow")
        check("targeted add", decision(run(pre(repo, "Bash", {"command": "git add billing.ts"}))), "allow")
        check("plain commit", decision(run(pre(repo, "Bash", {"command": 'git commit -m "x"'}))), "allow")
        check("push", decision(run(pre(repo, "Bash", {"command": "git push -u origin feat"}))), "allow")
        check("status", decision(run(pre(repo, "Bash", {"command": "git status --short"}))), "allow")
        check("log", decision(run(pre(repo, "Bash", {"command": "git log --oneline -20"}))), "allow")
        check("stash list", decision(run(pre(repo, "Bash", {"command": "git stash list"}))), "allow")
        check("worktree add is the remedy", decision(run(pre(repo, "Bash", {"command": "git worktree add .claude/worktrees/x -b x main"}))), "allow")
        check("npm test", decision(run(pre(repo, "Bash", {"command": "npm test"}))), "allow")

        print("\n[D] a stale claim frees the checkout")
        repo = make_repo(base / "stale")
        claim(repo, "gone", repo, [repo / "auth.ts"], age=45 * 60)
        check("floor-mover", decision(run(pre(repo, "Bash", {"command": "git switch feat"}))), "allow")
        check("same file", decision(run(pre(repo, "Edit", {"file_path": str(repo / "auth.ts")}))), "allow")

        print("\n[E] worktrees isolate — except the stash, which they do not")
        repo = make_repo(base / "wt")
        worktree = repo / ".claude" / "worktrees" / "feat"
        git(repo, "worktree", "add", "-q", str(worktree), "-b", "feat")
        claim(repo, "other", repo, [repo / "auth.ts"])
        check("switch inside my worktree", decision(run(pre(worktree, "Bash", {"command": "git switch other"}))), "allow")
        check("add -A inside my worktree", decision(run(pre(worktree, "Bash", {"command": "git add -A"}))), "allow")
        check("same-named file in my worktree", decision(run(pre(worktree, "Edit", {"file_path": str(worktree / "auth.ts")}))), "allow")
        check("stash is repo-wide", decision(run(pre(worktree, "Bash", {"command": "git stash"}))), "deny")
        check(
            "reaching back into the main checkout",
            decision(run(pre(worktree, "Bash", {"command": f'git -C "{repo}" switch feat'}))),
            "deny",
        )

        print("\n[F] modes")
        repo = make_repo(base / "modes")
        claim(repo, "other", repo, [repo / "auth.ts"])
        check("off", decision(run(pre(repo, "Bash", {"command": "git switch feat"}), {"CLAUDE_PARALLEL_GUARD": "off"})), "allow")
        check(
            "strict denies an unrelated file",
            decision(run(pre(repo, "Edit", {"file_path": str(repo / "billing.ts")}), {"CLAUDE_PARALLEL_GUARD": "strict"})),
            "deny",
        )
        check(
            "balanced allows it",
            decision(run(pre(repo, "Edit", {"file_path": str(repo / "billing.ts")}))),
            "allow",
        )

        print("\n[G] it fails open on everything it cannot read")
        loose = base / "not-a-repo"
        loose.mkdir()
        check("no git repository", decision(run(pre(loose, "Bash", {"command": "git switch feat"}))), "allow")
        check("no cwd", decision(run({"session_id": "mine", "hook_event_name": "PreToolUse", "tool_name": "Bash"})), "allow")
        repo = make_repo(base / "junk")
        (repo / ".git" / REGISTRY).mkdir(parents=True, exist_ok=True)
        (repo / ".git" / REGISTRY / "broken.json").write_text("{not json", encoding="utf-8")
        check("unreadable claim", decision(run(pre(repo, "Bash", {"command": "git switch feat"}))), "allow")

        print("\n[H] it stands down where the repo ships its own guard")
        repo = make_repo(base / "hasguard")
        (repo / ".claude").mkdir()
        (repo / ".claude" / "settings.json").write_text(
            json.dumps(
                {
                    "hooks": {
                        "PreToolUse": [
                            {
                                "matcher": "Bash",
                                "hooks": [{"type": "command", "command": "node scripts/concurrent-writer-guard.mjs"}],
                            }
                        ]
                    }
                }
            ),
            encoding="utf-8",
        )
        claim(repo, "other", repo, [repo / "auth.ts"])
        check("floor-mover", decision(run(pre(repo, "Bash", {"command": "git switch feat"}))), "allow")
        check("no claim written", (repo / ".git" / REGISTRY / "mine.json").is_file(), False)

        print("\n[I] session lifecycle")
        repo = make_repo(base / "lifecycle")
        quiet = run({"session_id": "mine", "hook_event_name": "SessionStart", "cwd": str(repo)})
        check("SessionStart says nothing when alone", quiet, {})
        claim(repo, "other", repo, [repo / "auth.ts"])
        noisy = run({"session_id": "mine", "hook_event_name": "SessionStart", "cwd": str(repo)})
        check(
            "SessionStart warns when contended",
            "EnterWorktree" in ((noisy.get("hookSpecificOutput") or {}).get("additionalContext") or ""),
            True,
        )
        run(pre(repo, "Edit", {"file_path": str(repo / "mine.ts")}))
        check("claim exists", (repo / ".git" / REGISTRY / "mine.json").is_file(), True)
        run({"session_id": "mine", "hook_event_name": "SessionEnd", "cwd": str(repo)})
        check("SessionEnd releases it", (repo / ".git" / REGISTRY / "mine.json").is_file(), False)

        print("\n[J] a denial explains itself")
        repo = make_repo(base / "message")
        claim(repo, "other", repo, [repo / "auth.ts"])
        reason = (run(pre(repo, "Bash", {"command": "git switch feat"}))["hookSpecificOutput"])["permissionDecisionReason"]
        check("names the remedy", "EnterWorktree" in reason, True)
        check("names the escape hatch", "CLAUDE_PARALLEL_GUARD=off" in reason, True)
        check("names the other session", "other"[:8] in reason, True)
        collision = (run(pre(repo, "Edit", {"file_path": str(repo / "auth.ts")}))["hookSpecificOutput"])["permissionDecisionReason"]
        check("collision lists the other session's files", "auth.ts" in collision, True)

        print("\n[K] a --repo install does not mistake itself for someone else's guard")
        repo = make_repo(base / "repoinstall")
        subprocess.run(
            [sys.executable, str(GUARD.parent / "install.py"), "--repo", str(repo)],
            check=True,
            capture_output=True,
        )
        claim(repo, "other", repo, [repo / "auth.ts"])
        check("still denies", decision(run(pre(repo, "Bash", {"command": "git switch feat"}))), "deny")

    print()
    if failures:
        print(f"{len(failures)} failing: {', '.join(failures)}")
        return 1
    print("all green")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
