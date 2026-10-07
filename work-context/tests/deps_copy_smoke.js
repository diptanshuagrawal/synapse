/**
 * Dependency-register copy text: the chase list you paste to another pod's EM.
 *
 * It is graded against the SOP v2 handover score, so the grouping has to match how the
 * program team actually counts a row. Source: engg-cycle-planner scripts/emplan/scorer.py
 *   1  no delivery epic for that team
 *   3  epic linked, but the month's budget FIELD is empty
 *  10  epic linked and the field is filled
 * A budget field SET to 0 counts as filled for the score while committing nothing, so it
 * gets its own section rather than silently passing or silently failing.
 *
 * copyText() is extracted from deps.html and run directly: it is pure string building, so
 * a DOM is unnecessary and the extraction keeps the test honest about the shipped source.
 */
'use strict';
const fs = require('fs');
const path = require('path');

const SRC = path.join(__dirname, '..', 'derived', 'deps.html');
const html = fs.readFileSync(SRC, 'utf8');
const block = html.match(/function depScore[\s\S]*?\nfunction copyText[\s\S]*?\n\}/);
if (!block) {
  console.error('CRASH could not extract copyText from deps.html');
  process.exit(1);
}

let ran = 0;
const fail = [];
function check(name, cond, detail) {
  ran++;
  if (cond) { console.log('  ok  ' + name); }
  else { fail.push(name); console.log('FAIL  ' + name + '  -> ' + (detail === undefined ? '' : detail)); }
}

// ---- harness: the globals deps.html's copyText closes over -------------------
// copyText closes over these in the page, so they have to be real globals here
globalThis.D = {
  myPods: ['CORE - Core Platform'],
  me: { name: 'Sam Lee' },
  months: [{ label: 'Oct-26' }, { label: 'Nov-26' }],
};
globalThis.CYCLES = [];
globalThis.cycleText = () => globalThis.CYCLES.join(', ');
const { depScore, copyText } =
  new Function(block[0] + '; return {depScore, readinessRows, copyText};')();

const row = (key, over) => Object.assign({
  key, summary: key + ' summary', url: 'u/' + key,
  pod: 'DISC - Discovery', project: 'DISC',
  epic: '', epicDue: '', cycles: ['Oct-26', 'Nov-26'],
  months: { 'Oct-26': 0, 'Nov-26': 0 },
  monthsSet: { 'Oct-26': false, 'Nov-26': false },
}, over);

console.log('--- 1. scorer matches the SOP ---');
check('no epic is 1', depScore(false, true) === 1, String(depScore(false, true)));
check('epic but empty budget field is 3', depScore(true, false) === 3, String(depScore(true, false)));
check('epic and filled field is 10', depScore(true, true) === 10, String(depScore(true, true)));

console.log('\n--- 2. a missing epic is grouped and dated ---');
let out = copyText([row('IDEA-1')], 'DISC - Discovery');
check('names the pod', /DISC - Discovery/.test(out), out.split('\n')[0]);
check('groups it under no delivery epic', /NO DELIVERY EPIC/.test(out), out);
check('says which cycles need it', /needed for Oct-26, Nov-26/.test(out), out);
check('carries the initiative link', /u\/IDEA-1/.test(out), out);
check('asks for the epic to be created', /create and link a delivery epic/.test(out), out);
check('reports the score', /average 1\/10/.test(out), out);

console.log('\n--- 3. epic exists but the budget field is empty ---');
out = copyText([row('IDEA-2', { epic: 'DISC-5', epicDue: '2026-10-31' })], 'DISC - Discovery');
check('grouped as no budget', /EPIC EXISTS BUT NO BUDGET/.test(out), out);
check('not reported as a missing epic', !/NO DELIVERY EPIC/.test(out), out);
check('names the epic and the months', /DISC-5 has no budget for Oct-26, Nov-26/.test(out), out);
check('scores 3', /average 3\/10/.test(out), out);

console.log('\n--- 4. a budget set to zero is its own case ---');
// the SOP counts the field as filled, so this scores 10 while committing nothing
out = copyText([row('IDEA-3', {
  epic: 'DISC-6', epicDue: '2026-10-31',
  months: { 'Oct-26': 0, 'Nov-26': 0 },
  monthsSet: { 'Oct-26': true, 'Nov-26': true },
})], 'DISC - Discovery');
check('scores 10, matching the published sheet', /average 10\/10/.test(out), out);
check('but is still called out', /BUDGETED 0 SP/.test(out), out);
check('and says nothing is committed', /nothing is committed/.test(out), out);
check('is not listed as already ready', !/ALREADY READY/.test(out), out);
check('asks for the zeros to be confirmed', /confirm the 0 SP entries/.test(out), out);

