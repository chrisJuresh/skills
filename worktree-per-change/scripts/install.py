#!/usr/bin/env python3
"""Install, inspect or remove the worktree-per-change guard.

The guard is three hook registrations pointing at one script. `--session-ownership` adds
two more, pointing at a second and separate one: the guard keeps two *changes* out of each
other's way, and `worktree_owner.py` keeps two *sessions* out of one tree, which the guard
has nothing to say about. `--repo <path>` is the
usual install: the rule is a property of *the repository* — what its branches mean,
what its PRs are for — so it belongs in that repository's committed
`.claude/settings.json` where everyone working in it gets the same rule. Installing at
user scope (`~/.claude/`) is available for a machine where every repository should
behave this way, but it applies `development` (or whatever `--branch` says) to repos
that may integrate through something else, so prefer the per-repo install.

Any predecessor concurrent-writer guard found in the same settings file is removed, not
left beside this one: two hooks denying one action with two different remedies is the
flail both of them exist to prevent.

The integration branch is *asked for*, not defaulted. It is the one setting that cannot
be inferred and cannot be silently wrong: a repo told the wrong one opens every future PR
against a branch nobody merges, and nothing about that looks broken until someone goes
looking for the work. `--branch` answers it without a prompt, for scripted installs.

Some repositories cannot take a committed install at all — a shared checkout where
agent configuration would change a *teammate's* session is the case this was written
for, and there the answer is not to commit it anyway. `--settings-file
settings.local.json` registers the hooks in the gitignored file instead, and
`--guard-root <dir>` keeps the guard and `land.py` outside the repository and
references them absolutely. What that buys is a real install with nothing added to the
repo; what it costs is that **a fresh worktree does not contain an untracked settings
file**, so whatever creates worktrees has to put one there. The installer says so
rather than leaving it to be discovered.

Usage:
    python install.py --status                what is installed, and where the work is
    python install.py --repo . --dry-run      show the exact settings.json changes
    python install.py --repo .                install into this repository, committed
    python install.py --repo . --branch queue  ... integrating through `queue`
    python install.py --repo . --session-ownership   ... one worktree, one session
    python install.py --repo . --settings-file settings.local.json \
        --guard-root ~/tooling/.claude    ... committing nothing to the repository
    python install.py                         install at user scope
    python install.py --repo . --uninstall    remove it
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

GUARD_SOURCE = Path(__file__).resolve().parent / "worktree_guard.py"
GUARD_FILENAME = "worktree-guard.py"
OWNER_SOURCE = Path(__file__).resolve().parent / "worktree_owner.py"
OWNER_FILENAME = "worktree-owner.py"
LAND_SOURCE = Path(__file__).resolve().parent / "land.py"
LAND_FILENAME = "land.py"
CONFIG_FILENAME = "worktree-per-change.json"
DEFAULT_BRANCH = "development"

# The invocation the allowlist entry below matches, character for character. Permission
# rules are prefix matches on the command string, so the rule and the call have to be
# spelled the same way — a relative path from the worktree root, which is where the
# session's cwd already is, and the same on every platform.
LAND_COMMAND = f"python .claude/scripts/{LAND_FILENAME}"


def land_command(lander: Path | None, repo: Path | None,
                 interpreter: str = "python") -> str:
    """How `land.py` will actually be typed, which is what the allowlist has to match.

    In the ordinary install it is committed inside the repo and typed relative to the
    worktree root, which is where the session's cwd already is. Under `--guard-root` it
    lives outside the repository and there is no relative path that reaches it from every
    worktree, so the entry carries the absolute one. Getting this wrong is the quiet
    failure `rule()` warns about: the file looks right and every call is still stopped.

    The interpreter is part of the entry for the same reason, and `python` is a guess that
    a committed install has to make and a machine-local one does not: on a macOS box
    `python` is frequently not on `PATH` at all, so an entry naming it covers a command
    nobody can run. A local install already knows which interpreter exists here.
    """
    if lander is None:
        return LAND_COMMAND
    try:
        if repo is not None and lander.is_relative_to(repo):
            return f"{interpreter} {lander.relative_to(repo).as_posix()}"
    except (AttributeError, ValueError):  # is_relative_to is 3.9+
        pass
    return f"{interpreter} {lander}"

# Commands that never change a working tree, a branch, a remote or an account. They are
# listed so they are never *stopped*, which is a different question from whether they are
# *allowed* by the guard: a hook's `deny` beats a permission `allow`, so in a repo running
# this guard the allowlist can be generous exactly where the guard is strict.
#
# Two rules about the entries themselves, both learned by getting them wrong:
#
#   - Matching is a prefix match on the whole string, so `git branch:*` would allow
#     `git branch -D`, and `git config:*` would allow `git config user.email x`. Every
#     entry here names the read-only *subcommand or flag*, never the bare verb.
#   - `gh api` is absent on purpose. It carries `--method DELETE`, so allowing it allows
#     everything the token can do; there is no prefix that separates a GET from a merge.
READ_ONLY = [
    # --- git: interrogating history and state
    "git status:*", "git log:*", "git show:*", "git diff:*", "git diff-tree:*",
    "git diff-index:*", "git blame:*", "git shortlog:*", "git describe:*",
    "git name-rev:*", "git grep:*", "git count-objects:*", "git reflog show:*",
    # --- git: resolving names and refs
    "git rev-parse:*", "git rev-list:*", "git merge-base:*", "git for-each-ref:*",
    "git show-ref:*", "git ls-remote:*", "git symbolic-ref --short:*",
    # `git branch` alone would cover `-D`; these are the listing forms only.
    "git branch --list:*", "git branch --show-current:*", "git branch -a:*",
    "git branch -r:*", "git branch -v:*", "git branch -vv:*", "git branch --merged:*",
    # --- git: interrogating the tree and the config
    "git ls-files:*", "git ls-tree:*", "git cat-file:*", "git check-ignore:*",
    "git check-attr:*", "git config --get:*", "git config --get-all:*",
    "git config --list:*", "git remote -v:*", "git remote get-url:*",
    "git remote show:*", "git worktree list:*",
    # `git fetch` writes, but only to remote-tracking refs — never the working tree, the
    # index or a local branch. The protocol requires fetching before every worktree is
    # cut, so a fetch that needs asking is a fetch that gets skipped, and a stale base is
    # the failure that costs a whole change.
    "git fetch:*",
    # --- gh: reading the forge
    "gh pr view:*", "gh pr list:*", "gh pr diff:*", "gh pr checks:*", "gh pr status:*",
    "gh issue view:*", "gh issue list:*", "gh repo view:*", "gh run view:*",
    "gh run list:*", "gh workflow view:*", "gh workflow list:*", "gh release view:*",
    "gh release list:*", "gh label list:*", "gh search:*", "gh auth status:*",
]

# The protocol's own write commands. Narrow on purpose, and narrow in two different ways:
# `land.py` is narrow because of what the *script* refuses (it merges only the PR whose
# head is the branch in the worktree it runs from, into the branch the repo recorded), and
# the git entries are narrow because the *guard* refuses them outside a worktree.
#
# `Bash(gh pr merge:*)` is what this list exists to avoid. It would merge any PR in any
# repository the machine is authenticated to, on any base, which is a far larger grant
# than "this agent may finish the change it is working on".
_DELIVERY_GIT = [
    "git add:*", "git commit:*", "git push -u origin HEAD:*",
    "git switch -c:*", "git worktree add:*", "git worktree remove:*",
    "git worktree prune:*", "git branch -D:*",
]
DELIVERY = [f"{LAND_COMMAND}:*", *_DELIVERY_GIT]


def delivery(land: str = LAND_COMMAND) -> list[str]:
    return [f"{land}:*", *_DELIVERY_GIT]


# The two files this protocol needs that git will not carry for it, and that no hook can
# supply. Both are about what a worktree does *not* get, which is why installing the guard
# without them leaves a repository that fails in ways nothing on disk explains.
#
# `.gitignore`: the installer is what names `.claude/worktrees/` as where worktrees go, so
# the installer is what creates this hazard. A worktree is a checkout of the repository
# inside itself, and a `git add -A` that catches one commits it as a gitlink no clone can
# resolve. Measured in the first repository to adopt this guard: it arrived with exactly
# that already in its history, committed by an earlier session, and the ignore had to be
# added by hand before the next `git add -A` did it again.
#
# `.worktreeinclude`: `settings.local.json` holds this machine's permission mode and is
# ignored, so no worktree gets it and every worktree falls back to the default. What that
# looks like from inside is the protocol's own writes being refused for no visible reason
# -- measured 2026-08-15 in the same repository, `git add` allowed in one worktree and
# denied in the next one cut minutes later, which reads as the tool being broken rather
# than as a file being missing. It is the one ignored-but-required file every repo running
# this guard has, because the guard is what makes those writes necessary.
IGNORE_ENTRIES = (".claude/worktrees/", ".claude/settings.local.json")
# Why each one, for the `--status` report. They are ignored for entirely different
# reasons and a single message for both would be wrong about one of them.
IGNORE_WHY = {
    ".claude/worktrees/":
        "a `git add -A` over a live worktree commits it as a gitlink no clone can resolve",
    ".claude/settings.local.json":
        "this machine's permission mode is not everybody's, and committing it hands it to them",
}
IGNORE_NOTE = """
# Every change gets its own worktree under here, so each one is a checkout of this
# repository inside itself -- a `git add -A` that catches one commits it as a gitlink
# no clone can resolve. settings.local.json is this machine's permission mode, which
# is nobody else's. The guard itself stays TRACKED: .claude/settings.json,
# .claude/hooks/ and .claude/worktree-per-change.json, because a worktree only gets a
# file if git puts it there.
"""

INCLUDE_PATH = ".worktreeinclude"
INCLUDE_ENTRIES = (".claude/settings.local.json",)
INCLUDE_NOTE = """
# Untracked files copied into every new worktree.
#
# A worktree is a fresh checkout of tracked files and nothing else, so anything
# .gitignore covers is simply absent from it, and the failure looks like the tool being
# broken rather than the file being missing. settings.local.json is this machine's
# permission mode: without it a new worktree falls back to the default and the
# protocol's own writes start being refused.
"""


# `git check-ignore -v`: `<source>:<line>:<pattern>\t<path>`. Non-greedy, so a Windows
# drive letter in an absolute source (`C:/…/.git/info/exclude`) stays in the source.
IGNORE_SOURCE = re.compile(r"^(.*?):\d+:.*\t")


def carried_by_repository(repo: Path, entry: str) -> bool:
    """Whether the rule git applies to `entry` comes from a tracked `.gitignore`.

    `-v` names the one pattern that decided, and git consults its sources in precedence
    order -- the tree's `.gitignore` files, then `.git/info/exclude`, then
    `core.excludesFile` -- so a source that is not a tracked `.gitignore` means no
    tracked `.gitignore` covers the path at all. A negated pattern that wins exits
    non-zero, the same as no match.
    """
    # `check-ignore` exits non-zero when the path is not ignored, which `git()` returns
    # as None. A git that cannot answer at all lands in the same branch, and a line that
    # turns out to have been redundant is the cheap way to be wrong.
    verdict = git(repo, "check-ignore", "-v", entry)
    found = IGNORE_SOURCE.match(verdict or "")
    if not found or Path(found.group(1)).name != ".gitignore":
        return False
    # `ls-files` prints nothing for an untracked file and fails for one outside the tree,
    # which is where a global excludes file called `~/.gitignore` lives.
    return bool(git(repo, "ls-files", "--", found.group(1)))


def missing_ignores(repo: Path, entries=IGNORE_ENTRIES) -> list[str]:
    """Which of `entries` this repository itself does not already ignore.

    Asked of git rather than of the file, so a repo that already covers them under a
    broader pattern does not collect a redundant line.

    **Only a tracked `.gitignore` counts as an answer.** The question is whether the
    *repository* carries the rule, and the other two places git reads ignores from are
    this machine rather than the repository: a global `core.excludesFile`, and
    `.git/info/exclude`, which lives in the git directory and is never committed. Both
    were measured answering for it. On a machine whose global ignore names
    `**/.claude/settings.local.json`, the check reported nothing missing and the repo went
    out to everyone else without the entry; on 2026-09-23, a repo whose info/exclude
    named `.claude/worktrees/` got the explanatory note and `settings.local.json` and not
    the one line that keeps a live worktree from being committed as a gitlink. That is
    the same shape as a hash of the working copy being true only where it was computed --
    right on the machine that installed, false everywhere the file actually travels.

    The entry is probed exactly as it is written, trailing slash included: a `foo/`
    pattern matches directories only, and git decides what a path *is* by looking at the
    disk, so probing `foo` for a directory that does not exist yet answers no.
    """
    try:
        present = (repo / ".gitignore").read_text(encoding="utf-8").splitlines()
    except OSError:
        present = []
    missing = []
    for entry in entries:
        if entry in present or entry.rstrip("/") in present:
            continue
        if carried_by_repository(repo, entry):
            continue
        missing.append(entry)
    return missing


def missing_includes(repo: Path, entries=INCLUDE_ENTRIES) -> list[str]:
    """Which of `entries` are not already listed in `.worktreeinclude`."""
    try:
        present = (repo / INCLUDE_PATH).read_text(encoding="utf-8").splitlines()
    except OSError:
        present = []
    listed = {line.strip() for line in present if line.strip() and not line.startswith("#")}
    return [entry for entry in entries if entry not in listed]


def append_block(path: Path, note: str, entries: list[str]) -> None:
    """Append `note` and `entries` to `path`, leaving every byte already there alone.

    Appended as bytes on purpose. `write_text` translates newlines, so reading a file
    with LF endings and writing it back on Windows rewrites every existing line to CRLF
    -- a one-line addition arriving as a whole-file diff, in the file most likely to be
    under review at the time.
    """
    try:
        existing = path.read_bytes()
    except OSError:
        existing = b""
    tail = b"" if not existing or existing.endswith(b"\n") else b"\n"
    body = (note.lstrip("\n") + "\n".join(entries) + "\n").encode("utf-8")
    with path.open("ab") as handle:
        handle.write(tail + (b"\n" if existing else b"") + body)


def rule(command: str) -> str:
    """The settings.json spelling of an allowlist entry.

    A rule is `Bash(<command>)`, tool name and all — a bare `git status:*` in the allow
    list is not a narrower rule, it is a rule that matches nothing, and it fails in the
    quietest possible way: the file looks right, the entry is there, and every command it
    was supposed to cover still gets stopped. The lists above stay bare so they read as
    commands and so this spelling lives in exactly one place.
    """
    return f"Bash({command})"

MATCHER = "Write|Edit|NotebookEdit|Bash|PowerShell"
EVENTS = ("PreToolUse", "SessionStart", "Stop")
# The ownership hook has no `Stop` opinion: it arbitrates who may write in a tree, and a
# session ending is not a write. Registering it there would cost a interpreter start per
# stop to decide nothing.
OWNER_EVENTS = ("PreToolUse", "SessionStart")
STATE_DIRNAME = "claude-worktree-gate"

# Guards this one supersedes. A repo that ships one of these is mid-migration, not
# doubly protected — see references/replacing-a-concurrent-writer-guard.md.
LEGACY_GUARD = re.compile(r"concurrent[-_]?writer|writer[-_]?guard|parallel[-_]?guard", re.I)

SKILL_SOURCE = GUARD_SOURCE.parent.parent
SKILL_NAME = SKILL_SOURCE.name


def settings_path(root: Path, name: str = "settings.json") -> Path:
    return root / name


def guard_path(root: Path) -> Path:
    return root / "hooks" / GUARD_FILENAME


def owner_path(root: Path) -> Path:
    return root / "hooks" / OWNER_FILENAME


def land_path(root: Path) -> Path:
    # Beside the hooks rather than among them: this one is invoked by a person or an
    # agent, not by an event, and the directory it sits in is part of the allowlist entry.
    return root / "scripts" / LAND_FILENAME


def add_permissions(settings: dict, entries: list[str]) -> tuple[dict, int]:
    """Union `entries` into `permissions.allow`, preserving order and whatever is there.

    A union rather than a write: the permissions block is the repo's, and an operator who
    allowed something else is not asking this installer to have an opinion about it. The
    same reasoning as the config file — a resync must not be the moment a decision
    disappears.
    """
    block = settings.setdefault("permissions", {})
    if not isinstance(block, dict):
        settings["permissions"] = block = {}
    allow = block.setdefault("allow", [])
    if not isinstance(allow, list):
        block["allow"] = allow = []
    have = {rule for rule in allow if isinstance(rule, str)}
    added = [rule for rule in entries if rule not in have]
    allow.extend(added)
    return settings, len(added)


# An allowlist entry for `land.py` exactly as this installer writes one: an interpreter,
# a path ending in the script, and the trailing wildcard. Removing by exact string cannot
# do this job, because the interpreter and the path both depend on flags the uninstall is
# usually run without — and an entry left behind is a standing grant for a script that is
# gone. It is deliberately narrow: an operator who NARROWED the rule by hand
# (`… land.py --dry-run:*`) has made a decision, and this does not match it.
LAND_ENTRY = re.compile(r"^Bash\((?:\S+\s+)?\S*" + re.escape(LAND_FILENAME) + r":\*\)$")


def drop_permissions(settings: dict, entries: list[str]) -> dict:
    block = settings.get("permissions")
    if not isinstance(block, dict) or not isinstance(block.get("allow"), list):
        return settings
    ours = set(entries)
    block["allow"] = [
        rule for rule in block["allow"]
        if rule not in ours and not (isinstance(rule, str) and LAND_ENTRY.match(rule))
    ]
    if not block["allow"]:
        block.pop("allow")
    if not block:
        settings.pop("permissions")
    return settings


def choose_branch(repo: Path | None, given: str | None) -> str:
    """The branch changes merge into — asked, never assumed.

    This is the setting that is silently wrong. A guard installed with the wrong one
    denies nothing and breaks nothing; it just aims every future PR at a branch nobody
    merges, and the work sits there looking delivered. It cost a real repository exactly
    that: `development` existed, so the default took it, while the branch its PRs actually
    landed on was `main`.

    So it is a question, with the repository's own evidence offered as the answer. When
    there is nobody to ask — a scripted install, CI, a pipe — it refuses instead of
    guessing, because the guess is the failure.

    **A repository that has already answered is not asked again.** The recorded answer
    outranks everything below, because the commonest run of this installer is not a first
    install but a **resync**, and re-asking there is how a recorded decision gets
    overwritten: the list offered below is the remote's branches with the *default* branch
    named as such, and for every repository this protocol is built for the default branch
    is exactly the wrong answer. Both known consumers integrate through something else —
    `development` where the default is `main`, `queue` where the default is `master` — so
    an operator resyncing was shown the wrong branch as the obvious one and asked to retype
    the right one from memory. `--branch` still overrides, which is how a repository that
    genuinely changes its integration branch says so.
    """
    if given:
        return given
    if repo is not None:
        recorded = load(repo / ".claude" / CONFIG_FILENAME).get("integrationBranch")
        if isinstance(recorded, str) and recorded.strip():
            return recorded.strip()
    candidates: list[str] = []
    if repo is not None:
        head = git(repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
        if head:
            candidates.append(head.split("/", 1)[-1])
        listing = git(repo, "for-each-ref", "--format=%(refname:short)", "refs/remotes/origin")
        for line in (listing or "").splitlines():
            name = line.split("/", 1)[-1].strip()
            if name and name != "HEAD" and name not in candidates:
                candidates.append(name)

    def unanswerable() -> SystemExit:
        hint = f" (this repo has {', '.join(candidates[:6])})" if candidates else ""
        print(
            "which branch do changes merge into? pass --branch NAME.\n"
            f"Nothing is assumed here{hint}: a repo pointed at the wrong integration "
            "branch opens every PR against a branch nobody merges, and that failure is "
            "invisible until someone goes looking for the work.",
            file=sys.stderr,
        )
        return SystemExit(2)

    # Whether stdin is a terminal is not the question — whether an answer arrives is. The
    # two come apart in both directions: under Git Bash on Windows `isatty()` reports a
    # terminal for stdin redirected from /dev/null (measured — the prompt went out and the
    # read raised), and a perfectly good answer arrives down a pipe from a script, where
    # `isatty()` is false. So ask, read, and treat end-of-input as the refusal.
    print("Which branch do changes in this repository merge into?")
    if candidates:
        print("  on the remote: " + ", ".join(candidates[:8]))
        print(f"  (its default branch is {candidates[0]})")
    print(
        "  Some repos integrate through the default branch; others hold changes on a\n"
        "  `development` or `queue` branch and promote from there. It is the base every\n"
        "  PR this guard opens will target, so it has to be the one people actually merge."
    )
    while True:
        try:
            answer = input("branch: ").strip()
        except EOFError:
            raise unanswerable() from None
        if answer:
            return answer
        print("  a branch name is required — there is no safe default for this.")


def load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def hook_text(hook: dict) -> str:
    return " ".join(str(p) for p in (hook.get("command"), *(hook.get("args") or [])) if p)


def is_ours(hook: dict) -> bool:
    # Both hooks, so `strip` clears an ownership registration whose script this run is not
    # going to write back. Otherwise `--no-session-ownership` deletes the file and leaves
    # the hook pointing at it, which is an error on every tool call rather than an
    # uninstall.
    blob = hook_text(hook)
    return any(
        name in blob
        for name in (GUARD_FILENAME, "worktree_guard.py", OWNER_FILENAME, "worktree_owner.py")
    )


def is_owner(hook: dict) -> bool:
    blob = hook_text(hook)
    return OWNER_FILENAME in blob or "worktree_owner.py" in blob


def is_legacy(hook: dict) -> bool:
    return not is_ours(hook) and bool(LEGACY_GUARD.search(hook_text(hook)))


def link_skill(user_root: Path, dry_run: bool) -> str:
    """Make `/worktree-per-change` resolve in every repo.

    The guard's denials point at this skill by name, so a machine with the hook and
    without the skill hands an agent a dead reference at exactly the moment it needs
    the protocol. A link rather than a copy, so editing the source keeps working. On
    Windows a directory junction is the one form that needs no privilege.
    """
    destination = user_root / "skills" / SKILL_NAME
    if destination.exists() or destination.is_symlink():
        return f"skill   -> {destination} (already there, left alone)"
    if dry_run:
        return f"would link {destination} -> {SKILL_SOURCE}"
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.symlink(SKILL_SOURCE, destination, target_is_directory=True)
        return f"skill   -> {destination} (symlink to {SKILL_SOURCE})"
    except (OSError, NotImplementedError, AttributeError):
        pass
    if os.name == "nt":
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(destination), str(SKILL_SOURCE)],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return f"skill   -> {destination} (junction to {SKILL_SOURCE})"
    shutil.copytree(SKILL_SOURCE, destination)
    return f"skill   -> {destination} (copy — re-run install after editing the source)"


def strip(settings: dict, also_legacy: bool) -> tuple[dict, list[str]]:
    """Remove this guard's registrations, and optionally any predecessor's."""
    removed: list[str] = []
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return settings, removed
    for event in list(hooks):
        matchers = hooks.get(event)
        if not isinstance(matchers, list):
            continue
        kept = []
        for matcher in matchers:
            if not isinstance(matcher, dict):
                kept.append(matcher)
                continue
            inner = []
            for hook in matcher.get("hooks") or []:
                if isinstance(hook, dict) and (is_ours(hook) or (also_legacy and is_legacy(hook))):
                    if is_legacy(hook):
                        removed.append(f"{event}: {hook_text(hook)}")
                    continue
                inner.append(hook)
            if inner:
                kept.append({**matcher, "hooks": inner})
            elif not matcher.get("hooks"):
                kept.append(matcher)
        if kept:
            hooks[event] = kept
        else:
            hooks.pop(event)
    if not hooks:
        settings.pop("hooks", None)
    return settings, removed


def entry(interpreter: str, script: str, event: str, owner: bool = False) -> dict:
    # `-S` skips site initialisation, ~13% of interpreter startup and worth having on a
    # hook that runs before every write-tool call. Both hooks are stdlib-only by design so
    # that they can.
    hook = {"type": "command", "command": interpreter, "args": ["-S", script], "timeout": 10}
    if event == "PreToolUse":
        hook["statusMessage"] = (
            "Checking this worktree is this session's" if owner
            else "Checking this change is in its own worktree"
        )
        return {"matcher": MATCHER, "hooks": [hook]}
    if event == "Stop":
        hook["timeout"] = 20  # It shells out to git; only here, and only once per stop.
    return {"hooks": [hook]}


def add_ours(settings: dict, interpreter: str, script: str, owner: str | None = None) -> dict:
    """Register the guard, and the ownership hook when the repo asked for one.

    Two separate registrations rather than one hook doing both jobs. They answer different
    questions — "is this tree a worktree, on the right branch, not already merged" against
    "is it *yours*" — they keep separate state, and a repo can run either alone. It also
    keeps the guard a file downstream repos can vendor by digest, which they cannot do
    with one that grew a second rule.

    Order matters slightly and in the guard's favour: it is appended first, so a call that
    breaks both rules is denied with the protocol's own message rather than with a
    remedy that assumes the tree was legitimate to begin with.
    """
    hooks = settings.setdefault("hooks", {})
    for event in EVENTS:
        bucket = hooks.setdefault(event, [])
        if not isinstance(bucket, list):
            hooks[event] = bucket = []
        bucket.append(entry(interpreter, script, event))
    if owner:
        for event in OWNER_EVENTS:
            bucket = hooks.setdefault(event, [])
            if not isinstance(bucket, list):
                hooks[event] = bucket = []
            bucket.append(entry(interpreter, owner, event, owner=True))
    return settings


def write_json(path: Path, blob: dict, backup: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if backup and path.exists():
        shutil.copy2(path, path.with_suffix(f".json.{time.strftime('%Y%m%d-%H%M%S')}.bak"))
    body = json.dumps(blob, indent=2) + "\n"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(body, encoding="utf-8")
    os.replace(temporary, path)


def content_hash(data: bytes) -> str:
    """sha256 of `data` with its line endings normalised to LF.

    The record has to survive the round trip through git, and the bytes on disk do not.
    A repo that pins `* text=auto eol=lf` hands out LF on every platform; one that
    leaves it to `core.autocrlf` hands out CRLF on Windows and LF everywhere else. So
    hashing the working copy records a number that is true on the machine that ran the
    installer and false on the Linux runner meant to check it — and it fails in the
    direction that costs most, reporting drift in a file nobody touched.

    Measured: installing into a repo with `eol=lf` from a Windows checkout, where the
    copy arrives with 1022 CRLFs, gives a hash matching no checkout of that repo on any
    platform, including the one that wrote it, as soon as git normalises the file.

    Normalising is what git itself stores, so both sides can reach the same number
    without knowing each other's settings. A gate checking this must normalise too —
    see SKILL.md.
    """
    return hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest()


def provenance(script: Path, source: Path = GUARD_SOURCE) -> dict:
    """Where the copy at `script` came from — source, upstream commit, hash.

    A committed guard is a fork the moment this skill moves, and a stale one is the
    kind of broken hook that still looks like it works: it denies confidently and
    prints a remedy that no longer fits. Recording which commit the copy came from is
    what lets a repo's own gate ask whether it has drifted.

    The installer writes it because the installer is the only thing that knows both
    halves at once, and knows them at the only moment they are both true. Left to a
    human step it is written once and then silently wrong from the next resync on —
    which is the same failure one level up.

    Every field is best-effort. A skill directory that is not a git checkout still
    installs; it just cannot say which commit it was, and an absent `syncedFrom` is
    honest where a stale one is not.
    """
    where = f"{SKILL_NAME}/scripts/{source.name}"
    record: dict = {"source": where}
    origin = git(SKILL_SOURCE, "remote", "get-url", "origin")
    if origin:
        stem = origin[: -len(".git")] if origin.endswith(".git") else origin
        record["source"] = f"{stem} {where}"
    head = git(SKILL_SOURCE, "rev-parse", "HEAD")
    if head:
        record["syncedFrom"] = head
    try:
        record["sha256"] = content_hash(script.read_bytes())
    except OSError:
        pass
    return record


# ------------------------------------------------------------------------- status


def find_tree(start: Path):
    for directory in [start, *start.parents]:
        marker = directory / ".git"
        try:
            if marker.is_dir():
                return directory, marker, False
            if marker.is_file():
                text = marker.read_text(encoding="utf-8", errors="replace").strip()
                if text.startswith("gitdir:"):
                    git_dir = Path(text.split(":", 1)[1].strip())
                    if not git_dir.is_absolute():
                        git_dir = directory / git_dir
                    return directory, Path(os.path.normpath(str(git_dir))), True
        except OSError:
            return None
    return None


def git(tree: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(tree), *args], capture_output=True, text=True, timeout=15
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


# What a repository has decided for itself, for the `--status` report. Every one of these
# is optional and off when absent, which is the property that lets one skill serve
# repositories that disagree — and it is also what makes them invisible. A resync prints
# this so an operator can see which of their repo's decisions are recorded and which are
# not: the mechanism existing is not the same as this repository having used it, and the
# gap between those two is silent everywhere else.
# `always` is False for a key this installer writes itself with a default in it. Presence
# is a decision for a hand-written key and means nothing for an installer-written one, and
# conflating the two is how "this repo declared nothing" stops being sayable:
# `sessionOwnership` is recorded either way so a resync keeps the answer, so a repo that
# has never heard of it still has the key.
DECLARATIONS = (
    ("delivery", "delivery: this repo's own commands, in place of push/PR/merge", True),
    ("protectedMergeTargets", "protected merge targets: no session merges into these", True),
    ("mergeIntegrationBeforeLanding", "land.py merges the integration branch down first", True),
    ("worktreesRoot", "worktrees go here, and the remedy text says so", True),
    ("sessionOwnership", "one worktree, one session", False),
)


def declarations(blob: dict) -> list[str]:
    """One line per decision this repository has recorded, or one saying it recorded none."""
    lines = []
    for name, what, hand_written in DECLARATIONS:
        # For a hand-written key, `false` and `[]` are answers the repo gave rather than
        # absences, so presence is the test and not truthiness — reading
        # `mergeIntegrationBeforeLanding: false` as "nothing here" is how a report would
        # invite a decision that has already been made.
        if name not in blob:
            continue
        value = blob[name]
        if not hand_written and not value:
            continue
        if isinstance(value, dict):
            detail = ", ".join(f"{k}={json.dumps(v)}" for k, v in value.items())
        elif isinstance(value, list):
            detail = ", ".join(str(v) for v in value) or "(none)"
        else:
            detail = json.dumps(value)
        lines.append(f"{what} — {detail}")
    if not lines:
        lines.append(
            "no other declarations — this repo takes the default protocol throughout. "
            "See references/guard-internals.md#configuration for what it may declare."
        )
    return lines


def report_status(user_root: Path, repo: Path | None) -> int:
    # Both spellings, because `--settings-file settings.local.json` is a real install and a
    # status that reads only settings.json reports it as absent. "Not installed" about a
    # guard that is running is the one answer here that makes somebody install it twice.
    scopes = [("user", user_root, "settings.json")]
    if repo is not None:
        scopes += [("repo", repo / ".claude", "settings.json"),
                   ("local", repo / ".claude", "settings.local.json")]
    for label, root, name in scopes:
        if label == "local" and not settings_path(root, name).is_file():
            continue
        settings = load(settings_path(root, name))
        events, owner_events, legacy = [], [], []
        for event, matchers in (settings.get("hooks") or {}).items():
            for matcher in matchers if isinstance(matchers, list) else []:
                for hook in (matcher or {}).get("hooks") or []:
                    if not isinstance(hook, dict):
                        continue
                    # Owner first: `is_ours` covers both so that `strip` clears both, so
                    # asking it first would report every ownership registration as a
                    # guard one and the status would say the rule is installed twice.
                    if is_owner(hook):
                        owner_events.append(event)
                    elif is_ours(hook):
                        events.append(event)
                    elif is_legacy(hook):
                        legacy.append(event)
        state = f"installed ({', '.join(sorted(set(events)))})" if events else "not installed"
        print(f"{label:5} {settings_path(root, name)}  ->  {state}")
        if owner_events:
            print(f"      + session ownership ({', '.join(sorted(set(owner_events)))})")
        if events and label == "local":
            print("      ! untracked, so absent from every fresh worktree unless something "
                  "writes it there")
        if legacy:
            print(f"      ! a predecessor guard is still registered ({', '.join(sorted(set(legacy)))})")

    print(f"\nmode: {os.environ.get('CLAUDE_WORKTREE_GATE') or 'on (default)'}"
          f"   ownership: {os.environ.get('CLAUDE_WORKTREE_OWNER') or 'on (default)'}")

    # `--repo` names the repository being reported on, so the working half reads it too.
    # It used to read `Path.cwd()` unconditionally, which put the named repo's settings and
    # some *other* repo's worktrees under one heading with nothing saying they differed.
    where = repo if repo is not None else Path.cwd()
    located = find_tree(where)
    if located is None:
        print(f"{where} is not a git repository — the guard stands down there.")
        return 0
    tree, git_dir, linked = located
    common = git_dir
    pointer = git_dir / "commondir"
    if pointer.is_file():
        target = Path(pointer.read_text(encoding="utf-8").strip())
        common = Path(os.path.normpath(str(git_dir / target if not target.is_absolute() else target)))
    main_root = common.parent if common.name == ".git" else tree

    declared = load(main_root / ".claude" / CONFIG_FILENAME)
    branch = os.environ.get("CLAUDE_INTEGRATION_BRANCH") or (
        declared.get("integrationBranch") or DEFAULT_BRANCH
    )
    print(f"\nrepository: {main_root}")
    print(f"integrates through: {branch}")
    for line in declarations(declared):
        print(f"  {line}")
    print(f"{where} is: "
          + ("a worktree — writes allowed" if linked else "the MAIN CHECKOUT — writes denied"))

    # Both of these are silent when wrong, and neither is repaired by anything the guard
    # does at runtime — a repo installed before the installer wrote them has to be told.
    # Only asked of a repo that has this guard: elsewhere an unignored `.claude/worktrees/`
    # is not a finding, it is a directory nothing is going to put a worktree in.
    installed = (main_root / ".claude" / CONFIG_FILENAME).is_file()
    unignored = missing_ignores(main_root) if installed else []
    unincluded = missing_includes(main_root) if installed else []
    for entry in unignored:
        print(f"  ! {entry} is not ignored — {IGNORE_WHY.get(entry, 'it should be')}")
    for entry in unincluded:
        print(f"  ! {entry} is not in .worktreeinclude — new worktrees fall back to the "
              "default permission mode, and the protocol's own writes start being refused")
    if unignored or unincluded:
        print("    re-running the installer adds them; it appends and does not rewrite")

    listing = git(main_root, "worktree", "list", "--porcelain") or ""
    spent_dir = common / STATE_DIRNAME / "spent"
    claims_dir = common / STATE_DIRNAME / "claims"
    trees = [line.split(" ", 1)[1] for line in listing.splitlines() if line.startswith("worktree ")]
    if len(trees) <= 1:
        print("  no worktrees — the next change needs one")
        return 0
    print("  worktrees:")
    for path in trees[1:]:
        where = Path(path)
        head = git(where, "rev-parse", "--abbrev-ref", "HEAD") or "?"
        dirty = git(where, "status", "--porcelain") or ""
        ahead = git(where, "rev-list", "--count", f"origin/{branch}..HEAD")
        unlanded = int(ahead) if (ahead or "").isdigit() else 0
        stem = re.sub(r"[^A-Za-z0-9._-]", "_", where.name)
        spent = (spent_dir / f"{stem}.json").is_file()
        flags = []
        if spent:
            flags.append("merged/spent")
        # Reported whether or not the ownership hook is installed here. A claim file is
        # left behind by a session that held this tree, and knowing that is useful exactly
        # when the hook has since been turned off and the collision is possible again.
        claim = load(claims_dir / f"{stem}.json")
        if claim.get("session"):
            flags.append(f"held by {str(claim['session']).split('-')[0]}")
        if dirty:
            flags.append(f"{len(dirty.splitlines())} uncommitted")
        if unlanded:
            flags.append(f"{unlanded} unlanded commit(s)")
        print(f"    {where.name:32} {head:32} {', '.join(flags) or 'clean and landed'}")
    return 0


# --------------------------------------------------------------------------- main


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", metavar="PATH", help="install into a repository instead of ~/.claude")
    parser.add_argument("--branch", metavar="NAME", help="branch changes merge into (asked for if omitted)")
    parser.add_argument("--uninstall", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--python", metavar="EXE", help="interpreter to run the guard with")
    parser.add_argument("--keep-legacy", action="store_true", help="leave a predecessor guard registered")
    parser.add_argument("--no-skill", action="store_true", help="skip linking the skill into ~/.claude/skills")
    parser.add_argument("--no-permissions", action="store_true",
                        help="skip the allowlist for read-only and protocol commands")
    parser.add_argument("--permissions-only", action="store_true",
                        help="write only the allowlist — no hooks, no guard, no config")
    parser.add_argument("--settings-file", metavar="NAME", default="settings.json",
                        help="settings file to register in (e.g. settings.local.json, "
                             "for a repo where agent config must not be committed)")
    parser.add_argument("--guard-root", metavar="DIR",
                        help="keep the guard and land.py in DIR and reference them "
                             "absolutely, instead of copying them into the repository")
    parser.add_argument("--worktrees-root", metavar="PATH",
                        help="where this repo's worktrees go, quoted in the guard's "
                             "remedy text (default .claude/worktrees)")
    # Three states, not two: the repo's recorded answer is the default, and either flag
    # overrides it for one run. A plain boolean would make "the repo asked for this"
    # indistinguishable from "this resync forgot to", and a resync that silently retires a
    # rule people are relying on is the worst of the three outcomes.
    parser.add_argument("--session-ownership", action="store_true", default=None,
                        help="also install worktree-owner.py: one worktree, one session")
    parser.add_argument("--no-session-ownership", dest="session_ownership",
                        action="store_false", help="skip it, whatever the repo records")
    args = parser.parse_args()

    try:  # Windows consoles default to a codepage that mangles the report's punctuation.
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass

    user_root = Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude"))
    repo = Path(args.repo).resolve() if args.repo else None
    root = (repo / ".claude") if repo else user_root
    # Where the guard and land.py are PUT, which is no longer always where the settings
    # file is. Kept separate from `root` so the settings file, the config record and the
    # scripts can each end up in the place that repository allows.
    files_root = Path(args.guard_root).expanduser().resolve() if args.guard_root else root
    # Committed means "git is the backup and a `.bak` beside it lands in someone's `git
    # status`". A settings.local.json is gitignored by convention everywhere this matters,
    # so it gets the backup that a committed file must not have.
    committed = repo is not None and args.settings_file == "settings.json" and not args.guard_root
    # A committed install is read on other machines and must not carry this one's paths;
    # a local one is nobody else's file, and pinning what actually exists here beats
    # naming `python` and hoping. Computed here because the allowlist entry for `land.py`
    # has to be spelled with the same interpreter that will type it.
    interpreter = args.python or ("python" if committed else sys.executable)

    if args.session_ownership is None and repo is not None:
        args.session_ownership = bool(
            load(repo / ".claude" / CONFIG_FILENAME).get("sessionOwnership")
        )
    # Never at user scope. The ownership hook keys its claims off the repository's common
    # git dir, so it is per-repo state by construction, and a user-scope registration would
    # run it against every repository on the machine — including the ones with no worktree
    # protocol at all, where it has nothing to say and one interpreter start to say it in.
    if repo is None:
        args.session_ownership = False

    if args.status:
        return report_status(user_root, repo)

    if args.permissions_only:
        # The read-only half, and nothing else. This is the user-scope install that
        # actually makes sense: a repo-scoped rule cannot cover a session that has to read
        # a *different* repository, and installing this guard into another repo is exactly
        # that shape of task. The hooks stay per repo, because the rule they enforce does.
        entries = [rule(command) for command in READ_ONLY]
        if repo:
            entries += [rule(command) for command in
                        delivery(land_command(land_path(files_root), repo, interpreter))]
        target = settings_path(root, args.settings_file)
        settings = load(target)
        settings, granted = add_permissions(settings, entries)
        if args.dry_run:
            print(f"would allow {granted} command(s) in {target}\n"
                  + "\n".join(f"  {r}" for r in entries))
            return 0
        write_json(target, settings, backup=not committed)
        print(f"allow   -> {target} ({granted} added, {len(entries) - granted} already there)")
        print("Restart or /reload any running sessions for the rules to take effect.")
        return 0

    if not GUARD_SOURCE.is_file():
        print(f"guard script missing: {GUARD_SOURCE}", file=sys.stderr)
        return 1
    if args.session_ownership and not OWNER_SOURCE.is_file():
        # Refused rather than quietly downgraded to a guard-only install. The operator
        # asked for a rule; installing three quarters of one and printing nothing is how a
        # repo ends up believing it is protected.
        print(f"ownership hook missing: {OWNER_SOURCE}", file=sys.stderr)
        return 1

    target = settings_path(root, args.settings_file)
    settings = load(target)
    before = json.dumps(settings, indent=2)
    settings, removed = strip(settings, also_legacy=not args.keep_legacy)

    if args.uninstall:
        # Only the entries this installer added, and only by exact match. An operator who
        # allowed something else, or narrowed one of ours by hand, has made a decision;
        # an uninstaller that took the whole block would silently reverse it.
        # Both spellings of the land.py entry, not just the one this invocation would
        # write. An uninstall is frequently run without the `--guard-root` the install
        # had, and an entry left behind is a grant nobody remembers making.
        stale = (READ_ONLY + delivery()
                 + delivery(land_command(land_path(files_root), repo, interpreter)))
        settings = drop_permissions(settings, [rule(c) for c in dict.fromkeys(stale)])
        after = json.dumps(settings, indent=2)
        if args.dry_run:
            print(f"would rewrite {target}\n--- before\n{before}\n--- after\n{after}")
            return 0
        # No `.bak` beside a committed settings file: git is already the backup, and the
        # stray file shows up in `git status` for whoever installs next.
        write_json(target, settings, backup=not committed)
        for path in (guard_path(files_root), owner_path(files_root), land_path(files_root),
                     (repo / ".claude" / CONFIG_FILENAME) if repo else None):
            if path is None:
                continue
            try:
                path.unlink()
            except OSError:
                pass
        linked = user_root / "skills" / SKILL_NAME
        if not repo and (linked.is_symlink() or (os.name == "nt" and linked.is_dir())):
            try:
                linked.unlink() if linked.is_symlink() else os.rmdir(linked)
                print(f"unlinked {linked}")
            except OSError:
                print(f"leave {linked} in place — remove it by hand if you want it gone")
        print(f"removed the guard from {target}")
        kept = [name for name in (".gitignore", INCLUDE_PATH)
                if repo and (repo / name).is_file()]
        if kept:
            # Left alone deliberately. Un-ignoring `.claude/worktrees/` is how a stale
            # checkout ends up committed as a gitlink, and that outlives the guard.
            print(", ".join(kept)
                  + (" are" if len(kept) > 1 else " is")
                  + " left as found — edit by hand if you want the worktree entries gone.")
        return 0

    script = guard_path(files_root)
    # A repo install is committed and read on other machines and in every worktree, so it
    # must not carry this machine's interpreter path or this checkout's absolute location:
    # `${CLAUDE_PROJECT_DIR}` resolves to whichever tree the session is actually in, and
    # `python` resolves to whatever that machine has. A user-scope install is the opposite
    # case — it is nobody else's file and there is no project dir to expand — so it pins
    # the interpreter that ran the installer.
    # `--guard-root` puts the guard outside the repository, and then `${CLAUDE_PROJECT_DIR}`
    # is exactly the wrong reference: it resolves to whichever tree the session is in, which
    # is where the file deliberately is not. The absolute path is also what makes ONE copy
    # serve every worktree of every repo installed this way, which is the point of the flag.
    reference = (
        "${CLAUDE_PROJECT_DIR}/.claude/hooks/" + GUARD_FILENAME
        if repo and not args.guard_root
        else str(script)
    )
    owner_script = owner_path(files_root) if args.session_ownership else None
    owner_reference = None
    if owner_script is not None:
        owner_reference = (
            "${CLAUDE_PROJECT_DIR}/.claude/hooks/" + OWNER_FILENAME
            if repo and not args.guard_root
            else str(owner_script)
        )
    settings = add_ours(settings, interpreter, reference, owner_reference)
    # A user-scope install gets the read-only entries and not the delivery ones. The
    # delivery entries are safe *because the guard scopes them* — `git commit:*` is
    # bounded by a hook that denies it outside a worktree — and at user scope they would
    # apply to repositories that have no such hook.
    lander = land_path(files_root) if repo else None
    wanted = [] if args.no_permissions else [
        rule(command) for command in
        (READ_ONLY + delivery(land_command(lander, repo, interpreter)) if repo else READ_ONLY)
    ]
    settings, granted = add_permissions(settings, wanted)
    after = json.dumps(settings, indent=2)
    branch = choose_branch(repo, args.branch)
    config = (repo / ".claude" / CONFIG_FILENAME) if repo else None

    if args.dry_run:
        print(f"would copy  {GUARD_SOURCE}\n        ->  {script}")
        if owner_script is not None:
            print(f"would copy  {OWNER_SOURCE}\n        ->  {owner_script}")
        if lander is not None:
            print(f"would copy  {LAND_SOURCE}\n        ->  {lander}")
        if granted:
            print(f"would allow {granted} command(s) in {target} (permissions.allow)")
        if config is not None:
            # The hash is of the file that WOULD be copied, so the dry run shows the record
            # the real run will write rather than a placeholder for it.
            print(f"would write {config}  ->  integrationBranch = {branch}"
                  + (f", worktreesRoot = {args.worktrees_root}" if args.worktrees_root else "")
                  + f", sessionOwnership = {bool(args.session_ownership)}"
                  + f", guard = {json.dumps(provenance(GUARD_SOURCE))}")
        if repo:
            for entry in missing_ignores(repo):
                print(f"would ignore {entry} in {repo / '.gitignore'}")
            for entry in missing_includes(repo):
                print(f"would copy {entry} into every new worktree "
                      f"via {repo / INCLUDE_PATH}")
        for line in removed:
            print(f"would remove predecessor guard  {line}")
        if not args.no_skill:
            print(link_skill(user_root, dry_run=True))
        print(f"would rewrite {target}\n--- before\n{before}\n--- after\n{after}")
        return 0

    script.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(GUARD_SOURCE, script)
    if owner_script is not None:
        owner_script.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(OWNER_SOURCE, owner_script)
    elif args.session_ownership is False:
        # Asked for explicitly, so the file goes as well as the registration. A script left
        # behind after `--no-session-ownership` is one a later resync would find and take
        # for a deliberate copy.
        try:
            owner_path(files_root).unlink()
        except OSError:
            pass
    if lander is not None and LAND_SOURCE.is_file():
        lander.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(LAND_SOURCE, lander)
    if config is not None:
        # Merged into what is there rather than written over it. The file is the repo's,
        # not this installer's: it carries the branch, the provenance below, and whatever
        # else that repo decided belongs beside them, and a resync is exactly the moment
        # a replace would drop the lot.
        blob = load(config)
        blob["integrationBranch"] = branch
        if args.worktrees_root:
            # Recorded only when asked for. The guard defaults to `.claude/worktrees`, and
            # writing that default in explicitly would turn a value the skill can change
            # into one every repo has pinned to today's answer.
            blob["worktreesRoot"] = args.worktrees_root
        blob["guard"] = provenance(script)
        # Recorded either way, so a resync without the flag keeps whichever answer this
        # repo gave. `worktreesRoot` is recorded only when asked for because its default
        # is the skill's to change; this one is a rule the repo either adopted or did not.
        blob["sessionOwnership"] = bool(args.session_ownership)
        if owner_script is not None and owner_script.is_file():
            blob["owner"] = provenance(owner_script, OWNER_SOURCE)
        else:
            blob.pop("owner", None)
        if lander is not None and lander.is_file():
            # Recorded for the same reason the guard's copy is: it is committed, so it is
            # a fork the moment this skill moves, and a repo's gate can only ask whether
            # it has drifted if something wrote down where it came from.
            blob["land"] = provenance(lander, LAND_SOURCE)
        write_json(config, blob, backup=False)
        synced = blob["guard"].get("syncedFrom")
        print(f"config  -> {config} (integrationBranch = {branch}"
              f"{', syncedFrom ' + synced[:12] if synced else ''})")
    # Both are appended, never rewritten, and only for what is missing: these are the
    # repo's own files and are likely to have been curated by hand.
    ignores = missing_ignores(repo) if repo else []
    includes = missing_includes(repo) if repo else []
    if repo and ignores:
        append_block(repo / ".gitignore", IGNORE_NOTE, ignores)
        print(f"ignore  -> {repo / '.gitignore'} ({', '.join(ignores)})")
    if repo and includes:
        append_block(repo / INCLUDE_PATH, INCLUDE_NOTE, includes)
        print(f"include -> {repo / INCLUDE_PATH} ({', '.join(includes)})")
    for line in removed:
        print(f"removed predecessor guard  {line}")
    # Same rule as the uninstall path, and it was missing here: no `.bak` beside a
    # *committed* settings file. Git is already the backup, and the stray file lands in the
    # next person's `git status` — measured 2026-08-15 on a resync, where it showed up
    # untracked beside the change it was supposed to be protecting. A user-scope settings
    # file has no git behind it, so that one is still copied first.
    write_json(target, settings, backup=not committed)
    print(f"guard   -> {script}")
    if owner_script is not None:
        print(f"owner   -> {owner_script} (one worktree, one session; "
              f"release a tree with `python3 {owner_script} --release <tree>`)")
    if lander is not None:
        print(f"land    -> {lander} (run it as "
              f"`{land_command(lander, repo, interpreter)}` from a worktree)")
    if not args.no_skill:
        print(link_skill(user_root, dry_run=False))
    print(f"hooks   -> {target} ({', '.join(EVENTS)}"
          + (f"; ownership on {', '.join(OWNER_EVENTS)}" if owner_script is not None else "")
          + ")")
    if granted:
        scope = "read-only git/gh, and the protocol's own writes" if repo else "read-only git/gh"
        print(f"allow   -> {target} ({granted} command(s): {scope})")
    print("Restart or /reload any running sessions for the hooks to take effect.")
    if committed:
        files = ([target, script, config] + ([owner_script] if owner_script is not None else [])
                 + ([lander] if lander is not None else []))
        if ignores:
            files.append(repo / ".gitignore")
        if includes:
            files.append(repo / INCLUDE_PATH)
        print("Commit " + ", ".join(str(p.relative_to(repo)) for p in files)
              + " for everyone working here to get it.")
    elif repo:
        # The honest version of the sentence above, and the one thing about this install
        # that will otherwise be found out the hard way. A worktree is a checkout of
        # TRACKED files, so an untracked settings file is absent from every worktree this
        # guard sends a session into — the rule would apply in the main checkout, where
        # nothing is supposed to happen, and nowhere else.
        print(f"Nothing here is committed: {target.name} is not tracked, so it does NOT "
              "exist in a fresh worktree.")
        print("Whatever creates worktrees here has to write one into each of them, or the "
              "guard covers only the main checkout.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
