/**
 * DOM smoke test for derived/monthly.html (the Monthly Planner board).
 *
 * The page is a single self-contained HTML file with no build step and no JS test
 * runner in this repo, so this harness loads it into jsdom, stubs every /api/* call,
 * and drives the real handlers. Run it through tests/test_monthly_planner_ui.py, or
 * directly:  node tests/monthly_planner_smoke.js
 *
 * jsdom is a dev-only dependency: npm install --prefix tests
 *
 * The clock is frozen at 2026-10-15 so due-date colouring and the default month
 * window are deterministic.
 */
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const HTML = path.join(ROOT, 'derived', 'monthly.html');
let JSDOM;
try { ({ JSDOM } = require(path.join(__dirname, 'node_modules', 'jsdom'))); }
catch (e) {
  console.error('jsdom not installed — run: npm install --prefix ' + __dirname);
  process.exit(2);
}

const FIXED_MS = Date.UTC(2026, 9, 15, 12, 0, 0);   // 2026-10-15
const TODAY = '2026-10-15';

const mk = (key, epic, budgets, epicExtra = {}) => ({
  key, url: 'u/' + key, summary: key + ' summary', status: 'In Progress',
  orgPriority: 'Prog 0', engDri: 'Sam Lee', prodDri: 'Priya',
  epic: epic ? Object.assign({ key: epic, url: 'u/' + epic, summary: epic + ' epic' }, epicExtra) : null,
  cycles: ['Oct-26'], budgets, podTags: ['MY POD'],
  budgetsSet: Object.fromEntries(Object.keys(budgets).map(m => [m, true])),
});

const BARS = { checkpoint: 'Gap Closure (T-1)', date: '2026-10-02',
  perCycle: { 'Oct-26': { bar: 8, checkpoint: 'Gap Closure (T-1)', date: '2026-10-02',
                          passed: false, allGreen: true } } };
const INITS = { pods: ['MY POD'], v: 3, bars: BARS, initiatives: [
  // epic budget Oct=30, plan says 30            -> in sync
  mk('IDEA-1', 'BOARD-1', { Oct: 30, Nov: 0 }, {
    dueDate: '2026-10-20', status: 'In Progress', assignee: 'Asha Rao',
    health: 'On Track', cycles: ['Oct-26'], overallBudget: 40, priority: 'P1',
    labels: 'cbs,sunset', challenges: 'Dependency on NEWSYS' }),
  // epic budget Oct=5, plan says 7              -> pending, due date already past
  mk('IDEA-2', 'BOARD-2', { Oct: 5, Nov: 0 }, { dueDate: '2026-09-01', status: 'To Do' }),
  // epic budget Oct=12, nothing ticked          -> stale in Jira, no due date set
  mk('IDEA-3', 'BOARD-3', { Oct: 12, Nov: 0 }, { dueDate: '' }),
  // no linked epic                              -> no submit button, epic cols blank
  mk('IDEA-4', null, { Oct: 0, Nov: 0 }),
  // shared epic: 12 + 8 = 20 matches Jira       -> both rows in sync
  mk('IDEA-6', 'BOARD-6', { Oct: 20, Nov: 0 }, { dueDate: '2026-12-15' }),
  mk('IDEA-7', 'BOARD-6', { Oct: 20, Nov: 0 }, { dueDate: '2026-12-15' }),
  // archived idea that still carries a budget — must never reach a capacity total
  Object.assign(mk('IDEA-8', 'BOARD-8', { Oct: 25, Nov: 0 }, { dueDate: '2026-11-02' }),
                { archived: true, archivedOn: '2026-09-29' }),
]};
// delivery dates live on the initiative, not the epic
INITS.initiatives[0].targetGoLive = '2026-10-29';
INITS.initiatives[0].targetHandover = '2026-10-09';

const PLAN = {
  'IDEA-1': { Oct: { on: true,  sp: 30, seeded: true }, Nov: { on: false, sp: 0, seeded: true } },
  'IDEA-2': { Oct: { on: true,  sp: 7,  seeded: true }, Nov: { on: false, sp: 0, seeded: true } },
  'IDEA-3': { Oct: { on: false, sp: 0,  seeded: true }, Nov: { on: false, sp: 0, seeded: true } },
  'IDEA-4': { Oct: { on: true,  sp: 9,  seeded: true }, Nov: { on: false, sp: 0, seeded: true } },
  'IDEA-6': { Oct: { on: true,  sp: 12, seeded: true }, Nov: { on: false, sp: 0, seeded: true } },
  'IDEA-7': { Oct: { on: true,  sp: 8,  seeded: true }, Nov: { on: false, sp: 0, seeded: true } },
  'IDEA-8': { Oct: { on: true,  sp: 25, seeded: true }, Nov: { on: false, sp: 0, seeded: true } },
};
const ST0 = { pods: null, months: ['2026-10', '2026-11'],
  order: ['IDEA-1', 'IDEA-2', 'IDEA-3', 'IDEA-4', 'IDEA-6', 'IDEA-7'],
  plan: PLAN, showAll: true, capOpen: {}, initOpen: {}, est: {}, cols: [] };

const posted = [], detailCalls = [], metaCalls = [], monthCalls = [];
const depCalls = [];
let depRows = [];   // cross-pod readiness rows; per-test
let depFail = false;
let POD_MODE = 'full', writeFails = false, linkPreview = null, unlinkFails = false;
let auditReversed = [], auditBroken = [];

// mirrors capacity_engine.planner_editable_fields() / the page's COL_MODEL
const EDITABLE_FIELDS = {
  status: { target: 'initiative', field: '@status' },
  engDri: { target: 'initiative', field: 'cf_eng' },
  pods: { target: 'initiative', field: 'cf_pods' },
  goLive: { target: 'initiative', field: 'cf_golive' },
  handover: { target: 'initiative', field: 'cf_handover' },
  epicDue: { target: 'epic', field: 'duedate' },
  epicStatus: { target: 'epic', field: '@status' },
  epicOwner: { target: 'epic', field: 'assignee' },
  epicHealth: { target: 'epic', field: 'cf_health' },
  epicLabels: { target: 'epic', field: 'labels' },
  epicChal: { target: 'epic', field: 'cf_chal' },
};
const COL_TARGET = {
  status: { scope: 'init', attr: 'status' }, engDri: { scope: 'init', attr: 'engDri' },
  pods: { scope: 'init', attr: 'podTags' },
  epicDue: { scope: 'epic', attr: 'dueDate' }, epicStatus: { scope: 'epic', attr: 'status' },
  epicOwner: { scope: 'epic', attr: 'assignee' }, epicHealth: { scope: 'epic', attr: 'health' },
  epicLabels: { scope: 'epic', attr: 'labels' }, epicChal: { scope: 'epic', attr: 'challenges' },
};
// what /editmeta would answer per column
const FIELD_META = {
  status:     { editable: true, kind: 'transition', options: [{ id: '31', label: 'Done' }, { id: '11', label: 'Blocked' }] },
  engDri:     { editable: true, kind: 'users', options: [], value: [] },
  pods:       { editable: true, kind: 'multi', value: ['p1'],
                options: [{ id: 'p1', label: 'MY POD' }, { id: 'p2', label: 'COREP - Core Platform' }] },
  epicDue:    { editable: true, kind: 'date', options: [], value: '2026-10-20' },
  epicStatus: { editable: true, kind: 'transition', options: [{ id: '21', label: 'In Progress' }] },
  epicOwner:  { editable: true, kind: 'user', options: [], value: null },
  epicHealth: { editable: true, kind: 'select', value: 'h1',
                options: [{ id: 'h1', label: '🍀 On Time' }, { id: 'h2', label: '⚠️ At Risk' }] },
  epicLabels: { editable: true, kind: 'labels', options: [], value: ['cbs', 'sunset'] },
  epicChal:   { editable: false, kind: 'text', options: [],
                why: 'not on the edit screen, or you lack permission' },
};
const OPT_LABEL = { '31': 'Done', '11': 'Blocked', '21': 'In Progress',
                    h1: '🍀 On Time', h2: '⚠️ At Risk', p1: 'MY POD', p2: 'COREP - Core Platform' };
function displayFor(col, value) {
  const kind = FIELD_META[col].kind;
  if (kind === 'labels') return (value || []).join(', ');
  if (kind === 'user' || kind === 'users')
    return value ? 'Asha Rao' : '';
  if (kind === 'multi') return (value || []).map(v => OPT_LABEL[v] || v).join(', ');
  if (kind === 'select' || kind === 'transition') return OPT_LABEL[value] || '';
  return value == null ? '' : String(value);
}
const NARROW = { pods: ['Q'], v: 3,
  initiatives: [mk('IDEA-9', 'BOARD-9', { Oct: 0, Nov: 0 }), INITS.initiatives[0]] };

async function fetchStub(p, opts) {
  p = String(p);
  const body = opts && opts.body ? JSON.parse(opts.body) : null;
  let out = {};
  if (p.startsWith('/api/initiatives')) out = POD_MODE === 'full' ? INITS : NARROW;
  else if (p.startsWith('/api/initiative?')) { detailCalls.push(p);
    out = { descriptionText: 'the description', impact: 'some impact',
            descriptionRich: [[{ t: 'One Pager: ', h: '' },
                               { t: 'The Doc', h: 'https://example.com/wiki/the-doc' }],
                              [{ t: 'plain line', h: '' }],
                              [{ t: 'bad link', h: 'javascript:alert(1)' }]] }; }
  else if (p.startsWith('/api/month?')) {
    monthCalls.push(p);
    const ym = decodeURIComponent((p.split('ym=')[1] || '2026-10').split('&')[0]);
    const qs = new URLSearchParams(p.split('?')[1] || '');
    const ws = qs.get('start'), we = qs.get('end');
    const [yy, mm] = ym.split('-').map(Number);
    const last = new Date(Date.UTC(yy, mm, 0)).getUTCDate();
    const DOW = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
    const days = [];
    for (let d = 1; d <= last; d++) {
      const dt = new Date(Date.UTC(yy, mm - 1, d)), w = dt.getUTCDay();
      const iso = `${ym}-${String(d).padStart(2,'0')}`;
      days.push({ date: iso, dow: DOW[w], weekend: w === 0 || w === 6,
        holiday: iso === '2026-10-02' ? { type: 'holiday', occasion: 'Gandhi Jayanti' } : null });
    }
    // two people, both present every day, so rate is predictable in the layout maths
    const mkp = n => ({ name: n, statuses: days.map(d => d.weekend ? 'WE'
      : d.holiday ? 'H' : ''), net: 20, sp: 15 });
    const win = ws && we;
    const kept = win ? days.filter(d => d.date >= ws && d.date <= we) : days;
    const mkp2 = n => ({ name: n, statuses: kept.map(d => d.weekend ? 'WE' : d.holiday ? 'H' : ''),
      net: 20, sp: 15 });
    out = { v: 4, label: win ? `${ws} to ${we}` : ym, key: ym, cycle: 'Oct-26',
            cycleWindow: false, customWindow: !!win,
            start: win ? ws : days[0].date, end: win ? we : days[days.length-1].date,
            teamSP: win ? 20 : 40, workingDays: 20, teamNetDays: win ? 20 : 40,
            utilisation: 90, days: kept, people: [mkp2('Sam Lee'), mkp2('Asha Rao')] };
  }
  else if (p.startsWith('/api/dependencies')) { depCalls.push(p);
    if (depFail) throw new Error('dependencies unavailable');
    out = { rows: depRows }; }
  else if (p.startsWith('/api/pods')) out = { pods: ['P', 'Q'] };
  else if (p.startsWith('/api/link-audit')) {
    posted.push(body);
    out = body.fix
      ? { checked: 6, reversed: auditReversed, fixed: auditReversed, broken: auditBroken }
      : { checked: 6, reversed: auditReversed, fixed: [], broken: [] };
  }
  else if (p.startsWith('/api/unlink-epic')) {
    posted.push(body);
    out = unlinkFails
      ? { __error__: `${body.initiative} has 2 linked epics (BOARD-1, BOARD-9) — say which one` }
      : { ok: true, initiative: body.initiative,
          epic: { key: body.epicKey, url: 'u/' + body.epicKey, summary: 'kept epic' } };
  }
  else if (p.startsWith('/api/link-epic')) {
    posted.push(body);
    out = body.mode === 'preview' ? linkPreview
      : { status: body.mode === 'create' ? 'created' : 'linked',
          epic: { key: body.mode === 'create' ? 'BOARD-NEW' : body.epicKey,
                  url: 'u/x', summary: 'linked epic' } };
  }
  else if (p.startsWith('/api/me')) out = { accountId: 'acc-me', name: 'Sam Lee', email: 'd@x.com' };
  else if (p.startsWith('/api/planner-fields')) out = { fields: EDITABLE_FIELDS };
  else if (p.startsWith('/api/user-search')) out = { users: [
    { accountId: 'acc-1', label: 'Asha Rao', email: 'pv@x.com' },
    { accountId: 'acc-2', label: 'Padma Rao', email: 'pr@x.com' }] };
  else if (p.startsWith('/api/field-meta')) {
    metaCalls.push(body);
    out = { key: body.key, fields: Object.fromEntries(
      body.columns.map(c => [c, FIELD_META[c] || { editable: false, why: 'unknown column' }])) };
  }
  else if (p.startsWith('/api/set-field')) {
    posted.push(body);
    if (writeFails) out = { __error__: "Field cannot be set" };
    else {
      const m = COL_TARGET[body.column];
      const owner = INITS.initiatives.find(i =>
        m.scope === 'init' ? i.key === body.key : (i.epic && i.epic.key === body.key));
      const from = owner ? (m.scope === 'init' ? owner[m.attr] : owner.epic[m.attr]) : '';
      const display = displayFor(body.column, body.value);
      out = { ok: true, key: body.key, column: body.column, from, to: body.value,
              display, undoable: FIELD_META[body.column].kind !== 'transition',
              unchanged: false };
    }
  }
  else if (p.startsWith('/api/submit-budgets')) {
    posted.push(body);
    const cur = {};
    INITS.initiatives.forEach(i => { if (i.epic) cur[i.epic.key] = i.budgets; });
    const diffs = [];
    Object.entries(body.epicBudgets).forEach(([ek, mb]) => body.months.forEach(m => {
      const to = Math.round((+(mb[m] || 0)) * 10) / 10;
      const frm = Math.round((+((cur[ek] || {})[m]) || 0) * 10) / 10;
      if (to !== frm) diffs.push({ epic: ek, month: m, from: frm, to });
    }));
    if (body.dryRun) out = { dryRun: true, diffs };
    else {
      diffs.forEach(d => { cur[d.epic][d.month] = d.to; });
      out = { dryRun: false, diffs,
        applied: [...new Set(diffs.map(d => d.epic))].map(e => ({ epic: e, ok: true })) };
    }
  }
  return { json: async () => out };
}

