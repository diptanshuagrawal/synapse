// Marking leave the team never posted, on any capacity calendar in Synapse.
//
// Leave that was taken but never announced is invisible to the Slack ingest, so the person
// reads as fully available and every number measured against their capacity is wrong: the
// sprint looks affordable, the efficiency looks poor, and nothing on screen says why. The
// fix is to let whoever knows say so, wherever they happen to be looking.
//
// This file is the one implementation. It began inside monthly.html, which is why that
// page's behaviour is the behaviour: clicking a weekday cycles the override. Pages differ
// only in how a cell is reached, because the sprint board already spends a plain click on
// adding a task and cannot spend it twice.
(function () {
  'use strict';

  // leave -> WFH -> present -> clear, and round again.
  //
  // "present" is NOT the same as "clear". P asserts the person WAS working, which overrides
  // a leave the ingest reported; clearing just removes the override and lets the ingest win.
  // Both directions are needed, because a leave recorded in error has to stay gone after the
  // next rebuild rather than quietly coming back.
  var CYCLE = { '': 'L', 'L': 'W', 'W': 'P', 'P': '' };
  var WORD = {
    'L': 'on leave',
    'W': 'WFH',
    'P': 'present (overrides a reported leave)',
    '': 'override removed'
  };

  // Weekends and public holidays are nobody's leave to mark.
  var FIXED = { 'WE': 1, 'H': 1 };

  var CSS = [
    'td.lv-cell{cursor:pointer;}',
    'td.lv-cell:hover{outline:2px solid var(--fg,#333);outline-offset:-2px;}',
    // An override is a different KIND of fact from a reported absence, so it has to look
    // different: underlined means a person typed it, not that anyone posted it.
    'td.lv-man{text-decoration:underline;text-underline-offset:2px;text-decoration-thickness:2px;}',
    'td.lv-saving{opacity:.45;cursor:progress;}',
    '.lv-off td.lv-cell{cursor:inherit;}',
    '.lv-off td.lv-cell:hover{outline:none;}'
  ].join('');

  function style() {
    if (document.getElementById('lv-style')) return;
    var el = document.createElement('style');
    el.id = 'lv-style';
    el.textContent = CSS;
    (document.head || document.documentElement).appendChild(el);
  }

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  // The attributes a capacity cell needs to become markable. Returned as a string so a page
  // can keep building its grid with template literals, which is how all of them do it.
  //
  // `status` is the day's code from the capacity engine, `mark` the override already on it.
  // A fixed day returns nothing at all, so an inert cell stays inert.
  function attrs(canonical, date, status, mark, name) {
    if (FIXED[status] || !canonical || !date) return '';
    var m = mark || '';
    var who = name || canonical;
    var title = who + ' · ' + date + '\n'
      + (m ? 'marked here: ' + (WORD[m] || m) + '\n' : '')
      + 'click to cycle leave → WFH → present → clear';
    return ' data-lv-c="' + esc(canonical) + '" data-lv-d="' + esc(date)
      + '" data-lv-cur="' + esc(m) + '" title="' + esc(title) + '"';
  }

  // The class names that go with those attributes: one to say the cell is markable, one to
  // say it already carries an override.
  function cls(status, mark) {
    if (FIXED[status]) return '';
    return 'lv-cell' + (mark ? ' lv-man' : '');
  }

  function post(url, body) {
    return fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    }).then(function (r) { return r.json(); });
  }

  // `opts.by`       who is marking, recorded with the override
  // `opts.onChange` called after a successful write; the page reloads its own capacity,
  //                 because only the page knows what it cached and under which window
  // `opts.toast`    (msg, kind, undo) — optional; without one, nothing is announced
  // `opts.enabled`  () => boolean, for pages where marking is a mode rather than the default
  function wire(root, opts) {
    style();
    opts = opts || {};
    var scope = root || document;
    scope.querySelectorAll('td[data-lv-c]').forEach(function (td) {
      if (td.dataset.lvWired) return;
      td.dataset.lvWired = '1';
      td.addEventListener('click', function (ev) {
        if (opts.enabled && !opts.enabled()) return;      // not in marking mode: let it be
        ev.stopPropagation();                             // panels own their collapse clicks
        ev.preventDefault();
        if (td.classList.contains('lv-saving')) return;
        var canonical = td.dataset.lvC, date = td.dataset.lvD;
        if (!canonical || !date) {
          if (opts.toast) opts.toast('This row has no person id, so leave cannot be marked.', 'bad');
          return;
        }
        var prev = td.dataset.lvCur || '';
        var next = Object.prototype.hasOwnProperty.call(CYCLE, prev) ? CYCLE[prev] : 'L';
        td.classList.add('lv-saving');
        post('/api/leave', {
          canonical: canonical, dates: [date], code: next, by: opts.by || 'em'
        }).catch(function (e) {
          return { __error__: e.message };
        }).then(function (r) {
          td.classList.remove('lv-saving');
          if (!r || r.__error__) {
            if (opts.toast) opts.toast('Could not save: ' + ((r && r.__error__) || 'unknown'), 'bad');
            return;
          }
          // Shown BEFORE the reload: the reload rebuilds the grid and this cell stops
          // existing, so anything said about it has to be said first.
          if (opts.toast) {
            opts.toast(canonical + ' · ' + date + ' — ' + WORD[next], 'ok', {
              label: 'Undo',
              fn: function () {
                return post('/api/leave', {
                  canonical: canonical, dates: [date], code: prev, by: opts.by || 'em'
                }).then(function () { return opts.onChange && opts.onChange(); });
              }
            });
          }
          return opts.onChange && opts.onChange();
        });
      });
    });
  }

  window.LeaveEditor = {
    CYCLE: CYCLE, WORD: WORD, FIXED: FIXED,
    attrs: attrs, cls: cls, wire: wire, isFixed: function (s) { return !!FIXED[s]; }
  };
})();
