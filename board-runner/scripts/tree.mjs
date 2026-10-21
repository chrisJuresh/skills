#!/usr/bin/env node
/**
 * Live tree of the board: every ticket, what blocks it, and which chat is on it.
 *
 *   node tree.mjs              serve it at http://localhost:7717 and keep it fresh
 *   node tree.mjs --watch      the same tree, redrawn in the terminal
 *   node tree.mjs --once       print it once and exit
 *
 * Five sources, because no single one knows the whole story:
 *
 *   GitHub          the graph — open/closed, labels, and the `blocked_by` edges
 *   state.json      the scheduler's own jobs: session id, start time, turns, cost
 *   logs/*.log      what each worker is doing right now, and when it last moved
 *   git worktree    work standing on disk, including chats nobody scheduled
 *   ~/.claude/projects/<cwd>/<sid>.jsonl
 *                   every session's transcript — this is how a chat that was
 *                   started by hand, rather than by the scheduler, still appears
 *
 * The GitHub half is cached for 45s (it is the slow half); everything local is
 * re-read on every poll, so a worker's current tool shows up within seconds.
 */

import { execFileSync } from 'node:child_process';
import { createServer } from 'node:http';
import { readFileSync, readdirSync, statSync, existsSync, openSync, readSync, closeSync, fstatSync } from 'node:fs';
import { dirname, join, basename } from 'node:path';
import { fileURLToPath } from 'node:url';
import { homedir } from 'node:os';

const HERE = dirname(fileURLToPath(import.meta.url));
const LOGS = join(HERE, 'logs');
const PROJECTS = join(homedir(), '.claude', 'projects');
const cfg = JSON.parse(readFileSync(join(HERE, 'config.json'), 'utf8'));

const argv = process.argv.slice(2);
const flag = (n) => argv.includes(`--${n}`);
const opt = (n, d) => { const i = argv.indexOf(`--${n}`); return i === -1 || i === argv.length - 1 ? d : argv[i + 1]; };
const PORT = Number(opt('port', 7717));

const gh = (args, cwd) => execFileSync('gh', args, { cwd, encoding: 'utf8', maxBuffer: 32 * 1024 * 1024 });
const git = (args, cwd) => { try { return execFileSync('git', args, { cwd, encoding: 'utf8' }); } catch { return ''; } };

// ------------------------------------------------------------- GitHub, cached
let graphCache = { at: 0, data: null };

function fetchGraph() {
  const repos = {};
  for (const [name, repo] of Object.entries(cfg.repos)) {
    const issues = JSON.parse(gh(['issue', 'list', '--state', 'all', '--limit', '120',
      '--json', 'number,title,state,labels,closedAt,updatedAt'], repo.cwd));
    const byNum = new Map();
    for (const it of issues) {
      byNum.set(it.number, {
        number: it.number,
        title: it.title,
        closed: it.state === 'CLOSED',
        labels: it.labels.map((l) => l.name),
        closedAt: it.closedAt,
        blockedBy: [],
      });
    }
    // Only open issues need their edges — a closed one blocks nothing.
    for (const it of issues) {
      if (it.state !== 'OPEN') continue;
      try {
        const deps = JSON.parse(gh(['api', `repos/${repo.slug}/issues/${it.number}/dependencies/blocked_by`,
          '--jq', '[.[]|{number,state}]'], repo.cwd));
        byNum.get(it.number).blockedBy = deps.map((d) => ({ number: d.number, open: d.state === 'open' }));
      } catch { /* no dependency API answer — treat as unblocked */ }
    }
    repos[name] = byNum;
  }
  return repos;
}

function graph() {
  if (Date.now() - graphCache.at < 45_000 && graphCache.data) return graphCache.data;
  try {
    graphCache = { at: Date.now(), data: fetchGraph() };
  } catch (e) {
    if (!graphCache.data) throw e;           // nothing to fall back to
    graphCache.at = Date.now() - 30_000;     // stale, but retry sooner
  }
  return graphCache.data;
}