const dom = new JSDOM(fs.readFileSync(HTML, 'utf8'), {
  runScripts: 'dangerously', url: 'http://localhost/',
  beforeParse(window) {
    window.localStorage.setItem('monthlyPlanner_v1', JSON.stringify(ST0));
    window.fetch = fetchStub;
    const RealDate = window.Date;
    class FrozenDate extends RealDate {
      constructor(...a) { super(...(a.length ? a : [FIXED_MS])); }
      static now() { return FIXED_MS; }
    }
    window.Date = FrozenDate;
  },
});
const w = dom.window;

const fail = [];
let ran = 0;
const check = (name, cond, got) => {
  ran++;
  console.log(`${cond ? '  ok' : 'FAIL'}  ${name}${cond ? '' : '  -> ' + got}`);
  if (!cond) fail.push(name);
};
const txt = sel => { const e = w.document.querySelector(sel); return e ? e.textContent.trim() : '(missing)'; };
const btnFor = k => [...w.document.querySelectorAll('.subbtn')].find(b => b.dataset.k === k);
const rowFor = k => [...w.document.querySelectorAll('tr.row')].find(r => r.dataset.k === k);
const headers = () => [...w.document.querySelectorAll('thead th')].map(t => t.textContent.trim());
const xcellsOf = k => [...rowFor(k).querySelectorAll('td.xcol')];
const colIdx = label => headers().indexOf(label) - 4;   // 4 fixed cols before the optional ones
const xcell = (k, label) => xcellsOf(k)[colIdx(label)];
const tick = id => { const cb = w.document.querySelector(`#ddCols .menu input[value="${id}"]`);
  cb.checked = true; cb.dispatchEvent(new w.Event('change', { bubbles: true })); };
const renderNow = () => w.eval('renderBoard()');

