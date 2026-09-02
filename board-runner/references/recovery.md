# Recovery: credits, crashes, and a closed laptop

An unattended run has to be interruptible, because the things that interrupt it are
ordinary: a token budget resets tomorrow, a laptop lid closes, Windows reboots for an
update at 04:00. What matters is that stopping costs the *remaining* work rather than the
work already done.

## Recovery is per ticket, not per run

Every job is a separate `claude -p` process with its own `--session-id`, recorded in
`state.json` along with the directory it ran in:

```json
"photos-93": {
  "repo": "photos", "issue": 93,
  "sessionId": "c143d862-adf1-4ca3-b1fd-451ff6aff3fb",
  "cwd": "C:/Users/Chris/Documents/photos",
  "startedAt": "2026-09-02T04:12:00.394Z",
  "status": "running"
}
```

So a stopped ticket resumes with everything it had learned:

```bash
claude --resume c143d862-adf1-4ca3-b1fd-451ff6aff3fb
```

Run it **from the directory `cwd` names**. The session comes back inside the worktree it
had already cut, with its plan, its reading and its half-finished diff intact, and finishes
the ticket. Nothing needs to be explained to it.

This is why session ids are recorded before the process starts rather than after it
finishes. A job that dies without ever reporting is exactly the one you need the id for.

## Restarting the runner

Just run it again. It re-derives the board from GitHub rather than from anything it wrote
down, so it needs no memory of where it was. Two things keep a restart from doubling work:

- **the claim label** keeps it off tickets another process still holds;
- **closed issues** are simply no longer in the query.

A restart with `--only <repo>` writes `state-<repo>.json` rather than `state.json`, so a
second instance can be started for one repository without overwriting the first's record.
The board reads every `state*.json`.

## A claim left by a worker that died

A worker killed between claiming and exiting leaves `agent-running` on its issue, and the
runner will politely skip that ticket forever. Clear it by hand:

```bash
gh issue edit <n> --remove-label agent-running
```

Check first that nothing is actually on it — `git worktree list` in that repository, and
the board's own "unscheduled chat" list, which reads session transcripts rather than the
runner's state and will show a hand-started chat the runner knows nothing about.

## A standing worktree is a question, not a failure

Workers are told that if a ticket needs a decision only the author can make, they should
stop, say so, and **leave the worktree in place** rather than guess. So after a run:

```bash
git worktree list
```

A worktree with commits in it and no merged PR is a worker that got somewhere and then
found a question. Read its log — `logs/<repo>-<issue>.log` ends with what it said — and
either answer it and resume the session, or take the tree down.

This is also why `--uninstall` does not delete `config.json` or `logs/`. Removing the tool
should not remove the record of what it did.

## What is not recoverable

A ticket whose worker **landed the change but never closed the issue** looks identical to
one that did nothing: no open PR, no remote branch, no claim. The runner may launch it
again, and the second worker will find its own work already on the integration branch.

`worktree-per-change`'s `land.py` refuses a branch that has already merged, before pushing
anything, which catches this at the last moment. But the real defence is upstream: the
worker prompt says plainly that closing the issue is what releases the tickets behind it,
because an unclosed issue also stalls the entire cascade. If a run ends with everything
blocked on a ticket whose work is visibly on `main`, close it by hand and the next poll
will move.
