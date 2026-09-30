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
});

const INITS = { pods: ['MY POD'], v: 3, initiatives: [
  // epic budget Oct=30, plan says 30            -> in sync
  mk('OINT-1', 'BOARD-1', { Oct: 30, Nov: 0 }, {
    dueDate: '2026-10-20', status: 'In Progress', assignee: 'Asha Rao',
    health: 'On Track', cycles: ['Oct-26'], overallBudget: 40, priority: 'P1',
    labels: 'cbs,sunset', challenges: 'Dependency on NEWSYS' }),
  // epic budget Oct=5, plan says 7              -> pending, due date already past
  mk('OINT-2', 'BOARD-2', { Oct: 5, Nov: 0 }, { dueDate: '2026-09-01', status: 'To Do' }),
  // epic budget Oct=12, nothing ticked          -> stale in Jira, no due date set
  mk('OINT-3', 'BOARD-3', { Oct: 12, Nov: 0 }, { dueDate: '' }),
  // no linked epic                              -> no submit button, epic cols blank
  mk('OINT-4', null, { Oct: 0, Nov: 0 }),
  // shared epic: 12 + 8 = 20 matches Jira       -> both rows in sync
  mk('OINT-6', 'BOARD-6', { Oct: 20, Nov: 0 }, { dueDate: '2026-12-15' }),
  mk('OINT-7', 'BOARD-6', { Oct: 20, Nov: 0 }, { dueDate: '2026-12-15' }),
]};

const PLAN = {
  'OINT-1': { Oct: { on: true,  sp: 30, seeded: true }, Nov: { on: false, sp: 0, seeded: true } },
  'OINT-2': { Oct: { on: true,  sp: 7,  seeded: true }, Nov: { on: false, sp: 0, seeded: true } },
  'OINT-3': { Oct: { on: false, sp: 0,  seeded: true }, Nov: { on: false, sp: 0, seeded: true } },
  'OINT-4': { Oct: { on: true,  sp: 9,  seeded: true }, Nov: { on: false, sp: 0, seeded: true } },
  'OINT-6': { Oct: { on: true,  sp: 12, seeded: true }, Nov: { on: false, sp: 0, seeded: true } },
  'OINT-7': { Oct: { on: true,  sp: 8,  seeded: true }, Nov: { on: false, sp: 0, seeded: true } },
};
const ST0 = { pods: null, months: ['2026-10', '2026-11'],
  order: ['OINT-1', 'OINT-2', 'OINT-3', 'OINT-4', 'OINT-6', 'OINT-7'],
  plan: PLAN, showAll: true, capOpen: {}, initOpen: {}, est: {}, cols: [] };

const posted = [], detailCalls = [], metaCalls = [];
let POD_MODE = 'full', writeFails = false, linkPreview = null, unlinkFails = false;
let auditReversed = [], auditBroken = [];

// mirrors capacity_engine.planner_editable_fields() / the page's COL_MODEL
const EDITABLE_FIELDS = {
  status: { target: 'initiative', field: '@status' },
  engDri: { target: 'initiative', field: 'cf_eng' },
  pods: { target: 'initiative', field: 'cf_pods' },
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
  initiatives: [mk('OINT-9', 'BOARD-9', { Oct: 0, Nov: 0 }), INITS.initiatives[0]] };

