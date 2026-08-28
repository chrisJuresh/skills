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


def names(output: dict | None, path: Path) -> bool:
    """Does the guard's output name `path`?

    The path is escaped the way the output already escaped it, rather than matched raw.
    `json.dumps` doubles a backslash, so on Windows `str(path) in json.dumps(output)` is
    False however plainly the path is named — and the assertions that ask this question
    are split between expecting True and expecting False, so a raw comparison does not
    fail honestly on that platform: it fails the two that expect a tree to be reported
    and passes the one that expects silence for the wrong reason. A sweep that had
    stopped reporting anything at all would have looked the same.
    """
    return json.dumps(str(path))[1:-1] in json.dumps(output or {})


def spent_marker_for(repo: Path, tree: Path) -> Path:
    """Where the guard writes `tree`'s spent marker — recomputed, not imported.

    The point of asserting the denial spells this path out is that a session can act on it,
    so the test derives it the same way a reader would (state lives in the COMMON git dir,
    keyed by the worktree's leaf name) rather than calling the function under test and
    agreeing with whatever it says.
    """
    return repo / ".git" / "claude-worktree-gate" / "spent" / f"{tree.name}.json"


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


def build_published(root: Path) -> tuple[Path, Path, Path, Path]:
    """A repo with a real `origin`, whose default branch has diverged from its
    integration branch — plus the three worktrees that tell the `Stop` gate's cases apart.

    The divergence is the whole fixture. `origin/development..HEAD` counts it in every
    worktree cut from `main`, which is what the harness cuts, so a gate reading only that
    fired at every `Stop` in a session that had delivered everything it did — and the five
    steps it then prescribed would have squash-merged the divergence onto `development`.

    A real bare remote rather than a hand-written `refs/remotes/…`: the whole question is
    what git considers published, and a faked ref would let a broken `--not --remotes`
    keep passing.
    """
    remote = root / "origin.git"
    git(root, "init", "--bare", "-q", str(remote))

    published = root / "published"
    published.mkdir()
    git(published, "init", "-b", "main")
    git(published, "config", "user.email", "t@example.com")
    git(published, "config", "user.name", "t")
    (published / "README.md").write_text("hello\n", encoding="utf-8")
    git(published, "add", "README.md")
    git(published, "commit", "-m", "init")
    git(published, "branch", "development")
    # The commit `main` has and `development` has not — published, and nobody's to deliver.
    (published / "main-only.txt").write_text("main moved on\n", encoding="utf-8")
    git(published, "add", "main-only.txt")
    git(published, "commit", "-m", "main moves on")

    claude = published / ".claude"
    claude.mkdir()
    (claude / "worktree-per-change.json").write_text(
        json.dumps({"integrationBranch": "development"}), encoding="utf-8"
    )
    git(published, "remote", "add", "origin", str(remote))
    git(published, "push", "-q", "origin", "main", "development")

    # What the harness cuts when nobody passes it a base: a tree on the DEFAULT branch.
    harness = published / ".claude" / "worktrees" / "harness"
    git(published, "worktree", "add", str(harness), "-b", "claude/objective-lalande", "main")

    # Real undelivered work: a commit on no remote at all.
    unpushed = published / ".claude" / "worktrees" / "unpushed"
    git(published, "worktree", "add", str(unpushed), "-b", "a-real-topic", "development")
    (unpushed / "work.txt").write_text("the work\n", encoding="utf-8")
    git(unpushed, "add", "work.txt")
    git(unpushed, "commit", "-m", "the work")

    # Pushed, and still not on the integration branch — the other real case, and the one
    # a bare `HEAD --not --remotes` would go quiet on.
    onremote = published / ".claude" / "worktrees" / "onremote"
    git(published, "worktree", "add", str(onremote), "-b", "a-pushed-topic", "development")
    (onremote / "pushed.txt").write_text("pushed\n", encoding="utf-8")
    git(onremote, "add", "pushed.txt")
    git(onremote, "commit", "-m", "pushed work")
    git(onremote, "push", "-q", "-u", "origin", "HEAD")

    return published, harness, unpushed, onremote


