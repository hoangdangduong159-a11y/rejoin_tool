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
<html lang="vi">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Bảng Điều Khiển — Rejoin Tool v4.0 Ultra</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600;700&family=Plus+Jakarta+Sans:wght@400;500;600;700&family=Space+Grotesk:wght@600;700&display=swap" rel="stylesheet">
<style>
:root{
  --bg:#060913;
  --surface:rgba(13,20,36,0.88);
  --surface-2:rgba(9,14,27,0.94);
  --line:rgba(148,163,184,0.16);
  --line-strong:rgba(34,211,238,0.34);
  --text:#e2e8f0;
  --muted:#8b9bb4;
  --cyan:#22d3ee;
  --emerald:#10b981;
  --amber:#f59e0b;
  --rose:#f43f5e;
  --indigo:#6366f1;
}
*{box-sizing:border-box}
html,body{margin:0;padding:0;min-height:100%}
body{
  background:
    radial-gradient(900px 420px at 10% -10%, rgba(34,211,238,0.13), transparent 60%),
    radial-gradient(760px 380px at 92% 0%, rgba(99,102,241,0.12), transparent 55%),
    radial-gradient(circle, rgba(148,163,184,0.08) 1px, transparent 1px),
    var(--bg);
  background-size:auto, auto, 24px 24px, auto;
  color:var(--text);
  font-family:'Plus Jakarta Sans',system-ui,-apple-system,sans-serif;
  padding:14px;
  line-height:1.45;
}
.mono,.kpi-v,.num{font-family:'JetBrains Mono',ui-monospace,monospace;font-variant-numeric:tabular-nums}
.wrap{max-width:1240px;margin:0 auto;display:grid;gap:12px}
.card{
  background:var(--surface);
  border:1px solid var(--line);
  border-radius:14px;
  padding:14px 16px;
  backdrop-filter:blur(12px);
  box-shadow:0 12px 30px rgba(2,6,23,0.45);
}
.topbar{
  display:flex;flex-wrap:wrap;justify-content:space-between;align-items:center;gap:12px;
  border-color:rgba(34,211,238,0.24);
  background:linear-gradient(135deg, rgba(13,22,42,0.95), rgba(9,15,30,0.92));
}
.brand{display:flex;align-items:center;gap:12px}
.logo{
  width:40px;height:40px;border-radius:11px;
  display:grid;place-items:center;
  background:linear-gradient(135deg, rgba(34,211,238,0.22), rgba(99,102,241,0.25));
  border:1px solid var(--line-strong);
  color:var(--cyan);font-family:'Space Grotesk',sans-serif;font-weight:700;font-size:17px;
  box-shadow:0 0 18px rgba(34,211,238,0.18);
}
.title{
  font-family:'Space Grotesk',sans-serif;font-weight:700;font-size:18px;letter-spacing:-0.01em;
  display:flex;align-items:center;gap:8px;flex-wrap:wrap;
}
.pill-live{
  display:inline-flex;align-items:center;gap:6px;padding:3px 9px;border-radius:999px;
  font-size:11px;font-weight:600;border:1px solid var(--line);background:rgba(15,23,42,0.8);color:var(--muted);
}
.pill-live.on{border-color:rgba(16,185,129,0.45);background:rgba(16,185,129,0.12);color:#34d399}
.dot{width:7px;height:7px;border-radius:50%;background:currentColor}
.pill-live.on .dot{box-shadow:0 0 8px #10b981;animation:pulse 1.8s infinite}
@keyframes pulse{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.45;transform:scale(.85)}}
.sub{font-size:12px;color:var(--muted);margin-top:2px}
.bar{display:flex;flex-wrap:wrap;gap:7px;align-items:center}
button{
  border:1px solid var(--line);background:#111b30;color:var(--text);
  padding:7px 12px;border-radius:9px;cursor:pointer;font-size:12px;font-weight:600;
  transition:all .16s ease;font-family:inherit;display:inline-flex;align-items:center;gap:5px;
}
button:hover{border-color:var(--cyan);color:#fff;transform:translateY(-1px)}
button:active{transform:translateY(0)}
button.pri{background:linear-gradient(135deg,#0891b2,#0284c7);border-color:rgba(34,211,238,0.55);color:#fff}
button.ok{background:rgba(16,185,129,0.18);border-color:rgba(16,185,129,0.45);color:#34d399}
button.warn{background:rgba(245,158,11,0.18);border-color:rgba(245,158,11,0.45);color:#fbbf24}
button.err{background:rgba(244,63,94,0.18);border-color:rgba(244,63,94,0.45);color:#fb7185}
button.chip{background:#0b1325;color:var(--muted);padding:6px 10px}
button.chip.active{background:rgba(34,211,238,0.16);border-color:var(--cyan);color:#67e8f9;box-shadow:0 0 12px rgba(34,211,238,0.15)}

.arranger{
  display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:10px;
  padding:11px 15px;border-color:rgba(34,211,238,0.2);
}
.arr-title{font-size:12px;font-weight:700;color:#cbd5e1;display:flex;align-items:center;gap:8px;flex-wrap:wrap}

.metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(175px,1fr));gap:10px}
.kpi{position:relative;overflow:hidden}
.kpi-l{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em;font-weight:600}
.kpi-v{font-size:21px;font-weight:700;margin-top:4px;color:#f8fafc}
.kpi-sub{font-size:11px;color:var(--muted);margin-top:3px}
.meter{height:5px;background:rgba(148,163,184,0.14);border-radius:999px;margin-top:8px;overflow:hidden}
.meter>span{display:block;height:100%;border-radius:999px;background:linear-gradient(90deg,var(--cyan),var(--indigo));transition:width .35s ease}
.meter.ok>span{background:linear-gradient(90deg,#10b981,#34d399)}
.meter.warn>span{background:linear-gradient(90deg,#f59e0b,#fbbf24)}
.meter.err>span{background:linear-gradient(90deg,#f43f5e,#fb7185)}

.sec-hdr{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:center;gap:8px;margin-bottom:10px}
.sec-t{font-family:'Space Grotesk',sans-serif;font-weight:700;font-size:15px;color:#f1f5f9;display:flex;align-items:center;gap:8px}

table{width:100%;border-collapse:collapse;font-size:12.5px}
th,td{padding:10px 8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:middle}
th{color:var(--muted);font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.04em}
tbody tr:hover{background:rgba(34,211,238,0.04)}

.badge{padding:3px 9px;border-radius:999px;font-size:11px;font-weight:700;display:inline-flex;align-items:center;gap:5px;white-space:nowrap}
.badge::before{content:"";width:6px;height:6px;border-radius:50%;background:currentColor}
.RUNNING{background:rgba(16,185,129,0.16);color:#34d399;border:1px solid rgba(16,185,129,0.35)}
.LAUNCHING,.WAITING_COOKIE,.PAUSED_CHALLENGE{background:rgba(245,158,11,0.16);color:#fbbf24;border:1px solid rgba(245,158,11,0.35)}
.REJOINING,.ERROR,.RETIRED{background:rgba(244,63,94,0.16);color:#fb7185;border:1px solid rgba(244,63,94,0.35)}
.IDLE,.STOPPED,.ROTATING{background:rgba(34,211,238,0.14);color:#67e8f9;border:1px solid rgba(34,211,238,0.3)}

.ram-cell{min-width:82px}
.ram-bar{height:4px;background:rgba(148,163,184,0.16);border-radius:999px;margin-top:4px;overflow:hidden}
.ram-bar>i{display:block;height:100%;background:var(--cyan)}

.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:12px}
textarea,input,select{
  width:100%;background:var(--surface-2);color:var(--text);
  border:1px solid var(--line);border-radius:9px;padding:8px 10px;font-size:12px;font-family:inherit;
}
textarea:focus,input:focus,select:focus{outline:none;border-color:var(--cyan);box-shadow:0 0 0 2px rgba(34,211,238,0.14)}
.row{display:flex;gap:8px;align-items:center}.row>*{flex:1}
.checks{display:grid;grid-template-columns:repeat(auto-fit,minmax(145px,1fr));gap:6px;font-size:12px;margin:10px 0}
label.chk{
  display:flex;align-items:center;gap:7px;background:var(--surface-2);
  padding:7px 9px;border-radius:9px;border:1px solid var(--line);cursor:pointer;transition:border-color .15s;
}
label.chk:hover{border-color:rgba(34,211,238,0.45)}
label.chk input{width:auto;accent-color:var(--cyan)}

.logs{
  max-height:250px;overflow:auto;font-family:'JetBrains Mono',monospace;font-size:11.5px;
  background:#04070f;padding:10px 12px;border-radius:10px;border:1px solid var(--line);line-height:1.5;
}
.log-line{padding:1px 0;word-break:break-word}
.log-ok{color:#34d399}.log-warn{color:#fbbf24}.log-err{color:#fb7185}.log-sys{color:#38bdf8}.log-dim{color:#94a3b8}

.screen-box{display:none}
.screen-box.open{display:block}
.screen-img{max-width:100%;max-height:460px;border-radius:10px;border:1px solid var(--line);display:block;margin:8px auto 0;background:#03050b}

#toast{
  position:fixed;bottom:16px;right:16px;z-index:99;
  background:#091122;border:1px solid var(--cyan);color:#f8fafc;
  padding:9px 15px;border-radius:10px;font-size:12px;font-weight:600;
  box-shadow:0 10px 26px rgba(0,0,0,0.55);display:none;
}

@media (max-width:760px){
  body{padding:10px}
  .metrics{grid-template-columns:repeat(2,1fr)}
  table,thead,tbody,th,td,tr{display:block}
  thead{display:none}
  tbody tr{
    background:var(--surface-2);border:1px solid var(--line);border-radius:12px;
    padding:10px;margin-bottom:9px;
  }
  tbody td{
    display:flex;justify-content:space-between;align-items:center;gap:8px;
    padding:5px 2px;border-bottom:1px dashed rgba(148,163,184,0.12);text-align:right;
  }
  tbody td:last-child{border-bottom:none;justify-content:flex-end;padding-top:8px}
  tbody td::before{content:attr(data-label);color:var(--muted);font-size:11px;font-weight:600;text-align:left}
}
</style>
</head>
<body>
<div class="wrap">
  <div class="card topbar">
    <div class="brand">
      <div class="logo">RJ</div>
      <div>
        <div class="title">
          <span>Bảng Điều Khiển Auto Rejoin Roblox</span>
          <span class="pill-live" id="live_pill"><span class="dot"></span><span id="live_txt">Đang kết nối</span></span>
        </div>
        <div class="sub" id="sub">Đang tải thông tin hệ thống...</div>
      </div>
    </div>
    <div class="bar">
      <button class="pri" onclick="sortNow()">Xếp Trái → Phải</button>
      <button onclick="toggleScreen()">Xem Màn Hình</button>
      <button onclick="act('clean_ram')">Dọn RAM</button>
      <button onclick="act('optimize_cloud')">Tối ưu Cloud</button>
      <button onclick="act('sync_download')">Đồng bộ Download</button>
      <button onclick="act('webhook_test')">Test Discord</button>
      <button onclick="setToken()">Mật khẩu</button>
    </div>
  </div>

  <div class="card arranger">
    <div class="arr-title">
      <span>Sắp xếp cửa sổ Clone (Trái → Phải)</span>
      <span class="pill-live on" id="auto_sort_badge"><span class="dot"></span><span id="auto_sort_txt">Tự động: BẬT (Mặc định)</span></span>
    </div>
    <div class="bar">
      <button class="chip" id="mode_left_to_right" onclick="setLayoutMode('left_to_right')">Trái → Phải (Mặc định)</button>
      <button class="chip" id="mode_horizontal_strip" onclick="setLayoutMode('horizontal_strip')">Dải ngang 1 hàng</button>
      <button class="chip" id="mode_grid" onclick="setLayoutMode('grid')">Chia lưới đều</button>
      <button class="chip" id="mode_tiny" onclick="setLayoutMode('tiny')">Ô thu nhỏ</button>
      <button id="btn_toggle_autosort" onclick="toggleAutoSort()">Tắt tự động</button>
    </div>
  </div>

  <div class="metrics">
    <div class="card kpi">
      <div class="kpi-l">CPU / Tải máy</div>
      <div class="kpi-v" id="k_cpu">-</div>
      <div class="kpi-sub" id="k_cpu_sub">Đang đo...</div>
      <div class="meter" id="m_cpu"><span style="width:0%"></span></div>
    </div>
    <div class="card kpi">
      <div class="kpi-l">RAM đang dùng</div>
      <div class="kpi-v" id="k_ram">-</div>
      <div class="kpi-sub" id="k_ram_sub">Tool: -</div>
      <div class="meter" id="m_ram"><span style="width:0%"></span></div>
    </div>
    <div class="card kpi">
      <div class="kpi-l">Kho Acc chờ</div>
      <div class="kpi-v" id="k_q">-</div>
      <div class="kpi-sub" id="k_q_sub">Phân loại: -</div>
    </div>
    <div class="card kpi">
      <div class="kpi-l">Đã xong / Acc lỗi</div>
      <div class="kpi-v" id="k_dd">-</div>
      <div class="kpi-sub">Tổng kết phiên chạy</div>
    </div>
    <div class="card kpi">
      <div class="kpi-l">Bộ nhớ trống</div>
      <div class="kpi-v" id="k_disk">-</div>
      <div class="kpi-sub" id="k_disk_sub">Phân vùng /data</div>
    </div>
  </div>

  <div class="card screen-box" id="screen_box">
    <div class="sec-hdr">
      <div class="sec-t">Ảnh Màn Hình Thiết Bị Trực Tiếp</div>
      <div class="bar">
        <button class="pri" onclick="loadScreen()">Làm mới ảnh</button>
        <button onclick="toggleScreen(false)">Đóng</button>
      </div>
    </div>
    <img id="screen_img" class="screen-img" alt="Ảnh chụp màn hình thiết bị">
  </div>

  <div class="card">
    <div class="sec-hdr">
      <div class="sec-t">Danh Sách Tab Roblox (<span id="inst_count">0</span>)</div>
      <div class="bar">
        <button class="ok" onclick="act('rejoin_instance','all')">Vào lại tất cả</button>
        <button onclick="act('reload_config')">Tải lại cài đặt</button>
      </div>
    </div>
    <div style="overflow:auto">
      <table>
        <thead>
          <tr>
            <th>Tab Roblox</th><th>Tài khoản</th><th>Trạng thái</th>
            <th>Kiểm tra / Tín hiệu</th><th>RAM</th><th>Vào lại / Đổi acc</th>
            <th>Sự kiện</th><th>Thao tác</th>
          </tr>
        </thead>
        <tbody id="inst_rows"></tbody>
      </table>
    </div>
  </div>

  <div class="grid2">
    <div class="card">
      <div class="sec-hdr">
        <div class="sec-t">Kho Cookie &amp; Link Server Game</div>
        <span class="mono" id="ck_lines" style="font-size:11px;color:var(--muted)">0 dòng</span>
      </div>
      <textarea id="ck_box" rows="3" placeholder="Dán cookie hoặc user:pass:cookie (mỗi acc 1 dòng)" oninput="updateCkLines()"></textarea>
      <div class="bar" style="margin-top:8px">
        <button class="pri" onclick="addCookies(false)">Thêm Cookie</button>
        <button onclick="addCookies(true)">Thêm &amp; Lọc acc lỗi</button>
      </div>
      <div class="row" style="margin-top:10px">
        <select id="link_target_inst" style="flex:0 0 135px"><option value="all">Tất cả Tab</option></select>
        <input id="link_box" placeholder="ID Game hoặc Link VIP/Share">
        <button class="pri" style="flex:0 0 auto" onclick="setLinks()">Gắn link</button>
      </div>
    </div>

    <div class="card">
      <div class="sec-hdr">
        <div class="sec-t">Tự Đổi Acc Blox Fruits (config-change.json)</div>
      </div>
      <div class="row">
        <div><span class="kpi-l">Cấp độ</span><input id="bf_level" type="number" min="0"></div>
        <div><span class="kpi-l">Beli</span><input id="bf_beli" type="number" min="0"></div>
        <div><span class="kpi-l">Điểm F</span><input id="bf_frag" type="number" min="0"></div>
        <div><span class="kpi-l">Tộc / Ver</span><input id="bf_race" placeholder="0 hoặc V4"></div>
      </div>
      <div class="checks" id="bf_checks"></div>
      <button class="pri" onclick="saveBf()">Lưu cấu hình Blox Fruits</button>
    </div>
  </div>

  <div class="card">
    <div class="sec-hdr">
      <div class="sec-t">Nhật Ký Hoạt Động</div>
      <div class="bar">
        <input id="log_filter" placeholder="Lọc log (VD: clone1, 279, WEB)..." style="width:210px" oninput="renderLogs()">
        <button id="btn_autoscroll" class="chip active" onclick="toggleAutoScroll()">Tự cuộn: BẬT</button>
      </div>
    </div>
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
const BF_LABELS = {
  god_human:'Godhuman', sanguine_art:'Sanguine Art', cursed_dual_katana:'Song Kiếm (CDK)',
  true_triple_katana:'Tam Kiếm (TTK)', shark_anchor:'Mỏ Neo', skull_guitar:'Đàn (Skull)',
  mirror_fractal:'Gương', valkyrie_helm:'Mũ Valkyrie', pull_level:'Đã gạt cần'
};
const ST_VI = {
  RUNNING:'Đang chơi', LAUNCHING:'Đang mở', WAITING_COOKIE:'Chờ acc',
  REJOINING:'Đang vào lại', ROTATING:'Đang đổi acc', PAUSED_CHALLENGE:'Tạm dừng',
  RETIRED:'Đã nghỉ', STOPPED:'Đã dừng', ERROR:'Báo lỗi', IDLE:'Đang chờ'
};
const MODES = ['left_to_right','horizontal_strip','grid','tiny'];
let bfInit = false;
let curLayout = {sort_tabs:true, mode:'left_to_right'};
let cachedLogs = [];
let autoScrollLogs = true;
let screenObjUrl = null;

function esc(s){return String(s??'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}
function fmtSec(sec){
  if(sec==null) return '';
  const s=Math.max(0,Math.floor(sec)), m=Math.floor(s/60), r=s%60;
  return m>0 ? `${m}m${String(r).padStart(2,'0')}s` : `${r}s`;
}
function hdrs(json){
  const h = {};
  const tok = localStorage.getItem('rj_token') || '';
  if(tok) h['Authorization'] = 'Bearer ' + tok;
  if(json) h['Content-Type'] = 'application/json';
  return h;
}
function toast(msg){const t=document.getElementById('toast');t.textContent=msg;t.style.display='block';clearTimeout(t._tid);t._tid=setTimeout(()=>t.style.display='none',2800);}
function setToken(){const v=prompt('Nhập mật khẩu Web (để trống để xoá):', localStorage.getItem('rj_token')||''); if(v!==null){localStorage.setItem('rj_token',v.trim()); refresh();}}
function updateCkLines(){
  const lines=(document.getElementById('ck_box').value||'').split(/\r?\n/).filter(x=>x.trim()).length;
  document.getElementById('ck_lines').textContent=`${lines} dòng`;
}
function toggleAutoScroll(){
  autoScrollLogs=!autoScrollLogs;
  const b=document.getElementById('btn_autoscroll');
  b.textContent='Tự cuộn: '+(autoScrollLogs?'BẬT':'TẮT');
  b.classList.toggle('active',autoScrollLogs);
}
function renderLayoutBar(){
  const badge=document.getElementById('auto_sort_badge');
  const txt=document.getElementById('auto_sort_txt');
  const btn=document.getElementById('btn_toggle_autosort');
  badge.classList.toggle('on',!!curLayout.sort_tabs);
  txt.textContent=curLayout.sort_tabs?'Tự động: BẬT (Mặc định)':'Tự động: ĐANG TẮT';
  btn.textContent=curLayout.sort_tabs?'Tắt tự động':'Bật tự động';
  for(const m of MODES){
    const el=document.getElementById('mode_'+m);
    if(el) el.classList.toggle('active', curLayout.mode===m);
  }
}
async function api(path, opts={}){
  const r = await fetch(path, opts);
  if(r.status===401){toast('Chưa nhập đúng mật khẩu: bấm nút Mật khẩu'); throw new Error('401');}
  return await r.json();
}
async function act(action, instance, extra={}){
  const d = await api('/api/action',{method:'POST',headers:hdrs(true),body:JSON.stringify({action,instance,...extra})});
  if(action==='sort_tabs' || action==='tiny_tabs'){
    toast(d.ok ? `Đã sắp xếp ${d.resized??0} cửa sổ Roblox` : ('Lỗi: '+d.error));
  }else{
    toast(d.ok ? ('Đã thực hiện: '+action) : ('Lỗi: '+d.error));
  }
  refresh();
}
function sortNow(){ act('sort_tabs', null, {mode: curLayout.mode}); }
async function setLayoutMode(mode){
  curLayout.mode = mode;
  curLayout.sort_tabs = true;
  renderLayoutBar();
  const d = await api('/api/action',{method:'POST',headers:hdrs(true),body:JSON.stringify({action:'set_tab_layout',sort_tabs:true,mode})});
  toast(d.ok ? `Đã chọn kiểu xếp & căn chỉnh ${d.resized??0} tab` : ('Lỗi: '+d.error));
  refresh();
}
async function toggleAutoSort(){
  const next = !curLayout.sort_tabs;
  curLayout.sort_tabs = next;
  renderLayoutBar();
  const d = await api('/api/action',{method:'POST',headers:hdrs(true),body:JSON.stringify({action:'set_tab_layout',sort_tabs:next,mode:curLayout.mode})});
  toast(d.ok ? (next?'Đã bật tự động xếp tab':'Đã tắt tự động xếp tab') : ('Lỗi: '+d.error));
  refresh();
}
async function toggleScreen(force){
  const box=document.getElementById('screen_box');
  const open=typeof force==='boolean'?force:!box.classList.contains('open');
  box.classList.toggle('open',open);
  if(open) await loadScreen();
}
async function loadScreen(){
  try{
    const r = await fetch('/api/screenshot.png?t='+Date.now(), {headers:hdrs(false)});
    if(!r.ok){toast('Không chụp được màn hình (cần quyền root)'); return;}
    const blob = await r.blob();
    if(screenObjUrl) URL.revokeObjectURL(screenObjUrl);
    screenObjUrl = URL.createObjectURL(blob);
    document.getElementById('screen_img').src = screenObjUrl;
  }catch(e){toast('Lỗi tải ảnh màn hình');}
}
async function addCookies(prune){
  const text = document.getElementById('ck_box').value;
  const d = await api('/api/cookies',{method:'POST',headers:hdrs(true),body:JSON.stringify({text,prune})});
  if(d.ok){document.getElementById('ck_box').value=''; updateCkLines(); toast('Đã thêm '+d.added+' cookie'); refresh();}
}
async function setLinks(){
  const instance = document.getElementById('link_target_inst').value || 'all';
  const target = document.getElementById('link_box').value.trim();
  const d = await api('/api/links',{method:'POST',headers:hdrs(true),body:JSON.stringify({instance,target})});
  toast(d.ok ? `Đã gắn link cho ${d.updated||0} Tab` : d.error); refresh();
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
  toast(d.ok ? 'Đã lưu cấu hình Blox Fruits' : d.error); refresh();
}
function renderLogs(){
  const box=document.getElementById('logs');
  const q=(document.getElementById('log_filter').value||'').trim().toLowerCase();
  const rows=cachedLogs.filter(l=>!q || l.toLowerCase().includes(q));
  if(!rows.length){box.innerHTML='<div class="log-dim">Đang chờ nhật ký...</div>'; return;}
  box.innerHTML=rows.map(l=>{
    const low=l.toLowerCase();
    let cls='log-dim';
    if(low.includes('err')||low.includes('disconnect')||low.includes('stale')||low.includes('fail')||low.includes('dead')) cls='log-err';
    else if(low.includes('launching')||low.includes('wait')||low.includes('warn')||low.includes('challenge')) cls='log-warn';
    else if(low.includes('running')||low.includes('rotated')||low.includes('switched')||low.includes('added')||low.includes('adopted')) cls='log-ok';
    else if(low.includes('[web]')||low.includes('[sys]')||low.includes('[hub]')) cls='log-sys';
    return `<div class="log-line ${cls}">${esc(l)}</div>`;
  }).join('');
  if(autoScrollLogs) box.scrollTop=box.scrollHeight;
}
function setMeter(id, pct){
  const el=document.getElementById(id); if(!el) return;
  const p=Math.max(0,Math.min(100,Math.round(pct||0)));
  el.classList.remove('ok','warn','err');
  el.classList.add(p>=85?'err':(p>=65?'warn':'ok'));
  el.firstElementChild.style.width=p+'%';
}
async function refresh(){
  try{
    const d = await api('/api/status',{headers:hdrs(false)});
    if(!d.ok) return;
    const s = d.system || {}, c = d.counts || {}, cs = d.config_summary || {};
    const pill=document.getElementById('live_pill');
    pill.classList.toggle('on',!!d.daemon_alive);
    document.getElementById('live_txt').textContent = d.daemon_alive?'Đang treo tự động':'Chế độ xem';
    const swTxt = Object.entries(d.switched||{}).filter(([,v])=>v>0).map(([k,v])=>k+':'+v).join(' · ');
    const devTxt = cs.device_name ? `Máy: ${cs.device_name} · ` : '';
    document.getElementById('sub').textContent = `${devTxt}${d.daemon_alive?'Đang giám sát đa luồng':'Tool chưa chạy ngầm (mở tab để chạy)'}`;

    const cpuPct = Math.round(s.cpu_pct||0);
    document.getElementById('k_cpu').textContent = `${cpuPct}%`;
    document.getElementById('k_cpu_sub').textContent = `Load: ${s.load_1m||0} · ${s.cpu_count||1} nhân`;
    setMeter('m_cpu', cpuPct);

    const ramUsed=Math.round(s.ram_used_mb||0), ramTot=Math.round(s.ram_total_mb||0), ramPct=Math.round(s.ram_pct||0);
    document.getElementById('k_ram').textContent = `${ramUsed}/${ramTot}MB`;
    document.getElementById('k_ram_sub').textContent = `Chiếm ${ramPct}% · Tool ${Math.round(s.tool_ram_mb||0)}MB`;
    setMeter('m_ram', ramPct);

    document.getElementById('k_q').textContent = c.queue ?? 0;
    document.getElementById('k_q_sub').textContent = swTxt || 'Chưa có acc phân loại';
    document.getElementById('k_dd').textContent = `${c.done??0} / ${c.dead??0}`;
    document.getElementById('k_disk').textContent = `${s.disk_free_gb||0} GB`;
    document.getElementById('k_disk_sub').textContent = `Tổng ${s.disk_total_gb||0} GB`;

    if(typeof cs.sort_tabs === 'boolean'){
      curLayout.sort_tabs = cs.sort_tabs;
      curLayout.mode = cs.tiny_sort_tabs ? 'tiny' : (cs.sort_tabs_mode || 'left_to_right');
      renderLayoutBar();
    }

    const insts = d.instances || [];
    document.getElementById('inst_count').textContent = insts.length;
    const sel = document.getElementById('link_target_inst');
    const prevSel = sel.value || 'all';
    sel.innerHTML = '<option value="all">Tất cả Tab</option>' + insts.map(i=>`<option value="${esc(i.name)}">${esc(i.name)}</option>`).join('');
    if([...sel.options].some(o=>o.value===prevSel)) sel.value=prevSel;

    const tb = document.getElementById('inst_rows');
    tb.innerHTML = '';
    for(const r of insts){
      const tr = document.createElement('tr');
      const hb = r.check_method==='online' ? `online(${r.presence_strikes||0})` : (r.hb_age!=null ? Math.round(r.hb_age)+'s' : '-');
      const statsObj = r.stats || {};
      const errTxt = r.last_error_code ? ` [Lỗi ${r.last_error_code}]` : '';
      const nxtTxt = r.next_rejoin_in!=null ? ` · còn ${fmtSec(r.next_rejoin_in)}` : '';
      const stLabel = ST_VI[r.status] || r.status;
      const ramVal = r.ram_mb!=null ? Math.round(r.ram_mb) : null;
      const ramPctRow = ramVal!=null ? Math.min(100, Math.round((ramVal/1500)*100)) : 0;
      const statStr = Object.entries(statsObj).slice(0,2).map(([k,v])=>`${k}=${v}`).join(', ');
      tr.innerHTML = `
        <td data-label="Tab Roblox"><div><b>${esc(r.name)}</b><div class="mono" style="font-size:11px;color:var(--muted)">${esc(r.package)}</div></div></td>
        <td data-label="Tài khoản"><span style="font-weight:600;color:#f8fafc">${esc(r.username||'-')}</span></td>
        <td data-label="Trạng thái"><span class="badge ${esc(r.status)}">${esc(stLabel)}</span></td>
        <td data-label="Tín hiệu"><span class="mono">${esc(r.check_method||'-')} · ${esc(hb)}</span></td>
        <td data-label="RAM" class="ram-cell"><span class="mono">${ramVal!=null?ramVal+'MB':'-'}</span><div class="ram-bar"><i style="width:${ramPctRow}%"></i></div></td>
        <td data-label="Vào lại / Đổi"><span class="mono">${r.rejoin_count||0} / ${r.rotate_count||0}${esc(nxtTxt)}</span></td>
        <td data-label="Sự kiện" style="font-size:11.5px;color:var(--muted)">${esc((statStr || r.last_event || '-') + errTxt)}</td>
        <td data-label="Thao tác"><div class="bar">
          <button onclick="act('rejoin_instance','${esc(r.name)}')">Vào lại</button>
          <button class="warn" onclick="act('rotate_instance','${esc(r.name)}')">Đổi acc</button>
          <button class="err" onclick="act('stop_instance','${esc(r.name)}')">Dừng</button>
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
      box.innerHTML = BF_BOOLS.map(k=>`<label class="chk"><input type="checkbox" id="chk_${k}" ${cc[k]?'checked':''}>${BF_LABELS[k]||k}</label>`).join('');
    }
    cachedLogs = d.logs || [];
    renderLogs();
  }catch(e){}
}
renderLayoutBar();
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

    def _config_summary(self, cfg):
        return {
            "device_name": cfg.get("device_name", ""),
            "download_hub_sync": bool(cfg.get("download_hub_sync", True)),
            "server_hop_on_disconnect": bool(cfg.get("server_hop_on_disconnect", False)),
            "error_264_action": cfg.get("error_264_action", "cooldown"),
            "sort_tabs": bool(cfg.get("sort_tabs", True)),
            "sort_tabs_mode": str(cfg.get("sort_tabs_mode") or "left_to_right"),
            "tiny_sort_tabs": bool(cfg.get("tiny_sort_tabs", False)),
            "sort_tabs_interval_seconds": int(cfg.get("sort_tabs_interval_seconds") or 15),
        }

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
                "config_summary": self._config_summary(cfg),
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
                "config_summary": self._config_summary(cfg),
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
            "config_summary": self._config_summary(cfg),
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
            req_mode = str(body.get("mode") or ("tiny" if action == "tiny_tabs" else cfg.get("sort_tabs_mode") or "left_to_right"))
            tiny = action == "tiny_tabs" or req_mode == "tiny"
            if self.manager:
                resized = sort_roblox_tabs([w.state.package for w in self.manager.workers.values()], tiny=tiny, mode=req_mode)
                return {"ok": True, "resized": resized, "mode": req_mode}
            res = ipc_request("sort_tabs", {"tiny": tiny, "mode": req_mode}, sock_path=self.ipc_sock_path)
            if res.get("ok"):
                return {**res, "mode": req_mode}
            return {"ok": True, "resized": sort_roblox_tabs(pkgs, tiny=tiny, mode=req_mode), "mode": req_mode}

        if action == "set_tab_layout":
            valid_modes = {"left_to_right", "horizontal_strip", "grid", "tiny"}
            req_mode = str(body.get("mode") or cfg.get("sort_tabs_mode") or "left_to_right")
            if req_mode not in valid_modes:
                req_mode = "left_to_right"
            sort_on = bool(body.get("sort_tabs", True))
            cur_cfg = load_config()
            cur_cfg["sort_tabs"] = sort_on
            cur_cfg["sort_tabs_mode"] = req_mode
            cur_cfg["tiny_sort_tabs"] = (req_mode == "tiny")
            save_config(cur_cfg)
            if self.manager:
                self.manager.reload_config()
            else:
                ipc_request("reload_config", sock_path=self.ipc_sock_path, timeout=1.5)
            resized = 0
            if sort_on:
                target_pkgs = [w.state.package for w in self.manager.workers.values()] if self.manager else pkgs
                resized = sort_roblox_tabs(target_pkgs, tiny=(req_mode == "tiny"), mode=req_mode)
            return {"ok": True, "sort_tabs": sort_on, "mode": req_mode, "resized": resized}

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
