#!/usr/bin/env node
/**
 * AFK scheduler for /implement.
 *
 * One issue, one brand-new `claude -p` session, one worktree, one landed change.
 * The queue is not a list — it is a query. Every poll it re-asks GitHub which
 * `ready-for-agent` issues are open and have no open blocker, so a ticket that
 * unblocks because another agent just closed its blocker gets picked up on the
 * next tick without anybody deciding anything.
 *
 *   node scheduler.mjs --dry-run       show the graph and what would launch now
 *   node scheduler.mjs                 run it
 *   node scheduler.mjs --only photos   one repository
 *   node scheduler.mjs --deadline 09:30
 *
 * Nothing new is launched at or after the deadline. Jobs already running are left
 * to finish — killing a session mid-land is how you get a pushed branch with no
 * merge behind it.
 *
 * Recovery: every job's session id is recorded in state.json. An interrupted or
 * failed ticket resumes exactly where it stopped with
 *     claude --resume <sessionId>
 * run from that repository's directory. See HANDOFF.md.
 */

import { spawn, execFileSync } from 'node:child_process';
import { mkdirSync, appendFileSync, writeFileSync, readFileSync, existsSync } from 'node:fs';
import { randomUUID } from 'node:crypto';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const LOGS = join(HERE, 'logs');

const argv = process.argv.slice(2);
const flag = (n) => argv.includes(`--${n}`);
const opt = (n, d) => { const i = argv.indexOf(`--${n}`); return i === -1 || i === argv.length - 1 ? d : argv[i + 1]; };

const cfg = JSON.parse(readFileSync(join(HERE, 'config.json'), 'utf8'));
const DRY = flag('dry-run');
const ONLY = opt('only', null);
const RETRIES = Number(opt('retries', cfg.retries ?? 1));
const POLL_MS = Number(opt('poll', cfg.pollSeconds ?? 60)) * 1000;
const JOB_TIMEOUT_MS = Number(opt('job-timeout', cfg.jobTimeoutMinutes ?? 180)) * 60_000;
// A second instance (say `--only patry`) must not write over the first's state, so
// the file is named after the slice it is running. tree.mjs reads every state*.json.
const STATE = join(HERE, opt('state', ONLY ? `state-${ONLY}.json` : 'state.json'));

function deadlineAt(hhmm) {
  const [h, m] = hhmm.split(':').map(Number);
  const d = new Date();
  d.setHours(h, m, 0, 0);
  if (d.getTime() <= Date.now()) d.setDate(d.getDate() + 1);
  return d;
}
const DEADLINE = deadlineAt(opt('deadline', cfg.deadline ?? '10:00'));
const pastDeadline = () => Date.now() >= DEADLINE.getTime();

const ts = () => new Date().toLocaleTimeString('en-GB', { hour12: false });
const log = (m) => { const line = `[${ts()}] ${m}`; console.log(line); appendFileSync(join(HERE, 'scheduler.log'), line + '\n'); };

const gh = (args, cwd) => execFileSync('gh', args, { cwd, encoding: 'utf8', maxBuffer: 32 * 1024 * 1024 });

// -------------------------------------------------------------- the live graph
// `blocked_by` is GitHub's own dependency edge, so readiness is a fact about the
// board rather than a reading of the prose. An epic is the one thing the API
// cannot tell us — a parent with no blockers looks exactly like leaf work — so it
// is named in config.json and nowhere else.
function readiness(repoName) {
  const repo = cfg.repos[repoName];
  const raw = gh(['issue', 'list', '--state', 'open', '--limit', '200',
    '--label', cfg.agentLabel, '--json', 'number,title,labels'], repo.cwd);
  const issues = JSON.parse(raw);
  const out = [];
  for (const it of issues) {
    const labels = it.labels.map((l) => l.name);
    if (labels.includes(cfg.claimLabel)) { out.push({ ...it, state: 'claimed' }); continue; }
    if ((repo.epics ?? []).includes(it.number)) { out.push({ ...it, state: 'epic' }); continue; }
    if ((repo.claimed ?? []).includes(it.number)) { out.push({ ...it, state: 'claimed' }); continue; }
    let blockers = [];
    try {
      blockers = JSON.parse(gh(['api', `repos/${repo.slug}/issues/${it.number}/dependencies/blocked_by`,
        '--jq', '[.[]|{number,state}]'], repo.cwd));
    } catch { blockers = []; }
    const open = blockers.filter((b) => b.state === 'open').map((b) => b.number);
    out.push({ ...it, state: open.length ? 'blocked' : 'ready', blockedBy: open });
  }
  return out;
}

