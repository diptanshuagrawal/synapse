/**
 * Marking leave the team never posted, on every capacity calendar in Synapse.
 *
 * derived/leave-editor.js is loaded and run for real: it is a plain IIFE that puts one
 * object on `window`, so a couple of DOM stubs are all it needs. The cell decoration is
 * pure string building and gets the bulk of the checks, because that is the half that
 * decides whether a day can be marked at all.
 *
 * The two things worth protecting:
 *   - a weekend or a public holiday is nobody's leave to mark, and must stay inert
 *   - "present" and "cleared" are different: P asserts the person WAS working and
 *     overrides a reported leave, clearing just lets the ingest win again
 */
'use strict';
const fs = require('fs');
const path = require('path');

const SRC = path.join(__dirname, '..', 'derived', 'leave-editor.js');
const src = fs.readFileSync(SRC, 'utf8');

let ran = 0;
const fail = [];
function check(name, cond, detail) {
  ran++;
  if (cond) { console.log('  ok  ' + name); }
  else { fail.push(name); console.log('FAIL  ' + name + '  -> ' + (detail === undefined ? '' : detail)); }
}

// ---- the little of a browser this file touches -------------------------------
const made = [];
globalThis.document = {
  getElementById: () => null,
  createElement: (tag) => { const el = { tagName: tag, id: '', textContent: '' }; made.push(el); return el; },
  documentElement: { appendChild: (el) => made.push(el) },
  head: { appendChild: (el) => made.push(el) },
  querySelectorAll: () => [],
};
globalThis.window = globalThis;
globalThis.fetch = () => Promise.resolve({ json: () => Promise.resolve({ ok: true }) });

(0, eval)(src);
const LE = globalThis.LeaveEditor;
check('the module publishes itself', !!LE, typeof LE);

// ---- which days can be marked ------------------------------------------------
check('an available day is markable', LE.attrs('dev-one', '2026-11-04', '', '').includes('data-lv-c="dev-one"'));
check('a reported leave day is markable', LE.attrs('dev-one', '2026-11-04', 'L', '') !== '');
check('an on-call day is markable', LE.attrs('dev-one', '2026-11-04', 'O', '') !== '');
check('a WFH day is markable', LE.attrs('dev-one', '2026-11-04', 'W', '') !== '');

check('a weekend is NOT markable', LE.attrs('dev-one', '2026-11-07', 'WE', '') === '',
      LE.attrs('dev-one', '2026-11-07', 'WE', ''));
check('a public holiday is NOT markable', LE.attrs('dev-one', '2026-11-04', 'H', '') === '');
check('isFixed agrees with attrs', LE.isFixed('WE') && LE.isFixed('H') && !LE.isFixed('L'));
check('a weekend gets no cell class either', LE.cls('WE', '') === '', LE.cls('WE', ''));

// A row with no person id cannot be written back, so it must not look clickable.
check('a cell with no canonical is not markable', LE.attrs('', '2026-11-04', '', '') === '');
check('a cell with no date is not markable', LE.attrs('dev-one', '', '', '') === '');

// ---- an override looks different from a reported absence ---------------------
check('an untouched day is plain', LE.cls('L', '') === 'lv-cell', LE.cls('L', ''));
check('an overridden day is marked as one', LE.cls('L', 'L') === 'lv-cell lv-man', LE.cls('L', 'L'));
check('the override is carried for the next click', LE.attrs('d', '2026-11-04', '', 'W').includes('data-lv-cur="W"'));
check('no override means an empty cur', LE.attrs('d', '2026-11-04', '', '').includes('data-lv-cur=""'));

// ---- the cycle ----------------------------------------------------------------
check('nothing -> leave', LE.CYCLE[''] === 'L');
check('leave -> WFH', LE.CYCLE['L'] === 'W');
check('WFH -> present', LE.CYCLE['W'] === 'P');
check('present -> cleared', LE.CYCLE['P'] === '');
check('the cycle closes', Object.keys(LE.CYCLE).every(k => k in LE.CYCLE));
// P is the one that is easy to lose: without it an ingested leave cannot be contradicted.
check('present is a state of its own, not the same as cleared',
      LE.CYCLE['W'] === 'P' && LE.CYCLE['P'] === '' && LE.WORD['P'] !== LE.WORD['']);
check('present says what it does', /overrides/.test(LE.WORD['P']), LE.WORD['P']);
check('every code has a word', ['L', 'W', 'P', ''].every(c => typeof LE.WORD[c] === 'string'));

// ---- the title tells you what the click will do -------------------------------
const t = LE.attrs('dev-one', '2026-11-04', '', '', 'Dev One');
check('the title names the person', t.includes('Dev One'), t);
check('the title names the date', t.includes('2026-11-04'), t);
check('the title explains the cycle', /leave/.test(t) && /WFH/.test(t) && /present/.test(t), t);
check('an existing override is spelled out', LE.attrs('d', '2026-11-04', '', 'L', 'Dev').includes('marked here'));
check('the canonical stands in when there is no display name',
      LE.attrs('dev-one', '2026-11-04', '', '').includes('dev-one'));

// ---- attributes are escaped ----------------------------------------------------
const nasty = LE.attrs('a"b', '2026-11-04', '', '', '<script>');
check('a quote in an id cannot break out of the attribute', !nasty.includes('a"b'), nasty);
check('a tag in a name is escaped', !nasty.includes('<script>'), nasty);

// ---- wiring ---------------------------------------------------------------------
// The stylesheet comes in with the first wire(), not on load: a page that never draws a
// calendar should not carry its CSS.
check('no stylesheet before anything is wired', !made.some(e => e.id === 'lv-style'));

const wired = [];
function cell(attrs) {
  const ds = Object.assign({}, attrs);
  return {
    dataset: ds,
    classList: { add() {}, remove() {}, contains: () => false },
    addEventListener: (ev, fn) => wired.push({ ev, fn, ds }),
  };
}
const cells = [cell({ lvC: 'dev-one', lvD: '2026-11-04', lvCur: '' })];
LE.wire({ querySelectorAll: () => cells }, { by: 'test' });
check('a style element is created on first wire', made.some(e => e.id === 'lv-style'));
check('the cell gets a click handler', wired.length === 1 && wired[0].ev === 'click');
check('a wired cell is remembered', cells[0].dataset.lvWired === '1');

// Wiring the same grid twice must not stack handlers: pages re-render on every change.
LE.wire({ querySelectorAll: () => cells }, { by: 'test' });
check('wiring twice does not double the handlers', wired.length === 1, wired.length);

console.log(fail.length ? `\n${fail.length} of ${ran} FAILED: ${fail.join(', ')}`
                        : `\nall ${ran} checks passed`);
process.exit(fail.length ? 1 : 0);
