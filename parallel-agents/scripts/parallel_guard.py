#!/usr/bin/env python3
"""Stop two Claude sessions in one checkout from silently corrupting each other's work.

Claude Code already enforces isolation for a session that is *inside* a worktree:
file edits, command working directories and git redirects that reach back into the
main checkout are all blocked. What nothing covers is the step before that — two
sessions sharing the main checkout, neither of them isolated yet. That is where the
failures are silent rather than loud, and a conflict you resolve is far cheaper than
a branch that goes red for something it never did.

Three failures, and only three, are worth a denial:

  * A **floor-mover** (`checkout`, `switch`, `reset`, `rebase`, `merge`, ...) rewrites
    the files another session is mid-edit on. Neither session sees an error.
  * **Blind staging** (`git add -A`, `git commit -a`) sweeps another session's
    half-finished files into your commit. Neither session sees an error.
  * **Two sessions writing one file.** The second write discards the first. There is
    no conflict marker, because git never sees two versions.

Plus one that survives worktrees, which is why it is checked repo-wide rather than
per-tree: `refs/stash` is a single stack for the whole repository, so a `git stash`
in one worktree renumbers another's entries.

Everything else proceeds. That restraint is deliberate: Claude Code's own agent-teams
guidance has teammates share one working directory and partition files between them,
so a guard that denied every write to a shared checkout would deny a supported
workflow. Denying only the silent-wrongness leaves the supported ones working.

A session enters the registry the first time it actually writes, so an idle session
and a read-only agent never claim anything and a lone writer is never blocked.

It fails **open** on every question it cannot answer — no repo, no git metadata, an
unparseable payload, an unreadable registry. Blocking the only writer in a tree over
state the guard merely failed to read is the worse error, and it is the error that
makes someone delete the hook.

  CLAUDE_PARALLEL_GUARD=off       turns it off
  CLAUDE_PARALLEL_GUARD=strict    denies every write to a checkout another session holds
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
import time
from pathlib import Path

# A session counts as live while either its last write or its transcript is recent.
# The transcript is the better signal of the two: it moves on every turn, so a session
# that has spent twenty minutes reading still looks alive. Twenty minutes is longer
# than any gap inside a working session and short enough that a session killed rather
# than closed frees the tree without anyone tidying up.
LIVE_TTL = 20 * 60

# How many recently-written paths a claim remembers. Enough to cover a session's
# working set; bounded so a long session's claim file cannot grow without limit.
MAX_TRACKED_PATHS = 200

REGISTRY_DIRNAME = "claude-parallel-sessions"

FILE_TOOLS = {"Edit", "Write", "NotebookEdit"}
SHELL_TOOLS = {"Bash", "PowerShell"}

# Subcommands that rewrite the working tree or move HEAD under whoever else is in it.
# `worktree` is absent because taking one is the remedy this guard recommends, and
# `branch`/`tag` are absent because moving a ref touches nobody's files.
FLOOR_MOVERS = {
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
}

# `git add` flags that stage the whole tree rather than the paths you name.
BLIND_ADD_FLAGS = {"-A", "--all", "-u", "--update", "--no-ignore-removal"}
BLIND_ADD_PATHSPECS = {".", ":/", "*", "./"}

_SEGMENT = re.compile(r"&&|\|\||[;\n|]")

# A repo that ships its own concurrent-writer guard gets left alone: two guards means
# two denials with two different remedies for one action, which is the flail this
# exists to prevent.
_OTHER_GUARD = re.compile(r"concurrent[-_]?writer|writer[-_]?guard|parallel[-_]?guard", re.I)


# --------------------------------------------------------------------------- paths


def key(path) -> str:
    """A comparable spelling of a path. Case-insensitive where the filesystem is."""
    return os.path.normcase(os.path.normpath(os.path.abspath(str(path))))


def find_tree(start: Path):
    """The working tree containing `start`, and its git directory, or None.

    Walks the filesystem rather than shelling out to git: a subprocess per write-tool
    call is the one cost this hook cannot amortise, and `.git` tells us everything.
    """
    try:
        candidates = [start, *start.parents]
    except (OSError, ValueError):
        return None
    for directory in candidates:
        marker = directory / ".git"
        try:
            if marker.is_dir():
                return directory, marker
            if marker.is_file():
                text = marker.read_text(encoding="utf-8", errors="replace").strip()
                if not text.startswith("gitdir:"):
                    return None
                git_dir = Path(text.split(":", 1)[1].strip())
                if not git_dir.is_absolute():
                    git_dir = directory / git_dir
                return directory, Path(os.path.normpath(str(git_dir)))
        except OSError:
            return None
    return None


def common_git_dir(git_dir: Path) -> Path:
    """The git directory every worktree of this repository shares.

    A linked worktree's git dir holds a `commondir` file pointing back at the main
    one. The registry lives there so all the worktrees read the same claims — the
    whole point, since `.claude/` is checked out separately in each of them.
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


