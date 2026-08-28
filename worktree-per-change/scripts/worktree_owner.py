#!/usr/bin/env python3
"""One worktree, one session. A second session is told whose it is, and where to go.

WHY THIS EXISTS

`worktree_guard.py` isolates *changes* from each other and says nothing about
*sessions*. Two concurrent sessions in one worktree pass every check it makes — the tree
is a linked worktree, it is not on the integration branch, its PR has not merged — so
nothing stops them, and on 2026-08-25 two of them drove one frontend worktree for half an
hour. Every consequence was silent:

  * both dev servers wrote the same build output directory and both died mid-run;
  * one session's `pnpm dev` took the port from the server already there;
  * one session's screenshot run captured the other's uncommitted edit, so the "after"
    image it delivered was of a change it did not author;
  * the app's auth cookies are host-scoped, not port-scoped, so switching role on one
    server switched it on the other.

None of that raises an error. It produces a screenshot that is wrong, and the ordinary
reading of a wrong screenshot is that the code is wrong. That is the most expensive kind
of quiet, and it is why this is a hook rather than a paragraph in `CLAUDE.md`.

The skill's own headline claim — "two writers never share a directory" — is true of the
main checkout and false of a worktree, which is the gap this closes.

WHAT IT DOES

The first write into a linked worktree claims it for that session. A *different*
session's write into a claimed tree is denied, with the command that gives it a tree of
its own. A claim lapses when its session goes quiet, because the alternative is a crashed
session locking a tree until somebody reads this file. Liveness is the owner's transcript
mtime, which every turn touches — so a session that is actually working is never more
than a turn away from fresh, and one that died is stale within the hour.

A pid was rejected as the liveness signal: hooks are handed a session id and a transcript
path, not the agent's pid, and a pid recorded from the hook's own process dies at the end
of every hook call.

WHAT IT DELIBERATELY DOES NOT DO

It is a **second hook, not an edit to the guard.** The guard answers "is this tree a
worktree, on the right branch, not already merged"; this answers "is it *yours*". They
compose, they own separate state, and a repo can run either alone. Repos that vendor the
guard by digest — the skill tells them to — would report drift on an edit and cannot
report anything about a file they did not take.

It does not stop a session **reading** another tree. Comparing two branches on disk is
ordinary work. Only the file tools and the commands that build, serve or write are
refused, and `git` is left to the guard, whose rules are per-tree rather than
per-session.

It does not arbitrate ports or dev servers. It records who holds a tree; what a repo's
own tooling does with that is the repo's business, and the claim files are plain JSON so
it can read them.

  CLAUDE_WORKTREE_OWNER=off          turns it off
  CLAUDE_WORKTREE_OWNER=warn         reports instead of denying
  CLAUDE_WORKTREE_OWNER_TTL=<secs>   how long a quiet session keeps its claim (default 2700)

Read from the hook's environment, so they are the operator's switches and not a
session's — a `CLAUDE_WORKTREE_OWNER=off <cmd>` prefix sets it for that one command, by
which point this hook has already run.

Fails open on everything: no state directory, an unreadable claim, an unparseable
payload, a command it cannot lex. Blocking the only writer in a tree over state the hook
merely failed to read is the worse error.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
import time
from pathlib import Path

SELF = Path(__file__).resolve()

# The guard's state directory, shared on purpose. Everything either hook records belongs
# to the repository rather than to a checkout of it, it must be identical from every
# worktree, and it must never appear in `git status` — which is exactly what the common
# git dir is. One directory also means one thing to delete.
STATE_DIRNAME = "claude-worktree-gate"
CONFIG_FILENAME = "worktree-per-change.json"
DEFAULT_WORKTREES_ROOT = ".claude/worktrees"
DEFAULT_BRANCH = "development"

# How long a claim outlives its session's last turn. 45 minutes: a session mid-work
# touches its transcript every turn, so this is never close for anyone actually working,
# while a session killed between two turns frees its tree inside a coffee break.
DEFAULT_TTL = 2700

# Refresh no more often than this. Every write in a worktree passes through here and the
# claim only has to be fresh to the minute.
REFRESH_AFTER = 60

FILE_TOOLS = {"Edit", "Write", "NotebookEdit"}
SHELL_TOOLS = {"Bash", "PowerShell"}

# Two tiers, because "which directory does this command act on" has two answers.
#
# A package runner acts on the package in its CURRENT directory — `pnpm dev` in a
# worktree is that worktree's dev server, named nowhere in the command. So for these the
# cwd itself is a target, and that is the shape nobody caught on 2026-08-25.
PACKAGE_RUNNERS = {
    "pnpm", "pnpx", "npm", "npx", "yarn", "bun", "deno", "corepack",
    "turbo", "next", "vite", "vitest", "jest", "tsc", "tsx", "playwright", "storybook",
    "cargo", "go", "mvn", "gradle", "gradlew", "dotnet", "poetry", "uv", "pytest", "tox",
    "make", "just", "rake", "bundle", "composer", "docker", "docker-compose",
}

# An interpreter acts on the file it is handed, and a session legitimately runs a script
# from its own scratch directory while sitting anywhere. So for these only PATH TOKENS
# count — `node <tree>/shoot.mjs` targets the tree, `node ~/scratch/shoot.mjs` targets
# nothing. The writers are here for the same reason: the path is the target, the cwd is
# not.
PATH_RUNNERS = {
    "node", "bash", "sh", "zsh", "fish", "python", "python3", "ruby", "perl", "pwsh",
    "rm", "mv", "cp", "touch", "mkdir", "tee", "truncate", "chmod",
}

# Deliberately in neither: `git` (the guard's business, and its own rules are per-tree
# not per-session), and every reader — `cat`, `grep`, `rg`, `ls`, `head`, `diff`, `find`.
# Reading another session's tree is ordinary work, and a hook that refused it would be
# turned off within the day, after which nothing is enforced at all.

# `-C <dir>` / `--dir <dir>` / `--prefix <dir>` / `--cwd <dir>`: a runner pointed
# somewhere else. `cd` is handled separately because it sets the directory for what
# follows rather than for itself.
DIR_FLAGS = {"-C", "--dir", "--prefix", "--cwd", "--project-dir", "--directory"}

_SEGMENT = re.compile(r"&&|\|\||[;\n|]")


# ------------------------------------------------------------------------ the trees


def find_tree(start: Path):
    """`(tree_root, git_dir, linked)` for the working tree containing `start`, or None.

    Lifted from the guard, and for the guard's reason: a linked worktree has `.git` as a
    *file* holding a `gitdir:` pointer and the main checkout has it as a directory, so
    "is this an isolated tree" is a stat rather than a subprocess or path arithmetic
    against a configured worktrees root. A repo that keeps its trees somewhere this hook
    was never told about is still handled correctly, which is the whole reason not to do
    it by layout.

    Duplicated rather than imported. Each hook is copied into a repo on its own and has
    to run when the other is absent, and `worktree-guard.py` is not an importable module
    name anyway. Twenty lines is the cheaper half of that trade.
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
    """The git directory every worktree of this repository shares."""
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


