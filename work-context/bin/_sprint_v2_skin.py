#!/usr/bin/env python3
"""Generate sprint-planner-v2.html as a pure visual reskin of sprint-planner.html.

Same pattern as dashboard v3: the working <script> (all the API wiring, localStorage,
accept flow) is kept BYTE-FOR-BYTE; only the <style> block and a few static-markup
niceties (step-number chips, title) are swapped. v2 is served by the same sprint_server
(static derived/ dir) at /sprint-planner-v2.html, shares the sprintPlanner_v3 localStorage
key, and hits the identical /api/* endpoints — so it stays in lockstep with v1's logic.

Re-run after editing NEW_CSS to regenerate. Local-only artifact.
"""
import re
import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from derive.sources_config import team_title  # noqa: E402

DERIVED = ROOT / "derived"
SRC = DERIVED / "sprint-planner.html"
OUT = DERIVED / "sprint-planner-v2.html"

# v1's title/masthead carry the team display name; mirror it from config so this
# generator holds no hardcoded org identity (the .replace target must match v1 exactly).
TEAM = team_title()

NEW_CSS = """
  @import url('https://fonts.googleapis.com/css2?family=Hanken+Grotesk:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500&display=swap');
  :root{
    --bg:#f3f6fa; --surface:#ffffff; --surface2:#eef2f8; --border:#e4e9f2;
    --text:#15202e; --muted:#5b6675; --hint:#9aa6b4;
    --accent:#3a55d9; --accent-2:#2c41b8; --accent-soft:rgba(58,85,217,.07);
    --avail:#e9f1e8; --leave:#f6e9ec; --leave-t:#a23b4e; --wfh:#f6efdd; --wfh-t:#866113;
    --onc:#e7eefa; --onc-t:#2b57a8; --hol:#eceaf9; --hol-t:#4742ab; --we:#eef1f5;
    --info:#2b57a8; --danger:#b23a4a; --danger-bg:#f8edef; --warn:#866113; --ok:#3b6b3f; --sp-bg:#e4eede;
    --shadow:0 1px 2px rgba(20,32,46,.04),0 2px 6px rgba(20,32,46,.05);
    --radius:14px;
  }
  :root[data-theme="dark"]{
    --bg:#10151c; --surface:#171f2a; --surface2:#1e2835; --border:#2a3543;
    --text:#e6ecf3; --muted:#97a3b2; --hint:#697483;
    --accent:#7e8df2; --accent-2:#6f7eea; --accent-soft:rgba(126,141,242,.15);
    --avail:#1b2a17; --leave:#3a1e24; --leave-t:#f0a3b0; --wfh:#352a13; --wfh-t:#e8c074;
    --onc:#162840; --onc-t:#8fb4ea; --hol:#221f4a; --hol-t:#b3adf2; --we:#1e2835;
    --info:#8fb4ea; --danger:#f0a3b0; --danger-bg:#3a1e24; --warn:#e8c074; --ok:#9fce7e; --sp-bg:#23311a;
    --shadow:0 1px 2px rgba(0,0,0,.32);
  }
  @media (prefers-color-scheme: dark){
    :root[data-theme="auto"]{
      --bg:#10151c; --surface:#171f2a; --surface2:#1e2835; --border:#2a3543;
      --text:#e6ecf3; --muted:#97a3b2; --hint:#697483;
      --accent:#7e8df2; --accent-2:#6f7eea; --accent-soft:rgba(126,141,242,.15);
      --avail:#1b2a17; --leave:#3a1e24; --leave-t:#f0a3b0; --wfh:#352a13; --wfh-t:#e8c074;
      --onc:#162840; --onc-t:#8fb4ea; --hol:#221f4a; --hol-t:#b3adf2; --we:#1e2835;
      --info:#8fb4ea; --danger:#f0a3b0; --danger-bg:#3a1e24; --warn:#e8c074; --ok:#9fce7e; --sp-bg:#23311a;
      --shadow:0 1px 2px rgba(0,0,0,.32);
    }
  }
  *{box-sizing:border-box}
  html{scroll-behavior:smooth}
  body{margin:0;background:var(--bg);color:var(--text);
    font-family:'Hanken Grotesk',-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
    font-size:14px;line-height:1.55;padding:0;-webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility;}
  .wrap{max-width:1180px;margin:0 auto;padding:26px 26px 96px;}
  /* masthead */
  .topbar{display:flex;justify-content:space-between;align-items:center;gap:12px;
    padding-bottom:14px;border-bottom:1px solid var(--border);margin-bottom:4px;}
  h1{font-size:23px;font-weight:700;letter-spacing:-.02em;margin:0;}
  h1 .tag{font:500 11px/1 'IBM Plex Mono',monospace;color:var(--accent);background:var(--accent-soft);
    padding:3px 7px;border-radius:6px;margin-left:9px;vertical-align:middle;letter-spacing:0;}
  .sub{color:var(--muted);font-size:12.5px;margin:8px 0;}
  #theme{font-size:12px;padding:7px 13px;white-space:nowrap;}
  /* step headers */
  h2{display:flex;align-items:center;gap:11px;font-size:16px;font-weight:600;letter-spacing:-.01em;margin:40px 0 5px;}
  h2 .n{display:inline-flex;align-items:center;justify-content:center;width:25px;height:25px;border-radius:8px;
    background:var(--accent-soft);color:var(--accent);font:600 12px/1 'IBM Plex Mono',monospace;flex:none;}
  h2 span{font-weight:400;font-size:12px;color:var(--muted);letter-spacing:0;}
  /* cards */
  .card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);
    padding:18px 20px;overflow-x:auto;box-shadow:var(--shadow);}
  /* tables */
  table{border-collapse:collapse;font-size:13px;}
  th{text-align:left;font-weight:500;color:var(--muted);font-size:10.5px;letter-spacing:.045em;text-transform:uppercase;
    padding:6px 8px;border-bottom:1px solid var(--border);white-space:nowrap;}
  td{padding:6px 8px;border-bottom:1px solid var(--border);vertical-align:middle;}
  tbody tr:last-child td{border-bottom:none;}
  /* inputs */
  input,select{font:inherit;font-size:13px;color:var(--text);background:var(--surface);border:1px solid var(--border);
    border-radius:8px;padding:6px 9px;transition:border-color .12s,box-shadow .12s;}
  input.num{width:64px;}
  input:focus,select:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft);}
  input.errin{border-color:var(--danger);background:var(--danger-bg);}
  input::placeholder{color:var(--hint);}
  /* buttons */
  button{font:inherit;font-size:13px;font-weight:500;color:var(--text);background:var(--surface);
    border:1px solid var(--border);border-radius:9px;padding:8px 15px;cursor:pointer;
    transition:background .12s,border-color .12s,transform .04s;}
  button:hover{background:var(--surface2);border-color:var(--hint);}
  button:active{transform:translateY(.5px);}
  button:focus-visible{outline:none;box-shadow:0 0 0 3px var(--accent-soft);}
  button.primary{background:var(--accent);color:#fff;border-color:var(--accent);font-weight:600;}
  button.primary:hover{background:var(--accent-2);border-color:var(--accent-2);}
  .del{color:var(--danger);border:none;background:none;padding:2px 7px;cursor:pointer;font-size:16px;border-radius:6px;}
  .del:hover{background:var(--danger-bg);}
  .toolbar{display:flex;gap:9px;flex-wrap:wrap;margin:12px 0;align-items:center;}
  /* metrics */
  .cards{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin:14px 0 6px;max-width:800px;}
  .metric{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:13px 16px;box-shadow:var(--shadow);}
  .metric .l{font-size:10.5px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em;}
  .metric .v{font:600 26px/1.1 'IBM Plex Mono',monospace;margin-top:6px;letter-spacing:-.02em;}
  /* gantt — the signature: a precise, calm capacity ribbon */
  .gantt-card{display:block;width:100%;background:var(--surface);border:1px solid var(--border);
    border-radius:var(--radius);padding:14px 16px;box-shadow:var(--shadow);margin-top:10px;}
  .g{width:100%;table-layout:fixed;border-collapse:collapse;}
  .g td,.g th{border:1px solid var(--border);text-align:center;padding:0;}
  .g .nm{text-align:left;padding:9px 13px;white-space:nowrap;font-size:13px;font-weight:500;width:22%;background:var(--surface);}
  .g .cell{height:38px;font-size:12px;font-weight:600;}
  .g .wk{font:500 11px/1.15 'Hanken Grotesk';color:var(--text);padding:0 7px;white-space:normal;}
  .g thead th{background:var(--surface2);}
  .g .dh{font:500 11px/1.2 'IBM Plex Mono',monospace;color:var(--text);padding:5px 2px;}
  .g .dh small{display:block;font-size:9px;color:var(--muted);font-weight:400;}
  .g .dh.hol{color:var(--hol-t);}
  .g .net{width:50px;font:600 12px/1 'IBM Plex Mono',monospace;background:var(--avail);min-width:38px;}
  .g .net.sp{background:var(--sp-bg);}
  .c-avail{background:var(--avail);} .c-L{background:var(--leave);color:var(--leave-t);}
  .c-W{background:var(--wfh);color:var(--wfh-t);} .c-O{background:var(--onc);color:var(--onc-t);}
  .c-H{background:var(--hol);color:var(--hol-t);} .c-WE{background:var(--we);}
  .legend{display:flex;flex-wrap:wrap;gap:14px;margin-top:11px;font-size:11px;color:var(--muted);}
  .legend span{display:flex;align-items:center;gap:6px;}
  .sw{width:11px;height:11px;border-radius:3px;display:inline-block;border:1px solid rgba(20,32,46,.05);}
  /* plan bits */
  .chip{display:inline-block;font:500 11px/1.4 'IBM Plex Mono',monospace;padding:2px 8px;border-radius:7px;
    background:var(--surface2);color:var(--muted);margin:2px 3px 2px 0;}
  .chip.r{background:var(--onc);color:var(--onc-t);} .chip.cont{color:var(--muted);}
  .callout{border-radius:10px;padding:11px 14px;font-size:13px;margin-top:9px;line-height:1.5;border:1px solid transparent;}
  .callout.danger{background:var(--danger-bg);color:var(--danger);border-color:rgba(178,58,74,.18);}
  .callout.warn{background:var(--wfh);color:var(--wfh-t);border-color:rgba(134,97,19,.20);}
  .callout.ok{background:var(--avail);color:var(--ok);border-color:rgba(59,107,63,.20);}
  .rationale{background:var(--accent-soft);border-radius:10px;padding:12px 15px;font-size:13px;line-height:1.55;
    margin:8px 0;border-left:3px solid var(--accent);}
  .pick{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:10px 13px;margin-bottom:7px;
    font-size:13px;box-shadow:var(--shadow);}
  .pick a,.tview a{color:var(--info);text-decoration:none;font-weight:500;} .pick a:hover,.tview a:hover{text-decoration:underline;}
  .tlink{font-size:11px;}
  .tview{background:var(--surface);border:1px solid var(--accent);border-radius:10px;padding:12px 15px;margin:9px 0;
    font-size:13px;box-shadow:var(--shadow);}
  .empty{color:var(--hint);font-size:13px;padding:10px 2px;}
  code{background:var(--surface2);padding:1px 6px;border-radius:5px;font:500 12px 'IBM Plex Mono',monospace;}
  details summary{cursor:pointer;color:var(--muted);}
  @media (max-width:640px){
    .wrap{padding:18px 14px 64px;} .cards{grid-template-columns:repeat(2,1fr);} h2{margin-top:30px;}
    .topbar{flex-wrap:wrap;}
  }
  @media (prefers-reduced-motion:reduce){ *{transition:none!important;scroll-behavior:auto!important;} }
"""


