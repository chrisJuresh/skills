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


def reason(output: dict | None) -> str:
    return ((output or {}).get("hookSpecificOutput") or {}).get("permissionDecisionReason", "")


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


def build_foreign(root: Path) -> Path:
    """A second, unrelated repository — the one the guard has no business policing.

    Deliberately shaped like the checkout that produced the bug: on a feature branch, and
    with **no `development` branch at all**, so a denial's own remedy ("cut a worktree off
    `origin/development`") would be impossible to carry out there.
    """
    foreign = root / "foreign"
    foreign.mkdir()
    git(foreign, "init", "-b", "a-feature")
    git(foreign, "config", "user.email", "t@example.com")
    git(foreign, "config", "user.name", "t")
    (foreign / "a.txt").write_text("x\n", encoding="utf-8")
    git(foreign, "add", "a.txt")
    git(foreign, "commit", "-m", "init")
    return foreign


def build(root: Path) -> tuple[Path, Path, Path, Path]:
    """A repo with `development` as its integration branch, plus three worktrees."""
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

    # A path with a space in it, which only a parser keeping quoted arguments whole can
    # read: `cd "/a b/tree"` used to arrive as two tokens and resolve to neither.
    spaced = main / ".claude" / "worktrees" / "with space"
    git(main, "worktree", "add", str(spaced), "-b", "a-spaced-topic", "development")

    return main, topic, onbase, spaced


def worktree_at(repo: Path, name: str, branch: str, merged: bool = True) -> Path:
    """A worktree at `<repo>/.claude/worktrees/<name>`, by default one that has merged.

    `merged=True` runs the merge through the guard rather than writing the marker file
    directly: the marker's shape is the guard's business, and a fixture that hand-rolls it
    passes while the real thing is broken.
    """
    tree = repo / ".claude" / "worktrees" / name
    git(repo, "worktree", "add", str(tree), "-b", branch, "development")
    if merged:
        run(shell(tree, "gh pr merge --squash --delete-branch"))
    return tree


# --------------------------------------------------------------------------- cases


