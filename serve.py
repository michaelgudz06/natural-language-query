"""
Local interactive tester for the Fundable NL query planner.

Type a question, watch the whole decision unfold: which model handled it, WHY it
picked that route (the model's own reasoning), what it looked up and what it
resolved to, the exact query it built, and whether the live Fundable API accepts
it (with a real result count).

Run:
    python3 serve.py
    # then open http://localhost:8000 in your browser

Reads keys from .env automatically. Uses the two-stage pipeline (cheap gpt-4o-mini
escalating to strong gpt-5.1) + the live Fundable API.
"""
from __future__ import annotations

import json
import os
import ssl
import sys
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

# --- load .env ------------------------------------------------------------
_ENV = os.path.join(os.path.dirname(__file__), ".env")
if os.path.exists(_ENV):
    for line in open(_ENV):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k, v)

import certifi  # noqa: E402
from pipeline import production_pipeline  # noqa: E402
from filters_view import describe_filters  # noqa: E402
from exclude import apply_location_exclusion  # noqa: E402
from resolvers import OverrideResolver  # noqa: E402

_CTX = ssl.create_default_context(cafile=certifi.where())
_PIPE = None
_BASE_RESOLVER = None


def pipe():
    global _PIPE, _BASE_RESOLVER
    if _PIPE is None:
        _PIPE = production_pipeline()   # single model, no router
        _BASE_RESOLVER = _PIPE.resolver
    return _PIPE


def api_accepts(route: str, body: dict):
    base = os.environ.get("FUNDABLE_BASE_URL", "https://www.tryfundable.ai/api/v1")
    key = os.environ.get("FUNDABLE_API_KEY", "")
    b = dict(body); b["page_size"] = 1
    req = urllib.request.Request(base + route.split(" ")[1], data=json.dumps(b).encode(),
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=25, context=_CTX) as r:
            d = json.loads(r.read().decode())
        data = d.get("data", {})
        total = (d.get("meta") or {}).get("total_count")
        if total is None and isinstance(data, dict):
            total = data.get("totalCount")
        return {"accepted": True, "total_count": total}
    except Exception as e:  # noqa: BLE001
        return {"accepted": False, "error": str(e)[:120]}


def _fetch(route: str, body: dict, page_size: int) -> dict:
    """POST a query and return {count, companies} (used by the exclusion helper)."""
    base = os.environ.get("FUNDABLE_BASE_URL", "https://www.tryfundable.ai/api/v1")
    key = os.environ.get("FUNDABLE_API_KEY", "")
    b = dict(body); b["page_size"] = page_size
    req = urllib.request.Request(base + route.split(" ")[1], data=json.dumps(b).encode(),
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=25, context=_CTX) as r:
            d = json.loads(r.read().decode())
        count = (d.get("meta") or {}).get("total_count") or (d.get("data") or {}).get("totalCount")
        companies = (d.get("data") or {}).get("companies", [])
        return {"count": count, "companies": companies}
    except Exception:  # noqa: BLE001
        return {"count": None, "companies": []}


def plan(query: str, overrides: list | None = None) -> dict:
    t0 = time.time()
    p = pipe()
    # apply the user's clarification picks (clickable follow-ups), else default
    p.resolver = OverrideResolver(_BASE_RESOLVER, overrides) if overrides else _BASE_RESOLVER
    r = p.run(query)
    r["api"] = api_accepts(r["route"], r["body"]) if r["valid"] else {"accepted": False, "error": "invalid body"}
    r["filters"] = describe_filters(r["route"], r["body"], r.get("resolver_calls"))
    # client-side exclusion ("everywhere but X") — only /companies carries the
    # company location chain on its rows, so scope the demo to that route.
    excl_locs = (r.get("exclusions") or {}).get("locations") or []
    if excl_locs and r["route"] == "POST /companies" and r["valid"]:
        r["exclusion_result"] = apply_location_exclusion(
            r["route"], r["body"], excl_locs, lambda b, ps: _fetch(r["route"], b, ps))
    r["wall_s"] = round(time.time() - t0, 1)
    return r


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _send(self, code, body, ctype="application/json"):
        # Encode FIRST, then set Content-Length to the BYTE count. Using the
        # string length truncates any response containing multi-byte UTF-8
        # characters (⚠ → — “ ” ✓ …), which silently breaks the served JS.
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, PAGE, "text/html; charset=utf-8")
        elif self.path == "/app.js":
            self._send(200, APP_JS, "application/javascript; charset=utf-8")
        elif self.path == "/api/report":
            p = os.path.join(os.path.dirname(__file__), "docs", "report.json")
            if os.path.exists(p):
                self._send(200, open(p).read(), "application/json")
            else:
                self._send(404, json.dumps({"error": "no report"}))
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        if self.path != "/api/plan":
            return self._send(404, b"not found", "text/plain")
        n = int(self.headers.get("Content-Length", 0))
        try:
            payload = json.loads(self.rfile.read(n))
            q = payload.get("query", "").strip()
            overrides = payload.get("overrides") or []
            if not q:
                return self._send(400, json.dumps({"error": "empty query"}))
            self._send(200, json.dumps(plan(q, overrides)))
        except Exception as e:  # noqa: BLE001
            self._send(500, json.dumps({"error": str(e)[:200]}))


