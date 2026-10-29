#!/usr/bin/env python3
"""Serve a localhost dashboard for Continuous Codex project state."""

from __future__ import annotations

import argparse
import json
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


STATUS = Path(__file__).with_name("codex_continuous_status.py")

HTML = """<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Continuous Codex</title>
<style>
:root{color-scheme:light;font:14px/1.45 system-ui,sans-serif;color:#172124;background:#f4f7f6}
*{box-sizing:border-box}body{max-width:1120px;margin:0 auto;padding:28px 24px 48px}
header{display:flex;justify-content:space-between;align-items:flex-start;gap:20px;border-bottom:1px solid #cbd5d2;padding-bottom:18px}
h1{font-size:22px;margin:0 0 5px;font-weight:650}#project{color:#53615f;overflow-wrap:anywhere}
#updated{color:#53615f;white-space:nowrap}.metrics{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));border-bottom:1px solid #cbd5d2;margin-bottom:24px}
.metric{padding:18px 16px 18px 0;border-right:1px solid #d8dfdd}.metric+.metric{padding-left:18px}
.metric:last-child{border:0}.label{color:#53615f;font-size:12px}.value{font-size:21px;font-weight:650;margin-top:4px;overflow-wrap:anywhere}
main{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:28px 40px}
section{min-width:0}h2{font-size:15px;margin:0 0 12px;padding-bottom:8px;border-bottom:1px solid #d8dfdd}
.rows{display:grid;grid-template-columns:minmax(100px,1fr) minmax(0,1.5fr);gap:9px 16px}.key{color:#53615f}.value-small{overflow-wrap:anywhere}
.state{display:inline-flex;align-items:center;gap:7px}.dot{width:8px;height:8px;border-radius:50%;background:#798582;flex:0 0 auto}.ok .dot{background:#16836a}.bad .dot{background:#c84e44}.warn .dot{background:#bd780e}
#handoff{overflow-wrap:anywhere}#changes{margin:0;padding:0;list-style:none;max-height:260px;overflow:auto}#changes li{padding:6px 0;border-bottom:1px solid #e0e6e4;font:12px/1.4 ui-monospace,monospace;overflow-wrap:anywhere}
#changes .empty{color:#53615f;font:14px system-ui,sans-serif;border:0}.error{color:#a52f29}
@media(max-width:700px){body{padding:20px 16px 36px}header{display:block}#updated{display:block;margin-top:8px}.metrics{grid-template-columns:repeat(2,minmax(0,1fr))}.metric:nth-child(2){border-right:0}.metric:nth-child(3){padding-left:0}.metric:nth-child(3),.metric:nth-child(4){border-top:1px solid #d8dfdd}main{grid-template-columns:1fr;gap:22px}.rows{grid-template-columns:minmax(90px,1fr) minmax(0,1.7fr)}}
</style>
<body><header><div><h1>Continuous Codex</h1><div id="project">Loading project</div></div><div id="updated">Connecting</div></header>
<div class="metrics" id="metrics"></div><main>
<section><h2>Runtime</h2><div class="rows" id="runtime"></div></section>
<section><h2>Codex resources</h2><div class="rows" id="resources"></div></section>
<section><h2>Latest handoff</h2><div id="handoff">None found</div></section>
<section><h2>Working tree</h2><ul id="changes"><li class="empty">Loading</li></ul></section>
</main>
<script>
const query = new URLSearchParams(location.search);
const project = query.get('project') || '';
const deployment = query.get('deployment') || '';
const codexHome = query.get('codex-home') || '';
function node(tag, cls, value){const el=document.createElement(tag);if(cls)el.className=cls;if(value!==undefined)el.textContent=value;return el}
function row(parent,key,value,state){parent.append(node('div','key',key));const val=node('div','value-small',value);if(state){val.className='state '+state;val.prepend(node('span','dot'));}parent.append(val)}
function metric(parent,label,value){const item=node('div','metric');item.append(node('div','label',label),node('div','value',String(value)));parent.append(item)}
async function load(){
  const params=new URLSearchParams();if(project)params.set('project',project);if(deployment)params.set('deployment',deployment);if(codexHome)params.set('codex-home',codexHome);
  try{
    const response=await fetch('/api/status?'+params.toString(),{cache:'no-store'});if(!response.ok)throw new Error('Status command failed');
    const data=await response.json();document.querySelector('#project').textContent=data.project;
    const metrics=document.querySelector('#metrics');metrics.replaceChildren();
    metric(metrics,'Codex hooks',data.hooks_registered);metric(metrics,'Installed skills',data.skills_installed);metric(metrics,'Memory records',data.memory.available?((data.memory.archival_memory||0)+(data.memory.core_memory||0)):'Unavailable');metric(metrics,'Git changes',data.git_status.length);
    const runtime=document.querySelector('#runtime');runtime.replaceChildren();
    row(runtime,'Codex',data.codex_version,data.codex_version==='not installed'?'bad':'ok');
    row(runtime,'PostgreSQL',data.docker_status,data.docker_status==='not running'?'warn':(data.docker_status==='not installed'?'bad':'ok'));
    row(runtime,'Memory',data.memory.available?'Connected':(data.memory.reason||'Unavailable'),data.memory.available?'ok':'warn');
    row(runtime,'TLDR',data.tldr_available?data.tldr_version:'Not installed',data.tldr_available?'ok':'warn');
    row(runtime,'TLDR semantic',data.tldr_semantic_enabled?'Enabled':'Lexical fallback',data.tldr_semantic_enabled?'ok':'warn');
    row(runtime,'RLM',data.rlm_available?(data.rlm_version+' / '+data.rlm_skills_installed+' skills'):'Not installed',data.rlm_available?'ok':'warn');
    row(runtime,'Braintrust tracing',data.braintrust_enabled?'Enabled':'Disabled',data.braintrust_enabled?'warn':'ok');
    row(runtime,'Subagents',data.subagents_enabled?'Enabled':'Disabled',data.subagents_enabled?'warn':'ok');
    const resources=document.querySelector('#resources');resources.replaceChildren();
    row(resources,'MCP servers',data.mcp_configured+' configured / '+data.mcp_available+' available');
    row(resources,'Agent roles',data.agent_roles_registered+' registered / '+data.agent_roles_available+' available');
    row(resources,'Codex rules',data.global_rules_installed?'Installed':'Not installed',data.global_rules_installed?'ok':'warn');
    row(resources,'Skill conflicts',String(data.skill_collisions),data.skill_collisions?'warn':'ok');
    row(resources,'Deployment',data.deployment);
    const handoff=document.querySelector('#handoff');handoff.replaceChildren();
    if(data.latest_handoff){handoff.append(node('div','value-small',data.latest_handoff.name),node('div','key',data.latest_handoff.mtime),node('div','value-small',data.latest_handoff.path));}else{handoff.textContent='None found';}
    const changes=document.querySelector('#changes');changes.replaceChildren();
    if(data.git_status.length){for(const item of data.git_status.slice(0,100))changes.append(node('li','',item));}else{changes.append(node('li','empty','Working tree clean'));}
    document.querySelector('#updated').textContent='Updated '+new Date().toLocaleTimeString();
  }catch(error){document.querySelector('#updated').textContent='Update failed';document.querySelector('#handoff').textContent=error.message;document.querySelector('#handoff').className='error';}
}
load();setInterval(load,5000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    project = Path.cwd()
    deployment = Path.home() / ".codex" / "continuous-claude-v3"
    codex_home = Path.home() / ".codex"

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/api/status":
            command = [
                "python3",
                str(STATUS),
                "--project",
                str(self.project),
                "--deployment",
                str(self.deployment),
                "--codex-home",
                str(self.codex_home),
                "--json",
            ]
            try:
                result = subprocess.run(command, text=True, capture_output=True, check=False, timeout=10)
                body = (result.stdout if result.returncode == 0 else json.dumps({"error": "status command failed"})).encode("utf-8")
                status = 200 if result.returncode == 0 else 503
            except (OSError, subprocess.TimeoutExpired):
                body, status = b'{"error":"status command timed out"}', 503
            content_type = "application/json; charset=utf-8"
        elif path == "/" or path == "/index.html":
            body, status = HTML.encode("utf-8"), 200
            content_type = "text/html; charset=utf-8"
        else:
            body, status = b"Not found", 404
            content_type = "text/plain; charset=utf-8"
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_: object) -> None:
        return


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--project", type=Path, default=Path.cwd())
    parser.add_argument("--deployment", type=Path, default=Path.home() / ".codex" / "continuous-claude-v3")
    parser.add_argument("--codex-home", type=Path, default=Path.home() / ".codex")
    args = parser.parse_args()
    Handler.project = args.project.expanduser().resolve()
    Handler.deployment = args.deployment.expanduser().resolve()
    Handler.codex_home = args.codex_home.expanduser().resolve()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Continuous Codex dashboard: http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
