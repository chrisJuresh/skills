#!/usr/bin/env python3
"""Checks for worktree_owner.py, against real git worktrees in a temp directory.

Two categories, and the second is the one that matters:

  * **Denials that must happen.** Each is one shape the 2026-08-25 collision actually
    took — a file tool carrying an absolute path, a package runner naming nothing at all,
    a `cd`, a `-C`, a script run out of someone else's tree.
  * **Allows that must keep happening.** A hook that refuses a read, a sibling repo, the
    main checkout or a session's own scratch script is a hook someone turns off, and then
    nothing is enforced at all. Reading another tree is ordinary work and every one of
    those cases is asserted here.

    python test_worktree_owner.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

OWNER = Path(__file__).resolve().parent / "worktree_owner.py"

PASSED = 0
FAILED: list[str] = []


def git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    )


def run(payload: dict, env: dict | None = None) -> dict | None:
    """Feed the hook a payload; return its JSON output, or None for silence."""
    environment = {**os.environ, "CLAUDE_WORKTREE_OWNER": "on"}
    environment.pop("CLAUDE_WORKTREE_OWNER_TTL", None)
    environment.pop("CLAUDE_INTEGRATION_BRANCH", None)
    environment.pop("CLAUDE_WORKTREES_ROOT", None)
    environment.update(env or {})
    result = subprocess.run(
        [sys.executable, "-S", str(OWNER)],
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
    if "systemMessage" in (output or {}):
        return "warn"
    inner = output.get("hookSpecificOutput") or {}
    return inner.get("permissionDecision", "allow")


def reason(output: dict | None) -> str:
    return ((output or {}).get("hookSpecificOutput") or {}).get("permissionDecisionReason", "")


def context(output: dict | None) -> str:
    return ((output or {}).get("hookSpecificOutput") or {}).get("additionalContext", "")


def check(name: str, got, want) -> None:
    global PASSED
    if got == want:
        PASSED += 1
    else:
        FAILED.append(f"{name}: expected {want!r}, got {got!r}")


def claim_for(repo: Path, tree: Path) -> Path:
    """Where the hook files `tree`'s claim — recomputed, not imported.

    The point of asserting on this path is that a reader can find it, so the test derives
    it the way the docs describe (state lives in the COMMON git dir, keyed by the
    worktree's leaf name) rather than calling the function under test and agreeing with
    whatever it says.
    """
    return repo / ".git" / "claude-worktree-gate" / "claims" / f"{tree.name}.json"


def edit(cwd: Path, path: Path, session: str, transcript: Path) -> dict:
    return {
        "session_id": session,
        "transcript_path": str(transcript),
        "hook_event_name": "PreToolUse",
        "cwd": str(cwd),
        "tool_name": "Edit",
        "tool_input": {"file_path": str(path)},
    }


def shell(cwd: Path, command: str, session: str, transcript: Path) -> dict:
    return {
        "session_id": session,
        "transcript_path": str(transcript),
        "hook_event_name": "PreToolUse",
        "cwd": str(cwd),
        "tool_name": "Bash",
        "tool_input": {"command": command},
    }


# ------------------------------------------------------------------------ fixtures


def build(root: Path) -> tuple[Path, Path, Path]:
    """A repo with `development` as its integration branch, and two worktrees."""
    main = root / "repo"
    main.mkdir()
    git(main, "init", "-b", "main")
    git(main, "config", "user.email", "t@example.com")
    git(main, "config", "user.name", "t")
    (main / "package.json").write_text("{}\n", encoding="utf-8")
    git(main, "add", "package.json")
    git(main, "commit", "-m", "init")
    git(main, "branch", "development")

    claude = main / ".claude"
    claude.mkdir()
    (claude / "worktree-per-change.json").write_text(
        json.dumps({"integrationBranch": "development"}), encoding="utf-8"
    )

    first = main / ".claude" / "worktrees" / "first"
    git(main, "worktree", "add", str(first), "-b", "a-first", "development")
    (first / "src").mkdir()
    (first / "src" / "app.tsx").write_text("x\n", encoding="utf-8")

    second = main / ".claude" / "worktrees" / "second"
    git(main, "worktree", "add", str(second), "-b", "a-second", "development")

    return main, first, second


def build_foreign(root: Path) -> Path:
    """A second, unrelated repository — the one this hook has no business policing."""
    foreign = root / "foreign"
    foreign.mkdir()
    git(foreign, "init", "-b", "a-feature")
    git(foreign, "config", "user.email", "t@example.com")
    git(foreign, "config", "user.name", "t")
    (foreign / "a.txt").write_text("x\n", encoding="utf-8")
    git(foreign, "add", "a.txt")
    git(foreign, "commit", "-m", "init")
    return foreign


def main() -> int:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw).resolve()
        repo, first, second = build(root)
        foreign = build_foreign(root)

        # Stand-in transcripts. Their mtime is the liveness signal, so the test can age a
        # session out by touching one — which is the only way to exercise lapsing without
        # sleeping for 45 minutes.
        alive_a = root / "a.jsonl"
        alive_b = root / "b.jsonl"
        for path in (alive_a, alive_b):
            path.write_text("{}\n", encoding="utf-8")

        A, B = "sess-aaaa-1111", "sess-bbbb-2222"

        # ------------------------------------------------------------- claiming

        check(
            "A's first edit in a fresh worktree is allowed",
            decision(run(edit(first, first / "src" / "app.tsx", A, alive_a))),
            "allow",
        )
        check("...and files a claim", claim_for(repo, first).is_file(), True)
        claim = json.loads(claim_for(repo, first).read_text(encoding="utf-8"))
        check("the claim names the session", claim.get("session"), A)
        check("the claim names the tree", claim.get("tree"), str(first))
        check(
            "A's second edit is fine",
            decision(run(edit(first, first / "package.json", A, alive_a))),
            "allow",
        )

        # ------------------------------------------------------- a second session

        for name, payload in [
            ("an Edit into A's tree", edit(first, first / "package.json", B, alive_b)),
            ("a package runner sitting in A's tree", shell(first, "pnpm dev", B, alive_b)),
            ("a `cd` into A's tree", shell(repo, f"cd {first} && pnpm dev", B, alive_b)),
            ("a runner pointed at A's tree with -C", shell(repo, f"pnpm -C {first} dev", B, alive_b)),
            ("a script that lives in A's tree", shell(repo, f"node {first}/package.json", B, alive_b)),
            ("an rm inside A's tree", shell(repo, f"rm {first}/package.json", B, alive_b)),
        ]:
            check(f"B is denied {name}", decision(run(payload)), "deny")

        denial = reason(run(edit(first, first / "package.json", B, alive_b)))
        check("the denial names the tree", str(first) in denial, True)
        check("the denial names the owning session", "sess" in denial, True)
        check(
            "the denial's remedy uses the repo's integration branch",
            "origin/development" in denial,
            True,
        )
        check(
            "the denial's remedy uses the default worktrees root",
            ".claude/worktrees/<name>" in denial,
            True,
        )
        check("the denial offers --release", "--release" in denial, True)
        check(
            "a denied `cd` says reading in place is still fine",
            "Read it in place" in reason(run(shell(repo, f"cd {first}", B, alive_b))),
            True,
        )

        # A repo that records a different layout gets a remedy it can actually carry out.
        config = repo / ".claude" / "worktree-per-change.json"
        config.write_text(
            json.dumps({"integrationBranch": "trunk", "worktreesRoot": "../trees"}),
            encoding="utf-8",
        )
        moved = reason(run(edit(first, first / "package.json", B, alive_b)))
        check("the remedy follows integrationBranch", "origin/trunk" in moved, True)
        check("the remedy follows worktreesRoot", "../trees/<name>" in moved, True)
        config.write_text(
            json.dumps({"integrationBranch": "development"}), encoding="utf-8"
        )

        # -------------------------------- what B is still allowed, the half that matters

        for name, payload in [
            ("reading a file in A's tree", shell(repo, f"cat {first}/package.json", B, alive_b)),
            ("grepping A's tree", shell(repo, f"grep -rn foo {first}/src", B, alive_b)),
            ("diffing the two trees", shell(repo, f"diff {first}/package.json {second}/package.json", B, alive_b)),
            ("git in A's tree, which is the guard's business", shell(repo, f"git -C {first} log --oneline", B, alive_b)),
            ("editing the main checkout, also the guard's business", edit(repo, repo / "package.json", B, alive_b)),
            ("a package runner in the main checkout", shell(repo, "pnpm install", B, alive_b)),
            ("its own scratch script, run from anywhere", shell(repo, f"node {root}/shoot.mjs", B, alive_b)),
            ("an unrelated repository entirely", shell(foreign, "pnpm test", B, alive_b)),
            ("editing in an unrelated repository", edit(foreign, foreign / "a.txt", B, alive_b)),
            ("its OWN worktree", edit(second, second / "package.json", B, alive_b)),
        ]:
            check(f"B may still: {name}", decision(run(payload)), "allow")

        check(
            "a claim in one repo does not reach into another",
            claim_for(foreign, foreign).exists(),
            False,
        )

        # ------------------------------------------------------------- presence

        claim_for(repo, first).unlink()
        run(shell(first, "cat package.json", "sess-reader", alive_a))
        check(
            "a read from inside a tree claims it — presence is the honest signal",
            json.loads(claim_for(repo, first).read_text(encoding="utf-8")).get("session"),
            "sess-reader",
        )
        claim_for(repo, first).unlink()
        run(edit(first, first / "package.json", A, alive_a))

        # ------------------------------------------------------------- lapsing

        old = time.time() - 60 * 60 * 24
        os.utime(alive_a, (old, old))
        check(
            "B takes a tree whose owner has gone quiet",
            decision(run(edit(first, first / "package.json", B, alive_b))),
            "allow",
        )
        check(
            "...and the claim moves to B",
            json.loads(claim_for(repo, first).read_text(encoding="utf-8")).get("session"),
            B,
        )
        # The other half of the pair: a short TTL must not be the only thing keeping the
        # denial alive, and a long one must not resurrect a claim the owner has released.
        os.utime(alive_b, None)
        check(
            "a live claim still denies with a generous TTL",
            decision(run(edit(first, first / "package.json", A, alive_a),
                         env={"CLAUDE_WORKTREE_OWNER_TTL": "86400"})),
            "deny",
        )
        # The floor, asserted rather than asserted through. `CLAUDE_WORKTREE_OWNER_TTL` is
        # clamped up to 60 seconds, so a `TTL=1` against a claim touched a moment ago is
        # still a live claim — an operator who sets it low has not accidentally disabled
        # the hook, and a test that expected `allow` here would pass only by way of a
        # timing race.
        check(
            "a TTL below the 60-second floor is clamped to it, not honoured",
            decision(run(edit(first, first / "package.json", A, alive_a),
                         env={"CLAUDE_WORKTREE_OWNER_TTL": "1"})),
            "deny",
        )
        idle = time.time() - 120
        os.utime(alive_b, (idle, idle))
        check(
            "a short TTL frees a tree whose owner has been idle past it",
            decision(run(edit(first, first / "package.json", A, alive_a),
                         env={"CLAUDE_WORKTREE_OWNER_TTL": "60"})),
            "allow",
        )
        # Hand the tree back to A for the rest of the suite.
        claim_for(repo, first).unlink()
        os.utime(alive_a, None)
        run(edit(first, first / "package.json", A, alive_a))

        # ------------------------------------------------------------- the switches

        check(
            "CLAUDE_WORKTREE_OWNER=off",
            decision(run(edit(first, first / "package.json", B, alive_b),
                         env={"CLAUDE_WORKTREE_OWNER": "off"})),
            "allow",
        )
        check(
            "CLAUDE_WORKTREE_OWNER=warn reports instead of denying",
            decision(run(edit(first, first / "package.json", B, alive_b),
                         env={"CLAUDE_WORKTREE_OWNER": "warn"})),
            "warn",
        )

        # --------------------------------------- garbage in, silence out: it fails OPEN

        for name, payload in [
            ("a command shlex cannot lex", shell(repo, 'echo "unterminated', B, alive_b)),
            ("a shell payload held in one quoted token", shell(repo, f"bash -lc 'cd {first} && pnpm dev'", B, alive_b)),
        ]:
            check(f"fails open: {name}", decision(run(payload)), "allow")
        for name, raw in [
            ("an unparseable payload", "not json"),
            ("no cwd", '{"hook_event_name":"PreToolUse"}'),
            ("an unknown event", '{"hook_event_name":"PreCompact","cwd":"%s"}' % root),
        ]:
            result = subprocess.run(
                [sys.executable, "-S", str(OWNER)], input=raw,
                capture_output=True, text=True, env={**os.environ},
            )
            check(f"fails open: {name}", (result.returncode, result.stdout.strip()), (0, ""))

        # ------------------------- the escape hatch is not denied by the thing it escapes

        # Until 2026-08-26 running the `--release` the denial prints was itself denied:
        # the lexer saw the tree path as an argument and called it a write target long
        # before `main()` reached the `--release` branch, so the only way out was a
        # spelling the message did not print.
        for name, command in [
            ("by absolute path, the form the denial prints", f"python3 {OWNER} --release {first}"),
            ("run from inside the tree, where a denied session usually sits", f"python3 {OWNER} --release {first}"),
            ("by leaf name, which release() also takes", f"python3 {OWNER} --release first"),
        ]:
            where = first if "inside" in name else repo
            check(
                f"B may release A's tree {name}",
                decision(run(shell(where, command, B, alive_b))),
                "allow",
            )
        # ...and the exemption is this script plus the flag, nothing looser. Each of these
        # still names A's tree with no valid `--release` of it, so each is still a write.
        for name, command in [
            ("the same script without --release", f"python3 {OWNER} {first}"),
            ("--release handed to some other script", f"python3 {root}/other.py --release {first}"),
            ("a command that merely says the word", f"node --release {first}/package.json"),
            ("a release riding alongside a real write", f"python3 {OWNER} --release {first} && pnpm -C {first} dev"),
        ]:
            check(f"still denied: {name}", decision(run(shell(repo, command, B, alive_b))), "deny")

        # --release itself, run as a command rather than lexed.
        subprocess.run([sys.executable, "-S", str(OWNER), "--release", str(first)],
                       capture_output=True, text=True, cwd=str(repo))
        check("--release removes the claim", claim_for(repo, first).exists(), False)
        run(edit(first, first / "package.json", A, alive_a))
        subprocess.run([sys.executable, "-S", str(OWNER), "--release", "first"],
                       capture_output=True, text=True, cwd=str(repo))
        check("--release by leaf name removes it too", claim_for(repo, first).exists(), False)

        # ------------------------------------------------------------- SessionStart

        run(edit(first, first / "package.json", A, alive_a))
        start = run({
            "session_id": B,
            "transcript_path": str(alive_b),
            "hook_event_name": "SessionStart",
            "cwd": str(repo),
        })
        body = context(start)
        check("the report names the held tree", str(first) in body, True)
        check("the report names the free one", str(second) in body, True)
        check("the report says who holds it", "held by session" in body, True)
        check("the report says how to cut one", "git worktree add" in body, True)
        mine = context(run({
            "session_id": A,
            "transcript_path": str(alive_a),
            "hook_event_name": "SessionStart",
            "cwd": str(repo),
        }))
        check("a session's own tree is marked as its own", "**yours**" in mine, True)
        # A session starting up INSIDE a tree it does not own is the shape that produced
        # the incident, and it is worth saying before the first denial rather than after.
        sitting = context(run({
            "session_id": B,
            "transcript_path": str(alive_b),
            "hook_event_name": "SessionStart",
            "cwd": str(first),
        }))
        check(
            "a session sitting in a foreign tree is warned at startup",
            "the first write will be denied" in sitting,
            True,
        )
        check(
            "...and being warned does not steal the claim",
            json.loads(claim_for(repo, first).read_text(encoding="utf-8")).get("session"),
            A,
        )
        check(
            "a repository with no worktrees reports nothing",
            context(run({
                "session_id": B,
                "transcript_path": str(alive_b),
                "hook_event_name": "SessionStart",
                "cwd": str(foreign),
            })),
            "",
        )

        # A registered tree that has been removed but not pruned must not appear as a
        # phantom row: `git worktree remove` leaves the register entry until a prune.
        shutil.rmtree(second)
        pruned = context(run({
            "session_id": B,
            "transcript_path": str(alive_b),
            "hook_event_name": "SessionStart",
            "cwd": str(repo),
        }))
        check("a deleted-but-unpruned tree is not reported", str(second) in pruned, False)

    print(f"{PASSED} passed, {len(FAILED)} failed")
    for line in FAILED:
        print(f"  FAIL  {line}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    if shutil.which("git") is None:
        print("git is not on PATH")
        raise SystemExit(1)
    raise SystemExit(main())
