/**
 * Sprint board layout: a bar occupies its owner's WORKING days.
 *
 * This is the one piece of the board that is easy to get silently wrong. A five-day task
 * for someone with two days of leave in the middle really does span seven calendar days,
 * and a planner that lays it out as a flat five-cell block is a spreadsheet with colours —
 * it will promise a delivery date the person cannot meet.
 *
 * layout() and offFor() are extracted from the shipped page and run directly: pure
 * arithmetic over a statuses array, so no DOM is needed.
 */
'use strict';
const fs = require('fs');
const path = require('path');

const SRC = path.join(__dirname, '..', 'derived', 'sprint-board.html');
const html = fs.readFileSync(SRC, 'utf8');
const off = html.match(/const offFor = [\s\S]*?;\n/);
const lay = html.match(/function layout\(task\)\{[\s\S]*?\n\}/);
const wlen = html.match(/function workLen\([\s\S]*?\n\}/);
// A day splits into quarters, so almost everything on the board now needs the quarter
// helpers in scope. Pulled from the page once and prepended to every extracted function.
const QUARTER = ['const STEP = [^\n]*', 'const EPS = [^\n]*', 'const snapQ = [^\n]*', 'const nq = [\\s\\S]*?;\n']
  .map(re => (html.match(new RegExp(re)) || [''])[0]).join('\n');
if (!off || !lay || !wlen || !/snapQ/.test(QUARTER)) {
  console.error('CRASH could not extract layout() from sprint-board.html'); process.exit(1); }

const effSrc = ['const effOf = [^\\n]*', 'function effortOf\\([\\s\\S]*?\\n\\}',
                'function spanOf\\([\\s\\S]*?\\n\\}', 'function setEffort\\([\\s\\S]*?\\n\\}']
  .map(re => (html.match(new RegExp(re)) || [''])[0]).join('\n');

let ran = 0; const fail = [];
function check(name, cond, detail) {
  ran++;
  if (cond) console.log('  ok  ' + name);
  else { fail.push(name); console.log('FAIL  ' + name + '  -> ' + (detail === undefined ? '' : detail)); }
}

// D is read at CALL time, not capture time: the page keeps it as a module-level let that
// changes on every load, so binding a snapshot here would test a fixture the code never sees.
const posSrc = ['const dayIdx = [^\n]*', 'const dateAt = [^\n]*', 'function posOf\\([\\s\\S]*?\\n\\}']
  .map(re => (html.match(new RegExp(re)) || [''])[0]).join('\n');
const { layout, workLen, snapQ, posOf } = new Function(
  `${QUARTER} ${off[0]} ${posSrc} ${lay[0]} ${wlen[0]} return {layout, workLen, snapQ, posOf};`)();
const setD = d => { globalThis.D = d; };

const mkD = statuses => ({ days: statuses.map((_, i) => ({ date: '2026-10-' + String(i + 1).padStart(2, '0') })),
                          people: [{ canonical: 'p1', statuses }] });
// A scheduled task is pinned to a DATE. The helper takes a day index for readability and
// converts, exactly as the page does when something is dropped on a cell.
const task = (start, days) => ({ who: 'p1', date: globalThis.D.days[Math.floor(start)].date,
                                 off: Math.round((start - Math.floor(start)) * 4) / 4, days });

console.log('--- 1. a clear run is contiguous ---');
setD(mkD(['', '', '', '', '']));
let r = layout(task(0, 3));
check('one span', r.spans.length === 1, JSON.stringify(r.spans));
check('covers the first three days', r.spans[0].from === 0 && r.spans[0].to === 3, JSON.stringify(r.spans));
check('nothing left over', r.unplaced === 0, String(r.unplaced));

console.log('\n--- 2. a weekend splits the bar and pushes the end out ---');
setD(mkD(['', '', 'WE', 'WE', '', '']));
r = layout(task(0, 4));
check('two spans', r.spans.length === 2, JSON.stringify(r.spans));
check('the weekend is skipped, not consumed', r.spans[1].from === 4, JSON.stringify(r.spans));
check('4 working days still land', r.end === 6, String(r.end));

console.log('\n--- 3. that person\'s own leave is skipped too ---');
// the whole point: capacity is per person, so one dev\'s leave moves only their bars
setD(mkD(['', 'L', 'L', '', '']));
r = layout(task(0, 3));
check('leave days are stepped over', r.spans.length === 2 && r.spans[1].from === 3, JSON.stringify(r.spans));
check('and the task ends later than its length', r.end === 5, String(r.end));

console.log('\n--- 4. on-call and holidays are non-working too ---');
setD(mkD(['', 'O', '', 'H', '']));
r = layout(task(0, 3));
check('on-call skipped', !r.spans.some(s => s.from <= 1 && s.to > 1), JSON.stringify(r.spans));
check('holiday skipped', !r.spans.some(s => s.from <= 3 && s.to > 3), JSON.stringify(r.spans));
check('three working days placed', r.end === 5, String(r.end));

console.log('\n--- 5. WFH is a working day ---');
setD(mkD(['W', 'W', 'W']));
r = layout(task(0, 3));
check('wfh counts as available', r.spans.length === 1 && r.unplaced === 0, JSON.stringify(r));

console.log('\n--- 6. work that cannot fit is reported, not silently dropped ---');
setD(mkD(['', '', 'WE', 'WE']));
r = layout(task(0, 5));
check('the overflow is counted', r.unplaced === 3, String(r.unplaced));
check('what fits is still drawn', r.spans.length === 1 && r.spans[0].to === 2, JSON.stringify(r.spans));

console.log('\n--- 7. starting on a non-working day begins at the next one ---');
setD(mkD(['L', '', '']));
r = layout(task(0, 2));
check('starts on the first day actually available', r.spans[0].from === 1, JSON.stringify(r.spans));

console.log('\n--- 8. work dated outside this window draws nothing ---');
setD(mkD(['', '']));
// The plan is one list across every sprint and window, so a task scheduled on a day this
// window does not cover is not an error - it belongs to a different stretch of calendar.
check('a day the window does not cover draws nothing',
      layout({ who: 'p1', date: '2027-03-01', off: 0, days: 1 }) === null, 'expected null');
check('an unknown person draws nothing',
      layout({ who: 'nobody', date: globalThis.D.days[0].date, off: 0, days: 1 }) === null,
      'expected null');
