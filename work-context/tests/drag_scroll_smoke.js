/**
 * Scrolling the page while a native drag is in flight.
 *
 * derived/drag-scroll.js is loaded and run for real: it is a plain IIFE that installs
 * itself on the document and puts one object on `window`, so a handful of DOM stubs are
 * all it needs. There is no layout here, so every box states its own rect and its own
 * scroll extents — which is the honest way to test this, because the whole question is
 * what the numbers say, not what a browser would have painted.
 *
 * The four things worth protecting:
 *   - an axis is not given up just because an inner scroller declined it, or a grid
 *     taller than the window would block the document from ever moving
 *   - a scroller already at its end must not swallow the tick either
 *   - the innermost scroller that IS at its edge and CAN move wins
 *   - the timer always stops, including on Escape, or a stuck auto-scroll outlives the drag
 */
'use strict';
const fs = require('fs');
const path = require('path');

const SRC = path.join(__dirname, '..', 'derived', 'drag-scroll.js');
const src = fs.readFileSync(SRC, 'utf8');

let ran = 0;
const fail = [];
function check(name, cond, detail) {
  ran++;
  if (cond) { console.log('  ok  ' + name); }
  else { fail.push(name); console.log('FAIL  ' + name + '  -> ' + (detail === undefined ? '' : detail)); }
}

// ---- the little of a browser this file touches -------------------------------
const listeners = {};
function on(ev, fn) { (listeners[ev] = listeners[ev] || []).push(fn); }
function fire(ev, props) {
  (listeners[ev] || []).forEach(fn => fn(Object.assign({ type: ev }, props || {})));
}

let HIT = null;                       // what elementFromPoint returns
const scrolledWindow = [];

globalThis.document = {
  addEventListener: on,
  elementFromPoint: () => HIT,
  body: { nodeType: 1, tag: 'body' },
  documentElement: { nodeType: 1, tag: 'html' },
};
globalThis.window = globalThis;
globalThis.addEventListener = on;
globalThis.innerWidth = 1200;
globalThis.innerHeight = 800;
globalThis.scrollBy = (x, y) => scrolledWindow.push([x, y]);
globalThis.getComputedStyle = (el) =>
  (el && el.style) || { overflowX: 'visible', overflowY: 'visible' };

(0, eval)(src);
const DS = globalThis.DragScroll;
check('the module publishes itself', !!DS, typeof DS);

// A fake box. `rect` is viewport coordinates; the scroll extents decide whether it has
// anywhere left to go, which is the half that is easy to get backwards.
function box(o) {
  const b = {
    nodeType: 1,
    parentElement: o.parent || null,
    style: o.style || { overflowX: 'visible', overflowY: 'visible' },
    scrollTop: o.scrollTop || 0, scrollLeft: o.scrollLeft || 0,
    scrollHeight: o.scrollHeight || 0, clientHeight: o.clientHeight || 0,
    scrollWidth: o.scrollWidth || 0, clientWidth: o.clientWidth || 0,
    getBoundingClientRect: () => o.rect,
  };
  return b;
}
const VERT = { overflowY: 'auto', overflowX: 'visible' };
const HORZ = { overflowY: 'visible', overflowX: 'auto' };

// ---- ramp ---------------------------------------------------------------------
check('no push outside the zone', DS.ramp(0, 20) === 0, DS.ramp(0, 20));
check('a negative depth is still no push', DS.ramp(-1, 20) === 0);
check('full speed at the edge', DS.ramp(1, 20) === 20);
check('past the edge clamps, it does not race', DS.ramp(4, 20) === 20, DS.ramp(4, 20));
check('halfway in is about half speed', DS.ramp(0.5, 20) === 10, DS.ramp(0.5, 20));
check('a sliver of depth still moves a pixel', DS.ramp(0.01, 20) === 1, DS.ramp(0.01, 20));

// ---- nudge --------------------------------------------------------------------
// A 1000px-tall box from y=0, zone 72.
check('the middle of a box is still', DS.nudge(500, 0, 1000, 72, 20) === 0);
check('near the top pulls back', DS.nudge(10, 0, 1000, 72, 20) < 0, DS.nudge(10, 0, 1000, 72, 20));
check('near the bottom pushes on', DS.nudge(990, 0, 1000, 72, 20) > 0);
check('at the very top is full speed', DS.nudge(0, 0, 1000, 72, 20) === -20);
check('at the very bottom is full speed', DS.nudge(1000, 0, 1000, 72, 20) === 20);
// The pointer leaving the box entirely is the common case: you overshoot on the way to
// the edge. It must stay at full speed rather than wrapping round to nothing.
check('above the box is full speed, not zero', DS.nudge(-300, 0, 1000, 72, 20) === -20,
      DS.nudge(-300, 0, 1000, 72, 20));
