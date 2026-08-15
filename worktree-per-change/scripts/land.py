#!/usr/bin/env python3
"""Land the change in the worktree this is run from: push, PR, merge, verify.

This exists to be *allowlisted*, and everything about it is shaped by that.

The protocol's last three steps — push, open a PR, merge it — are the ones a
permission layer stops, because they are the ones that reach outside the machine.
Left stopped, the rule collapses into "the agent does the work and a human finishes
it", which is the failure the `Stop` hook already refuses: a branch that exists only
on this disk is not a delivered change. So the steps have to be grantable.

The obvious grant is `Bash(gh pr merge:*)`, and it is far too wide: it merges any PR
in any repository the machine is authenticated to, on any base. This script is the
narrow alternative. It takes **no PR number and no branch** — it merges the PR whose
head is the branch checked out in the worktree it was run from, into the integration
branch that repository recorded, and refuses everything else. One allowlist entry for
this file grants exactly the protocol and nothing beside it.

What it is NOT is a way around a permission decision. It runs `gh` and `git` under
their own names and prints every command before running it; a machine that has not
allowed it is a machine where it does not run. Wrapping the same commands to make them
unrecognisable would be a different program with a different purpose, and it would also
not work for long.

It stops before removing the worktree, because nothing can remove the tree it is
standing in — see SKILL.md step 4 for the two commands that follow, and the allowlist
entries that let them run.

It refuses a branch that has already merged, before pushing anything. That is not a
nicety: after a change lands there is no remote branch and no open PR, so every later
check reads like a change that was never delivered, and running this twice opens a
second PR whose diff is empty and merges it.

Usage:
    python .claude/scripts/land.py              push, PR if needed, merge, verify
    python .claude/scripts/land.py --dry-run    print the sequence, run none of it
    python .claude/scripts/land.py --title T --body-file B    ... for the PR it creates
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

CONFIG_FILENAME = "worktree-per-change.json"
DEFAULT_INTEGRATION_BRANCH = "development"


class Refused(Exception):
    """A precondition of the protocol is not met. The message says which."""


# ----------------------------------------------------------------- running things


def run(argv: list[str], cwd: Path, dry_run: bool = False, capture: bool = True):
    """Run `argv`, echoing it first.

    Echoing is not decoration. This script is allowlisted, which means a human agreed to
    it once and is not watching each run; the transcript of what it actually did is the
    only remaining record, and a silent wrapper around `gh pr merge` is precisely the
    thing nobody should install.
    """
    print("  $ " + " ".join(argv))
    if dry_run:
        return subprocess.CompletedProcess(argv, 0, "", "")
    return subprocess.run(argv, cwd=str(cwd), capture_output=capture, text=True)


def git(cwd: Path, *args: str) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=30
    )
    return result.stdout.strip() if result.returncode == 0 else None


# ------------------------------------------------------------------- where are we


def locate(start: Path) -> tuple[Path, Path]:
    """Return (worktree root, main checkout root), or refuse.

    `.git` is a file in a linked worktree and a directory in the main checkout, which is
    the same one-stat test the guard uses. Sharing the test matters more than sharing the
    code: a script that thought it was in a worktree where the guard thought otherwise
    would push from a directory the guard had just refused to let anyone edit.
    """
    for directory in [start, *start.parents]:
        marker = directory / ".git"
        if marker.is_dir():
            raise Refused(
                f"{directory} is the MAIN CHECKOUT, and nothing is landed from here.\n"
                "Every change is made in its own worktree and landed from inside it. "
                "If the change you meant to land is in a worktree, run this from there."
            )
        if marker.is_file():
            text = marker.read_text(encoding="utf-8", errors="replace").strip()
            if not text.startswith("gitdir:"):
                continue
            git_dir = Path(text.split(":", 1)[1].strip())
            if not git_dir.is_absolute():
                git_dir = directory / git_dir
            git_dir = Path(os.path.normpath(str(git_dir)))
            common = git_dir
            pointer = git_dir / "commondir"
            if pointer.is_file():
                target = Path(pointer.read_text(encoding="utf-8").strip())
                common = Path(
                    os.path.normpath(
                        str(git_dir / target if not target.is_absolute() else target)
                    )
                )
            main_root = common.parent if common.name == ".git" else directory
            return directory, main_root
    raise Refused(f"{start} is not inside a git repository.")


def integration_branch(main_root: Path) -> str:
    """The branch this repository's PRs target.

    Read from the repository rather than guessed, and read from the *main checkout* so
    that every worktree of it agrees. The environment override is the operator's, and
    exists for the same reason it does in the guard.

    The fallback is a fallback, not a default anyone should reach: `install.py` asks and
    records the answer, so a repo arriving here without a record is one where the guard
    was installed by hand. Landing into the wrong branch is not recoverable by rerunning,
    so this says which branch it is about to use, every time.
    """
    override = (os.environ.get("CLAUDE_INTEGRATION_BRANCH") or "").strip()
    if override:
        return override
    try:
        blob = json.loads(
            (main_root / ".claude" / CONFIG_FILENAME).read_text(encoding="utf-8")
        )
        name = blob.get("integrationBranch")
        if isinstance(name, str) and name.strip():
            return name.strip()
    except (OSError, ValueError):
        pass
    return DEFAULT_INTEGRATION_BRANCH


# ------------------------------------------------------------------------- checks


def preflight(tree: Path, branch: str) -> str:
    """Refuse anything that is not "this worktree's own finished change"."""
    topic = git(tree, "rev-parse", "--abbrev-ref", "HEAD")
    if not topic or topic == "HEAD":
        raise Refused(
            "This worktree is on a detached HEAD, so there is no branch to open a PR "
            "from. `git switch -c <short-topic-name>` first."
        )
    if topic == branch:
        raise Refused(
            f"This worktree is sitting on `{branch}`, the branch changes merge INTO. "
            f"A PR from `{branch}` to `{branch}` is not a change.\n"
            "`git switch -c <short-topic-name>` first — the guard denies edits here for "
            "the same reason."
        )
    dirty = git(tree, "status", "--porcelain") or ""
    if dirty.strip():
        names = "\n".join(f"    {line}" for line in dirty.splitlines()[:10])
        more = "" if len(dirty.splitlines()) <= 10 else f"\n    ... and {len(dirty.splitlines()) - 10} more"
        raise Refused(
            "This worktree has uncommitted changes, and landing would leave them "
            f"behind on a branch about to be deleted:\n{names}{more}\n"
            "Commit them (`git add <paths> && git commit`) or discard them, then run "
            "this again."
        )
    return topic


# -------------------------------------------------------------------------- steps


def head_pr(tree: Path, topic: str, state: str) -> int | None:
    """The PR whose head is `topic` and whose state is `state`, if there is one.

    Asked by head branch, never by number: the number is the input that would let this
    script merge something that has nothing to do with the worktree it was run from,
    which is the whole reason it is narrow enough to allowlist.
    """
    result = subprocess.run(
        ["gh", "pr", "list", "--head", topic, "--state", state,
         "--json", "number", "--jq", ".[0].number"],
        cwd=str(tree), capture_output=True, text=True,
    )
    value = (result.stdout or "").strip()
    return int(value) if value.isdigit() else None


def land(tree: Path, main_root: Path, branch: str, topic: str, args) -> int:
    print(f"repository:  {main_root}")
    print(f"worktree:    {tree}")
    print(f"branch:      {topic}  ->  {branch}")
    print()

    # Asked BEFORE the push, because the push is what makes this undetectable. A landed
    # change leaves no remote branch and no *open* PR, so every check after the push
    # reads exactly like a change that has not been delivered yet: `git push -u origin
    # HEAD` recreates the deleted branch and succeeds, the search for an open PR finds
    # nothing, and a second PR is opened from a branch whose content is already on the
    # integration branch. It merges, because an empty diff is a mergeable one.
    #
    # Measured 2026-08-15: this script run twice against one worktree put `(#55)` and
    # `(#56)` on `main` for one change, the second changing no files. The push-failure
    # branch below was written expecting to catch this and cannot -- the push is the
    # step that succeeds.
    #
    # Refusing is right rather than merely safe: the protocol is one worktree, one
    # branch, one change, so a branch that has already merged has nothing left to
    # deliver. A second change gets a new worktree.
    landed = head_pr(tree, topic, "merged")
    if landed is not None:
        raise Refused(
            f"#{landed} has already merged `{topic}`, so this change is finished and "
            "nothing here is undelivered.\n"
            "Take this worktree down and cut a new one for the next change:\n"
            f"    git worktree remove {tree}\n"
            f"    git branch -D {topic}"
        )

    print("push")
    pushed = run(["git", "push", "-u", "origin", "HEAD"], tree, args.dry_run)
    if pushed.returncode != 0:
        # This used to say "if this branch has already merged, the change is finished",
        # which was the right diagnosis attached to the wrong step: a merged branch is
        # deleted, so pushing it back up succeeds. The check above catches that case now,
        # and what is left here is the remote refusing the push on its own terms -- a
        # protected branch, a rejected non-fast-forward, no credentials.
        raise Refused(
            f"the push was refused:\n{(pushed.stderr or '').strip()}"
        )

    number = None if args.dry_run else head_pr(tree, topic, "open")
    print("\npull request")
    if number is None:
        create = ["gh", "pr", "create", "--base", branch]
        create += ["--title", args.title] if args.title else []
        create += ["--body-file", args.body_file] if args.body_file else []
        if not args.title and not args.body_file:
            create += ["--fill"]
        created = run(create, tree, args.dry_run)
        if not args.dry_run:
            if created.returncode != 0:
                raise Refused(f"gh pr create failed:\n{(created.stderr or '').strip()}")
            number = head_pr(tree, topic, "open")
            if number is None:
                raise Refused(
                    "the PR was created but cannot be found by head branch, so this "
                    "will not merge something it has not identified. Merge it by hand."
                )
            print(f"  opened #{number}")
    else:
        print(f"  #{number} is already open for {topic}")

    # No `--delete-branch`, deliberately. It makes `gh` do local git work after the API
    # call — it checks out the base branch to delete the merged one — and under this
    # protocol that always fails, because the main checkout is permanently sitting on the
    # integration branch:
    #
    #     failed to run git: fatal: 'main' is already used by worktree at '…'
    #
    # Measured 2026-08-15, landing this script's own first change. The merge had already
    # happened on the forge; only the cleanup failed. So the flag cannot do the one thing
    # it is for here, and asking it to leaves the remote branch standing while returning
    # an error — the worst of both. The deletion is done explicitly below instead, where
    # it is one API call that cannot be confused by what this checkout has checked out.
    print("\nmerge")
    target = str(number) if number is not None else "<n>"
    merged = run(["gh", "pr", "merge", target, "--squash"], tree, args.dry_run)

    # A non-zero exit is a question, not an answer. `gh` can merge the PR and then fail on
    # something afterwards, and the two are indistinguishable from the exit code — which is
    # exactly how the run above ended: exit 1, a git error, and a merged PR. Refusing on
    # the exit code alone reports a change as unlanded while it is sitting on the
    # integration branch, and sends the next session to redo it.
    print("\nverify")
    if args.dry_run:
        run(["gh", "pr", "view", target, "--json", "state", "--jq", ".state"], tree, True)
        run(["git", "push", "origin", "--delete", topic], tree, True)
        print("  (dry run — nothing was pushed, opened, merged or deleted)")
        return 0

    state = run(["gh", "pr", "view", str(number), "--json", "state", "--jq", ".state"], tree)
    landed = (state.stdout or "").strip()
    if landed != "MERGED":
        raise Refused(
            f"the forge reports #{number} as {landed or 'unknown'}, not MERGED"
            + (f", and gh reported:\n{(merged.stderr or '').strip()}" if merged.returncode else "")
            + "\nThe branch has NOT been deleted and the change has not landed."
        )
    if merged.returncode:
        # Worth saying out loud rather than swallowing: the merge landed, something after
        # it did not, and the next person to read this transcript should know which.
        print(f"  gh exited {merged.returncode} after the merge — {(merged.stderr or '').strip()}")
    print(f"  #{number} is MERGED")

    # The branch is deleted here, from the forge, for the reason in the comment above and
    # one more: a merged branch left standing is a live push target after the PR that
    # reviewed it has closed, and a commit pushed there looks like ordinary work and
    # reaches the integration branch never.
    deleted = run(["git", "push", "origin", "--delete", topic], tree)
    run(["git", "fetch", "origin", "--prune"], tree)
    remote = git(tree, "ls-remote", "--heads", "origin", topic) or ""
    if remote.strip():
        print(f"  ! {topic} is STILL on the remote: {(deleted.stderr or '').strip()}")
        print(f"  ! delete it by hand — `git push origin --delete {topic}`")
    else:
        print(f"  {topic} is gone from the remote")

    print(f"\n#{number} is merged. This worktree is finished; take it down from the main")
    print("checkout, because nothing can remove the tree it is standing in:")
    print(f"    git worktree remove {tree}")
    print(f"    git branch -D {topic}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="print the sequence, run none of it")
    parser.add_argument("--title", metavar="TEXT", help="title for the PR, if one is created")
    parser.add_argument("--body-file", metavar="PATH", help="body file for the PR, if one is created")
    args = parser.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass

    try:
        tree, main_root = locate(Path.cwd().resolve())
        branch = integration_branch(main_root)
        topic = preflight(tree, branch)
        return land(tree, main_root, branch, topic, args)
    except Refused as refusal:
        print(f"refused: {refusal}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