def owned_tree(path: Path):
    """`(tree_root, common_git_dir)` when `path` is inside a LINKED worktree, else None.

    The main checkout is excluded deliberately and not by oversight: it is shared by
    every session by design, the guard already refuses writes there, and a claim on it
    would deny the one place root tooling is supposed to run.
    """
    found = find_tree(path)
    if found is None:
        return None
    tree_root, git_dir, linked = found
    if not linked:
        return None
    return tree_root, common_git_dir(git_dir)


def claims_dir(common: Path) -> Path:
    return common / STATE_DIRNAME / "claims"


def claim_file(common: Path, tree_root: Path) -> Path:
    stem = re.sub(r"[^A-Za-z0-9._-]", "_", Path(tree_root).name) or "tree"
    return claims_dir(common) / f"{stem}.json"


def read_claim(path: Path) -> dict | None:
    try:
        blob = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return blob if isinstance(blob, dict) else None


def last_turn(claim: dict) -> float:
    """When the owning session was last known to be alive.

    The transcript's mtime, because the session itself touches it every turn — a
    heartbeat this hook does not have to maintain and cannot get wrong. `last_seen` is
    the fallback for a claim whose transcript has been rotated away or was never
    recorded, and it only moves when the owner writes something.
    """
    transcript = claim.get("transcript")
    if isinstance(transcript, str) and transcript:
        try:
            return Path(transcript).stat().st_mtime
        except OSError:
            pass
    seen = claim.get("last_seen")
    return float(seen) if isinstance(seen, (int, float)) else 0.0


