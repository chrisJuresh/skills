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

Every rule is scoped to the tree the operation **targets**, not the directory the
session happens to sit in — those differ constantly, and the session's own status is
the wrong answer for every case where they do. So `cd ../other-repo && git add -A` is
that repository's business and passes; a `git -C <main-checkout>` from a worktree is
judged as the main checkout and does not; a write to an absolute path inside a linked
worktree is judged as that worktree. A command that names no target means the
session's own tree, which is what an ordinary command means anyway.

It fails **open** on every question it cannot answer — no repo, unreadable git
metadata, an unparseable payload. Blocking the only writer in a tree over state the
guard merely failed to read is the worse error, and it is the error that gets a hook
deleted.

  CLAUDE_WORKTREE_GATE=off        turns it off
  CLAUDE_WORKTREE_GATE=warn       reports instead of denying
  CLAUDE_INTEGRATION_BRANCH=x     overrides the branch changes merge into

All three are read from the **hook's** environment, which is the Claude Code process's,
so they are the operator's switches and not a session's: `CLAUDE_WORKTREE_GATE=off git
add …` sets the variable for that command alone, and by then this hook has already run
and denied it. Changing one takes effect for sessions started afterwards.
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

# Where a new worktree goes, relative to the main checkout. This is quoted in the remedy
# text and used for nothing else: whether a directory IS a worktree is a stat on `.git`
# (see find_tree), never path arithmetic, so a wrong value here cannot mis-classify a
# tree. It is configurable because it can still be wrong in the way that costs a turn —
# a repository that does not gitignore `.claude/` cannot put worktrees there without
# every tree arriving as untracked files in `git status`, and a remedy naming a path the
# repo has ruled out is a remedy nobody can take.
DEFAULT_WORKTREES_ROOT = ".claude/worktrees"

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

# The characters a shell reads as operators rather than as part of a word. `\n` is in the
# set deliberately, and `whitespace` is narrowed to match: shlex counts a newline as
# whitespace, and whitespace is tested first, so leaving it there would erase the boundary
# between two commands on separate lines and let a `cd` on the first leak into the second.
_PUNCTUATION = "();<>|&\n"

_GIT = {"git", "git.exe"}
_CHDIR = {"cd", "pushd"}

# The pre-tokenizer reading, kept for text the lexer cannot lex at all. It splits raw
# characters, so it cannot tell a `&&` between two commands from one inside a quoted
# argument — which is the false positive tokenizing first exists to remove.
_SEGMENT = re.compile(r"&&|\|\||[;\n|]")

# What "the change has landed" looks like on the command line. Running one of these
# spends the worktree it ran in: the next edit has to start from a new one. They are
# found by `merge_calls`, which parses — see there for why a regex over the whole
# command string was both too eager and not eager enough.
_GH = {"gh", "gh.exe"}
_PYTHON = {"python", "python3", "py", "python.exe", "python3.exe", "py.exe"}
_LAND = {"land.py"}

# `FOO=bar gh pr merge` — a shell reads the assignments as environment for the command
# that follows, so the command is the first token that is not one.
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

# `<<EOF`, `<<-'EOF'`, `<< "EOF"` — the start of a heredoc, whose body is data. `<<<` is
# a here-string and not one, which the leading character class already excludes.
_HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")

# Branches a merge is never run into from a session. The same `protectedMergeTargets`
# key land.py reads, and empty by default for the same reason: for most repositories the
# integration branch *is* `development` or `main` and merging into it is the protocol.
#
# This is the half that catches `gh pr merge` typed directly, where land.py is not
# involved at all — without it, opting a repo in would only redirect the well-behaved
# path and leave the shortcut open.
DEFAULT_PROTECTED_MERGE_TARGETS: frozenset[str] = frozenset()


# --------------------------------------------------------------------------- paths


def key(path) -> str:
    """A comparable spelling of a path. Case-insensitive where the filesystem is.

    Symlinks are resolved, because the two paths being compared are frequently derived
    from different sources and only one of them has been through git. Measured on macOS:
    a repo under `/var/folders/…` records its worktrees' git dir as `/private/var/…`, so
    an unresolved comparison reads one repository as two — which made the guard stand
    down on the very tree it was protecting.
    """
    return os.path.normcase(os.path.realpath(str(path)))


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


def phrase(value) -> str | None:
    """A configured string, or None for anything that is not usably one."""
    return value.strip() if isinstance(value, str) and value.strip() else None