check('and a task with no day at all draws nothing',
      layout({ who: 'p1', date: '', off: 0, days: 1 }) === null, 'expected null');



console.log('\n--- 9. creating a task from a cell ---');
// Enter in a cell editor shipped doing NOTHING: the editor opened, the input cleared, and no
// task appeared. The commit lived inside the keydown closure where no test could reach it,
// so this drives the same addTaskAt() the key handler calls.
const add = html.match(/function addTaskAt\([\s\S]*?\n\}/);
check('the commit path is extractable', !!add, 'addTaskAt not found');
if (add) {
  const nxt = html.match(/function nextTint\([\s\S]*?\n\}/);
  check('the tint chooser is extractable', !!nxt, 'nextTint not found');
  const env = new Function(`
    ${QUARTER}
    ${posSrc}
    const D = {days: [0,1,2,3,4,5,6,7,8,9].map(i =>
      ({date: '2026-11-' + String(i + 1).padStart(2, '0')}))};
    const BAR_TINTS = 6;
    ${nxt ? nxt[0] : 'function nextTint(){ return 1; }'}
    let TASKS = []; let dirty = false;
    const uid = () => 't' + (TASKS.length + 1);
    const markDirty = () => { dirty = true; };
    ${add[0]}
    return {addTaskAt, get tasks(){ return TASKS; }, get dirty(){ return dirty; }};`)();
  const t = env.addTaskAt('p1', 3, 'Spike: charges');
  check('a task is created', env.tasks.length === 1, String(env.tasks.length));
  check('on the person whose cell was clicked', t && t.who === 'p1', t && t.who);
  check('on the day that was clicked', t && t.date === '2026-11-04' && t.off === 0,
        t && t.date + '+' + t.off);
  check('one day long by default', t && t.days === 1, t && String(t.days));
  check('with the typed name', t && t.title === 'Spike: charges', t && t.title);
  check('and the board is marked unsaved', env.dirty === true, String(env.dirty));
  check('it gets a colour of its own', t && t.tint >= 1 && t.tint <= 6, t && String(t.tint));

  check('an empty name creates nothing', env.addTaskAt('p1', 1, '   ') === null
        && env.tasks.length === 1, String(env.tasks.length));
  check('a missing person creates nothing', env.addTaskAt('', 1, 'x') === null, 'created one');
  const t2 = env.addTaskAt('p1', -4, 'clamped');
  check('a negative day is clamped into the sprint', t2 && t2.date === '2026-11-01',
        t2 && t2.date);
  const t3 = env.addTaskAt('p1', '2', 'from string');
  check('a string day index still parses', t3 && t3.date === '2026-11-03', t3 && t3.date);
}

console.log('\n--- renaming a task ---');
const ren = html.match(/function renameTask\([\s\S]*?\n\}/);
check('the rename path is extractable', !!ren, 'renameTask not found');
if (ren && add) {
  const env2 = new Function(`
    ${QUARTER}
    let TASKS = [{id:'t1', title:'old name', who:'p1', start:0, days:1, key:''}];
    let dirty = false;
    const markDirty = () => { dirty = true; };
    ${ren[0]}
    return {renameTask, get tasks(){ return TASKS; }, get dirty(){ return dirty; }};`)();
  const r = env2.renameTask('t1', '  new name  ');
  check('the name changes', env2.tasks[0].title === 'new name', env2.tasks[0].title);
  check('it is trimmed', r && r.title === 'new name', r && r.title);
  check('and the board is marked unsaved', env2.dirty === true, String(env2.dirty));
  check('an empty name is refused', env2.renameTask('t1', '   ') === null
        && env2.tasks[0].title === 'new name', env2.tasks[0].title);
  check('an unknown id is refused', env2.renameTask('nope', 'x') === null, 'renamed something');
  check('an unchanged name is a no-op', env2.renameTask('t1', 'new name') === null,
        'reported a change');
}

console.log('\n--- colours spread across a full board ---');
const nxt2 = html.match(/function nextTint\([\s\S]*?\n\}/);
if (nxt2) {
  const N = new Function(`const BAR_TINTS = 6; ${nxt2[0]} return nextTint;`)();
  // Seven tasks on four colours is what the hash gave us; the chooser must do better.
  const board = [];
  for (let i = 0; i < 7; i++) board.push({tint: N(board)});
  const used = new Set(board.map(t => t.tint));
  check('seven tasks use all six colours', used.size === 6, String(used.size));
  const counts = {}; board.forEach(t => counts[t.tint] = (counts[t.tint] || 0) + 1);
  check('and none is used three times', Math.max(...Object.values(counts)) <= 2,
        JSON.stringify(counts));
  check('an empty board starts at the first colour', N([]) === 1, String(N([])));
  check('a junk tint on a saved task is ignored',
        N([{tint: 99}, {tint: null}, {tint: 1}]) === 2, String(N([{tint: 99}, {tint: 1}])));
}