def ttl() -> int:
    try:
        return max(60, int(os.environ.get("CLAUDE_WORKTREE_OWNER_TTL") or DEFAULT_TTL))
    except ValueError:
        return DEFAULT_TTL


def stale(claim: dict, now: float) -> bool:
    return (now - last_turn(claim)) > ttl()


def hold(path: Path, tree: Path, session: str, transcript: str | None) -> None:
    """Write or refresh the claim.

    Best-effort throughout: a claim that cannot be written is not a reason to stop the
    session that was going to do the work anyway. The failure mode of an unwritten claim
    is the behaviour of a repo without this hook, which is where everyone was last week.
    """
    existing = read_claim(path)
    now = time.time()
    if (
        existing
        and existing.get("session") == session
        and now - float(existing.get("last_seen") or 0) < REFRESH_AFTER
    ):
        return
    claimed_at = now
    if existing and existing.get("session") == session:
        claimed_at = float(existing.get("claimed_at") or now)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "tree": str(tree),
                    "session": session,
                    "transcript": transcript or "",
                    "claimed_at": claimed_at,
                    "last_seen": now,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    except OSError:
        pass


# --------------------------------------------------------------- the repo's own answers


def main_root(common: Path) -> Path | None:
    return common.parent if common.name == ".git" else None


def config(common: Path) -> dict:
    root = main_root(common)
    if root is None:
        return {}
    try:
        blob = json.loads((root / ".claude" / CONFIG_FILENAME).read_text(encoding="utf-8"))
        return blob if isinstance(blob, dict) else {}
    except (OSError, ValueError):
        return {}


def setting(common: Path, key: str, env: str, fallback: str) -> str:
    """A remedy value, from the environment, then the repo's record, then the default.

    Text only, and the same three-step lookup the guard makes for the same two keys — a
    remedy naming a branch the repo does not have, or a path it has ruled out, costs a
    turn and teaches the reader to stop reading these messages.
    """
    override = (os.environ.get(env) or "").strip()
    if override:
        return override
    value = config(common).get(key)
    if isinstance(value, str) and value.strip():
        return value.strip().rstrip("/\\")
    return fallback


# --------------------------------------------------------------- what a call targets


def absolute(token: str, base: Path) -> Path:
    candidate = Path(token)
    return candidate if candidate.is_absolute() else base / token


def unquote(token: str) -> str:
    """A token with its shell quoting removed and nothing else touched."""
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


def lex(raw: str) -> list | None:
    r"""One segment's tokens, with backslashes left alone. None when it cannot be read.

    Deliberately **not** `shlex.split(raw)`, whose POSIX mode consumes a backslash as an
    escape: `cd C:\Users\chris\trees\alpha` comes back as `C:Userschristreesalpha`,
    which exists nowhere, so the token names no directory and the call is allowed — on the
    one platform where `\` is the separator, and for every command whose target is spelled
    the platform's own way. Measured on Windows: four of this hook's denials and its
    read-in-place hint all silently became allows. `worktree_guard.py` avoids the same
    trap in `unquote`, for the same reason.
    """
    try:
        return [unquote(token) for token in shlex.split(raw, comments=True, posix=False)]
    except ValueError:
        return None