class Delivery:
    """How *this* repository delivers a change and takes a worktree down.

    Push, PR, `gh pr merge`, `ExitWorktree`, remove is the default, and it is right for
    most repositories. It is not right for all of them, and a gate that prescribes it
    anyway is worse than one that prescribes nothing. Measured 2026-08-23, in a repository
    that had dropped pull requests by recorded decision and delivers with a single
    command: the `Stop` block fired correctly on the invariant — a branch that exists only
    on this disk is not a delivered change, which was exactly that repository's own
    position — and then handed over five steps contradicting three of its decisions, one
    of them (`ExitWorktree`) unreachable there by construction. The session's cost is
    deciding which of two documents to believe, and nothing in the refusal answers it.

    So the invariant stays the guard's and the steps become the repository's, declared
    beside the integration branch in `.claude/worktree-per-change.json`:

        "delivery": {
          "command": "pnpm feature land",
          "teardown": "pnpm feature clean <name>",
          "enterWorktree": false
        }

    `<name>` and `<branch>` in the **teardown** are filled in with the worktree's leaf name
    and its branch, so what the gate prints is runnable rather than a template. Not in
    `command`: the briefing that prints it runs at `SessionStart`, where there is no
    worktree yet, and a placeholder filled in some messages and left standing in others is
    worse than one that never fills.

    Every key is optional and absence means the default, so a repository that has declared
    nothing reads exactly as it did before this existed. That is the point: the repository
    that needs this is the one that has already written its protocol down somewhere, and
    every other one should never learn that the mechanism is here.
    """

    def __init__(self, blob=None):
        blob = blob if isinstance(blob, dict) else {}
        self.command = phrase(blob.get("command"))
        self.teardown = phrase(blob.get("teardown"))
        # Only an explicit `false` turns it off. An absent key is not a repository saying
        # it enters by path; it is a repository that has not been asked.
        self.enter = blob.get("enterWorktree") is not False

    def fill(self, command: str, tree, topic: str | None) -> str:
        return command.replace("<name>", Path(tree).name).replace("<branch>", topic or "<branch>")

    def entering(self) -> str:
        """How a session gets into a worktree, as an instruction."""
        if self.enter:
            # Not "with that path": this sentence is also the first line of the
            # `SessionStart` briefing, where no path has been named yet.
            return "call **EnterWorktree** on the worktree you just created"
        return (
            "`cd` into that path — in a command of its own, with the path spelled out in "
            "full. Not through a shell variable and not joined to the next command with "
            "`&&`: this hook reads a `cd` or a `-C` argument as *tokens*, so `cd $W && git "
            "add …` and `git -C \"$W\" add …` are both unreadable, both fall back to the "
            "session's own directory, and both are therefore denied as the main checkout"
        )

    def protocol(self, branch: str) -> str:
        if not self.command:
            return PROTOCOL.format(branch=branch, enter=self.entering())
        return (
            f"The protocol: {self.entering()} before the first edit. Work, commit, then "
            f"`{self.command}` — this repository declares that as its own delivery, in "
            "place of push, PR and merge. A second change in the same session starts a "
            "new worktree — one worktree, one branch, one change."
        )

    def base_note(self, branch: str, worktrees: str) -> str:
        if self.enter:
            return BASE_NOTE.format(branch=branch, worktrees=worktrees)
        # Half of BASE_NOTE is about `worktree.baseRef` choosing the wrong base, which is
        # a property of EnterWorktree and reads as noise in a repository that never calls
        # it. The rule it exists to protect — cut from the FETCHED remote tip — is not.
        return (
            f"The base is `origin/{branch}` — the FETCHED remote tip — and never local "
            "HEAD, never whatever branch the main checkout is sitting on, and never an "
            "unfetched local ref. A stale base silently reintroduces work already landed "
            "as a conflict:\n"
            f"`git fetch origin {branch} && git worktree add {worktrees}/<name> "
            f"-b <branch> origin/{branch}`"
        )

    def finishing(self) -> str:
        """The teardown as one sentence, for the `SessionStart` briefing."""
        if self.teardown:
            return f"`{self.teardown}` takes it down."
        exit_first = '`ExitWorktree` (`action: "keep"`), then ' if self.enter else ""
        return (
            f"after the merge, {exit_first}`git worktree remove <path>` and "
            "`git branch -D <branch>` from the main checkout."
        )

    def deliver(self, branch: str, protected: bool = False) -> str:
        """The steps from an uncommitted worktree to a delivered change.

        `protected` is this repository having declared that no session merges into this
        branch. The steps then stop at the open pull request: prescribing a merge that the
        guard denies a moment later is the same self-contradiction a declared `delivery`
        block exists to remove.
        """
        commit = COMMIT_STEP
        if self.command:
            return (
                commit
                + f"2. `{self.command}` — this repository's declared delivery command. It "
                "is what this repository has instead of the push-PR-merge sequence, so do "
                "not reconstruct that sequence by hand here."
            )
        if protected:
            last = (
                f"4. Leave the pull request open. `{branch}` is a protected branch here, "
                "so merging it is a person's decision — say in your reply that it is "
                "open and waiting, and do not delete the branch it is opened from."
            )
        else:
            last = (
                "4. `gh pr merge --squash` (add `--admin` only if the repo's checks do "
                "not apply here). Never `--delete-branch`: it makes `gh` check out the "
                "base branch this main checkout permanently holds, so it fails *after* "
                "the merge and leaves the branch it was asked to delete on the remote."
            )
        return (
            commit
            + "2. `git push -u origin HEAD`\n"
            f"3. `gh pr create --base {branch} --fill`\n"
            + last
            + "\n"
        )


def delivery(main_root: Path | None) -> Delivery:
    if main_root is None:
        return Delivery()
    try:
        blob = json.loads((main_root / ".claude" / CONFIG_FILENAME).read_text(encoding="utf-8"))
        return Delivery(blob.get("delivery"))
    except (OSError, ValueError, AttributeError):
        return Delivery()
def protected_targets(main_root: Path | None) -> frozenset[str]:
    """What this repository refuses to merge into, from its own record.

    Read from the same per-repo config as the branch, and additive only: there is no key
    that removes a name and no environment override, so a repo that has opted in cannot be
    talked back out of it by a later `CLAUDE_INTEGRATION_BRANCH`.
    """
    if main_root is not None:
        try:
            blob = json.loads((main_root / ".claude" / CONFIG_FILENAME).read_text(encoding="utf-8"))
            extra = blob.get("protectedMergeTargets")
            if isinstance(extra, list):
                names = {str(n).strip().lower() for n in extra if str(n).strip()}
                return DEFAULT_PROTECTED_MERGE_TARGETS | frozenset(names)
        except (OSError, ValueError, AttributeError):
            pass
    return DEFAULT_PROTECTED_MERGE_TARGETS


def is_protected(branch: str, protected: frozenset[str]) -> bool:
    """Match the bare branch name, `origin/`-qualified or not, case-insensitively.

    Normalised rather than compared literally so `Develop`, `origin/develop` and
    `refs/heads/develop` are not three ways past the same check.
    """
    name = (branch or "").strip().lower()
    for prefix in ("refs/heads/", "refs/remotes/"):
        if name.startswith(prefix):
            name = name[len(prefix) :]
    if "/" in name:
        name = name.rsplit("/", 1)[-1]
    return name in protected


def reason_protected_merge(branch: str) -> str:
    """Why a merge into a shared trunk is refused, and what to do instead."""
    return (
        f"**`gh pr merge` is not run against `{branch}`.**\n\n"
        f"`{branch}` is a protected branch: the pull request into it *is* the review, so "
        "merging it is a person's decision and not one this session takes.\n\n"
        "The change is still delivered the same way — push and open the PR:\n\n"
        "```\ngit push -u origin HEAD\ngh pr create --base "
        f"{branch} --fill\n```\n\n"
        "Then leave it open and say so. `land.py` does exactly this and stops at the same "
        "point.\n\n"
        "Squash-merging without review is for a batch branch you own; point "
        "`integrationBranch` in `.claude/worktree-per-change.json` at one if that is what "
        "this is."
    )