PAGE = r"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Fundable Planner — Live Tester</title>
<style>
:root{--paper:#0e1218;--card:#161c25;--ink:#e8edf3;--muted:#9aa6b4;--faint:#6b7686;
--line:#232c38;--line2:#313c4a;--accent:#34c9bd;--accent-soft:#10322f;--ok:#46c17a;
--warn:#e0a53a;--ok-soft:#14301f;--warn-soft:#33260d;--code:#0b0f15;
--key:#7fd8cf;--str:#e0a55f;--num:#8fb8ff;
--mono:ui-monospace,"SF Mono","JetBrains Mono",Menlo,Consolas,monospace;
--sans:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);font-family:var(--sans);line-height:1.5}
.wrap{max-width:820px;margin:0 auto;padding:36px 20px 80px}
.eyebrow{font-family:var(--mono);font-size:12px;letter-spacing:.16em;text-transform:uppercase;color:var(--accent);margin:0 0 8px}
h1{font-size:26px;margin:0 0 6px;letter-spacing:-.02em}
.sub{color:var(--muted);font-size:15px;margin:0 0 22px}
.box{display:flex;gap:8px;margin-bottom:12px}
input{flex:1;background:var(--card);border:1px solid var(--line2);border-radius:10px;
padding:13px 15px;color:var(--ink);font-size:15px;font-family:var(--sans);outline:none}
input:focus{border-color:var(--accent)}
button{background:var(--accent);color:#04201d;border:0;border-radius:10px;padding:0 20px;
font-weight:650;font-size:14px;cursor:pointer;font-family:var(--sans)}
button:disabled{opacity:.5;cursor:default}
.chips{display:flex;flex-wrap:wrap;gap:7px;margin-bottom:26px}
.chip{font-size:12.5px;color:var(--muted);background:var(--card);border:1px solid var(--line);
border-radius:999px;padding:5px 11px;cursor:pointer}
.chip:hover{border-color:var(--accent);color:var(--ink)}
.step{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px 18px;margin-bottom:12px}
.step .lbl{font-family:var(--mono);font-size:11px;letter-spacing:.1em;text-transform:uppercase;color:var(--faint);margin-bottom:9px}
.pill{font-family:var(--mono);font-size:11px;font-weight:600;padding:3px 8px;border-radius:999px;white-space:nowrap}
.pill.route{background:var(--accent-soft);color:var(--accent)}
.pill.cheap{background:var(--ok-soft);color:var(--ok)}
.pill.strong{background:var(--warn-soft);color:var(--warn)}
.pill.ok{background:var(--ok-soft);color:var(--ok)}
.pill.warn{background:var(--warn-soft);color:var(--warn)}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.reason{font-size:15px;line-height:1.55}
.stats{display:flex;flex-wrap:wrap;gap:8px;margin-top:14px}
.stat{font-family:var(--mono);font-size:11.5px;color:var(--muted);background:var(--code);
border:1px solid var(--line);border-radius:7px;padding:5px 10px}
.stat b{color:var(--accent);font-weight:600}
.esc{font-size:13px;color:var(--warn);margin-top:10px;padding-top:10px;border-top:1px dashed var(--line2)}
.lu{display:flex;gap:10px;align-items:baseline;padding:6px 0;border-bottom:1px solid var(--line);font-size:13.5px}
.lu:last-child{border:0}
.lu .t{font-family:var(--mono);font-size:11px;color:var(--faint);width:66px;flex:none;text-transform:uppercase}
.lu .arrow{color:var(--faint)}
.lu .val{font-family:var(--mono);font-size:12.5px;color:var(--accent)}
.opts{display:flex;flex-wrap:wrap;gap:5px;margin-top:5px}
.opt{font-family:var(--mono);font-size:11px;padding:3px 7px;border-radius:6px;border:1px solid var(--line2);color:var(--ink);background:none}
button.opt{cursor:pointer}
button.opt.pick:hover{border-color:var(--accent);color:var(--accent);background:var(--accent-soft)}
.filters{display:flex;flex-wrap:wrap;gap:8px}
.fchip{display:inline-flex;align-items:stretch;border:1px solid var(--accent);border-radius:8px;overflow:hidden;font-family:var(--mono);font-size:12px}
.fchip .fk{background:var(--accent-soft);color:var(--accent);padding:5px 9px;font-weight:600}
.fchip .fv{padding:5px 10px;color:var(--ink)}
pre{margin:0;background:var(--code);border-radius:9px;padding:13px 15px;overflow-x:auto}
code{font-family:var(--mono);font-size:12.5px;line-height:1.6;white-space:pre;color:#cdd8e4}
.k{color:var(--key)}.s{color:var(--str)}.n{color:var(--num)}
.big{font-size:15px;font-weight:600}
.muted{color:var(--muted)}.faint{color:var(--faint);font-size:12px;font-family:var(--mono)}
.spin{display:inline-block;width:15px;height:15px;border:2px solid var(--line2);border-top-color:var(--accent);border-radius:50%;animation:s .7s linear infinite;vertical-align:-2px}
@keyframes s{to{transform:rotate(360deg)}}
.tabs{display:flex;gap:4px;margin:0 0 22px;border-bottom:1px solid var(--line)}
.tab{background:none;border:0;border-bottom:2px solid transparent;color:var(--muted);
font-family:var(--sans);font-size:14px;font-weight:550;padding:9px 14px;cursor:pointer;margin-bottom:-1px}
.tab:hover{color:var(--ink)}
.tab.active{color:var(--accent)}
.panel[hidden]{display:none}
.banner{background:var(--accent-soft);border:1px solid var(--accent);border-radius:12px;padding:16px 18px;margin-bottom:22px}
.banner .h{font-family:var(--mono);font-size:12px;letter-spacing:.1em;text-transform:uppercase;color:var(--accent);margin-bottom:6px}
.banner .m{font-size:17px;font-weight:640;margin-bottom:10px}
.banner .m code{font-family:var(--mono);font-size:15px;color:var(--accent)}
.scorecard{display:flex;flex-wrap:wrap;gap:8px}
.sc{font-family:var(--mono);font-size:12px;background:var(--card);border:1px solid var(--line);border-radius:8px;padding:6px 11px}
.sc b{color:var(--accent);font-weight:600}
h2.sec{font-size:13px;font-family:var(--mono);text-transform:uppercase;letter-spacing:.1em;color:var(--faint);margin:28px 0 6px}
.subtle{font-size:12.5px;color:var(--muted);margin:0 0 14px}
.twrap{overflow-x:auto;border:1px solid var(--line);border-radius:10px;margin-bottom:6px}
table.cmp{width:100%;border-collapse:collapse;font-size:13px}
table.cmp th,table.cmp td{text-align:right;padding:9px 13px;border-bottom:1px solid var(--line);white-space:nowrap}
table.cmp th:first-child,table.cmp td:first-child{text-align:left}
table.cmp th{font-family:var(--mono);font-size:10.5px;text-transform:uppercase;letter-spacing:.06em;color:var(--faint);font-weight:600}
table.cmp td{font-family:var(--mono);font-variant-numeric:tabular-nums;color:var(--muted)}
table.cmp td:first-child{color:var(--ink);font-family:var(--sans)}
table.cmp tbody tr:last-child td{border-bottom:0}
table.cmp tr.win td{background:var(--accent-soft)}
table.cmp tr.win td:first-child{color:var(--accent);font-weight:600}
.pillmini{font-family:var(--mono);font-size:10px;background:var(--accent-soft);color:var(--accent);padding:2px 6px;border-radius:5px;margin-left:6px}
</style></head><body><div class="wrap">
<p class="eyebrow">Live Tester · Real Models · Real API</p>
<h1>Ask it anything about startups &amp; funding</h1>
<p class="sub">Type a question the way you'd say it. Watch it decide the route, look up the real IDs, build the query, and hit the live Fundable API.</p>

<div class="tabs">
  <button class="tab active" data-tab="try">Try</button>
  <button class="tab" data-tab="compare">Model comparison</button>
  <button class="tab" data-tab="report">Report</button>
</div>

<div id="panel-try" class="panel">
  <div class="box">
    <input id="q" placeholder="e.g. Series B fintech companies in New York that raised over $20M" autofocus>
    <button id="go">Run</button>
  </div>
  <div class="chips" id="chips"></div>
  <div id="out"></div>
</div>

<div id="panel-compare" class="panel" hidden>
  <div class="banner">
    <div class="h">Production pick</div>
    <div class="m"><code>gemini-3.5-flash-lite</code> — single model, no router</div>
    <div class="scorecard">
      <span class="sc">route <b>97%</b></span>
      <span class="sc">API-accepted <b>100%</b></span>
      <span class="sc">JSON-valid <b>100%</b></span>
      <span class="sc"><b>$0.63</b> / 1,000</span>
      <span class="sc">end-to-end <b>2.2s</b> p50</span>
    </div>
  </div>

  <h2 class="sec">Models — planning quality</h2>
  <p class="subtle">Route + intent extraction, 109 test cases, live on OpenRouter. Isolated from the (model-agnostic) resolver/API layer.</p>
  <div class="twrap"><table class="cmp">
    <thead><tr><th>Model</th><th>Route acc</th><th>Intent recall</th><th>$ / 1k</th><th>Latency p50</th></tr></thead>
    <tbody>
      <tr class="win"><td>gemini-3.5-flash-lite <span class="pillmini">PICK</span></td><td>96%</td><td>95%</td><td>$0.60</td><td>999ms</td></tr>
      <tr><td>gemini-2.5-flash</td><td>95%</td><td>94%</td><td>$0.57</td><td>1237ms</td></tr>
      <tr><td>gpt-4o-mini</td><td>93%</td><td>95%</td><td>$0.18</td><td>1802ms</td></tr>
      <tr><td>gpt-5.1</td><td>91%</td><td>95%</td><td>$2.18</td><td>1810ms</td></tr>
      <tr><td>gemma-4-31b</td><td>93%</td><td>95%</td><td>$0.13</td><td>3422ms</td></tr>
      <tr><td>gemini-2.5-flash-lite</td><td>86%</td><td>91%</td><td>$0.13</td><td>903ms</td></tr>
      <tr><td>gemini-3.1-flash-lite</td><td>85%</td><td>94%</td><td>$0.43</td><td>1258ms</td></tr>
      <tr><td>qwen3.6-flash <span class="pillmini">CN</span></td><td>98%</td><td>96%</td><td>$1.24</td><td>7098ms</td></tr>
      <tr><td>minimax-m2.7 <span class="pillmini">CN</span></td><td>96%</td><td>96%</td><td>$0.99</td><td>2249ms</td></tr>
      <tr><td>deepseek-v4-flash <span class="pillmini">CN</span></td><td>95%</td><td>93%</td><td><b>$0.16</b></td><td>7057ms</td></tr>
    </tbody>
  </table></div>
  <p class="subtle">Surprise: gpt-5.1 (the priciest) wasn't the most accurate. The flash-lite tier beats it on accuracy, cost, and speed.
  Chinese models (CN) tested on request: <b>qwen3.6-flash tops route accuracy at 98%</b> but is 4.5&times; slower (it emits ~970 output tokens of reasoning);
  <b>deepseek-v4-flash is 4.5&times; cheaper</b> than our pick at similar accuracy, but slow and with a very unstable tail (p95 &gt; 100s).
  gemini-3.5-flash-lite stays the pick for an interactive tool because it's the fastest by a wide margin. Route accuracy varies &plusmn;2 points run-to-run.</p>

  <h2 class="sec">Architectures — different ways to build it</h2>
  <p class="subtle">Same 30 curated queries, different approaches. Verdict: single model wins — the router never fired, the orchestrator split just added latency.</p>
  <div class="twrap"><table class="cmp">
    <thead><tr><th>Architecture</th><th>Route acc</th><th>Intent recall</th><th>$ / 1k</th><th>Latency p50</th><th>Calls/query</th></tr></thead>
    <tbody>
      <tr class="win"><td>single: gemini-3.5-flash-lite <span class="pillmini">PICK</span></td><td>97%</td><td>91%</td><td>$0.61</td><td>990ms</td><td>1</td></tr>
      <tr><td>single: gpt-4o-mini</td><td>97%</td><td>91%</td><td>$0.18</td><td>1755ms</td><td>1</td></tr>
      <tr><td>router: 4o-mini &rarr; gpt-5.1</td><td>97%</td><td>91%</td><td>$0.18</td><td>1602ms</td><td>1.00</td></tr>
      <tr><td>orch/exec: 3.5-flash-lite + flash-lite</td><td>96%</td><td>86%</td><td>$0.22</td><td>1042ms</td><td>2</td></tr>
      <tr><td>single: gemma-4-31b</td><td>93%</td><td>95%</td><td>$0.13</td><td>3422ms</td><td>1</td></tr>
      <tr><td>router: flash-lite &rarr; 3.5-flash-lite</td><td>93%</td><td>87%</td><td>$0.15</td><td>955ms</td><td>1.03</td></tr>
      <tr><td>orch/exec: gpt-5.1 + flash-lite</td><td>93%</td><td>87%</td><td>$0.53</td><td>1090ms</td><td>2</td></tr>
      <tr><td>single: gemini-2.5-flash-lite</td><td>90%</td><td>87%</td><td>$0.13</td><td>957ms</td><td>1</td></tr>
    </tbody>
  </table></div>
  <p class="subtle">"Calls/query" = 1.00 for the router means it never escalated — the cheap model was always confident enough. So the router is pure overhead here; single model is the same accuracy, simpler.</p>
</div>

<div id="panel-report" class="panel" hidden></div>

</div>
<script src="/app.js"></script></body></html>"""


APP_JS = r"""
const EX=["Series B AI companies in New York that raised over $20M",
"Founders who previously worked at Google",
"CTOs of healthcare startups in Boston",
"Stanford founders who raised seed in fintech",
"Ex-Google founders in AI",
"Most active VC firms investing in climate",
"fintech companies, Series B+, everywhere but America",
"Which companies did Sequoia back?"];
const chips=document.getElementById('chips');
EX.forEach(e=>{const c=document.createElement('span');c.className='chip';c.textContent=e;
c.onclick=()=>{document.getElementById('q').value=e;run()};chips.appendChild(c)});

function esc(s){return (s||'').replace(/[&<>]/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[m]))}
function usd(x){x=x||0; if(x===0)return '$0'; if(x<0.01)return '$'+x.toFixed(5); return '$'+x.toFixed(2);}
function fmt(n){return (n==null)?'—':Number(n).toLocaleString();}
function hl(obj){let j=JSON.stringify(obj,null,2);
 return esc(j).replace(/"([^"]+)":/g,'"<span class=k>$1</span>":')
  .replace(/: "([^"]*)"/g,': "<span class=s>$1</span>"')
  .replace(/: (\d+\.?\d*)/g,': <span class=n>$1</span>')
  .replace(/: (true|false)/g,': <span class=n>$1</span>')}