def tree_for(path: Path, session_tree: Path, main_root: Path | None) -> str | None:
    """Which working tree a target path belongs to, as a comparable key.

    Cheap on purpose: a target is nearly always inside the session's own tree, and
    the only other case worth resolving is a path in the main checkout while the
    session sits in a worktree under `.claude/worktrees/`.
    """
    target = key(path)
    if target == key(session_tree) or target.startswith(key(session_tree) + os.sep):
        return key(session_tree)
    if main_root is None:
        return None
    main = key(main_root)
    if target != main and not target.startswith(main + os.sep):
        return None
    nested = os.path.join(main, key(".claude/worktrees"))
    if target.startswith(nested + os.sep):
        remainder = target[len(nested) + 1 :].split(os.sep)
        return os.path.join(nested, remainder[0]) if remainder else main
    return main


# ------------------------------------------------------------------- command parse


def git_calls(command: str):
    """Every mutating git call in a shell command, as (subcommand, flags, -C target).

    The `-C` read is textual and never expands a variable, so `git -C "$W" commit` is
    judged against the session's own directory instead. That is the guard being
    conservative rather than clever: the cost is spelling one path out in full, and
    the alternative is trusting a shell expansion it cannot evaluate.
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
        directory = None
        subcommand = None
        index = 0
        while index < len(rest):
            token = rest[index]
            if token == "-C" and index + 1 < len(rest):
                directory = rest[index + 1]
                index += 2
                continue
            if token.startswith("-"):
                index += 1
                continue
            subcommand = token
            break
        if subcommand is None:
            continue
        calls.append((subcommand, rest[index + 1 :], directory))
    return calls


def classify(subcommand: str, args: list[str]) -> str | None:
    """What kind of damage this git call can do to a session sharing the tree."""
    if subcommand == "stash":
        # `list`/`show` only read the stack.
        if args and args[0] in {"list", "show"}:
            return None
        return "stash"
    if subcommand in FLOOR_MOVERS:
        return "floor"
    if subcommand == "add":
        if any(a in BLIND_ADD_FLAGS or a in BLIND_ADD_PATHSPECS for a in args):
            return "blind_stage"
        return None
    if subcommand == "commit":
        if "--amend" in args:
            # The commit you are rewriting may be the other session's — this is one of
            # the three failures reported from running two `/implement` runs in one
            # checkout, and it leaves no trace that anything was overwritten.
            return "amend"
        for arg in args:
            if arg in {"-a", "--all"}:
                return "blind_stage"
            if arg.startswith("-") and not arg.startswith("--") and "a" in arg[1:]:
                return "blind_stage"  # -am, -av, ...
        return None
    return None


# ------------------------------------------------------------------------ registry


def read_claims(registry: Path, me: str, now: float):
    """Every other session's live claim on this repository."""
    live = []
    try:
        entries = list(registry.iterdir())
    except OSError:
        return live
    for entry in entries:
        if entry.suffix != ".json" or entry.stem == me:
            continue
        try:
            claim = json.loads(entry.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(claim, dict):
            continue
        seen = claim.get("last_write_at", 0)
        transcript = claim.get("transcript_path")
        if isinstance(transcript, str) and transcript:
            try:
                seen = max(seen, os.path.getmtime(transcript))
            except OSError:
                pass
        if not isinstance(seen, (int, float)) or now - seen > LIVE_TTL:
            continue
        claim["_seen"] = seen
        claim["_file"] = entry
        live.append(claim)
    return live


def record(registry: Path, me: str, payload: dict, tree: str, paths: list[str], now: float) -> None:
    """Refresh this session's claim. One file per session, so there is no lost update."""
    path = registry / f"{me}.json"
    claim = {}
    try:
        claim = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(claim, dict):
            claim = {}
    except (OSError, ValueError):
        claim = {}

    written = claim.get("paths")
    if not isinstance(written, dict):
        written = {}
    for item in paths:
        written[item] = now
    written = {k: v for k, v in written.items() if isinstance(v, (int, float)) and now - v <= LIVE_TTL}
    if len(written) > MAX_TRACKED_PATHS:
        keep = sorted(written.items(), key=lambda kv: kv[1], reverse=True)[:MAX_TRACKED_PATHS]
        written = dict(keep)

    claim.update(
        {
            "session_id": me,
            "tree_root": tree,
            "cwd": payload.get("cwd"),
            "transcript_path": payload.get("transcript_path"),
            "last_write_at": now,
            "paths": written,
        }
    )
    claim.setdefault("since", now)
    # A claim that had gone stale starts again from now, so a session returning from a
    # long idle cannot reclaim seniority over whoever took the tree while it was away.
    if now - claim.get("since", now) > LIVE_TTL and now - claim.get("last_write_at", now) > LIVE_TTL:
        claim["since"] = now

    try:
        registry.mkdir(parents=True, exist_ok=True)
        temporary = registry / f"{me}.json.tmp"
        temporary.write_text(json.dumps(claim), encoding="utf-8")
        os.replace(temporary, path)
    except OSError:
        pass


# ------------------------------------------------------------------------- reports


def short(session_id) -> str:
    return str(session_id)[:8] if session_id else "another session"


def ago(seconds: float) -> str:
    minutes = int(max(0.0, seconds) // 60)
    return "just now" if minutes < 1 else f"{minutes}m ago"


ESCAPE = (
    "If that session is actually gone — check with `list_sessions` — set "
    "CLAUDE_PARALLEL_GUARD=off for this session. `/parallel-agents` has the full protocol."
)

WORKTREE_REMEDY = (
    "Take your own checkout instead: call **EnterWorktree**. It creates a worktree "
    "under `.claude/worktrees/`, moves this session into it, and from then on Claude "
    "Code blocks anything that reaches back here. If your branch needs a base other "
    "than the repository's default, create it first with "
    "`git worktree add .claude/worktrees/<name> -b <branch> <base>` and then "
    "EnterWorktree that path — `worktree.baseRef` only chooses between the default "
    "branch and local HEAD, never a named branch."
)


def reason_floor(subcommand: str, other: dict, now: float) -> str:
    return (
        f"Denied: `git {subcommand}` rewrites the working tree and moves HEAD, and Claude "
        f"session {short(other.get('session_id'))} (active {ago(now - other['_seen'])}) is "
        "writing in this same checkout. It would lose the files it is mid-edit on, and "
        "neither of you would see an error — that silence is why this is denied rather "
        "than warned about.\n\n" + WORKTREE_REMEDY + "\n\n" + ESCAPE
    )


def reason_blind(subcommand: str, other: dict, now: float) -> str:
    return (
        f"Denied: `git {subcommand}` here stages every change in the checkout, and Claude "
        f"session {short(other.get('session_id'))} (active {ago(now - other['_seen'])}) has "
        "uncommitted work in it. Your commit would carry its half-finished files into "
        "history under your message.\n\n"
        "Stage what you changed by name: `git status --short` to see the tree, then "
        "`git add <path> ...` and commit. That is the whole fix — you do not need a "
        "worktree for it.\n\n" + ESCAPE
    )


def reason_amend(other: dict, now: float) -> str:
    return (
        "Denied: `git commit --amend` rewrites the last commit on this branch, and Claude "
        f"session {short(other.get('session_id'))} (active {ago(now - other['_seen'])}) is "
        "committing in this same checkout — so the commit you would rewrite may be its, "
        "and nothing would say so.\n\n"
        "Make a new commit instead: `git add <paths> && git commit -m \"...\"`. Squash "
        "later, on your own branch, once you are the only writer here — or take one now "
        "with EnterWorktree and amend freely in it.\n\n" + ESCAPE
    )


def reason_stash(other: dict, now: float) -> str:
    return (
        "Denied: `refs/stash` is one stack for the whole repository, shared by every "
        f"worktree, and Claude session {short(other.get('session_id'))} (active "
        f"{ago(now - other['_seen'])}) is live in it. Your push renumbers its entries, so "
        "a later `stash pop` or `stash drop` in either session takes the wrong one. This "
        "is the one place a worktree looks like isolation and is not.\n\n"
        "Commit instead — a commit belongs to your branch and no stranger can pop it: "
        "`git add <paths> && git commit -m \"wip\"`.\n\n" + ESCAPE
    )


def reason_collision(target: str, other: dict, now: float, when: float) -> str:
    recent = sorted(
        (p for p, t in (other.get("paths") or {}).items() if isinstance(t, (int, float))),
        key=lambda p: other["paths"][p],
        reverse=True,
    )[:5]
    listing = "\n".join(f"  {os.path.basename(p)}  ({p})" for p in recent)
    return (
        f"Denied: Claude session {short(other.get('session_id'))} wrote this file "
        f"{ago(now - when)} and is still live in this checkout. Two sessions editing one "
        "file means the later write silently discards the earlier one — git never sees "
        "two versions, so there is no conflict marker and no error.\n\n"
        f"File: {target}\n\nThat session has recently written:\n{listing}\n\n"
        "Pick one: work on files it is not touching (partitioning by file is how "
        "Claude Code's own agent teams share a directory), ask it what it is doing with "
        "`list_sessions` and a message, or take your own checkout with EnterWorktree.\n\n"
        + ESCAPE
    )


def reason_strict(other: dict, now: float) -> str:
    return (
        f"Denied: Claude session {short(other.get('session_id'))} (active "
        f"{ago(now - other['_seen'])}) is already writing in this checkout, and this repo "
        "runs the guard in strict mode — a second writer isolates before it writes "
        "anything, rather than after it collides.\n\n" + WORKTREE_REMEDY + "\n\n" + ESCAPE
    )


def deny(reason: str) -> None:
    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        },
        sys.stdout,
    )