def worktrees_root(main_root: Path | None) -> str:
    """Where this repository's worktrees go, as the remedy text should spell it.

    Read from the same per-repo config as the branch, for the same reason: repositories
    differ and neither answer is wrong. A repo that gitignores `.claude/` wants the
    default; one that does not needs them somewhere outside itself, and a few keep them
    beside the checkout so an editor indexes one tree at a time.

    Text only. Getting it wrong misleads a reader and cannot mis-classify a directory —
    which is exactly why it is worth reading rather than assuming, because a remedy that
    names a path the repo ignores nowhere sends the next session to create untracked
    files it will then be told off for.
    """
    override = (os.environ.get("CLAUDE_WORKTREES_ROOT") or "").strip()
    if override:
        return override
    if main_root is not None:
        try:
            blob = json.loads((main_root / ".claude" / CONFIG_FILENAME).read_text(encoding="utf-8"))
            name = blob.get("worktreesRoot")
            if isinstance(name, str) and name.strip():
                return name.strip().rstrip("/\\")
        except (OSError, ValueError, AttributeError):
            pass
    return DEFAULT_WORKTREES_ROOT


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
    # An unfinished rebase or merge outranks the marker, because the marker is written
    # before `gh pr merge` runs and a refused merge leaves the same one. Conflict
    # resolution is a tree full of edits, every one of which this denial would refuse
    # while telling the session its work was already delivered — and there is no remedy
    # it could print, since the gate is the operator's and a fresh worktree abandons the
    # conflict. Measured on 2026-08-13 against a `DIRTY` PR whose merge was refused.
    #
    # It does open a way past a *true* marker: start a rebase, then edit. That is a
    # deliberate act, not an accident, and it sits with the redirect and the aliased
    # `git` in "Limits" — the guard is here to stop a session continuing a merged branch
    # by mistake, not to win against one determined to.
    if mid_operation(tree_root):
        return None
    return marker


def mid_operation(tree: Path) -> bool:
    """Whether a rebase, merge or cherry-pick is unfinished in this worktree.

    Stat-only, and deliberately so: this runs over every spent marker at SessionStart, and
    the one thing the guard may not become is a `git` subprocess on a path it takes
    routinely. Git records an in-progress operation as state inside the worktree's own git
    dir, so its presence is the whole test.

    What it is for: a tree in this condition cannot be a delivered change, whatever a
    marker says about it. Conflict resolution is the work, and it is happening now.
    """
    try:
        pointer = (tree / ".git").read_text(encoding="utf-8").strip()
    except OSError:
        return False
    if not pointer.startswith("gitdir:"):
        return False
    git_dir = Path(pointer.split(":", 1)[1].strip())
    return any(
        (git_dir / name).exists()
        for name in ("rebase-merge", "rebase-apply", "MERGE_HEAD", "CHERRY_PICK_HEAD")
    )


def sweep_spent(common: Path) -> tuple[list[str], list[str]]:
    """Landed worktrees still on disk, directories that are only their remains, and a
    marker file dropped for everything that is no longer a worktree.

    All three are cleanup. The first list is what a session inherits from one that crashed
    or was killed between `gh pr merge` and taking its tree down, which nothing else
    reports: a merged worktree is indistinguishable from an in-progress one to anybody
    reading `git worktree list`.

    The second list is the same sweep telling the truth about a *half-finished* teardown.
    `git worktree remove` deregisters the worktree first and deletes the files second, and
    when the delete fails it keeps the deregistration — so git goes quiet while the whole
    checkout is still sitting there. A directory in that state is not a worktree and must
    not be reported as one, because the remedy for a worktree is the command that has
    already run and now refuses: `fatal: '<path>' is not a working tree`. Measured
    2026-08-28 in the first repository to adopt this guard: three leftover directories
    under `.claude/worktrees/`, one of them a full checkout with `node_modules` in it,
    while `git worktree list` named only the main checkout — and the sweep had been asking
    every new session to `git worktree remove` one of them since the day it was left.

    The test is the same single stat the rest of this guard turns on: `.git` is a *file* in
    a linked worktree, and a directory git has let go of does not have one at all.

    Dropping the marker for a tree that is gone — or that is now only a directory — is not
    tidiness either. Markers are keyed by the worktree's *leaf name*, so a stale one denies
    the first edit in the next worktree that happens to be named the same — a fresh tree
    reported as already merged, which is the most confusing denial this guard can produce.
    The marker also has nothing left to protect once the tree is deregistered: whatever is
    inside that directory now resolves to the main checkout, which is denied anyway.

    A tree mid-rebase is left out of the lists entirely, and its marker is left alone. The
    marker is written *before* `gh pr merge` runs, so a merge that failed leaves exactly the
    same file as one that landed; measured on 2026-08-13, a `DIRTY` PR whose merge was
    refused had a spent marker, an unresolved rebase and ten modified files, and this sweep
    named it to every new session as merged and asked for it to be removed. Being wrong in
    that direction costs somebody else's conflict resolution, which is the most expensive
    thing this hook could destroy, so the in-progress trees are the ones it stays quiet
    about.
    """
    standing: list[str] = []
    remains: list[str] = []
    try:
        entries = sorted((state_dir(common) / "spent").iterdir())
    except OSError:
        return standing, remains
    for marker in entries:
        try:
            tree = json.loads(marker.read_text(encoding="utf-8")).get("tree")
        except (OSError, ValueError, AttributeError):
            continue
        if not isinstance(tree, str) or not tree:
            continue
        path = Path(tree)
        if path.is_dir():
            if (path / ".git").exists():
                if not mid_operation(path):
                    standing.append(tree)
                continue
            remains.append(tree)
        try:
            marker.unlink()
        except OSError:
            pass
    return standing, remains


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


