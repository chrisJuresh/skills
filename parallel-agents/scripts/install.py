#!/usr/bin/env python3
"""Install, inspect or remove the parallel-agents guard.

The guard is three hook registrations pointing at one script. Installing it at user
scope (`~/.claude/`) is the default because the problem it solves is not a property
of any one repository — it is a property of you having two sessions open — and a
per-repo install would have to be repeated for every checkout you ever work in.

`--repo <path>` writes the same thing into a repository's committed
`.claude/settings.json` instead, for the case where the rule has to hold for
teammates too and not just for you.

Usage:
    python install.py --status            what is installed, and who is in this repo
    python install.py --dry-run           show the exact settings.json changes
    python install.py                     install at user scope
    python install.py --repo .            install into this repository, committed
    python install.py --uninstall         remove (add --repo to remove a repo install)
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

GUARD_SOURCE = Path(__file__).resolve().parent / "parallel_guard.py"
GUARD_FILENAME = "parallel-guard.py"
MATCHER = "Write|Edit|NotebookEdit|Bash|PowerShell"
EVENTS = ("PreToolUse", "SessionStart", "SessionEnd")
REGISTRY_DIRNAME = "claude-parallel-sessions"


SKILL_SOURCE = GUARD_SOURCE.parent.parent
SKILL_NAME = SKILL_SOURCE.name


def settings_path(root: Path) -> Path:
    return root / "settings.json"


def link_skill(user_root: Path, dry_run: bool) -> str:
    """Make `/parallel-agents` resolve in every repo.

    The guard's denials point at this skill by name, so a machine with the hook and
    without the skill hands an agent a dead reference at exactly the moment it needs
    the protocol. A link rather than a copy, so editing the source keeps working:
    Claude Code follows a symlinked skill directory and reads `SKILL.md` from the
    target. On Windows a directory junction is the one form that needs no privilege.
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
        import subprocess

        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(destination), str(SKILL_SOURCE)],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return f"skill   -> {destination} (junction to {SKILL_SOURCE})"
    shutil.copytree(SKILL_SOURCE, destination)
    return f"skill   -> {destination} (copy — re-run install after editing the source)"


def guard_path(root: Path) -> Path:
    return root / "hooks" / GUARD_FILENAME


def is_ours(hook: dict) -> bool:
    blob = " ".join(str(p) for p in (hook.get("command"), *(hook.get("args") or [])) if p)
    return GUARD_FILENAME in blob or "parallel_guard.py" in blob


def load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def strip_ours(settings: dict) -> dict:
    """Remove every registration of this guard, leaving other hooks untouched."""
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return settings
    for event in list(hooks):
        matchers = hooks.get(event)
        if not isinstance(matchers, list):
            continue
        kept = []
        for matcher in matchers:
            if not isinstance(matcher, dict):
                kept.append(matcher)
                continue
            inner = [h for h in (matcher.get("hooks") or []) if not (isinstance(h, dict) and is_ours(h))]
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
    return settings


def entry(interpreter: str, script: Path, event: str) -> dict:
    # `-S` skips site initialisation, which is ~13% of the interpreter's startup and
    # buys nothing here: the guard is stdlib-only by design, precisely so it can run
    # on every write-tool call without anyone noticing.
    hook = {
        "type": "command",
        "command": interpreter,
        "args": ["-S", str(script)],
        "timeout": 10,
    }
    if event == "PreToolUse":
        hook["statusMessage"] = "Checking for other agents in this checkout"
    matcher = {"hooks": [hook]}
    if event == "PreToolUse":
        matcher["matcher"] = MATCHER
    return matcher


def add_ours(settings: dict, interpreter: str, script: Path) -> dict:
    hooks = settings.setdefault("hooks", {})
    for event in EVENTS:
        bucket = hooks.setdefault(event, [])
        if not isinstance(bucket, list):
            hooks[event] = bucket = []
        bucket.append(entry(interpreter, script, event))
    return settings