def session_start_notice(others: list, now: float) -> None:
    lines = []
    for other in sorted(others, key=lambda c: -c["_seen"])[:3]:
        paths = other.get("paths") or {}
        recent = sorted(paths, key=lambda p: paths[p], reverse=True)[:5]
        files = ", ".join(os.path.basename(p) for p in recent) or "no files yet"
        lines.append(
            f"  - session {short(other.get('session_id'))}, active {ago(now - other['_seen'])}, "
            f"has written: {files}"
        )
    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": (
                    "Another Claude session is already writing in this checkout:\n"
                    + "\n".join(lines)
                    + "\nIf you are going to write here too, stay off those files, or call "
                    "EnterWorktree to get your own checkout. Branch-switching, `git add -A` "
                    "and `git stash` are blocked here while it is live. `/parallel-agents` "
                    "explains why."
                ),
            }
        },
        sys.stdout,
    )


# ---------------------------------------------------------------------------- main


def repo_ships_its_own_guard(main_root: Path) -> bool:
    """True when this repo already enforces the rule with a hook of its own."""
    for name in ("settings.json", "settings.local.json"):
        path = main_root / ".claude" / name
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        try:
            settings = json.loads(text)
        except ValueError:
            continue
        hooks = (settings.get("hooks") or {}).get("PreToolUse") or []
        for matcher in hooks:
            for hook in (matcher or {}).get("hooks") or []:
                command = " ".join(
                    str(part) for part in (hook.get("command"), *(hook.get("args") or [])) if part
                )
                if "parallel_guard" in command or "parallel-guard" in command:
                    continue  # our own installation, in --repo mode
                if _OTHER_GUARD.search(command):
                    return True
    return False