def leave_unfinished_rebase(onbase: Path, tree: Path) -> None:
    """Leave a real, unfinished rebase in `tree` — the state a refused merge is fixed from.

    Two commits are genuinely collided rather than `rebase-merge` being created by hand.
    What the guard reads is git's own in-progress state, so a fixture that faked the
    directory would keep passing while the real detection was broken — the same reason
    `worktree_at` runs the merge through the guard instead of writing the marker itself.
    """
    (tree / "clash.txt").write_text("the topic's line\n", encoding="utf-8")
    git(tree, "add", "clash.txt")
    git(tree, "commit", "-m", "topic side")
    # `onbase` is the worktree sitting on the integration branch, so this is the collision
    # arriving from the base — which is what a rebase after a refused merge runs into.
    (onbase / "clash.txt").write_text("the base's line\n", encoding="utf-8")
    git(onbase, "add", "clash.txt")
    git(onbase, "commit", "-m", "base side")
    # Exits non-zero on the conflict, which is the entire point — so no `check=True`.
    subprocess.run(
        ["git", "-C", str(tree), "rebase", "development"],
        capture_output=True,
        text=True,
    )


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

        # --- where the remedy sends you ----------------------------------------
        # The path in the remedy is the one thing the guard says that a session then
        # ACTS on, and it is the one thing the guard cannot verify: whether a directory
        # is a worktree is a stat on `.git`, so a wrong path here denies nothing and
        # breaks nothing — it just sends the next session to make untracked files in a
        # repo that does not ignore them. Hence a config key, and hence these two.
        check(
            "the remedy names the default worktrees root when the repo says nothing",
            ".claude/worktrees/<name>" in reason(run(write(repo, str(repo / "README.md")))),
            True,
        )
        config = repo / ".claude" / "worktree-per-change.json"
        config.write_text(
            json.dumps({"integrationBranch": "development",
                        "worktreesRoot": "../trees/repo"}),
            encoding="utf-8",
        )
        relocated = reason(run(write(repo, str(repo / "README.md"))))
        check(
            "a repo that puts its worktrees elsewhere gets its own path in the remedy",
            "../trees/repo/<name>" in relocated,
            True,
        )
        check(
            "and the path it has ruled out is not also offered",
            ".claude/worktrees/<name>" in relocated,
            False,
        )
        # Restored, because every check after this one reads the default remedy.
        config.write_text(
            json.dumps({"integrationBranch": "development"}), encoding="utf-8"
        )

        # --- a protected merge target ------------------------------------------
        # The other half of the same rule land.py enforces. Without this, opting a repo
        # in would only redirect the well-behaved path and leave `gh pr merge` typed by
        # hand as an open shortcut straight onto the trunk.
        config.write_text(
            json.dumps({"integrationBranch": "development",
                        "protectedMergeTargets": ["development"]}),
            encoding="utf-8",
        )
        check(
            "`gh pr merge` into a protected branch is denied",
            decision(run(shell(topic, "gh pr merge --squash"))),
            "deny",
        )
        denial = reason(run(shell(topic, "gh pr merge --squash")))
        check("the denial names the branch", "development" in denial, True)
        check("and points at push-and-open instead", "gh pr create --base" in denial, True)
        check(
            "it is refused in the main checkout too, not only in a worktree",
            decision(run(shell(repo, "gh pr merge 12 --squash"))),
            "deny",
        )
        # A denied merge never ran, so the worktree must NOT be spent by it — spending it
        # here would strand a live change in a tree the guard then refuses to edit, and
        # the change would need a new worktree to finish something that never started.
        # Asked of a FRESH tree: `topic` was already spent by the allowed merge above,
        # and a spent tree would answer "deny" for a reason that has nothing to do with
        # this — which is exactly how this check would pass while the ordering was wrong.
        unspent = repo / ".claude" / "worktrees" / "unspent"
        git(repo, "worktree", "add", str(unspent), "-b", "unspent-topic", "development")
        check(
            "the fresh tree starts writable",
            decision(run(write(unspent, str(unspent / "README.md")))),
            "allow",
        )
        check(
            "the merge is denied there too",
            decision(run(shell(unspent, "gh pr merge --squash"))),
            "deny",
        )
        check(
            "a denied merge does not spend the worktree",
            decision(run(write(unspent, str(unspent / "README.md")))),
            "allow",
        )
        # `land.py` reaches the same merge, so the same refusal has to reach it. It was
        # invisible to the phrase-matching this check used to do, which would have left
        # the route SKILL.md recommends as the way past a rule the repo opted into.
        check(
            "the declared delivery script is refused too",
            decision(run(shell(unspent, "python .claude/scripts/land.py"))),
            "deny",
        )
        check(
            "and refusing it does not spend the tree either",
            decision(run(write(unspent, str(unspent / "README.md")))),
            "allow",
        )
        # The protected check reads the same parse as the mark, so prose costs nothing
        # here for the same reason it costs nothing there.
        check(
            "quoting the phrase is not attempting a merge",
            decision(run(shell(unspent, "grep -rn 'gh pr merge' docs/"))),
            "allow",
        )
        # Unrelated gh calls are untouched; this is not a block on `gh`.
        check(
            "`gh pr create` is unaffected",
            decision(run(shell(topic, "gh pr create --base development --fill"))),
            "allow",
        )
        # Restored, because every check after this one expects the unprotected default.
        config.write_text(
            json.dumps({"integrationBranch": "development"}), encoding="utf-8"
        )
        check(
            "with nothing configured `gh pr merge` is allowed again",
            decision(run(shell(topic, "gh pr merge --squash"))),
            "allow",
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
            names(swept, landed),
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

        # --- a teardown that deregistered and then could not delete -------------
        # `git worktree remove` drops the registration first and deletes the files second,
        # so a delete that fails — a held file, a `node_modules` nothing will let go —
        # leaves a directory git no longer knows about. Measured 2026-08-28 in the first
        # repository to adopt this guard: three of them under one `.claude/worktrees/`, one
        # a full checkout with dependencies installed, while `git worktree list` named only
        # the main checkout. The sweep had been asking every new session since to run the
        # command that had already run and now refuses with `is not a working tree`.
        gone = worktree_at(repo, "gone", "dev/gone")
        (gone / "node_modules").mkdir()
        (gone / ".git").unlink()
        git(repo, "worktree", "prune")
        left = run({"session_id": "c8", "hook_event_name": "SessionStart", "cwd": str(repo)})
        body = json.dumps(left or {})
        heading = body.find("no longer worktrees")
        check(
            "the sweep still names a directory git has let go of",
            names(left, gone),
            True,
        )
        check(
            # Under the other heading the remedy is `git worktree remove`, which is the one
            # command that cannot work here — so which list it lands in is the whole point.
            "and files it where the remedy is deleting the directory, not removing a worktree",
            heading != -1 and body.find(json.dumps(str(gone))[1:-1]) > heading,
            True,
        )
        check(
            "the marker does not survive a tree that is only a directory",
            spent_marker_for(repo, gone).exists(),
            False,
        )
        # The path is not reusable until somebody deletes the directory, which is the
        # remedy the new heading asks for; once they have, the name has to be free again.
        shutil.rmtree(gone)
        reused = worktree_at(repo, "gone", "dev/gone-again", merged=False)
        check(
            "so the next worktree to take that name is not born spent",
            decision(run(write(reused, str(reused / "README.md")))),
            "allow",
        )

        # --- a merge the forge refused ------------------------------------------
        # The marker goes down BEFORE `gh pr merge` runs, because no after-hook can tell a
        # merge from a merge that failed. That was called the harmless direction while only
        # the merging session saw it; the sweep and the Stop block then started reporting it
        # to everybody as fact. Measured in integration-console on 2026-08-13: a `DIRTY` PR
        # whose merge was refused, an unresolved rebase, ten modified files — announced to
        # every new session as merged, with a request to remove the tree. Conflict
        # resolution is the most expensive thing this hook could destroy.
        refused = worktree_at(repo, "refused-tree", "dev/refused")
        refused_write = run(write(refused, str(refused / "a.txt")))
        check(
            "a spent tree refuses a write while nothing is in progress",
            decision(refused_write),
            "deny",
        )

        # ...and that refusal has to carry its own doubt. `mid_operation` covers the common
        # shape — a conflict, so a rebase — but a merge refused for a FAILING CHECK leaves no
        # rebase and lands here, where the text is the only thing a session gets. Measured
        # 2026-08-13: a session read the old wording ("this worktree's change has already
        # landed") as fact, believed its work was delivered, and spent two turns reporting a
        # guard bug rather than clearing a marker. Three things make that recoverable, so
        # three checks — the claim is hedged, the forge is named as the arbiter, and the
        # marker path is spelled out.
        check(
            "the spent denial does not assert the merge landed",
            "has already landed" in reason(refused_write),
            False,
        )
        check(
            "the spent denial names the check that settles it",
            "gh pr view" in reason(refused_write),
            True,
        )
        check(
            "the spent denial names the marker to remove",
            str(spent_marker_for(repo, refused)) in reason(refused_write),
            True,
        )
        leave_unfinished_rebase(onbase, refused)
        check(
            "an unfinished rebase outranks the spent marker",
            decision(run(write(refused, str(refused / "a.txt")))),
            "allow",
        )
        mid = run({"session_id": "c6", "hook_event_name": "Stop", "cwd": str(refused)})
        check(
            "Stop does not tell a mid-rebase session its change was delivered",
            "recorded a merge" in ((mid or {}).get("reason", "")),
            False,
        )
        swept_mid = run({"session_id": "c7", "hook_event_name": "SessionStart", "cwd": str(repo)})
        check(
            "the sweep does not name a tree that is mid-rebase",
            names(swept_mid, refused),
            False,
        )
        # The other half of the pair: suppressing the in-progress tree must not suppress
        # the sweep itself, which is what a bare "does not name" assertion would allow.
        check(
            "the sweep still names a landed tree with nothing in progress",
            names(swept_mid, still),
            True,
        )

        # --- what counts as running the merge ------------------------------------
        # The mark used to be a regex over the whole command string, so a command that
        # merely *contained* the phrase spent the worktree. Measured 2026-08-22: a session
        # writing this protocol's own docs was denied its next edit, with no PR anywhere.
        quoting = worktree_at(repo, "quoting", "dev/quoting", merged=False)
        run(shell(quoting, 'grep -rn "gh pr merge" docs/'))
        check(
            "grepping for the merge phrase does not spend the worktree",
            decision(run(write(quoting, str(quoting / "a.txt")))),
            "allow",
        )
        run(shell(quoting, "echo gh pr merge"))
        check(
            "the phrase as another command's argument does not spend it",
            decision(run(write(quoting, str(quoting / "a.txt")))),
            "allow",
        )
        run(shell(quoting, "cat > docs/contract.md <<EOF\n| guard | gh pr merge ran |\nEOF"))
        check(
            "a heredoc writing a document that quotes the phrase does not spend it",
            decision(run(write(quoting, str(quoting / "a.txt")))),
            "allow",
        )
        # `--repo o/r` puts a flag's VALUE in front of the subcommand, so a reading that
        # only skipped tokens starting with `-` would miss the merge entirely.
        run(shell(quoting, "gh --repo o/r pr merge --squash"))
        check(
            "`gh pr merge` behind a flag with a value still spends it",
            decision(run(write(quoting, str(quoting / "a.txt")))),
            "deny",
        )

        # `land.py` is the delivery route SKILL.md recommends, and the regex never saw it:
        # the command string holds no `gh pr merge`, so the supported path left no mark.
        landing = worktree_at(repo, "landing", "dev/landing", merged=False)
        run(shell(landing, "python .claude/scripts/land.py"))
        check(
            "land.py spends the worktree it delivered from",
            decision(run(write(landing, str(landing / "a.txt")))),
            "deny",
        )

        # The mark belongs on the tree the merge RAN IN. Measured 2026-08-23: a session
        # merged another worktree with a leading `cd` and marked its own tree instead —
        # refusing its own Stop, and leaving the tree that merged editable.
        merging = worktree_at(repo, "merging", "dev/merging", merged=False)
        elsewhere = worktree_at(repo, "elsewhere", "dev/elsewhere", merged=False)
        run(shell(merging, f'cd "{elsewhere}" && gh pr merge --squash'))
        check(
            "a merge run in another worktree marks that worktree",
            decision(run(write(elsewhere, str(elsewhere / "a.txt")))),
            "deny",
        )
        check(
            "and does not mark the tree the session was sitting in",
            decision(run(write(merging, str(merging / "a.txt")))),
            "allow",
        )

        # --- the Stop gate counts what is undelivered, not what is unmerged -------
        published, harness, unpushed, onremote = build_published(root)
        check(
            "Stop does not block on the default branch's divergence",
            decision(run({"session_id": "p1", "hook_event_name": "Stop", "cwd": str(harness)})),
            "allow",
        )
        held = run({"session_id": "p2", "hook_event_name": "Stop", "cwd": str(unpushed)})
        check("Stop still blocks on a commit that is on no remote", decision(held), "block")
        check(
            "and says that is what it counted",
            "on no remote" in (held or {}).get("reason", ""),
            True,
        )
        waiting = run({"session_id": "p3", "hook_event_name": "Stop", "cwd": str(onremote)})
        check(
            "Stop blocks on a branch pushed but never landed",
            decision(waiting),
            "block",
        )
        check(
            "and names the pushed branch rather than calling it disk-only",
            "origin/a-pushed-topic" in (waiting or {}).get("reason", ""),
            True,
        )

        # --- a repository may name its own delivery -------------------------------
        config = published / ".claude" / "worktree-per-change.json"
        config.write_text(
            json.dumps(
                {
                    "integrationBranch": "development",
                    "delivery": {
                        "command": "pnpm feature land",
                        "teardown": "pnpm feature clean <name>",
                        "enterWorktree": False,
                    },
                }
            ),
            encoding="utf-8",
        )
        declared = (
            run({"session_id": "p4", "hook_event_name": "Stop", "cwd": str(unpushed)}) or {}
        ).get("reason", "")
        check("the block names the repository's own delivery command",
              "pnpm feature land" in declared, True)
        check("and does not prescribe a pull request it does not open",
              "gh pr create" in declared, False)
        check("its teardown is the declared one, with the worktree's name filled in",
              "pnpm feature clean unpushed" in declared, True)
        check("and it does not prescribe a step the repository cannot reach",
              "ExitWorktree" in declared, False)
        briefing = run({"session_id": "p5", "hook_event_name": "SessionStart", "cwd": str(published)})
        context = ((briefing or {}).get("hookSpecificOutput") or {}).get("additionalContext", "")
        check("SessionStart states the declared protocol",
              "pnpm feature land" in context, True)
        check("and tells a by-path repository how a `cd` has to be spelled",
              "in a command of its own" in context, True)

        # The default is what every repository that has declared nothing still gets.
        config.write_text(
            json.dumps({"integrationBranch": "development"}), encoding="utf-8"
        )
        default = (
            run({"session_id": "p6", "hook_event_name": "Stop", "cwd": str(unpushed)}) or {}
        ).get("reason", "")
        check("an undeclared repository still gets the PR protocol",
              "gh pr create --base development" in default, True)
        check("and still gets the ExitWorktree step",
              "ExitWorktree" in default, True)

        # --- a protected target changes what "delivered" means -------------------
        # Where the repository has declared that no session merges into the integration
        # branch, a pushed topic branch IS the finished state: the session committed,
        # pushed, opened the PR and handed it to a person. Counting it as undelivered
        # refuses `Stop` twice in a session that followed the protocol exactly — the same
        # shape of wrong gate as counting `origin/<branch>..HEAD` was.
        config.write_text(
            json.dumps({"integrationBranch": "development",
                        "protectedMergeTargets": ["development"]}),
            encoding="utf-8",
        )
        check(
            "Stop lets a pushed, unmerged branch end where the target is protected",
            decision(run({"session_id": "p7", "hook_event_name": "Stop", "cwd": str(onremote)})),
            "allow",
        )
        # Unpushed work is undelivered under any repository's rules, so that half stands.
        guarded = run({"session_id": "p8", "hook_event_name": "Stop", "cwd": str(unpushed)}) or {}
        check(
            "but an unpushed commit still holds the session open",
            guarded.get("decision"),
            "block",
        )
        check(
            "and the steps stop at the open PR rather than prescribing a denied merge",
            "gh pr merge" in guarded.get("reason", ""),
            False,
        )
        check(
            "saying so, so the session knows the PR is the end of its job",
            "Leave the pull request open" in guarded.get("reason", ""),
            True,
        )
        config.write_text(
            json.dumps({"integrationBranch": "development"}), encoding="utf-8"
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