def main():
    html = SRC.read_text()

    # 1) swap the entire <style>…</style> block
    html = re.sub(r"<style>.*?</style>", "<style>" + NEW_CSS + "</style>", html, count=1, flags=re.S)

    # 2) title + masthead wordmark
    html = html.replace(f"<title>Sprint Planner — {TEAM}</title>",
                        "<title>Sprint Planner · v2</title>")
    html = html.replace(
        '<div style="display:flex;justify-content:space-between;align-items:center;gap:12px;">\n'
        f'    <h1>Sprint planner — {TEAM}</h1>',
        '<header class="topbar">\n'
        '    <h1>Sprint planner<span class="tag">v2</span></h1>')
    # close the header tag (the matching </div> right after the theme button)
    html = html.replace(
        '<button id="theme" style="font-size:12px;padding:6px 12px;white-space:nowrap">◐ Auto</button>\n  </div>',
        '<button id="theme">◐ Auto</button>\n  </header>')

    # 3) step-number chips: "<h2 …>3 · Title …" -> "<h2 …><span class='n'>3</span>Title …"
    html = re.sub(r'(<h2[^>]*>)(\d+)\s*·\s*', r'\1<span class="n">\2</span>', html)

    OUT.write_text(html)
    print(f"wrote {OUT}  ({len(html)} bytes)")


if __name__ == "__main__":
    main()