(async () => {
  await new Promise(r => w.addEventListener('load', r));
  await new Promise(r => setTimeout(r, 300));

  console.log('\n--- 1. pending vs submitted state ---');
  check('header shows pending count', txt('#submit') === '⤴ Submit all (1)', txt('#submit'));
  check('IDEA-2 offers Submit (1)', btnFor('IDEA-2').textContent === '⤴ Submit (1)', btnFor('IDEA-2').textContent);
  check('IDEA-2 enabled', btnFor('IDEA-2').disabled === false, btnFor('IDEA-2').disabled);
  check('IDEA-1 shows in Jira', btnFor('IDEA-1').textContent === '✓ in Jira', btnFor('IDEA-1').textContent);
  check('IDEA-1 disabled', btnFor('IDEA-1').disabled === true, '');
  check('IDEA-3 flagged stale', btnFor('IDEA-3').textContent === '⚠ stale in Jira', btnFor('IDEA-3').textContent);
  check('shared epic rows in sync', btnFor('IDEA-6').textContent === '✓ in Jira', btnFor('IDEA-6').textContent);
  check('no-epic row has no submit', btnFor('IDEA-4') === undefined, 'button present');
  const dirty = [...w.document.querySelectorAll('.mcell input.dirty')].map(i => i.dataset.k + '/' + i.dataset.m);
  check('only IDEA-2 Oct cell marked dirty', JSON.stringify(dirty) === '["IDEA-2/Oct"]', JSON.stringify(dirty));

  console.log('\n--- 2. one-click submit: no confirm()/alert(), toast + Undo ---');
  let confirmed = 0, alerted = 0;
  w.confirm = () => { confirmed++; return true; };
  w.alert = () => { alerted++; };
  btnFor('IDEA-2').dispatchEvent(new w.Event('click', { bubbles: true }));
  await new Promise(r => setTimeout(r, 200));
  check('no confirm() shown', confirmed === 0, confirmed);
  check('no alert() shown', alerted === 0, alerted);
  check('exactly one write posted', posted.filter(p => !p.dryRun).length === 1, JSON.stringify(posted));
  check('no dry-run round-trip', posted.filter(p => p.dryRun).length === 0, posted.length);
  const toastEl = w.document.querySelector('.toast');
  check('toast rendered', !!toastEl && /Updated BOARD-2/.test(toastEl.textContent), toastEl && toastEl.textContent);
  check('toast offers Undo', !!toastEl && toastEl.querySelector('button')?.textContent === 'Undo', '');
  check('row flips to in Jira', btnFor('IDEA-2').textContent === '✓ in Jira', btnFor('IDEA-2').textContent);
  check('header goes to all submitted', txt('#submit') === '✓ All submitted', txt('#submit'));

  console.log('\n--- 3. Undo restores the previous Jira value ---');
  toastEl.querySelector('button').dispatchEvent(new w.Event('click', { bubbles: true }));
  await new Promise(r => setTimeout(r, 200));
  check('undo wrote 7 -> 5 back', posted[posted.length - 1].epicBudgets['BOARD-2'].Oct === 5,
        JSON.stringify(posted[posted.length - 1].epicBudgets));
  check('row pending again', btnFor('IDEA-2').textContent === '⤴ Submit (1)', btnFor('IDEA-2').textContent);

  console.log('\n--- 4. bulk submit all ---');
  const before = posted.length;
  w.document.getElementById('submit').dispatchEvent(new w.Event('click', { bubbles: true }));
  await new Promise(r => setTimeout(r, 200));
  check('bulk posted once', posted.length === before + 1, posted.length - before);
  check('bulk sent only the pending epic',
        JSON.stringify(Object.keys(posted[posted.length - 1].epicBudgets)) === '["BOARD-2"]',
        JSON.stringify(Object.keys(posted[posted.length - 1].epicBudgets)));
  check('bulk needed no confirm', confirmed === 0, confirmed);

  console.log('\n--- 5. column picker: initiative fields ---');
  const colBtn = w.document.querySelector('#ddCols button');
  check('picker button present', colBtn.textContent === '＋ Columns', colBtn.textContent);
  colBtn.dispatchEvent(new w.Event('click', { bubbles: true }));
  const groups = [...w.document.querySelectorAll('#ddCols .menu .grp')].map(g => g.textContent);
  check('menu grouped Initiative/Epic/Plan',
        JSON.stringify(groups) === '["Initiative","Epic","Plan"]', JSON.stringify(groups));
  check('every column has a picker entry',
        w.document.querySelectorAll('#ddCols .menu input').length === w.eval('EXTRA_COLS.length'),
        w.document.querySelectorAll('#ddCols .menu input').length);
  const headBefore = headers().length;
  tick('status'); tick('engDri');
  check('two columns added', headers().length === headBefore + 2, headers().join('|'));
  check('picker button shows count', w.document.querySelector('#ddCols button').textContent === '＋ Columns (2)',
        w.document.querySelector('#ddCols button').textContent);
  check('Eng DRI value rendered', xcell('IDEA-1', 'Eng DRI').textContent === 'Sam Lee',
        xcell('IDEA-1', 'Eng DRI').textContent);
  check('Eng DRI dropped from summary cell', !rowFor('IDEA-1').querySelector('.dri')?.textContent.includes('Eng'),
        rowFor('IDEA-1').querySelector('.dri')?.textContent);

  console.log('\n--- 6. column picker: epic-level fields ---');
  ['epicDue', 'epicStatus', 'epicOwner', 'epicHealth', 'epicLabels', 'epicChal'].forEach(tick);
  check('cell count matches header count',
        rowFor('IDEA-1').querySelectorAll('td').length === headers().length,
        rowFor('IDEA-1').querySelectorAll('td').length + ' vs ' + headers().length);
  // due-date rendering + editing is covered in 6b (it is an input, not static text)
  check('epic status rendered', xcell('IDEA-1', 'Epic status').textContent === 'In Progress',
        xcell('IDEA-1', 'Epic status').textContent);
  check('epic assignee rendered', xcell('IDEA-1', 'Epic assignee').textContent === 'Asha Rao',
        xcell('IDEA-1', 'Epic assignee').textContent);
  check('epic health rendered', xcell('IDEA-1', 'Epic health').textContent === 'On Track',
        xcell('IDEA-1', 'Epic health').textContent);
  check('challenges rendered', xcell('IDEA-1', 'Challenges').textContent === 'Dependency on NEWSYS',
        xcell('IDEA-1', 'Challenges').textContent);
  check('no Overall budget column offered',   // its Jira field id is stale — see monthly.html
        !w.eval('EXTRA_COLS.some(c=>c.id==="epicBudget")'), 'epicBudget still listed');
  check('zero-ish epic fields degrade to dash', xcell('IDEA-2', 'Epic assignee').textContent === '—',
        xcell('IDEA-2', 'Epic assignee').textContent);
  check('detail row colspan still spans the table', (() => {
    const d = w.document.querySelector('tr.detail td');
    return d ? +d.getAttribute('colspan') === headers().length : true; })(), 'n/a');
  check('column choice persisted',
        JSON.parse(w.localStorage.getItem('monthlyPlanner_v1')).cols.includes('epicDue'), '');

  console.log('\n--- 6b. epic due date is editable ---');
  const dueOf = k => xcell(k, 'Epic due date').querySelector('input.duedate');
  check('due date is an input', dueOf('IDEA-1') && dueOf('IDEA-1').type === 'date',
        xcell('IDEA-1', 'Epic due date').innerHTML);
  check('input carries the current value', dueOf('IDEA-1').value === '2026-10-20', dueOf('IDEA-1').value);
  check('unset date marked, still editable',
        dueOf('IDEA-3').classList.contains('unset') && dueOf('IDEA-3').disabled === false,
        dueOf('IDEA-3').className);
  check('past-due input flagged', dueOf('IDEA-2').classList.contains('past'), dueOf('IDEA-2').className);
  check('after-window input flagged', dueOf('IDEA-6').classList.contains('late'), dueOf('IDEA-6').className);
  check('no epic -> no date input', xcell('IDEA-4', 'Epic due date').textContent === '—',
        xcell('IDEA-4', 'Epic due date').textContent);

  let n = posted.length;
  const d3 = dueOf('IDEA-3'); d3.value = '2026-11-28';
  d3.dispatchEvent(new w.Event('change', { bubbles: true }));
  await new Promise(r => setTimeout(r, 200));
  const duePost = posted[posted.length - 1];
  check('edit posts through the generic /api/set-field',
        duePost.key === 'BOARD-3' && duePost.column === 'epicDue' && duePost.value === '2026-11-28',
        JSON.stringify(duePost));
  check('one request per edit', posted.length === n + 1, posted.length - n);
  check('value persists after re-render', dueOf('IDEA-3').value === '2026-11-28', dueOf('IDEA-3').value);
  check('no longer marked unset', !dueOf('IDEA-3').classList.contains('unset'), dueOf('IDEA-3').className);
  const dueToast = [...w.document.querySelectorAll('.toast')].pop();
  check('toast names the change', /BOARD-3 Epic due date → 2026-11-28/.test(dueToast.textContent),
        dueToast.textContent);
  check('toast offers Undo', dueToast.querySelector('button')?.textContent === 'Undo', '');

  n = posted.length;
  dueToast.querySelector('button').dispatchEvent(new w.Event('click', { bubbles: true }));
  await new Promise(r => setTimeout(r, 200));
  check('undo restores the previous value', posted[posted.length - 1].value === '',
        JSON.stringify(posted[posted.length - 1]));
  check('input back to empty', dueOf('IDEA-3').value === '', dueOf('IDEA-3').value);

  console.log('\n--- 6c. every Jira-backed column edits inline ---');
  const cellOf = (k, label) => xcell(k, label);
  check('editable column marked', cellOf('IDEA-1', 'Epic health').classList.contains('editable'),
        cellOf('IDEA-1', 'Epic health').className);
  check('editable cell knows its issue', cellOf('IDEA-1', 'Epic health').dataset.issue === 'BOARD-1',
        cellOf('IDEA-1', 'Epic health').dataset.issue);
  check('initiative column targets the initiative',
        cellOf('IDEA-1', 'Status').dataset.issue === 'IDEA-1', cellOf('IDEA-1', 'Status').dataset.issue);
  check('epic column on a row with no epic is read-only',
        !cellOf('IDEA-4', 'Epic health').classList.contains('editable'),
        cellOf('IDEA-4', 'Epic health').className);

  // select
  metaCalls.length = 0;
  cellOf('IDEA-1', 'Epic health').dispatchEvent(new w.Event('click', { bubbles: true }));
  await new Promise(r => setTimeout(r, 120));
  check('click asks the server what is editable', metaCalls.length === 1, JSON.stringify(metaCalls));
  let sel = cellOf('IDEA-1', 'Epic health').querySelector('select.inline');
  check('select rendered with options', sel && sel.options.length === 3, sel && sel.options.length);
  check('current value preselected', sel.value === 'h1', sel.value);
  n = posted.length;
  sel.value = 'h2'; sel.dispatchEvent(new w.Event('change', { bubbles: true }));
  await new Promise(r => setTimeout(r, 150));
  check('select writes the option id',
        posted[posted.length - 1].column === 'epicHealth' && posted[posted.length - 1].value === 'h2',
        JSON.stringify(posted[posted.length - 1]));
  check('cell repaints with the new label', cellOf('IDEA-1', 'Epic health').textContent === '⚠️ At Risk',
        cellOf('IDEA-1', 'Epic health').textContent);
  check('write does not refetch editmeta', metaCalls.length === 1, metaCalls.length);

  // transition
  cellOf('IDEA-1', 'Status').dispatchEvent(new w.Event('click', { bubbles: true }));
  await new Promise(r => setTimeout(r, 120));
  sel = cellOf('IDEA-1', 'Status').querySelector('select.inline');
  check('status offers transitions', sel && sel.options.length === 3, sel && sel.options.length);
  sel.value = '31'; sel.dispatchEvent(new w.Event('change', { bubbles: true }));
  await new Promise(r => setTimeout(r, 150));
  check('transition posts its id', posted[posted.length - 1].value === '31',
        JSON.stringify(posted[posted.length - 1]));
  const trToast = [...w.document.querySelectorAll('.toast')].pop();
  check('transition offers no Undo (not reversible)', !trToast.querySelector('button'),
        trToast.textContent);

  // labels
  cellOf('IDEA-1', 'Epic labels').dispatchEvent(new w.Event('click', { bubbles: true }));
  await new Promise(r => setTimeout(r, 120));
  let inp = cellOf('IDEA-1', 'Epic labels').querySelector('input.inline');
  check('labels prefilled comma separated', inp && inp.value === 'cbs, sunset', inp && inp.value);
  inp.value = 'cbs, sunset, migration';
  inp.dispatchEvent(new w.Event('change', { bubbles: true }));
  await new Promise(r => setTimeout(r, 150));
  check('labels post as an array',
        JSON.stringify(posted[posted.length - 1].value) === '["cbs","sunset","migration"]',
        JSON.stringify(posted[posted.length - 1].value));

  // user picker
  cellOf('IDEA-1', 'Epic assignee').dispatchEvent(new w.Event('click', { bubbles: true }));
  await new Promise(r => setTimeout(r, 120));
  inp = cellOf('IDEA-1', 'Epic assignee').querySelector('input.userpick');
  check('user picker rendered', !!inp, cellOf('IDEA-1', 'Epic assignee').innerHTML);
  inp.value = 'Pad'; inp.dispatchEvent(new w.Event('input', { bubbles: true }));
  await new Promise(r => setTimeout(r, 400));
  const opts = cellOf('IDEA-1', 'Epic assignee').querySelectorAll('.uopt[data-id]');
  check('typeahead lists matches', opts.length === 2, opts.length);
  n = posted.length;
  opts[0].dispatchEvent(new w.Event('mousedown', { bubbles: true }));
  await new Promise(r => setTimeout(r, 200));
  check('picking a user posts the accountId, not the name',
        posted[posted.length - 1].value === 'acc-1', JSON.stringify(posted[posted.length - 1]));

  // blurring a half-typed name must not clear the assignee
  cellOf('IDEA-1', 'Epic assignee').dispatchEvent(new w.Event('click', { bubbles: true }));
  await new Promise(r => setTimeout(r, 120));
  inp = cellOf('IDEA-1', 'Epic assignee').querySelector('input.userpick');
  n = posted.length;
  inp.value = 'Pad';
  inp.dispatchEvent(new w.Event('blur', { bubbles: true }));
  await new Promise(r => setTimeout(r, 250));
  check('blur without a pick writes nothing', posted.length === n, JSON.stringify(posted.slice(n)));

  // a column Jira says is not editable
  cellOf('IDEA-1', 'Challenges').dispatchEvent(new w.Event('click', { bubbles: true }));
  await new Promise(r => setTimeout(r, 150));
  check('non-editable column says why, writes nothing',
        [...w.document.querySelectorAll('.toast.warn')].some(t => /not editable on BOARD-1/.test(t.textContent)),
        [...w.document.querySelectorAll('.toast')].map(t => t.textContent).join(' | '));
  check('no control opened for it', !cellOf('IDEA-1', 'Challenges').querySelector('select, input'), '');

  // failed write
  writeFails = true;
  n = posted.length;
  const metaBefore = metaCalls.length;
  cellOf('IDEA-1', 'Epic health').dispatchEvent(new w.Event('click', { bubbles: true }));
  await new Promise(r => setTimeout(r, 120));
  check('stale editmeta was dropped, so reopening refetches', metaCalls.length === metaBefore + 1,
        metaCalls.length - metaBefore);
  sel = cellOf('IDEA-1', 'Epic health').querySelector('select.inline');
  sel.value = 'h1'; sel.dispatchEvent(new w.Event('change', { bubbles: true }));
  await new Promise(r => setTimeout(r, 200));
  check('failed write surfaces the Jira error',
        [...w.document.querySelectorAll('.toast.bad')].some(t => /Field cannot be set/.test(t.textContent)),
        [...w.document.querySelectorAll('.toast.bad')].map(t => t.textContent).join(' | '));
  check('cell reverts to the stored value',
        cellOf('IDEA-1', 'Epic health').textContent === '⚠️ At Risk',
        cellOf('IDEA-1', 'Epic health').textContent);
  writeFails = false;

  console.log('\n--- 6d. months are plain numbers, no tick box ---');
  const numFor = (k, m) => [...w.document.querySelectorAll('.mcell input[type=number]')]
    .find(i => i.dataset.k === k && i.dataset.m === m);
  check('no checkboxes anywhere', w.document.querySelectorAll('.mcell input[type=checkbox]').length === 0,
        w.document.querySelectorAll('.mcell input[type=checkbox]').length);
  check('unplanned month shows Jira value as placeholder',
        numFor('IDEA-3', 'Oct').placeholder === '12', numFor('IDEA-3', 'Oct').placeholder);
  check('that input is editable, not disabled', numFor('IDEA-3', 'Oct').disabled === false, '');
  check('placeholder explains itself', /Jira holds 12 SP/.test(numFor('IDEA-3', 'Oct').title),
        numFor('IDEA-3', 'Oct').title);
  check('planned month shows its value', numFor('IDEA-1', 'Oct').value === '30', numFor('IDEA-1', 'Oct').value);
  check('month with no Jira budget stays blank',
        numFor('IDEA-3', 'Nov').placeholder === '', numFor('IDEA-3', 'Nov').placeholder);

  // typing plans the month; clearing unplans it
  const m3 = numFor('IDEA-3', 'Oct');
  m3.value = '4'; m3.dispatchEvent(new w.Event('input', { bubbles: true }));
  await new Promise(r => setTimeout(r, 60));
  check('typing plans the month', w.eval('ST.plan["IDEA-3"].Oct.on') === true, w.eval('ST.plan["IDEA-3"].Oct.on'));
  check('value stored', w.eval('ST.plan["IDEA-3"].Oct.sp') === 4, w.eval('ST.plan["IDEA-3"].Oct.sp'));
  m3.value = '0'; m3.dispatchEvent(new w.Event('input', { bubbles: true }));
  await new Promise(r => setTimeout(r, 60));
  check('explicit 0 stays planned (clears the Jira budget)',
        w.eval('ST.plan["IDEA-3"].Oct.on') === true && w.eval('ST.plan["IDEA-3"].Oct.sp') === 0,
        w.eval('JSON.stringify(ST.plan["IDEA-3"].Oct)'));
  m3.value = ''; m3.dispatchEvent(new w.Event('input', { bubbles: true }));
  await new Promise(r => setTimeout(r, 60));
  check('clearing unplans the month', w.eval('ST.plan["IDEA-3"].Oct.on') === false,
        w.eval('ST.plan["IDEA-3"].Oct.on'));

  console.log('\n--- 6e. linking an epic is one picker, not a confirm() chain ---');
  confirmed = 0; alerted = 0;
  const linkBtn = () => [...w.document.querySelectorAll('.linkbtn:not(.retry)')]
    .find(b => b.dataset.k === 'IDEA-4');
  linkPreview = { status: 'preview', initiativeSummary: 'IDEA-4 summary', matches: [
    { key: 'BOARD-90', summary: 'IDEA-4 summary', status: 'To Do', exact: true },
    { key: 'BOARD-91', summary: 'Something close', status: 'In Progress', exact: false }] };
  linkBtn().dispatchEvent(new w.Event('click', { bubbles: true }));
  await new Promise(r => setTimeout(r, 200));
  let modal = w.document.querySelector('.modal-back');
  check('a picker opens', !!modal, 'no modal');
  check('no confirm() used', confirmed === 0, confirmed);
  check('every candidate listed at once',
        modal.querySelectorAll('.plist .pick').length === 2,
        modal.querySelectorAll('.plist .pick').length);
  check('exact match badged', !!modal.querySelector('.pbadge'), modal.innerHTML.slice(0, 200));
  check('create is an explicit choice, not a Cancel fall-through',
        !!modal.querySelector('.pick.create'), '');

  n = posted.length;
  modal.querySelector('.pick.create').dispatchEvent(new w.Event('click', { bubbles: true }));
  await new Promise(r => setTimeout(r, 200));
  check('create posts mode=create',
        posted[posted.length - 1].mode === 'create' && posted[posted.length - 1].initiative === 'IDEA-4',
        JSON.stringify(posted[posted.length - 1]));
  check('picker closed', !w.document.querySelector('.modal-back'), 'still open');
  check('no alert() used', alerted === 0, alerted);
  check('result is a toast',
        [...w.document.querySelectorAll('.toast')].some(t => /Created and linked BOARD-NEW to IDEA-4/.test(t.textContent)),
        [...w.document.querySelectorAll('.toast')].map(t => t.textContent).join(' | '));
  check('row now shows the epic', !!rowFor('IDEA-4').querySelector('.epic'),
        rowFor('IDEA-4').innerHTML.slice(0, 120));

  // cancelling must write nothing
  INITS.initiatives.find(i => i.key === 'IDEA-4').epic = null;
  renderNow();
  linkBtn().dispatchEvent(new w.Event('click', { bubbles: true }));
  await new Promise(r => setTimeout(r, 200));
  n = posted.length;
  w.document.querySelector('.mcancel').dispatchEvent(new w.Event('click', { bubbles: true }));
  await new Promise(r => setTimeout(r, 120));
  check('cancel closes and writes nothing',
        !w.document.querySelector('.modal-back') && posted.length === n,
        JSON.stringify(posted.slice(n)));

  // Escape also cancels
  linkBtn().dispatchEvent(new w.Event('click', { bubbles: true }));
  await new Promise(r => setTimeout(r, 200));
  n = posted.length;
  w.document.dispatchEvent(new w.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
  await new Promise(r => setTimeout(r, 120));
  check('Escape cancels', !w.document.querySelector('.modal-back') && posted.length === n, '');

  // no candidates at all: a one-button dialog is just a confirm(), so there must be none
  INITS.initiatives.find(i => i.key === 'IDEA-4').epic = null;
  renderNow();
  linkPreview = { status: 'preview', initiativeSummary: 'IDEA-4 summary', matches: [] };
  n = posted.length;
  linkBtn().dispatchEvent(new w.Event('click', { bubbles: true }));
  await new Promise(r => setTimeout(r, 250));
  check('no candidates -> no dialog at all', !w.document.querySelector('.modal-back'),
        'a one-option dialog was shown');
  check('it just creates', posted.some(x => x.mode === 'create'),
        JSON.stringify(posted.slice(n)));
  const mk = [...w.document.querySelectorAll('.toast')].pop();
  check('toast names the created epic', /Created and linked BOARD-NEW to IDEA-4/.test(mk.textContent),
        mk.textContent);
  check('toast offers unlink as the way out',
        /Undo \(unlink\)/.test(mk.querySelector('button')?.textContent || ''),
        mk.querySelector('button')?.textContent);
  n = posted.length;
  mk.querySelector('button').dispatchEvent(new w.Event('click', { bubbles: true }));
  await new Promise(r => setTimeout(r, 200));
  check('that undo unlinks the new epic',
        posted[posted.length - 1].initiative === 'IDEA-4' && posted[posted.length - 1].epicKey === 'BOARD-NEW',
        JSON.stringify(posted[posted.length - 1]));

  console.log('\n--- 6f. unlinking an epic ---');
  const unlinkBtn = k => [...w.document.querySelectorAll('.unlinkbtn')].find(b => b.dataset.k === k);
  check('linked row offers unlink', !!unlinkBtn('IDEA-1'), 'no unlink button');
  check('unlink names both sides', /Unlink BOARD-1 from IDEA-1/.test(unlinkBtn('IDEA-1').title),
        unlinkBtn('IDEA-1').title);
  check('unlink says the epic survives', /epic itself is kept/.test(unlinkBtn('IDEA-1').title),
        unlinkBtn('IDEA-1').title);
  check('unlinked row offers none', !unlinkBtn('IDEA-4'), 'unlink shown on a row with no epic');

  n = posted.length;
  unlinkBtn('IDEA-1').dispatchEvent(new w.Event('click', { bubbles: true }));
  await new Promise(r => setTimeout(r, 200));
  check('posts initiative + epic',
        posted[posted.length - 1].initiative === 'IDEA-1' && posted[posted.length - 1].epicKey === 'BOARD-1',
        JSON.stringify(posted[posted.length - 1]));
  check('one request', posted.length === n + 1, posted.length - n);
  check('row drops to link-epic', !!rowFor('IDEA-1').querySelector('.linkbtn'),
        rowFor('IDEA-1').innerHTML.slice(0, 150));
  check('epic columns go read-only for that row',
        !xcell('IDEA-1', 'Epic health').classList.contains('editable'),
        xcell('IDEA-1', 'Epic health').className);
  let ul = [...w.document.querySelectorAll('.toast')].pop();
  check('toast says the epic is untouched', /epic and its budgets are untouched/.test(ul.textContent),
        ul.textContent);
  check('toast offers Re-link', ul.querySelector('button')?.textContent === 'Re-link', '');

  n = posted.length;
  ul.querySelector('button').dispatchEvent(new w.Event('click', { bubbles: true }));
  await new Promise(r => setTimeout(r, 200));
  check('re-link posts mode=link with the same epic',
        posted[posted.length - 1].mode === 'link' && posted[posted.length - 1].epicKey === 'BOARD-1',
        JSON.stringify(posted[posted.length - 1]));
  check('row shows the epic again', !!rowFor('IDEA-1').querySelector('.epic'),
        rowFor('IDEA-1').innerHTML.slice(0, 150));

  unlinkFails = true;
  unlinkBtn('IDEA-1').dispatchEvent(new w.Event('click', { bubbles: true }));
  await new Promise(r => setTimeout(r, 200));
  check('ambiguous link is refused, not guessed',
        [...w.document.querySelectorAll('.toast.bad')].some(t => /say which one/.test(t.textContent)),
        [...w.document.querySelectorAll('.toast.bad')].map(t => t.textContent).join(' | '));
  check('row keeps its epic on failure', !!rowFor('IDEA-1').querySelector('.epic'),
        rowFor('IDEA-1').innerHTML.slice(0, 150));
  unlinkFails = false;

  console.log('\n--- 6g. Tech DRI filter ---');
  const setPods = (k, pods, dri) => { const it = INITS.initiatives.find(i => i.key === k);
    it.podTags = ['MY POD', ...pods]; it.engDri = dri; };
  setPods('IDEA-1', ['COREP - Core Platform'], 'Sam Lee');
  setPods('IDEA-2', ['COREP - Core Platform'], 'Someone Else');
  setPods('IDEA-3', [], 'Someone Else');
  setPods('IDEA-4', [], 'Someone Else');
  setPods('IDEA-6', ['BOPS - BRANCH-BANKING'], 'Sam Lee');
  setPods('IDEA-7', ['LEO - Liabilities'], 'Someone Else');
  renderNow();

  const driBtn = w.document.getElementById('dri');
  check('Tech DRI button present', driBtn.textContent === 'Tech DRI: anyone', driBtn.textContent);
  check('no group-by button (the /deps report replaces it)',
        !w.document.getElementById('groupby'), 'group button still present');
  check('no check-links button', !w.document.getElementById('auditlinks'), 'audit button still present');
  const allRows = [...w.document.querySelectorAll('tr.row')].length;

  driBtn.dispatchEvent(new w.Event('click', { bubbles: true }));
  await new Promise(r => setTimeout(r, 120));
  check('filter names me once identified', driBtn.textContent === 'Tech DRI: Sam Lee', driBtn.textContent);
  const keys = [...w.document.querySelectorAll('tr.row')].map(r => r.dataset.k);
  check('only my Tech DRI rows remain',
        JSON.stringify(keys) === '["IDEA-1","IDEA-6","IDEA-8"]' && keys.length < allRows,
        JSON.stringify(keys));
  check('rows still draggable', [...w.document.querySelectorAll('tr.row')]
        .every(r => r.getAttribute('draggable') === 'true'), '');
  driBtn.dispatchEvent(new w.Event('click', { bubbles: true }));
  await new Promise(r => setTimeout(r, 120));
  check('toggling back restores every row',
        [...w.document.querySelectorAll('tr.row')].length === allRows, '');

  console.log('\n--- 6g2. PODs column in the planner ---');
  INITS.initiatives.find(i => i.key === 'IDEA-1').podTags =
    ['MY POD', 'COREP - Core Platform', 'BOPS - BRANCH-BANKING'];
  renderNow();
  w.document.querySelector('#ddCols button').dispatchEvent(new w.Event('click', { bubbles: true }));
  check('PODs is offered as a column',
        !!w.document.querySelector('#ddCols .menu input[value="pods"]'), 'not in the picker');
  tick('pods');
  await new Promise(r => setTimeout(r, 100));
  const podCell = xcell('IDEA-1', 'PODs');
  const chips = c => [...c.querySelectorAll('.podchip')].map(x=>x.textContent.trim());
  check('PODs renders one chip per pod', chips(podCell).join(' ') === 'MY POD COREP BOPS',
        chips(podCell).join(' '));
  check('own pod is highlighted', !!podCell.querySelector('.podchip.own'), podCell.innerHTML);
  check('full labels kept in the tooltip',
        /COREP - Core Platform/.test(podCell.querySelector('.podwrap').title), podCell.innerHTML);

  // the real failure this replaced: two pods sharing a prefix collapsed to "BOARD, BOARD"
  INITS.initiatives.find(i => i.key === 'IDEA-2').podTags =
    ['BOARD - Core Payments Platform', 'BOARD - Core Ledger Platform', 'LEO - Liabilities'];
  renderNow();
  const dup = chips(xcell('IDEA-2', 'PODs'));
  check('colliding prefixes are disambiguated',
        dup.join(' ') === 'BOARD·Payments BOARD·Ledger LEO', dup.join(' '));
  check('no duplicate chip labels', new Set(dup).size === dup.length, dup.join(' '));

  // an initiative can carry 31 pods; the cell must not explode
  INITS.initiatives.find(i => i.key === 'IDEA-3').podTags =
    Array.from({length: 31}, (_, i) => `P${i} - Pod number ${i}`);
  renderNow();
  const many = xcell('IDEA-3', 'PODs');
  check('long pod lists are capped', chips(many).length === 7, chips(many).length);
  check('the overflow chip counts the rest',
        many.querySelector('.podchip.more').textContent.trim() === '+25',
        many.querySelector('.podchip.more').textContent);
  check('all 31 stay in the tooltip', many.querySelector('.podwrap').title.split('\n').length === 31,
        many.querySelector('.podwrap').title.split('\n').length);
  INITS.initiatives.find(i => i.key === 'IDEA-2').podTags = ['MY POD'];
  INITS.initiatives.find(i => i.key === 'IDEA-3').podTags = ['MY POD'];
  renderNow();
  check('PODs cell is editable', podCell.classList.contains('editable'), podCell.className);
  check('it targets the initiative', podCell.dataset.issue === 'IDEA-1', podCell.dataset.issue);

  n = posted.length;
  podCell.dispatchEvent(new w.Event('click', { bubbles: true }));
  await new Promise(r => setTimeout(r, 150));
  const podSel = podCell.querySelector('select.inline');
  check('multi-select opens with 33-style option list', !!podSel && podSel.multiple,
        podCell.innerHTML.slice(0, 120));
  check('current pods preselected', [...podSel.selectedOptions].map(o => o.value).join(',') === 'p1',
        [...podSel.selectedOptions].map(o => o.value).join(','));
  [...podSel.options].forEach(o => { o.selected = true; });
  podSel.dispatchEvent(new w.Event('change', { bubbles: true }));
  await new Promise(r => setTimeout(r, 200));
  check('edit posts the option ids as an array',
        JSON.stringify(posted[posted.length - 1].value) === '["p1","p2"]',
        JSON.stringify(posted[posted.length - 1]));
  check('cell repaints from the write',
        chips(xcell('IDEA-1', 'PODs')).join(' ') === 'MY POD COREP',
        chips(xcell('IDEA-1', 'PODs')).join(' '));

  console.log('\n--- 6h. reload from Jira confirms it happened ---');
  INITS.generated = '2026-09-01';
  renderNow();
  const ageEl = () => w.document.getElementById('dataage');
  check('stale data is labelled', /Jira data: 2026-09-01 — stale/.test(ageEl().textContent),
        ageEl().textContent);
  let freshAsked = false;
  const realFetch = w.fetch;
  w.fetch = async (p, o) => {
    if (String(p).includes('/api/initiatives') && String(p).includes('fresh=1')) {
      freshAsked = true;
      return { json: async () => ({ ...INITS, generated: TODAY }) };
    }
    return realFetch(p, o);
  };
  const rl = w.document.getElementById('recalc');
  rl.dispatchEvent(new w.Event('click', { bubbles: true }));
  await new Promise(r => setTimeout(r, 250));
  check('reload requests fresh=1', freshAsked, 'no fresh=1 request');
  check('button restored after reload', rl.disabled === false && rl.textContent === '↻ Reload from Jira',
        rl.textContent + '/' + rl.disabled);
  check('a toast confirms the reload',
        [...w.document.querySelectorAll('.toast')].some(t => /Reloaded from Jira/.test(t.textContent)),
        [...w.document.querySelectorAll('.toast')].map(t => t.textContent).join(' | '));
  check('toast names the previous build',
        [...w.document.querySelectorAll('.toast')].some(t => /was 2026-09-01/.test(t.textContent)), '');
  check('stale marker cleared', !/stale/.test(ageEl().textContent), ageEl().textContent);
  w.fetch = realFetch;

  console.log('\n--- 7. expanded detail row is never stuck on "Loading…" ---');
  detailCalls.length = 0;
  w.eval('ST.initOpen["IDEA-1"]=true; save(); DETAILS={}; renderBoard();');
  check('render kicks off the detail fetch', detailCalls.length === 1, JSON.stringify(detailCalls));
  await new Promise(r => setTimeout(r, 200));
  const det = [...w.document.querySelectorAll('tr.detail')].find(t => t.dataset.k === 'IDEA-1');
  check('detail resolves', det && /One Pager/.test(det.textContent), det && det.textContent.trim());
  check('no duplicate fetch on re-render',
        (() => { w.eval('renderBoard()'); return detailCalls.length === 1; })(), detailCalls.length);

  console.log('\n--- 8. stack-rank survives a pod filter change ---');
  w.eval('ST.order=["IDEA-6","IDEA-1","IDEA-2","IDEA-3","IDEA-4","IDEA-7","IDEA-8"]; save();');
  const ranked = JSON.parse(w.localStorage.getItem('monthlyPlanner_v1')).order.slice();
  POD_MODE = 'narrow';
  await w.eval('loadInitiatives(false)');
  await new Promise(r => setTimeout(r, 200));
  const narrowed = JSON.parse(w.localStorage.getItem('monthlyPlanner_v1')).order;
  check('keys outside the filter are kept', ranked.every(k => narrowed.includes(k)), JSON.stringify(narrowed));
  check('new pod keys appended', narrowed.includes('IDEA-9'), JSON.stringify(narrowed));
  POD_MODE = 'full';
  await w.eval('loadInitiatives(false)');
  await new Promise(r => setTimeout(r, 200));
  const back = JSON.parse(w.localStorage.getItem('monthlyPlanner_v1')).order;
  check('original rank restored exactly',
        JSON.stringify(back.slice(0, ranked.length)) === JSON.stringify(ranked), JSON.stringify(back));
  check('table renders in that order',
        JSON.stringify([...w.document.querySelectorAll('tr.row')].map(r => r.dataset.k)) === JSON.stringify(ranked),
        JSON.stringify([...w.document.querySelectorAll('tr.row')].map(r => r.dataset.k)));

  console.log('\n--- 9. Jira free text is escaped, never parsed as HTML ---');
  // Anyone who can edit an initiative in Jira controls these strings, and the board
  // injects them via innerHTML. Unescaped, a crafted summary would run script in the
  // planner, which can POST to /api/set-field and /api/submit-budgets as the user.
  const XSS = '<img src=x onerror="window.__pwned=1">';
  w.__pwned = undefined;
  w.eval(`INITS.initiatives.forEach(i => { if(i.key === 'IDEA-1'){
            i.summary = ${JSON.stringify(XSS)}; i.engDri = ${JSON.stringify(XSS)};
            i.prodDri = ${JSON.stringify(XSS)}; i.orgPriority = ${JSON.stringify(XSS)}; } });
          ST.cols = ['pri','eng','prod']; renderBoard();`);
  await new Promise(r => setTimeout(r, 50));
  const xrow = [...w.document.querySelectorAll('tr.row')].find(r => r.dataset.k === 'IDEA-1');
  check('no injected element in the row', xrow && xrow.querySelector('img') === null,
        xrow && xrow.innerHTML.slice(0, 140));
  check('payload survives as literal text', xrow && xrow.textContent.includes('<img src=x'),
        xrow && xrow.textContent.slice(0, 140));
  check('onerror never fired', w.__pwned === undefined, String(w.__pwned));

  // The expanded detail row renders Impact + Description, both Jira free text, and was the
  // spot the first pass of this fix missed.
  w.eval(`DETAILS['IDEA-1'] = { impact: ${JSON.stringify(XSS)},
                                descriptionText: ${JSON.stringify(XSS + ' a & b')} };
          ST.initOpen['IDEA-1'] = true; renderBoard();`);
  await new Promise(r => setTimeout(r, 50));
  const xdet = [...w.document.querySelectorAll('tr.detail')].find(t => t.dataset.k === 'IDEA-1');
  check('detail row parses no injected element', xdet && xdet.querySelector('img') === null,
        xdet && xdet.innerHTML.slice(0, 140));
  check('impact kept as literal text', xdet && xdet.textContent.includes('<img src=x'),
        xdet && xdet.textContent.slice(0, 100));
  check('ampersand in description survives intact', xdet && xdet.textContent.includes('a & b'),
        xdet && xdet.textContent.slice(-60));
  w.eval("ST.initOpen['IDEA-1']=false; DETAILS={}; renderBoard();");

  // Pod names are Jira multi-select option values and render into the filter menu.
  w.eval(`PODS = [${JSON.stringify(XSS)}]; renderPodMenu();`);
  const menu = w.document.querySelector('#ddPods .menu');
  check('pod filter menu parses no injected element', menu && menu.querySelector('img') === null,
        menu && menu.innerHTML.slice(0, 140));
  check('pod name kept as literal text', menu && menu.textContent.includes('<img src=x'),
        menu && menu.textContent.slice(0, 100));

  console.log('\n--- 10. archived ideas: visible, but never counted ---');
  // JPD archiving sets a field and leaves the status alone, so these used to slip past the
  // statusCategory filter and silently inflate the planned total.
  w.eval('ST.order=["IDEA-1","IDEA-2","IDEA-3","IDEA-4","IDEA-6","IDEA-7","IDEA-8"]; ST.driMe=false; save(); renderBoard();');
  await new Promise(r => setTimeout(r, 50));
  const arow = [...w.document.querySelectorAll('tr.row')].find(r => r.dataset.k === 'IDEA-8');
  check('archived row still renders', !!arow, 'missing');
  check('archived row is tagged', arow && !!arow.querySelector('.archtag'), arow && arow.innerHTML.slice(0, 120));
  check('archived row is styled as archived', arow && arow.classList.contains('archived'),
        arow && arow.className);
  // IDEA-8 plans 25 SP in Oct; every other Oct row sums to 66
  const octPlanned = w.eval('plannedFor("Oct")');
  check('archived budget is excluded from the planned total', octPlanned === 66, String(octPlanned));
  const eb = w.eval('JSON.stringify(Object.keys(gatherEpicBudgets()))');
  check('archived epic is never queued for submit', !JSON.parse(eb).includes('BOARD-8'), eb);

  console.log('\n--- 11. initiative delivery dates ---');
  w.eval('ST.cols=["goLive","handover"]; save(); renderBoard();');
  await new Promise(r => setTimeout(r, 50));
  const drow = [...w.document.querySelectorAll('tr.row')].find(r => r.dataset.k === 'IDEA-1');
  check('target go-live renders', drow && /2026-10-29/.test(drow.textContent), drow && drow.textContent);
  check('target handover renders', drow && /2026-10-09/.test(drow.textContent), drow && drow.textContent);
  const gcell = drow && drow.querySelector('[data-col="goLive"]');
  check('go-live cell is editable', gcell && gcell.classList.contains('editable'),
        gcell && gcell.className);
  check('go-live edits target the initiative, not the epic',
        gcell && gcell.dataset.issue === 'IDEA-1', gcell && gcell.dataset.issue);
  const erow = [...w.document.querySelectorAll('tr.row')].find(r => r.dataset.k === 'IDEA-2');
  check('an unset date shows a dash', erow && /—/.test(erow.textContent), erow && erow.textContent);

  console.log('\n--- 12. description links are clickable ---');
  w.eval('ST.initOpen["IDEA-1"]=true; DETAILS={}; renderBoard();');
  await new Promise(r => setTimeout(r, 200));
  const ldet = [...w.document.querySelectorAll('tr.detail')].find(t => t.dataset.k === 'IDEA-1');
  const anchors = ldet ? [...ldet.querySelectorAll('a')] : [];
  check('the one-pager renders as a real link', anchors.length === 1, String(anchors.length));
  check('link points at the source doc',
        anchors[0] && anchors[0].getAttribute('href') === 'https://example.com/wiki/the-doc',
        anchors[0] && anchors[0].getAttribute('href'));
  check('link text is the label, not the raw url',
        anchors[0] && anchors[0].textContent === 'The Doc', anchors[0] && anchors[0].textContent);
  check('link opens in a new tab safely',
        anchors[0] && /noopener/.test(anchors[0].getAttribute('rel') || ''),
        anchors[0] && anchors[0].getAttribute('rel'));
  // a javascript: href must be dropped, not rendered as an anchor
  check('javascript: href is not turned into a link',
        !anchors.some(a => /^javascript:/i.test(a.getAttribute('href') || '')),
        anchors.map(a => a.getAttribute('href')).join(' | '));
  check('its text is still shown', ldet && /bad link/.test(ldet.textContent), ldet && ldet.textContent);
  w.eval('ST.initOpen["IDEA-1"]=false; DETAILS={}; ST.cols=[]; save(); renderBoard();');

  console.log('\n--- 13. an initiative retagged in Jira leaves the view ---');
  // The PFMS/KFT case: Planning Cycle moved Oct -> Dec in Jira, but a local draft plan still
  // pinned it to Oct, so it never left the Oct view. Planning Cycle alone now decides.
  w.eval(`INITS.initiatives.find(i=>i.key==='IDEA-2').cycles = ['Dec-26'];
          ST.showAll = false; ST.driMe = false; REVEAL = false;
          ST.plan['IDEA-2'].Oct = {on:true, sp:7, seeded:true};
          save(); renderBoard();`);
  await new Promise(r => setTimeout(r, 50));
  const shown = () => [...w.document.querySelectorAll('tr.row')].map(r => r.dataset.k);
  check('retagged initiative is gone from the month view', !shown().includes('IDEA-2'),
        JSON.stringify(shown()));
  check('in-cycle rows are unaffected', shown().includes('IDEA-1'), JSON.stringify(shown()));
  // IDEA-2 drafted 7 SP in Oct; once it is out of the cycle that must leave the total,
  // otherwise the number has an invisible contributor.
  check('its draft SP leaves the planned total', w.eval('plannedFor("Oct")') === 59,
        String(w.eval('plannedFor("Oct")')));

  // it carries a draft, so it must be disclosed rather than vanishing silently
  const note = w.document.querySelector('.stranded');
  check('a note says something was hidden', !!note, 'no note');
  check('the note counts it', note && /1 initiative hidden/.test(note.textContent),
        note && note.textContent.trim());

  w.document.getElementById('revealStranded').dispatchEvent(new w.Event('click', {bubbles:true}));
  await new Promise(r => setTimeout(r, 50));
  check('Show them brings it back', shown().includes('IDEA-2'), JSON.stringify(shown()));

  // Revealed rows used to reappear at their rank position, interleaved with the cycle's own
  // rows and visually identical to them, so the button's effect was unfindable in a long
  // table. They now land last, as one banded run under a heading.
  const order = shown();
  check('the revealed row is LAST, not back at its rank',
        order[order.length - 1] === 'IDEA-2', JSON.stringify(order));
  const sep = w.document.querySelector('tr.offsep');
  check('a separator row bands the group off', !!sep, 'no tr.offsep');
  check('the separator counts the revealed rows',
        sep && /1 off-cycle initiative\b/.test(sep.textContent), sep && sep.textContent.trim());
  check('and names the cycle they are missing',
        sep && /Oct-26/.test(sep.textContent), sep && sep.textContent.trim());
  check('the separator sits immediately before the first revealed row',
        sep && sep.nextElementSibling && sep.nextElementSibling.dataset.k === 'IDEA-2',
        sep && sep.nextElementSibling && sep.nextElementSibling.dataset.k);
  check('the separator spans the whole table',
        sep && +sep.querySelector('td').getAttribute('colspan') ===
              w.document.querySelectorAll('#board thead th').length,
        sep && sep.querySelector('td').getAttribute('colspan'));
  const offRow = w.document.querySelector('tr.row[data-k="IDEA-2"]');
  check('the revealed row is tinted as off-cycle',
        offRow && offRow.classList.contains('offcycle'), offRow && offRow.className);
  check('in-cycle rows are NOT tinted',
        !w.document.querySelector('tr.row[data-k="IDEA-1"]').classList.contains('offcycle'),
        w.document.querySelector('tr.row[data-k="IDEA-1"]').className);
  check('the row carries the cycle it IS tagged for',
        offRow && /Dec-26/.test(offRow.querySelector('.offtag').textContent),
        offRow && offRow.querySelector('.offtag') && offRow.querySelector('.offtag').textContent);
  // the headline count is the cycle's own rows; revealing must not inflate it
  const cnote = w.document.querySelector('#board .note');
  check('the count line still reports the in-cycle rows only',
        cnote && /Showing 6 of 7 /.test(cnote.textContent), cnote && cnote.textContent.trim());
  check('and reports the revealed ones separately',
        cnote && /plus 1 off-cycle, listed at the bottom/.test(cnote.textContent),
        cnote && cnote.textContent.trim());
  // revealing is a view, never a capacity change
  check('revealing does not move the planned total', w.eval('plannedFor("Oct")') === 59,
        String(w.eval('plannedFor("Oct")')));

  w.document.getElementById('revealStranded').dispatchEvent(new w.Event('click', {bubbles:true}));
  await new Promise(r => setTimeout(r, 50));
  check('toggling again hides it', !shown().includes('IDEA-2'), JSON.stringify(shown()));
  check('and takes the separator with it', !w.document.querySelector('tr.offsep'),
        'tr.offsep survived the hide');

  w.document.getElementById('clearStranded').dispatchEvent(new w.Event('click', {bubbles:true}));
  await new Promise(r => setTimeout(r, 50));
  check('clearing drops the draft entry', w.eval('ST.plan["IDEA-2"].Oct.on') === false,
        JSON.stringify(w.eval('JSON.stringify(ST.plan["IDEA-2"])')));
  // the note stays, but now for the right reason: IDEA-2 still has Oct=5 filed in Jira,
  // and a filed budget must never be hidden without saying so
  const n1 = w.document.querySelector('.stranded');
  check('it is still disclosed because Jira holds a budget for it',
        n1 && /budget already filed/.test(n1.textContent), n1 && n1.textContent.trim());
  check('an undo is offered',
        [...w.document.querySelectorAll('.toast')].some(t => /Cleared/.test(t.textContent)),
        [...w.document.querySelectorAll('.toast')].map(t => t.textContent).join(' | '));

  // a filed Jira budget must keep the row disclosed even with no local draft at all
  w.eval(`INITS.initiatives.find(i=>i.key==='IDEA-3').cycles = ['Dec-26'];
          INITS.initiatives.find(i=>i.key==='IDEA-3').budgets.Oct = 12;
          ST.plan['IDEA-3'].Oct = {on:false, sp:0, seeded:true}; save(); renderBoard();`);
  await new Promise(r => setTimeout(r, 50));
  const n2 = w.document.querySelector('.stranded');
  check('a filed Jira budget is never hidden silently',
        n2 && /budget already filed/.test(n2.textContent), n2 && n2.textContent.trim());

  console.log('\n--- 14. a wide column set scrolls instead of clipping ---');
  // With enough optional columns the table outgrows the panel. It used to spill out and the
  // action column was clipped away entirely, putting Submit out of reach.
  w.eval('ST.cols=["cycles","goLive","epicDue","pods"]; ST.showAll=true; save(); renderBoard();');
  await new Promise(r => setTimeout(r, 50));
  const wrap = w.document.querySelector('#board .tablewrap');
  check('the table sits in a scroll wrapper', !!wrap, 'no .tablewrap');
  check('the wrapper holds the table', !!(wrap && wrap.querySelector('table')), 'table not inside');
  const actTh = w.document.querySelector('#board thead th.act');
  check('the action column header is pinnable', !!actTh, 'no th.act');
  const anyRow = w.document.querySelector('#board tr.row');
  check('every row still has its action cell', !!(anyRow && anyRow.querySelector('td.act')),
        anyRow && anyRow.innerHTML.slice(-120));
  check('the note stays outside the scroll area',
        !!(w.document.querySelector('#board .note') && !wrap.querySelector('.note')), 'note inside wrapper');
  w.eval('ST.cols=[]; ST.showAll=false; save(); renderBoard();');

  console.log('\n--- 15. schedule lane sits on the team calendar ---');
  // Bars are laid over real working days, so weekends and public holidays push the end out.
  // rate = teamSP/teamNetDays = 40/40 = 1 SP per person-day, so 1 dev clears 1 SP per day.
  w.eval('ST.months=["2026-10"]; ST.showAll=true; ST.driMe=false; ST.schOpen=true;'
       + 'ST.heads={}; save();');
  await w.eval('loadCaps()');
  await new Promise(r => setTimeout(r, 120));
  const sched = w.document.getElementById('sched');
  check('the lane renders', !!sched.querySelector('table.sch'), sched.innerHTML.slice(0, 120));
  check('it uses one column per calendar day',
        sched.querySelectorAll('thead th.gd').length === 31,
        String(sched.querySelectorAll('thead th.gd').length));
  check('the public holiday column is marked',
        !!sched.querySelector('thead th.gd.hol'), 'no holiday header');

  const r1 = [...sched.querySelectorAll('tbody tr')]
    .find(t => /IDEA-1\b/.test(t.querySelector('.sname').textContent));
  check('a planned initiative gets a row', !!r1, 'IDEA-1 row missing');
  const cells = r1 ? [...r1.querySelectorAll('td.sc')] : [];
  const barOn = cells.map(c => !!c.querySelector('.sbar'));
  // IDEA-1 plans 30 SP in Oct at 1 dev = 30 working days, but October only has 20,
  // so it must run to the end of the window and report the spill.
  check('no bar on the weekend', !barOn[2] && !barOn[3], JSON.stringify(barOn.slice(0, 6)));
  check('no bar on the public holiday (Oct 2)', !barOn[1], JSON.stringify(barOn.slice(0, 4)));
  check('bars do fall on working days', barOn[0] && barOn[4], JSON.stringify(barOn.slice(0, 6)));
  check('an over-long plan reports the spill',
        /SP spills/.test(r1.querySelector('.sname').textContent),
        r1.querySelector('.sname').textContent.trim());

  // more people must finish it sooner
  const daysAt1 = barOn.filter(Boolean).length;
  w.eval('ST.heads={"IDEA-1":3}; save(); renderSchedule();');
  await new Promise(r => setTimeout(r, 60));
  const r2 = [...w.document.querySelectorAll('#sched tbody tr')]
    .find(t => /IDEA-1\b/.test(t.querySelector('.sname').textContent));
  const daysAt3 = [...r2.querySelectorAll('td.sc')].filter(c => c.querySelector('.sbar')).length;
  check('adding people shortens the bar', daysAt3 < daysAt1, `${daysAt1} -> ${daysAt3} days`);
  check('headcount shows on the row', /\b3\b/.test(r2.querySelector('.heads').textContent),
        r2.querySelector('.heads').textContent);
  console.log('\n--- 15b. dragging a bar moves its start day ---');
  // Every bar used to begin on day 1 of the window, so nothing could be sequenced behind
  // anything else and "past due" was an artefact of 24 initiatives starting the same morning.
  // The pointer handling needs a layout engine jsdom does not have (no elementFromPoint, no
  // pointer capture), so the SHIPPED commit function is driven directly rather than faked.
  w.eval('ST.heads={"IDEA-1":1}; ST.schStart={}; save(); renderSchedule();');
  await new Promise(r => setTimeout(r, 60));
  const schRowOf = k => [...w.document.querySelectorAll('#sched tbody tr')]
    .find(t => new RegExp(k + '\\b').test(t.querySelector('.sname').textContent));
  const firstBarIdx = k => {
    const tr = schRowOf(k);
    return tr ? [...tr.querySelectorAll('td.sc')].findIndex(c => c.querySelector('.sbar')) : -99;
  };
  const autoIdx = firstBarIdx('IDEA-1');
  check('it starts at the top of the window by default', autoIdx === 0, String(autoIdx));
  check('no reset pip while it is on its default',
        !schRowOf('IDEA-1').querySelector('.spin'), 'a .spin was shown');

  // Oct 1 is a Thursday here, Oct 2 a public holiday, Oct 3/4 the weekend — so dropping on
  // index 2 must land on Monday the 5th, not on a day the bar could never start.
  w.eval('schCommitStart("IDEA-1", 2, 0, schDays());');
  await new Promise(r => setTimeout(r, 60));
  check('the drop snaps forward off the weekend',
        w.eval('ST.schStart["IDEA-1"]') === '2026-10-05', w.eval('ST.schStart["IDEA-1"]'));
  check('and the bar actually moved there', firstBarIdx('IDEA-1') === 4,
        String(firstBarIdx('IDEA-1')));
  check('the moved start is persisted',
        /"IDEA-1":"2026-10-05"/.test(w.localStorage.getItem(w.eval('LSKEY')) || ''),
        'not in localStorage');
  const pip = schRowOf('IDEA-1').querySelector('.spin');
  check('a reset pip appears', !!pip, 'no .spin');
  check('the pip names the day it was moved to',
        pip && /2026-10-05/.test(pip.getAttribute('title') || ''), pip && pip.getAttribute('title'));

  // a later start means fewer days left in the window, so more work spills out of it
  check('starting later spills more SP',
        /SP spills/.test(schRowOf('IDEA-1').querySelector('.sname').textContent),
        schRowOf('IDEA-1').querySelector('.sname').textContent.trim());

  // dropping it back where it started must CLEAR the pin, not freeze today's date there:
  // a frozen date stops tracking the budget if the initiative is later retagged
  w.eval('schCommitStart("IDEA-1", 0, 0, schDays());');
  await new Promise(r => setTimeout(r, 60));
  check('dropping it back on its default clears the pin',
        w.eval('ST.schStart["IDEA-1"] === undefined'),
        JSON.stringify(w.eval('JSON.stringify(ST.schStart)')));
  check('rather than freezing the date there',
        !schRowOf('IDEA-1').querySelector('.spin'), 'a .spin survived');

  // the reset pip is the manual way back
  w.eval('schCommitStart("IDEA-1", 6, 0, schDays());');
  await new Promise(r => setTimeout(r, 60));
  check('moved again', !!w.eval('ST.schStart["IDEA-1"]'), 'not pinned');
  schRowOf('IDEA-1').querySelector('.spin').dispatchEvent(new w.Event('click', {bubbles:true}));
  await new Promise(r => setTimeout(r, 60));
  check('the reset pip puts it back', w.eval('ST.schStart["IDEA-1"] === undefined'),
        JSON.stringify(w.eval('JSON.stringify(ST.schStart)')));
  check('and the bar returns to the top of the window', firstBarIdx('IDEA-1') === 0,
        String(firstBarIdx('IDEA-1')));

  // a start day stored as a DATE survives a window change; one that falls outside the
  // window must not vanish silently, or the stored value would be unreachable
  w.eval('ST.schStart={"IDEA-1":"2026-12-09"}; save(); renderSchedule();');
  await new Promise(r => setTimeout(r, 60));
  check('an out-of-window start falls back to the default', firstBarIdx('IDEA-1') === 0,
        String(firstBarIdx('IDEA-1')));
  const lost = schRowOf('IDEA-1').querySelector('.spin');
  check('but it is still shown, with its date', lost && /2026-12-09/.test(lost.textContent),
        lost && lost.textContent);
  check('and says why the bar ignored it',
        lost && /outside the months/.test(lost.getAttribute('title') || ''),
        lost && lost.getAttribute('title'));

  // moving one bar must never move another
  w.eval('ST.schStart={"IDEA-1":"2026-10-12"}; save(); renderSchedule();');
  await new Promise(r => setTimeout(r, 60));
  check('other rows keep their own start',
        firstBarIdx('IDEA-3') === 0 || firstBarIdx('IDEA-3') === -99,
        String(firstBarIdx('IDEA-3')));

  w.eval('ST.schOpen=false; ST.heads={}; ST.schStart={}; ST.showAll=false; save(); renderBoard();');

  console.log('\n--- 16. choosing the cycle window from the UI ---');
  // A planning cycle is two sprints, so its window is not the calendar month. The default
  // comes from the server; picking dates here overrides it and must refetch the capacity.
  w.eval('ST.months=["2026-10"]; ST.win={}; ST.winOpen=null; ST.capOpen={}; save(); CAPS={};');
  await w.eval('loadCaps()');
  await new Promise(r => setTimeout(r, 80));
  const winEl = () => w.document.querySelector('#strip .win');
  check('the window is shown on the capacity card', !!winEl(),
        w.document.getElementById('strip').textContent.slice(0, 90));
  check('it is not marked custom by default',
        winEl() && !winEl().classList.contains('custom'), winEl() && winEl().className);

  winEl().dispatchEvent(new w.Event('click', {bubbles:true}));
  await new Promise(r => setTimeout(r, 60));
  const box = w.document.querySelector('#strip .wined');
  check('clicking it opens the date editor', !!box, 'no editor');
  check('it is prefilled from the current window',
        box && box.querySelector('.wfrom').value === '2026-10-01', box && box.querySelector('.wfrom').value);
  check('Reset is disabled while the window is the default',
        box && box.querySelector('.wreset').disabled, 'reset enabled');

  // an end before the start must be refused, not silently applied
  box.querySelector('.wfrom').value = '2026-10-20';
  box.querySelector('.wto').value   = '2026-10-05';
  box.querySelector('.wapply').dispatchEvent(new w.Event('click', {bubbles:true}));
  await new Promise(r => setTimeout(r, 60));
  check('a backwards window is refused',
        !((w.eval('JSON.stringify(ST.win)') || '{}').includes('2026-10-20')),
        w.eval('JSON.stringify(ST.win)'));
  check('and it says why',
        [...w.document.querySelectorAll('.toast')].some(t => /before the start/.test(t.textContent)),
        [...w.document.querySelectorAll('.toast')].map(t => t.textContent).join(' | '));

  const callsBefore = monthCalls.length;
  box.querySelector('.wfrom').value = '2026-10-07';
  box.querySelector('.wto').value   = '2026-10-20';
  box.querySelector('.wapply').dispatchEvent(new w.Event('click', {bubbles:true}));
  await new Promise(r => setTimeout(r, 120));
  check('the chosen window is stored',
        /2026-10-07/.test(w.eval('JSON.stringify(ST.win)')), w.eval('JSON.stringify(ST.win)'));
  check('capacity is refetched for that window',
        monthCalls.length > callsBefore &&
        /start=2026-10-07.*end=2026-10-20/.test(monthCalls[monthCalls.length-1]),
        monthCalls[monthCalls.length-1]);
  check('the card marks the window as custom',
        winEl() && winEl().classList.contains('custom'), winEl() && winEl().className);
  check('the schedule lane follows the new window',
        w.eval('schDays().length') === 14, String(w.eval('schDays().length')));

  winEl().dispatchEvent(new w.Event('click', {bubbles:true}));
  await new Promise(r => setTimeout(r, 60));
  w.document.querySelector('#strip .wined .wreset').dispatchEvent(new w.Event('click', {bubbles:true}));
  await new Promise(r => setTimeout(r, 120));
  check('Reset clears the override', w.eval('JSON.stringify(ST.win)') === '{}',
        w.eval('JSON.stringify(ST.win)'));
  check('and the default window comes back',
        w.eval('schDays().length') === 31, String(w.eval('schDays().length')));
  w.eval('ST.months=["2026-10","2026-11"]; ST.win={}; save();');
  console.log('\n--- 17. handover readiness: what you are MEASURED on ---');
  // Rules: ~/engg-cycle-planning/handover-score-rules.md
  //   1 = no delivery epic, 3 = epic but the month's budget FIELD is empty, 10 = both.
  //   The headline counts YOUR pod's rows across every initiative tagging you.
  check('scorer: epic + filled field is 10', w.eval('scoreRow(true,true).score') === 10,
        w.eval('JSON.stringify(scoreRow(true,true))'));
  check('scorer: epic, empty field is 3', w.eval('scoreRow(true,false).score') === 3,
        w.eval('JSON.stringify(scoreRow(true,false))'));
  check('scorer: no epic is 1 either way',
        w.eval('scoreRow(false,true).score') === 1 && w.eval('scoreRow(false,false).score') === 1,
        w.eval('JSON.stringify([scoreRow(false,true),scoreRow(false,false)])'));

  w.eval(`ME = {accountId:"acc-me", name:"Sam Lee"};
    const g = k => INITS.initiatives.find(i=>i.key===k);
    ["IDEA-1","IDEA-2","IDEA-3","IDEA-4"].forEach(k=>{
      const i = g(k); i.cycles = ["Oct-26"]; i.engDri = "Sam Lee";
      if(i.epic){ i.epic.linkReversed = false; i.epic.status = "In Progress";
                  i.epic.dueDate = "2026-11-03"; }
    });
    g("IDEA-1").budgets.Oct = 30; g("IDEA-1").budgetsSet.Oct = true;
    g("IDEA-4").epic = null;
    ST.months=["2026-10"]; ST.showAll=true; ST.driMe=false; ST.archived=false;
    DEPS = {}; depsState = 'ready'; save(); renderBoard();`);
  await new Promise(r => setTimeout(r, 60));
  const badge = k => { const row = [...w.document.querySelectorAll('tr.row')]
      .find(t => t.dataset.k === k); return row && row.querySelector('.rdy'); };

  check('epic + budget + due date is a clean tick',
        badge('IDEA-1') && badge('IDEA-1').classList.contains('ok'),
        badge('IDEA-1') && badge('IDEA-1').className + ' ' + badge('IDEA-1').title);
  check('the tooltip separates what you are measured on',
        badge('IDEA-1') && /YOUR SCORE \(what you are measured on\)/.test(badge('IDEA-1').title),
        badge('IDEA-1') && badge('IDEA-1').title);
  check('no epic scores 1 and shows red',
        badge('IDEA-4') && badge('IDEA-4').classList.contains('bad') &&
        /Missing epic/.test(badge('IDEA-4').title),
        badge('IDEA-4') && badge('IDEA-4').className);

  // the headline covers EVERY initiative tagging your pod, not only your DRI ones
  w.eval('INITS.initiatives.find(i=>i.key==="IDEA-3").engDri = "Someone Else"; renderBoard();');
  await new Promise(r => setTimeout(r, 40));
  check('a row you do not own still carries a badge', !!badge('IDEA-3'),
        'badge missing on a non-DRI row');

  console.log('\n--- 17b. zero counts as filled, but is still surfaced ---');
  // "The check is presence, not value." Four green rows can represent zero effort.
  w.eval(`const t = INITS.initiatives.find(i=>i.key==="IDEA-2");
          t.budgets.Oct = 0; t.budgetsSet.Oct = true; renderBoard();`);
  await new Promise(r => setTimeout(r, 40));
  check('an explicit 0 still scores 10', badge('IDEA-2') && /10\/10/.test(badge('IDEA-2').title),
        badge('IDEA-2') && badge('IDEA-2').title);
  check('but it is not a clean tick',
        badge('IDEA-2') && !badge('IDEA-2').classList.contains('ok'),
        badge('IDEA-2') && badge('IDEA-2').className);
  check('and says nothing is committed',
        badge('IDEA-2') && /nothing committed/.test(badge('IDEA-2').title),
        badge('IDEA-2') && badge('IDEA-2').title);

  w.eval(`const t = INITS.initiatives.find(i=>i.key==="IDEA-2");
          t.budgets.Oct = 0; t.budgetsSet.Oct = false; renderBoard();`);
  await new Promise(r => setTimeout(r, 40));
  check('a never-filled field scores 3, not 10',
        badge('IDEA-2') && /Missing budget/.test(badge('IDEA-2').title) &&
        /3\/10/.test(badge('IDEA-2').title),
        badge('IDEA-2') && badge('IDEA-2').title);

  console.log('\n--- 17c. due date is acceptance, and is not scored ---');
  // "A tagged team with ... an epic with no target date counts as a missing/unaccepted
  // dependency." So a row can score 10 and still be unaccepted.
  w.eval(`INITS.initiatives.find(i=>i.key==="IDEA-1").epic.dueDate = ""; renderBoard();`);
  await new Promise(r => setTimeout(r, 40));
  check('a missing due date does NOT change the score',
        badge('IDEA-1') && /10\/10/.test(badge('IDEA-1').title),
        badge('IDEA-1') && badge('IDEA-1').title);
  check('but it is reported as unaccepted',
        badge('IDEA-1') && /unaccepted dependency/.test(badge('IDEA-1').title),
        badge('IDEA-1') && badge('IDEA-1').title);
  check('and it is no longer a clean tick',
        badge('IDEA-1') && !badge('IDEA-1').classList.contains('ok'),
        badge('IDEA-1') && badge('IDEA-1').className);
  w.eval(`INITS.initiatives.find(i=>i.key==="IDEA-1").epic.dueDate = "2026-11-03"; renderBoard();`);

  console.log('\n--- 17d. a cancelled epic does not count as linked ---');
  w.eval(`INITS.initiatives.find(i=>i.key==="IDEA-1").epic.status = "Cancelled"; renderBoard();`);
  await new Promise(r => setTimeout(r, 40));
  check('a cancelled epic scores 1, like no epic at all',
        badge('IDEA-1') && /Missing epic/.test(badge('IDEA-1').title),
        badge('IDEA-1') && badge('IDEA-1').title);
  w.eval(`const e = INITS.initiatives.find(i=>i.key==="IDEA-1").epic;
          e.status = "Done"; renderBoard();`);
  await new Promise(r => setTimeout(r, 40));
  check('a DONE epic still counts as linked',
        badge('IDEA-1') && !/Missing epic/.test(badge('IDEA-1').title),
        badge('IDEA-1') && badge('IDEA-1').title);
  w.eval(`INITS.initiatives.find(i=>i.key==="IDEA-1").epic.status = "In Progress"; renderBoard();`);

  console.log('\n--- 17e. partner pods: accountable, not measured ---');
  // "A dependent POD failing to budget does not lower your score." It is their row.
  w.eval(`DEPS = {"IDEA-1": [
      {key:"IDEA-1", pod:"DISC - Discovery", epic:"", epicStatus:"", epicDue:"",
       months:{"Oct-26":0}, monthsSet:{"Oct-26":false}}
    ]}; depsState='ready'; renderBoard();`);
  await new Promise(r => setTimeout(r, 40));
  check('a partner pod with no epic does NOT change your score',
        badge('IDEA-1') && /10\/10/.test(badge('IDEA-1').title),
        badge('IDEA-1') && badge('IDEA-1').title);
  check('your row stays a clean tick',
        badge('IDEA-1') && badge('IDEA-1').classList.contains('ok'),
        badge('IDEA-1') && badge('IDEA-1').className);
  check('but the partner gap is listed as yours to resolve',
        badge('IDEA-1') && /you own resolution as Tech DRI/.test(badge('IDEA-1').title),
        badge('IDEA-1') && badge('IDEA-1').title);
  check('naming the pod and what it is missing',
        badge('IDEA-1') && /DISC.*Missing epic/.test(badge('IDEA-1').title),
        badge('IDEA-1') && badge('IDEA-1').title);

  // a partner pod that is merely unaccepted is still worth listing
  w.eval(`DEPS = {"IDEA-1": [
      {key:"IDEA-1", pod:"LEO - Liabilities", epic:"LEO-1", epicStatus:"In Progress",
       epicDue:"", months:{"Oct-26":5}, monthsSet:{"Oct-26":true}}
    ]}; renderBoard();`);
  await new Promise(r => setTimeout(r, 40));
  check('a partner epic with no due date is flagged unaccepted',
        badge('IDEA-1') && /LEO.*unaccepted/.test(badge('IDEA-1').title),
        badge('IDEA-1') && badge('IDEA-1').title);

  // partner data is only fetched for your DRI rows, so only those can be pending
  w.eval(`DEPS = {}; depsState = 'idle'; renderBoard();`);
  await new Promise(r => setTimeout(r, 40));
  check('your own score is shown even while partner pods load',
        badge('IDEA-1') && /10\/10/.test(badge('IDEA-1').title),
        badge('IDEA-1') && badge('IDEA-1').title);
  check('and it says the other pods are still being checked',
        badge('IDEA-1') && /Checking the other tagged pods/.test(badge('IDEA-1').title),
        badge('IDEA-1') && badge('IDEA-1').title);
  w.eval(`DEPS = {}; depsState = 'ready'; ST.showAll=false; save(); renderBoard();`);

  console.log('\n--- 18. handover readiness panel (from the program team CLI) ---');
  // The page does NOT recompute the score. The CLI counts rows a PODs JQL cannot see (the
  // Horizontals field, and epics linked to initiatives that never declared the POD), and a
  // local reimplementation read 6.5 where the real figure was 6.77. So the panel renders
  // whatever /api/handover returns and must not quietly "fix" it.
  const HO_FIX = {
    readiness: {
      cycle_id: 'ON-26', fetched: '2026-10-05T09:00:00', v: 1,
      rollup: { per_project: [
        { project:'BOARD', month:'Oct-26', rows:30, avg:6.77, pct_green:63.3,
          missing_epic:10, missing_budget:1, bar:null, meets_bar:null },
        { project:'BOARD', month:'Nov-26', rows:26, avg:6.0, pct_green:53.8,
          missing_epic:10, missing_budget:2, bar:8, meets_bar:false } ] },
      rows: [
        { initiative:'IDEA-1', initiative_summary:'one', team:'BOARD', month:'Oct-26',
          score:1, status:'Missing epic', budget:0, epic_due:'', epics:[], mine:true, note:'' },
        { initiative:'IDEA-2', initiative_summary:'two', team:'BOARD', month:'Oct-26',
          score:3, status:'Missing budget', budget:0, epic_due:'2026-11-03',
          epics:['BOARD-2'], mine:true, note:'' },
        { initiative:'IDEA-3', initiative_summary:'three', team:'BOARD', month:'Oct-26',
          score:10, status:'OK', budget:0, epic_due:'2026-11-03', epics:['BOARD-3'],
          mine:true, note:'' },                                   // budgeted ZERO
        { initiative:'IDEA-4', initiative_summary:'four', team:'BOARD', month:'Oct-26',
          score:10, status:'OK', budget:5, epic_due:'', epics:['BOARD-4'],
          mine:true, note:'' },                                   // no due date
        { initiative:'IDEA-5', initiative_summary:'five', team:'BOARD', month:'Oct-26',
          score:10, status:'OK', budget:5, epic_due:'2026-11-03', epics:['BOARD-5'],
          mine:true, note:'' },
        { initiative:'IDEA-1', initiative_summary:'one', team:'DISC', month:'Oct-26',
          score:1, status:'Missing epic', budget:0, epic_due:'', epics:[], mine:false,
          note:'POD not declared (from linked epic)' },
        { initiative:'IDEA-2', initiative_summary:'two', team:'DISC', month:'Oct-26',
          score:3, status:'Missing budget', budget:0, epic_due:'', epics:['DISC-1'],
          mine:false, note:'' },
        { initiative:'IDEA-3', initiative_summary:'three', team:'LEO', month:'Oct-26',
          score:10, status:'OK', budget:5, epic_due:'2026-11-03', epics:['LEO-1'],
          mine:false, note:'' } ],
    },
    acceptance: { em:'sam@x.com', total:16, accepted:2, missingGoLive:3, missingHandover:13,
      missingEither:14, rows: [
        { key:'IDEA-9', url:'u/IDEA-9', summary:'nine', months:['Oct-26'],
          goLive:'', handover:'', missing:['Target Go-Live','Target Handover'] },
        { key:'IDEA-8', url:'u/IDEA-8', summary:'eight', months:['Oct-26'],
          goLive:'2026-12-01', handover:'', missing:['Target Handover'] } ] },
    history: [
      { date:'2026-10-04', project:'BOARD', month:'Oct-26', rows:28, avg:6.5, pctGreen:60 },
      { date:'2026-10-05', project:'BOARD', month:'Oct-26', rows:30, avg:6.77, pctGreen:63.3 } ],
  };
  w.eval(`HO = ${JSON.stringify(HO_FIX)}; HOSTATE = 'ready'; HOLIST = null; renderHandover();`);
  await new Promise(r => setTimeout(r, 50));
  const pan = () => w.document.getElementById('handover');
  const hoTxt = () => pan().textContent.replace(/\s+/g, ' ');

  check('the panel renders a card per cycle month',
        pan().querySelectorAll('.hcard').length >= 2,
        String(pan().querySelectorAll('.hcard').length));
  check('it shows the CLI average verbatim', /6\.77/.test(hoTxt()), hoTxt().slice(0, 200));
  check('and the row count beside it', /30 rows/.test(hoTxt()), hoTxt().slice(0, 200));
  check('status counts sit next to the average',
        /1 missing budget/.test(hoTxt()) && /10 missing epic/.test(hoTxt()), hoTxt().slice(0, 400));
  check('below-bar is called out', /below the bar of 8/.test(hoTxt()), hoTxt());

  console.log('\n--- 18b. the false-green traps ---');
  check('a zero budget is counted', /1 budgeted 0/.test(hoTxt()), hoTxt());
  check('a missing due date is counted', /1 no due date/.test(hoTxt()), hoTxt());
  check('the note explains presence-not-value',
        /presence is checked, not value/.test(hoTxt()), hoTxt().slice(-400));
  check('and that a due date is not scored at all',
        /not scored at all/.test(hoTxt()), hoTxt().slice(-400));

  const chipFor = id => [...pan().querySelectorAll('.hchip')].find(b => b.dataset.l === id);
  chipFor('zero:Oct-26').dispatchEvent(new w.Event('click', {bubbles:true}));
  await new Promise(r => setTimeout(r, 40));
  check('listing the zeros shows only the zero-budget row',
        [...pan().querySelectorAll('.hor .k')].map(a=>a.textContent).join(',') === 'IDEA-3',
        [...pan().querySelectorAll('.hor .k')].map(a=>a.textContent).join(','));
  check('and says nothing is committed',
        /Nothing is committed/.test(pan().textContent), pan().textContent.slice(0, 600));

  chipFor('nodue:Oct-26').dispatchEvent(new w.Event('click', {bubbles:true}));
  await new Promise(r => setTimeout(r, 40));
  check('listing no-due-date shows only that row',
        [...pan().querySelectorAll('.hor .k')].map(a=>a.textContent).join(',') === 'IDEA-4',
        [...pan().querySelectorAll('.hor .k')].map(a=>a.textContent).join(','));
  check('and calls it an unaccepted dependency',
        /unaccepted dependency/.test(pan().textContent), pan().textContent.slice(0, 700));

  console.log('\n--- 18c. rows to fix, worst first ---');
  chipFor('missingEpic:Oct-26').dispatchEvent(new w.Event('click', {bubbles:true}));
  await new Promise(r => setTimeout(r, 40));
  check('only your own pod rows are listed',
        [...pan().querySelectorAll('.hor .k')].map(a=>a.textContent).join(',') === 'IDEA-1',
        [...pan().querySelectorAll('.hor .k')].map(a=>a.textContent).join(','));
  check('each row carries team and month',
        /BOARD · Oct-26/.test(pan().textContent), pan().textContent.slice(0, 800));
  check('the score is shown per row', !!pan().querySelector('.hor .sc.s1'),
        pan().innerHTML.slice(0, 200));
  check('missing budget is named the cheapest move',
        (() => { chipFor('missingBudget:Oct-26').dispatchEvent(new w.Event('click', {bubbles:true}));
                 return /cheapest move/.test(pan().textContent); })(),
        pan().textContent.slice(0, 700));

  console.log('\n--- 18d. Tech DRI exposure: accountable, not measured ---');
  w.eval('HOLIST = null; renderHandover();');
  await new Promise(r => setTimeout(r, 40));
  check('other teams are grouped', pan().querySelectorAll('.hteam').length === 2,
        [...pan().querySelectorAll('.hteam')].map(b=>b.textContent.trim()).join(' | '));
  check('with a per-team average',
        /DISC\s*2/.test(hoTxt()), [...pan().querySelectorAll('.hteam')].map(b=>b.textContent.replace(/\s+/g,' ')).join(' | '));
  check('worst team first',
        pan().querySelectorAll('.hteam')[0].textContent.includes('DISC'),
        pan().querySelectorAll('.hteam')[0].textContent);
  check('it says these are not in your score',
        /not in your score/.test(hoTxt()), hoTxt());
  check('their rows are excluded from your month cards',
        !/8 rows/.test(hoTxt()), hoTxt().slice(0, 300));
  [...pan().querySelectorAll('.hteam')][0].dispatchEvent(new w.Event('click', {bubbles:true}));
  await new Promise(r => setTimeout(r, 40));
  check('drilling a team lists its rows to fix',
        [...pan().querySelectorAll('.hor .k')].length === 2,
        [...pan().querySelectorAll('.hor .k')].map(a=>a.textContent).join(','));
  check('and names you as resolution owner',
        /resolution owner/.test(pan().textContent), pan().textContent.slice(0, 600));

  console.log('\n--- 18e. EM acceptance, gated on the initiative target dates ---');
  w.eval('HOLIST = null; renderHandover();');
  await new Promise(r => setTimeout(r, 40));
  check('acceptance has its own card', /EM acceptance/.test(hoTxt()), hoTxt());
  check('showing accepted out of total', /2\s*\/\s*16/.test(hoTxt()), hoTxt());
  check('and how many lack a target date', /14 missing a target date/.test(hoTxt()), hoTxt());
  check('it is marked separate from the handover score',
        /separate from the handover score/.test(hoTxt()), hoTxt());
  const accChip = [...pan().querySelectorAll('.hchip')].find(b=>b.dataset.l === 'accHandover:');
  check('a chip exists for the missing handover dates', !!accChip,
        [...pan().querySelectorAll('.hchip')].map(b=>b.dataset.l).join(','));
  accChip.dispatchEvent(new w.Event('click', {bubbles:true}));
  await new Promise(r => setTimeout(r, 40));
  check('listing them shows both initiatives missing a handover date',
        [...pan().querySelectorAll('.hor .k')].map(a=>a.textContent).join(',') === 'IDEA-9,IDEA-8',
        [...pan().querySelectorAll('.hor .k')].map(a=>a.textContent).join(','));

  console.log('\n--- 18f. the denominator moves, so show the counts ---');
  w.eval('HOLIST = null; renderHandover();');
  await new Promise(r => setTimeout(r, 40));
  check('the trend names the previous snapshot', /since 2026-10-04/.test(hoTxt()), hoTxt());
  check('it reports the row delta too', /\+2 rows/.test(hoTxt()), hoTxt());
  check('and warns when the denominator moved',
        /denominator moved/.test(hoTxt()), hoTxt());

  console.log('\n--- 18g. failure is visible, never silent ---');
  w.eval(`HOSTATE = 'failed'; HO = {readiness:{__error__:'workspace not found'}}; renderHandover();`);
  await new Promise(r => setTimeout(r, 40));
  check('a failed check says so', /unavailable/.test(hoTxt()), hoTxt());
  check('it surfaces the reason', /workspace not found/.test(hoTxt()), hoTxt());
  check('and offers a retry', !!w.document.getElementById('horetry'), pan().innerHTML.slice(0,200));
  check('no score is invented', !/6\.77/.test(hoTxt()), hoTxt());
  w.eval(`HOSTATE = 'idle'; HO = null; renderHandover();`);


  console.log(fail.length ? `\n${fail.length} of ${ran} FAILED: ${fail.join(', ')}`
                          : `\nall ${ran} checks passed`);
  process.exit(fail.length ? 1 : 0);
})().catch(e => { console.error('CRASH', e); process.exit(1); });