check('below the box is full speed, not zero', DS.nudge(1300, 0, 1000, 72, 20) === 20);
check('just inside the zone is gentle', DS.nudge(928 + 1, 0, 1000, 72, 20) === 1,
      DS.nudge(929, 0, 1000, 72, 20));
check('the zone boundary itself is still', DS.nudge(928, 0, 1000, 72, 20) === 0,
      DS.nudge(928, 0, 1000, 72, 20));
// A short box would be entirely zone, so every position would scroll it and nothing
// could ever be pointed at.
check('a box too short for two zones is left alone', DS.nudge(50, 0, 100, 72, 20) === 0);

// ---- room ------------------------------------------------------------------------
const atTop = box({ rect: {}, scrollTop: 0, clientHeight: 300, scrollHeight: 900 });
const midway = box({ rect: {}, scrollTop: 300, clientHeight: 300, scrollHeight: 900 });
const atEnd = box({ rect: {}, scrollTop: 600, clientHeight: 300, scrollHeight: 900 });
check('at the top there is nowhere up', DS.room(atTop, 'y', -1) === false);
check('at the top there is room down', DS.room(atTop, 'y', 1) === true);
check('midway there is room both ways', DS.room(midway, 'y', -1) && DS.room(midway, 'y', 1));
check('at the end there is nowhere down', DS.room(atEnd, 'y', 1) === false);
const wide = box({ rect: {}, scrollLeft: 0, clientWidth: 400, scrollWidth: 1600 });
check('a horizontal box has room right', DS.room(wide, 'x', 1) === true);
check('a horizontal box at 0 has none left', DS.room(wide, 'x', -1) === false);

// ---- overflows -------------------------------------------------------------------
check('visible overflow is not a scroller',
      DS.overflows(box({ rect: {}, clientHeight: 100, scrollHeight: 900 }), 'y') === false);
check('auto overflow with content to spare is a scroller',
      DS.overflows(box({ rect: {}, style: VERT, clientHeight: 100, scrollHeight: 900 }), 'y') === true);
// Marked auto but nothing overflowing: not a scroller, or it would eat every tick.
check('auto overflow with nothing to scroll is not a scroller',
      DS.overflows(box({ rect: {}, style: VERT, clientHeight: 900, scrollHeight: 900 }), 'y') === false);

// ---- step -------------------------------------------------------------------------
function reset() { scrolledWindow.length = 0; }

// The backlog panel, scrollable, pointer held near its lower edge.
reset();
const panel = box({
  rect: { top: 100, bottom: 500, left: 0, right: 300 }, style: VERT,
  scrollTop: 50, clientHeight: 400, scrollHeight: 1600,
});
HIT = box({ rect: { top: 480, bottom: 495, left: 0, right: 300 }, parent: panel });
let did = DS.step(150, 495);
check('the scroller under the pointer moves', panel.scrollTop > 50, panel.scrollTop);
check('and the window is left alone', scrolledWindow.length === 0, JSON.stringify(scrolledWindow));
check('step reports what it moved', did.y > 0, JSON.stringify(did));

// The same panel scrolled to its end: it must not swallow the tick.
reset();
const spent = box({
  rect: { top: 100, bottom: 500, left: 0, right: 300 }, style: VERT,
  scrollTop: 1200, clientHeight: 400, scrollHeight: 1600,
});
HIT = box({ rect: { top: 480, bottom: 495, left: 0, right: 300 }, parent: spent });
DS.step(150, 780);
check('a scroller at its end does not block the window', scrolledWindow.length === 1,
      JSON.stringify(scrolledWindow));
check('and it did not move itself', spent.scrollTop === 1200, spent.scrollTop);

// A grid taller than the window: the pointer sits in its MIDDLE while being at the
// bottom of the viewport, which is exactly when the document should move.
reset();
const tall = box({
  rect: { top: -600, bottom: 2400, left: 0, right: 1200 }, style: VERT,
  scrollTop: 0, clientHeight: 3000, scrollHeight: 3000,
});
HIT = box({ rect: { top: 760, bottom: 790, left: 0, right: 1200 }, parent: tall });
DS.step(600, 790);
check('a tall inner box does not strand the page', scrolledWindow.length === 1
      && scrolledWindow[0][1] > 0, JSON.stringify(scrolledWindow));

