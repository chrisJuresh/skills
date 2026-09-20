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
import os
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


def record_branch(
    repo: Path,
    branch: str,
    protected: list[str] | None = None,
    require_issue: bool | None = None,
) -> None:
    config = repo / ".claude" / "worktree-per-change.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    blob: dict = {"integrationBranch": branch}
    if protected is not None:
        blob["protectedMergeTargets"] = protected
    if require_issue is not None:
        blob["requireIssueReference"] = require_issue
    config.write_text(json.dumps(blob), encoding="utf-8")


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

        # A title with no body file still gets `--fill`, which is where the body comes
        # from; gh lets the explicit title win. Without it the command carries neither a
        # body nor a way to derive one, and gh refuses that non-interactively — after the
        # push, so the failure arrives at the forge and reads as gh's problem.
        titled = land(dirty, "--dry-run", "--title", "A title")
        check("a title alone still fills the body", "--fill" in titled.stdout, True)
        check("and keeps the explicit title", "--title A title" in titled.stdout, True)
        # A body file replaces `--fill` rather than joining it.
        filed = land(dirty, "--dry-run", "--body-file", "body.md")
        check("a body file is used instead of --fill", "--fill" in filed.stdout, False)
        check("and is passed to gh", "--body-file body.md" in filed.stdout, True)
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

        # --- a protected target is pushed and opened, never merged -----------------
        # The point of the whole script is that delivery happens unasked, so the refusal
        # is placed at the merge step rather than in preflight: refusing at the top would
        # abort before the push and leave the change undelivered, which is the failure
        # this script exists to prevent. Push and open still happen; only the merge does
        # not, and the exit code says success because the change IS delivered.
        guarded = repo_with_commit(root, "guarded", "develop")
        record_branch(guarded, "develop", protected=["develop"])
        git(guarded, "worktree", "add", "-q", "-b", "feature", str(root / "wt-guarded"), "develop")
        gtree = root / "wt-guarded"
        (gtree / "f.txt").write_text("work\n", encoding="utf-8")
        git(gtree, "add", "f.txt")
        git(gtree, "commit", "-qm", "work")

        out = land(gtree, "--dry-run")
        check("a protected target still succeeds", out.returncode, 0)
        check("the header says it will not merge", "merge:       NO" in out.stdout, True)
        check("the push still runs", "git push -u origin HEAD" in out.stdout, True)
        check("the PR is still opened", "gh pr create --base develop" in out.stdout, True)
        check("the merge is refused", "NOT MERGING" in out.stdout, True)
        check("and gh pr merge is never printed", "gh pr merge" in out.stdout, False)
        # Deleting the branch would close the head of a PR nobody has read yet.
        check("the branch is not deleted", "--delete origin" in out.stdout, False)
        check("it is not reported as a failure", "refused" in out.stderr, False)

        # Matched on the bare name, so neither case nor an `origin/` qualifier is a way
        # past it — both are plausible values for `CLAUDE_INTEGRATION_BRANCH`.
        for spelling in ("Develop", "origin/develop", "refs/heads/develop"):
            env = dict(os.environ, CLAUDE_INTEGRATION_BRANCH=spelling)
            out = subprocess.run(
                [sys.executable, str(LAND), "--dry-run"],
                cwd=str(gtree), capture_output=True, text=True, env=env,
            )
            check(f"`{spelling}` is still protected", "NOT MERGING" in out.stdout, True)

        # The override cannot switch the protection OFF either: pointing it at a branch
        # this repo does not protect is allowed, which is what makes the batch flow work,
        # but `develop` stays refused however it is spelled (checked just above).
        env = dict(os.environ, CLAUDE_INTEGRATION_BRANCH="ENS-1-batch")
        out = subprocess.run(
            [sys.executable, str(LAND), "--dry-run"],
            cwd=str(gtree), capture_output=True, text=True, env=env,
        )
        check("an unprotected batch branch still merges", "gh pr merge" in out.stdout, True)

        # --- the default is empty, so existing repositories are unaffected ----------
        # Most repositories integrate through `development` or `main` and squash-merging
        # into it IS the protocol. A default list here would break every one of them.
        plain = repo_with_commit(root, "unprotected", "main")
        record_branch(plain, "main")
        git(plain, "worktree", "add", "-q", "-b", "t2", str(root / "wt-plain"), "main")
        ptree = root / "wt-plain"
        (ptree / "g.txt").write_text("work\n", encoding="utf-8")
        git(ptree, "add", "g.txt")
        git(ptree, "commit", "-qm", "work")
        out = land(ptree, "--dry-run")
        check("an unconfigured repo merges into main as before", "gh pr merge" in out.stdout, True)
        check("and says nothing about protection", "NOT MERGING" in out.stdout, False)

        # --- a pull request that would close no issue -------------------------------
        # Off unless the repository asks, and when it does, the body is read by the same
        # route the PR will get it: `--fill` means the branch's commit messages. The
        # incident behind it is a PR that named its issue in the TITLE, which no forge
        # acts on, leaving the issue open and the next session rebuilding landed work.
        tickets = repo_with_commit(root, "tickets", "main")
        record_branch(tickets, "main", require_issue=True)
        git(tickets, "worktree", "add", "-q", "-b", "silent", str(root / "wt-silent"), "main")
        silent = root / "wt-silent"
        (silent / "a.txt").write_text("work\n", encoding="utf-8")
        git(silent, "add", "a.txt")
        git(silent, "commit", "-qm", "Resolve step (#4)")
        out = land(silent, "--dry-run")
        check("a body that closes nothing is refused", out.returncode, 2)
        check("and the keyword is spelled out", "Closes #<n>." in out.stderr, True)
        check("and the title is named as not enough", "TITLE is not read" in out.stderr, True)
        check("and the escape hatch is offered", "No issue:" in out.stderr, True)

        # The keyword in the commit message is what `--fill` will carry into the body.
        git(silent, "commit", "-q", "--amend", "-m", "Resolve step\n\nCloses #4.")
        out = land(silent, "--dry-run")
        check("a commit that closes an issue is accepted", out.returncode, 0)

        # A change that genuinely closes nothing says so, with a reason.
        git(tickets, "worktree", "add", "-q", "-b", "chore", str(root / "wt-chore"), "main")
        chore = root / "wt-chore"
        (chore / "b.txt").write_text("bump\n", encoding="utf-8")
        git(chore, "add", "b.txt")
        git(chore, "commit", "-qm", "Bump the pin\n\nNo issue: upstream resync.")
        check("`No issue: <why>` passes", land(chore, "--dry-run").returncode, 0)

        # ... but the reason is the point of it, so the bare words are not a way past.
        git(chore, "commit", "-q", "--amend", "-m", "Bump the pin\n\nNo issue")
        check("`No issue` with no reason does not", land(chore, "--dry-run").returncode, 2)

        # An unreadable body file is let through rather than guessed at: refusing to land
        # a delivered change over a file this script failed to open is the worse error.
        out = land(chore, "--dry-run", "--title", "T", "--body-file", "nowhere.md")
        check("an unreadable body file is let through", out.returncode, 0)

        # And a repository that has not asked is untouched by any of it.
        git(repo, "worktree", "add", "-q", "-b", "quiet", str(root / "wt-quiet"), "queue")
        quiet = root / "wt-quiet"
        (quiet / "c.txt").write_text("work\n", encoding="utf-8")
        git(quiet, "add", "c.txt")
        git(quiet, "commit", "-qm", "no keyword anywhere")
        check("an unconfigured repo is not asked for an issue",
              land(quiet, "--dry-run").returncode, 0)

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