def tokenize(command: str) -> list[str] | None:
    """Shell tokens, quoting intact, operators as tokens of their own. None if unlexable.

    Tokenizing *before* looking for command boundaries is the whole point of this
    function. Splitting raw text on `&&`, `|` and newlines reads the inside of a quoted
    argument as shell: measured on 2026-08-13, a `gh pr create --body "…"` whose body held
    the line `cd ~/x && git add -A` and a markdown table of pipes was denied as a `git add`
    in the main checkout. The lexer hands that body back as a single token, and a single
    token is never a command.

    `posix=False` — the default — is what keeps the quotes on, and they are exactly what
    separates a quoted argument from a bare word. `unquote` takes them off again at the two
    places that read a token as a path or as a command name, and nowhere else.
    """
    lexer = shlex.shlex(command, punctuation_chars=_PUNCTUATION)
    lexer.whitespace_split = True
    lexer.whitespace = " \t\r"
    # `#` starts no comment, matching `shlex.split`, which turns comments off too. Dropping
    # the rest of a line would hide whatever git call sits on it, and losing a call is the
    # one direction this guard may not fail in.
    lexer.commenters = ""
    try:
        return list(lexer)
    except ValueError:
        return None


def unquote(token: str) -> str:
    """A token with its shell quoting removed and nothing else touched.

    Deliberately not `shlex.split`, which is POSIX-mode and consumes backslashes as well:
    that would turn a PowerShell `C:\\Users\\x` into `C:Usersx` and hand the guard a path
    resolving nowhere, on the one platform where `\\` is the separator.
    """
    out: list[str] = []
    quote = ""
    for character in token:
        if quote:
            if character == quote:
                quote = ""
            else:
                out.append(character)
        elif character in "'\"":
            quote = character
        else:
            out.append(character)
    return "".join(out)


def names(token: str, commands: set[str]) -> bool:
    """Whether a token is one of `commands` *as a command* — quoting allowed, prose not.

    `"git"` counts: a shell runs it, so declining to read a quoted spelling would make the
    whole guard one quote deep. A token holding whitespace does not count, and that is the
    fix — `"cd ~/x && git add -A"` arrives from the lexer whole precisely so it can be told
    apart from a command, and no command this guard cares about is spelled with a space.
    """
    bare = unquote(token)
    if not bare or any(character.isspace() for character in bare):
        return False
    return Path(bare).name in commands


def operator(token: str) -> bool:
    """A token the lexer built out of punctuation alone — `&&`, `;`, `|`, a newline, a run
    of them — which is where one command ends and the next begins."""
    return bool(token) and all(character in _PUNCTUATION for character in token)


def dir_token(token: str) -> str | None:
    """A `cd` or `-C` argument as a directory, or None when it is not one to read.

    `~` is expanded, because that expansion is deterministic and needs no shell. Anything
    a shell would have to *evaluate* — `$VAR`, a backtick, a glob, `cd -` — is not, and
    reads as None: guessing at it would be the guard trusting an expansion it cannot see.

    The token still carries its quotes, because that is how the lexer marks an argument;
    they come off first so that `cd "/tmp/a b"` reads as a path rather than as nothing.
    """
    token = unquote(token)
    if not token or token.startswith("-"):
        return None
    if any(character in token for character in "$`*?"):
        return None
    return os.path.expanduser(token)


def joined(base: str | None, token: str | None) -> str | None:
    """`cd` and `-C` composed left to right, the way a shell and repeated `-C` compose.

    None propagates: a leg this hook could not read makes the whole chain unreadable, and
    an unreadable chain falls back to the session's own tree, which is the conservative
    end (it is the tree the guard is there to protect).
    """
    if token is None:
        return None
    return os.path.join(base, token) if base else token


def segments(command: str) -> list[list[str]]:
    """The command as one token list per command, with every token's quoting still on.

    Two readings, and the first is the one that runs. `tokenize` lexes the whole string
    and hands back operators as tokens, so a `&&` or a `|` that is merely *inside* an
    argument stays inside it. Only text the lexer refuses outright — an unbalanced quote,
    and nothing else met in practice — falls back to splitting the raw characters.

    The fallback over-reports boundaries, which costs a false denial; a parser that
    declined to read such text at all would under-report them, which costs a missed one.
    Only the first of those two is a failure a guard may have.
    """
    tokens = tokenize(command)
    if tokens is None:
        commands = []
        for segment in _SEGMENT.split(command):
            try:
                commands.append(shlex.split(segment, posix=False))
            except ValueError:
                commands.append(segment.split())
        return commands
    commands, current = [], []
    for token in tokens:
        if operator(token):
            commands.append(current)
            current = []
        else:
            current.append(token)
    commands.append(current)
    return commands


def git_calls(command: str):
    """Every git subcommand in a shell command, as (subcommand, args, where).

    `where` is the directory the call would run in as far as the *text* says: a `cd`
    earlier in the chain, one or more `git -C`, or None when the command names none — or
    names one this hook cannot read, `git -C "$W" switch` being the standing example.
    Both spellings of None mean the same thing downstream, "the session's own tree",
    which is what an ordinary command means and the conservative reading of one that
    could not be parsed.

    Every `git` in a segment is read, not only the first. A newline is an operator here
    and a segment should therefore hold one command, but that rests on a lexer setting
    rather than on anything structural, and stopping at the first `git` would turn a
    change in that setting into silently missed calls — which is the failure this guard
    is not allowed to have. Scanning on costs nothing when the assumption holds.

    Still textual, and still never a shell: the cost is spelling a path out in full
    instead of hiding it in a variable, and the alternative is trusting an expansion the
    guard cannot evaluate.
    """
    calls = []
    chain = None
    for tokens in segments(command):
        if not tokens:
            continue
        if names(tokens[0], _CHDIR):
            # A `cd` carries to every later segment, which is what `&&` and `;` do. It is
            # read for its path only — whether the `cd` would have *succeeded* is not a
            # question worth answering, since a command whose `cd` failed writes nothing.
            chain = joined(chain, dir_token(tokens[1])) if len(tokens) > 1 else None
            continue
        index = 0
        while index < len(tokens):
            if not names(tokens[index], _GIT):
                index += 1
                continue
            where = chain
            index += 1
            while index < len(tokens):
                flag = unquote(tokens[index])
                if not flag.startswith("-"):
                    break
                if flag == "-C":
                    where = joined(
                        where,
                        dir_token(tokens[index + 1]) if index + 1 < len(tokens) else None,
                    )
                index += 2 if flag in {"-C", "-c"} else 1
            if index < len(tokens):
                calls.append(
                    (unquote(tokens[index]), [unquote(t) for t in tokens[index + 1 :]], where)
                )
                index += 1
    return calls


