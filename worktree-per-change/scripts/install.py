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

Usage:
    python install.py --status                what is installed, and where the work is
    python install.py --repo . --dry-run      show the exact settings.json changes
    python install.py --repo .                install into this repository, committed
    python install.py --repo . --branch main  ... integrating through `main` instead
    python install.py                         install at user scope
    python install.py --repo . --uninstall    remove it
"""

from __future__ import annotations

import argparse
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
CONFIG_FILENAME = "worktree-per-change.json"
DEFAULT_BRANCH = "development"

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
    parser.add_argument("--branch", metavar="NAME", help=f"branch changes merge into (default {DEFAULT_BRANCH})")
    parser.add_argument("--uninstall", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--python", metavar="EXE", help="interpreter to run the guard with")
    parser.add_argument("--keep-legacy", action="store_true", help="leave a predecessor guard registered")
    parser.add_argument("--no-skill", action="store_true", help="skip linking the skill into ~/.claude/skills")
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

    if not GUARD_SOURCE.is_file():
        print(f"guard script missing: {GUARD_SOURCE}", file=sys.stderr)
        return 1

    target = settings_path(root)
    settings = load(target)
    before = json.dumps(settings, indent=2)
    settings, removed = strip(settings, also_legacy=not args.keep_legacy)

    if args.uninstall:
        after = json.dumps(settings, indent=2)
        if args.dry_run:
            print(f"would rewrite {target}\n--- before\n{before}\n--- after\n{after}")
            return 0
        # No `.bak` beside a committed settings file: git is already the backup, and the
        # stray file shows up in `git status` for whoever installs next.
        write_json(target, settings, backup=repo is None)
        for path in (guard_path(root), (repo / ".claude" / CONFIG_FILENAME) if repo else None):
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
    after = json.dumps(settings, indent=2)
    branch = args.branch or DEFAULT_BRANCH
    config = (repo / ".claude" / CONFIG_FILENAME) if repo else None

    if args.dry_run:
        print(f"would copy  {GUARD_SOURCE}\n        ->  {script}")
        if config is not None:
            print(f"would write {config}  ->  integrationBranch = {branch}")
        for line in removed:
            print(f"would remove predecessor guard  {line}")
        if not args.no_skill:
            print(link_skill(user_root, dry_run=True))
        print(f"would rewrite {target}\n--- before\n{before}\n--- after\n{after}")
        return 0

    script.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(GUARD_SOURCE, script)
    if config is not None:
        write_json(config, {"integrationBranch": branch}, backup=False)
        print(f"config  -> {config} (integrationBranch = {branch})")
    for line in removed:
        print(f"removed predecessor guard  {line}")
    write_json(target, settings, backup=True)
    print(f"guard   -> {script}")
    if not args.no_skill:
        print(link_skill(user_root, dry_run=False))
    print(f"hooks   -> {target} ({', '.join(EVENTS)})")
    print("Restart or /reload any running sessions for the hooks to take effect.")
    if repo:
        print(f"Commit {target.relative_to(repo)}, {script.relative_to(repo)} and "
              f"{config.relative_to(repo)} for everyone working here to get it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