def write_settings(path: Path, settings: dict, backup: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if backup and path.exists():
        stamp = time.strftime("%Y%m%d-%H%M%S")
        shutil.copy2(path, path.with_suffix(f".json.{stamp}.bak"))
    body = json.dumps(settings, indent=2) + "\n"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(body, encoding="utf-8")
    os.replace(temporary, path)


# ------------------------------------------------------------------------- status


def find_tree(start: Path):
    for directory in [start, *start.parents]:
        marker = directory / ".git"
        try:
            if marker.is_dir():
                return directory, marker
            if marker.is_file():
                text = marker.read_text(encoding="utf-8", errors="replace").strip()
                if text.startswith("gitdir:"):
                    git_dir = Path(text.split(":", 1)[1].strip())
                    if not git_dir.is_absolute():
                        git_dir = directory / git_dir
                    return directory, Path(os.path.normpath(str(git_dir)))
        except OSError:
            return None
    return None


def report_status(user_root: Path, repo: Path | None) -> int:
    for label, root in (("user", user_root), ("repo", (repo / ".claude") if repo else None)):
        if root is None:
            continue
        settings = load(settings_path(root))
        events = []
        for event, matchers in (settings.get("hooks") or {}).items():
            for matcher in matchers if isinstance(matchers, list) else []:
                if any(isinstance(h, dict) and is_ours(h) for h in (matcher or {}).get("hooks") or []):
                    events.append(event)
        state = f"installed ({', '.join(sorted(set(events)))})" if events else "not installed"
        print(f"{label:5} {settings_path(root)}  ->  {state}")

    mode = os.environ.get("CLAUDE_PARALLEL_GUARD") or "balanced (default)"
    print(f"\nmode: {mode}")

    located = find_tree(Path.cwd())
    if located is None:
        print("cwd is not a git repository — the guard stands down here.")
        return 0
    tree, git_dir = located
    pointer = git_dir / "commondir"
    if pointer.is_file():
        target = Path(pointer.read_text(encoding="utf-8").strip())
        git_dir = Path(os.path.normpath(str(git_dir / target if not target.is_absolute() else target)))
    registry = git_dir / REGISTRY_DIRNAME
    print(f"\nrepository: {tree}")

    main_root = git_dir.parent if git_dir.name == ".git" else None
    if main_root:
        for name in ("settings.json", "settings.local.json"):
            blob = load(main_root / ".claude" / name)
            for matchers in (blob.get("hooks") or {}).get("PreToolUse") or []:
                for hook in (matchers or {}).get("hooks") or []:
                    text = " ".join(str(p) for p in (hook.get("command"), *(hook.get("args") or [])) if p)
                    if not is_ours(hook) and ("concurrent" in text or "writer-guard" in text):
                        print(f"  ! this repo ships its own guard ({name}) — ours stands down here")

    now = time.time()
    claims = sorted(registry.glob("*.json")) if registry.is_dir() else []
    if not claims:
        print("  no sessions have claimed this checkout")
        return 0
    print("  sessions that have written here:")
    for path in claims:
        try:
            claim = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        seen = claim.get("last_write_at", 0)
        transcript = claim.get("transcript_path")
        if isinstance(transcript, str) and transcript:
            try:
                seen = max(seen, os.path.getmtime(transcript))
            except OSError:
                pass
        age = int(max(0.0, now - seen) // 60)
        live = "live" if age < 20 else "stale"
        files = len(claim.get("paths") or {})
        print(
            f"    {str(claim.get('session_id'))[:8]}  {live:5}  last active {age}m ago  "
            f"{files} file(s)  tree={claim.get('tree_root')}"
        )
    return 0


# --------------------------------------------------------------------------- main


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", metavar="PATH", help="install into a repository instead of ~/.claude")
    parser.add_argument("--uninstall", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--no-skill",
        action="store_true",
        help="skip linking the skill into ~/.claude/skills (the guard's denials name it)",
    )
    args = parser.parse_args()

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
    settings = strip_ours(settings)

    if args.uninstall:
        after = json.dumps(settings, indent=2)
        if args.dry_run:
            print(f"would rewrite {target}\n--- before\n{before}\n--- after\n{after}")
            return 0
        write_settings(target, settings, backup=True)
        try:
            guard_path(root).unlink()
        except OSError:
            pass
        linked = user_root / "skills" / SKILL_NAME
        if linked.is_symlink() or (os.name == "nt" and linked.is_dir() and not repo):
            try:
                linked.unlink() if linked.is_symlink() else os.rmdir(linked)
                print(f"unlinked {linked}")
            except OSError:
                print(f"leave {linked} in place — remove it by hand if you want it gone")
        print(f"removed the guard from {target}")
        print("Existing claim files live in <repo>/.git/claude-parallel-sessions/ and are inert; "
              "delete them if you want the directory gone.")
        return 0

    script = guard_path(root)
    settings = add_ours(settings, sys.executable, script)
    after = json.dumps(settings, indent=2)

    if args.dry_run:
        print(f"would copy  {GUARD_SOURCE}\n        ->  {script}")
        if not args.no_skill:
            print(link_skill(user_root, dry_run=True))
        print(f"would rewrite {target}\n--- before\n{before}\n--- after\n{after}")
        return 0

    script.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(GUARD_SOURCE, script)
    write_settings(target, settings, backup=True)
    print(f"guard   -> {script}")
    if not args.no_skill:
        print(link_skill(user_root, dry_run=False))
    print(f"hooks   -> {target} (PreToolUse, SessionStart, SessionEnd)")
    print("mode    -> balanced. Set CLAUDE_PARALLEL_GUARD=strict per repo, or =off to disable.")
    print("Restart or /reload any running sessions for the hooks to take effect.")
    if repo:
        print(f"Commit {target.relative_to(repo)} and {script.relative_to(repo)} for teammates to get it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
