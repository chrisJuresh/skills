#!/usr/bin/env python3
"""Install, inspect or remove the worktree-per-change guard.

The guard is three hook registrations pointing at one script. `--repo <path>` is the
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

Usage:
    python install.py --status                what is installed, and where the work is
    python install.py --repo . --dry-run      show the exact settings.json changes
    python install.py --repo .                install into this repository, committed
    python install.py --repo . --branch queue  ... integrating through `queue`
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
LAND_SOURCE = Path(__file__).resolve().parent / "land.py"
LAND_FILENAME = "land.py"
CONFIG_FILENAME = "worktree-per-change.json"
DEFAULT_BRANCH = "development"

# The invocation the allowlist entry below matches, character for character. Permission
# rules are prefix matches on the command string, so the rule and the call have to be
# spelled the same way — a relative path from the worktree root, which is where the
# session's cwd already is, and the same on every platform.
LAND_COMMAND = f"python .claude/scripts/{LAND_FILENAME}"

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
DELIVERY = [
    f"{LAND_COMMAND}:*",
    "git add:*", "git commit:*", "git push -u origin HEAD:*",
    "git switch -c:*", "git worktree add:*", "git worktree remove:*",
    "git worktree prune:*", "git branch -D:*",
]


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
STATE_DIRNAME = "claude-worktree-gate"

# Guards this one supersedes. A repo that ships one of these is mid-migration, not
# doubly protected — see references/replacing-a-concurrent-writer-guard.md.
LEGACY_GUARD = re.compile(r"concurrent[-_]?writer|writer[-_]?guard|parallel[-_]?guard", re.I)

SKILL_SOURCE = GUARD_SOURCE.parent.parent
SKILL_NAME = SKILL_SOURCE.name


def settings_path(root: Path) -> Path:
    return root / "settings.json"


def guard_path(root: Path) -> Path:
    return root / "hooks" / GUARD_FILENAME


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


def drop_permissions(settings: dict, entries: list[str]) -> dict:
    block = settings.get("permissions")
    if not isinstance(block, dict) or not isinstance(block.get("allow"), list):
        return settings
    ours = set(entries)
    block["allow"] = [rule for rule in block["allow"] if rule not in ours]
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
    """
    if given:
        return given
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
    blob = hook_text(hook)
    return GUARD_FILENAME in blob or "worktree_guard.py" in blob


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


def entry(interpreter: str, script: str, event: str) -> dict:
    # `-S` skips site initialisation, ~13% of interpreter startup and worth having on a
    # hook that runs before every write-tool call. The guard is stdlib-only by design so
    # that it can.
    hook = {"type": "command", "command": interpreter, "args": ["-S", script], "timeout": 10}
    if event == "PreToolUse":
        hook["statusMessage"] = "Checking this change is in its own worktree"
        return {"matcher": MATCHER, "hooks": [hook]}
    if event == "Stop":
        hook["timeout"] = 20  # It shells out to git; only here, and only once per stop.
    return {"hooks": [hook]}


def add_ours(settings: dict, interpreter: str, script: str) -> dict:
    hooks = settings.setdefault("hooks", {})
    for event in EVENTS:
        bucket = hooks.setdefault(event, [])
        if not isinstance(bucket, list):
            hooks[event] = bucket = []
        bucket.append(entry(interpreter, script, event))
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