def is_release_call(tokens: list, cwd: Path) -> bool:
    """Is this segment this script, being run with `--release`?

    `--release <tree>` names a worktree in order to drop its claim, and everything below
    reads a named worktree as somewhere a command is about to write — so without this the
    hook denies the exact command its own denial prints, and the escape hatch is reachable
    only by a session that already knows to spell it another way. Confirmed 2026-08-26.

    Matched by filename rather than by identity, which is looser than it first looks like
    it should be and is the correct looseness: under a committed install every worktree
    holds its own copy of this file at its own path, so `resolve() == SELF` would exempt
    the copy belonging to the session being denied and refuse the one it was told to run.

    Still narrow. It requires `--release` *and* a token that resolves to a real file with
    this script's name; a command that merely mentions the word, or names some other
    script, is exempted from nothing, and a release riding alongside a real write is two
    segments, of which only the first is exempt.
    """
    if "--release" not in tokens:
        return False
    for token in tokens:
        if token.startswith("-"):
            continue
        try:
            candidate = absolute(token, cwd).resolve()
            if candidate.name == SELF.name and candidate.is_file():
                return True
        except OSError:
            continue
    return False


def dirs_from_command(command: str, cwd: Path) -> list:
    """`(directory, why)` for each directory a command would build, serve or write in.

    Three shapes, and the middle one is what caught nobody on 2026-08-25:

      * `pnpm dev` in a worktree session — the tree is named nowhere at all;
      * `cd <tree> && pnpm dev` from a session sitting somewhere else;
      * `node <tree>/scripts/shoot.mjs` from a session that never left its own tree.

    A token counts as a path only if it exists on disk. That is what keeps the payload of
    `bash -lc '...'` — one token holding a whole command line — from being read as a
    directory, and it means a command this cannot lex contributes nothing rather than
    something wrong.
    """
    found = []
    for raw in _SEGMENT.split(command):
        tokens = lex(raw)
        if not tokens:
            continue
        verb = Path(tokens[0]).name
        if is_release_call(tokens, cwd):
            continue
        if verb in {"cd", "pushd"}:
            # A `cd` into a foreign tree is the gesture that says work is about to happen
            # there, and it is worth refusing on its own: the Bash tool's directory
            # persists between calls, so everything after it inherits the tree silently.
            for token in tokens[1:]:
                if token.startswith("-"):
                    continue
                candidate = absolute(token, cwd)
                try:
                    if candidate.is_dir():
                        found.append((candidate, "cd"))
                except OSError:
                    continue
            continue
        here = cwd
        rest = tokens[1:]
        for index, token in enumerate(rest):
            if token in DIR_FLAGS and index + 1 < len(rest):
                here = absolute(rest[index + 1], cwd)
        if verb in PACKAGE_RUNNERS:
            found.append((here, verb))
        if verb in PACKAGE_RUNNERS or verb in PATH_RUNNERS:
            for token in rest:
                if token.startswith("-"):
                    continue
                candidate = absolute(token, here)
                try:
                    if candidate.exists():
                        found.append((candidate if candidate.is_dir() else candidate.parent, verb))
                except OSError:
                    continue
    return found


def targets(payload: dict, cwd: Path) -> list:
    """`(path, why)` for everything this call would land in.

    Judged by what the call NAMES, not by where the session sits — the two differ
    constantly, and a file tool carrying an absolute path into another tree is the exact
    shape of the 2026-08-25 collision.
    """
    tool = payload.get("tool_name", "")
    tool_input = payload.get("tool_input") or {}
    if tool in FILE_TOOLS:
        path = tool_input.get("file_path") or tool_input.get("notebook_path")
        if not isinstance(path, str) or not path:
            return []
        return [(absolute(path, cwd), tool.lower())]
    if tool in SHELL_TOOLS:
        command = tool_input.get("command")
        if not isinstance(command, str):
            return []
        return dirs_from_command(command, cwd)
    return []


# ------------------------------------------------------------------------- reporting


def short(session: str) -> str:
    return session.split("-")[0] if session else "unknown"