// ------------------------------------------------------------------ the claim
// A label, so that a ticket a scheduler has taken is visible to every other chat
// and to the next poll. Removed again when the job fails, so a retry can see it.
function claim(repo, n, on) {
  if (DRY) return;
  try {
    gh(['issue', 'edit', String(n), on ? '--add-label' : '--remove-label', cfg.claimLabel], repo.cwd);
  } catch (e) { log(`  (claim ${on ? 'set' : 'clear'} failed on #${n}: ${String(e.message).split('\n')[0]})`); }
}

// ------------------------------------------------------------------- the gate
// `blocked_by` knows when a dependency has *landed*; it does not know that the job
// producing that dependency's numbers is still pinning the GPU. A repo may declare a
// command that must exit 0 before anything new starts in it.
function gateOpen(repoName, repo) {
  if (!repo.gate) return { open: true };
  try {
    execFileSync(repo.gate[0], repo.gate.slice(1), { cwd: HERE, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] });
    return { open: true };
  } catch (e) {
    const why = (e.stderr || '').trim().split('\n').filter(Boolean).slice(0, 2).join('; ');
    return { open: false, why: why || `gate exited ${e.status}` };
  }
}

function ensureLabel(repo) {
  if (DRY) return;
  try {
    gh(['label', 'create', cfg.claimLabel, '--description', 'An unattended agent holds this ticket', '--color', 'fbca04'], repo.cwd);
  } catch { /* already exists */ }
}

// ------------------------------------------------------------------ the prompt
function promptFor(issue, repo) {
  return [
    `/implement https://github.com/${repo.slug}/issues/${issue.number}`,
    ``,
    `Read the ticket first with \`gh issue view ${issue.number}\`, and read its parent if it names one.`,
    ``,
    `This is an unattended run. Nobody will answer a question, so where the ticket leaves a`,
    `choice open, make the smallest defensible one, write down what you chose and why in the`,
    `commit message, and carry on. If the ticket turns out to need a decision only the author`,
    `can make, stop, say so plainly, and leave the worktree in place rather than guessing.`,
    ``,
    `Follow the worktree-per-change protocol: nothing is written in the main checkout, the`,
    `worktree is cut from fetched origin/${repo.integration}, and the change is delivered — not left`,
    `on a local branch. ${repo.landHint}`,
    ``,
    `Other agents are working other tickets in this repository at the same time, and tickets`,
    `unblock as they land. Expect the base to have moved under you by the time you land, and`,
    `bring it down rather than forcing anything.`,
    ``,
    `When the change has landed, close the issue with \`gh issue close ${issue.number}\` and a one-line`,
    `comment saying what landed. Closing it is what releases the tickets blocked on this one.`,
  ].join('\n');
}

// --------------------------------------------------------------------- one job
// Only `jobs` is carried forward. Anything else a previous version of this file
// left behind is not state — it is a stale claim about work that may never have run.
const state = { jobs: existsSync(STATE) ? (JSON.parse(readFileSync(STATE, 'utf8')).jobs ?? {}) : {} };
const saveState = () => writeFileSync(STATE, JSON.stringify(
  { deadline: DEADLINE.toISOString(), updatedAt: new Date().toISOString(), ...state }, null, 2));