// ----------------------------------------------------------- the local halves
// Every state*.json, because a repo gated on compute runs under its own scheduler
// instance with its own state file.
function schedulerJobs() {
  const jobs = {};
  for (const f of readdirSync(HERE).filter((f) => /^state.*\.json$/.test(f))) {
    try { Object.assign(jobs, JSON.parse(readFileSync(join(HERE, f), 'utf8')).jobs ?? {}); } catch { /* mid-write */ }
  }
  return jobs;
}

// What a repo's gate says right now, so the board can explain an idle slot rather
// than just showing one.
function gateStatus(repo) {
  if (!repo.gate) return null;
  try {
    execFileSync(repo.gate[0], repo.gate.slice(1), { cwd: HERE, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] });
    return { open: true, why: repo.gateWhy ?? null };
  } catch (e) {
    return {
      open: false,
      why: repo.gateWhy ?? null,
      detail: (e.stderr || '').trim().split('\n').filter(Boolean)
        .map((l) => l.replace(/^gate: busy — /, '')).slice(0, 3),
    };
  }
}

// The last thing a worker actually did. Tool lines are prefixed "  · " by the
// scheduler, so the trail reads as a sequence of moves rather than a wall of text.
function workerActivity(key) {
  const f = join(LOGS, `${key}.log`);
  if (!existsSync(f)) return null;
  const st = statSync(f);
  const tail = readFileSync(f, 'utf8').slice(-6000).split('\n').filter((l) => l.trim());
  const tools = tail.filter((l) => l.startsWith('  · ')).map((l) => l.slice(4).trim());
  const said = [...tail].reverse().find((l) => !l.startsWith('  · ') && !l.startsWith('=====') && !l.startsWith('---') && !l.startsWith('[stderr]'));
  return {
    idleSeconds: Math.round((Date.now() - st.mtimeMs) / 1000),
    lastTool: tools[tools.length - 1] ?? null,
    toolCount: tools.length,
    lastSaid: said ? said.slice(0, 200) : null,
  };
}

// ------------------------------------------------------- context window usage
// Read only the tail of the stream: these files reach hundreds of megabytes, and the
// answer is always near the end. Cached on file size, so a poll costs one stat for a
// worker that has not spoken since the last one.
function tailBytes(path, n) {
  const fd = openSync(path, 'r');
  try {
    const size = fstatSync(fd).size;
    const start = Math.max(0, size - n);
    const len = size - start;
    const buf = Buffer.alloc(len);
    readSync(fd, buf, 0, len, start);
    return { text: buf.toString('utf8'), partial: start > 0 };
  } finally { closeSync(fd); }
}

// Context window per model. A finished run states its own in the result event, so
// those are learned rather than assumed; the seed is what completed opus-5 runs here
// reported (1M), and anything unknown falls back to the conservative 200k.
const WINDOW = new Map([['claude-opus-5', 1_000_000]]);
const windowFor = (model) => WINDOW.get(model) ?? 200_000;

const ctxCache = new Map();
function contextFor(key) {
  const f = join(LOGS, `${key}.jsonl`);
  if (!existsSync(f)) return null;
  let size; try { size = statSync(f).size; } catch { return null; }
  const hit = ctxCache.get(key);
  if (hit && hit.size === size) return hit.ctx;

  let ctx = null;
  for (const span of [262144, 2097152]) {
    const { text, partial } = tailBytes(f, span);
    const lines = text.split('\n');
    if (partial) lines.shift();               // the first line is a fragment
    for (let i = lines.length - 1; i >= 0 && !ctx; i--) {
      const l = lines[i];
      if (l.charCodeAt(0) !== 123) continue;  // '{'
      let ev; try { ev = JSON.parse(l); } catch { continue; }
      // A finished run states its window; remember it for every run of that model.
      if (ev.type === 'result' && ev.modelUsage) {
        for (const [m, u] of Object.entries(ev.modelUsage)) if (u?.contextWindow) WINDOW.set(m, u.contextWindow);
      }
      if (ev.type !== 'assistant') continue;
      // Subagent turns carry their own context and are billed to their own window —
      // counting one as the chat's occupancy reads a 141k subagent as the 454k chat.
      if (ev.parent_tool_use_id) continue;
      const u = ev.message?.usage;
      if (!u) continue;
      // What the model was carrying on its last request: fresh input plus everything
      // read from or written to cache. Output is not part of the prompt.
      const used = (u.input_tokens ?? 0) + (u.cache_read_input_tokens ?? 0) + (u.cache_creation_input_tokens ?? 0);
      if (used > 0) ctx = { used, window: windowFor(ev.message?.model) };
    }
    if (ctx || !partial) break;               // nothing more to find in a whole file
  }
  if (ctx) ctx.pct = Math.round((ctx.used / ctx.window) * 100);
  ctxCache.set(key, { size, ctx });
  return ctx;
}