def merges(tokens: list[str]) -> str | None:
    """What this one command would merge the change with, or None if it would not.

    Only the **command position** is read, unlike `git_calls`, which reads every `git` in
    a segment. That difference is deliberate and it is the whole fix: `echo gh pr merge`,
    a `grep` for the phrase, and a heredoc writing a document that quotes it all put those
    three words in a command string without running anything, and a scan that read them
    anywhere would spend the worktree for each. Measured 2026-08-22: a repository writing
    its own protocol docs through a heredoc marked its tree merged and was denied its next
    edit, with no pull request anywhere to have merged.

    Under-reporting is the safe direction *here*, and only here. A missed `git` on the
    write path is an unguarded mutation, so `git_calls` over-reports on purpose. A missed
    merge is a mark not written — and the mark is a convenience whose own denial tells you
    to confirm with the forge anyway. A mark written wrongly, by contrast, denies every
    further edit in a tree that has delivered nothing.
    """
    index = 0
    while index < len(tokens) and _ASSIGNMENT.match(unquote(tokens[index])):
        index += 1
    if index >= len(tokens):
        return None
    head = tokens[index]
    rest = (unquote(token) for token in tokens[index + 1 :])
    words = [word for word in rest if word and not word.startswith("-")]
    if names(head, _GH):
        # `gh pr merge`, and not `gh pr view` or `gh pr list`. The pair is looked for
        # anywhere in the arguments rather than at their head, because `gh --repo o/r pr
        # merge` puts a flag's *value* in front of the subcommand and dropping tokens that
        # merely start with `-` does not remove it.
        adjacent = any(words[at : at + 2] == ["pr", "merge"] for at in range(len(words)))
        return "gh pr merge" if adjacent else None
    if names(head, _LAND):
        return "land.py"
    if names(head, _PYTHON):
        # `python .claude/scripts/land.py`, which is the shape SKILL.md now recommends and
        # which the old regex never saw at all: the string holds no `gh pr merge`, so the
        # supported delivery route left no mark, the `SessionStart` sweep never reported
        # the tree, and the cleanup gate never fired for it.
        return "land.py" if any(Path(word).name in _LAND for word in words) else None
    return None


def without_heredocs(command: str) -> str:
    """The command with every heredoc BODY dropped, keeping the commands around it.

    A heredoc body is the text a command is *given*, not text a shell runs, and the
    lexer cannot know that: `|`, `&` and `;` inside it are read as operators, so a line
    of a markdown table splits into segments and any word can end up looking like a
    command. That is how the measured false positive happened — a session writing this
    protocol's own documentation through `cat > contract.md <<EOF`, with a table row
    naming `gh pr merge` as the thing that spends a worktree, spent its worktree and was
    denied its next edit.

    Used for the merge reading only. `git_calls` still reads heredoc bodies, and should:
    a body it wrongly reads as `git add` in the main checkout costs a denial the session
    can work around in one call, while `bash <<EOF` and `cat <<EOF | sh` genuinely do run
    theirs. The asymmetry is the same one `merges` is built on — over-report on the write
    path, under-report on the mark.
    """
    lines = command.splitlines()
    kept, index = [], 0
    while index < len(lines):
        line = lines[index]
        kept.append(line)
        index += 1
        for match in _HEREDOC.finditer(line):
            delimiter = match.group(2)
            while index < len(lines) and lines[index].strip() != delimiter:
                index += 1
            index += 1  # the terminator line itself, which is not body either
    return "\n".join(kept)


def merge_calls(command: str) -> list[tuple[str, str | None]]:
    """Every merge in a shell command, as (what, where) — the same reading as `git_calls`.

    `where` matters as much as `what`. The mark used to be filed against the tree the
    *session* was in, which is only the tree that merged when the merge was run bare.
    Measured 2026-08-23: a session ran `cd <other-worktree> && gh pr merge`, and the mark
    landed on its own harness-made tree — stamped with a branch that had no pull request,
    refusing that session's `Stop` — while the tree that actually merged went unmarked and
    would have been editable afterwards. Both halves are wrong from one missing reading,
    and `cd` composition is a thing this parser already does.
    """
    calls: list[tuple[str, str | None]] = []
    chain = None
    for tokens in segments(without_heredocs(command)):
        if not tokens:
            continue
        if names(tokens[0], _CHDIR):
            chain = joined(chain, dir_token(tokens[1])) if len(tokens) > 1 else None
            continue
        what = merges(tokens)
        if what is not None:
            calls.append((what, chain))
    return calls


# ------------------------------------------------------------------------- reports


PROTOCOL = (
    "The protocol: {enter} before the first edit. Work, commit, "
    "`git push -u origin HEAD`, open a PR into `{branch}` with `gh pr create --base "
    "{branch}`, then `gh pr merge`. A second change in the same session starts a new "
    "worktree — one worktree, one branch, one PR, one change."
)

BASE_NOTE = (
    "The base is `origin/{branch}` — the FETCHED remote tip — and never local HEAD, never "
    "whatever branch the main checkout is sitting on, and never an unfetched local ref. So "
    "create the worktree with git first and enter that path:\n"
    "`git fetch origin {branch} && git worktree add {worktrees}/<name> -b <branch> "
    "origin/{branch}` then EnterWorktree with that path. `worktree.baseRef` never accepts a "
    "branch name — it chooses between the repository's default branch and local HEAD, and "
    "here BOTH are wrong — so a bare EnterWorktree cuts from the wrong place and carries "
    "changes you did not make into your diff without complaining."
)

