// Drives the REAL page: click a cell, type a name, press Enter. The first version of this
// board shipped with that path dead (the editor opened, the input cleared, no task appeared)
// because every test here stopped at layout(). This loads the shipped HTML and uses it.
const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

const file = path.join(__dirname, '..', 'derived', 'sprint-board.html');
let ran = 0; const fail = [];
const check = (name, ok, got) => {
  ran++; if (ok) console.log('  ok  ' + name);
  else { fail.push(name); console.log('  FAIL ' + name + (got !== undefined ? '  got: ' + got : '')); }
};

const DAYS = ['2026-11-04','2026-11-05','2026-11-06','2026-11-07','2026-11-08',
              '2026-11-09','2026-11-10','2026-11-11','2026-11-12','2026-11-13'];
const board = {
  v: 1, mode: 'window', generated: '2026-11-01',
  window: {start: DAYS[0], end: DAYS[DAYS.length - 1]},
  sprints: [{id: 0, name: '2026-11-04 → 2026-11-13', start: DAYS[0], end: DAYS[9], state: 'planned'}],
  days: DAYS.map(d => ({date: d, dow: 'Wed', weekend: ['2026-11-07','2026-11-08'].includes(d),
                        holiday: null})),
  people: [
    {name: 'Dev One', canonical: 'dev-one', role: 'SDE1', eff: 0.85, net: 8, sp: 6.8,
     statuses: ['', '', '', 'WE', 'WE', '', 'L', '', '', '']},
    {name: 'Dev Two', canonical: 'dev-two', role: 'SDE2', eff: 0.75, net: 8, sp: 6.0,
     statuses: ['', '', '', 'WE', 'WE', '', '', '', '', '']}],
  tickets: [], projects: [], plan: {tasks: []}
};

const saved = [];
const routes = {
  '/api/sprints': {sprints: [{id: 9372, name: 'OND 26 S1', state: 'active',
                              start: '2026-10-07', end: '2026-10-21'}]},
  '/api/projects': {projects: [{key: 'TEAM', name: 'Team project'}]}
};

// jsdom does not fetch <script src>, so the shared leave editor is run here the way a
// browser would run it: before the page's own scripts, on the same window.
function loadLeaveEditor(w) {
  const js = fs.readFileSync(path.join(__dirname, '..', 'derived', 'leave-editor.js'), 'utf8');
  w.eval(js);
}

const src = fs.readFileSync(file, 'utf8');
const dom = new JSDOM(src, {
  runScripts: 'dangerously', url: 'http://localhost:8765/sprint-board',
  beforeParse(w) {
    loadLeaveEditor(w);
    w.alert = () => {};
    w.fetch = (url, opts) => {
      if (opts && opts.method === 'POST') { saved.push(JSON.parse(opts.body));
        return Promise.resolve({json: () => Promise.resolve({ok: true})}); }
      const base = String(url).split('?')[0];
      const body = base === '/api/sprint-board' ? board : (routes[base] || {});
      return Promise.resolve({json: () => Promise.resolve(body)});
    };
    w.HTMLElement.prototype.scrollIntoView = function () {};
    // jsdom does not fire blur when a focused element is removed, but Chrome does - and that
    // difference hid a real bug: close() removed the input, blur re-entered close(), and the
    // second remove() threw NotFoundError from inside the first, killing the Enter handler
    // before the task was ever created. Make the test DOM behave like the browser.
    // jsdom has no PointerEvent, and the board is driven entirely by pointer events.
    if (!w.PointerEvent) {
      w.PointerEvent = class PointerEvent extends w.MouseEvent {
        constructor(type, init) { super(type, init); this.pointerId = (init || {}).pointerId || 1; }
      };
    }
    // jsdom has no layout, so the drag path's hit-test finds nothing. Returning null is
    // what a browser returns for a pointer that is not over a cell: the drag aborts, and
    // the tap counting that rename depends on still runs.
    w.document.elementFromPoint = () => null;
    // jsdom has no layout, so every rect is zero-sized and the board cannot work out which
    // cell a pointer is in. Give the day columns a width: 65px each, laid left to right,
    // which is what they measure in the browser.
    const CELL_W = 65;
    const realRect = w.Element.prototype.getBoundingClientRect;
    w.Element.prototype.getBoundingClientRect = function () {
      if (this.classList && this.classList.contains('cell')) {
        const i = +this.dataset.i || 0;
        return {left: i * CELL_W, right: (i + 1) * CELL_W, width: CELL_W,
                top: 0, bottom: 34, height: 34, x: i * CELL_W, y: 0};
      }
      return realRect.call(this);
    };
    const rm = w.Element.prototype.remove;
    w.Element.prototype.remove = function () {
      const wasFocused = this.ownerDocument && this.ownerDocument.activeElement === this;
      if (wasFocused) this.dispatchEvent(new w.FocusEvent('blur', {bubbles: false}));
      if (!this.parentNode) {
        throw new w.DOMException(
          "Failed to execute 'remove' on 'Element': The node to be removed is no longer a " +
          'child of this node.', 'NotFoundError');
      }
      return rm.call(this);
    };
  }
});
const w = dom.window, doc = w.document;
const key = (el, k) => el.dispatchEvent(new w.KeyboardEvent('keydown',
  {key: k, bubbles: true, cancelable: true}));