def targets(payload: dict, cwd: Path):
    """What this tool call would do, as (kind, path) pairs. kind: file | floor | ..."""
    tool = payload.get("tool_name", "")
    tool_input = payload.get("tool_input") or {}
    if tool in FILE_TOOLS:
        path = tool_input.get("file_path")
        if isinstance(path, str) and path:
            candidate = Path(path)
            return [("file", candidate if candidate.is_absolute() else cwd / candidate)]
        return []
    if tool in SHELL_TOOLS:
        command = tool_input.get("command")
        if not isinstance(command, str):
            return []
        found = []
        for subcommand, args, directory in git_calls(command):
            kind = classify(subcommand, args)
            if kind is None:
                continue
            where = Path(directory) if directory else cwd
            if not where.is_absolute():
                where = cwd / where
            found.append((f"{kind}:{subcommand}", where))
        return found
    return []


def main() -> None:
    mode = (os.environ.get("CLAUDE_PARALLEL_GUARD") or "balanced").strip().lower()
    if mode == "off":
        return

    payload = json.load(sys.stdin)
    event = payload.get("hook_event_name")
    session = payload.get("session_id")
    cwd = payload.get("cwd")
    if not session or not cwd:
        return

    located = find_tree(Path(cwd))
    if located is None:
        return
    session_tree, git_dir = located
    common = common_git_dir(git_dir)
    main_root = common.parent if common.name == ".git" else None

    if main_root is not None and repo_ships_its_own_guard(main_root):
        return

    registry = common / REGISTRY_DIRNAME
    now = time.time()

    if event == "SessionEnd":
        try:
            (registry / f"{session}.json").unlink()
        except OSError:
            pass
        return

    if event == "SessionStart":
        others = read_claims(registry, session, now)
        if others:
            session_start_notice(others, now)
        return

    if event != "PreToolUse":
        return

    wanted = targets(payload, Path(cwd))
    if not wanted:
        return

    # The uncontended path stops here: one directory listing, then a claim refresh.
    others = read_claims(registry, session, now)
    if not others:
        record(registry, session, payload, key(session_tree), [key(p) for k, p in wanted if k == "file"], now)
        return

    by_tree = {}
    for other in others:
        by_tree.setdefault(other.get("tree_root"), []).append(other)

    for kind, path in wanted:
        tree = tree_for(path, session_tree, main_root)
        sharing = by_tree.get(tree, []) if tree else []

        if kind.startswith("stash"):
            # Repo-wide: the stash stack ignores worktree boundaries.
            deny(reason_stash(others[0], now))
            return

        if mode == "strict" and sharing:
            deny(reason_strict(sharing[0], now))
            return

        if kind.startswith("floor:") and sharing:
            deny(reason_floor(kind.split(":", 1)[1], sharing[0], now))
            return

        if kind.startswith("blind_stage:") and sharing:
            deny(reason_blind(kind.split(":", 1)[1], sharing[0], now))
            return

        if kind.startswith("amend:") and sharing:
            deny(reason_amend(sharing[0], now))
            return

        if kind == "file":
            target = key(path)
            for other in sharing:
                when = (other.get("paths") or {}).get(target)
                if isinstance(when, (int, float)) and now - when <= LIVE_TTL:
                    deny(reason_collision(str(path), other, now, when))
                    return

    record(registry, session, payload, key(session_tree), [key(p) for k, p in wanted if k == "file"], now)


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001 — fail open, always.
        pass
    sys.exit(0)