ESCAPE = (
    "`/worktree-per-change` has the full protocol. **A session cannot turn this guard "
    "off**, and reading otherwise wastes a turn finding out: `CLAUDE_WORKTREE_GATE` is "
    "read from the hook's own environment, so a `CLAUDE_WORKTREE_GATE=off` prefix sets it "
    "for that one command while the hook that denied the command has already run. Setting "
    "it for the Claude Code process, or in a settings `env` block, is the operator's move "
    "and takes a new session. So if this denial is provably wrong, the move that works is "
    "to say so plainly in your reply — what you were doing, what it blocked, and why the "
    "guard is wrong — and stop, rather than spending turns on a way around it."
)


# The first step of every delivery, named once so the branches below cannot drift.
COMMIT_STEP = (
    '1. `git add <paths> && git commit -m "..."` — name the paths; never `git add -A`.\n'
)


def cleanup_steps(tree: Path | str, topic: str | None, plan: Delivery) -> str:
    """How a landed worktree comes down, spelled out because two of the four steps trap.

    `ExitWorktree` with `action: "remove"` is the one everybody reaches for, and it cannot
    do this job: it removes only a worktree EnterWorktree *itself* created, where under this
    protocol the tree is made with `git worktree add` and entered by path. Measured — it
    refuses outright, saying the session does not own the worktree and to use `"keep"`. So
    the cost is a wasted call rather than a tree that quietly stays, and asking for `"keep"`
    up front is what turns four steps into four steps instead of five.

    A repository that has declared its own teardown gets that instead, and one that does
    not enter worktrees loses the `ExitWorktree` step rather than being told to take a
    step it cannot reach. See `Delivery`.
    """
    name = topic or "<branch>"
    if plan.teardown:
        return (
            f"`{plan.fill(plan.teardown, tree, topic)}` — this repository's declared "
            "teardown, which is what it has in place of the four commands. Confirm the "
            "change landed first if it does not confirm that itself: the worktree, the "
            "local branch and the remote branch only mean anything together, and a "
            "teardown run on a merge the forge refused throws away the branch that still "
            "has to reach the integration branch."
        )
    steps = [
        "`gh pr view <n> --json state --jq .state` — expect `MERGED`. Ask the forge, "
        "not git: `git branch -d`, `--merged` and `merge-base --is-ancestor` all read a "
        "squash-merged branch as unmerged, so under this protocol all three are false "
        "negatives."
    ]
    if plan.enter:
        steps.append(
            '`ExitWorktree` with `action: "keep"` — it returns the session to the main '
            'checkout. **Not `"remove"`**: that removes only a worktree EnterWorktree '
            "created itself, and refuses on one it merely entered by path, so it cannot "
            "take this tree down."
        )
    steps.append(
        f"`git worktree remove {tree}` — from the main checkout, which is where it is "
        "allowed and the only place it can run. Nothing can remove the tree it is "
        "standing in."
    )
    steps.append(
        f"`git branch -D {name}`, then `git ls-remote --heads origin {name}` and "
        f"`git push origin --delete {name}` if that prints anything. "
        "`--delete-branch` deletes the local branch first and abandons the remote one when "
        "that fails, which is the normal case here because your worktree still has the "
        "branch checked out at merge time. Ask the REMOTE, not `git branch -r` behind a "
        "pruning fetch: merging your own PR moves the integration branch, so that fetch is "
        "the one likely to die on `cannot lock ref`, and then either it takes the check down "
        "with it or the check answers from a stale cache and the branch looks already gone."
    )
    return "\n".join(f"{number}. {step}" for number, step in enumerate(steps, start=1))