function worktrees(repo) {
  return git(['worktree', 'list', '--porcelain'], repo.cwd)
    .split('\n\n').map((b) => {
      const path = /worktree (.+)/.exec(b)?.[1];
      const branch = /branch refs\/heads\/(.+)/.exec(b)?.[1] ?? (b.includes('detached') ? 'detached' : null);
      return path ? { path: path.trim(), branch, name: basename(path.trim()) } : null;
    }).filter((w) => w && w.path.includes('worktrees'));
}

// ------------------------------------------------- chats nobody scheduled
// Every session, scheduler-run or hand-started, writes a transcript under a
// directory named for its cwd. A recently-touched one is a live chat; the issue
// it is on is usually named in its opening message.
function encodeCwd(p) { return p.replace(/[:\\/.]/g, '-'); }

function loosSessions(repo, knownIds) {
  const dirs = readdirSync(PROJECTS).filter((d) => {
    const want = encodeCwd(repo.cwd).toLowerCase();
    return d.toLowerCase() === want || d.toLowerCase().startsWith(want + '--');
  });
  const out = [];
  const cutoff = Date.now() - 6 * 3600_000;
  for (const d of dirs) {
    const dir = join(PROJECTS, d);
    let files;
    try { files = readdirSync(dir).filter((f) => f.endsWith('.jsonl')); } catch { continue; }
    for (const f of files) {
      const full = join(dir, f);
      let st; try { st = statSync(full); } catch { continue; }
      if (st.mtimeMs < cutoff) continue;
      const sid = f.replace(/\.jsonl$/, '');
      if (knownIds.has(sid)) continue;
      let issue = null;
      try {
        const head = readFileSync(full, 'utf8').slice(0, 12000);
        issue = Number(/issues\/(\d+)/.exec(head)?.[1] ?? /gh issue view (\d+)/.exec(head)?.[1] ?? 0) || null;
      } catch { /* unreadable */ }
      out.push({
        sessionId: sid,
        issue,
        dir: d,
        idleSeconds: Math.round((Date.now() - st.mtimeMs) / 1000),
      });
    }
  }
  return out.sort((a, b) => a.idleSeconds - b.idleSeconds);
}