def ago(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes < 1:
        return "under a minute ago"
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    hours = minutes // 60
    return f"{hours} hour{'s' if hours != 1 else ''} ago"


def cut_your_own(common: Path) -> str:
    branch = setting(common, "integrationBranch", "CLAUDE_INTEGRATION_BRANCH", DEFAULT_BRANCH)
    root = setting(common, "worktreesRoot", "CLAUDE_WORKTREES_ROOT", DEFAULT_WORKTREES_ROOT)
    return (
        f"  git fetch origin {branch}\n"
        f"  git worktree add {root}/<name> -b <branch> origin/{branch}"
    )


def reason_foreign(tree: Path, common: Path, claim: dict, now: float, why: str) -> str:
    return (
        f"Denied: `{tree}` is another session's worktree. Session "
        f"`{short(claim.get('session', ''))}` claimed it "
        f"{ago(now - float(claim.get('claimed_at') or now))} and was last active "
        f"{ago(now - last_turn(claim))}.\n\n"
        "One worktree, one session. Two sessions in one tree fails *quietly*: on "
        "2026-08-25 two of them shared one frontend tree, both dev servers wrote the same "
        "build directory and died, one took the other's port, and one session's "
        "screenshots recorded the other's uncommitted edit as its own \"after\".\n\n"
        "Cut your own — that is the answer here, and it costs a worktree:\n"
        + cut_your_own(common)
        + "\n\nIf that session is finished and only its tree is left behind, release it "
        "deliberately:\n"
        # An absolute tree path, because the session being denied is usually SITTING in
        # the tree it does not own, and a path relative to somewhere else does not run
        # from there.
        f"  python3 {SELF} --release {tree}\n"
        f"A claim also lapses on its own once its session has been quiet for "
        f"{ttl() // 60} minutes. **Do not** work around this by writing through a path "
        "the hook cannot see: the point is not the file, it is the other session's "
        "`git status`.\n\n"
        + (
            "Only *reading* that tree? Read it in place — absolute paths, no `cd`. This "
            "refused the `cd`, not the read, because the shell tool keeps its directory "
            "between calls and everything after it would have inherited the tree.\n\n"
            if why == "cd"
            else ""
        )
        + "See the worktree-per-change skill, 'A worktree belongs to one session'."
    )


def report(cwd: Path, session: str, transcript: str | None) -> str | None:
    """What this repository's worktrees are, and who holds them. Injected at SessionStart.

    The incident's own explanation was "any session that needs a runnable app reaches for
    the worktree that already has one, and they all reach for the same one". That is a
    knowledge problem before it is an enforcement problem, so the state is stated up front
    rather than discovered through a denial.

    The list comes from `<common>/worktrees/*/gitdir`, which is git's own register of
    linked worktrees — no subprocess, and nothing to keep in step with a configured
    layout.
    """
    found = find_tree(cwd)
    if found is None:
        return None
    common = common_git_dir(found[1])
    now = time.time()
    here = found[0] if found[2] else None
    lines: list[str] = []
    register = common / "worktrees"
    try:
        entries = sorted(p for p in register.iterdir() if p.is_dir())
    except OSError:
        return None
    for entry in entries:
        try:
            pointer = Path((entry / "gitdir").read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            continue
        tree = pointer.parent
        if not tree.is_dir():
            continue  # A registered tree that has been deleted but not pruned.
        claim = read_claim(claim_file(common, tree))
        if claim is None:
            lines.append(f"- `{tree}` — unclaimed")
        elif claim.get("session") == session:
            lines.append(f"- `{tree}` — **yours**")
        elif stale(claim, now):
            lines.append(
                f"- `{tree}` — claim lapsed (session `{short(claim.get('session', ''))}` "
                f"last active {ago(now - last_turn(claim))}); yours if you take it"
            )
        else:
            lines.append(
                f"- `{tree}` — held by session `{short(claim.get('session', ''))}`, active "
                f"{ago(now - last_turn(claim))}"
            )
    if not lines:
        return None
    context = (
        "**A worktree belongs to one session.** Writing in a tree another live session "
        "holds is denied by a hook — not for tidiness: on 2026-08-25 two sessions shared "
        "one tree and one of them delivered screenshots of the other's uncommitted "
        "edit.\n\n"
        + "\n".join(lines)
        + "\n\nNeed a tree? Cut your own; do not borrow one that is already set up.\n"
        + cut_your_own(common)
    )
    if here is not None:
        path = claim_file(common, here)
        claim = read_claim(path)
        if claim and claim.get("session") not in (None, session) and not stale(claim, now):
            context += (
                f"\n\n**This session is sitting in `{here}`, which session "
                f"`{short(claim.get('session', ''))}` holds.** Reading is fine; the first "
                "write will be denied. Cut your own tree before you start."
            )
        else:
            hold(path, here, session, transcript)
    return context


# ------------------------------------------------------------------------------ i/o


def emit(payload: dict) -> None:
    json.dump(payload, sys.stdout)


def deny(reason: str, warn_only: bool) -> None:
    if warn_only:
        emit({"systemMessage": "worktree-owner (warn mode) would have denied this. " + reason})
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


def release(argument: str) -> int:
    """Drop a claim, by tree path or by the tree's leaf name.

    The deliberate override, and the one a teardown script runs when the tree comes down.
    Scoped to the repository it is run from, because that is where the claims live — a
    cross-repo change carrying the same branch name in two repos releases one of them per
    invocation rather than both by accident.
    """
    candidate = Path(argument)
    if not candidate.is_absolute():
        candidate = (Path.cwd() / argument).resolve()
    found = owned_tree(candidate)
    if found is not None:
        path = claim_file(found[1], found[0])
        if path.exists():
            path.unlink()
            print(f"released {found[0]}")
        else:
            print(f"{found[0]} was not claimed")
        return 0
    # Not a path into a live worktree: a leaf name, or a tree already removed. Resolve
    # the repository from the cwd and drop the claim filed under that name.
    here = find_tree(Path.cwd())
    if here is None:
        print(f"not inside a git repository, so there is no claim to drop for '{argument}'")
        return 0
    common = common_git_dir(here[1])
    path = claim_file(common, Path(argument))
    if path.exists():
        path.unlink()
        print(f"released {path}")
    else:
        print(f"no claim for '{argument}'")
    return 0


def main() -> None:
    argv = sys.argv[1:]
    if argv and argv[0] == "--release":
        if len(argv) < 2:
            print("usage: worktree_owner.py --release <worktree path or name>")
            sys.exit(2)
        sys.exit(release(argv[1]))

    mode = (os.environ.get("CLAUDE_WORKTREE_OWNER") or "on").strip().lower()
    if mode == "off":
        return
    warn_only = mode == "warn"

    payload = json.load(sys.stdin)
    event = payload.get("hook_event_name")
    session = payload.get("session_id") or "unknown"
    transcript = payload.get("transcript_path")
    cwd = payload.get("cwd")
    if not cwd:
        return
    here = Path(cwd)

    if event == "SessionStart":
        context = report(here, session, transcript)
        if context:
            emit(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "SessionStart",
                        "additionalContext": context,
                    }
                }
            )
        return

    if event != "PreToolUse":
        return

    now = time.time()

    # Sitting in a tree claims it, whatever the call was — a read counts. Otherwise a
    # session that spends its first twenty minutes reading its own worktree has not
    # claimed it, and the tree is still free for someone else to take and then hold
    # against its owner. Presence is the honest signal, and it costs one stat.
    at = owned_tree(here)
    if at is not None:
        path = claim_file(at[1], at[0])
        claim = read_claim(path)
        if claim is None or claim.get("session") == session or stale(claim, now):
            hold(path, at[0], session, transcript)

    for target, why in targets(payload, here):
        found = owned_tree(target)
        if found is None:
            continue  # The main checkout, or nothing of ours. Not this hook's business.
        tree, common = found
        path = claim_file(common, tree)
        claim = read_claim(path)
        if claim is None or claim.get("session") == session or stale(claim, now):
            hold(path, tree, session, transcript)
            continue
        deny(reason_foreign(tree, common, claim, now, why), warn_only)
        return


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001 — fail open, always.
        pass
    sys.exit(0)
