// Scrolling the page while a drag is in flight.
//
// The backlog rows and the tray chips are dragged with native HTML5 drag-and-drop, and a
// native drag takes the wheel away: the browser owns the pointer for the duration, so no
// wheel event reaches the page and nothing scrolls. Pick a task off the backlog and the
// day you meant to drop it on is off-screen with no way to reach it, which leaves dropping
// it somewhere wrong and dragging it again as the only route.
//
// So the drag itself has to do the scrolling: hold near an edge and that edge moves. The
// edge is whichever scroller the pointer is actually inside — the horizontal grid wrapper
// when the sprint is wider than the window, the window otherwise — because a sprint is
// commonly off-screen in BOTH directions and only one of them is the document's.
//
// It installs itself on the document and needs no wiring. A page gets it by loading it.
(function () {
  'use strict';

  // How close to an edge counts as "at" it, and the fastest one tick may scroll. Both are
  // per tick at ~60/s: 20px is a brisk but followable glide, and 72px is wide enough to hit
  // on the way past without being so wide that the middle of a short list is always in it.
  var EDGE = 72;
  var MAX = 20;
  var TICK = 16;

  // A drag that never ends would leave the timer running forever. Chrome fires dragend
  // reliably, but a drag cancelled by the window losing focus has been known not to, and a
  // stuck auto-scroll is worse than no auto-scroll.
  var LIMIT = 60000;

  var timer = null, at = null, armed = 0;

  // Speed from how deep into the edge zone the pointer is: gentle on entry, full at the
  // edge itself. `t` is 0 at the inner boundary of the zone and 1 at the edge, and is
  // allowed past 1 when the pointer has left the box entirely, which clamps to full speed.
  function ramp(t, max) {
    if (!(t > 0)) return 0;
    return Math.ceil(Math.min(1, t) * max);
  }

  // How far to scroll one axis of one box this tick, signed: negative towards lo. `pos` is
  // the pointer, `lo`/`hi` the box's two edges on that axis, in the same coordinates.
  function nudge(pos, lo, hi, edge, max) {
    edge = edge || EDGE;
    max = max || MAX;
    if (hi - lo <= edge * 2) return 0;          // too short to have two zones; leave it be
    if (pos < lo + edge) return -ramp((lo + edge - pos) / edge, max);
    if (pos > hi - edge) return ramp((pos - (hi - edge)) / edge, max);
    return 0;
  }

  function overflows(el, ax) {
    var s;
    try { s = getComputedStyle(el); } catch (_) { return false; }
    var o = ax === 'y' ? s.overflowY : s.overflowX;
    if (!/^(auto|scroll|overlay)$/.test(o)) return false;
    return ax === 'y' ? el.scrollHeight > el.clientHeight + 1
                      : el.scrollWidth > el.clientWidth + 1;
  }

  // Whether the box has anywhere left to go in that direction. A scroller already at its
  // end must NOT swallow the tick, or a list scrolled to the bottom blocks the window
  // behind it from ever scrolling.
  function room(el, ax, dir) {
    if (ax === 'y') {
      return dir < 0 ? el.scrollTop > 0
                     : el.scrollTop + el.clientHeight < el.scrollHeight - 1;
    }
    return dir < 0 ? el.scrollLeft > 0
                   : el.scrollLeft + el.clientWidth < el.scrollWidth - 1;
  }

  // One tick. Walks out from the element under the pointer so the innermost scroller that
  // is both near its edge and able to move wins the axis, and the window gets whichever
  // axis nothing inside claimed.
  //
  // An axis is NOT given up just because a scroller declined it. A grid taller than the
  // window puts the pointer in its middle while sitting at the bottom of the viewport, and
  // that is precisely when the document is the thing that should move.
  //
  // `from` is the drag's own target when there is one, and it is a better answer than
  // hit-testing: an overlay scrollbar sits on top of the page, so a pointer near the right
  // edge of the window hit-tests to <html> and every scroller it is actually inside gets
  // missed. The drag never reports the scrollbar as its target.
  function step(x, y, from) {
    var el = from || null;
    if (!el) {
      try { el = document.elementFromPoint(x, y); } catch (_) { el = null; }
    }
    var did = { x: 0, y: 0 };
    var body = document.body, root = document.documentElement;

    while (el && el.nodeType === 1 && el !== body && el !== root) {
      if (!did.y || !did.x) {
        var r = el.getBoundingClientRect();
        if (!did.y && overflows(el, 'y')) {
          var dy = nudge(y, r.top, r.bottom, EDGE, MAX);
          if (dy && room(el, 'y', dy)) { el.scrollTop += dy; did.y = dy; }
        }
        if (!did.x && overflows(el, 'x')) {
          var dx = nudge(x, r.left, r.right, EDGE, MAX);
          if (dx && room(el, 'x', dx)) { el.scrollLeft += dx; did.x = dx; }
        }
      }
      el = el.parentElement;
    }

    var wy = did.y ? 0 : nudge(y, 0, window.innerHeight, EDGE, MAX);
    var wx = did.x ? 0 : nudge(x, 0, window.innerWidth, EDGE, MAX);
    if (wy || wx) {
      window.scrollBy(wx, wy);
      did.y = did.y || wy;
      did.x = did.x || wx;
    }
    return did;
  }

  function stop() {
    if (timer) { clearInterval(timer); timer = null; }
    at = null;
    armed = 0;
  }

  function start() {
    armed = Date.now();
    if (timer) return;
    timer = setInterval(function () {
      if (Date.now() - armed > LIMIT) { stop(); return; }
      if (at) step(at.x, at.y, at.el);
    }, TICK);
  }

  // dragover is the only position report a native drag gives, and it keeps arriving while
  // the pointer is held still, which is what makes "hold at the edge" work at all.
  function install() {
    document.addEventListener('dragstart', start, true);
    document.addEventListener('dragover', function (e) {
      if (!timer) start();                 // a drag begun outside the page still scrolls
      var el = e.target;
      at = { x: e.clientX, y: e.clientY, el: (el && el.nodeType === 1) ? el : null };
    }, true);
    ['dragend', 'drop', 'dragexit', 'pointerup', 'blur'].forEach(function (ev) {
      document.addEventListener(ev, stop, true);
    });
    window.addEventListener('blur', stop);
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') stop();      // Esc cancels the drag; the browser tells no one
    }, true);
  }

  window.DragScroll = {
    EDGE: EDGE, MAX: MAX,
    ramp: ramp, nudge: nudge, overflows: overflows, room: room,
    step: step, start: start, stop: stop, install: install,
    running: function () { return !!timer; }
  };

  install();
})();
