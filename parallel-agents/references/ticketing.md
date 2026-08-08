# Working a ticket queue with several agents

A worktree isolates files. It does not stop two agents picking up the same ticket, and it
does not stop them both editing the one glossary file the planning skills write to. This
page is the coordination layer above the checkout.

It is written against Matt Pocock's engineering chain — `grill-with-docs` → `to-spec` →
`to-tickets` → `implement` → `code-review` — because that is the ticketing system in use,
but the rules generalise to any tracker. **Where this page and that chain's own docs
disagree, Claude Code's documented behaviour wins**; the two places that happens are called
out below.

## Read the tracker config first

`/setup-matt-pocock-skills` writes **`docs/agents/issue-tracker.md`** at the repo root, and
that file is the answer to "where do issues live" — GitHub via `gh`, GitLab via `glab`,
local markdown under `.scratch/`, or a paragraph of prose for anything else. Read it before
touching a ticket, and use the commands it names. Do not assume `gh`; a repo configured for
local markdown has no issues to query and an agent that reaches for `gh` there wastes a
round trip and then invents a workflow.

If the file does not exist, the repo has not been set up. Say so and ask, rather than
picking a tracker on the user's behalf.

## Claim before you build

Assignment is the lock at the work-item layer, and it is cheap:

1. **Read the ticket** — including its comments and the links in its body. The ticket is
   the brief; the spec behind it is context, not a second set of instructions.
2. **Check it is startable.** `to-tickets` gives every ticket its blocking edges. On GitHub
   that is native dependencies (`issue_dependencies_summary.blocked_by`); elsewhere it is a
   "Blocked by" line. Anything above zero means an open blocker — name it and stop, rather
   than building against a blocker's unwritten half.
3. **Claim it, as the first write of the session** — assign yourself. Doing it first means
   an abandoned session still shows who was in it, and a second agent scanning the frontier
   skips it.
4. **Then pick your tree** — the table in `SKILL.md`.

The **frontier** is the set of tickets whose blockers are all closed and which nobody has
assigned. That set, not the ticket count, is how much parallelism the work actually
supports. Four agents on a linear chain of four tickets is one agent's throughput and four
agents' token spend.

If work stops half-finished, leave the issue open, assigned, with a comment saying where it
got to. An open ticket with a note is recoverable; a closed one that did not ship is not.

## Branch and tree before `/implement`, not after

`/implement` commits to **the branch you are already on**. It does not create one and it
does not ask. In a shared checkout `HEAD` is whatever another session last checked out, so
"the current branch" is not a base — it is a coin flip, and it is how a ticket ends up
carrying an unrelated ticket's commits.

So the order is: claim → decide the base → get the tree → *then* `/implement`.

The base is the ticket's blocker where that blocker is closed but not yet merged, and the
repo's integration branch otherwise. Note that this is exactly the case `worktree.baseRef`
cannot express — it chooses only between the default branch and local `HEAD`, never a named
branch — so create the worktree with git and enter it:

```bash
git worktree add .claude/worktrees/<ticket> -b <ticket>-<slug> <base>
```

then `EnterWorktree` that path.

**One invocation, one ticket, one session.** `/implement` documents batch dispatch and
subagent fan-out as not existing, and running several side by side in one checkout as worse
than unsupported — the field reports behind that are an amend landing on another session's
commit, a stash vanishing from `refs/stash`, and commits arriving on the wrong branch, all
in one afternoon. Two of those three are exactly what the guard denies, and the third is why
`SKILL.md` says commit rather than stash. Parallelism across tickets is safe when each
ticket gets its own session and its own tree; it is not safe inside one session.

**Where the chain's docs and Claude Code disagree:** `/implement`'s FAQ describes git
worktrees as "the community workaround". They are not — Claude Code ships worktree creation,
entry, resumption, per-subagent isolation, `.worktreeinclude` and automatic cleanup, and
enforces the boundary at the tool layer for a session that entered one. Use `EnterWorktree`
and treat the isolation as real, because it is.

## After the build

`/implement` has no completion step. It ends at the commit: it does not close the ticket,
does not tick the acceptance criteria, and does not act on what `code-review` found. On a
dependency chain that matters more than it sounds, because the frontier is defined by closed
blockers — if nothing gets closed, nothing ever becomes visibly unblocked and the other
agents have nothing to pick up. So close it yourself, with a comment naming the branch and
what a reader can now see, and let the dependents unblock themselves. Do not edit another
ticket's body to record that this one is done.

`code-review` reviews `git diff <fixed-point>...HEAD`, which excludes staged and
working-tree changes — commit first, or it has nothing to look at. In a worktree the fixed
point is the base you branched from.

**The session that wrote a change is the one that should merge it back.** It already holds
the intent behind every hunk; batching four branches' conflicts onto one agent at the end
throws that away and makes it reconstruct what four sessions already knew.

## The planning skills assume one writer

`grill-with-docs` is stateful by design: resolved terms land in `CONTEXT.md` as they
resolve, and decisions land as ADRs under `docs/adr/`. Its own documentation says plainly
that this assumes a single person curating them, and reports state drift on roughly a fifth
of sampled merged PRs in a two-developer repo — with the hand-curated surfaces drifting
worse than agent memory did.

That shapes how several agents share a repo:

- **`CONTEXT.md` is a single shared insert point.** Two agents grilling at once both edit
  it, and the second write silently discards the first. The guard's same-file rule denies
  that, which is the correct outcome: run grilling sessions one at a time, or partition
  them by context in a multi-context repo.
- **ADRs are one file per decision, so they never conflict.** Copy that shape for anything
  else that appends — a decision log, a changelog, a status page. A file that does not
  exist yet cannot conflict; an append-ordered list means every branch inserts at the same
  line, forever. Where an index is genuinely needed, generate it and re-run the generator
  to resolve, rather than hand-merging.
- **Prefer a linter over a sweep.** Pruning stale docs by hand does not hold. A
  deterministic citation-and-link check in CI does.

## Doc edits, when several branches are open

The other docs are edited in place, and in-place edits to *different sections* merge without
help. What breaks that is not the edit, it is the tidying around it: reflowing a paragraph,
renumbering a list, "while I'm here" fixes to sections the branch has nothing to do with.
Each of those turns a three-line change into a whole-file conflict against every other open
branch.

- Touch the section your change is about, and nothing else.
- Do not reflow. Leave the line breaks as they are even where they are ugly.
- One doc commit, and put it last — a conflict then costs one resolution against the final
  state instead of one per commit that touched the file.
- Do not reach for `merge=union` to silence append-point conflicts. It keeps *both* sides of
  every hunk, so on prose a paragraph one agent edited and another rewrote lands twice:
  merged clean, wrong, and unreviewed. A conflict you have to look at beats a merge you
  do not.