def main() -> int:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        repo, topic, onbase, spaced = build(root)

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

        # --- scoped to the repository the operation targets ---------------------
        # The bug this closes: a session whose cwd happened to be inside a guarded repo
        # had every `git` mutator denied even when the command operated on a different
        # checkout — while `Write` to that same other checkout was allowed, which is what
        # showed the asymmetry was accidental. A denial there is unfollowable as well as
        # wrong: the remedy it prints names a branch the other repo does not have.
        foreign = build_foreign(root)
        for command in (
            f"cd {foreign} && git add -A",
            f"cd {foreign} && git checkout a.txt",
            f"cd {foreign} && git commit -m x",
            f"git -C {foreign} add -A",
            f"cd {foreign} && git stash",
            "cd ../foreign && git add -A",
        ):
            check(
                f"`{command}` targets another repository and is allowed",
                decision(run(shell(repo, command))),
                "allow",
            )
        check(
            "a `cd` into a directory that is no repository at all is allowed",
            decision(run(shell(repo, f"cd {root} && git add -A"))),
            "allow",
        )
        check(
            "a write into another repository is allowed",
            decision(run(write(repo, str(foreign / "a.txt")))),
            "allow",
        )

        # The other direction, which is the half that must not be weakened: naming the
        # guarded tree from somewhere else does not buy an exemption.
        check(
            "a `cd` into this repository's own main checkout is still denied",
            decision(run(shell(repo, f"cd {repo} && git reset --hard"))),
            "deny",
        )
        check(
            "`cd` back into the main checkout from a worktree is denied",
            decision(run(shell(topic, f"cd {repo} && git commit -m x"))),
            "deny",
        )
        check(
            "`git -C <main checkout>` from a worktree is denied",
            decision(run(shell(topic, f"git -C {repo} add ."))),
            "deny",
        )
        check(
            "a foreign first call does not excuse a second one that comes home",
            decision(run(shell(repo, f"cd {foreign} && git add -A && git -C {repo} commit -m x"))),
            "deny",
        )
        check(
            "a write back into the main checkout from a worktree is denied",
            decision(run(write(topic, str(repo / "README.md")))),
            "deny",
        )

        # A target this hook cannot read means the session's own tree — the reading that
        # keeps every ordinary command behaving exactly as it did.
        check(
            "an unexpandable `cd` falls back to the session's own tree",
            decision(run(shell(repo, 'cd "$OTHER" && git add -A'))),
            "deny",
        )
        check(
            '`git -C "$W"` still reads as the session\'s own tree',
            decision(run(shell(repo, 'git -C "$W" commit -m x'))),
            "deny",
        )

        # A worktree of this repository is where work happens, whichever directory the
        # command was issued from.
        check(
            "`cd <worktree>` from the main checkout is allowed",
            decision(run(shell(repo, f"cd {topic} && git commit -m x"))),
            "allow",
        )
        check(
            "`git -C <worktree>` from the main checkout is allowed",
            decision(run(shell(repo, f"git -C {topic} add README.md"))),
            "allow",
        )
        check(
            "a write into a worktree, named from the main checkout, is allowed",
            decision(run(write(repo, str(topic / "README.md")))),
            "allow",
        )
        check(
            "a write into a worktree that is on the integration branch is denied from anywhere",
            decision(run(write(topic, str(onbase / "README.md")))),
            "deny",
        )
        check(
            "another worktree's stash is still this repository's one stash stack",
            decision(run(shell(topic, f"cd {onbase} && git stash"))),
            "deny",
        )

        # --- shell, not prose that looks like it --------------------------------
        # The parser tokenizes before it looks for command boundaries. Splitting raw text
        # on `&&`, `|` and newlines first read the inside of a quoted argument as shell:
        # measured on 2026-08-13, a `gh pr create --body "…"` whose body held the line
        # `cd ~/x && git add -A` and a markdown table of pipes was denied as a `git add` in
        # the main checkout, and `--body-file` was the workaround. Both halves need cover —
        # the false positive that is fixed, and the real commands that must keep being seen.
        # Every line here is load-bearing: it takes an operator *inside* the quotes to
        # reproduce the bug. The raw-text split cuts the quoted string in half, each half
        # is left with one unbalanced quote, `shlex.split` raises on both, and the bare
        # `segment.split()` fallback then exposes the prose word by word.
        body = (
            "## What changed\n"
            "\n"
            "The install step is now `cd ~/x && git add -A`, which used to be manual.\n"
            "Do not reach for git stash here || git reset --hard, both lose work.\n"
            "\n"
            "| case | before | after |\n"
            "| --- | --- | --- |\n"
            "| clean tree | manual | automatic |\n"
        )
        # From the main checkout the body's `git add`, and from a worktree its `git stash`,
        # are both commands the guard really would deny — so an allow here is the parser
        # telling prose from shell, not the rule standing down.
        for cwd, place in ((repo, "the main checkout"), (topic, "a worktree")):
            check(
                f"a quoted PR body is not the commands it describes, from {place}",
                decision(run(shell(cwd, f'gh pr create --base development --title x --body "{body}"'))),
                "allow",
            )
        # `git stash` is denied in worktrees too, so a message about it was denied in the one
        # place all the work happens — the same bug with no `--body-file` to escape to.
        for message in (
            "wip && git stash instead",
            "see the table | git stash | done",
            "line one\ngit stash is banned",
        ):
            check(
                f"a commit message mentioning git is not a git call ({message!r})",
                decision(run(shell(topic, f'git commit -m "{message}"'))),
                "allow",
            )

        # The other half: everything the old raw-text split caught has to stay caught.
        for command in ("echo x&&git commit -m y", "true;git add -A", "git status|grep x&&git reset --hard"):
            check(
                f"`{command}` is still seen without spaces around the operator",
                decision(run(shell(repo, command))),
                "deny",
            )
        # A newline is whitespace to shlex, which would have erased both boundaries here.
        # The lexer is handed `\n` as punctuation instead, so line two is its own command
        # and the `cd` on line one still carries into it.
        check(
            "a git call on the next line is still seen",
            decision(run(shell(repo, "git status --short\ngit add -A"))),
            "deny",
        )
        check(
            "a `cd` on the line before still carries into the next command",
            decision(run(shell(topic, f"cd {repo}\ngit commit -m x"))),
            "deny",
        )
        check(
            "...including when it carries the call out of this repository",
            decision(run(shell(repo, f"cd {foreign}\ngit add -A"))),
            "allow",
        )
        # Reading only the bare spelling would make the whole guard one quote deep.
        for command in ('"git" add -A', "'git' commit -m x"):
            check(
                f"`{command}` is not laundered by quoting the command name",
                decision(run(shell(repo, command))),
                "deny",
            )
        check(
            "a quoted path with a space in it reads as one path",
            decision(run(shell(repo, f'cd "{spaced}" && git commit -m x'))),
            "allow",
        )
        check(
            "`git -C` takes a quoted path too",
            decision(run(shell(repo, f'git -C "{spaced}" add README.md'))),
            "allow",
        )
        check(
            "...and quoting does not launder a target back into the main checkout",
            decision(run(shell(topic, f'git -C "{repo}" add README.md'))),
            "deny",
        )
        # An unbalanced quote is the one input shlex refuses outright, and the raw-text
        # split is kept for it: over-reporting boundaries costs a false denial, where
        # declining to read the text would cost a missed one. Only the first is a failure
        # a guard may have.
        for command in ('git commit -m "unclosed', "echo 'x && git add -A"):
            check(
                f"unlexable text falls back to the older reading ({command!r})",
                decision(run(shell(repo, command))),
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
        # --- the escape hatch names an action that exists -----------------------
        # `CLAUDE_WORKTREE_GATE` is read from the hook's environment, so a session cannot
        # set it: a `CLAUDE_WORKTREE_GATE=off git add …` prefix reaches the command, not
        # the hook that already vetted it. A message telling the reader to do that spends
        # a turn proving it does not work, which is worse than saying "ask the operator".
        denial = reason(run(write(repo, str(repo / "README.md"))))
        check(
            "the denial does not tell a session to set a variable it cannot set",
            "cannot turn this guard off" in denial and "operator" in denial,
            True,
        )
        check(
            "it names what the operator would have to do, and that it needs a new session",
            "new session" in denial,
            True,
        )
        check(
            "and it names the move that is actually available: say so, and stop",
            "say so plainly in your reply" in denial,
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

        # --- cleanup, the other half of finishing --------------------------------
        # Delivery had two hooks and the teardown had a paragraph in a doc, so the
        # worktrees piled up: the change lands, the reply is truthful, and a stale
        # checkout plus a live push target stay behind for the next session to work
        # out the status of.
        landed = worktree_at(repo,"landed-tree", "dev/landed")
        stopped = run({"session_id": "c1", "hook_event_name": "Stop", "cwd": str(landed)})
        check("Stop blocks in a worktree whose PR merged", decision(stopped), "block")
        # A Stop block carries its message at the top level, not in the
        # `permissionDecisionReason` the `reason()` helper reads, so it is pulled out
        # by hand here — and under a name of its own, since binding `reason` would
        # shadow that helper for the whole function.
        teardown = (stopped or {}).get("reason", "")
        check(
            "the block says how to take the tree down",
            "git worktree remove" in teardown and "git branch -D dev/landed" in teardown,
            True,
        )
        check(
            # `action: "remove"` refuses on a worktree EnterWorktree only entered, which is
            # every worktree here. A teardown message that recommends it costs the session a
            # round trip at the one moment it is trying to stop.
            "it names the ExitWorktree action that can take the tree down",
            'action: "keep"' in teardown and '"remove"' in teardown,
            True,
        )
        for _ in range(2):
            run({"session_id": "c2", "hook_event_name": "Stop", "cwd": str(landed)})
        check(
            "it gives up rather than trapping a session that will not clean up",
            decision(run({"session_id": "c2", "hook_event_name": "Stop", "cwd": str(landed)})),
            "allow",
        )

        swept = run({"session_id": "c3", "hook_event_name": "SessionStart", "cwd": str(repo)})
        check(
            "SessionStart reports a landed worktree still on disk",
            str(landed) in json.dumps(swept or {}),
            True,
        )

        # A marker is named after the worktree's LEAF NAME, so it outlives the tree.
        # Left in place it denies the first edit in the next worktree to take that
        # name — a fresh checkout told its change has already landed.
        git(repo, "worktree", "remove", "--force", str(landed))
        git(repo, "branch", "-D", "dev/landed")
        run({"session_id": "c4", "hook_event_name": "SessionStart", "cwd": str(repo)})
        recycled = worktree_at(repo,"landed-tree", "dev/landed-again", merged=False)
        check(
            "a reused worktree name is not spent",
            decision(run(write(recycled, str(recycled / "README.md")))),
            "allow",
        )

        # The sweep must not amount to forgetting everything: reusing the tree that
        # merged is the failure the marker exists for.
        still = worktree_at(repo,"still-spent", "dev/still-spent")
        run({"session_id": "c5", "hook_event_name": "SessionStart", "cwd": str(repo)})
        check(
            "the branch that actually merged is still refused",
            decision(run(write(still, str(still / "README.md")))),
            "deny",
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
