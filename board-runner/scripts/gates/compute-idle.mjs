#!/usr/bin/env node
/**
 * Exit 0 when the machine is free to start heavy work, non-zero while a training
 * or export run still holds it.
 *
 *   node gates/compute-idle.mjs "patry|train_tuned|export_sweep" [logDir] [idleSeconds]
 *
 * Why a gate at all: `blocked_by` says a ticket may not start until its dependency
 * has *landed*. It says nothing about a dependency that has landed its code and is
 * still running the job that produces its numbers, nor about two tickets that are
 * logically independent and would both want the one GPU. Both of those are how you
 * get an OOM at 3am and two half-finished arms.
 *
 * Processes are the primary signal — a run that is alive is alive, whatever it has
 * written lately. The log-mtime check is a backstop for a run whose process has been
 * re-parented or renamed: a file still growing means something is still working.
 */

import { execFileSync } from 'node:child_process';
import { readdirSync, statSync, existsSync } from 'node:fs';
import { join } from 'node:path';

const [pattern, logDir, idleArg] = process.argv.slice(2);
if (!pattern) { console.error('usage: compute-idle.mjs <cmdline-regex> [logDir] [idleSeconds]'); process.exit(2); }
const idleSeconds = Number(idleArg ?? 240);
const re = new RegExp(pattern, 'i');

// --- processes -------------------------------------------------------------
// Anything python-ish whose command line names this project or one of its jobs.
let busy = [];
try {
  const out = execFileSync('powershell', ['-NoProfile', '-NonInteractive', '-Command',
    "Get-CimInstance Win32_Process -Filter \"Name like '%python%'\" | " +
    "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"],
    { encoding: 'utf8', maxBuffer: 8 * 1024 * 1024 });
  const raw = out.trim() ? JSON.parse(out) : [];
  const procs = Array.isArray(raw) ? raw : [raw];
  busy = procs.filter((p) => p && p.CommandLine && re.test(p.CommandLine))
    // pytest and other short-lived tooling is not a training run
    .filter((p) => !/-m pytest|\bpytest\b/i.test(p.CommandLine))
    .map((p) => ({ pid: p.ProcessId, cmd: p.CommandLine.slice(0, 110) }));
} catch (e) {
  // A gate that cannot see the machine must refuse, not wave work through.
  console.error(`gate: could not inspect processes (${String(e.message).split('\n')[0]})`);
  process.exit(1);
}

if (busy.length) {
  for (const b of busy) console.error(`gate: busy — pid ${b.pid} ${b.cmd}`);
  process.exit(1);
}

// --- logs still growing ----------------------------------------------------
if (logDir && existsSync(logDir)) {
  const cutoff = Date.now() - idleSeconds * 1000;
  for (const f of readdirSync(logDir).filter((f) => f.endsWith('.log'))) {
    let st; try { st = statSync(join(logDir, f)); } catch { continue; }
    if (st.mtimeMs > cutoff) {
      console.error(`gate: busy — ${f} written ${Math.round((Date.now() - st.mtimeMs) / 1000)}s ago`);
      process.exit(1);
    }
  }
}

console.log('gate: idle');
process.exit(0);