setTimeout(() => {
  console.log('--- the page loads ---');
  const grid = doc.querySelector('table.g');
  check('the capacity grid renders', !!grid, 'no table.g');
  check('one row per person', doc.querySelectorAll('table.g tr.row, table.g tbody tr').length >= 2,
        String(doc.querySelectorAll('table.g tbody tr').length));

  console.log('\n--- clicking a cell and typing a name ---');
  const cell = doc.querySelector('td.cell[data-w="dev-one"][data-i="1"]')
            || doc.querySelector('td.cell:not(.c-WE):not(.c-L)');
  check('a working cell is clickable', !!cell, 'no open cell');
  if (cell) {
    cell.dispatchEvent(new w.MouseEvent('click', {bubbles: true}));
    const inp = cell.querySelector('input.cellin');
    check('clicking opens an editor in the cell', !!inp, 'no input');
    if (inp) {
      inp.focus();
      inp.value = 'Spike: charge engine';
      key(inp, 'Enter');
      const bars = doc.querySelectorAll('.bar, .task, [data-task]');
      check('Enter creates a task', bars.length >= 1, String(bars.length));
      check('the task carries the typed name',
            /Spike: charge engine/.test(doc.querySelector('table.g').textContent),
            doc.querySelector('table.g').textContent.slice(0, 120));
      check('the editor closes', !cell.querySelector('input.cellin'), 'input still there');
      check('the board is now unsaved',
            /unsaved|Save/i.test(doc.getElementById('saveBtn').textContent),
            doc.getElementById('saveBtn').textContent);
    }
  }

  console.log('\n--- Escape abandons ---');
  const c2 = doc.querySelector('td.cell[data-w="dev-two"][data-i="2"]');
  if (c2) {
    c2.dispatchEvent(new w.MouseEvent('click', {bubbles: true}));
    const inp2 = c2.querySelector('input.cellin');
    if (inp2) { inp2.value = 'throwaway'; key(inp2, 'Escape'); }
    check('Escape closes the editor', !c2.querySelector('input.cellin'), 'still open');
    check('and creates nothing', !/throwaway/.test(doc.body.textContent), 'task was created');
  } else check('a second cell exists to test Escape', false, 'not found');

  console.log('\n--- an empty name creates nothing ---');
  const c3 = doc.querySelector('td.cell[data-w="dev-two"][data-i="5"]');
  if (c3) {
    const before = doc.querySelectorAll('.bar, .task, [data-task]').length;
    c3.dispatchEvent(new w.MouseEvent('click', {bubbles: true}));
    const inp3 = c3.querySelector('input.cellin');
    if (inp3) { inp3.value = '   '; key(inp3, 'Enter'); }
    check('a blank name is ignored',
          doc.querySelectorAll('.bar, .task, [data-task]').length === before,
          String(doc.querySelectorAll('.bar, .task, [data-task]').length));
  } else check('a third cell exists', false, 'not found');

  console.log('\n--- renaming a task you typed ---');
  const bar = doc.querySelector('#boardP .bar');
  check('the new task drew a bar', !!bar, 'no bar');
  if (bar) {
    // A real mouse never delivers dblclick here: the drag handler preventDefault()s the
    // pointerdown, which suppresses the compatibility mouse events. Two pointerdowns is
    // what the hardware actually sends, so that is what this drives.
    const pd = () => bar.dispatchEvent(new w.PointerEvent('pointerdown',
      {bubbles: true, cancelable: true, pointerId: 1, clientX: 10, clientY: 10}));
    pd(); pd();
    const cell = bar.closest('td.cell');
    const ed = cell && cell.querySelector('input.cellin');
    check('double-click opens an editor on it', !!ed, 'no editor');
    if (ed) {
      check('prefilled with the current name', ed.value === 'Spike: charge engine', ed.value);
      ed.focus();
      ed.value = 'Spike: charges and tax';
      key(ed, 'Enter');
      check('the name changes on the board',
            /Spike: charges and tax/.test(doc.getElementById('boardP').textContent),
            doc.getElementById('boardP').textContent.slice(0, 80));
      check('the old name is gone',
            !/Spike: charge engine</.test(doc.getElementById('boardP').innerHTML), 'still there');
    }
  }

  console.log('\n--- autosave ---');
  check('autosave is on a 5 second timer', /AUTOSAVE_MS\s*=\s*5000/.test(src), 'not 5000');
  const before = saved.length;
  w.markDirty();
  check('it fires when there is something to save', w.shouldAutosave() === true, 'declined');
  const c4 = doc.querySelector('td.cell[data-w="dev-two"][data-i="8"]');
  if (c4) {
    c4.dispatchEvent(new w.MouseEvent('click', {bubbles: true}));
    check('it holds off while a name is being typed', w.shouldAutosave() === false, 'saved mid-type');
    const open4 = c4.querySelector('input.cellin');
    if (open4) { open4.focus(); key(open4, 'Escape'); }
  }
  check('and resumes once the editor closes', w.shouldAutosave() === true, 'still held off');

  console.log('\n--- the backlog: make a heading once, then keep typing ---');
  const back = doc.getElementById('back');
  check('there is a backlog', !!back, 'no #back');
  const catForm = doc.getElementById('addcat');
  check('a category can be made before there is anything in it', !!catForm, 'no new-category box');
  if (catForm) {
    const plannedBefore = (doc.getElementById('cards').textContent
      .replace(/\s+/g, ' ').match(/Planned days ([\d.]+)/) || [])[1];
    doc.getElementById('catIn').value = 'TBM';
    catForm.dispatchEvent(new w.Event('submit', {bubbles: true, cancelable: true}));
    const head = [...doc.querySelectorAll('#back .cath')].find(h => /TBM/.test(h.textContent));
    check('the empty heading appears', !!head, doc.getElementById('back').textContent.slice(0, 70));
    check('and can be removed while it is empty', !!head && !!head.querySelector('.catx'),
          'no remove control');

    // Type under it, twice, without naming the category again.
    const addTo = cat => {
      const f = [...doc.querySelectorAll('#back .addrow')].find(x => x.dataset.addto === cat);
      return f;
    };
    const typeInto = (cat, line) => {
      const f = addTo(cat);
      if (!f) return false;
      f.querySelector('input').value = line;
      f.dispatchEvent(new w.Event('submit', {bubbles: true, cancelable: true}));
      return true;
    };
    check('the heading has its own input', !!addTo('TBM'), 'no per-category input');
    typeInto('TBM', 'Rollback strategy');
    typeInto('TBM', 'Integration tests PR');

    const tbm = [...doc.querySelectorAll('#back .cat')].find(c => c.dataset.cat === 'TBM');
    const rows = tbm ? tbm.querySelectorAll('.row.note') : [];
    check('both lines land under it without retyping the category', rows.length === 2,
          String(rows.length));
    check('they are rows, one below the other, not tiles side by side',
          /\.rows\{[^}]*flex-direction:column/.test(src), 'still a tile wall');
    check('the heading is no longer removable once it holds work',
          !doc.querySelector('#back .cat[data-cat="TBM"] .catx'), 'still removable');

    const plannedAfter = (doc.getElementById('cards').textContent
      .replace(/\s+/g, ' ').match(/Planned days ([\d.]+)/) || [])[1];
    check('writing ideas down does not spend anyone\'s days', plannedAfter === plannedBefore,
          plannedBefore + ' -> ' + plannedAfter);

    // A heading is saved in its own right, so an empty one is still there tomorrow.
    w.doSave({auto: true});
    const body = saved[saved.length - 1] || {};
    check('the headings are saved with the plan',
          Array.isArray(body.cats) && body.cats.includes('TBM'), JSON.stringify(body.cats));
  }

  console.log('\n--- editing a row in place ---');
  {
    const row = [...doc.querySelectorAll('#back .row.note')]
      .find(r => /Rollback strategy/.test(r.textContent));
    check('there is a row to edit', !!row, 'no row');
    if (row) {
      // Rename: click the name, type, Enter.
      row.querySelector('.rt').dispatchEvent(new w.MouseEvent('click', {bubbles: true}));
      let inp = row.querySelector('input.rowin');
      check('clicking the name opens an editor', !!inp, 'no editor');
      check('prefilled with the current name', !!inp && inp.value === 'Rollback strategy',
            inp && inp.value);
      check('the row stops being draggable while editing', row.draggable === false,
            String(row.draggable));
      if (inp) {
        inp.focus();
        inp.value = 'Rollback strategy finalisation';
        key(inp, 'Enter');
      }
      check('the name changes',
            /Rollback strategy finalisation/.test(doc.getElementById('back').textContent),
            doc.getElementById('back').textContent.slice(0, 80));

      // Effort: click the days, type, Enter.
      const row2 = [...doc.querySelectorAll('#back .row.note')]
        .find(r => /Rollback strategy finalisation/.test(r.textContent));
      check('the row is draggable again', !!row2 && row2.draggable !== false,
            row2 && String(row2.draggable));
      row2.querySelector('.sp').dispatchEvent(new w.MouseEvent('click', {bubbles: true}));
      let din = row2.querySelector('input.rowin.days');
      check('clicking the effort opens an editor', !!din, 'no days editor');
      check('prefilled with the current effort', !!din && din.value === '1', din && din.value);
      if (din) { din.focus(); din.value = '2.5'; key(din, 'Enter'); }
      const row3 = [...doc.querySelectorAll('#back .row.note')]
        .find(r => /Rollback strategy finalisation/.test(r.textContent));
      check('the effort changes', /2\.5d/.test(row3.textContent), row3.textContent.trim());

      // Escape leaves it alone.
      row3.querySelector('.rt').dispatchEvent(new w.MouseEvent('click', {bubbles: true}));
      const esc = row3.querySelector('input.rowin');
      if (esc) { esc.focus(); esc.value = 'throwaway'; key(esc, 'Escape'); }
      check('Escape abandons the edit',
            !/throwaway/.test(doc.getElementById('back').textContent)
            && /Rollback strategy finalisation/.test(doc.getElementById('back').textContent),
            doc.getElementById('back').textContent.slice(0, 80));
    }
  }

  console.log('\n--- moving a task to another category ---');
  {
    // The reported bug: dropping a row on another heading pasted the drag payload into
    // that heading's add box as text, and the task never moved.
    doc.getElementById('catIn').value = 'CASA';
    doc.getElementById('addcat').dispatchEvent(
      new w.Event('submit', {bubbles: true, cancelable: true}));
    const row = [...doc.querySelectorAll('#back .row.note')]
      .find(r => /Integration tests PR/.test(r.textContent));
    const casa = [...doc.querySelectorAll('#back .cat')].find(c => c.dataset.cat === 'CASA');
    check('there is a heading to move it to', !!row && !!casa, 'missing row or heading');
    if (row && casa) {
      const data = {};
      const dt = {setData: (k, v) => { data[k] = v; }, getData: k => data[k]};
      row.dispatchEvent(Object.assign(new w.Event('dragstart', {bubbles: true}),
                                      {dataTransfer: dt}));
      const over = Object.assign(new w.Event('dragover', {bubbles: true, cancelable: true}),
                                 {dataTransfer: dt});
      casa.dispatchEvent(over);
      check('the heading accepts the drag', over.defaultPrevented, 'drop would not fire');
      const drop = Object.assign(new w.Event('drop', {bubbles: true, cancelable: true}),
                                 {dataTransfer: dt});
      casa.dispatchEvent(drop);
      check('and the browser is stopped from pasting the payload as text',
            drop.defaultPrevented, 'payload would land in the add box');
      const nowIn = [...doc.querySelectorAll('#back .cat')]
        .find(c => c.dataset.cat === 'CASA');
      check('the task moves to that heading', /Integration tests PR/.test(nowIn.textContent),
            nowIn.textContent.slice(0, 70));
      const tbm2 = [...doc.querySelectorAll('#back .cat')].find(c => c.dataset.cat === 'TBM');
      check('and leaves the one it came from',
            !/Integration tests PR/.test(tbm2.textContent), tbm2.textContent.slice(0, 70));
      check('no add box was filled with JSON',
            ![...doc.querySelectorAll('#back input')].some(i => /"from"/.test(i.value)),
            'payload text landed in an input');
    }
  }

  // Drag a row onto a day: now it belongs to someone.
  const note = doc.querySelector('#back .row.note');
  const cellFor = doc.querySelector('td.cell[data-w="dev-two"][data-i="9"]')
               || doc.querySelector('td.cell.open');
  if (note && cellFor) {
    const data = {};
    const dt = {setData: (k, v) => { data[k] = v; }, getData: k => data[k]};
    note.dispatchEvent(Object.assign(new w.Event('dragstart', {bubbles: true}),
                                     {dataTransfer: dt}));
    check('the backlog says which task is moving', /"from":"backlog"/.test(data['text/plain']),
          data['text/plain']);
    cellFor.dispatchEvent(Object.assign(new w.Event('drop', {bubbles: true, cancelable: true}),
                                        {dataTransfer: dt}));
    check('it lands on the board',
          /Rollback strategy/.test(doc.getElementById('boardP').textContent),
          doc.getElementById('boardP').textContent.slice(0, 80));
    check('and leaves the backlog',
          ![...doc.querySelectorAll('#back .row.note')].some(r => /Rollback strategy/.test(r.textContent)),
          doc.getElementById('back').textContent.slice(0, 70));
  } else check('a row can be dragged onto a day', false, 'no row or no open cell');

  // Drag it back: that is what taking it out of the sprint means.
  const bar3 = [...doc.querySelectorAll('#boardP .bar')]
    .find(b => /Rollback strategy/.test(b.textContent));
  if (bar3 && back) {
    const cell3 = bar3.closest('td.cell');
    doc.elementFromPoint = (x) => (x < 100 ? cell3 : back);
    const pe = (t, x) => new w.PointerEvent(t, {bubbles: true, cancelable: true, pointerId: 1,
                                                clientX: x, clientY: 5});
    bar3.dispatchEvent(pe('pointerdown', 5));
    const tab = doc.querySelector('#boardP table.g');
    tab.dispatchEvent(pe('pointermove', 500));
    tab.dispatchEvent(pe('pointerup', 500));
    doc.elementFromPoint = () => null;
    check('dropping a bar on the backlog takes it out of the sprint',
          !/Rollback strategy/.test(doc.getElementById('boardP').textContent),
          'still on the board');
    check('and it comes back under its own heading',
          !![...doc.querySelectorAll('#back .cat')].find(
            c => c.dataset.cat === 'TBM' && /Rollback strategy/.test(c.textContent)),
          doc.getElementById('back').textContent.slice(0, 90));
  } else check('a scheduled task can be dragged back', false, 'no bar to drag');

  console.log('\n--- the From / To window ---');
  const fd = doc.getElementById('fromD'), td = doc.getElementById('toD');
  check('there is a From date input', fd && fd.type === 'date', fd && fd.type);
  check('there is a To date input', td && td.type === 'date', td && td.type);
  check('there is a way back to the sprint', !!doc.getElementById('clearWin'), 'no clearWin');
  if (fd && td) {
    fd.value = '2026-11-04'; td.value = '2026-11-13';
    fd.dispatchEvent(new w.Event('change', {bubbles: true}));
    td.dispatchEvent(new w.Event('change', {bubbles: true}));
  }

  console.log('\n--- saving ---');
  // doSave runs one at a time, so the earlier save in the backlog section has to finish
  // before this one. Same reason the real board does not stack them.
  setTimeout(() => {
  console.log('\n--- switching the filter keeps unsaved work ---');
  {
    // Each sprint and each date window has its OWN plan, so a switch is a different plan.
    // An edit made a second ago must be saved before the next one loads over it.
    const before = saved.length;
    w.markDirty();
    const fd2 = doc.getElementById('fromD'), td3 = doc.getElementById('toD');
    fd2.value = '2026-12-01'; td3.value = '2026-12-10';
    fd2.dispatchEvent(new w.Event('change', {bubbles: true}));
    check('changing the window saves the plan being left', saved.length > before,
          String(saved.length) + ' vs ' + before);
  }


  w.doSave({auto: true});
  setTimeout(() => {
    check('autosave posts the plan', saved.length > before, String(saved.length));
    check('the button says it saved', /^Saved/.test(doc.getElementById('saveBtn').textContent),
          doc.getElementById('saveBtn').textContent);
    const last = saved[saved.length - 1] || {};
    check('the window goes with it', last.from === '2026-11-04' && last.to === '2026-11-13',
          last.from + '..' + last.to);
    check('no sprint id is sent in date mode', Array.isArray(last.sprints) && !last.sprints.length,
          JSON.stringify(last.sprints));
    check('every save carries the headings', Array.isArray(last.cats), JSON.stringify(last.cats));
    console.log(fail.length ? `\n${fail.length} of ${ran} FAILED: ${fail.join(', ')}`
                            : `\nall ${ran} checks passed`);
    process.exit(fail.length ? 1 : 0);
  }, 60);
  }, 30);
}, 150);
