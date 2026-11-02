/**
 * Find on page, for every Synapse page at once.
 *
 * The two pieces worth testing without a browser are the ones that decide what a match IS:
 * flattening the page into one string (so a phrase spanning elements still matches, and
 * the markup's own indentation does not break it), and scanning that string. Both are
 * extracted from the shipped nav and run directly.
 */
'use strict';
const fs = require('fs');
const { JSDOM } = require('jsdom');

const navPath = process.argv[2];
if (!navPath) { console.error('CRASH no nav html given'); process.exit(1); }
const nav = fs.readFileSync(navPath, 'utf8');

let ran = 0; const fail = [];
const check = (name, ok, got) => {
  ran++; if (ok) console.log('  ok  ' + name);
  else { fail.push(name); console.log('FAIL  ' + name + '  -> ' + (got === undefined ? '' : got)); }
};
// A broken step() throws rather than returning a wrong answer, and a stack trace is a
// worse way to learn that than a named failure.
const checkNoThrow = (name, fn, want) => {
  let got;
  try { got = fn(); } catch (e) { check(name, false, 'threw: ' + e.message); return; }
  check(name, got === want, String(got));
};

const page = (body) => new JSDOM(
  `<!doctype html><html><body>${body}${nav}</body></html>`,
  { runScripts: 'dangerously', url: 'http://localhost:8765/' });

console.log('--- scanning the flattened text ---');
{
  const dom = page('<p>hello</p>');
  const F = dom.window.synapseFind;
  check('the find bar installs itself', !!F, 'window.synapseFind missing');
  if (F) {
    check('a word is found', JSON.stringify(F.scanText('one two one', 'one', false))
          === '[[0,3],[8,11]]', JSON.stringify(F.scanText('one two one', 'one', false)));
    check('nothing matches an empty query', F.scanText('abc', '', false).length === 0,
          String(F.scanText('abc', '', false).length));
    check('case is ignored by default',
          F.scanText('Rollback', 'rollback', false).length === 1, 'missed');
    check('and respected when asked',
          F.scanText('Rollback', 'rollback', true).length === 0, 'matched anyway');
    // Stepping through overlapping hits is not something a reader wants.
    check('matches do not overlap', JSON.stringify(F.scanText('aaaa', 'aa', false))
          === '[[0,2],[2,4]]', JSON.stringify(F.scanText('aaaa', 'aa', false)));
    check('a query longer than the text finds nothing',
          F.scanText('ab', 'abcdef', false).length === 0, 'found something');
  }
}

console.log('\n--- flattening a real page ---');
{
  const dom = page(`<div><b>TEAM-1</b> rollback strategy</div>
     <table><tr><td>Dev   One</td><td>2.5d</td></tr></table>
     <script>var hidden = "rollback";</script>
     <style>.x{content:"rollback"}</style>`);
  const F = dom.window.synapseFind;
  const idx = F.buildIndex(dom.window.document.body);
  check('a phrase spanning two elements matches',
        F.scanText(idx.text, 'TEAM-1 rollback', false).length === 1, JSON.stringify(idx.text));
  check('the markup\'s own indentation does not break a match',
        F.scanText(idx.text, 'strategy Dev One', false).length === 1, JSON.stringify(idx.text));
  check('runs of spaces inside text collapse too',
        F.scanText(idx.text, 'Dev One', false).length === 1, JSON.stringify(idx.text));
  check('script contents are not searched',
        F.scanText(idx.text, 'var hidden', false).length === 0, idx.text);
  check('nor is css', !/content:/.test(idx.text), idx.text);
  check('the nav itself is not searched', !/Dashboard/.test(idx.text), idx.text.slice(0, 120));
  check('the find box itself is not searched', !/Find on page/.test(idx.text),
        idx.text.slice(0, 120));
  check('every character maps back to a node', idx.map.length === idx.text.length,
        idx.map.length + ' vs ' + idx.text.length);
  const hit = F.scanText(idx.text, 'rollback', false)[0];
  check('a match points at the node it came from',
        idx.map[hit[0]][0].nodeValue.slice(idx.map[hit[0]][1], idx.map[hit[0]][1] + 8) === 'rollback',
        idx.map[hit[0]][0].nodeValue);
}