def report_status(user_root: Path, repo: Path | None) -> int:
    for label, root in (("user", user_root), ("repo", (repo / ".claude") if repo else None)):
        if root is None:
            continue
        settings = load(settings_path(root))
        events, legacy = [], []
        for event, matchers in (settings.get("hooks") or {}).items():
            for matcher in matchers if isinstance(matchers, list) else []:
                for hook in (matcher or {}).get("hooks") or []:
                    if isinstance(hook, dict) and is_ours(hook):
                        events.append(event)
                    elif isinstance(hook, dict) and is_legacy(hook):
                        legacy.append(event)
        state = f"installed ({', '.join(sorted(set(events)))})" if events else "not installed"
        print(f"{label:5} {settings_path(root)}  ->  {state}")
        if legacy:
            print(f"      ! a predecessor guard is still registered ({', '.join(sorted(set(legacy)))})")

    print(f"\nmode: {os.environ.get('CLAUDE_WORKTREE_GATE') or 'on (default)'}")

    located = find_tree(Path.cwd())
    if located is None:
        print("cwd is not a git repository — the guard stands down here.")
        return 0
    tree, git_dir, linked = located
    common = git_dir
    pointer = git_dir / "commondir"
    if pointer.is_file():
        target = Path(pointer.read_text(encoding="utf-8").strip())
        common = Path(os.path.normpath(str(git_dir / target if not target.is_absolute() else target)))
    main_root = common.parent if common.name == ".git" else tree

    branch = os.environ.get("CLAUDE_INTEGRATION_BRANCH") or (
        load(main_root / ".claude" / CONFIG_FILENAME).get("integrationBranch") or DEFAULT_BRANCH
    )
    print(f"\nrepository: {main_root}")
    print(f"integrates through: {branch}")
    print(f"cwd is: {'a worktree — writes allowed' if linked else 'the MAIN CHECKOUT — writes denied'}")

    listing = git(main_root, "worktree", "list", "--porcelain") or ""
    spent_dir = common / STATE_DIRNAME / "spent"
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
    args = parser.parse_args()

    try:  # Windows consoles default to a codepage that mangles the report's punctuation.
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass

    user_root = Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude"))
    repo = Path(args.repo).resolve() if args.repo else None
    root = (repo / ".claude") if repo else user_root

    if args.status:
        return report_status(user_root, repo)

    if args.permissions_only:
        # The read-only half, and nothing else. This is the user-scope install that
        # actually makes sense: a repo-scoped rule cannot cover a session that has to read
        # a *different* repository, and installing this guard into another repo is exactly
        # that shape of task. The hooks stay per repo, because the rule they enforce does.
        entries = [rule(command) for command in READ_ONLY]
        if repo:
            entries += [rule(command) for command in DELIVERY]
        target = settings_path(root)
        settings = load(target)
        settings, granted = add_permissions(settings, entries)
        if args.dry_run:
            print(f"would allow {granted} command(s) in {target}\n"
                  + "\n".join(f"  {r}" for r in entries))
            return 0
        write_json(target, settings, backup=repo is None)
        print(f"allow   -> {target} ({granted} added, {len(entries) - granted} already there)")
        print("Restart or /reload any running sessions for the rules to take effect.")
        return 0

    if not GUARD_SOURCE.is_file():
        print(f"guard script missing: {GUARD_SOURCE}", file=sys.stderr)
        return 1

    target = settings_path(root)
    settings = load(target)
    before = json.dumps(settings, indent=2)
    settings, removed = strip(settings, also_legacy=not args.keep_legacy)

    if args.uninstall:
        # Only the entries this installer added, and only by exact match. An operator who
        # allowed something else, or narrowed one of ours by hand, has made a decision;
        # an uninstaller that took the whole block would silently reverse it.
        settings = drop_permissions(settings, [rule(c) for c in READ_ONLY + DELIVERY])
        after = json.dumps(settings, indent=2)
        if args.dry_run:
            print(f"would rewrite {target}\n--- before\n{before}\n--- after\n{after}")
            return 0
        # No `.bak` beside a committed settings file: git is already the backup, and the
        # stray file shows up in `git status` for whoever installs next.
        write_json(target, settings, backup=repo is None)
        for path in (guard_path(root), land_path(root),
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
        return 0

    script = guard_path(root)
    # A repo install is committed and read on other machines and in every worktree, so it
    # must not carry this machine's interpreter path or this checkout's absolute location:
    # `${CLAUDE_PROJECT_DIR}` resolves to whichever tree the session is actually in, and
    # `python` resolves to whatever that machine has. A user-scope install is the opposite
    # case — it is nobody else's file and there is no project dir to expand — so it pins
    # the interpreter that ran the installer.
    interpreter = args.python or ("python" if repo else sys.executable)
    reference = "${CLAUDE_PROJECT_DIR}/.claude/hooks/" + GUARD_FILENAME if repo else str(script)
    settings = add_ours(settings, interpreter, reference)
    # A user-scope install gets the read-only entries and not the delivery ones. The
    # delivery entries are safe *because the guard scopes them* — `git commit:*` is
    # bounded by a hook that denies it outside a worktree — and at user scope they would
    # apply to repositories that have no such hook.
    wanted = [] if args.no_permissions else [
        rule(command) for command in (READ_ONLY + DELIVERY if repo else READ_ONLY)
    ]
    settings, granted = add_permissions(settings, wanted)
    after = json.dumps(settings, indent=2)
    branch = choose_branch(repo, args.branch)
    config = (repo / ".claude" / CONFIG_FILENAME) if repo else None
    lander = land_path(root) if repo else None

    if args.dry_run:
        print(f"would copy  {GUARD_SOURCE}\n        ->  {script}")
        if lander is not None:
            print(f"would copy  {LAND_SOURCE}\n        ->  {lander}")
        if granted:
            print(f"would allow {granted} command(s) in {target} (permissions.allow)")
        if config is not None:
            # The hash is of the file that WOULD be copied, so the dry run shows the record
            # the real run will write rather than a placeholder for it.
            print(f"would write {config}  ->  integrationBranch = {branch}, "
                  f"guard = {json.dumps(provenance(GUARD_SOURCE))}")
        for line in removed:
            print(f"would remove predecessor guard  {line}")
        if not args.no_skill:
            print(link_skill(user_root, dry_run=True))
        print(f"would rewrite {target}\n--- before\n{before}\n--- after\n{after}")
        return 0

    script.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(GUARD_SOURCE, script)
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
        blob["guard"] = provenance(script)
        if lander is not None and lander.is_file():
            # Recorded for the same reason the guard's copy is: it is committed, so it is
            # a fork the moment this skill moves, and a repo's gate can only ask whether
            # it has drifted if something wrote down where it came from.
            blob["land"] = provenance(lander, LAND_SOURCE)
        write_json(config, blob, backup=False)
        synced = blob["guard"].get("syncedFrom")
        print(f"config  -> {config} (integrationBranch = {branch}"
              f"{', syncedFrom ' + synced[:12] if synced else ''})")
    for line in removed:
        print(f"removed predecessor guard  {line}")
    # Same rule as the uninstall path, and it was missing here: no `.bak` beside a
    # *committed* settings file. Git is already the backup, and the stray file lands in the
    # next person's `git status` — measured 2026-08-15 on a resync, where it showed up
    # untracked beside the change it was supposed to be protecting. A user-scope settings
    # file has no git behind it, so that one is still copied first.
    write_json(target, settings, backup=repo is None)
    print(f"guard   -> {script}")
    if lander is not None:
        print(f"land    -> {lander} (run it as `{LAND_COMMAND}` from a worktree)")
    if not args.no_skill:
        print(link_skill(user_root, dry_run=False))
    print(f"hooks   -> {target} ({', '.join(EVENTS)})")
    if granted:
        scope = "read-only git/gh, and the protocol's own writes" if repo else "read-only git/gh"
        print(f"allow   -> {target} ({granted} command(s): {scope})")
    print("Restart or /reload any running sessions for the hooks to take effect.")
    if repo:
        files = [target, script, config] + ([lander] if lander is not None else [])
        print("Commit " + ", ".join(str(p.relative_to(repo)) for p in files)
              + " for everyone working here to get it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