console.log('\n--- 5. a fully ready row ---');
out = copyText([row('IDEA-4', {
  epic: 'DISC-7', epicDue: '2026-10-31',
  months: { 'Oct-26': 5, 'Nov-26': 3 },
  monthsSet: { 'Oct-26': true, 'Nov-26': true },
})], 'DISC - Discovery');
check('listed as already ready', /ALREADY READY/.test(out), out);
check('with its epic and due date', /DISC-7 · due 2026-10-31/.test(out), out);
check('nothing outstanding', /Nothing outstanding/.test(out), out);
check('no gap sections at all',
      !/NO DELIVERY EPIC|NO BUDGET|BUDGETED 0 SP/.test(out), out);

console.log('\n--- 6. ready and not-ready in different months ---');
// fine in Oct, missing in Nov: it must appear ONCE, as a gap, never also as ready
out = copyText([row('IDEA-5', {
  epic: 'DISC-8', epicDue: '2026-10-31',
  months: { 'Oct-26': 5, 'Nov-26': 0 },
  monthsSet: { 'Oct-26': true, 'Nov-26': false },
})], 'DISC - Discovery');
check('reported as a gap', /DISC-8 has no budget for Nov-26/.test(out), out);
check('and NOT also as ready', !/ALREADY READY/.test(out), out);
check('score averages the two months', /average 6\.5\/10/.test(out), out);

console.log('\n--- 7. an initiative outside the viewed cycle ---');
// chasing it is still useful, but it must be labelled with ITS cycle, not the view's
globalThis.CYCLES = ['Oct-26', 'Nov-26'];
out = copyText([row('IDEA-6', { cycles: ['Dec-26'] })], 'DISC - Discovery');
check('labelled with its own cycle', /needed for Dec-26/.test(out), out);
check('not claimed for the viewed months', !/needed for Oct-26/.test(out), out);
globalThis.CYCLES = [];

console.log('\n--- 7b. acceptance is the due date, and is not scored ---');
// SOP: "a tagged team with ... an epic with no target date counts as a missing/unaccepted
// dependency". The score does not see it, so these read green on the published sheet.
out = copyText([row('IDEA-A', {
  epic: 'DISC-11', epicDue: '',
  months: { 'Oct-26': 5, 'Nov-26': 5 },
  monthsSet: { 'Oct-26': true, 'Nov-26': true },
})], 'DISC - Discovery');
check('still scores 10', /average 10\/10/.test(out), out);
check('but is reported as not accepted', /NOT FORMALLY ACCEPTED/.test(out), out);
check('says the due date is the acceptance signal', /acceptance signal/.test(out), out);
check('warns that it reads green on the sheet', /read green on the sheet/.test(out), out);
check('asks for a due date', /signals you have accepted the dependency/.test(out), out);
check('not listed as already ready', !/ALREADY READY/.test(out), out);

console.log('\n--- 7c. a cancelled epic is not a linked epic ---');
out = copyText([row('IDEA-B', {
  epic: 'DISC-12', epicStatus: 'Cancelled', epicDue: '2026-10-31',
  months: { 'Oct-26': 5, 'Nov-26': 5 },
  monthsSet: { 'Oct-26': true, 'Nov-26': true },
})], 'DISC - Discovery');
check('a cancelled epic scores 1', /average 1\/10/.test(out), out);
check('and is listed as needing a delivery epic', /NO DELIVERY EPIC/.test(out), out);

out = copyText([row('IDEA-C', {
  epic: 'DISC-13', epicStatus: 'Done', epicDue: '2026-10-31',
  months: { 'Oct-26': 5, 'Nov-26': 5 },
  monthsSet: { 'Oct-26': true, 'Nov-26': true },
})], 'DISC - Discovery');
check('a DONE epic still counts as linked', /average 10\/10/.test(out), out);
check('and is already ready', /ALREADY READY/.test(out), out);

console.log('\n--- 8. a mixed pod reads as a checklist ---');
out = copyText([
  row('IDEA-7'),
  row('IDEA-8', { epic: 'DISC-9',  epicDue: '2026-10-31',
                  monthsSet: { 'Oct-26': true, 'Nov-26': false }, months: { 'Oct-26': 4, 'Nov-26': 0 } }),
  row('IDEA-9', { epic: 'DISC-10', epicDue: '2026-10-31',
                  monthsSet: { 'Oct-26': true, 'Nov-26': true }, months: { 'Oct-26': 6, 'Nov-26': 6 } }),
], 'DISC - Discovery');
check('every section present', /NO DELIVERY EPIC/.test(out) && /NO BUDGET/.test(out)
      && /ALREADY READY/.test(out), out);
check('each initiative appears exactly once',
      ['IDEA-7', 'IDEA-8', 'IDEA-9'].every(k =>
        (out.match(new RegExp(k + ' —', 'g')) || []).length +
        (out.match(new RegExp(k + ' — DISC', 'g')) || []).length <= 2), out);
check('6 rows counted (3 initiatives x 2 months)', /\/6 ready/.test(out), out);
check('asks cover every gap kind',
      /create and link a delivery epic/.test(out) && /fill the monthly budget/.test(out), out);

console.log(fail.length ? `\n${fail.length} of ${ran} FAILED: ${fail.join(', ')}`
                        : `\nall ${ran} checks passed`);
process.exit(fail.length ? 1 : 0);
