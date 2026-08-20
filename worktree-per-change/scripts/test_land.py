#!/usr/bin/env python3
"""Checks against land.py, run against real repositories in a temp dir.

What this suite is for: `land.py` exists to be allowlisted, which means a human agrees to
it once and then never sees it again. Everything that keeps that safe is a *refusal* —
it merges the PR whose head is the branch in the worktree it was run from, into the branch
that repository recorded, and nothing else. A refusal that quietly stopped refusing would
look exactly like a script that had nothing to refuse.

So the refusals are the tests, and they are asked of real git repositories rather than of
mocks: the one that matters most (main checkout versus worktree) is a question about what
`.git` *is* on disk, and a mock would answer it by agreeing with the code.

The forge is never called. Everything here is either a refusal, which happens before any
network step, or a dry run, which prints the sequence and runs none of it — so this suite
needs `git` and does not need `gh`, an account, or a remote.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
LAND = HERE / "land.py"

PASSED = 0
FAILED: list[str] = []


def check(name: str, got, want) -> None:
    global PASSED
    if got == want:
        PASSED += 1
    else:
        FAILED.append(f"{name}: expected {want!r}, got {got!r}")


def land(cwd: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(LAND), *extra], cwd=str(cwd), capture_output=True, text=True
    )


def git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)


def repo_with_commit(root: Path, name: str, branch: str) -> Path:
    repo = root / name
    repo.mkdir(parents=True)
    git(repo.parent, "init", "-q", "-b", branch, str(repo))
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "t")
    (repo / "README.md").write_text("seed\n", encoding="utf-8")
    git(repo, "add", "README.md")
    git(repo, "commit", "-qm", "seed")
    return repo


def record_branch(repo: Path, branch: str) -> None:
    config = repo / ".claude" / "worktree-per-change.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(json.dumps({"integrationBranch": branch}), encoding="utf-8")


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)

        # --- the main checkout is refused ------------------------------------------
        # The first and most important refusal, and the one a wrapper around `gh pr merge`
        # would not make: landing from the main checkout would push whatever branch that
        # directory happens to be sitting on, which under this protocol is the integration
        # branch itself.
        repo = repo_with_commit(root, "main-checkout", "queue")
        record_branch(repo, "queue")
        out = land(repo)
        check("the main checkout is refused", out.returncode, 2)
        check("and is named as the reason", "MAIN CHECKOUT" in out.stderr, True)

        # --- a worktree sitting on the integration branch is refused ---------------
        # A PR from `queue` into `queue` is not a change, and the guard denies edits in
        # such a tree for the same reason. Refusing here keeps the two agreeing.
        git(repo, "worktree", "add", "-q", str(root / "wt-on-queue"), "--detach")
        on_queue = root / "wt-on-queue"
        git(on_queue, "switch", "-q", "queue")
        out = land(on_queue)
        check("a worktree on the integration branch is refused", out.returncode, 2)
        check("and says to cut a topic branch", "switch -c" in out.stderr, True)

        # --- uncommitted work is refused -------------------------------------------
        # Landing deletes the branch. Anything uncommitted at that moment is work about to
        # be stranded in a directory whose branch no longer exists, so this refuses rather
        # than sweeping it up — the same reason the protocol never says `git add -A`.
        git(repo, "worktree", "add", "-q", "-b", "topic", str(root / "wt-dirty"), "queue")
        dirty = root / "wt-dirty"
        (dirty / "scratch.txt").write_text("half-finished\n", encoding="utf-8")
        out = land(dirty)
        check("an uncommitted change is refused", out.returncode, 2)
        check("and the file is named", "scratch.txt" in out.stderr, True)

        # --- a clean topic branch reaches the sequence ------------------------------
        git(dirty, "add", "scratch.txt")
        git(dirty, "commit", "-qm", "finish it")
        out = land(dirty, "--dry-run")
        check("a clean topic branch is accepted", out.returncode, 0)
        check("it reads the branch from the repository", "topic  ->  queue" in out.stdout, True)

        # The sequence is printed in full, because an allowlisted script that is not
        # watched is one whose transcript is the only record of what it did.
        for step in ("git push -u origin HEAD", "gh pr create --base queue",
                     "gh pr merge", "--squash", "git push origin --delete topic"):
            check(f"the dry run shows `{step}`", step in out.stdout, True)
        check("the dry run says it did nothing", "nothing was pushed" in out.stdout, True)

        # `--delete-branch` is deliberately absent, and this is the check that keeps it
        # absent. It makes `gh` do local git work after the API call — it checks out the
        # base branch in order to delete the merged one — and under this protocol the main
        # checkout is permanently sitting on the base, so it always fails, *after* the
        # merge has already happened. Measured 2026-08-15 landing this script's own first
        # change: `fatal: 'main' is already used by worktree at ...`, exit 1, and a MERGED
        # pull request with its branch still standing.
        check("it does not ask gh to delete the branch",
              "--delete-branch" in out.stdout, False)

        # It never learns a PR number from its arguments — that is what keeps one
        # allowlist entry from being a grant over every PR on the machine.
        check("it takes no PR number", "--pr" in (land(dirty, "--help").stdout or ""), False)

        # --- the branch comes from the repository, not from a default ---------------
        # A repo that records `main` must not be landed into `development` because that is
        # what the guard falls back to. The record is the answer.
        other = repo_with_commit(root, "records-main", "main")
        record_branch(other, "main")
        git(other, "worktree", "add", "-q", "-b", "fix", str(root / "wt-main"), "main")
        out = land(root / "wt-main", "--dry-run")
        check("it targets the recorded branch", "fix  ->  main" in out.stdout, True)
        check("and opens the PR against it", "--base main" in out.stdout, True)

        # --- merging the integration branch down ------------------------------------
        # Off unless asked for, because in a repo where changes land one at a time it is a
        # fetch and a merge commit that buy nothing. The default has to be *provable*: this
        # is the flag whose accidental arrival would change what every existing consumer's
        # `land.py` does on the happy path.
        out = land(root / "wt-main", "--dry-run")
        check("the merge-down is off by default", "merge main down first" in out.stdout, False)
        out = land(root / "wt-main", "--dry-run", "--merge-integration")
        check("the flag turns it on", "merge main down first" in out.stdout, True)
        check("and it runs before the push",
              out.stdout.index("merge main down first") < out.stdout.index("push"), True)

        recorded = repo_with_commit(root, "records-merge", "main")
        config = recorded / ".claude" / "worktree-per-change.json"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(
            json.dumps({"integrationBranch": "main", "mergeIntegrationBeforeLanding": True}),
            encoding="utf-8",
        )
        git(recorded, "worktree", "add", "-q", "-b", "topic", str(root / "wt-merge"), "main")
        out = land(root / "wt-merge", "--dry-run")
        check("the repo can record it instead", "merge main down first" in out.stdout, True)
        out = land(root / "wt-merge", "--dry-run", "--no-merge-integration")
        check("and one run can still opt out", "merge main down first" in out.stdout, False)

        # A real conflict, resolved by nobody: the point of doing this locally is that the
        # refusal arrives with the conflict in the tree and before anything is pushed. A
        # script that resolved it would be landing a guess about somebody's code.
        conflicted = repo_with_commit(root, "conflicting", "main")
        config = conflicted / ".claude" / "worktree-per-change.json"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(json.dumps({"integrationBranch": "main"}), encoding="utf-8")
        git(conflicted, "worktree", "add", "-q", "-b", "mine", str(root / "wt-conflict"), "main")
        mine = root / "wt-conflict"
        (mine / "README.md").write_text("mine\n", encoding="utf-8")
        git(mine, "commit", "-qam", "mine")
        # `main` moves under it, touching the same line. `origin` is the repo itself, so
        # the fetch has something real to fetch and no network is involved.
        (conflicted / "README.md").write_text("theirs\n", encoding="utf-8")
        git(conflicted, "commit", "-qam", "theirs")
        git(mine, "remote", "add", "origin", str(conflicted))
        out = land(mine, "--merge-integration")
        check("a conflicting merge-down refuses", out.returncode, 2)
        check("and says the base moved", "has moved since this branch was cut" in out.stderr, True)
        check("and says nothing was pushed", "Nothing has been pushed" in out.stderr, True)
        check("and leaves the conflict in the tree to be resolved",
              "<<<<<<<" in (mine / "README.md").read_text(encoding="utf-8"), True)

        # Rerun after a resolution: the second attempt gets past the merge step, which is
        # the half of the design that makes the refusal cheap rather than terminal.
        (mine / "README.md").write_text("settled\n", encoding="utf-8")
        git(mine, "add", "README.md")
        git(mine, "commit", "-qm", "resolve")
        out = land(mine, "--dry-run", "--merge-integration")
        check("and a resolved tree gets through it", "already contains origin/main" in out.stdout, True)

        # --- a tree outside a repository -------------------------------------------
        loose = root / "not-a-repo"
        loose.mkdir()
        out = land(loose)
        check("a directory outside a repository is refused", out.returncode, 2)

    print(f"{PASSED} passed, {len(FAILED)} failed")
    for line in FAILED:
        print(f"  FAIL  {line}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    if shutil.which("git") is None:
        print("git is not on PATH")
        raise SystemExit(1)
    raise SystemExit(main())
