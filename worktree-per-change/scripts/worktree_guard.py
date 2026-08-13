#!/usr/bin/env python3
"""Every change gets its own worktree, its own branch, and its own merged PR.

The rule this enforces is deliberately absolute: **nothing is ever written in the
main checkout**. Not a one-line fix, not a typo, not "just this once". An absolute
rule is enforceable and a judgement call is not — the moment the protocol asks an
agent to decide whether a change is small enough to do in place, every change is
small enough, and the main checkout is back to being a place where two writers
collide and a half-finished edit rides into someone else's commit.

So the shape of every change is fixed:

    EnterWorktree  ->  edit, commit  ->  push  ->  PR  ->  merge  ->  next change
                                                                     gets a new one

Three hooks hold the three ends of that:

  * `PreToolUse` denies `Edit`/`Write`/`NotebookEdit` anywhere but a linked worktree,
    denies them in a worktree sitting on the integration branch, and denies them in a
    worktree whose PR has already merged — because "a new worktree every time" is only
    a real rule if reusing a spent one is refused.
  * `Stop` refuses to end a session that is walking away from committed-but-unlanded
    work. A branch that only exists on this disk is not a delivered change.
  * `SessionStart` states the protocol, so an agent knows it before its first denial
    rather than after.

`git stash` is denied everywhere, worktree or not: `refs/stash` is a single stack for
the whole repository, so a push in one worktree renumbers another's entries and a
later `pop` in *either* takes the wrong one. It is the one hazard a worktree looks
like it isolates and does not.

It fails **open** on every question it cannot answer — no repo, unreadable git
metadata, an unparseable payload. Blocking the only writer in a tree over state the
guard merely failed to read is the worse error, and it is the error that gets a hook
deleted.

  CLAUDE_WORKTREE_GATE=off        turns it off
  CLAUDE_WORKTREE_GATE=warn       reports instead of denying
  CLAUDE_INTEGRATION_BRANCH=x     overrides the branch changes merge into
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

STATE_DIRNAME = "claude-worktree-gate"
CONFIG_FILENAME = "worktree-per-change.json"
DEFAULT_INTEGRATION_BRANCH = "development"

# How many times `Stop` may refuse before it gives up and lets the session end. A hook
# that can block forever is a hook that hangs a session, and an agent that has ignored
# the same instruction twice is not going to take it on the third telling.
MAX_STOP_BLOCKS = 2

FILE_TOOLS = {"Edit", "Write", "NotebookEdit"}
SHELL_TOOLS = {"Bash", "PowerShell"}

# Subcommands that write history or move the floor. In the main checkout every one of
# them is wrong under this protocol: the checkout is a place to read from and pull
# into, and nothing else.
MUTATORS = {
    "add",
    "commit",
    "checkout",
    "switch",
    "restore",
    "reset",
    "rebase",
    "merge",
    "cherry-pick",
    "revert",
    "am",
    "apply",
    "clean",
    "rm",
    "mv",
}

_SEGMENT = re.compile(r"&&|\|\||[;\n|]")

# What "the change has landed" looks like on the command line. Seeing one of these
# spends the current worktree: the next edit has to start from a new one.
_MERGED = re.compile(r"\bgh\s+pr\s+merge\b", re.I)


# --------------------------------------------------------------------------- paths


def key(path) -> str:
    """A comparable spelling of a path. Case-insensitive where the filesystem is."""
    return os.path.normcase(os.path.normpath(os.path.abspath(str(path))))


def find_tree(start: Path):
    """The working tree containing `start`, its git dir, and whether it is linked.

    Walks the filesystem rather than shelling out: a subprocess on every write-tool
    call is the one cost this hook cannot amortise, and `.git` answers the question
    on its own. A linked worktree has `.git` as a *file* holding a `gitdir:` pointer,
    which is exactly the test that separates "isolated" from "the main checkout" — and
    it is a property of the tree itself, so no path arithmetic against
    `.claude/worktrees/` can get it wrong.
    """
    try:
        candidates = [start, *start.parents]
    except (OSError, ValueError):
        return None
    for directory in candidates:
        marker = directory / ".git"
        try:
            if marker.is_dir():
                return directory, marker, False
            if marker.is_file():
                text = marker.read_text(encoding="utf-8", errors="replace").strip()
                if not text.startswith("gitdir:"):
                    return None
                git_dir = Path(text.split(":", 1)[1].strip())
                if not git_dir.is_absolute():
                    git_dir = directory / git_dir
                return directory, Path(os.path.normpath(str(git_dir))), True
        except OSError:
            return None
    return None


def common_git_dir(git_dir: Path) -> Path:
    """The git directory every worktree of this repository shares.

    State lives there so all the worktrees read the same file — the whole point, since
    `.claude/` is checked out separately in each of them — and so nothing this hook
    writes ever shows up in `git status`.
    """
    pointer = git_dir / "commondir"
    try:
        if pointer.is_file():
            target = Path(pointer.read_text(encoding="utf-8").strip())
            if not target.is_absolute():
                target = git_dir / target
            return Path(os.path.normpath(str(target)))
    except OSError:
        pass
    return git_dir


def branch_of(git_dir: Path) -> str | None:
    """The checked-out branch, read straight out of `HEAD`. None if detached."""
    try:
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return head[16:] if head.startswith("ref: refs/heads/") else None


def integration_branch(main_root: Path | None) -> str:
    """The branch every change merges into.

    Per repository, because a repo that integrates through `development` and one that
    integrates through `main` both exist and neither is wrong. Committed next to the
    hook rather than inferred from the remote's default branch: the default branch is
    frequently *not* the integration branch, and guessing it wrong sends every PR at
    the wrong target.
    """
    override = (os.environ.get("CLAUDE_INTEGRATION_BRANCH") or "").strip()
    if override:
        return override
    if main_root is not None:
        try:
            blob = json.loads((main_root / ".claude" / CONFIG_FILENAME).read_text(encoding="utf-8"))
            name = blob.get("integrationBranch")
            if isinstance(name, str) and name.strip():
                return name.strip()
        except (OSError, ValueError, AttributeError):
            pass
    return DEFAULT_INTEGRATION_BRANCH


# --------------------------------------------------------------------------- state


def state_dir(common: Path) -> Path:
    return common / STATE_DIRNAME


def spent_marker(common: Path, tree_root: Path) -> Path:
    stem = re.sub(r"[^A-Za-z0-9._-]", "_", Path(tree_root).name) or "tree"
    return state_dir(common) / "spent" / f"{stem}.json"


def mark_spent(common: Path, tree_root: Path, why: str) -> None:
    path = spent_marker(common, tree_root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"tree": str(tree_root), "at": time.time(), "why": why}),
            encoding="utf-8",
        )
    except OSError:
        pass


def is_spent(common: Path, tree_root: Path) -> dict | None:
    try:
        return json.loads(spent_marker(common, tree_root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def stop_blocks(common: Path, session: str, bump: bool = False) -> int:
    path = state_dir(common) / f"stop-{re.sub(r'[^A-Za-z0-9._-]', '_', session)}.json"
    count = 0
    try:
        count = int(json.loads(path.read_text(encoding="utf-8")).get("blocks", 0))
    except (OSError, ValueError, TypeError):
        count = 0
    if bump:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"blocks": count + 1}), encoding="utf-8")
        except OSError:
            pass
    return count


# ------------------------------------------------------------------- command parse


def git_calls(command: str):
    """Every git subcommand in a shell command, as (subcommand, args).

    Textual, and it never expands a variable: `git -C "$W" switch` is judged as a plain
    `switch`. That is the guard being conservative rather than clever — the cost is
    spelling a path out in full, and the alternative is trusting a shell expansion it
    cannot evaluate.
    """
    calls = []
    for segment in _SEGMENT.split(command):
        try:
            tokens = shlex.split(segment)
        except ValueError:
            tokens = segment.split()
        if not tokens:
            continue
        try:
            start = next(i for i, t in enumerate(tokens) if Path(t).name in {"git", "git.exe"})
        except StopIteration:
            continue
        rest = tokens[start + 1 :]
        index = 0
        while index < len(rest) and rest[index].startswith("-"):
            index += 2 if rest[index] in {"-C", "-c"} else 1
        if index < len(rest):
            calls.append((rest[index], rest[index + 1 :]))
    return calls


# ------------------------------------------------------------------------- reports


PROTOCOL = (
    "The protocol: call **EnterWorktree** before the first edit. Work, commit, "
    "`git push -u origin HEAD`, open a PR into `{branch}` with `gh pr create --base "
    "{branch}`, then `gh pr merge`. A second change in the same session starts a new "
    "worktree — one worktree, one branch, one PR, one change."
)

BASE_NOTE = (
    "If the change needs a base other than the repository's default branch — which it "
    "does here, since changes integrate through `{branch}` — create the worktree with "
    "git first and enter that path:\n"
    "`git worktree add .claude/worktrees/<name> -b <branch> origin/{branch}` then "
    "EnterWorktree with that path. `worktree.baseRef` only chooses between the default "
    "branch and local HEAD, never a named branch, so a bare EnterWorktree cuts from the "
    "wrong place and carries the divergence into your diff without complaining."
)

ESCAPE = (
    "`/worktree-per-change` has the full protocol. Set CLAUDE_WORKTREE_GATE=off only if "
    "the guard is provably wrong, and say in your reply that you did it and why."
)


def reason_main_checkout(what: str, branch: str) -> str:
    return (
        f"Denied: {what} in the main checkout. Every change in this repository is made "
        "in its own worktree, on its own branch, and reaches the integration branch as a "
        "merged PR — there is no size of change that skips that, because the exception is "
        "what puts two writers back in one directory and half-finished work into someone "
        "else's commit.\n\n"
        + PROTOCOL.format(branch=branch)
        + "\n\n"
        + BASE_NOTE.format(branch=branch)
        + "\n\n"
        + ESCAPE
    )


def reason_integration_branch(branch: str) -> str:
    return (
        f"Denied: this tree is on `{branch}`, the branch changes merge *into*. Committing "
        "here would put the change on the integration branch without a PR, and the next "
        "session to pull would get it without anyone having read it.\n\n"
        f"Branch first: `git switch -c <short-topic-name> origin/{branch}`, then edit.\n\n"
        + ESCAPE
    )


def reason_spent(marker: dict, branch: str) -> str:
    landed = marker.get("why") or "its PR merged"
    return (
        f"Denied: this worktree's change has already landed ({landed}), so it is finished "
        "work. Editing it again grows a branch that has been reviewed and merged, and the "
        "new edit reaches nobody until someone notices and opens a second PR from a tree "
        "that looks done.\n\n"
        "The next change is a new one: call **EnterWorktree** again for a fresh worktree "
        "and branch, cut from the current "
        f"`origin/{branch}` so it already contains what you just merged.\n\n"
        + BASE_NOTE.format(branch=branch)
        + "\n\n"
        + ESCAPE
    )


def reason_stash() -> str:
    return (
        "Denied: `refs/stash` is one stack for the whole repository, shared by every "
        "worktree, so a push here renumbers the entries in every other tree and a later "
        "`pop` or `drop` in either takes the wrong one. This is the one hazard a worktree "
        "looks like it isolates and does not.\n\n"
        "Commit instead — a commit belongs to your branch and no stranger can pop it: "
        '`git add <paths> && git commit -m "wip"`.\n\n' + ESCAPE
    )


def deny(reason: str, warn_only: bool) -> None:
    if warn_only:
        # Report and say nothing about permission. Emitting an explicit `allow` here
        # would auto-approve the call and make warn mode *more* permissive than no
        # guard at all, which is the opposite of what it is for.
        emit({"systemMessage": "worktree-per-change (warn mode) would have denied this. " + reason})
        return
    emit(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }
    )


def emit(payload: dict) -> None:
    json.dump(payload, sys.stdout)


# ---------------------------------------------------------------------------- stop


def git(tree: Path, *args: str) -> str | None:
    """One git call, for the `Stop` hook only. Never on the write path."""
    try:
        result = subprocess.run(
            ["git", "-C", str(tree), *args],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def unlanded(tree: Path, branch: str) -> str | None:
    """What this worktree is holding that the integration branch has not got.

    Returns a human sentence, or None when there is nothing to keep the session open
    for. Every question it cannot answer resolves to None: a `Stop` hook that blocks on
    a git call that merely failed would strand a session with no way out.
    """
    dirty = git(tree, "status", "--porcelain")
    if dirty is None:
        return None
    ahead = git(tree, "rev-list", "--count", f"origin/{branch}..HEAD")
    commits = int(ahead) if (ahead or "").isdigit() else 0
    if not dirty and not commits:
        return None

    parts = []
    if dirty:
        parts.append(f"{len(dirty.splitlines())} uncommitted file(s)")
    if commits:
        parts.append(f"{commits} commit(s) not in origin/{branch}")
    return " and ".join(parts)


def block_stop(tree: Path, branch: str, holding: str) -> None:
    emit(
        {
            "decision": "block",
            "reason": (
                f"This worktree is holding {holding}. A branch that exists only on this "
                "disk is not a delivered change — the operator is left with a directory "
                "nobody will look in, and the next session cuts its worktree from an "
                f"`origin/{branch}` that is missing your work.\n\n"
                "Finish it before stopping:\n"
                "1. `git add <paths> && git commit -m \"...\"` — name the paths; never "
                "`git add -A`.\n"
                "2. `git push -u origin HEAD`\n"
                f"3. `gh pr create --base {branch} --fill`\n"
                "4. `gh pr merge --squash --delete-branch` (add `--admin` only if the "
                "repo's checks do not apply here)\n"
                "5. `ExitWorktree` with `action: \"remove\"`, then `git branch -d "
                "<branch>` — a merged branch left standing is a live push target after "
                "the PR that reviewed it has closed.\n\n"
                "If the change is genuinely abandoned, say so plainly in your reply and "
                "leave the worktree standing — do not delete it, and do not stash."
            ),
        }
    )


# ---------------------------------------------------------------------------- main


def target_paths(payload: dict, cwd: Path):
    tool_input = payload.get("tool_input") or {}
    path = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not isinstance(path, str) or not path:
        return []
    candidate = Path(path)
    return [candidate if candidate.is_absolute() else cwd / candidate]


def main() -> None:
    mode = (os.environ.get("CLAUDE_WORKTREE_GATE") or "on").strip().lower()
    if mode == "off":
        return
    warn_only = mode == "warn"

    payload = json.load(sys.stdin)
    event = payload.get("hook_event_name")
    session = payload.get("session_id") or "unknown"
    cwd = payload.get("cwd")
    if not cwd:
        return

    located = find_tree(Path(cwd))
    if located is None:
        return  # Not a git repository. Nothing to protect, nothing to say.
    tree_root, git_dir, linked = located
    common = common_git_dir(git_dir)
    main_root = common.parent if common.name == ".git" else None
    branch = integration_branch(main_root)

    if event == "SessionStart":
        emit(
            {
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": (
                        "This repository writes only from worktrees. Edits to the main "
                        "checkout are denied by a hook, including one-line ones.\n\n"
                        + PROTOCOL.format(branch=branch)
                        + "\n\n"
                        + BASE_NOTE.format(branch=branch)
                    ),
                }
            }
        )
        return

    if event == "Stop":
        if not linked:
            return
        if stop_blocks(common, session) >= MAX_STOP_BLOCKS:
            return
        holding = unlanded(tree_root, branch)
        if holding:
            stop_blocks(common, session, bump=True)
            block_stop(tree_root, branch, holding)
        return

    if event != "PreToolUse":
        return

    tool = payload.get("tool_name", "")

    if tool in FILE_TOOLS:
        for path in target_paths(payload, Path(cwd)):
            target = key(path)
            root = key(tree_root)
            if target != root and not target.startswith(root + os.sep):
                continue  # Outside this repository — not this repository's rule.
            if not linked:
                deny(reason_main_checkout("file edits are not made", branch), warn_only)
                return
            if branch_of(git_dir) == branch:
                deny(reason_integration_branch(branch), warn_only)
                return
            marker = is_spent(common, tree_root)
            if marker:
                deny(reason_spent(marker, branch), warn_only)
                return
        return

    if tool not in SHELL_TOOLS:
        return

    command = (payload.get("tool_input") or {}).get("command")
    if not isinstance(command, str):
        return

    if linked and _MERGED.search(command):
        # Recorded *before* the merge runs rather than after, because there is no
        # after-hook that can tell a merge apart from a merge that failed. A worktree
        # marked spent by a merge that did not land is the harmless direction: the
        # remedy is a new worktree, which is what the protocol wanted anyway.
        mark_spent(common, tree_root, "gh pr merge was run from this worktree")

    for subcommand, _args in git_calls(command):
        if subcommand == "stash" and not (_args and _args[0] in {"list", "show"}):
            deny(reason_stash(), warn_only)
            return
        if not linked and subcommand in MUTATORS:
            deny(reason_main_checkout(f"`git {subcommand}` does not run", branch), warn_only)
            return


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001 — fail open, always.
        pass
    sys.exit(0)
