#!/usr/bin/env python3
"""Every change gets its own worktree, its own branch, and its own merged PR.

The rule this enforces is deliberately absolute: **nothing is ever written in the
main checkout**. Not a one-line fix, not a typo, not "just this once". An absolute
rule is enforceable and a judgement call is not — the moment the protocol asks an
agent to decide whether a change is small enough to do in place, every change is
small enough, and the main checkout is back to being a place where two writers
collide and a half-finished edit rides into someone else's commit.

So the shape of every change is fixed:

    EnterWorktree  ->  edit, commit  ->  push  ->  PR  ->  merge  ->  remove the tree
                                                                     and the branch;
                                                                     the next change
                                                                     gets a new one

Three hooks hold the ends of that:

  * `PreToolUse` denies `Edit`/`Write`/`NotebookEdit` anywhere but a linked worktree,
    denies them in a worktree sitting on the integration branch, and denies them in a
    worktree whose PR has already merged — because "a new worktree every time" is only
    a real rule if reusing a spent one is refused.
  * `Stop` refuses to end a session that is walking away from committed-but-unlanded
    work — a branch that only exists on this disk is not a delivered change — and
    refuses just as much to end one sitting in a worktree whose PR *has* merged. The
    teardown is the half that used to be nobody's: the change lands, the reply is
    truthful, and a stale checkout plus a live push target stay behind for the next
    session to work out the status of.
  * `SessionStart` states the protocol, so an agent knows it before its first denial
    rather than after, and reports landed worktrees an earlier session left on disk.

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


def mark_spent(common: Path, tree_root: Path, topic: str | None, why: str) -> None:
    path = spent_marker(common, tree_root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {"tree": str(tree_root), "branch": topic, "at": time.time(), "why": why}
            ),
            encoding="utf-8",
        )
    except OSError:
        pass


def is_spent(common: Path, tree_root: Path, topic: str | None) -> dict | None:
    """The marker saying this worktree's change has landed, if there is one.

    What is spent is a *branch in a tree*, not a directory name. The marker file is named
    after the worktree's leaf name — the only stable, filesystem-safe handle available —
    so it has to confirm both fields before it applies. Two worktrees can share a leaf
    name (`../hermes-dev-x` and `.claude/worktrees/hermes-dev-x`), and once cleanup is
    routine a path gets *reused*: the same name, cut again off the integration branch, for
    the next change. Trusting the filename alone would greet that fresh tree with "your
    change has already landed", which is the most confusing denial this guard can produce.
    """
    try:
        marker = json.loads(spent_marker(common, tree_root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(marker, dict):
        return None
    recorded = marker.get("tree")
    if isinstance(recorded, str) and key(recorded) != key(tree_root):
        return None
    # `branch` is absent from markers written before it was recorded. Those still mean
    # what they said — the tree they name has merged — so a missing field matches.
    was = marker.get("branch")
    if isinstance(was, str) and topic is not None and was != topic:
        return None
    return marker


def sweep_spent(common: Path) -> list[str]:
    """Landed worktrees still on disk, and a marker file dropped for each one that isn't.

    Both halves are cleanup. The list is what a session inherits from one that crashed or
    was killed between `gh pr merge` and taking its tree down, which nothing else reports:
    a merged worktree is indistinguishable from an in-progress one to anybody reading
    `git worktree list`.

    Dropping the marker for a tree that is gone is not tidiness either. Markers are keyed
    by the worktree's *leaf name*, so a stale one denies the first edit in the next
    worktree that happens to be named the same — a fresh tree reported as already merged,
    which is the most confusing denial this guard can produce.
    """
    standing = []
    try:
        entries = sorted((state_dir(common) / "spent").iterdir())
    except OSError:
        return standing
    for marker in entries:
        try:
            tree = json.loads(marker.read_text(encoding="utf-8")).get("tree")
        except (OSError, ValueError, AttributeError):
            continue
        if not isinstance(tree, str) or not tree:
            continue
        if Path(tree).is_dir():
            standing.append(tree)
        else:
            try:
                marker.unlink()
            except OSError:
                pass
    return standing


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
    "The base is `origin/{branch}` — the FETCHED remote tip — and never local HEAD, never "
    "whatever branch the main checkout is sitting on, and never an unfetched local ref. So "
    "create the worktree with git first and enter that path:\n"
    "`git fetch origin {branch} && git worktree add .claude/worktrees/<name> -b <branch> "
    "origin/{branch}` then EnterWorktree with that path. `worktree.baseRef` never accepts a "
    "branch name — it chooses between the repository's default branch and local HEAD, and "
    "here BOTH are wrong — so a bare EnterWorktree cuts from the wrong place and carries "
    "changes you did not make into your diff without complaining."
)

ESCAPE = (
    "`/worktree-per-change` has the full protocol. Set CLAUDE_WORKTREE_GATE=off only if "
    "the guard is provably wrong, and say in your reply that you did it and why."
)


def cleanup_steps(tree: Path | str, topic: str | None) -> str:
    """How a landed worktree comes down, spelled out because two of the four steps trap.

    `ExitWorktree` with `action: "remove"` is the one everybody reaches for, and it cannot
    do this job: it removes only a worktree EnterWorktree *itself* created, where under this
    protocol the tree is made with `git worktree add` and entered by path. Measured — it
    refuses outright, saying the session does not own the worktree and to use `"keep"`. So
    the cost is a wasted call rather than a tree that quietly stays, and asking for `"keep"`
    up front is what turns four steps into four steps instead of five.
    """
    name = topic or "<branch>"
    return (
        f"1. `gh pr view <n> --json state --jq .state` — expect `MERGED`. Ask the forge, "
        "not git: `git branch -d`, `--merged` and `merge-base --is-ancestor` all read a "
        "squash-merged branch as unmerged, so under this protocol all three are false "
        "negatives.\n"
        '2. `ExitWorktree` with `action: "keep"` — it returns the session to the main '
        'checkout. **Not `"remove"`**: that removes only a worktree EnterWorktree created '
        "itself, and refuses on one it merely entered by path, so it cannot take this tree "
        "down.\n"
        f"3. `git worktree remove {tree}` — from the main checkout, which is where it is "
        "allowed and the only place it can run. Nothing can remove the tree it is "
        "standing in.\n"
        f"4. `git branch -D {name}`, then `git fetch origin --prune && git branch -r` and "
        f"`git push origin --delete {name}` if the remote branch is still listed. "
        "`--delete-branch` deletes the local branch first and abandons the remote one when "
        "that fails, which is the normal case here because your worktree still has the "
        "branch checked out at merge time."
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


def block_stop(tree: Path, branch: str, topic: str | None, holding: str) -> None:
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
                "5. Then take the worktree down — the four steps below.\n\n"
                + cleanup_steps(tree, topic)
                + "\n\nIf the change is genuinely abandoned, say so plainly in your reply "
                "and leave the worktree standing — do not delete it, and do not stash."
            ),
        }
    )


def block_stop_cleanup(tree: Path, branch: str, topic: str | None, marker: dict) -> None:
    """Refuse to end a session sitting in a worktree whose change has already landed.

    Cleanup is the half of the protocol nothing used to hold. Delivery had a hook and a
    denial each; the teardown had a paragraph in a doc, and the failure mode is silent —
    the change is merged, the reply is truthful, and what is left behind is a directory
    plus a branch that the *next* session has to establish the status of before it can
    trust either. Handing the operator the two commands is not delivering the work; they
    only have to run them because the session that knew the answer stopped first.
    """
    landed = marker.get("why") or "its PR merged"
    emit(
        {
            "decision": "block",
            "reason": (
                f"This worktree's change has landed ({landed}) and the worktree is still "
                "standing. Taking it down is part of finishing, not an errand to hand over: "
                "a worktree with no live branch is a stale checkout, a merged branch is a "
                "push target after the PR that reviewed it has closed, and either one left "
                "behind costs the next session a status check before it can trust what it "
                "is looking at.\n\n"
                + cleanup_steps(tree, topic)
                + "\n\nIf the operator asked for this tree to stay — to look at the diff, or "
                "to keep a dev server on it — leave it and say so plainly in your reply, "
                "with the path. That is the one reason to stop with it standing."
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
        context = (
            "This repository writes only from worktrees. Edits to the main "
            "checkout are denied by a hook, including one-line ones.\n\n"
            + PROTOCOL.format(branch=branch)
            + "\n\n"
            + BASE_NOTE.format(branch=branch)
            + "\n\nA change is finished when its worktree is gone too: after the merge, "
            "`ExitWorktree` (`action: \"keep\"`), then `git worktree remove <path>` and "
            "`git branch -D <branch>` from the main checkout."
        )
        # The sweep runs at SessionStart deliberately: it is the one moment nothing is in
        # flight, so a landed tree still on disk is somebody's leftovers rather than the
        # work in progress two minutes from its own merge.
        standing = sweep_spent(common)
        if standing:
            context += (
                "\n\nLanded worktrees still on disk, left by an earlier session:\n"
                + "\n".join(f"- {path}" for path in standing)
                + "\nEach one's PR has merged. Remove the ones that are yours — "
                "`git worktree remove <path>` then `git branch -D <branch>`, from the main "
                "checkout. A worktree another session is holding is its business even after "
                "its branch merges: leave it, and say it is there."
            )
        emit(
            {
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": context,
                }
            }
        )
        return

    if event == "Stop":
        if not linked:
            return
        if stop_blocks(common, session) >= MAX_STOP_BLOCKS:
            return
        # Spent first. A landed tree can also read as holding unlanded commits — a squash
        # merge leaves none of the branch's own commits in `origin/<branch>` — and telling
        # a session to push work it has already merged is the one wrong answer here.
        topic = branch_of(git_dir)
        marker = is_spent(common, tree_root, topic)
        if marker:
            stop_blocks(common, session, bump=True)
            block_stop_cleanup(tree_root, branch, topic, marker)
            return
        holding = unlanded(tree_root, branch)
        if holding:
            stop_blocks(common, session, bump=True)
            block_stop(tree_root, branch, topic, holding)
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
            topic = branch_of(git_dir)
            if topic == branch:
                deny(reason_integration_branch(branch), warn_only)
                return
            marker = is_spent(common, tree_root, topic)
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
        mark_spent(
            common,
            tree_root,
            branch_of(git_dir),
            "gh pr merge was run from this worktree",
        )

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
