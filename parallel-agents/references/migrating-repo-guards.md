# Repos that already ship a concurrent-writer guard

Some repositories enforce this rule themselves, with a committed `PreToolUse` hook. The
global guard detects those and exits silently, so nothing here is urgent — this page is for
deciding whether to consolidate, and what it would cost.

A repo guard is recognised by `concurrent-writer`, `writer-guard` or `parallel-guard`
appearing in a hook command in the repo's `.claude/settings.json` or
`.claude/settings.local.json`. The separator is not load-bearing — a hyphen, an underscore
or nothing all match, so a repo whose hook is `concurrent_writer_guard.py` is recognised —
but the words are: a command mentioning `concurrent` alone is not a guard, and does not stand
anything down. `install.py --status` applies the same test, so if it does not report a repo
guard, the guard is not standing down either. To see what a given repo has, read that file
and the script it points at, and note four things before deciding anything:

| What to look for | Why it matters |
|---|---|
| **Registry shape** — one file per session, or one shared file every session read-modify-writes | A single shared file is the exact lost-update race the guard exists to prevent. One file per session cannot lose an update. |
| **Staleness window** — how long a claim survives without activity | Too short hands a checkout away mid-task; too long strands the next writer. |
| **Arbitration** — earliest live claim owns the tree, or first claim wins until it expires | Decides who gets denied when two sessions arrive together. |
| **The rule** — deny every write landing in the main checkout (strict), or deny only the silently-destructive ones (balanced) | Strict is right where a human is often in an editor at the same time; it denies agent-team workflows by design. |

A committed guard written *after* the convention alone failed is evidence for keeping it,
not against. That history is the strongest argument in this whole page.

## What the global guard does that a typical repo guard does not

Check each of these against the repo's own hook rather than assuming:

- **Same-file collision.** Most repo guards reason entirely about the index and `HEAD`, and
  never notice two sessions editing one file. Under a strict rule that gap is covered by
  accident (the second writer is denied everything), but it is the failure that survives
  once anyone relaxes the rule, and it is the one Claude Code's agent teams run into by
  design.
- **`git stash`, enforced.** Repos commonly *document* that `refs/stash` is one stack shared
  by every worktree, without denying it — so the one failure a worktree does not isolate is
  the one nothing catches.
- **Liveness from the transcript, not just the last write.** A session that reads for
  twenty-five minutes looks dead to a last-write-only signal, and hands its checkout to a
  second writer mid-task. Reading the transcript's mtime as well fixes that without
  lengthening the timeout.
- **Release on `SessionEnd`**, so a clean exit frees the tree immediately instead of after
  the timeout.
- **A heads-up at `SessionStart`** when someone else is already writing, so a session can
  isolate before it collides rather than after.
- **No lost updates in the registry**, because it is one file per session.
- **It covers every repo on the machine**, including checkouts that a per-repo guard cannot
  reach.

## What you would lose by deleting a repo guard

- **Enforcement for colleagues.** This is usually the decisive one. Where the rule is
  team-wide and the failure lands in shared history and review rather than one person's
  afternoon, a teammate who has not installed anything gets no enforcement at all from a
  guard living in your `~/.claude/`.
- **The strict rule**, unless you turn it back on — see below.
- **A remedy in the repo's own terms.** A repo whose denial points at its own
  `scripts/new-worktree.sh` — branching off its integration branch and copying
  `.worktreeinclude` across — is giving better advice than a generic message can. The global
  guard's message points at `EnterWorktree`, which cuts from the repository's *default*
  branch; wherever work merges through a non-default branch, that base is wrong. A generic
  remedy is worse than a specific one.

## Recommendation

**Default to keeping a committed repo guard and changing nothing.** The global guard stands
down there, and in the repos where it matters most the committed hook is doing something the
global one structurally cannot: holding the rule for people who never installed it.

Take the improvements piecemeal instead, if they are worth it — the same-file rule and the
`git stash` denial are the two that add cover rather than duplicate it, and both are small
additions to an existing guard.

## If you do consolidate

Per repository:

1. Install the global guard into the repo instead of your home directory, in strict mode if
   that is what the repo enforced, so the behaviour does not change under anyone:

   ```bash
   python scripts/install.py --repo . --dry-run
   ```

   Then without `--dry-run`, and add to that repo's `.claude/settings.json`:

   ```json
   { "env": { "CLAUDE_PARALLEL_GUARD": "strict" } }
   ```

2. Remove the old hook registration from the repo's `.claude/settings.json` **and** the hook
   script — leaving the registration behind makes the new guard stand down and the repo ends
   up with no guard at all, which is the worst of the three outcomes.

3. Rewrite the remedy text where the base branch is not the repository's default. The guard
   spells out `git worktree add … <base>` followed by `EnterWorktree`, which is correct
   generically; a repo with its own worktree script should say so instead.

4. Update the prose that describes the rule — whatever the repo's own docs assert about
   concurrent writers — including the env var name, which becomes `CLAUDE_PARALLEL_GUARD=off`.
   A doc asserting a contract nothing holds is how the original failures happened.

5. Run `python scripts/test_guard.py`, then check `python scripts/install.py --status` from
   inside the repo: it should report the repo install and no longer warn that the repo ships
   its own guard.

Leave the old registry files alone; they are inert, and anything living inside `.git` can
never dirty `git status`.