function runJob(issue, repoName) {
  const repo = cfg.repos[repoName];
  const key = `${repoName}-${issue.number}`;
  const sessionId = randomUUID();
  const jsonl = join(LOGS, `${key}.jsonl`);
  const text = join(LOGS, `${key}.log`);

  const args = [
    '-p', promptFor(issue, repo),
    '--permission-mode', 'bypassPermissions',
    '--model', 'opus',
    '--effort', 'xhigh',
    '--session-id', sessionId,
    '--name', `implement-${key}`,
    '--output-format', 'stream-json',
    '--verbose',
  ];

  state.jobs[key] = { repo: repoName, issue: issue.number, title: issue.title, sessionId, cwd: repo.cwd, startedAt: new Date().toISOString(), status: 'running' };
  saveState();
  claim(repo, issue.number, true);
  log(`start ${key}  "${issue.title.slice(0, 60)}"  session=${sessionId.slice(0, 8)}`);
  appendFileSync(text, `\n===== ${new Date().toISOString()} start ${key} session=${sessionId} =====\n`);

  return new Promise((resolve) => {
    const child = spawn('claude', args, { cwd: repo.cwd, stdio: ['ignore', 'pipe', 'pipe'], shell: false, windowsHide: true });
    const killTimer = setTimeout(() => {
      log(`TIMEOUT ${key} after ${JOB_TIMEOUT_MS / 60000}min — killing (resume with: claude --resume ${sessionId})`);
      child.kill('SIGKILL');
    }, JOB_TIMEOUT_MS);

    let buf = '';
    child.stdout.on('data', (chunk) => {
      appendFileSync(jsonl, chunk);
      buf += chunk.toString();
      const lines = buf.split('\n'); buf = lines.pop();
      for (const line of lines) {
        if (!line.trim()) continue;
        let ev; try { ev = JSON.parse(line); } catch { continue; }
        if (ev.type === 'assistant' && ev.message?.content) {
          for (const c of ev.message.content) {
            if (c.type === 'tool_use') appendFileSync(text, `  · ${c.name}\n`);
            if (c.type === 'text' && c.text.trim()) appendFileSync(text, `${c.text}\n`);
          }
        }
        if (ev.type === 'result') {
          appendFileSync(text, `\n--- ${ev.subtype} turns=${ev.num_turns} cost=$${(ev.total_cost_usd ?? 0).toFixed(2)} ---\n`);
          state.jobs[key].costUsd = ev.total_cost_usd;
          state.jobs[key].turns = ev.num_turns;
        }
      }
    });
    child.stderr.on('data', (c) => appendFileSync(text, `[stderr] ${c}`));

    child.on('error', (err) => {
      clearTimeout(killTimer);
      appendFileSync(text, `[spawn error] ${err.message}\n`);
      resolve({ key, code: -1 });
    });
    child.on('close', (code) => {
      clearTimeout(killTimer);
      appendFileSync(text, `\n===== ${new Date().toISOString()} exit ${code} =====\n`);
      log(`${code === 0 ? 'done ' : 'FAIL '} ${key}  exit=${code}`);
      resolve({ key, code });
    });
  });
}