const out=document.getElementById('out'),go=document.getElementById('go'),qi=document.getElementById('q');
qi.addEventListener('keydown',e=>{if(e.key==='Enter')run()});
go.onclick=run;

// tab switching
document.querySelectorAll('.tab').forEach(t=>{
  t.onclick=()=>{
    document.querySelectorAll('.tab').forEach(x=>x.classList.remove('active'));
    t.classList.add('active');
    const which=t.dataset.tab;
    document.getElementById('panel-try').hidden = which!=='try';
    document.getElementById('panel-compare').hidden = which!=='compare';
    document.getElementById('panel-report').hidden = which!=='report';
    if(which==='report') loadReport();
  };
});

let reportLoaded=false;
async function loadReport(){
  if(reportLoaded) return; reportLoaded=true;
  const el=document.getElementById('panel-report');
  el.innerHTML='<div class="step"><span class="spin"></span> <span class="muted">Loading evaluation report…</span></div>';
  try{
    const d=await (await fetch('/api/report')).json();
    renderReport(el,d);
  }catch(e){ el.innerHTML='<div class="step"><span class="pill warn">no report yet</span> <span class="muted">Run <code>python3 src/report.py</code> to generate it.</span></div>'; reportLoaded=false; }
}
function pctf(x){return Math.round((x||0)*100)+'%';}
function renderReport(el,d){
 const m=d.metrics; let h='';
 h+='<div class="banner"><div class="h">Evaluation report · '+esc(d.generated)+'</div>'+
    '<div class="m"><code>'+esc((d.production_model||'').replace(/^.*\//,''))+'</code> · '+esc(d.provider)+' · n='+d.n+' · parallel resolvers ✓</div>'+
    '<div class="scorecard"><span class="sc">route <b>'+pctf(m.route_accuracy)+'</b></span>'+
    '<span class="sc">API-valid <b>'+pctf(m.api_valid_rate)+'</b></span>'+
    '<span class="sc">JSON-valid <b>'+pctf(m.json_validity_rate)+'</b></span>'+
    '<span class="sc"><b>$'+m.cost_per_1k.toFixed(2)+'</b> / 1,000</span></div></div>';
 const npass=d.acceptance.filter(c=>c.pass).length;
 h+='<h2 class="sec">Acceptance criteria — '+npass+'/'+d.acceptance.length+(npass===d.acceptance.length?' ✓ all pass':'')+'</h2><div class="step">';
 d.acceptance.forEach(c=>{
   h+='<div style="display:flex;align-items:center;gap:10px;padding:7px 0;border-bottom:1px solid var(--line);font-size:13.5px">'+
      '<span class="pill '+(c.pass?'ok':'warn')+'">'+(c.pass?'PASS':'FAIL')+'</span>'+
      '<span style="flex:1">'+esc(c.name)+'</span><span class="faint">'+esc(c.value)+'</span></div>';
 });
 h+='</div>';
 const bre=m.route_accuracy_by_endpoint;
 if(bre){
   h+='<h2 class="sec">Route accuracy by endpoint</h2>';
   h+='<div class="twrap"><table class="cmp"><thead><tr><th>Endpoint</th><th>Cases</th><th>Route accuracy</th></tr></thead><tbody>';
   Object.keys(bre).forEach(k=>{h+='<tr><td>POST /'+esc(k)+'</td><td>'+bre[k].n+'</td><td>'+pctf(bre[k].accuracy)+'</td></tr>';});
   h+='</tbody></table></div><p class="subtle">All four endpoints are exercised — not just /companies.</p>';
 }
 h+='<h2 class="sec">Metrics (production model, live)</h2><div class="twrap"><table class="cmp"><tbody>';
 [['Route accuracy',pctf(m.route_accuracy)],['Filter accuracy',pctf(m.filter_accuracy)],
  ['Resolver-backed field F1',pctf(m.resolver_field_f1)],['JSON validity',pctf(m.json_validity_rate)],
  ['API-valid (dry-run)',pctf(m.api_valid_rate)],['Avg tokens in / out',m.avg_in_tokens+' / '+m.avg_out_tokens],
  ['Cost / 1,000 queries','$'+m.cost_per_1k.toFixed(2)],
  ['Model latency p50 / p95',m.model_p50_ms+' / '+m.model_p95_ms+' ms'],
  ['Full plan, excl. resolver p50 / p95',m.plan_excl_resolver_p50_ms+' / '+m.plan_excl_resolver_p95_ms+' ms'],
  ['Full plan, incl. resolver p50 / p95',m.plan_incl_resolver_p50_ms+' / '+m.plan_incl_resolver_p95_ms+' ms']
 ].forEach(r=>h+='<tr><td>'+r[0]+'</td><td>'+r[1]+'</td></tr>');
 h+='</tbody></table></div>';
 h+='<h2 class="sec">Models tested</h2><div class="twrap"><table class="cmp"><thead><tr><th>Model</th><th>Provider</th><th>Route acc</th><th>Tok in/out</th><th>$/1k</th><th>p50/p95</th></tr></thead><tbody>';
 d.models_tested.forEach(x=>{const win=x.model===d.production_model;
   h+='<tr class="'+(win?'win':'')+'"><td>'+esc(x.model.replace(/^.*\//,''))+(win?' <span class="pillmini">PICK</span>':'')+
      '</td><td>'+esc(x.provider)+'</td><td>'+pctf(x.route_acc)+'</td><td>'+x.avg_in+'/'+x.avg_out+'</td><td>$'+x.cost_per_1k.toFixed(2)+'</td><td>'+x.model_p50+'/'+x.model_p95+'ms</td></tr>';});
 h+='</tbody></table></div>';
 h+='<h2 class="sec">Recommendation</h2><p class="subtle" style="font-size:14px;max-width:70ch">'+esc(d.recommendation)+'</p>';
 el.innerHTML=h;
}

let lastQuery='', overrides=[];
async function run(){ lastQuery=qi.value.trim(); overrides=[]; await runWith(); }
async function pick(type,query,permalink,label){
  // user clicked a clarification option — lock it in and re-run
  overrides=overrides.filter(o=>!(o.type===type&&o.query===query));
  overrides.push({type,query,permalink,label});
  await runWith();
}
async function runWith(){
 const q=lastQuery; if(!q)return;
 go.disabled=true; out.innerHTML='<div class="step"><span class="spin"></span> <span class="muted">Thinking… (model + live API, ~3–8s)</span></div>';
 try{
  const r=await fetch('/api/plan',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({query:q,overrides})});
  const d=await r.json();
  if(d.error){out.innerHTML='<div class="step"><span class="pill warn">error</span> <span class="muted">'+esc(d.error)+'</span></div>';go.disabled=false;return}
  render(q,d);
 }catch(e){out.innerHTML='<div class="step"><span class="pill warn">error</span> '+esc(''+e)+'</div>'}
 go.disabled=false;
}

function render(q,d){
 const t=d.trace||{}, fin=t.final||{}, lg=d._log||{};
 const modelName = (lg.model||'model').replace(/^.*\//,'');
 const modelPill = lg.escalated
   ? '<span class="pill strong">escalated → '+esc(modelName)+'</span>'
   : '<span class="pill cheap">'+esc(modelName)+'</span>';
 let h='';

 // banner showing the clarification picks currently locked in
 if(overrides.length){
   h+='<div class="step" style="border-color:var(--accent)"><div class="row" style="align-items:center">'+
      '<span class="pill route">your picks</span>'+
      overrides.map(o=>'<span class="fchip"><span class="fk">'+esc(o.query)+'</span><span class="fv">'+esc(o.label)+'</span></span>').join('')+
      '<button class="opt" id="clearpicks">clear</button></div></div>';
 }

 // 1. decision + reasoning
 h+='<div class="step"><div class="lbl">1 · How it decided</div>';
 h+='<div class="row" style="margin-bottom:10px">'+modelPill+
    '<span class="pill route">'+esc(d.route)+'</span>'+
    '<span class="faint">confidence '+(Math.round((fin.confidence||0)*100))+'%</span></div>';
 h+='<div class="reason">'+esc(fin.reasoning||'—')+'</div>';
 // thinking time + cost strip
 h+='<div class="stats">'+
    '<span class="stat"><b>'+((lg.think_ms||0)/1000).toFixed(1)+'s</b> thinking</span>'+
    '<span class="stat"><b>'+(lg.tokens_in||0)+'</b> in / <b>'+(lg.tokens_out||0)+'</b> out tokens</span>'+
    '<span class="stat"><b>'+usd(lg.cost_usd)+'</b> this query</span>'+
    '<span class="stat">≈ <b>'+usd(lg.cost_per_1k)+'</b> / 1,000</span>'+
    '</div>';
 if(lg.escalated){
   h+='<div class="esc">⚠ The cheap model was '+(lg.escalate_reason==='low_confidence'?'unsure (low confidence)':'about to build an invalid query')+
      ', so it handed off to the stronger model automatically.';
   if(t.cheap&&t.cheap.reasoning) h+='<br><span class="muted">Cheap model first said:</span> “'+esc(t.cheap.reasoning)+'”';
   h+='</div>';
 }
 h+='</div>';

 // 2. filters enabled
 const filters=d.filters||[];
 if(filters.length){
   h+='<div class="step"><div class="lbl">2 · Filters it enabled</div>';
   h+='<div class="filters">';
   filters.forEach(f=>{h+='<span class="fchip"><span class="fk">'+esc(f.filter)+'</span><span class="fv">'+esc(f.value)+'</span></span>';});
   h+='</div>';
   h+='<div class="faint" style="margin-top:11px">Each chip is one filter this query switched on — this is exactly what maps onto your filter sidebar controls.</div>';
   h+='</div>';
 }

 // 3. lookups
 const calls=d.resolver_calls||[];
 if(calls.length){
   h+='<div class="step"><div class="lbl">3 · What it looked up (live)</div>';
   calls.forEach(c=>{
     h+='<div class="lu"><span class="t">'+esc(c.type)+'</span><span>“'+esc(c.params.name)+'”</span><span class="arrow">→</span>';
     if(c.status==='resolved') h+='<span class="val">'+esc(c.chosen_value)+'</span>';
     else if(c.status==='needs_clarification'){h+='<span class="pill warn">needs you to pick</span>';}
     else h+='<span class="muted">no match — filter dropped</span>';
     h+='</div>';
     if(c.status==='needs_clarification'&&c.options&&c.options.length){
       h+='<div class="opts">';
       c.options.forEach(o=>{
         h+='<button class="opt pick" data-type="'+esc(c.type)+'" data-query="'+esc(c.params.name)+
            '" data-permalink="'+esc(o.value)+'" data-label="'+esc(o.label)+'">'+esc(o.label)+'</button>';
       });
       h+='<div class="faint" style="margin-top:6px">Click one to lock it in and re-run.</div></div>';
     }
   });
   h+='</div>';
 }

 // 3. query built
 h+='<div class="step"><div class="lbl">4 · The query it built</div>';
 h+='<pre><code>'+hl(d.body)+'</code></pre></div>';

 // 4. live API
 h+='<div class="step"><div class="lbl">5 · Live Fundable API</div>';
 const api=d.api||{};
 if(d.needs_clarification&&d.needs_clarification.length){
   h+='<div class="row"><span class="pill warn">waiting on your pick</span><span class="muted">Choose an option above, then it narrows the results.</span></div>';
 }
 if(api.accepted){
   const cnt=(api.total_count!=null)?Number(api.total_count).toLocaleString():'—';
   h+='<div class="row" style="margin-top:8px"><span class="pill ok">✓ API accepted</span><span class="big">'+cnt+'</span><span class="muted">matching results</span></div>';
 }else{
   h+='<div class="row" style="margin-top:8px"><span class="pill warn">not accepted</span><span class="muted">'+esc(api.error||'')+'</span></div>';
 }
 (d.warnings||[]).forEach(w=>{ h+='<div class="esc" style="color:var(--warn)">⚠ '+esc(w)+'</div>'; });
 h+='<div class="faint" style="margin-top:10px">total '+(d.wall_s||'?')+'s · model '+((lg.stages&&(lg.stages.plan_ms||0))/1000).toFixed(1)+'s · route '+esc(d.route)+'</div>';
 h+='</div>';

 // 6. exclusion applied client-side ("everywhere but X")
 const er=d.exclusion_result;
 if(er){
   const lbl=(er.labels||[]).join(', ');
   h+='<div class="step" style="border-color:var(--accent)"><div class="lbl">✦ Exclusion — "not in '+esc(lbl)+'" (applied client-side)</div>';
   h+='<div class="faint" style="margin-bottom:12px">Fundable can\'t exclude server-side, so we compute it: everywhere minus the excluded set (exact count), then drop excluded rows from the page.</div>';
   h+='<div class="row" style="align-items:center;font-family:var(--mono);font-size:15px;flex-wrap:wrap">'+
      '<span class="sc" style="font-size:14px">everywhere <b>'+fmt(er.total)+'</b></span>'+
      '<span style="color:var(--faint)">&minus;</span>'+
      '<span class="sc" style="font-size:14px">in '+esc(lbl)+' <b>'+fmt(er.in_excluded)+'</b></span>'+
      '<span style="color:var(--faint)">=</span>'+
      '<span class="sc" style="font-size:14px;border-color:var(--accent)">not in '+esc(lbl)+' <b>'+fmt(er.net)+'</b></span></div>';
   if(er.kept_sample && er.kept_sample.length){
     h+='<div class="lu" style="margin-top:12px"><span class="t" style="color:var(--ok)">kept</span><span>'+er.kept_sample.map(esc).join(', ')+'</span></div>';
   }
   if(er.removed_sample && er.removed_sample.length){
     h+='<div class="lu"><span class="t" style="color:var(--warn)">removed</span><span class="muted">'+er.removed_sample.map(esc).join(', ')+' <span class="faint">(in '+esc(lbl)+')</span></span></div>';
   }
   h+='<div class="faint" style="margin-top:10px">On the first page, kept '+er.page_kept+' of '+er.page_size+' (the rest were in '+esc(lbl)+').</div>';
   h+='</div>';
 }

 out.innerHTML=h;
 // wire up the clickable clarification options + clear button
 out.querySelectorAll('.pick').forEach(b=>b.onclick=()=>pick(b.dataset.type,b.dataset.query,b.dataset.permalink,b.dataset.label));
 const cp=document.getElementById('clearpicks'); if(cp) cp.onclick=()=>{overrides=[];runWith();};
}
"""


if __name__ == "__main__":
    missing = [k for k in ("OPENROUTER_API_KEY", "FUNDABLE_API_KEY") if not os.environ.get(k)]
    if missing:
        print(f"⚠  missing keys in .env: {', '.join(missing)}")
    port = int(os.environ.get("PORT", "8000"))
    url = f"http://localhost:{port}"
    print(f"\n  Fundable planner tester → {url}\n  (Ctrl-C to stop)\n")
    import threading
    import webbrowser
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()  # pop open the browser
    try:
        ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\n  stopped.")