// ------------------------------------------------------------------ the model
function model() {
  const g = graph();
  const jobs = schedulerJobs();
  const knownIds = new Set(Object.values(jobs).map((j) => j.sessionId));
  const out = { at: new Date().toISOString(), deadline: cfg.deadline, repos: {} };

  for (const [name, repo] of Object.entries(cfg.repos)) {
    const byNum = g[name];
    const trees = worktrees(repo);
    const nodes = new Map();

    for (const [n, it] of byNum) {
      // Epics are umbrellas, never work. They are not on the board at all.
      if ((repo.epics ?? []).includes(n)) continue;
      const key = `${name}-${n}`;
      const job = jobs[key];
      // A closed ticket stays on the board if something still hangs off it, or if
      // this scheduler is the thing that closed it — that is a result worth seeing.
      if (it.closed && !job && !hasOpenDependent(byNum, n)) continue;
      const openBlockers = it.blockedBy.filter((b) => b.open).map((b) => b.number);
      const human = it.labels.includes(cfg.humanLabel ?? 'ready-for-human');
      const claimed = it.labels.includes(cfg.claimLabel) || (repo.claimed ?? []).includes(n);
      const live = job && job.status === 'running';

      let status;
      if (it.closed) status = 'done';
      else if (live) status = 'running';
      else if (job && job.status === 'exited-fail') status = 'failed';
      else if (job && job.status === 'exited-ok') status = 'landed?';
      else if (human) status = 'human';
      else if (openBlockers.length) status = 'blocked';
      else if (claimed) status = 'claimed';
      else status = 'ready';

      nodes.set(n, {
        number: n, title: it.title, status, openBlockers, human,
        scheduled: !!job,
        blockedBy: it.blockedBy.map((b) => b.number),
        job: job ? {
          sessionId: job.sessionId, status: job.status, startedAt: job.startedAt,
          turns: job.turns, costUsd: job.costUsd, cwd: job.cwd,
          activity: live ? workerActivity(key) : null,
          context: contextFor(key),
        } : null,
      });
    }

    out.repos[name] = {
      slug: repo.slug,
      concurrency: repo.concurrency,
      gate: gateStatus(repo),
      nodes: [...nodes.values()].sort((a, b) => a.number - b.number),
      worktrees: trees,
      looseSessions: loosSessions(repo, knownIds),
    };
  }
  return out;
}

function hasOpenDependent(byNum, n) {
  for (const [, it] of byNum) if (!it.closed && it.blockedBy.some((b) => b.number === n)) return true;
  return false;
}

// --------------------------------------------------------------- the ascii tree
const GLYPH = {
  running: '●', ready: '○', blocked: '·', done: '✓', human: '✋',
  claimed: '◐', failed: '✗', 'landed?': '?',
};
const COLOUR = {
  running: '\x1b[92m', ready: '\x1b[96m', blocked: '\x1b[31m', done: '\x1b[32m',
  human: '\x1b[95m', claimed: '\x1b[93m', failed: '\x1b[91m', 'landed?': '\x1b[93m',
};
const R = '\x1b[0m', DIM = '\x1b[2m';
// Marks a ticket this scheduler launched, whatever became of it afterwards.
const MARK = '\x1b[96m⚙\x1b[0m';