// ------------------------------------------------------------------- the loops
// One loop per repository. Each holds its own concurrency cap, so a slow
// repository cannot starve the other.
async function schedule(repoName) {
  const repo = cfg.repos[repoName];
  ensureLabel(repo);
  const running = new Set();
  const attempts = new Map();
  const done = new Set();
  let lastGateWhy = null;   // so a held gate logs its reason once, not every minute
  let lastIdleMsg = null;

  for (;;) {
    let board;
    try { board = readiness(repoName); }
    catch (e) { log(`${repoName}: board query failed (${String(e.message).split('\n')[0]}) — retrying next tick`); board = null; }

    if (board) {
      const ready = board.filter((i) => i.state === 'ready' && !running.has(i.number) && !done.has(i.number));
      const blocked = board.filter((i) => i.state === 'blocked');

      if (running.size === 0 && ready.length === 0) {
        if (blocked.length === 0) { log(`${repoName}: nothing ready, nothing blocked, nothing running — done`); return; }
        const msg = `${repoName}: nothing runnable; ${blocked.length} blocked on ${[...new Set(blocked.flatMap((b) => b.blockedBy))].map((n) => `#${n}`).join(' ')}`;
        if (msg !== lastIdleMsg) { log(msg); lastIdleMsg = msg; }
        if (pastDeadline()) return;
      }

      // Checked once per tick rather than once per launch: the answer cannot change
      // between two launches in the same tick, and each check spawns a subprocess.
      let gate = { open: true };
      if (ready.length && running.size < repo.concurrency && !pastDeadline()) {
        gate = gateOpen(repoName, repo);
        if (!gate.open && gate.why !== lastGateWhy) {
          log(`${repoName}: holding ${ready.length} ready ticket(s) — ${gate.why}`);
          lastGateWhy = gate.why;
        } else if (gate.open && lastGateWhy) {
          log(`${repoName}: compute is free again — releasing work`);
          lastGateWhy = null;
        }
      }

      while (gate.open && running.size < repo.concurrency && ready.length && !pastDeadline()) {
        const issue = ready.shift();
        running.add(issue.number);
        (async () => {
          for (;;) {
            const a = (attempts.get(issue.number) ?? 0) + 1;
            attempts.set(issue.number, a);
            const res = await runJob(issue, repoName);
            const key = `${repoName}-${issue.number}`;
            state.jobs[key].status = res.code === 0 ? 'exited-ok' : 'exited-fail';
            state.jobs[key].endedAt = new Date().toISOString();
            state.jobs[key].attempts = a;
            saveState();
            if (res.code === 0) { done.add(issue.number); break; }
            claim(repo, issue.number, false);
            if (a > RETRIES || pastDeadline()) { done.add(issue.number); break; }
            log(`retry ${key} (attempt ${a + 1})`);
          }
          running.delete(issue.number);
        })();
      }

      if (pastDeadline() && running.size === 0) { log(`${repoName}: past deadline, nothing running — stopping`); return; }
      if (pastDeadline() && ready.length) log(`${repoName}: deadline — not launching ${ready.map((i) => `#${i.number}`).join(' ')}`);
    }

    await new Promise((r) => setTimeout(r, POLL_MS));
  }
}

// ----------------------------------------------------------------------- main
async function main() {
  mkdirSync(LOGS, { recursive: true });
  const names = Object.keys(cfg.repos).filter((r) => !ONLY || r === ONLY);
  log(`deadline ${DEADLINE.toLocaleString('en-GB')} — nothing new starts at or after it`);

  for (const r of names) {
    const board = readiness(r);
    log(`--- ${r} (${cfg.repos[r].concurrency} at a time) ---`);
    for (const i of board.sort((a, b) => a.number - b.number)) {
      const tag = i.state === 'ready' ? 'READY  ' : i.state === 'blocked' ? `blocked` : i.state === 'epic' ? 'epic   ' : 'claimed';
      const why = i.state === 'blocked' ? ` on ${i.blockedBy.map((n) => `#${n}`).join(' ')}` : '';
      log(`  ${tag} #${i.number}${why}  ${i.title.slice(0, 62)}`);
    }
  }
  if (DRY) { log('DRY RUN — nothing launched'); return; }

  await Promise.all(names.map(schedule));
  log('--- all schedulers stopped ---');
  for (const [k, j] of Object.entries(state.jobs)) {
    log(`${j.status === 'exited-ok' ? 'ok  ' : 'FAIL'} ${k}  attempts=${j.attempts ?? '?'} cost=$${(j.costUsd ?? 0).toFixed(2)}  resume: claude --resume ${j.sessionId}`);
  }
  saveState();
}

main().catch((e) => { console.error(e); process.exit(1); });
