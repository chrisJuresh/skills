# Replacing a repo's own concurrent-writer guard

Some repositories already enforce a weaker version of this rule with a committed
`PreToolUse` hook: they let sessions share the main checkout and deny only the
silently-destructive moves — a branch switch under another writer, a blind `git add -A`, two
sessions on one file.

Those guards and this one cannot both run. They disagree about the base case, not the
details: one permits main-checkout writes and denies the dangerous ones, the other denies
main-checkout writes outright. Leaving both registered gives one action two denials with two
different remedies, which is the flail every guard in this family exists to prevent. So
`install.py` **removes** a predecessor from the settings file it is writing, prints what it
removed, and `--keep-legacy` opts out if you want to own that problem deliberately.

A predecessor is recognised by `concurrent-writer`, `writer-guard` or `parallel-guard`
appearing in a hook command — the separator is optional, so `concurrent_writer_guard.py`
matches too.

## Migrating one

1. **Read the old hook before deleting it.** Not to keep it — to find what the repo learned.
   A guard written *after* the convention alone failed encodes a real incident, and the
   remedy text usually names repo-specific machinery: a `scripts/new-worktree.sh` that
   branches off the integration branch, a `.worktreeinclude` of untracked files a fresh tree
   needs, a dependency install that has to run. Every one of those is still needed under this
   protocol — more often, in fact, because now *every* change takes a worktree. Fold them
   into the repo's own docs, and into `.claude/worktree-per-change.json` where they are
   configuration.

2. **Install with the repo's real integration branch.** Not its default branch — check what
   its PRs actually target:

   ```bash
   python scripts/install.py --repo . --branch <integration> --dry-run
   ```

   Then without `--dry-run`. The old hook script is left on disk; delete it, or it sits there
   looking authoritative.

3. **Create the integration branch on the remote if it does not exist yet.** A repo moving
   from "branch off `main`, PR into `main`" to a `development` integration branch needs that
   branch to exist before the first PR, and everyone working in the repo needs telling — it
   changes what their PRs target too.

4. **Rewrite the prose.** Whatever `CLAUDE.md` / `AGENTS.md` says about concurrent writers is
   now wrong in its base case: it will say working in place is the common case, that a
   worktree is for when contention is detected, and that a worktree is taken on evidence.
   Under this protocol there is no evidence to gather. Replace the whole section rather than
   patching sentences — a doc asserting a contract nothing holds is how the original failures
   happened. Check the env var name too: it becomes `CLAUDE_WORKTREE_GATE`.

5. **Check the ticket workflow.** Repos with an issue tracker usually document a
   claim → branch → build → PR sequence, and often "agents never merge". That last line is
   the one this protocol changes, so change it explicitly rather than leaving two documents
   disagreeing about who merges.

6. **Verify.**

   ```bash
   python scripts/test_guard.py && python scripts/install.py --status
   ```

   `--status` should report the repo install, the right integration branch, no predecessor
   still registered, and — from the main checkout — `writes denied`.

Old registry files from the predecessor live inside `.git` and are inert. Anything in there
can never dirty `git status`, so leave them or delete them as you prefer.

## What you gain and what you give up

Gained: the main checkout is always clean and always on the integration branch, so the
operator's editor and dev server show what landed rather than someone's work in progress;
every change is a reviewable PR; and no two sessions can ever be in one directory, which
retires the whole class of failure the old guard could only narrow.

Given up: the cheap in-place fix. Under the old rule a one-line change in an uncontended
checkout cost nothing; here it costs a worktree, a branch, a push and a PR. That is the
trade, and it is worth stating plainly to whoever owns the repo rather than discovering it
on the first typo. `CLAUDE_WORKTREE_GATE=warn` runs the guard in report-only mode for a few
days if the repo wants to see the friction before committing to it.