function ascii(m) {
  const lines = [];
  const since = (iso) => { const s = Math.round((Date.now() - new Date(iso)) / 1000); return s > 3600 ? `${Math.floor(s / 3600)}h${Math.floor(s % 3600 / 60)}m` : s > 60 ? `${Math.floor(s / 60)}m` : `${s}s`; };
  lines.push(`${DIM}${new Date(m.at).toLocaleTimeString('en-GB', { hour12: false })} · nothing new launches at/after ${m.deadline}${R}`);

  for (const [name, repo] of Object.entries(m.repos)) {
    const live = repo.nodes.filter((n) => n.status === 'running').length;
    lines.push('');
    lines.push(`\x1b[1m${name}\x1b[0m ${DIM}${repo.slug} — ${live}/${repo.concurrency} slots${R}`);

    const byNum = new Map(repo.nodes.map((n) => [n.number, n]));
    // Hang a ticket under the blocker still gating it, not under one that has closed.
    const parentOf = (x) => {
      const onBoard = x.blockedBy.filter((b) => byNum.has(b) && b !== x.number);
      return onBoard.find((b) => byNum.get(b).status !== 'done') ?? onBoard[0] ?? null;
    };
    const childrenOf = (n) => repo.nodes.filter((x) => parentOf(x) === n);
    const roots = repo.nodes.filter((n) => parentOf(n) === null);
    const drawn = new Set();

    const draw = (node, prefix, last) => {
      if (drawn.has(node.number)) return;
      drawn.add(node.number);
      const g = GLYPH[node.status] ?? '?';
      const c = COLOUR[node.status] ?? '';
      const also = node.openBlockers.filter((b) => b !== undefined);
      const a = node.job?.activity;
      const bits = [];
      if (node.status === 'running' && node.job) {
        bits.push(`${since(node.job.startedAt)}`);
        if (a?.lastTool) bits.push(`${a.lastTool}·${a.toolCount}`);
        if (a && a.idleSeconds > 420) bits.push(`\x1b[91midle ${Math.floor(a.idleSeconds / 60)}m\x1b[0m`);
      }
      const ctx = node.job?.context;
      if (ctx) {
        const c2 = ctx.pct >= 85 ? '\x1b[91m' : ctx.pct >= 60 ? '\x1b[93m' : '\x1b[92m';
        bits.push(`${c2}ctx ${ctx.pct}%\x1b[0m ${DIM}${Math.round(ctx.used / 1000)}k/${Math.round(ctx.window / 1000)}k${R}`);
      }
      if (node.status === 'blocked' && also.length > 1) bits.push(`waits on ${also.map((x) => '#' + x).join(',')}`);
      const tail = bits.length ? ` ${DIM}[${bits.join(' · ')}]${R}` : '';
      const mark = node.scheduled ? MARK : ' ';
      lines.push(`${prefix}${last ? '└─' : '├─'} ${mark}${c}${g} #${node.number}${R} ${node.title.slice(0, 58)}${tail}`);
      const kids = childrenOf(node.number).filter((k) => !drawn.has(k.number));
      kids.forEach((k, i) => draw(k, prefix + (last ? '   ' : '│  '), i === kids.length - 1));
    };
    roots.forEach((r, i) => draw(r, ' ', i === roots.length - 1));
    repo.nodes.filter((n) => !drawn.has(n.number)).forEach((n, i, arr) => draw(n, ' ', i === arr.length - 1));

    for (const s of repo.looseSessions.filter((s) => s.idleSeconds < 900)) {
      lines.push(` ${DIM}   ~ unscheduled chat ${s.sessionId.slice(0, 8)}${s.issue ? ` on #${s.issue}` : ''} · last moved ${s.idleSeconds}s ago${R}`);
    }
  }
  lines.push('');
  lines.push(`${DIM}● running  ○ ready  · blocked  ✓ done  ✋ human  ◐ claimed  ✗ failed  ⚙ scheduler started it${R}`);
  return lines.join('\n');
}

// ------------------------------------------------------------------- the page
const PAGE = readFileSync(join(HERE, 'tree.html'), 'utf8');

// The model is recomputed on a timer and served from memory. Computing it inside
// the request handler looked simpler and was wrong: the first, uncached build makes
// a `gh` call per open issue and takes tens of seconds, during which a 4s poll piles
// request on request, each spawning its own subprocesses. Precomputing means a request
// is a string lookup and a slow rebuild costs one stale page rather than a stampede.
async function serve() {
  let latest = null, lastError = null, building = false;

  const rebuild = async () => {
    if (building) return;
    building = true;
    try { latest = model(); lastError = null; }
    catch (e) { lastError = String(e.message); }
    finally { building = false; }
  };

  await rebuild();
  setInterval(rebuild, 3000);

  createServer((req, res) => {
    if (req.url.startsWith('/api')) {
      res.writeHead(latest ? 200 : 503, { 'content-type': 'application/json', 'cache-control': 'no-store' });
      return res.end(latest ? JSON.stringify(latest) : JSON.stringify({ error: lastError ?? 'building…' }));
    }
    res.writeHead(200, { 'content-type': 'text/html; charset=utf-8' });
    res.end(PAGE);
  }).listen(PORT, () => console.log(`tree on http://localhost:${PORT}`));
}

if (flag('once')) console.log(ascii(model()));
else if (flag('watch')) {
  const tick = () => { process.stdout.write('\x1b[2J\x1b[H' + ascii(model()) + '\n'); };
  tick(); setInterval(tick, 5000);
} else serve();