console.log('\n--- driving the bar ---');
{
  const dom = page('<div id="c">alpha beta alpha gamma ALPHA</div>');
  const w = dom.window, d = w.document, F = w.synapseFind;
  const press = (k, mods) => d.dispatchEvent(new w.KeyboardEvent('keydown',
    Object.assign({key: k, bubbles: true, cancelable: true}, mods || {})));

  check('it starts closed', !d.body.classList.contains('sfind-on'), 'already open');
  const ev = new w.KeyboardEvent('keydown', {key: 'f', metaKey: true, bubbles: true,
                                             cancelable: true});
  d.dispatchEvent(ev);
  check('Cmd+F opens it', d.body.classList.contains('sfind-on'), 'still closed');
  check('and the browser\'s own find is suppressed', ev.defaultPrevented, 'not prevented');
  check('the box takes focus', d.activeElement === d.getElementById('sfindIn'),
        d.activeElement && d.activeElement.id);

  F.set('alpha');
  check('it counts every match', F.state().hits === 3, String(F.state().hits));
  check('and says where you are', d.getElementById('sfindN').textContent === '1/3',
        d.getElementById('sfindN').textContent);
  checkNoThrow('Enter moves to the next',
               () => { F.step(1); return d.getElementById('sfindN').textContent; }, '2/3');
  checkNoThrow('and wraps backwards past the first',
               () => { F.step(-1); F.step(-1); return d.getElementById('sfindN').textContent; },
               '3/3');
  checkNoThrow('and forwards past the last',
               () => { F.step(1); return d.getElementById('sfindN').textContent; }, '1/3');

  d.getElementById('sfindAa').dispatchEvent(new w.MouseEvent('click', {bubbles: true}));
  check('matching case narrows it', F.state().hits === 2, String(F.state().hits));
  check('and the toggle shows it is on',
        d.getElementById('sfindAa').classList.contains('on'), 'no on state');
  d.getElementById('sfindAa').dispatchEvent(new w.MouseEvent('click', {bubbles: true}));

  F.set('nothing here');
  check('no match says so', d.getElementById('sfindN').textContent === '0/0',
        d.getElementById('sfindN').textContent);
  check('and it is marked as a miss', d.getElementById('sfindN').className === 'none',
        d.getElementById('sfindN').className);
  check('stepping with no matches does nothing', (F.step(1), F.state().at === 0),
        String(F.state().at));

  F.set('alpha');
  press('Escape');
  check('Escape closes it', !d.body.classList.contains('sfind-on'), 'still open');
  check('and drops the matches', F.state().hits === 0, String(F.state().hits));
}

console.log('\n--- it survives a page that redraws itself ---');
{
  const dom = page('<div id="c">alpha</div>');
  const w = dom.window, d = w.document, F = w.synapseFind;
  F.open(); F.set('alpha');
  check('one match to start', F.state().hits === 1, String(F.state().hits));
  d.getElementById('c').innerHTML = 'alpha alpha alpha';
  F.run(true);
  check('a repaint is picked up, not left stale', F.state().hits === 3, String(F.state().hits));
  check('the page itself is untouched', d.getElementById('c').innerHTML === 'alpha alpha alpha',
        d.getElementById('c').innerHTML);
  // Nothing is wrapped: matches are drawn with the CSS Highlight API, so a page that
  // carries drag handlers and repaints itself keeps working.
  check('no elements were injected into the content',
        d.getElementById('c').children.length === 0,
        String(d.getElementById('c').children.length));
  check('and the observer does not retrigger on the bar\'s own counter',
        F.state().hits === 3, String(F.state().hits));
}

console.log(fail.length ? `\n${fail.length} of ${ran} FAILED: ${fail.join(', ')}`
                        : `\nall ${ran} checks passed`);
process.exit(fail.length ? 1 : 0);
