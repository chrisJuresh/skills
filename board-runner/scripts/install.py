#!/usr/bin/env python3
"""Install, inspect or remove the board-runner.

Unlike `worktree-per-change`, this installs nothing *into* a repository. The runner is a
pool of processes that stands outside the repositories it works, so it lives in one
directory of its own — `--dir`, default `~/afk` — holding the four scripts, a
`config.json`, and the logs and state of every run. A repository is *named* in that config
and otherwise untouched.

`--add-repo` reads the repository rather than asking about it: the slug from `origin`, the
integration branch and the delivery command from `.claude/worktree-per-change.json`. The
two skills then agree by construction instead of by being told the same thing twice, which
is the failure mode of writing the branch down in two places.

What it will not do is guess the **epics**. A parent ticket with no blockers and an agent
label is indistinguishable through the API from a small independent job, and the cost of
being wrong is asymmetric: naming a leaf as an epic means it never runs, which is visible;
missing a real epic means an agent tries to implement six tickets in one session, which is
not. `--suggest-epics` prints the candidates and the evidence for each, and leaves the
decision where it belongs.

Usage:
    python install.py --status
    python install.py --dir ~/afk --dry-run
    python install.py --dir ~/afk
    python install.py --dir ~/afk --add-repo ../photos
    python install.py --suggest-epics ../photos
    python install.py --dir ~/afk --uninstall
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL_SOURCE = HERE.parent
SKILL_NAME = SKILL_SOURCE.name
DEFAULT_DIR = Path.home() / "afk"
DEFAULT_DEADLINE = "10:00"

# Copied into the install directory. `gates/` is a directory because a repository may want
# its own predicate beside the one shipped here.
PAYLOAD = ["scheduler.mjs", "tree.mjs", "tree.html"]
PAYLOAD_DIRS = ["gates"]

DEFAULTS = {
    "deadline": DEFAULT_DEADLINE,
    "pollSeconds": 60,
    "jobTimeoutMinutes": 180,
    "retries": 1,
    "agentLabel": "ready-for-agent",
    "humanLabel": "ready-for-human",
    "claimLabel": "agent-running",
    "repos": {},
}


def run(args: list[str], cwd: Path | None = None) -> str:
    return subprocess.run(
        args, cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


def try_run(args: list[str], cwd: Path | None = None) -> str | None:
    try:
        return run(args, cwd)
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


# --------------------------------------------------------------------- detection
def detect_repo(path: Path) -> dict:
    """Everything about a repository that can be read rather than asked."""
    root = try_run(["git", "rev-parse", "--show-toplevel"], path)
    if not root:
        sys.exit(f"not a git repository: {path}")
    root = Path(root)

    url = try_run(["git", "remote", "get-url", "origin"], root) or ""
    m = re.search(r"[:/]([^/:]+/[^/]+?)(?:\.git)?$", url)
    if not m:
        sys.exit(f"cannot read an owner/repo slug from origin: {url!r}")
    slug = m.group(1)

    # The integration branch is the one setting that must not be guessed, and
    # worktree-per-change already recorded it. Fall back to the checked-out branch only
    # when that file is absent, and say so.
    cfg_path = root / ".claude" / "worktree-per-change.json"
    integration, delivery, inferred = None, None, False
    if cfg_path.exists():
        try:
            wpc = json.loads(cfg_path.read_text(encoding="utf-8"))
            integration = wpc.get("integrationBranch")
            delivery = (wpc.get("delivery") or {}).get("command")
        except json.JSONDecodeError:
            pass
    if not integration:
        integration = try_run(["git", "branch", "--show-current"], root) or "main"
        inferred = True

    if delivery:
        teardown = ""
        try:
            teardown = (json.loads(cfg_path.read_text(encoding="utf-8")).get("delivery") or {}).get("teardown") or ""
        except Exception:
            pass
        hint = (
            f"Deliver with `{delivery}`"
            + (f"; take the worktree down with `{teardown}`" if teardown else "")
            + ". This repository lands without pull requests."
        )
    elif (root / ".claude" / "scripts" / "land.py").exists():
        hint = (
            "Land with `python .claude/scripts/land.py --merge-integration` — other agents "
            f"are landing into {integration} at the same time, so the base must come down "
            "before you push."
        )
    else:
        hint = (
            f"Push the branch and open a pull request against `{integration}`, then merge it. "
            "Bring the base down first if anything else has landed."
        )

    return {
        "name": root.name,
        "entry": {
            "cwd": root.as_posix(),
            "slug": slug,
            "integration": integration,
            "concurrency": 2,
            "epics": [],
            "claimed": [],
            "landHint": hint,
        },
        "inferredBranch": inferred,
    }


def suggest_epics(path: Path, agent_label: str) -> int:
    """Print tickets that look like umbrellas. Decide nothing."""
    det = detect_repo(path)
    slug, root = det["entry"]["slug"], Path(det["entry"]["cwd"])
    raw = try_run(
        ["gh", "issue", "list", "--state", "open", "--limit", "200",
         "--label", agent_label, "--json", "number,title,body"], root
    )
    if raw is None:
        sys.exit("could not list issues — is `gh` installed and authenticated?")
    issues = json.loads(raw)

    print(f"{slug}: {len(issues)} open ticket(s) labelled {agent_label}\n")
    found = False
    for it in issues:
        n, body = it["number"], it.get("body") or ""
        blocked = try_run(
            ["gh", "api", f"repos/{slug}/issues/{n}/dependencies/blocked_by",
             "--jq", "[.[]|select(.state==\"open\")]|length"], root
        )
        if blocked not in (None, "0"):
            continue  # something already gates it, so it is not a root
        subs = try_run(
            ["gh", "api", f"repos/{slug}/issues/{n}/sub_issues", "--jq", "length"], root
        )
        why = []
        if subs not in (None, "0"):
            why.append(f"has {subs} sub-issue(s)")
        if re.search(r"^##\s*Problem Statement", body, re.M):
            why.append("opens with a Problem Statement")
        if re.search(r"^##\s*Parent", body, re.M):
            why = []  # names a parent, so it is a child and never an epic
        if why:
            found = True
            print(f"  #{n}  {it['title'][:66]}")
            print(f"        {', '.join(why)}")
    if not found:
        print("  nothing looks like an epic — but read the roots yourself before trusting that.")
    print("\nPut the real ones in config.json under the repo's \"epics\". Nothing was changed.")
    return 0


# ------------------------------------------------------------------- the install
def load_config(target: Path) -> dict:
    p = target / "config.json"
    if not p.exists():
        return json.loads(json.dumps(DEFAULTS))
    cfg = json.loads(p.read_text(encoding="utf-8"))
    # Merge rather than replace, so a re-run keeps every decision this install already
    # holds — the epics above all, which cost human judgement to get right.
    for k, v in DEFAULTS.items():
        cfg.setdefault(k, v)
    return cfg


def install(target: Path, add_repo: Path | None, deadline: str | None,
            link_skill: bool, dry_run: bool) -> int:
    changes: list[str] = []
    cfg = load_config(target)
    if deadline:
        cfg["deadline"] = deadline

    for name in PAYLOAD:
        src, dst = HERE / name, target / name
        state = "update" if dst.exists() else "create"
        if src.read_bytes() != (dst.read_bytes() if dst.exists() else b""):
            changes.append(f"{state}  {dst}")
            if not dry_run:
                target.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)

    for d in PAYLOAD_DIRS:
        for src in sorted((HERE / d).glob("*")):
            if not src.is_file():
                continue
            dst = target / d / src.name
            if src.read_bytes() != (dst.read_bytes() if dst.exists() else b""):
                changes.append(f"{'update' if dst.exists() else 'create'}  {dst}")
                if not dry_run:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)

    if add_repo:
        det = detect_repo(add_repo)
        e = det["entry"]
        cfg["repos"][det["name"]] = {**cfg["repos"].get(det["name"], {}), **e} \
            if det["name"] in cfg["repos"] else e
        changes.append(f"config  repo {det['name']} -> {e['slug']} via {e['integration']}")
        if det["inferredBranch"]:
            changes.append(
                f"        ! integration branch inferred from the checked-out branch "
                f"({e['integration']}). worktree-per-change was not installed there, so "
                f"nothing recorded it — check config.json."
            )
        changes.append(
            f"        ! \"epics\" is empty for {det['name']}. Run "
            f"--suggest-epics {add_repo} and fill it in before the first unattended run."
        )

    cfg_path = target / "config.json"
    new = json.dumps(cfg, indent=2) + "\n"
    if not cfg_path.exists() or cfg_path.read_text(encoding="utf-8") != new:
        changes.append(f"{'update' if cfg_path.exists() else 'create'}  {cfg_path}")
        if not dry_run:
            target.mkdir(parents=True, exist_ok=True)
            cfg_path.write_text(new, encoding="utf-8")

    if not dry_run:
        (target / "logs").mkdir(parents=True, exist_ok=True)

    if link_skill:
        changes.append(link(dry_run))

    print(("would change:" if dry_run else "changed:") if changes else "nothing to do.")
    for c in changes:
        print("  " + c)
    if changes and not dry_run:
        print(f"\n  node {target / 'scheduler.mjs'} --dry-run     # read the board")
        print(f"  node {target / 'tree.mjs'}                     # watch it")
    return 0


def link(dry_run: bool) -> str:
    dest = Path.home() / ".claude" / "skills" / SKILL_NAME
    if dest.is_symlink() or dest.exists():
        return f"skill   {dest} (already linked)"
    if dry_run:
        return f"skill   -> {dest}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.symlink(SKILL_SOURCE, dest, target_is_directory=True)
        return f"skill   -> {dest} (symlink to {SKILL_SOURCE})"
    except OSError:
        # Windows without developer mode refuses symlinks; a copy still resolves the skill.
        shutil.copytree(SKILL_SOURCE, dest)
        return f"skill   -> {dest} (copy — symlink refused; re-copy after an update)"


def status(target: Path) -> int:
    print(f"install dir  {target}  {'(present)' if target.exists() else '(not installed)'}")
    cfg_path = target / "config.json"
    if not cfg_path.exists():
        print("no config.json — nothing configured.")
        return 0
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    print(f"deadline     {cfg.get('deadline')}   agent label: {cfg.get('agentLabel')}")
    for name, r in (cfg.get("repos") or {}).items():
        epics = ", ".join(f"#{e}" for e in r.get("epics") or []) or "NONE SET"
        print(f"\n  {name}  {r.get('slug')}  -> {r.get('integration')}"
              f"  ({r.get('concurrency')} at a time)")
        print(f"    epics    {epics}")
        if r.get("gate"):
            print(f"    gate     {' '.join(r['gate'])}")
        wt = try_run(["git", "worktree", "list"], Path(r["cwd"])) or ""
        standing = [l.split()[0] for l in wt.splitlines() if "worktrees" in l]
        print(f"    worktrees {len(standing)} standing")

    jobs = {}
    for f in sorted(target.glob("state*.json")):
        try:
            jobs.update(json.loads(f.read_text(encoding="utf-8")).get("jobs") or {})
        except json.JSONDecodeError:
            pass
    if jobs:
        print(f"\n  {len(jobs)} job(s) recorded:")
        for k, j in jobs.items():
            print(f"    {j.get('status','?'):12} {k:22} resume: claude --resume {j.get('sessionId')}")
    return 0


def uninstall(target: Path, dry_run: bool) -> int:
    changes = []
    for name in PAYLOAD:
        if (target / name).exists():
            changes.append(f"remove  {target / name}")
            if not dry_run:
                (target / name).unlink()
    for d in PAYLOAD_DIRS:
        if (target / d).exists():
            changes.append(f"remove  {target / d}/")
            if not dry_run:
                shutil.rmtree(target / d)
    dest = Path.home() / ".claude" / "skills" / SKILL_NAME
    if dest.is_symlink() or dest.exists():
        changes.append(f"remove  {dest}")
        if not dry_run:
            dest.unlink() if dest.is_symlink() else shutil.rmtree(dest)
    # config.json, logs and state are the record of what ran. Removing the tool should not
    # remove the evidence, so they stay and are named.
    kept = [p.name for p in (target / "config.json", target / "logs") if p.exists()]
    print(("would change:" if dry_run else "changed:") if changes else "nothing to do.")
    for c in changes:
        print("  " + c)
    if kept:
        print(f"  kept    {', '.join(kept)} in {target} — the record of what ran. Delete by hand.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", type=Path, default=DEFAULT_DIR, help="where the runner lives (default ~/afk)")
    ap.add_argument("--add-repo", type=Path, metavar="PATH", help="detect a repository and add it to config.json")
    ap.add_argument("--suggest-epics", type=Path, metavar="PATH", help="print likely epics for a repository, change nothing")
    ap.add_argument("--deadline", metavar="HH:MM", help=f"wall-clock stop (default {DEFAULT_DEADLINE})")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--no-skill", action="store_true", help="skip the ~/.claude/skills link")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    target = a.dir.expanduser()
    if a.suggest_epics:
        return suggest_epics(a.suggest_epics, load_config(target).get("agentLabel", "ready-for-agent"))
    if a.status:
        return status(target)
    if a.uninstall:
        return uninstall(target, a.dry_run)
    return install(target, a.add_repo, a.deadline, not a.no_skill, a.dry_run)


if __name__ == "__main__":
    sys.exit(main())