console.log('\n--- a long name wraps, and spreads across the bar ---');
const grab = n => (html.match(new RegExp('function ' + n + '\\([\\s\\S]*?\\n\\}\\n')) || [])[0];
const pieces = ['packOne', 'packChunks', 'markCut', 'spreadChunks', 'flowChunks'].map(grab);
check('the flow rule is extractable', pieces.every(Boolean), 'not found');
if (pieces.every(Boolean)) {
  const F = new Function(pieces.join('') + 'return flowChunks;')();
  // A segment holds `n` characters per line. Real boxes measure pixels over wrapped lines;
  // the rule under test is how text is handed on, which is the same either way.
  const segs = (...caps) => L => caps.map(c => s => s.length <= c * L);
  const NAME = 'Cheque instrument backfill';

  // The reported bug: three cramped lines in the narrow first cell, the wide cell beside
  // it left empty.
  const spread = F(NAME, segs(7, 9), 4);
  check('text spreads onto the next cell instead of cramming the first',
        spread[1].length > 0, JSON.stringify(spread));
  check('and it breaks on a word', spread[0] === 'Cheque', JSON.stringify(spread));
  check('the wider segment carries the bulk', spread[1].length > spread[0].length,
        JSON.stringify(spread));
  check('nothing is lost', spread.join(' ').replace(/\s+/g, ' ').trim() === NAME,
        JSON.stringify(spread));
  check('nothing is repeated', spread[0] !== spread[1], JSON.stringify(spread));

  check('a name that fits on one line stays on one',
        JSON.stringify(F('Spike', segs(20, 20), 4)) === '["Spike",""]',
        JSON.stringify(F('Spike', segs(20, 20), 4)));

  // The reported bug in its purest form: the first segment CAN hold the whole name
  // stacked up, and must not, while the segment beside it sits empty.
  const roomy = F('one two three four', segs(9, 9), 4);
  check('a segment does not stack text it could pass on',
        roomy[0] === 'one two' && roomy[1] === 'three four', JSON.stringify(roomy));

  const three = F('alpha beta gamma delta', segs(6, 6, 12), 2);
  check('three segments each take their share', three.every(x => x.length > 0),
        JSON.stringify(three));
  check('and still spell the whole name',
        three.join(' ').replace(/\s+/g, ' ').trim() === 'alpha beta gamma delta',
        JSON.stringify(three));

  // A word no arrangement can keep whole has to be broken.
  const longWord = F('Reconciliationoftheledger x', segs(7, 9), 4);
  check('a word too long for any segment is split', longWord[0].length > 0 && longWord[1].length > 0,
        JSON.stringify(longWord));
  check('and none of it is dropped',
        longWord.join('').replace(/\s+/g, '') === 'Reconciliationoftheledgerx',
        JSON.stringify(longWord));

  // A one-day bar has nowhere to continue to, so it must say the name is cut off.
  const cut = F('Cheque instrument backfill reconciliation', segs(6), 2);
  check('a name too long for the whole bar is marked', cut[0].endsWith('…'), cut[0]);
  check('and room is made for the mark', cut[0].length <= 12, String(cut[0].length));

  const sp2 = F('ab cdefgh', segs(2, 20), 1);
  check('a continuation does not start with the space it broke on', sp2[1] === 'cdefgh',
        JSON.stringify(sp2));
  check('a segment too small for one character takes nothing',
        F('xyz', segs(0, 10), 1)[0] === '', JSON.stringify(F('xyz', segs(0, 10), 1)));
  check('an empty name produces empty segments',
        JSON.stringify(F('', segs(10, 10), 2)) === '["",""]',
        JSON.stringify(F('', segs(10, 10), 2)));
  check('a null name does not crash', JSON.stringify(F(null, segs(10), 2)) === '[""]',
        JSON.stringify(F(null, segs(10), 2)));
}
check('the line budget is what is measured, not just the box',
      /Math\.min\(box\.clientHeight, Math\.round\(lines \* lineH\)\)/.test(html),
      'line budget not applied');
check('a label that is too wide counts as not fitting, not just too tall',
      /scrollWidth\s*<=\s*box\.clientWidth/.test(html), 'width is not checked');
check('the close button stays out of the way until you hover',
      /\.bar \.x\{[^}]*display:none/.test(html) && /\.bar:hover \.x\{[^}]*display:flex/.test(html),
      'the x still takes width at rest');
check('the label wraps instead of running off on one line',
      /\.bar \.lbl\{[^}]*white-space:normal/.test(html), 'still nowrap');