// The grid wrapper scrolls sideways while the window scrolls down, in one tick: a sprint
// is commonly off-screen in both directions at once.
reset();
const gwrap = box({
  rect: { top: 200, bottom: 1400, left: 0, right: 1200 }, style: HORZ,
  scrollLeft: 0, clientWidth: 1200, scrollWidth: 4000,
});
HIT = box({ rect: { top: 760, bottom: 790, left: 1100, right: 1200 }, parent: gwrap });
did = DS.step(1195, 790);
check('the wrapper scrolls sideways', gwrap.scrollLeft > 0, gwrap.scrollLeft);
check('and the window still scrolls down in the same tick',
      scrolledWindow.length === 1 && scrolledWindow[0][1] > 0, JSON.stringify(scrolledWindow));
check('the window is not also pushed sideways',
      scrolledWindow[0][0] === 0, JSON.stringify(scrolledWindow));

// Nothing under the pointer at all, mid-viewport: a tick must be a no-op, not a throw.
reset();
HIT = null;
did = DS.step(600, 400);
check('a quiet pointer scrolls nothing', did.x === 0 && did.y === 0 && !scrolledWindow.length);

// The walk must stop at body/documentElement rather than climbing out of the document.
reset();
HIT = box({ rect: { top: 760, bottom: 790, left: 0, right: 1200 },
            parent: globalThis.document.body });
check('the walk terminates at body', typeof DS.step(600, 790) === 'object');

// An overlay scrollbar sits on top of the page, so a pointer near the right edge of the
// window hit-tests to <html> and every scroller it is really inside is missed. A real
// browser probe caught this: the grid wrapper refused to scroll because elementFromPoint
// kept answering HTML. The drag's own target is the better answer, so step takes one.
reset();
const sideways = box({
  rect: { top: 200, bottom: 500, left: 0, right: 1200 }, style: HORZ,
  scrollLeft: 0, clientWidth: 1198, scrollWidth: 4000,
});
const inner = box({ rect: { top: 200, bottom: 500, left: 0, right: 4000 }, parent: sideways });
HIT = globalThis.document.documentElement;        // what the scrollbar makes it answer
DS.step(1190, 350);
check('hit-testing the scrollbar scrolls nothing', sideways.scrollLeft === 0, sideways.scrollLeft);
DS.step(1190, 350, inner);
check("the drag's own target is used when given", sideways.scrollLeft > 0, sideways.scrollLeft);

// An explicit target must not be second-guessed by hit-testing.
reset();
const other = box({
  rect: { top: 0, bottom: 800, left: 0, right: 300 }, style: VERT,
  scrollTop: 0, clientHeight: 800, scrollHeight: 4000,
});
HIT = box({ rect: { top: 0, bottom: 10, left: 0, right: 10 }, parent: null });
DS.step(150, 798, box({ rect: { top: 700, bottom: 790, left: 0, right: 300 }, parent: other }));
check('an explicit target wins over elementFromPoint', other.scrollTop > 0, other.scrollTop);

// ---- the timer ----------------------------------------------------------------------
check('nothing is running before a drag', DS.running() === false);
fire('dragstart');
check('a drag starts the ticker', DS.running() === true);
fire('dragover', { clientX: 10, clientY: 20 });
check('a drag still running after dragover', DS.running() === true);
fire('drop');
check('a drop stops it', DS.running() === false, DS.running());

// Esc cancels a native drag and the browser reports it to nobody, so the module has to
// watch for it itself or the ticker outlives the drag.
fire('dragstart');
fire('keydown', { key: 'Escape' });
check('Escape stops it', DS.running() === false);

fire('dragstart');
fire('keydown', { key: 'a' });
check('an ordinary key does not stop it', DS.running() === true);
fire('dragend');
check('dragend stops it', DS.running() === false);

// A drag that began outside the window never fires dragstart here.
fire('dragover', { clientX: 10, clientY: 20 });
check('a drag entering from outside still scrolls', DS.running() === true);
DS.stop();
check('stop is idempotent', (DS.stop(), DS.running()) === false);

console.log(fail.length ? `\n${fail.length} of ${ran} FAILED: ${fail.join(', ')}`
                        : `\nall ${ran} checks passed`);
process.exit(fail.length ? 1 : 0);
