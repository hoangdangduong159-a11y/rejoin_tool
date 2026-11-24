"""Zero-dependency responsive Web Control Panel & REST API for Rejoin Tool v4.0."""
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import threading
from urllib.parse import parse_qs, urlparse

from ipc import DEFAULT_IPC_SOCK, ipc_request
from rjcore import (
    CHANGE_CONFIG,
    TMP,
    Store,
    load_change_config,
    load_config,
    mask,
    save_change_config,
    save_config,
    short,
    su,
)
from shared_hub import mirror_outputs_to_download
from sysutils import (
    capture_screenshot,
    get_system_stats,
    optimize_cloud_device,
    send_discord_embed,
    sort_roblox_tabs,
)

WEB_UI_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Rejoin Tool v4.0 Ultra</title>
<style>
:root{--bg:#0b1020;--card:rgba(19,27,46,.88);--line:rgba(148,163,184,.18);--text:#e2e8f0;--muted:#94a3b8;--pri:#38bdf8;--ok:#22c55e;--warn:#f59e0b;--err:#ef4444}
*{box-sizing:border-box;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
body{margin:0;background:radial-gradient(circle at top,#162447,var(--bg) 60%);color:var(--text);padding:14px}
.wrap{max-width:1180px;margin:0 auto;display:grid;gap:12px}
.top{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:center;gap:10px;background:var(--card);border:1px solid var(--line);border-radius:14px;padding:12px 16px}
.title{font-weight:700;font-size:18px;color:var(--pri)}
.metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(145px,1fr));gap:10px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:12px 14px;backdrop-filter:blur(10px)}
.kpi-l{font-size:12px;color:var(--muted)}.kpi-v{font-size:20px;font-weight:700;margin-top:3px}
.bar{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
button{border:1px solid var(--line);background:#1e293b;color:var(--text);padding:7px 11px;border-radius:9px;cursor:pointer;font-size:12px;font-weight:600}
button:hover{border-color:var(--pri);color:#fff}
button.pri{background:#0284c7;border-color:#0ea5e9}button.warn{background:#b45309}button.err{background:#b91c1c}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{padding:8px 6px;border-bottom:1px solid var(--line);text-align:left}
th{color:var(--muted);font-weight:600;font-size:12px}
.badge{padding:2px 8px;border-radius:999px;font-size:11px;font-weight:700;display:inline-block}
.RUNNING{background:rgba(34,197,94,.18);color:#4ade80}.LAUNCHING,.WAITING_COOKIE,.PAUSED_CHALLENGE{background:rgba(245,158,11,.2);color:#fbbf24}
.REJOINING,.ERROR,.RETIRED{background:rgba(239,68,68,.2);color:#f87171}.IDLE,.STOPPED,.ROTATING{background:rgba(56,189,248,.16);color:#7dd3fc}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:12px}
textarea,input{width:100%;background:#0f172a;color:var(--text);border:1px solid var(--line);border-radius:9px;padding:8px 10px;font-size:12px}
.logs{max-height:220px;overflow:auto;font-family:monospace;font-size:11px;background:#070b14;padding:10px;border-radius:10px;border:1px solid var(--line);white-space:pre-wrap}
.row{display:flex;gap:8px;align-items:center}.row>*{flex:1}
.checks{display:grid;grid-template-columns:repeat(auto-fit,minmax(145px,1fr));gap:6px;font-size:12px;margin:8px 0}
label.chk{display:flex;align-items:center;gap:6px;background:#0f172a;padding:6px 8px;border-radius:8px;border:1px solid var(--line)}
label.chk input{width:auto}
#toast{position:fixed;bottom:14px;right:14px;background:#0f172a;border:1px solid var(--pri);padding:8px 14px;border-radius:10px;font-size:12px;display:none}
</style>
</head>
<body>
<div class="wrap">
  <div class="top">
    <div>
      <div class="title">Rejoin Tool v4.0 Ultra</div>
      <div id="sub" style="font-size:12px;color:var(--muted)">Loading telemetry...</div>
    </div>
    <div class="bar">
      <button class="pri" onclick="act('sort_tabs')">Sort Tabs</button>
      <button onclick="act('tiny_tabs')">Tiny Tabs</button>
      <button onclick="act('clean_ram')">Clean RAM</button>
      <button onclick="act('optimize_cloud')">Cloud Tune</button>
      <button onclick="act('sync_download')">Sync Download</button>
      <button onclick="act('reload_config')">Reload</button>
      <button onclick="act('webhook_test')">Test Webhook</button>
      <button onclick="setToken()">Token</button>
    </div>
  </div>

  <div class="metrics">
    <div class="card"><div class="kpi-l">CPU / Load</div><div class="kpi-v" id="k_cpu">-</div></div>
    <div class="card"><div class="kpi-l">RAM Used</div><div class="kpi-v" id="k_ram">-</div></div>
    <div class="card"><div class="kpi-l">Cookie Queue</div><div class="kpi-v" id="k_q">-</div></div>
    <div class="card"><div class="kpi-l">Done / Dead</div><div class="kpi-v" id="k_dd">-</div></div>
    <div class="card"><div class="kpi-l">Disk Free</div><div class="kpi-v" id="k_disk">-</div></div>
  </div>

  <div class="card" style="overflow:auto">
    <table>
      <thead><tr><th>Instance</th><th>Account</th><th>Status</th><th>Mode / HB</th><th>RAM</th><th>Rej / Rot</th><th>Event</th><th>Actions</th></tr></thead>
      <tbody id="inst_rows"></tbody>
    </table>
  </div>

  <div class="grid2">
    <div class="card">
      <div style="font-weight:700;margin-bottom:8px">Cookie Queue & Target Link</div>
      <textarea id="ck_box" rows="3" placeholder="Paste cookie or user:pass:cookie (one per line)"></textarea>
      <div class="bar" style="margin-top:8px">
        <button class="pri" onclick="addCookies(false)">Add Cookies</button>
        <button onclick="addCookies(true)">Add & Prune Dead</button>
      </div>
      <div class="row" style="margin-top:10px">
        <input id="link_box" placeholder="Place ID or VIP/Share link for ALL instances">
        <button class="pri" style="flex:0 0 auto" onclick="setAllLinks()">Set All</button>
      </div>
    </div>

    <div class="card">
      <div style="font-weight:700;margin-bottom:8px">Blox Fruits Auto-Change (config-change.json)</div>
      <div class="row">
        <div><span class="kpi-l">Level</span><input id="bf_level" type="number" min="0"></div>
        <div><span class="kpi-l">Beli</span><input id="bf_beli" type="number" min="0"></div>
        <div><span class="kpi-l">Fragments</span><input id="bf_frag" type="number" min="0"></div>
        <div><span class="kpi-l">Race Ver</span><input id="bf_race" placeholder="0 or V4"></div>
      </div>
      <div class="checks" id="bf_checks"></div>
      <button class="pri" onclick="saveBf()">Save BF Config</button>
    </div>
  </div>

  <div class="card">
    <div style="font-weight:700;margin-bottom:6px">Recent Events</div>
    <div class="logs" id="logs"></div>
  </div>
</div>
<div id="toast"></div>
<script>
(() => {
  const p = new URLSearchParams(location.search);
  const qt = p.get('token');
  if (qt) {
    localStorage.setItem('rj_token', qt.trim());
    p.delete('token');
    const qs = p.toString();
    history.replaceState(null, '', location.pathname + (qs ? '?' + qs : ''));
  }
})();
const BF_BOOLS = ['god_human','sanguine_art','cursed_dual_katana','true_triple_katana','shark_anchor','skull_guitar','mirror_fractal','valkyrie_helm','pull_level'];
let bfInit = false;
function hdrs(json){
  const h = {};
  const tok = localStorage.getItem('rj_token') || '';
  if(tok) h['Authorization'] = 'Bearer ' + tok;
  if(json) h['Content-Type'] = 'application/json';
  return h;
}
function toast(msg){const t=document.getElementById('toast');t.textContent=msg;t.style.display='block';setTimeout(()=>t.style.display='none',2600);}
function setToken(){const v=prompt('Enter API Bearer Token (blank to clear):', localStorage.getItem('rj_token')||''); if(v!==null){localStorage.setItem('rj_token',v.trim()); refresh();}}
async function api(path, opts={}){
  const r = await fetch(path, opts);
  if(r.status===401){toast('Unauthorized: click Token button'); throw new Error('401');}
  return await r.json();
}
async function act(action, instance){
  const d = await api('/api/action',{method:'POST',headers:hdrs(true),body:JSON.stringify({action,instance})});
  toast(d.ok ? ('OK: '+action) : ('Err: '+d.error));
  refresh();
}
async function addCookies(prune){
  const text = document.getElementById('ck_box').value;
  const d = await api('/api/cookies',{method:'POST',headers:hdrs(true),body:JSON.stringify({text,prune})});
  if(d.ok){document.getElementById('ck_box').value=''; toast('Added '+d.added+' cookie(s)'); refresh();}
}
async function setAllLinks(){
  const target = document.getElementById('link_box').value.trim();
  const d = await api('/api/links',{method:'POST',headers:hdrs(true),body:JSON.stringify({instance:'all',target})});
  toast(d.ok ? 'Updated all targets' : d.error); refresh();
}
async function saveBf(){
  const payload = {
    level: Number(document.getElementById('bf_level').value||0),
    beli: Number(document.getElementById('bf_beli').value||0),
    fragment: Number(document.getElementById('bf_frag').value||0),
    race_ver: document.getElementById('bf_race').value.trim()
  };
  for(const k of BF_BOOLS){const el=document.getElementById('chk_'+k); if(el) payload[k]=el.checked;}
  const d = await api('/api/change-config',{method:'POST',headers:hdrs(true),body:JSON.stringify(payload)});
  toast(d.ok ? 'Saved config-change.json' : d.error); refresh();
}
async function refresh(){
  try{
    const d = await api('/api/status',{headers:hdrs(false)});
    if(!d.ok) return;
    const s = d.system || {}, c = d.counts || {};
    document.getElementById('sub').textContent = (d.daemon_alive?'Live Monitor':'Idle / Local') + ' • Switched: ' + Object.entries(d.switched||{}).filter(([,v])=>v>0).map(([k,v])=>k+':'+v).join(' ');
    document.getElementById('k_cpu').textContent = `${Math.round(s.cpu_pct||0)}% (${s.load_1m||0})`;
    document.getElementById('k_ram').textContent = `${Math.round(s.ram_used_mb||0)}/${Math.round(s.ram_total_mb||0)}MB`;
    document.getElementById('k_q').textContent = c.queue ?? 0;
    document.getElementById('k_dd').textContent = `${c.done??0} / ${c.dead??0}`;
    document.getElementById('k_disk').textContent = `${s.disk_free_gb||0} GB`;
    const tb = document.getElementById('inst_rows');
    tb.innerHTML = '';
    for(const r of (d.instances||[])){
      const tr = document.createElement('tr');
      const hb = r.check_method==='online' ? `online(${r.presence_strikes||0})` : (r.hb_age!=null ? Math.round(r.hb_age)+'s' : '-');
      const errTxt = r.last_error_code ? ` [E${r.last_error_code}]` : '';
      tr.innerHTML = `<td><b>${r.name}</b><br><span style="font-size:11px;color:var(--muted)">${r.package}</span></td>
        <td>${r.username||'-'}</td>
        <td><span class="badge ${r.status}">${r.status}</span></td>
        <td>${r.check_method||'-'} / ${hb}</td>
        <td>${r.ram_mb!=null?Math.round(r.ram_mb)+'MB':'-'}</td>
        <td>${r.rejoin_count||0} / ${r.rotate_count||0}</td>
        <td style="font-size:11px;color:var(--muted)">${(r.last_event||'-')+errTxt}</td>
        <td><div class="bar">
          <button onclick="act('rejoin_instance','${r.name}')">Rejoin</button>
          <button class="warn" onclick="act('rotate_instance','${r.name}')">Rotate</button>
          <button class="err" onclick="act('stop_instance','${r.name}')">Stop</button>
        </div></td>`;
      tb.appendChild(tr);
    }
    if(!bfInit && d.change_config){
      bfInit = true;
      const cc = d.change_config;
      document.getElementById('bf_level').value = cc.level||0;
      document.getElementById('bf_beli').value = cc.beli||0;
      document.getElementById('bf_frag').value = cc.fragment||0;
      document.getElementById('bf_race').value = cc.race_ver??'';
      const box = document.getElementById('bf_checks');
      box.innerHTML = BF_BOOLS.map(k=>`<label class="chk"><input type="checkbox" id="chk_${k}" ${cc[k]?'checked':''}>${k}</label>`).join('');
    }
    document.getElementById('logs').textContent = (d.logs||[]).join('\n') || 'Waiting for logs...';
  }catch(e){}
}
refresh(); setInterval(refresh, 3000);
</script>
</body>
</html>
"""


def _sanitize_instances(instances):
    safe = []
    for row in instances or []:
        d = dict(row)
        if "cookie" in d:
            d["cookie"] = short(d["cookie"])
        safe.append(d)
    return safe


class WebPanelServer:
    """Thread-safe local/LAN HTTP dashboard & REST API for Rejoin Tool."""

    def __init__(
        self,
        host="127.0.0.1",
        port=8787,
        token="",
        manager=None,
        store=None,
        change_config_path=None,
        ipc_sock_path=DEFAULT_IPC_SOCK,
    ):
        self.host = host or "127.0.0.1"
        self.port = int(port)
        self.token = str(token or "")
        self.manager = manager
        self.store = store or (manager.store if manager else Store())
        self.change_config_path = change_config_path or (manager.change_config_path if manager else CHANGE_CONFIG)
        self.ipc_sock_path = ipc_sock_path
        self._httpd = None
        self._thread = None

    @property
    def url(self):
        if self._httpd:
            h, p = self._httpd.server_address[:2]
            disp_host = "127.0.0.1" if h == "0.0.0.0" else h
            return f"http://{disp_host}:{p}"
        return f"http://{self.host}:{self.port}"

    def _is_authorized(self, handler):
        if not self.token:
            return True
        auth = handler.headers.get("Authorization", "")
        candidate = ""
        if auth.lower().startswith("bearer "):
            candidate = auth[7:].strip()
        if not candidate:
            candidate = handler.headers.get("X-API-Token", "").strip()
        if not candidate:
            qs = parse_qs(urlparse(handler.path).query)
            candidate = (qs.get("token") or [""])[0].strip()
        return bool(candidate) and hmac.compare_digest(candidate, self.token)

    def _collect_status(self):
        cfg = self.manager.config if self.manager else load_config()
        if self.manager:
            return {
                "ok": True,
                "daemon_alive": True,
                "instances": _sanitize_instances(self.manager.snapshot()),
                "counts": self.store.counts(),
                "switched": self.store.switched_counts(),
                "system": get_system_stats(),
                "logs": [mask(l) for l in self.manager._recent_logs(40)],
                "change_config": load_change_config(self.change_config_path),
                "config_summary": {
                    "device_name": cfg.get("device_name", ""),
                    "download_hub_sync": bool(cfg.get("download_hub_sync", True)),
                    "server_hop_on_disconnect": bool(cfg.get("server_hop_on_disconnect", False)),
                    "error_264_action": cfg.get("error_264_action", "cooldown"),
                },
            }

        res = ipc_request("status", sock_path=self.ipc_sock_path, timeout=1.5)
        if res.get("ok"):
            r = res["result"]
            return {
                "ok": True,
                "daemon_alive": True,
                "instances": _sanitize_instances(r.get("instances") or []),
                "counts": r.get("counts") or self.store.counts(),
                "switched": r.get("switched") or self.store.switched_counts(),
                "system": r.get("system") or get_system_stats(),
                "logs": [mask(l) for l in (r.get("logs") or [])],
                "change_config": load_change_config(self.change_config_path),
                "config_summary": {
                    "device_name": cfg.get("device_name", ""),
                    "download_hub_sync": bool(cfg.get("download_hub_sync", True)),
                    "server_hop_on_disconnect": bool(cfg.get("server_hop_on_disconnect", False)),
                    "error_264_action": cfg.get("error_264_action", "cooldown"),
                },
            }

        assigned = self.store.all_assigned()
        inst_rows = []
        for i in cfg.get("instances") or []:
            a = assigned.get(i["name"]) or {}
            inst_rows.append({
                "name": i["name"],
                "package": i.get("package", ""),
                "profile_name": i.get("profile", "default"),
                "username": a.get("username", "-"),
                "status": "IDLE" if i.get("enabled", True) else "STOPPED",
                "check_method": "-",
                "hb_age": None,
                "ram_mb": None,
                "rejoin_count": 0,
                "rotate_count": 0,
                "last_event": (i.get("target") or "-")[:32],
                "last_error_code": None,
            })
        return {
            "ok": True,
            "daemon_alive": False,
            "instances": inst_rows,
            "counts": self.store.counts(),
            "switched": self.store.switched_counts(),
            "system": get_system_stats(),
            "logs": [],
            "change_config": load_change_config(self.change_config_path),
            "config_summary": {
                "device_name": cfg.get("device_name", ""),
                "download_hub_sync": bool(cfg.get("download_hub_sync", True)),
                "server_hop_on_disconnect": bool(cfg.get("server_hop_on_disconnect", False)),
                "error_264_action": cfg.get("error_264_action", "cooldown"),
            },
        }

    def _handle_action(self, body):
        action = str(body.get("action") or "").strip()
        inst = str(body.get("instance") or "").strip()
        cfg = self.manager.config if self.manager else load_config()
        pkgs = [i.get("package") for i in cfg.get("instances") or [] if i.get("package")]

        if action == "rejoin_instance":
            if self.manager:
                ok = self.manager.force_rejoin(inst or "all", reason="web_rejoin")
                return {"ok": ok, "action": action, "instance": inst}
            return ipc_request("rejoin", {"instance": inst or "all", "reason": "web_rejoin"}, sock_path=self.ipc_sock_path)

        if action == "rotate_instance":
            if self.manager:
                ok = self.manager.force_rotate(inst, reason="web_rotate")
                return {"ok": ok, "action": action, "instance": inst}
            return ipc_request("rotate", {"instance": inst, "reason": "web_rotate"}, sock_path=self.ipc_sock_path)

        if action == "stop_instance":
            target_pkg = next((i.get("package") for i in cfg.get("instances", []) if i.get("name") == inst), None)
            if not target_pkg:
                return {"ok": False, "error": "instance_not_found"}
            from rjcore import force_stop
            force_stop(target_pkg)
            return {"ok": True, "stopped": target_pkg}

        if action in ("sort_tabs", "tiny_tabs"):
            tiny = action == "tiny_tabs"
            if self.manager:
                resized = sort_roblox_tabs([w.state.package for w in self.manager.workers.values()], tiny=tiny)
                return {"ok": True, "resized": resized}
            res = ipc_request("sort_tabs", {"tiny": tiny}, sock_path=self.ipc_sock_path)
            if res.get("ok"):
                return res
            return {"ok": True, "resized": sort_roblox_tabs(pkgs, tiny=tiny)}

        if action == "clean_ram":
            rc, _, _ = su("am kill-all", 15)
            return {"ok": rc == 0}

        if action == "optimize_cloud":
            return {"ok": True, "result": optimize_cloud_device(pkgs)}

        if action == "reload_config":
            if self.manager:
                return {"ok": True, "result": self.manager.reload_config()}
            return ipc_request("reload_config", sock_path=self.ipc_sock_path)

        if action == "sync_download":
            hub_dir = cfg.get("download_hub_dir") or "/storage/emulated/0/Download/RejoinTool"
            n = mirror_outputs_to_download(self.store, self.change_config_path, hub_dir=hub_dir)
            return {"ok": True, "mirrored": n}

        if action == "webhook_test":
            wh = (cfg.get("webhook") or "").strip()
            if not wh:
                return {"ok": False, "error": "webhook_not_configured"}
            ok = send_discord_embed(wh, "Web Panel Webhook Test", "Triggered from Rejoin Tool v4.0 Web Panel.", color=0x57F287)
            return {"ok": ok}

        return {"ok": False, "error": f"unknown_action:{action}"}

    def start(self):
        panel = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _fmt, *_args):
                return

            def _send_json(self, code, obj):
                data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)

            def _read_json(self):
                try:
                    length = min(262144, max(0, int(self.headers.get("Content-Length") or 0)))
                    raw = self.rfile.read(length) if length else b"{}"
                    obj = json.loads(raw.decode("utf-8"))
                    return obj if isinstance(obj, dict) else {}
                except Exception:
                    return {}

            def do_GET(self):
                parsed = urlparse(self.path)
                path = parsed.path
                if path == "/":
                    body = WEB_UI_HTML.encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return

                if not panel._is_authorized(self):
                    self._send_json(401, {"ok": False, "error": "unauthorized"})
                    return

                if path == "/api/status":
                    self._send_json(200, panel._collect_status())
                    return

                if path == "/api/switched":
                    sw_data = {}
                    if os.path.isdir(panel.store.switched_dir):
                        for fname in sorted(os.listdir(panel.store.switched_dir)):
                            if fname.endswith(".txt"):
                                p = os.path.join(panel.store.switched_dir, fname)
                                with open(p, encoding="utf-8", errors="ignore") as f:
                                    rows = [mask(l.strip()) for l in f if l.strip()]
                                sw_data[fname[:-4]] = rows[-50:]
                    self._send_json(200, {"ok": True, "counts": panel.store.switched_counts(), "items": sw_data})
                    return

                if path == "/api/screenshot.png":
                    cand = os.path.join(TMP, f"web_screen_{threading.get_ident()}.png")
                    try:
                        if not capture_screenshot(cand):
                            self._send_json(503, {"ok": False, "error": "screenshot_unavailable"})
                            return
                        with open(cand, "rb") as f:
                            img = f.read()
                        self.send_response(200)
                        self.send_header("Content-Type", "image/png")
                        self.send_header("Content-Length", str(len(img)))
                        self.end_headers()
                        self.wfile.write(img)
                    finally:
                        if os.path.exists(cand):
                            try:
                                os.remove(cand)
                            except OSError:
                                pass
                    return

                self._send_json(404, {"ok": False, "error": "not_found"})

            def do_POST(self):
                parsed = urlparse(self.path)
                path = parsed.path
                if not panel._is_authorized(self):
                    self._send_json(401, {"ok": False, "error": "unauthorized"})
                    return
                body = self._read_json()

                if path == "/api/action":
                    res = panel._handle_action(body)
                    self._send_json(200 if res.get("ok") else 400, res)
                    return

                if path == "/api/cookies":
                    text = str(body.get("text") or "")
                    added = panel.store.add_cookies(text)
                    pruned = None
                    if body.get("prune"):
                        pruned = panel.store.prune_invalid_cookies()
                    self._send_json(200, {"ok": True, "added": added, "pruned": pruned, "counts": panel.store.counts()})
                    return

                if path == "/api/links":
                    inst = str(body.get("instance") or "all").strip()
                    target = str(body.get("target") or "").strip()
                    cfg = load_config()
                    updated = 0
                    for row in cfg.get("instances") or []:
                        if inst == "all" or row.get("name") == inst or row.get("package") == inst:
                            row["target"] = target
                            updated += 1
                    save_config(cfg)
                    if panel.manager:
                        panel.manager.reload_config()
                    else:
                        ipc_request("reload_config", sock_path=panel.ipc_sock_path, timeout=1.5)
                    self._send_json(200, {"ok": True, "updated": updated})
                    return

                if path == "/api/change-config":
                    cur = load_change_config(panel.change_config_path)
                    cur.update(body)
                    saved = save_change_config(cur, path=panel.change_config_path)
                    if panel.manager:
                        panel.manager.reload_config()
                    else:
                        ipc_request("reload_config", sock_path=panel.ipc_sock_path, timeout=1.5)
                    self._send_json(200, {"ok": True, "change_config": saved})
                    return

                self._send_json(404, {"ok": False, "error": "not_found"})

        ThreadingHTTPServer.allow_reuse_address = True
        self._httpd = ThreadingHTTPServer((self.host, self.port), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True, name="rejoin-web")
        self._thread.start()
        return self.url

    def stop(self):
        if self._httpd:
            try:
                self._httpd.shutdown()
                self._httpd.server_close()
            except Exception:
                pass
            self._httpd = None
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
            self._thread = None