check('continuation segments no longer show an ellipsis stub',
      !/opacity:\.7">…</.test(html), 'stub still there');

console.log('\n--- quarter days ---');
setD(mkD(['', '', '', '', '']));
let q = layout(task(0, 0.25));
check('a quarter-day task is a quarter of a cell wide',
      q.spans.length === 1 && q.spans[0].from === 0 && q.spans[0].to === 0.25,
      JSON.stringify(q.spans));

q = layout(task(1.5, 0.5));
check('it can start half way through a day',
      q.spans[0].from === 1.5 && q.spans[0].to === 2, JSON.stringify(q.spans));

q = layout(task(0.75, 0.5));
check('and run over midnight into the next day',
      q.spans.length === 1 && q.spans[0].from === 0.75 && q.spans[0].to === 1.25,
      JSON.stringify(q.spans));

setD(mkD(['', 'WE', '']));
q = layout(task(0.5, 0.75));
check('a part day still steps over a day off',
      q.spans.length === 2 && q.spans[0].to === 1 && q.spans[1].from === 2
      && q.spans[1].to === 2.25, JSON.stringify(q.spans));

setD(mkD(['', '', '']));
check('a length is snapped to the nearest quarter',
      layout(task(0, 0.3)).spans[0].to === 0.25,
      JSON.stringify(layout(task(0, 0.3)).spans));
check('and so is an offset into the day',
      layout({who:'p1', date: globalThis.D.days[0].date, off: 0.4, days: 0.5})
        .spans[0].from === 0.5,
      JSON.stringify(layout({who:'p1', date: globalThis.D.days[0].date, off: 0.4,
                             days: 0.5}).spans));
check('a zero-length task still draws something to grab',
      layout(task(0, 0)).spans[0].to === 0.25,
      JSON.stringify(layout(task(0, 0)).spans));

// Resizing asks how long the bar is between two points, and must not count days off.
setD(mkD(['', 'WE', '', '']));
check('a resize over a day off does not inflate the estimate',
      workLen({statuses: ['', 'WE', '', '']}, 0, 3) === 2,
      String(workLen({statuses: ['', 'WE', '', '']}, 0, 3)));
check('and measures part days', workLen({statuses: ['', '', '']}, 0.5, 1.75) === 1.25,
      String(workLen({statuses: ['', '', '']}, 0.5, 1.75)));
check('a backwards measure is nothing', workLen({statuses: ['', '']}, 2, 1) === 0,
      String(workLen({statuses: ['', '']}, 2, 1)));

check('quarters are the only step the board stores',
      snapQ(0.4) === 0.5 && snapQ(0.1) === 0 && snapQ(1.3) === 1.25,
      [snapQ(0.4), snapQ(0.1), snapQ(1.3)].join(','));
check('a quarter day is shown as a quarter, not rounded to 0.3',
      /const nq = /.test(html) && !/\$\{n1\(pd\)\}d/.test(html), 'still using n1 for load');
check('the pointer reports where in the day it is',
      /within:\s*snapQ\(within\)/.test(html), 'cellAt has no sub-day position');
// Measuring a dead zone against an already-rounded value means a two-pixel wobble reads
// as a whole quarter. That is the shiver, and it survived a comment warning about it.
check('and reports it unrounded as well, for the drag to measure against',
      /raw: i \+ within/.test(html), 'the drag only sees a snapped pointer');
check('the drag measures from the raw pointer', /c\.raw - o\.grab/.test(html),
      'the dead zone is measured against a rounded value');

console.log('\n--- the pointer is found by geometry, not by what it lands on ---');
const cellSrc = html.match(/function cellIndexAt\([\s\S]*?\n\}/);
check('the cell rule is extractable', !!cellSrc, 'cellIndexAt not found');
if (cellSrc) {
  const CI = new Function(`${cellSrc[0]} return cellIndexAt;`)();
  // The bug: a bar is positioned over every cell it spans, so once it grew under the
  // pointer the hit test reported the cell the bar STARTS in and the offset inside it
  // clamped at the far edge. Dragging right then read as the length collapsing, and the
  // tile jumped backwards while the hand went forwards.
  check('a pixel in the first cell is the first cell', CI(10, 0, 65, 8) === 0, String(CI(10, 0, 65, 8)));
  check('and it keeps counting across the row', CI(200, 0, 65, 8) === 3, String(CI(200, 0, 65, 8)));
  check('three cells out does not saturate at one', CI(210, 10, 65, 8) === 3, String(CI(210, 10, 65, 8)));
  check('past the end of the row stops at the last',
        CI(9999, 0, 65, 8) === 7, String(CI(9999, 0, 65, 8)));
  check('before the row stops at the first', CI(-50, 0, 65, 8) === 0, String(CI(-50, 0, 65, 8)));
  check('a row with no width answers nothing', CI(10, 0, 0, 8) === -1, String(CI(10, 0, 0, 8)));
  check('a row with no cells answers nothing', CI(10, 0, 65, 0) === -1, String(CI(10, 0, 65, 0)));
}
check('the hit test is used for the row only',
      /closest\('#boardP tr\[data-w\]'\)/.test(html), 'still asking the hit element for its cell');

console.log('\n--- planning in effort rather than in calendar days ---');
if (effSrc && /function setEffort/.test(effSrc)) {
  const E = new Function(`${QUARTER}
    let TASKS = [{id:'t1', who:'p1', days:1, sp:0}, {id:'t2', who:'p1', days:2, sp:0}];
    let dirty = false; const markDirty = () => { dirty = true; };
    ${effSrc}
    return {effOf, effortOf, spanOf, setEffort, get tasks(){ return TASKS; },
            get dirty(){ return dirty; }};`)();
  const slow = {canonical: 'p1', eff: 0.5};

  // The ask, exactly: two points for someone at 0.5 is four days on the board and still
  // two against their capacity.
  check('two points at half efficiency is four days long', E.spanOf(2, slow) === 4,
        String(E.spanOf(2, slow)));
  const t = E.setEffort('t1', '2', [slow]);
  check('typing the effort sets the length', t && t.days === 4, t && String(t.days));
  check('and the effort is what it costs', E.effortOf(t, slow) === 2, String(E.effortOf(t, slow)));
  check('the board is marked unsaved', E.dirty === true, String(E.dirty));

  check('a quicker person needs fewer days for the same work',
        E.spanOf(2, {eff: 1}) === 2, String(E.spanOf(2, {eff: 1})));
  check('no efficiency on file means a day is a day',
        E.spanOf(2, {}) === 2, String(E.spanOf(2, {})));
  // Called ONCE each: a second identical call is a no-op by design and returns null,
  // which is how the first version of this check managed to crash itself.
  const snapped = E.setEffort('t2', '1.3', [slow]);
  check('effort snaps to a quarter', snapped && snapped.sp === 1.25, snapped && String(snapped.sp));
  const floored = E.setEffort('t2', '0', [slow]);
  check('and is never zero', floored && floored.sp === 0.25, floored && String(floored.sp));
  check('setting the same effort again changes nothing',
        E.setEffort('t2', '0.25', [slow]) === null, 'reported a change');
  // Asked of t1, which is sitting at 2. Asking t2 proved nothing: it was already at the
  // value junk coerces to, so the check passed whether the guard was there or not.
  check('junk is refused rather than zeroing the estimate',
        E.setEffort('t1', 'soon', [slow]) === null && E.tasks[0].sp === 2,
        'accepted junk: sp now ' + E.tasks[0].sp);
  check('an unknown id changes nothing', E.setEffort('nope', '2', [slow]) === null, 'changed');

  // A tile planned before this existed carries no effort. Its size must not change, and
  // the number it contributes must stop being a calendar day pretending to be work.
  const old = {id: 'o', who: 'p1', days: 4, sp: 0};
  check('an older tile is read as a span, not as effort',
        E.effortOf(old, slow) === 2, String(E.effortOf(old, slow)));
}
check('the mode is a toggle, not a setting buried somewhere',
      /id="effBtn"/.test(html), 'no toggle');
check('it travels with the plan, so two browsers cannot disagree',
      /cats: CATS, effort: EFFORT/.test(html) && /EFFORT = !!\(\(D\.plan\|\|\{\}\)\.effort\)/.test(html),
      'the mode is local to one browser');
check('planned counts the effort, not the days it spreads over',
      /EFFORT \? effortOf\(t, p\) : \(\+t\.days\|\|0\)/.test(html), 'planned still counts days');
check('and the number is on the tile, where it can be typed',
      /class="eff" data-e=/.test(html) && /function openEffort\(bar, chip\)/.test(html),
      'nowhere to type an effort');
check('typing an effort refreshes the row, not just the tiles',
      /if\(changed\)\{ renderCards\(\); renderBoard\(\); \}/.test(html),
      'the row and the card would disagree');
check('dragging the edge keeps the effort in step',
      /if\(EFFORT\) t2\.sp = snapQ\(nd \* effOf\(p\)\);/.test(html), 'drag leaves a stale effort');

console.log('\n--- capacity is what a person delivers, not days on a calendar ---');
const capSrc = html.match(/const capOf = [\s\S]*?\n\};/);
check('the capacity rule is extractable', !!capSrc, 'capOf not found');
if (capSrc) {
  const C = new Function(`${capSrc[0]} return capOf;`)();
  // A senior at 0.55 has seven working days and about four days of delivery in them: the
  // rest goes on review and design. Planning seven into that overcommits the sprint while
  // every number on screen still reads fine.
  check('a senior day is not a day of delivery', C({net: 7, eff: 0.55, sp: 3.85}) === 3.85,
        String(C({net: 7, eff: 0.55, sp: 3.85})));
  check('and a junior day is most of one', C({net: 6, eff: 0.85, sp: 5.1}) === 5.1,
        String(C({net: 6, eff: 0.85, sp: 5.1})));
  check('it is worked out when the server did not send it',
        C({net: 7, eff: 0.55}) === 3.85, String(C({net: 7, eff: 0.55})));
  check('no efficiency means take the days at face value',
        C({net: 7}) === 7, String(C({net: 7})));
  check('a zero-capacity person is zero, not their calendar',
        C({net: 0, eff: 0.85, sp: 0}) === 0, String(C({net: 0, eff: 0.85, sp: 0})));
  check('nothing at all does not become NaN', C({}) === 0, String(C({})));
}
check('a row is flagged against capacity, not against calendar days',
      /pd>cap\+EPS\?'over'/.test(html), 'still comparing with raw days');
