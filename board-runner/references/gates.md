# Gates: when a landed dependency is still holding the machine

`blocked_by` answers *may this ticket start?* in terms of the board. A gate answers it in
terms of the machine, and the two are not the same question.

## The failure

A ticket lands its code and closes. Everything blocked on it becomes runnable, correctly —
the seam it added exists, and the next ticket can build on it. But the *job* that ticket
started, the twelve-hour training run that produces the numbers its sibling will read, is
still going. The runner sees a clear board, launches the next ticket, and that ticket's
first act is to start a second training run on the one GPU.

That does not queue. It OOMs, and it takes the first run down with it — six hours of
compute lost to a scheduler that was reading the right graph and asking it the wrong
question.

Logical independence makes it worse rather than better. Two tickets with no edge between
them are exactly the pair a dependency-aware scheduler is most confident about running
together, and exactly the pair that will contend for hardware neither of them mentions.

## The shape

A repository may declare a `gate`: a command that must exit 0 before **anything new**
starts there.

```json
"gate": ["node", "gates/compute-idle.mjs", "study|train_|export_sweep", "F:/study", "240"],
"gateWhy": "one GPU and one CPU, and every arm here wants all of both"
```

It is checked **once per tick, not once per launch**. The answer cannot change between two
launches in the same tick, and each check spawns a subprocess.

`gateWhy` is not decoration. It is what the board prints when a slot sits empty with ready
work on it, and without it an idle repository reads as a stalled scheduler.

## The predicate that ships

`gates/compute-idle.mjs <cmdline-regex> [logDir] [idleSeconds]` refuses while either:

- **a process matches.** Any python-ish process whose command line matches the regex —
  the project's path, or the names of its jobs. `pytest` is excluded: test runs are not
  training runs and a repository always has one going.
- **a log is still growing.** Anything in `logDir` written within `idleSeconds`.

Processes are the primary signal: a run that is alive is alive, whatever it has written
lately. The log check is the backstop for a run whose process has been re-parented or
renamed, where a file still growing is the only evidence left.

**A gate that cannot inspect the machine refuses.** If the process query itself fails, it
exits non-zero rather than assuming the machine is free — failing open would produce
exactly the collision the gate exists to prevent, and it would do it at 3am.

## Choosing the regex

Match the project and its jobs, not the interpreter. `python` matches every unrelated
script on the machine and gates a repository forever; `train_tuned_rmse.py` matches one job
and misses the next one somebody adds. The pattern that survives is usually the project's
own path — its jobs almost always name their data directory on the command line — plus a
prefix for the job family:

```
study|train_|export_
```

Check it against a machine that is genuinely busy before trusting it:

```bash
node gates/compute-idle.mjs "study|train_|export_" F:/study 240; echo "exit=$?"
```

A busy machine prints the matching processes and exits 1. That output is what the board
shows, so read it as the user will.

## Granularity

Gating is per repository, not per ticket. Some tickets in a compute-heavy repository want
no GPU at all — a report to write, a figure to render — and the coarse rule holds them back
with the rest.

This is usually free, because in a repository where the arms are the work, the
report-writing tickets are downstream of the arms and blocked anyway. When it stops being
free, the gate belongs on the ticket rather than the repository, and this is the point at
which to move it. It has not been needed yet, which is why it is not built.