def reason_main_checkout(what: str, branch: str, plan: Delivery, worktrees: str) -> str:
    return (
        f"Denied: {what} in the main checkout. Every change in this repository is made "
        "in its own worktree, on its own branch, and reaches the integration branch as a "
        "merged PR — there is no size of change that skips that, because the exception is "
        "what puts two writers back in one directory and half-finished work into someone "
        "else's commit.\n\n"
        + plan.protocol(branch)
        + "\n\n"
        + plan.base_note(branch, worktrees)
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


def reason_spent(marker: dict, branch: str, common: Path, tree_root: Path,
                 plan: Delivery, worktrees: str) -> str:
    landed = marker.get("why") or "its PR merged"
    return (
        f"Denied: this worktree's change looks finished ({landed}), so editing it again "
        "grows a branch that has been reviewed and merged, and the new edit reaches nobody "
        "until someone notices and opens a second PR from a tree that looks done.\n\n"
        "The next change is a new one: cut a fresh worktree and branch from the current "
        f"`origin/{branch}`, so it already contains what you just merged, and then "
        f"{plan.entering()}.\n\n"
        # The mark records that `gh pr merge` RAN, not that it succeeded, and this is the
        # one denial in the guard that can therefore be flatly wrong about the state of
        # the world. Saying so here is not hedging: measured 2026-08-13, a session whose
        # merge GitHub refused for a conflict read this text as fact, believed its work
        # was delivered, and spent two turns reporting a guard bug instead of clearing a
        # marker. The unfinished-rebase check above catches the common shape of that; a
        # merge refused for a failing check leaves no rebase and still lands here.
        + spent_doubt(spent_marker(common, tree_root))
        + "\n\n"
        + plan.base_note(branch, worktrees)
        + "\n\n"
        + ESCAPE
    )


def spent_doubt(marker_path: Path) -> str:
    """The sentence that lets a wrongly-marked session out, with the check that decides it.

    Named after what it is: this mark is written *before* `gh pr merge` runs, so it records
    an attempt. Every other denial in this guard is about state git can be asked for
    directly; this one is about something that happened earlier and might not have worked.
    """
    return (
        "**But this mark records that the merge command RAN, not that it landed** — it is "
        "written before the command, because no later hook can tell a merge from a merge "
        "that failed. A merge GitHub refuses (a conflict with a base that moved, a failing "
        "check) leaves exactly this mark. So ask the forge before believing it:\n"
        "`gh pr view <n> --json state --jq .state`\n\n"
        "**`MERGED`** — the tree really is finished; take the fresh worktree above. "
        "**Anything else** — the mark is wrong, and clearing it is the fix rather than a "
        "workaround:\n"
        f"`rm {marker_path}`\n"
        "Then carry on in this tree. Clearing a mark for a PR the forge calls `MERGED` is "
        "how a merged branch grows a commit that reaches nobody, so check first, every time."
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


def counted(value: str | None) -> int:
    return int(value) if (value or "").isdigit() else 0


def has_ref(tree: Path, ref: str) -> bool:
    return git(tree, "rev-parse", "--verify", "--quiet", ref) is not None


def undelivered(tree: Path, branch: str, topic: str | None,
                protected: bool = False) -> str | None:
    """The commits in this worktree that nobody else can reach yet, as a phrase.

    `origin/<branch>..HEAD` was the whole test and it is not the question. It counts
    every commit the integration branch has not got — including the ones the repository's
    **default** branch has and the integration branch does not, which in a repo that
    integrates through `development` while defaulting to `main` is the entire divergence
    between them. Every worktree the harness cuts is on that history, so the gate fired at
    every `Stop` forever, in a session that had delivered everything it did.

    Measured 2026-08-24: one commit counted, and it was `origin/main`'s tip. The five
    steps the block then prescribes would have pushed that history and squash-merged some
    fifty thousand deletions onto `development` — so this is not a noisy gate, it is a
    gate whose remedy is destructive when it is wrong.

    Two questions instead of one, because the two undelivered states are genuinely
    different and only the first was ever really about "exists only on this disk":

    * commits on no remote at all — unpushed work, the strong case;
    * commits pushed to this worktree's own branch and not yet on the integration
      branch — a branch left standing with nobody merging it.

    A commit that is published on some *other* remote branch is neither. It is somebody
    else's landed work that this tree happens to sit on, and it is not this session's to
    deliver.

    The second question is dropped where the integration branch is a
    `protectedMergeTargets` name, because there a pushed topic branch **is** the finished
    state: no session merges into that branch, by that repository's own declaration, so a
    session that committed, pushed and opened the PR has delivered everything it is
    permitted to. Asking it anyway refuses `Stop` twice in a session that did the protocol
    exactly, which is the same shape of wrong gate as the `origin/<branch>..HEAD` count
    above — correct arithmetic, wrong question. Commits on no remote at all still count:
    those are undelivered under any repository's rules.
    """
    if not has_ref(tree, f"refs/remotes/origin/{branch}"):
        # No tracking ref for the integration branch: a local-only clone, or a fetch that
        # has never run. Nothing here can be compared against what was published — and
        # `--not --remotes` with no remote refs at all would count the whole history,
        # which is how a fail-open check becomes a session that cannot stop.
        return None
    only_here = counted(git(tree, "rev-list", "--count", "HEAD", "--not", "--remotes"))
    if only_here:
        return f"{only_here} commit(s) that are on no remote"
    if protected:
        return None
    if topic and has_ref(tree, f"refs/remotes/origin/{topic}"):
        pushed = counted(git(tree, "rev-list", "--count", f"origin/{branch}..origin/{topic}"))
        if pushed:
            return f"{pushed} commit(s) pushed to origin/{topic} and not in origin/{branch}"
    return None


def unlanded(tree: Path, branch: str, topic: str | None,
             protected: bool = False) -> str | None:
    """What this worktree is holding that the integration branch has not got.

    Returns a human sentence, or None when there is nothing to keep the session open
    for. Every question it cannot answer resolves to None: a `Stop` hook that blocks on
    a git call that merely failed would strand a session with no way out.
    """
    dirty = git(tree, "status", "--porcelain")
    if dirty is None:
        return None

    parts = []
    if dirty:
        parts.append(f"{len(dirty.splitlines())} uncommitted file(s)")
    held = undelivered(tree, branch, topic, protected)
    if held:
        parts.append(held)
    return " and ".join(parts) if parts else None


def block_stop(tree: Path, branch: str, topic: str | None, holding: str,
               plan: Delivery, protected: bool = False) -> None:
    emit(
        {
            "decision": "block",
            "reason": (
                f"This worktree is holding {holding}. A branch that exists only on this "
                "disk is not a delivered change — the operator is left with a directory "
                "nobody will look in, and the next session cuts its worktree from an "
                f"`origin/{branch}` that is missing your work.\n\n"
                "Finish it before stopping:\n"
                + plan.deliver(branch, protected)
                + "\nThen take the worktree down:\n\n"
                + cleanup_steps(tree, topic, plan)
                + "\n\nIf the change is genuinely abandoned, say so plainly in your reply "
                "and leave the worktree standing — do not delete it, and do not stash."
            ),
        }
    )


def block_stop_cleanup(tree: Path, branch: str, topic: str | None, marker: dict, plan: Delivery) -> None:
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
                f"This worktree recorded a merge ({landed}) and is still standing. Confirm "
                "that against the forge before acting on it: the record is written before "
                "the merge runs, so a merge the forge refused leaves the same one. If it "
                "did not land, finish the change instead — do not take the tree down.\n\n"
                "Otherwise, taking it down is part of finishing, not an errand to hand over: "
                "a worktree with no live branch is a stale checkout, a merged branch is a "
                "push target after the PR that reviewed it has closed, and either one left "
                "behind costs the next session a status check before it can trust what it "
                "is looking at.\n\n"
                + cleanup_steps(tree, topic, plan)
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


def targeted(where: str | None, cwd: Path, session, common: Path):
    """The tree an operation lands in — but only when that tree is *this* repository's.

    None means it is none of this repository's business: another repository entirely, or
    no repository at all. The answer comes from the path the operation names rather than
    from the directory the session sits in, because those differ constantly — a
    `cd`-then-git into a sibling checkout, a `git -C` back into the main checkout, an
    absolute path into a worktree — and in every one of those the session's own status is
    the wrong thing to judge by. `where` of None means the command named nothing (or named
    something unreadable), and then the session's own tree is both the honest reading and
    the conservative one.

    "The same repository" is the shared **common** git directory, not a path prefix: a
    linked worktree lives inside the main checkout's directory tree, so a prefix test
    reads every worktree as the main checkout, and an unrelated clone that happens to sit
    inside it as this repo's business. The common dir is neither.
    """
    if where is None:
        return session
    directory = Path(where)
    if not directory.is_absolute():
        directory = cwd / directory
    found = find_tree(directory)
    if found is None:
        return None
    if key(common_git_dir(found[1])) != key(common):
        return None
    return found


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
    plan = delivery(main_root)
    worktrees = worktrees_root(main_root)

    if event == "SessionStart":
        context = (
            "This repository writes only from worktrees. Edits to the main "
            "checkout are denied by a hook, including one-line ones.\n\n"
            + plan.protocol(branch)
            + "\n\n"
            + plan.base_note(branch, worktrees)
            + "\n\nA change is finished when its worktree is gone too: "
            + plan.finishing()
        )
        # The sweep runs at SessionStart deliberately: it is the one moment nothing is in
        # flight, so a landed tree still on disk is somebody's leftovers rather than the
        # work in progress two minutes from its own merge.
        standing, remains = sweep_spent(common)
        if standing:
            context += (
                "\n\nWorktrees still on disk that recorded a merge, left by an earlier "
                "session:\n"
                + "\n".join(f"- {path}" for path in standing)
                + "\nEach ran a merge command from inside itself. That is recorded *before* "
                "the merge, so a merge the forge refused leaves the same record as one that "
                "landed — confirm with `gh pr view <n> --json state` before removing "
                "anything, and treat uncommitted changes as a merge that did not land. Then "
                "`git worktree remove <path>` and `git branch -D <branch>`, from the main "
                "checkout, for the ones that are yours. A worktree another session is "
                "holding is its business even after its branch merges: leave it, and say it "
                "is there."
            )
        if remains:
            context += (
                "\n\nDirectories that recorded a merge and are no longer worktrees "
                "at all:\n"
                + "\n".join(f"- {path}" for path in remains)
                + "\n`git worktree remove` deregistered each of these and then failed "
                "to delete the files, so `git worktree list` is clean while the checkout "
                "is still there, and running that command again refuses with `is not a "
                "working tree`. Delete the directory itself, and expect that to fail "
                "while another process still holds a file inside it. None of this is a "
                "live worktree, so none of it is anybody's work in progress."
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
            block_stop_cleanup(tree_root, branch, topic, marker, plan)
            return
        protected = is_protected(branch, protected_targets(main_root))
        holding = unlanded(tree_root, branch, topic, protected)
        if holding:
            stop_blocks(common, session, bump=True)
            block_stop(tree_root, branch, topic, holding, plan, protected)
        return

    if event != "PreToolUse":
        return

    tool = payload.get("tool_name", "")

    if tool in FILE_TOOLS:
        for path in target_paths(payload, Path(cwd)):
            scope = targeted(str(path), Path(cwd), located, common)
            if scope is None:
                continue  # Outside this repository — not this repository's rule.
            target_root, target_git_dir, target_linked = scope
            if not target_linked:
                deny(reason_main_checkout("file edits are not made", branch, plan, worktrees), warn_only)
                return
            topic = branch_of(target_git_dir)
            if topic == branch:
                deny(reason_integration_branch(branch), warn_only)
                return
            marker = is_spent(common, target_root, topic)
            if marker:
                deny(reason_spent(marker, branch, common, target_root, plan, worktrees), warn_only)
                return
        return

    if tool not in SHELL_TOOLS:
        return

    command = (payload.get("tool_input") or {}).get("command")
    if not isinstance(command, str):
        return

    merges_here = merge_calls(command)
    if merges_here and is_protected(branch, protected_targets(main_root)):
        # Denied BEFORE anything is marked below, and the order is the whole point: a
        # denied merge never ran, so spending the worktree here would strand a live change
        # in a tree the guard then refuses to edit — the change would need a new worktree
        # to finish something that never started.
        #
        # Not gated on `linked`, unlike the marking. A merge into a shared trunk is
        # refused wherever it is typed; the main checkout is if anything the more likely
        # place for someone to try it.
        #
        # `land.py` is in `merges_here` too, and denying it is right: a repo with a
        # protected target has told this hook that no session merges into it, and a
        # session that has to reach for `land.py`'s push-and-open half can run its two
        # commands. Denying is what makes the refusal legible — `land.py` stopping
        # halfway with an open PR reads, to the session, like a step that did not work.
        deny(reason_protected_merge(branch), warn_only)
        return

    for what, where in merges_here:
        # Recorded *before* the merge runs rather than after, because there is no
        # after-hook that can tell a merge apart from a merge that failed.
        #
        # Being wrong in this direction is *survivable*, not harmless, and the difference
        # is what `mid_operation` and `spent_doubt` are for. "The remedy is a new
        # worktree, which is what the protocol wanted anyway" — the old note here — holds
        # only when the merge actually landed. When it did not, the branch still has to
        # reach the integration branch, and a fresh worktree abandons the conflict while
        # leaving an open PR that can never merge. So: an unfinished rebase outranks the
        # mark, and the denial names the forge check and the marker path rather than
        # asserting the change is done.
        scope = targeted(where, Path(cwd), located, common)
        if scope is None:
            continue  # Another repository's merge. Its worktrees, its marks.
        merged_root, merged_git_dir, merged_linked = scope
        if not merged_linked:
            # A merge run from the main checkout spends no worktree: there is no branch
            # here whose next edit would reach nobody.
            continue
        mark_spent(
            common,
            merged_root,
            branch_of(merged_git_dir),
            f"{what} was run from this worktree",
        )

    for subcommand, _args, where in git_calls(command):
        scope = targeted(where, Path(cwd), located, common)
        if scope is None:
            # Another repository, or none. Its branches, its integration branch, its rules
            # — and the remedy this hook would print is not even possible there.
            continue
        _, _, target_linked = scope
        if subcommand == "stash" and not (_args and _args[0] in {"list", "show"}):
            deny(reason_stash(), warn_only)
            return
        if not target_linked and subcommand in MUTATORS:
            deny(reason_main_checkout(f"`git {subcommand}` does not run", branch, plan, worktrees),
                 warn_only)
            return


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001 — fail open, always.
        pass
    sys.exit(0)