check('and so is the utilisation figure',
      /Math\.round\(planned\/cap\*100\)/.test(html), 'utilisation still divides by the calendar');
// table-layout is fixed, so a heading wider than its column does not widen it - it runs
// into the one beside it. "Capacity" in 12px mono is wider than the column it names.
check('the headings are not set in the same width as the numbers',
      /\.g thead th\.net\{font:600 11px[^}]*Hanken/.test(html),
      'a heading can outgrow its column');
check('and the trailing columns are wide enough to hold them',
      /\.g \.net\{width:62px;min-width:56px/.test(html), 'columns still 52px');
check('the capacity is shown, not just used',
      /<th class="net cap"/.test(html), 'nothing on screen explains the number');

console.log('\n--- moving several tiles at once ---');
const selSrc = ['function slots\\([\\s\\S]*?\\n\\}', 'function slotIndex\\([\\s\\S]*?\\n\\}',
                'function shiftBy\\([\\s\\S]*?\\n\\}']
  .map(re => (html.match(new RegExp(re)) || [''])[0]);
check('the move rules are extractable', selSrc.every(Boolean), 'not found');
if (selSrc.every(Boolean)) {
  const S = new Function(`${QUARTER} ${off[0]} ${posSrc}
    ${selSrc.join('\n')}
    return {slots, slotIndex, shiftBy};`)();

  const P = statuses => ({canonical: 'p1', statuses});
  const DAYS = n => ({days: Array.from({length: n}, (_, i) =>
    ({date: '2026-11-' + String(i + 1).padStart(2, '0')}))});
  const T = (date, off) => ({who: 'p1', date, off: off || 0, days: 1});

  // A week with the weekend in the middle, which is the whole point: "one day right"
  // means the owner's next working day, not the next square of calendar.
  setD(Object.assign(DAYS(7), {people: [P(['', '', '', 'WE', 'WE', '', ''])]}));
  const p = P(['', '', '', 'WE', 'WE', '', '']);
  check('only working quarters are steppable', S.slots(p, 7).length === 5 * 4,
        String(S.slots(p, 7).length));
  check('the weekend is not among them',
        !S.slots(p, 7).some(x => Math.floor(x) === 3 || Math.floor(x) === 4),
        JSON.stringify(S.slots(p, 7)));

  // A whole selection moves by the least room any one of them has, or the spacing that
  // makes the plan readable collapses against the edge.
  const mid = [T('2026-11-02'), T('2026-11-03')];
  check('a clear run moves the full step', S.shiftBy(mid, 4, [p], 7) === 4,
        String(S.shiftBy(mid, 4, [p], 7)));
  const atEnd = [T('2026-11-03'), T('2026-11-07', 0.75)];
  check('the one with least room decides for all', S.shiftBy(atEnd, 4, [p], 7) === 0,
        String(S.shiftBy(atEnd, 4, [p], 7)));
  const atStart = [T('2026-11-01'), T('2026-11-02')];
  check('and the same going left', S.shiftBy(atStart, -4, [p], 7) === 0,
        String(S.shiftBy(atStart, -4, [p], 7)));
  // A quarter past the 7th, with two quarters of that day left: asking for a whole day
  // right gives the two that exist rather than refusing or running off the end.
  check('a partial step is taken when there is some room',
        S.shiftBy([T('2026-11-07', 0.25)], 4, [p], 7) === 2,
        String(S.shiftBy([T('2026-11-07', 0.25)], 4, [p], 7)));
  check('nothing selected moves nothing', S.shiftBy([], 4, [p], 7) === 0, 'moved');
  check('a zero step moves nothing', S.shiftBy(mid, 0, [p], 7) === 0, 'moved');
  check('an unknown owner moves nothing',
        S.shiftBy([{who: 'ghost', date: '2026-11-02', off: 0}], 4, [p], 7) === 0, 'moved');

  // Stepping over the weekend: two days right from Wednesday lands on Monday.
  const s7 = S.slots(p, 7);
  const from = S.slotIndex(s7, 2);           // day index 2 = the 3rd, a working day
  check('one working day right steps over the weekend',
        Math.floor(s7[from + 4]) === 5, String(s7[from + 4]));
  check('a quarter step stays inside the day',
        Math.abs(s7[from + 1] - 2.25) < 1e-9, String(s7[from + 1]));
}
check('selected tiles are drawn as a set', /\.bar\.sel\{/.test(html), 'no selected style');
check('arrows move the selection',
      /e\.key === 'ArrowLeft' \|\| e\.key === 'ArrowRight'/.test(html)
      || /ArrowRight/.test(html), 'no arrow handling');
check('a plain arrow is a day and shift is a quarter',
      /e\.shiftKey \? 1 : 4/.test(html), 'wrong step sizes');
check('typing in a box is never hijacked by the arrows',
      /el\.tagName === 'INPUT'/.test(html), 'arrows would fight the editors');
check('selection repaints in place, not by redrawing the board',
      /function paintSel\(\)/.test(html), 'no in-place paint');

console.log('\n--- a tile latches instead of shivering ---');
const latchSrc = ['function latch\\([\\s\\S]*?\\n\\}', 'function slotPos\\([\\s\\S]*?\\n\\}',
                  'const LATCH = [^\\n]*']
  .map(re => (html.match(new RegExp(re)) || [''])[0]);
check('the latch rule is extractable', latchSrc.every(Boolean), 'not found');
if (latchSrc.every(Boolean)) {
  const L = new Function(`${QUARTER} ${latchSrc.join('\n')}
    return {latch, slotPos, LATCH};`)();

  // The shiver: a hand resting on a boundary wobbles by a few pixels and the tile used to
  // flip between two cells every frame.
  const jitter = [4.48, 4.52, 4.47, 4.55, 4.49, 4.53];
  let at = 4, moves = 0;
  jitter.forEach(w => { const n = Math.round(L.latch(at, w)); if (n !== at) moves++; at = n; });
  check('a hand wobbling on a boundary moves it not at all', moves === 0, String(moves));

  check('it holds until the pointer is clearly past the notch',
        Math.round(L.latch(4, 4.5)) === 4, String(L.latch(4, 4.5)));
  check('and then it goes', Math.round(L.latch(4, 4.7)) === 5, String(L.latch(4, 4.7)));
  check('the same going back', Math.round(L.latch(4, 3.3)) === 3, String(L.latch(4, 3.3)));
  check('a long drag still tracks the pointer',
        Math.round(L.latch(4, 9.1)) === 9, String(L.latch(4, 9.1)));
  check('once latched it does not creep back',
        Math.round(L.latch(5, 4.6)) === 5, String(L.latch(5, 4.6)));

  // Where the pointer is, measured BEFORE rounding, which is what the dead zone needs.
  const s4 = [0, 0.25, 0.5, 0.75, 2, 2.25];
  check('the pointer position is read as a fraction',
        Math.abs(L.slotPos(s4, 0.375) - 1.5) < 1e-9, String(L.slotPos(s4, 0.375)));
  check('before the first slot reads as the first',
        L.slotPos(s4, -3) === 0, String(L.slotPos(s4, -3)));
  check('past the last reads as the last',
        L.slotPos(s4, 99) === s4.length - 1, String(L.slotPos(s4, 99)));
  check('an empty row does not divide by zero', L.slotPos([], 5) === 0, String(L.slotPos([], 5)));
}
// Resizing is the same latch, measured in quarter-days of length rather than position.
check('a resize reads the length straight from the pointer',
      /workLen\(p, o\.start, end\) \/ STEP/.test(html), 'still biased by a quarter');
check('and never shrinks a bar to nothing',
      /Math\.max\(1, Math\.round\(latch\(t2\.days \/ STEP, want\)\)\)/.test(html),
      'a zero-length bar cannot be grabbed to fix');
check('a resize latches like a move does',
      /latch\(t2\.days \/ STEP, want\)/.test(html), 'the resize has no dead zone');
check('the drag acts once a frame, not once a pixel',
      /setTimeout\(apply, FRAME_MS\)/.test(html), 'still repainting per event');
// rAF does not fire in a background tab, nor on a machine with no display link - the
// drag would freeze rather than lag. Found by the headless browser refusing to animate.
check('and does not depend on the screen refreshing to work at all',
      !/requestAnimationFrame\(apply\)/.test(html), 'the drag can freeze');
check('a repaint keeps the tile looking held',
      /DRAGGING === t\.id \? ' drag'/.test(html), 'the dragged tile loses its state');
check('and lets go of it on release', /DRAGGING = '';/.test(html), 'it stays stuck');
check('the latch is animated, briefly', /\.bar\{transition:left \.0\d+s/.test(html),
      'no latch animation');
check('a drag redraws only the tile that moved',
      /liveTask\(t2\); markDirty\(\);/.test(html), 'still rebuilding the whole board');
// Recreating the element re-splits its label, and that measurement forces a layout. A
// dozen of those per frame while the hand moves IS the flicker.
check('and moves it by style, without rebuilding it at all',
      /function nudgeTask\(t\)/.test(html) && /el\.style\.width = /.test(html),
      'every step still recreates the tile');
check('falling back to a redraw only when the shape changed',
      /if\(!nudgeTask\(t\)\) drawTask\(t\);/.test(html), 'no fallback');
check('and reading every measurement before writing any style',
      /Every read is done before any write/.test(html), 'layout thrash per bar');
check('and a held tile does not animate',
      /\.bar\.drag\{transition:none;\}/.test(html), 'the width chases the hand');
check('and not at all for people who asked for less motion',
      /prefers-reduced-motion: reduce\)\{ \.bar\{transition:none/.test(html), 'ignores the setting');

console.log('\n--- the backlog: a heading, then everything under it ---');
const grab2 = n => (html.match(new RegExp('(?:function|const) ' + n + '[\\s\\S]*?\\n\\}')) || [])[0];
const parts2 = ['addCategory', 'removeCategory', 'recategorise', 'setDays', 'addNote',
                'byCategory', 'shelveTask', 'placeTask'].map(grab2);
const onBoardSrc = html.match(/const onBoard = [^\n]*\n/);
const inWinSrc = html.match(/const inWindow = [^\n]*\n/);
const daysOfSrc = html.match(/function daysOf\(c\)\{[\s\S]*?\n\}/);
const backSrc = html.match(/const backlog = [^\n]*\n/);
check('the backlog rules are extractable',
      parts2.every(Boolean) && !!onBoardSrc && !!inWinSrc && !!daysOfSrc && !!backSrc,
      'not found');
if (parts2.every(Boolean) && onBoardSrc && inWinSrc && daysOfSrc && backSrc) {
  const B = new Function(`
    ${QUARTER}
    ${posSrc}
    const CAT_NONE = 'Unsorted';
    const BAR_TINTS = 6;
    const uid = () => 'n' + (TASKS.length + 1);
    const nextTint = () => 1;
    let CATS = [];
    let TASKS = [];
    let dirty = false;
    const markDirty = () => { dirty = true; };
    let EFFORT = false;
    const D = {days: [0,1,2,3,4,5,6,7,8,9].map(i =>
      ({date: '2026-11-' + String(i + 1).padStart(2, '0')})), people: []};
    ${effSrc}
    ${onBoardSrc[0]} ${inWinSrc[0]} ${daysOfSrc[0]} ${backSrc[0]} ${parts2.join('\n')}
    return {addCategory, removeCategory, recategorise, setDays, addNote, byCategory, shelveTask, placeTask,
            backlog, onBoard, inWindow, daysOf, get tasks(){ return TASKS; }, get cats(){ return CATS; },
            get dirty(){ return dirty; }};`)();

  // A heading is made once, before there is anything to put under it.
  check('a heading can be made on its own', B.addCategory('TBM') === 'TBM', String(B.addCategory));
  check('it exists with nothing in it',
        B.byCategory(B.backlog(), B.cats).length === 1
        && B.byCategory(B.backlog(), B.cats)[0].tasks.length === 0,
        JSON.stringify(B.byCategory(B.backlog(), B.cats)));
  check('and the board is marked unsaved', B.dirty === true, String(B.dirty));
  check('the same heading twice is still one heading', B.addCategory('tbm') === null,
        JSON.stringify(B.cats));
  check('an empty name is not a heading', B.addCategory('   ') === null, JSON.stringify(B.cats));

  // Then everything goes under it without naming it again.
  const t1 = B.addNote('Rollback strategy finalisation', 'TBM');
  const t2 = B.addNote('Integration tests PR', 'TBM');
  check('tasks are added to a heading by name', !!t1 && t1.cat === 'TBM', t1 && t1.cat);
  check('more than one, without repeating the heading', B.backlog().length === 2,
        String(B.backlog().length));
  check('still one heading', B.cats.length === 1, JSON.stringify(B.cats));
  check('a task under a brand new heading creates it',
        B.addNote('branch changes PR', 'CASA').cat === 'CASA' && B.cats.length === 2,
        JSON.stringify(B.cats));
  check('a task with no heading lands somewhere findable',
        B.addNote('stray', '').cat === 'Unsorted', JSON.stringify(B.cats));
  check('an empty line is not a task', B.addNote('   ', 'TBM') === null, 'made one');

  check('nothing in the backlog is on the board', B.onBoard().length === 0, String(B.onBoard().length));
  check('and none of it costs anyone a day', B.daysOf('p1') === 0, String(B.daysOf('p1')));

  // An empty heading can be removed; one with work under it cannot.
  check('an empty heading can be removed',
        B.addCategory('Spare') === 'Spare' && B.removeCategory('Spare') === true,
        JSON.stringify(B.cats));
  check('a heading holding work is not removed by a click',
        B.removeCategory('TBM') === false && B.cats.includes('TBM'), JSON.stringify(B.cats));
  check('an unknown heading removes nothing', B.removeCategory('nope') === false, 'removed one');

  // Schedule it, then take it back out.
  const sched = B.placeTask(t1.id, 'p1', 2.5);
  check('a task can be scheduled', !!sched && sched.who === 'p1', sched && sched.who);
  check('at a quarter-day position', sched.date === '2026-11-03' && sched.off === 0.5,
        sched.date + '+' + sched.off);
  check('it leaves the backlog', B.backlog().length === 3, String(B.backlog().length));
  check('and starts costing days', B.daysOf('p1') === 1, String(B.daysOf('p1')));
  // Work scheduled on days this window does not cover still exists, but it must not be
  // counted against the capacity shown beside the window it is NOT in.
  const away = B.addNote('planned elsewhere', 'TBM');
  away.who = 'p1';
  away.date = '2027-03-01';
  check('off-window work is still on the board', B.onBoard().some(t=>t.id===away.id),
        'it vanished');
  check('but it is not in this window', !B.inWindow().some(t=>t.id===away.id), 'it is');
  check('and it does not count against capacity shown here', B.daysOf('p1') === 1,
        String(B.daysOf('p1')));
  check('nor does it show in the backlog', !B.backlog().some(t=>t.id===away.id),
        'it is listed as an idea');
  away.who = ''; away.date = '';

  const shelved = B.shelveTask(t1.id);
  check('and can be taken back out', !!shelved && shelved.who === '', shelved && shelved.who);
  check('it stops costing days', B.daysOf('p1') === 0, String(B.daysOf('p1')));
  check('returning under its own heading',
        B.byCategory(B.backlog(), B.cats).find(g=>g.cat==='TBM').tasks.some(t=>t.id===t1.id),
        'lost its heading');
  check('shelving something already in the backlog does nothing',
        B.shelveTask(t1.id) === null, 'shelved twice');

  // Dragging a row onto another heading files it there. Before this, the browser treated
  // that heading's add box as a text field and pasted the drag payload into it.
  const moved = B.recategorise(t1.id, 'CASA');
  check('a task can be moved to another heading', !!moved && moved.cat === 'CASA',
        moved && moved.cat);
  check('it leaves the one it came from',
        !B.byCategory(B.backlog(), B.cats).find(g=>g.cat==='TBM').tasks.some(t=>t.id===t1.id),
        'still under TBM');
  check('and arrives in the new one',
        B.byCategory(B.backlog(), B.cats).find(g=>g.cat==='CASA').tasks.some(t=>t.id===t1.id),
        'did not arrive');
  check('moving to a heading that does not exist yet creates it',
        B.recategorise(t2.id, 'Brand new').cat === 'Brand new' && B.cats.includes('Brand new'),
        JSON.stringify(B.cats));
  check('moving it to where it already is does nothing',
        B.recategorise(t1.id, 'CASA') === null, 'reported a move');
  check('an empty heading name moves nothing', B.recategorise(t1.id, '  ') === null, 'moved');
  check('an unknown id moves nothing', B.recategorise('nope', 'CASA') === null, 'moved');

  // A SCHEDULED task dropped on a heading comes off the board: filing it away and leaving
  // it in the sprint would be neither one thing nor the other.
  B.placeTask(t1.id, 'p1', 0);
  check('it was scheduled again', B.daysOf('p1') === 1, String(B.daysOf('p1')));
  const filed = B.recategorise(t1.id, 'TBM');
  check('dropping a scheduled task on a heading takes it off the board',
        !!filed && filed.who === '', filed && filed.who);
  check('and it stops costing days', B.daysOf('p1') === 0, String(B.daysOf('p1')));

  // Effort, typed. A backlog row has no bar to drag yet, so the number has to be editable.
  check('effort can be typed', !!B.setDays(t1.id, '2.5') && t1.days === 2.5, String(t1.days));
  check('it snaps to a quarter', !!B.setDays(t1.id, '1.3') && t1.days === 1.25, String(t1.days));
  check('a "d" suffix is tolerated', !!B.setDays(t1.id, '3d') && t1.days === 3, String(t1.days));
  check('nothing shorter than a quarter day',
        !!B.setDays(t1.id, '0') && t1.days === 0.25, String(t1.days));
  check('and nothing longer than the longest bar',
        !!B.setDays(t1.id, '900') && t1.days === 60, String(t1.days));
  check('junk is refused rather than zeroed',
        B.setDays(t1.id, 'soon') === null && t1.days === 60, String(t1.days));
  check('the same number again is not a change', B.setDays(t1.id, '60') === null, 'reported one');
  check('an unknown id changes nothing', B.setDays('nope', '2') === null, 'changed something');
  B.setDays(t1.id, '1');
  check('effort counts once it is scheduled',
        !!B.placeTask(t1.id, 'p1', 0) && B.daysOf('p1') === 1, String(B.daysOf('p1')));
  B.setDays(t1.id, '2');
  check('and re-estimating a scheduled task moves the total', B.daysOf('p1') === 2,
        String(B.daysOf('p1')));
  B.shelveTask(t1.id);
}
check('bars are painted from the board only, not the backlog',
      /onBoard\(\)\.forEach\(drawTask\)/.test(html), 'paintBars still walks every task');
check('there is one list, not a separate parking area',
      /id="backP"/.test(html) && !/id="parkP"/.test(html), 'parking area still there');
check('each heading has its own add box, so the category is typed once',
      /class="addrow" data-addto=/.test(html), 'no per-heading input');
check('tasks stack vertically rather than wrapping as tiles',
      /\.rows\{[^}]*flex-direction:column/.test(html), 'still a tile wall');
check('a heading accepts a drop instead of pasting into its add box',
      /box\.addEventListener\('drop'/.test(html) && /class="addrow"/.test(html),
      'no drop handler on the heading');
check('the heading being dropped on is highlighted', /\.cat\.into\{/.test(html),
      'no target feedback');
// Stack ranking. The page is the only place these can be checked: jsdom has no layout,
// so neither the line's position nor the flicker it would show has any meaning there.
check('a backlog row can be ranked above or below another',
      /\.row\.note\.dropb\{/.test(html) && /\.row\.note\.dropa\{/.test(html),
      'no insertion line, so above-from-below is guesswork');
check('which half of the row decides above or below',
      /\(e\.clientY - r\.top\) > r\.height \/ 2/.test(html), 'no half-row test');
// dragleave fires when the pointer crosses onto a child of the row, so clearing the line
// unconditionally makes it blink while you hold still over the row.
check('the insertion line survives the pointer crossing the row\'s own children',
      /dragleave'[\s\S]{0,220}c\.contains\(e\.relatedTarget\)/.test(html),
      'the line will flicker');
// A tray chip, a backlog row and a whole list all travel as text/plain, and dataTransfer
// cannot be READ during dragover, so the hover has to remember what was lifted.
check('the hover knows a lifted row from a lifted list from a lifted tray chip',
      /let LIFTED = null/.test(html)
      && /LIFTED\.kind !== 'row'/.test(html) && /LIFTED\.kind !== 'cat'/.test(html),
      'one kind of drag would be offered another kind of target');
// ONE variable, set by EVERY dragstart. Two flags let a drag that never fired dragend
// leave a stale one behind, and a half-finished list-move then swallowed the next task
// dropped on a heading — silently, because the heading claimed it and found the wrong
// payload. A test caught exactly that, so the shape is worth pinning down.
check('every kind of drag says what it is, rather than only clearing itself at the end',
      (html.match(/LIFTED = \{kind:/g) || []).length >= 3,
      'a cancelled drag would leave the last kind lingering');
check('no second flag to go stale alongside it',
      !/\bLIFTCAT\b/.test(html), 'there is a second drag flag again');
check('ranking moves the task within the list rather than storing a rank number',
      /function reorder\(id, targetId, after\)/.test(html) && !/\brank:/.test(html),
      'a second source of truth for position');
check('a row\'s name and effort can be edited in place',
      /function openRowEditor\(row, field\)/.test(html)
      && /openRowEditor\(el\.closest\('\.row\.note'\)/.test(html), 'no row editor');
check('editing turns off the row\'s drag so text can be selected',
      /row\.draggable = false;/.test(html), 'drag stays on while editing');
check('switching sprint or window saves first',
      /async function switchTo\(change\)/.test(html)
      && /if\(DIRTY\) await doSave/.test(html), 'a switch can lose edits');
check('and waits for a save already running rather than skipping it',
      /&& SAVING; i\+\+\) await new Promise/.test(html), 'an in-flight save is skipped');
check('every sprint and window keeps its own plan',
      /switchTo\(\(\)=>\{ ST\.sprints/.test(html)
      && /switchTo\(\(\)=>\{ ST\.from/.test(html), 'a switch does not go through switchTo');
check('the backlog lights up while a task is dragged over it',
      /\.backlog\.hot\{/.test(html), 'no drop feedback');
check('a plan saved with the old parked flag still opens',
      /if\(n\.parked\)\{ n\.who = ''; \}/.test(html), 'no migration');

console.log('\n--- task colours ---');
const tint = html.match(/function tintFor\([\s\S]*?\n\}/);
check('the tint rule is extractable', !!tint, 'tintFor not found');
if (tint) {
  const T = new Function(`const BAR_TINTS = 6; ${tint[0]} return tintFor;`)();
  check('a tint is always in range',
        ['a','b','t12','', 'x9y', 'task-7'].every(id => T(id) >= 1 && T(id) <= 6), 'out of range');
  check('the same task keeps its colour', T('t42') === T('t42'), 'unstable');
  const ids = Array.from({length: 40}, (_, i) => 't' + Math.random().toString(36).slice(2, 9));
  const used = new Set(ids.map(T));
  check('a boardful of tasks uses more than one colour', used.size >= 4, String(used.size));
  // Neighbouring bars are the ones you actually have to tell apart.
  const seq = ['t1','t2','t3','t4','t5','t6','t7','t8'].map(T);
  check('consecutive tasks are not all the same', new Set(seq).size >= 3, seq.join(','));
}
check('every tint has a dark-mode value',
      [1,2,3,4,5,6].every(n => new RegExp(`--t${n}:#[0-9a-f]{6}`, 'g').test(html)
                            && (html.match(new RegExp(`--t${n}:`, 'g')) || []).length === 2),
      'a tint is missing from one theme');
check('bars are tinted, not solid accent', !/\.bar\{[^}]*background:var\(--accent\)/.test(html),
      'still solid');

console.log(fail.length ? `\n${fail.length} of ${ran} FAILED: ${fail.join(', ')}`
                        : `\nall ${ran} checks passed`);
process.exit(fail.length ? 1 : 0);