async function fetchStub(p, opts) {
  p = String(p);
  const body = opts && opts.body ? JSON.parse(opts.body) : null;
  let out = {};
  if (p.startsWith('/api/initiatives')) out = POD_MODE === 'full' ? INITS : NARROW;
  else if (p.startsWith('/api/initiative?')) { detailCalls.push(p);
    out = { descriptionText: 'the description', impact: 'some impact' }; }
  else if (p.startsWith('/api/month?')) out = { label: 'Oct 2026', teamSP: 112, workingDays: 21,
    teamNetDays: 100, utilisation: 90, days: [], people: [] };
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
  check('OINT-2 offers Submit (1)', btnFor('OINT-2').textContent === '⤴ Submit (1)', btnFor('OINT-2').textContent);
  check('OINT-2 enabled', btnFor('OINT-2').disabled === false, btnFor('OINT-2').disabled);
  check('OINT-1 shows in Jira', btnFor('OINT-1').textContent === '✓ in Jira', btnFor('OINT-1').textContent);
  check('OINT-1 disabled', btnFor('OINT-1').disabled === true, '');
  check('OINT-3 flagged stale', btnFor('OINT-3').textContent === '⚠ stale in Jira', btnFor('OINT-3').textContent);
  check('shared epic rows in sync', btnFor('OINT-6').textContent === '✓ in Jira', btnFor('OINT-6').textContent);
  check('no-epic row has no submit', btnFor('OINT-4') === undefined, 'button present');
  const dirty = [...w.document.querySelectorAll('.mcell input.dirty')].map(i => i.dataset.k + '/' + i.dataset.m);
  check('only OINT-2 Oct cell marked dirty', JSON.stringify(dirty) === '["OINT-2/Oct"]', JSON.stringify(dirty));

  console.log('\n--- 2. one-click submit: no confirm()/alert(), toast + Undo ---');
  let confirmed = 0, alerted = 0;
  w.confirm = () => { confirmed++; return true; };
  w.alert = () => { alerted++; };
  btnFor('OINT-2').dispatchEvent(new w.Event('click', { bubbles: true }));
  await new Promise(r => setTimeout(r, 200));
  check('no confirm() shown', confirmed === 0, confirmed);
  check('no alert() shown', alerted === 0, alerted);
  check('exactly one write posted', posted.filter(p => !p.dryRun).length === 1, JSON.stringify(posted));
  check('no dry-run round-trip', posted.filter(p => p.dryRun).length === 0, posted.length);
  const toastEl = w.document.querySelector('.toast');
  check('toast rendered', !!toastEl && /Updated BOARD-2/.test(toastEl.textContent), toastEl && toastEl.textContent);
  check('toast offers Undo', !!toastEl && toastEl.querySelector('button')?.textContent === 'Undo', '');
  check('row flips to in Jira', btnFor('OINT-2').textContent === '✓ in Jira', btnFor('OINT-2').textContent);
  check('header goes to all submitted', txt('#submit') === '✓ All submitted', txt('#submit'));

  console.log('\n--- 3. Undo restores the previous Jira value ---');
  toastEl.querySelector('button').dispatchEvent(new w.Event('click', { bubbles: true }));
  await new Promise(r => setTimeout(r, 200));
  check('undo wrote 7 -> 5 back', posted[posted.length - 1].epicBudgets['BOARD-2'].Oct === 5,
        JSON.stringify(posted[posted.length - 1].epicBudgets));
  check('row pending again', btnFor('OINT-2').textContent === '⤴ Submit (1)', btnFor('OINT-2').textContent);

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
  check('Eng DRI value rendered', xcell('OINT-1', 'Eng DRI').textContent === 'Sam Lee',
        xcell('OINT-1', 'Eng DRI').textContent);
  check('Eng DRI dropped from summary cell', !rowFor('OINT-1').querySelector('.dri')?.textContent.includes('Eng'),
        rowFor('OINT-1').querySelector('.dri')?.textContent);

  console.log('\n--- 6. column picker: epic-level fields ---');
  ['epicDue', 'epicStatus', 'epicOwner', 'epicHealth', 'epicLabels', 'epicChal'].forEach(tick);
  check('cell count matches header count',
        rowFor('OINT-1').querySelectorAll('td').length === headers().length,
        rowFor('OINT-1').querySelectorAll('td').length + ' vs ' + headers().length);
  // due-date rendering + editing is covered in 6b (it is an input, not static text)
  check('epic status rendered', xcell('OINT-1', 'Epic status').textContent === 'In Progress',
        xcell('OINT-1', 'Epic status').textContent);
  check('epic assignee rendered', xcell('OINT-1', 'Epic assignee').textContent === 'Asha Rao',
        xcell('OINT-1', 'Epic assignee').textContent);
  check('epic health rendered', xcell('OINT-1', 'Epic health').textContent === 'On Track',
        xcell('OINT-1', 'Epic health').textContent);
  check('challenges rendered', xcell('OINT-1', 'Challenges').textContent === 'Dependency on NEWSYS',
        xcell('OINT-1', 'Challenges').textContent);
  check('no Overall budget column offered',   // its Jira field id is stale — see monthly.html
        !w.eval('EXTRA_COLS.some(c=>c.id==="epicBudget")'), 'epicBudget still listed');
  check('zero-ish epic fields degrade to dash', xcell('OINT-2', 'Epic assignee').textContent === '—',
        xcell('OINT-2', 'Epic assignee').textContent);
  check('detail row colspan still spans the table', (() => {
    const d = w.document.querySelector('tr.detail td');
    return d ? +d.getAttribute('colspan') === headers().length : true; })(), 'n/a');
  check('column choice persisted',
        JSON.parse(w.localStorage.getItem('monthlyPlanner_v1')).cols.includes('epicDue'), '');

  console.log('\n--- 6b. epic due date is editable ---');
  const dueOf = k => xcell(k, 'Epic due date').querySelector('input.duedate');
  check('due date is an input', dueOf('OINT-1') && dueOf('OINT-1').type === 'date',
        xcell('OINT-1', 'Epic due date').innerHTML);
  check('input carries the current value', dueOf('OINT-1').value === '2026-10-20', dueOf('OINT-1').value);
  check('unset date marked, still editable',
        dueOf('OINT-3').classList.contains('unset') && dueOf('OINT-3').disabled === false,
        dueOf('OINT-3').className);
  check('past-due input flagged', dueOf('OINT-2').classList.contains('past'), dueOf('OINT-2').className);
  check('after-window input flagged', dueOf('OINT-6').classList.contains('late'), dueOf('OINT-6').className);
  check('no epic -> no date input', xcell('OINT-4', 'Epic due date').textContent === '—',
        xcell('OINT-4', 'Epic due date').textContent);

  let n = posted.length;
  const d3 = dueOf('OINT-3'); d3.value = '2026-11-28';
  d3.dispatchEvent(new w.Event('change', { bubbles: true }));
  await new Promise(r => setTimeout(r, 200));
  const duePost = posted[posted.length - 1];
  check('edit posts through the generic /api/set-field',
        duePost.key === 'BOARD-3' && duePost.column === 'epicDue' && duePost.value === '2026-11-28',
        JSON.stringify(duePost));
  check('one request per edit', posted.length === n + 1, posted.length - n);
  check('value persists after re-render', dueOf('OINT-3').value === '2026-11-28', dueOf('OINT-3').value);
  check('no longer marked unset', !dueOf('OINT-3').classList.contains('unset'), dueOf('OINT-3').className);
  const dueToast = [...w.document.querySelectorAll('.toast')].pop();
  check('toast names the change', /BOARD-3 Epic due date → 2026-11-28/.test(dueToast.textContent),
        dueToast.textContent);
  check('toast offers Undo', dueToast.querySelector('button')?.textContent === 'Undo', '');

  n = posted.length;
  dueToast.querySelector('button').dispatchEvent(new w.Event('click', { bubbles: true }));
  await new Promise(r => setTimeout(r, 200));
  check('undo restores the previous value', posted[posted.length - 1].value === '',
        JSON.stringify(posted[posted.length - 1]));
  check('input back to empty', dueOf('OINT-3').value === '', dueOf('OINT-3').value);

  console.log('\n--- 6c. every Jira-backed column edits inline ---');
  const cellOf = (k, label) => xcell(k, label);
  check('editable column marked', cellOf('OINT-1', 'Epic health').classList.contains('editable'),
        cellOf('OINT-1', 'Epic health').className);
  check('editable cell knows its issue', cellOf('OINT-1', 'Epic health').dataset.issue === 'BOARD-1',
        cellOf('OINT-1', 'Epic health').dataset.issue);
  check('initiative column targets the initiative',
        cellOf('OINT-1', 'Status').dataset.issue === 'OINT-1', cellOf('OINT-1', 'Status').dataset.issue);
  check('epic column on a row with no epic is read-only',
        !cellOf('OINT-4', 'Epic health').classList.contains('editable'),
        cellOf('OINT-4', 'Epic health').className);

  // select
  metaCalls.length = 0;
  cellOf('OINT-1', 'Epic health').dispatchEvent(new w.Event('click', { bubbles: true }));
  await new Promise(r => setTimeout(r, 120));
  check('click asks the server what is editable', metaCalls.length === 1, JSON.stringify(metaCalls));
  let sel = cellOf('OINT-1', 'Epic health').querySelector('select.inline');
  check('select rendered with options', sel && sel.options.length === 3, sel && sel.options.length);
  check('current value preselected', sel.value === 'h1', sel.value);
  n = posted.length;
  sel.value = 'h2'; sel.dispatchEvent(new w.Event('change', { bubbles: true }));
  await new Promise(r => setTimeout(r, 150));
  check('select writes the option id',
        posted[posted.length - 1].column === 'epicHealth' && posted[posted.length - 1].value === 'h2',
        JSON.stringify(posted[posted.length - 1]));
  check('cell repaints with the new label', cellOf('OINT-1', 'Epic health').textContent === '⚠️ At Risk',
        cellOf('OINT-1', 'Epic health').textContent);
  check('write does not refetch editmeta', metaCalls.length === 1, metaCalls.length);

  // transition
  cellOf('OINT-1', 'Status').dispatchEvent(new w.Event('click', { bubbles: true }));
  await new Promise(r => setTimeout(r, 120));
  sel = cellOf('OINT-1', 'Status').querySelector('select.inline');
  check('status offers transitions', sel && sel.options.length === 3, sel && sel.options.length);
  sel.value = '31'; sel.dispatchEvent(new w.Event('change', { bubbles: true }));
  await new Promise(r => setTimeout(r, 150));
  check('transition posts its id', posted[posted.length - 1].value === '31',
        JSON.stringify(posted[posted.length - 1]));
  const trToast = [...w.document.querySelectorAll('.toast')].pop();
  check('transition offers no Undo (not reversible)', !trToast.querySelector('button'),
        trToast.textContent);

  // labels
  cellOf('OINT-1', 'Epic labels').dispatchEvent(new w.Event('click', { bubbles: true }));
  await new Promise(r => setTimeout(r, 120));
  let inp = cellOf('OINT-1', 'Epic labels').querySelector('input.inline');
  check('labels prefilled comma separated', inp && inp.value === 'cbs, sunset', inp && inp.value);
  inp.value = 'cbs, sunset, migration';
  inp.dispatchEvent(new w.Event('change', { bubbles: true }));
  await new Promise(r => setTimeout(r, 150));
  check('labels post as an array',
        JSON.stringify(posted[posted.length - 1].value) === '["cbs","sunset","migration"]',
        JSON.stringify(posted[posted.length - 1].value));

  // user picker
  cellOf('OINT-1', 'Epic assignee').dispatchEvent(new w.Event('click', { bubbles: true }));
  await new Promise(r => setTimeout(r, 120));
  inp = cellOf('OINT-1', 'Epic assignee').querySelector('input.userpick');
  check('user picker rendered', !!inp, cellOf('OINT-1', 'Epic assignee').innerHTML);
  inp.value = 'Pad'; inp.dispatchEvent(new w.Event('input', { bubbles: true }));
  await new Promise(r => setTimeout(r, 400));
  const opts = cellOf('OINT-1', 'Epic assignee').querySelectorAll('.uopt[data-id]');
  check('typeahead lists matches', opts.length === 2, opts.length);
  n = posted.length;
  opts[0].dispatchEvent(new w.Event('mousedown', { bubbles: true }));
  await new Promise(r => setTimeout(r, 200));
  check('picking a user posts the accountId, not the name',
        posted[posted.length - 1].value === 'acc-1', JSON.stringify(posted[posted.length - 1]));

  // blurring a half-typed name must not clear the assignee
  cellOf('OINT-1', 'Epic assignee').dispatchEvent(new w.Event('click', { bubbles: true }));
  await new Promise(r => setTimeout(r, 120));
  inp = cellOf('OINT-1', 'Epic assignee').querySelector('input.userpick');
  n = posted.length;
  inp.value = 'Pad';
  inp.dispatchEvent(new w.Event('blur', { bubbles: true }));
  await new Promise(r => setTimeout(r, 250));
  check('blur without a pick writes nothing', posted.length === n, JSON.stringify(posted.slice(n)));

  // a column Jira says is not editable
  cellOf('OINT-1', 'Challenges').dispatchEvent(new w.Event('click', { bubbles: true }));
  await new Promise(r => setTimeout(r, 150));
  check('non-editable column says why, writes nothing',
        [...w.document.querySelectorAll('.toast.warn')].some(t => /not editable on BOARD-1/.test(t.textContent)),
        [...w.document.querySelectorAll('.toast')].map(t => t.textContent).join(' | '));
  check('no control opened for it', !cellOf('OINT-1', 'Challenges').querySelector('select, input'), '');

  // failed write
  writeFails = true;
  n = posted.length;
  const metaBefore = metaCalls.length;
  cellOf('OINT-1', 'Epic health').dispatchEvent(new w.Event('click', { bubbles: true }));
  await new Promise(r => setTimeout(r, 120));
  check('stale editmeta was dropped, so reopening refetches', metaCalls.length === metaBefore + 1,
        metaCalls.length - metaBefore);
  sel = cellOf('OINT-1', 'Epic health').querySelector('select.inline');
  sel.value = 'h1'; sel.dispatchEvent(new w.Event('change', { bubbles: true }));
  await new Promise(r => setTimeout(r, 200));
  check('failed write surfaces the Jira error',
        [...w.document.querySelectorAll('.toast.bad')].some(t => /Field cannot be set/.test(t.textContent)),
        [...w.document.querySelectorAll('.toast.bad')].map(t => t.textContent).join(' | '));
  check('cell reverts to the stored value',
        cellOf('OINT-1', 'Epic health').textContent === '⚠️ At Risk',
        cellOf('OINT-1', 'Epic health').textContent);
  writeFails = false;

  console.log('\n--- 6d. months are plain numbers, no tick box ---');
  const numFor = (k, m) => [...w.document.querySelectorAll('.mcell input[type=number]')]
    .find(i => i.dataset.k === k && i.dataset.m === m);
  check('no checkboxes anywhere', w.document.querySelectorAll('.mcell input[type=checkbox]').length === 0,
        w.document.querySelectorAll('.mcell input[type=checkbox]').length);
  check('unplanned month shows Jira value as placeholder',
        numFor('OINT-3', 'Oct').placeholder === '12', numFor('OINT-3', 'Oct').placeholder);
  check('that input is editable, not disabled', numFor('OINT-3', 'Oct').disabled === false, '');
  check('placeholder explains itself', /Jira holds 12 SP/.test(numFor('OINT-3', 'Oct').title),
        numFor('OINT-3', 'Oct').title);
  check('planned month shows its value', numFor('OINT-1', 'Oct').value === '30', numFor('OINT-1', 'Oct').value);
  check('month with no Jira budget stays blank',
        numFor('OINT-3', 'Nov').placeholder === '', numFor('OINT-3', 'Nov').placeholder);

  // typing plans the month; clearing unplans it
  const m3 = numFor('OINT-3', 'Oct');
  m3.value = '4'; m3.dispatchEvent(new w.Event('input', { bubbles: true }));
  await new Promise(r => setTimeout(r, 60));
  check('typing plans the month', w.eval('ST.plan["OINT-3"].Oct.on') === true, w.eval('ST.plan["OINT-3"].Oct.on'));
  check('value stored', w.eval('ST.plan["OINT-3"].Oct.sp') === 4, w.eval('ST.plan["OINT-3"].Oct.sp'));
  m3.value = '0'; m3.dispatchEvent(new w.Event('input', { bubbles: true }));
  await new Promise(r => setTimeout(r, 60));
  check('explicit 0 stays planned (clears the Jira budget)',
        w.eval('ST.plan["OINT-3"].Oct.on') === true && w.eval('ST.plan["OINT-3"].Oct.sp') === 0,
        w.eval('JSON.stringify(ST.plan["OINT-3"].Oct)'));
  m3.value = ''; m3.dispatchEvent(new w.Event('input', { bubbles: true }));
  await new Promise(r => setTimeout(r, 60));
  check('clearing unplans the month', w.eval('ST.plan["OINT-3"].Oct.on') === false,
        w.eval('ST.plan["OINT-3"].Oct.on'));

  console.log('\n--- 6e. linking an epic is one picker, not a confirm() chain ---');
  confirmed = 0; alerted = 0;
  const linkBtn = () => [...w.document.querySelectorAll('.linkbtn:not(.retry)')]
    .find(b => b.dataset.k === 'OINT-4');
  linkPreview = { status: 'preview', initiativeSummary: 'OINT-4 summary', matches: [
    { key: 'BOARD-90', summary: 'OINT-4 summary', status: 'To Do', exact: true },
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
        posted[posted.length - 1].mode === 'create' && posted[posted.length - 1].initiative === 'OINT-4',
        JSON.stringify(posted[posted.length - 1]));
  check('picker closed', !w.document.querySelector('.modal-back'), 'still open');
  check('no alert() used', alerted === 0, alerted);
  check('result is a toast',
        [...w.document.querySelectorAll('.toast')].some(t => /Created and linked BOARD-NEW to OINT-4/.test(t.textContent)),
        [...w.document.querySelectorAll('.toast')].map(t => t.textContent).join(' | '));
  check('row now shows the epic', !!rowFor('OINT-4').querySelector('.epic'),
        rowFor('OINT-4').innerHTML.slice(0, 120));

  // cancelling must write nothing
  INITS.initiatives.find(i => i.key === 'OINT-4').epic = null;
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
  INITS.initiatives.find(i => i.key === 'OINT-4').epic = null;
  renderNow();
  linkPreview = { status: 'preview', initiativeSummary: 'OINT-4 summary', matches: [] };
  n = posted.length;
  linkBtn().dispatchEvent(new w.Event('click', { bubbles: true }));
  await new Promise(r => setTimeout(r, 250));
  check('no candidates -> no dialog at all', !w.document.querySelector('.modal-back'),
        'a one-option dialog was shown');
  check('it just creates', posted.some(x => x.mode === 'create'),
        JSON.stringify(posted.slice(n)));
  const mk = [...w.document.querySelectorAll('.toast')].pop();
  check('toast names the created epic', /Created and linked BOARD-NEW to OINT-4/.test(mk.textContent),
        mk.textContent);
  check('toast offers unlink as the way out',
        /Undo \(unlink\)/.test(mk.querySelector('button')?.textContent || ''),
        mk.querySelector('button')?.textContent);
  n = posted.length;
  mk.querySelector('button').dispatchEvent(new w.Event('click', { bubbles: true }));
  await new Promise(r => setTimeout(r, 200));
  check('that undo unlinks the new epic',
        posted[posted.length - 1].initiative === 'OINT-4' && posted[posted.length - 1].epicKey === 'BOARD-NEW',
        JSON.stringify(posted[posted.length - 1]));

  console.log('\n--- 6f. unlinking an epic ---');
  const unlinkBtn = k => [...w.document.querySelectorAll('.unlinkbtn')].find(b => b.dataset.k === k);
  check('linked row offers unlink', !!unlinkBtn('OINT-1'), 'no unlink button');
  check('unlink names both sides', /Unlink BOARD-1 from OINT-1/.test(unlinkBtn('OINT-1').title),
        unlinkBtn('OINT-1').title);
  check('unlink says the epic survives', /epic itself is kept/.test(unlinkBtn('OINT-1').title),
        unlinkBtn('OINT-1').title);
  check('unlinked row offers none', !unlinkBtn('OINT-4'), 'unlink shown on a row with no epic');

  n = posted.length;
  unlinkBtn('OINT-1').dispatchEvent(new w.Event('click', { bubbles: true }));
  await new Promise(r => setTimeout(r, 200));
  check('posts initiative + epic',
        posted[posted.length - 1].initiative === 'OINT-1' && posted[posted.length - 1].epicKey === 'BOARD-1',
        JSON.stringify(posted[posted.length - 1]));
  check('one request', posted.length === n + 1, posted.length - n);
  check('row drops to link-epic', !!rowFor('OINT-1').querySelector('.linkbtn'),
        rowFor('OINT-1').innerHTML.slice(0, 150));
  check('epic columns go read-only for that row',
        !xcell('OINT-1', 'Epic health').classList.contains('editable'),
        xcell('OINT-1', 'Epic health').className);
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
  check('row shows the epic again', !!rowFor('OINT-1').querySelector('.epic'),
        rowFor('OINT-1').innerHTML.slice(0, 150));

  unlinkFails = true;
  unlinkBtn('OINT-1').dispatchEvent(new w.Event('click', { bubbles: true }));
  await new Promise(r => setTimeout(r, 200));
  check('ambiguous link is refused, not guessed',
        [...w.document.querySelectorAll('.toast.bad')].some(t => /say which one/.test(t.textContent)),
        [...w.document.querySelectorAll('.toast.bad')].map(t => t.textContent).join(' | '));
  check('row keeps its epic on failure', !!rowFor('OINT-1').querySelector('.epic'),
        rowFor('OINT-1').innerHTML.slice(0, 150));
  unlinkFails = false;

  console.log('\n--- 6g. Tech DRI filter ---');
  const setPods = (k, pods, dri) => { const it = INITS.initiatives.find(i => i.key === k);
    it.podTags = ['MY POD', ...pods]; it.engDri = dri; };
  setPods('OINT-1', ['COREP - Core Platform'], 'Sam Lee');
  setPods('OINT-2', ['COREP - Core Platform'], 'Someone Else');
  setPods('OINT-3', [], 'Someone Else');
  setPods('OINT-4', [], 'Someone Else');
  setPods('OINT-6', ['BOPS - BRANCH-BANKING'], 'Sam Lee');
  setPods('OINT-7', ['LEO - Liabilities'], 'Someone Else');
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
        JSON.stringify(keys) === '["OINT-1","OINT-6"]' && keys.length < allRows, JSON.stringify(keys));
  check('rows still draggable', [...w.document.querySelectorAll('tr.row')]
        .every(r => r.getAttribute('draggable') === 'true'), '');
  driBtn.dispatchEvent(new w.Event('click', { bubbles: true }));
  await new Promise(r => setTimeout(r, 120));
  check('toggling back restores every row',
        [...w.document.querySelectorAll('tr.row')].length === allRows, '');

  console.log('\n--- 6g2. PODs column in the planner ---');
  INITS.initiatives.find(i => i.key === 'OINT-1').podTags =
    ['MY POD', 'COREP - Core Platform', 'BOPS - BRANCH-BANKING'];
  renderNow();
  w.document.querySelector('#ddCols button').dispatchEvent(new w.Event('click', { bubbles: true }));
  check('PODs is offered as a column',
        !!w.document.querySelector('#ddCols .menu input[value="pods"]'), 'not in the picker');
  tick('pods');
  await new Promise(r => setTimeout(r, 100));
  const podCell = xcell('OINT-1', 'PODs');
  const chips = c => [...c.querySelectorAll('.podchip')].map(x=>x.textContent.trim());
  check('PODs renders one chip per pod', chips(podCell).join(' ') === 'MY POD COREP BOPS',
        chips(podCell).join(' '));
  check('own pod is highlighted', !!podCell.querySelector('.podchip.own'), podCell.innerHTML);
  check('full labels kept in the tooltip',
        /COREP - Core Platform/.test(podCell.querySelector('.podwrap').title), podCell.innerHTML);

  // the real failure this replaced: two pods sharing a prefix collapsed to "BOARD, BOARD"
  INITS.initiatives.find(i => i.key === 'OINT-2').podTags =
    ['BOARD - Core Payments Platform', 'BOARD - Core Ledger Platform', 'LEO - Liabilities'];
  renderNow();
  const dup = chips(xcell('OINT-2', 'PODs'));
  check('colliding prefixes are disambiguated',
        dup.join(' ') === 'BOARD·Payments BOARD·Ledger LEO', dup.join(' '));
  check('no duplicate chip labels', new Set(dup).size === dup.length, dup.join(' '));

  // an initiative can carry 31 pods; the cell must not explode
  INITS.initiatives.find(i => i.key === 'OINT-3').podTags =
    Array.from({length: 31}, (_, i) => `P${i} - Pod number ${i}`);
  renderNow();
  const many = xcell('OINT-3', 'PODs');
  check('long pod lists are capped', chips(many).length === 7, chips(many).length);
  check('the overflow chip counts the rest',
        many.querySelector('.podchip.more').textContent.trim() === '+25',
        many.querySelector('.podchip.more').textContent);
  check('all 31 stay in the tooltip', many.querySelector('.podwrap').title.split('\n').length === 31,
        many.querySelector('.podwrap').title.split('\n').length);
  INITS.initiatives.find(i => i.key === 'OINT-2').podTags = ['MY POD'];
  INITS.initiatives.find(i => i.key === 'OINT-3').podTags = ['MY POD'];
  renderNow();
  check('PODs cell is editable', podCell.classList.contains('editable'), podCell.className);
  check('it targets the initiative', podCell.dataset.issue === 'OINT-1', podCell.dataset.issue);

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
        chips(xcell('OINT-1', 'PODs')).join(' ') === 'MY POD COREP',
        chips(xcell('OINT-1', 'PODs')).join(' '));

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
  w.eval('ST.initOpen["OINT-1"]=true; save(); DETAILS={}; renderBoard();');
  check('render kicks off the detail fetch', detailCalls.length === 1, JSON.stringify(detailCalls));
  await new Promise(r => setTimeout(r, 200));
  const det = [...w.document.querySelectorAll('tr.detail')].find(t => t.dataset.k === 'OINT-1');
  check('detail resolves', det && /the description/.test(det.textContent), det && det.textContent.trim());
  check('no duplicate fetch on re-render',
        (() => { w.eval('renderBoard()'); return detailCalls.length === 1; })(), detailCalls.length);

  console.log('\n--- 8. stack-rank survives a pod filter change ---');
  w.eval('ST.order=["OINT-6","OINT-1","OINT-2","OINT-3","OINT-4","OINT-7"]; save();');
  const ranked = JSON.parse(w.localStorage.getItem('monthlyPlanner_v1')).order.slice();
  POD_MODE = 'narrow';
  await w.eval('loadInitiatives(false)');
  await new Promise(r => setTimeout(r, 200));
  const narrowed = JSON.parse(w.localStorage.getItem('monthlyPlanner_v1')).order;
  check('keys outside the filter are kept', ranked.every(k => narrowed.includes(k)), JSON.stringify(narrowed));
  check('new pod keys appended', narrowed.includes('OINT-9'), JSON.stringify(narrowed));
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
  w.eval(`INITS.initiatives.forEach(i => { if(i.key === 'OINT-1'){
            i.summary = ${JSON.stringify(XSS)}; i.engDri = ${JSON.stringify(XSS)};
            i.prodDri = ${JSON.stringify(XSS)}; i.orgPriority = ${JSON.stringify(XSS)}; } });
          ST.cols = ['pri','eng','prod']; renderBoard();`);
  await new Promise(r => setTimeout(r, 50));
  const xrow = [...w.document.querySelectorAll('tr.row')].find(r => r.dataset.k === 'OINT-1');
  check('no injected element in the row', xrow && xrow.querySelector('img') === null,
        xrow && xrow.innerHTML.slice(0, 140));
  check('payload survives as literal text', xrow && xrow.textContent.includes('<img src=x'),
        xrow && xrow.textContent.slice(0, 140));
  check('onerror never fired', w.__pwned === undefined, String(w.__pwned));

  // The expanded detail row renders Impact + Description, both Jira free text, and was the
  // spot the first pass of this fix missed.
  w.eval(`DETAILS['OINT-1'] = { impact: ${JSON.stringify(XSS)},
                                descriptionText: ${JSON.stringify(XSS + ' a & b')} };
          ST.initOpen['OINT-1'] = true; renderBoard();`);
  await new Promise(r => setTimeout(r, 50));
  const xdet = [...w.document.querySelectorAll('tr.detail')].find(t => t.dataset.k === 'OINT-1');
  check('detail row parses no injected element', xdet && xdet.querySelector('img') === null,
        xdet && xdet.innerHTML.slice(0, 140));
  check('impact kept as literal text', xdet && xdet.textContent.includes('<img src=x'),
        xdet && xdet.textContent.slice(0, 100));
  check('ampersand in description survives intact', xdet && xdet.textContent.includes('a & b'),
        xdet && xdet.textContent.slice(-60));
  w.eval("ST.initOpen['OINT-1']=false; DETAILS={}; renderBoard();");

  // Pod names are Jira multi-select option values and render into the filter menu.
  w.eval(`PODS = [${JSON.stringify(XSS)}]; renderPodMenu();`);
  const menu = w.document.querySelector('#ddPods .menu');
  check('pod filter menu parses no injected element', menu && menu.querySelector('img') === null,
        menu && menu.innerHTML.slice(0, 140));
  check('pod name kept as literal text', menu && menu.textContent.includes('<img src=x'),
        menu && menu.textContent.slice(0, 100));

  console.log(fail.length ? `\n${fail.length} of ${ran} FAILED: ${fail.join(', ')}`
                          : `\nall ${ran} checks passed`);
  process.exit(fail.length ? 1 : 0);
})().catch(e => { console.error('CRASH', e); process.exit(1); });
