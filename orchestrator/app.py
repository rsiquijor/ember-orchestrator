#!/usr/bin/env python3
"""Ember Orchestrator UI shell (Phase 0).

Single page, stdlib only: a command box, Ember's plan presented for
approval, the tool trace, and the spend meter pinned at the top.
Runs on the stub Ember client + fixture Cube tools — $0, no network.

    python3 -m orchestrator.app        # http://127.0.0.1:8320
"""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .cube_tools import ApprovalTokens, FixtureCubeTools
from .ember_client import StubEmberClient
from .orchestrator import Orchestrator
from .spend import SpendMeter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = int(os.environ.get("EMBER_UI_PORT", "8320"))

meter = SpendMeter(state_path=os.path.join(ROOT, "spend", "state.json"))
tokens = ApprovalTokens()
orch = Orchestrator(StubEmberClient(), FixtureCubeTools(tokens), meter, tokens)

PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Ember Orchestrator — Metatron Cube</title>
<style>
 body{font-family:-apple-system,"Segoe UI",Roboto,Arial,sans-serif;margin:0;background:#0f1622;color:#e8eef6}
 .wrap{max-width:860px;margin:0 auto;padding:22px 18px 60px}
 h1{font-size:22px;margin:4px 0} .sub{color:#8fa3ba;font-size:13.5px;line-height:1.5}
 .meter{position:sticky;top:0;background:#16233a;border:1px solid #2a3d5f;border-radius:10px;padding:10px 14px;margin:14px 0;font-size:14px}
 .meter b{font-size:17px}
 .card{background:#16233a;border:1px solid #2a3d5f;border-radius:12px;padding:16px;margin:14px 0}
 .row{display:flex;gap:9px} input{flex:1;background:#0f1622;border:1px solid #2a3d5f;border-radius:9px;color:#e8eef6;font-size:15px;padding:10px 12px}
 button{background:#2456c8;color:#fff;border:none;border-radius:9px;padding:10px 18px;font-size:14.5px;font-weight:600;cursor:pointer}
 button.ghost{background:#22334f} button:disabled{opacity:.5}
 .step{font-family:ui-monospace,Menlo,monospace;font-size:13px;background:#0f1622;border-radius:8px;padding:8px 10px;margin:6px 0;white-space:pre-wrap}
 .trace{font-size:13.5px;color:#b9c9dd;line-height:1.55} .final{white-space:pre-wrap;line-height:1.6}
 .chips{display:flex;gap:8px;flex-wrap:wrap;margin:10px 0}
 .chip{background:#22334f;border:1px solid #2a3d5f;color:#dbe7f5;border-radius:999px;padding:5px 12px;font-size:13px;cursor:pointer}
</style></head><body><div class="wrap">
<h1>Ember Orchestrator <span style="color:#8fa3ba">for Metatron Cube</span></h1>
<p class="sub">Muse Ember proposes — a human approves — deterministic Cube workflows execute.
Phase 0 shell: stub Ember + fixture tools, $0 spend. Live Ember wiring lands with hackathon credits.</p>
<div class="meter" id="meter">Spend meter loading…</div>
<div class="card">
 <div class="row"><input id="cmd" placeholder="Ask for something the Cube can do…">
 <button id="propose">Propose</button></div>
 <div class="chips" id="chips"></div>
</div>
<div id="planHost"></div>
<script>
const $=id=>document.getElementById(id);
const EXAMPLES=["Send the Watcher status to WhatsApp","Draft the shift handover brief","Triage the help-desk tickets","What is the Watcher status?"];
for(const q of EXAMPLES){const c=document.createElement("button");c.className="chip";c.textContent=q;c.onclick=()=>{$("cmd").value=q;propose();};$("chips").appendChild(c);}
async function spend(){const s=await (await fetch("/api/spend")).json();
 $("meter").innerHTML=`Spend: <b>$${s.spent_usd.toFixed(2)}</b> of $10.00 cap · calls ${s.calls} · gates warn $7 / halt $9 / stop $9.75 · rate ${s.rate_configured?"configured":"not set (Phase 1)"}${s.warning?' · <span style="color:#ffb02e">WARNING: over $7</span>':""}`;}
async function propose(){const text=$("cmd").value.trim();if(!text)return;
 const r=await (await fetch("/api/propose",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({text})})).json();
 if(r.error){alert(r.error);return;}
 const host=$("planHost");const card=document.createElement("div");card.className="card";
 card.innerHTML=`<div style="font-weight:700">Plan ${r.id} — awaiting your approval</div>
 <p style="line-height:1.55">${r.rationale}</p>`+
 r.steps.map(s=>`<div class="step">${s.name}(${JSON.stringify(s.arguments)})</div>`).join("")+
 `<div class="row" style="margin-top:10px"><button data-a="ok">Approve &amp; run</button>
 <button class="ghost" data-a="no">Reject</button></div><div class="final" style="margin-top:12px"></div>`;
 card.querySelector('[data-a="ok"]').onclick=async ev=>{ev.target.disabled=true;
   const out=await (await fetch("/api/approve",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({plan_id:r.id})})).json();
   card.querySelector(".final").textContent=out.final_text||out.error||"";
   card.querySelector(".final").insertAdjacentHTML("beforeend","<div class='trace'>"+out.trace.map(t=>"✓ "+t.tool+" → "+JSON.stringify(t.result)).join("<br>")+"</div>");
   spend();};
 card.querySelector('[data-a="no"]').onclick=async ev=>{await fetch("/api/reject",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({plan_id:r.id})});
   card.querySelector(".final").textContent="Plan rejected. Nothing ran.";};
 host.prepend(card);spend();}
$("propose").onclick=propose;
$("cmd").addEventListener("keydown",e=>{if(e.key==="Enter")propose();});
spend();
</script></div></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        if self.path == "/":
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/spend":
            self._json(200, meter.summary())
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        try:
            data = self._body()
            if self.path == "/api/propose":
                plan = orch.propose(str(data.get("text", "")), "experiment")
                self._json(200, {"id": plan["id"], "rationale": plan["rationale"],
                                 "steps": plan["steps"]})
            elif self.path == "/api/approve":
                plan = orch.approve(str(data.get("plan_id", "")))
                self._json(200, {"final_text": plan.get("final_text", ""),
                                 "trace": plan.get("trace", [])})
            elif self.path == "/api/reject":
                orch.reject(str(data.get("plan_id", "")))
                self._json(200, {"ok": True})
            else:
                self._json(404, {"error": "not found"})
        except Exception as e:  # noqa: BLE001
            self._json(400, {"error": str(e)})

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
