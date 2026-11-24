"""System monitor, RAM/cache cleaner, window tab sorter, multi-executor scanner, low-graphics optimizer, Discord rich embeds."""
import json
import math
import os
import re
import shlex
import subprocess
import threading
import time

import requests
from rich.progress import BarColumn, DownloadColumn, Progress, SpinnerColumn, TextColumn, TimeRemainingColumn

from rjcore import DEFAULT_AUTOEXEC, DEFAULT_WORKSPACE, GOFILE_TOKEN, TMP, atomic_write, force_stop, mask, su

PROTECTED_SCRIPT = "rejoin_heartbeat.lua"

_CPU_LOCK = threading.Lock()
_LAST_CPU_SAMPLE = None

_IGNORED_SDCARD_DIRS = {
    "android", "dcim", "pictures", "movies", "music", "download",
    "alarms", "notifications", "podcasts", "ringtones", "audiobooks", "documents",
}


# ───────────────────────── system stats & RAM ─────────────────────────
def _sample_cpu_pct():
    global _LAST_CPU_SAMPLE
    try:
        with open("/proc/stat") as f:
            first = f.readline().split()
        if not first or first[0] != "cpu":
            return 0.0
        vals = [int(x) for x in first[1:]]
        idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
        total = sum(vals)
    except (OSError, ValueError, IndexError):
        return 0.0

    with _CPU_LOCK:
        prev = _LAST_CPU_SAMPLE
        _LAST_CPU_SAMPLE = (total, idle)
        if not prev:
            return 0.0
        dt = total - prev[0]
        di = idle - prev[1]
        if dt <= 0:
            return 0.0
        return round(max(0.0, min(100.0, (1.0 - (di / dt)) * 100.0)), 1)


def get_system_stats():
    """Return RAM, CPU %, load, Disk /data free, and uptime."""
    mem = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                parts = line.split(":")
                if len(parts) == 2:
                    val = parts[1].strip().split()[0]
                    mem[parts[0]] = int(val)
    except OSError:
        pass

    total_kb = mem.get("MemTotal", 0)
    avail_kb = mem.get("MemAvailable", mem.get("MemFree", 0) + mem.get("Cached", 0) + mem.get("Buffers", 0))
    ram_total_mb = round(total_kb / 1024.0, 1)
    ram_avail_mb = round(avail_kb / 1024.0, 1)
    ram_used_mb = round(max(0.0, ram_total_mb - ram_avail_mb), 1)
    ram_pct = round((ram_used_mb / ram_total_mb) * 100.0, 1) if ram_total_mb else 0.0

    load_1m = 0.0
    try:
        with open("/proc/loadavg") as f:
            load_1m = float(f.read().split()[0])
    except (OSError, ValueError, IndexError):
        pass

    disk_total_gb = 0.0
    disk_free_gb = 0.0
    try:
        st = os.statvfs("/data")
        disk_total_gb = round((st.f_blocks * st.f_frsize) / (1024.0 ** 3), 2)
        disk_free_gb = round((st.f_bavail * st.f_frsize) / (1024.0 ** 3), 2)
    except OSError:
        pass

    uptime_sec = 0.0
    try:
        with open("/proc/uptime") as f:
            uptime_sec = float(f.read().split()[0])
    except (OSError, ValueError, IndexError):
        pass

    tool_ram_mb = 0.0
    try:
        with open(f"/proc/{os.getpid()}/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    tool_ram_mb = round(int(line.split()[1]) / 1024.0, 1)
                    break
    except (OSError, ValueError, IndexError):
        pass

    return {
        "ram_total_mb": ram_total_mb,
        "ram_used_mb": ram_used_mb,
        "ram_avail_mb": ram_avail_mb,
        "ram_pct": ram_pct,
        "cpu_pct": _sample_cpu_pct(),
        "load_1m": load_1m,
        "cpu_count": os.cpu_count() or 1,
        "disk_total_gb": disk_total_gb,
        "disk_free_gb": disk_free_gb,
        "uptime_sec": uptime_sec,
        "tool_ram_mb": tool_ram_mb,
    }


def get_batch_pkg_usage(packages):
    """Collect PID and VmRSS (MB) for all packages in ONE su() call."""
    pkgs = []
    for p in packages or []:
        if p and p not in pkgs:
            pkgs.append(p)
    out_map = {p: {"pid": None, "mem_mb": None} for p in pkgs}
    if not pkgs:
        return out_map
    cmds = []
    for p in pkgs:
        qp = shlex.quote(p)
        cmds.append(
            f'P=$(pidof {qp} | awk \'{{print $1}}\'); '
            f'if [ -n "$P" ]; then R=$(grep -m1 "^VmRSS:" /proc/$P/status 2>/dev/null | awk \'{{print $2}}\'); '
            f'echo "{p}|$P|${{R:-0}}"; else echo "{p}||0"; fi'
        )
    rc, out, _ = su("; ".join(cmds), 12)
    if rc == 0 and out:
        for row in out.splitlines():
            parts = row.split("|")
            if len(parts) == 3 and parts[0] in out_map:
                pid_s, kb_s = parts[1].strip(), parts[2].strip()
                out_map[parts[0]] = {
                    "pid": int(pid_s) if pid_s.isdigit() else None,
                    "mem_mb": round(int(kb_s) / 1024.0, 1) if kb_s.isdigit() and int(kb_s) > 0 else None,
                }
    return out_map


def get_pkg_ram_mb(pkg):
    """Return VmRSS in MB for running package via root, or None."""
    return get_batch_pkg_usage([pkg]).get(pkg, {}).get("mem_mb")


# ───────────────────────── cleaner & optimizer ─────────────────────────
def clear_app_cache(pkg, include_webview=True):
    """Safely clear app cache/code_cache and WebView caches (never touches WebView Cookies or appData)."""
    q_d = shlex.quote(f"/data/data/{pkg}")
    q_sd = shlex.quote(f"/sdcard/Android/data/{pkg}")
    wv_part = ""
    if include_webview:
        q_wvc = shlex.quote(f"/data/data/{pkg}/app_webview/Default/Cache")
        q_wvcc = shlex.quote(f"/data/data/{pkg}/app_webview/Default/Code Cache")
        wv_part = f" {q_wvc}/* {q_wvcc}/*"
    rc, _, _ = su(
        f"rm -rf {q_d}/cache/* {q_d}/code_cache/* {q_sd}/cache/*{wv_part} 2>/dev/null; true",
        15,
    )
    return rc == 0


def kill_all_packages(packages):
    """Force-stop listed Roblox/Delta packages and trim background apps."""
    stopped = 0
    for pkg in packages:
        if pkg:
            force_stop(pkg)
            stopped += 1
    su("am kill-all", 15)
    return stopped


# ───────────────────────── multi-executor scanner ─────────────────────────
def _inspect_executor_root(root_path, label_hint=None):
    if not os.path.isdir(root_path):
        return None
    autoexec = None
    workspace = None
    try:
        entries = {e.lower(): e for e in os.listdir(root_path)}
    except OSError:
        return None

    for cand in ("autoexecute", "autoexec"):
        if cand in entries and os.path.isdir(os.path.join(root_path, entries[cand])):
            autoexec = os.path.join(root_path, entries[cand])
            break
    for cand in ("workspace",):
        if cand in entries and os.path.isdir(os.path.join(root_path, entries[cand])):
            workspace = os.path.join(root_path, entries[cand])
            break

    if not autoexec and not workspace:
        return None
    return {
        "name": label_hint or os.path.basename(root_path),
        "root": root_path,
        "autoexec": autoexec or os.path.join(root_path, "Autoexecute"),
        "workspace": workspace or os.path.join(root_path, "Workspace"),
    }


def scan_executor_dirs(pkg=None, sdcard_root="/storage/emulated/0"):
    """Discover Delta / Codex / Arceus / Fluxus / Cryptic / RejoinTool / custom executor directories."""
    found = []
    seen_roots = set()

    def _add(item):
        if item and item["root"] not in seen_roots:
            seen_roots.add(item["root"])
            found.append(item)

    if os.path.isdir(sdcard_root):
        try:
            for entry in sorted(os.scandir(sdcard_root), key=lambda e: e.name.lower()):
                if not entry.is_dir(follow_symlinks=False):
                    continue
                if entry.name.startswith(".") or entry.name.lower() in _IGNORED_SDCARD_DIRS:
                    continue
                _add(_inspect_executor_root(entry.path, entry.name))
        except OSError:
            pass

    if pkg:
        pkg_files = os.path.join(sdcard_root, "Android", "data", pkg, "files")
        if os.path.isdir(pkg_files):
            _add(_inspect_executor_root(pkg_files, pkg))
            try:
                for sub in sorted(os.scandir(pkg_files), key=lambda e: e.name.lower()):
                    if sub.is_dir(follow_symlinks=False):
                        _add(_inspect_executor_root(sub.path, f"{pkg}/{sub.name}"))
            except OSError:
                pass
        if sdcard_root == "/storage/emulated/0":
            priv_exe = f"/data/data/{pkg}/files/exe"
            if os.path.isdir(priv_exe):
                _add(_inspect_executor_root(priv_exe, f"{pkg}/exe"))

    if not found:
        found.append({
            "name": "Delta",
            "root": "/storage/emulated/0/Delta",
            "autoexec": DEFAULT_AUTOEXEC,
            "workspace": DEFAULT_WORKSPACE,
        })
    return found


def optimize_cloud_device(packages=None, disable_animations=True, whitelist_doze=True):
    """Optimize Cloud Phone OS: disable UI animations, whitelist Termux/clones from Doze, and trim background apps."""
    res = {"animations_disabled": False, "doze_whitelisted": 0, "trimmed": False}
    if disable_animations:
        rc, _, _ = su(
            "settings put global window_animation_scale 0; "
            "settings put global transition_animation_scale 0; "
            "settings put global animator_duration_scale 0",
            timeout=10,
        )
        res["animations_disabled"] = (rc == 0)

    if whitelist_doze:
        targets = ["com.termux", "com.termux.boot"]
        for p in packages or []:
            if p and p not in targets:
                targets.append(p)
        cmds = [f"dumpsys deviceidle whitelist +{shlex.quote(p)}" for p in targets]
        rc_w, _, _ = su("; ".join(cmds), timeout=15)
        if rc_w == 0:
            res["doze_whitelisted"] = len(targets)

    rc_k, _, _ = su("am kill-all", timeout=15)
    res["trimmed"] = (rc_k == 0)
    return res


# ───────────────────────── window grid & tab sorter ─────────────────────────
def get_screen_resolution():
    """Return (width, height) from `wm size`, preferring Override size."""
    rc, out, _ = su("wm size", 8)
    if rc == 0 and out:
        ovr = re.search(r"Override size:\s*(\d+)x(\d+)", out)
        if ovr:
            return int(ovr.group(1)), int(ovr.group(2))
        phy = re.search(r"Physical size:\s*(\d+)x(\d+)", out)
        if phy:
            return int(phy.group(1)), int(phy.group(2))
    return 1080, 1920


def _parse_task_ids_from_text(text, packages):
    results = {}
    if not text or not packages:
        return results
    pkg_set = set(packages)
    # Match lines containing taskId=123 or Task{... #123 ...} alongside a known package name
    for line in text.splitlines():
        matched_pkg = None
        for p in pkg_set:
            if re.search(rf"(?<![A-Za-z0-9_.]){re.escape(p)}(?![A-Za-z0-9_.])", line):
                matched_pkg = p
                break
        if not matched_pkg:
            continue
        m = (
            re.search(r"\btaskId\s*=\s*(\d+)", line)
            or re.search(r"\bTask\s+id\s*#?\s*(\d+)", line, re.IGNORECASE)
            or re.search(r"\bTask\{[^\n]*?#(\d+)\b", line)
            or re.search(r"\bt(\d+)\b", line)
        )
        if m:
            results[matched_pkg] = int(m.group(1))
    return results


def get_package_task_ids(packages):
    """Return {package: task_id} for currently open Android tasks."""
    if not packages:
        return {}
    for cmd in ("am stack list", "am task list", "dumpsys activity activities"):
        rc, out, _ = su(cmd, 12)
        if rc == 0 and out:
            found = _parse_task_ids_from_text(out, packages)
            if found:
                return found
    return {}


def calculate_grid_positions(count, width, height, tiny=False, top_inset=48):
    """Compute non-overlapping (left, top, right, bottom) rects for `count` windows."""
    if count <= 0:
        return []
    width = max(320, int(width))
    height = max(320, int(height))
    top_inset = max(0, min(int(top_inset), height // 4))
    usable_h = max(160, height - top_inset)

    cols = max(1, math.ceil(math.sqrt(count)))
    rows = max(1, math.ceil(count / cols))

    rects = []
    if tiny:
        tile_w = max(160, min(280, width // max(2, cols)))
        tile_h = max(160, min(280, usable_h // max(2, rows)))
        for idx in range(count):
            r = idx // cols
            c = idx % cols
            left = min(c * tile_w, max(0, width - tile_w))
            top = min(top_inset + r * tile_h, max(top_inset, height - tile_h))
            rects.append((left, top, min(width, left + tile_w), min(height, top + tile_h)))
        return rects

    cell_w = width // cols
    cell_h = usable_h // rows
    for idx in range(count):
        r = idx // cols
        c = idx % cols
        left = c * cell_w
        top = top_inset + r * cell_h
        right = width if c == cols - 1 else (c + 1) * cell_w
        bottom = height if r == rows - 1 else top_inset + (r + 1) * cell_h
        rects.append((left, top, right, bottom))
    return rects


def sort_roblox_tabs(packages, tiny=False):
    """Arrange active Roblox/Delta tasks into a freeform grid (or tiny tiles). Returns resized count."""
    tasks = get_package_task_ids(packages)
    if not tasks:
        return 0
    w, h = get_screen_resolution()
    ordered_pkgs = [p for p in packages if p in tasks]
    rects = calculate_grid_positions(len(ordered_pkgs), w, h, tiny=tiny)
    resized = 0
    for pkg, (l, t, r, b) in zip(ordered_pkgs, rects):
        tid = tasks[pkg]
        rc, _, _ = su(f"am task resize {tid} {l} {t} {r} {b}", 10)
        if rc == 0:
            resized += 1
    return resized


def focus_before_kill(target_pkg):
    """Shift Android window focus to Termux before stopping target_pkg to avoid launcher glitches."""
    tasks = get_package_task_ids(["com.termux", target_pkg])
    tid_termux = tasks.get("com.termux")
    if tid_termux and target_pkg in tasks:
        su(f"am task focus {tid_termux}", 8)
        return True
    return False


def stop_package(pkg, use_focus=True):
    """Stop package safely with optional pre-stop focus shift."""
    if use_focus:
        focus_before_kill(pkg)
    return force_stop(pkg)


def remove_package_tasks(pkg):
    """Remove Android task card(s) belonging to pkg (used when retiring deleted accounts)."""
    tasks = get_package_task_ids([pkg])
    tid = tasks.get(pkg)
    if tid:
        rc, _, _ = su(f"am stack remove {tid}", 8)
        return rc == 0
    return False


# ───────────────────────── multi-display screenshot ─────────────────────────
def _is_valid_png(path):
    if not os.path.exists(path) or os.path.getsize(path) <= 1024:
        return False
    try:
        with open(path, "rb") as f:
            return f.read(8) == b"\x89PNG\r\n\x1a\n"
    except OSError:
        return False


def capture_screenshot(out_path):
    """Capture device screen via root screencap with multi-display fallback."""
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    q_out = shlex.quote(out_path)
    rc, _, _ = su(f"screencap -p {q_out} && chmod 644 {q_out}", 15)
    if rc == 0 and _is_valid_png(out_path):
        return True

    # Multi-display fallback (SurfaceFlinger physical display IDs)
    rc_d, out_d, _ = su("dumpsys SurfaceFlinger --display-id", 10)
    if rc_d == 0 and out_d:
        for disp_id in re.findall(r"(?m)^Display\s+(\d+)\s+\(", out_d):
            rc2, _, _ = su(f"screencap -d {disp_id} -p {q_out} && chmod 644 {q_out}", 15)
            if rc2 == 0 and _is_valid_png(out_path):
                return True
    return False


# ───────────────────────── low graphics ─────────────────────────
def _patch_graphics_xml(xml_text, fps_cap=15, low_quality=True):
    out = xml_text
    if fps_cap and int(fps_cap) > 0:
        out = re.sub(
            r'(<int name="FramerateCap">)-?\d+(</int>)',
            rf"\g<1>{int(fps_cap)}\g<2>",
            out,
        )
    if low_quality:
        out = re.sub(
            r'(<int name="GraphicsQualityLevel">)-?\d+(</int>)',
            r"\g<1>1\g<2>",
            out,
        )
        out = re.sub(
            r'(<token name="SavedQualityLevel">)-?\d+(</token>)',
            r"\g<1>1\g<2>",
            out,
        )
    return out


def apply_low_graphics(pkg, fps_cap=15, low_quality=True):
    """Apply FPS cap & minimum graphics quality to GlobalBasicSettings_13.xml and ClientAppSettings.json."""
    q_pkg_dir = shlex.quote(f"/data/data/{pkg}")
    gbs_dst = f"/data/data/{pkg}/files/appData/GlobalBasicSettings_13.xml"
    q_gbs = shlex.quote(gbs_dst)
    rc, out, _ = su(f"test -f {q_gbs} && echo yes", 10)
    if rc == 0 and out == "yes":
        local = os.path.join(TMP, f"{pkg}_gbs.xml")
        q_local = shlex.quote(local)
        su(f"cp {q_gbs} {q_local} && chmod 666 {q_local}", 10)
        if os.path.exists(local):
            try:
                with open(local, encoding="utf-8", errors="ignore") as f:
                    patched = _patch_graphics_xml(f.read(), fps_cap=fps_cap, low_quality=low_quality)
                atomic_write(local, patched, 0o666)
                su(
                    f"U=$(stat -c %u:%g {q_pkg_dir}) && cp {q_local} {q_gbs} && "
                    f"chown $U {q_gbs} && chmod 660 {q_gbs} && restorecon {q_gbs} 2>/dev/null; true",
                    10,
                )
            finally:
                try:
                    os.remove(local)
                except OSError:
                    pass

    cs_dir = f"/data/data/{pkg}/files/appData/ClientSettings"
    cs_dst = f"{cs_dir}/ClientAppSettings.json"
    local_cs = os.path.join(TMP, f"{pkg}_cs.json")
    cfg = {
        "DFIntTaskSchedulerTargetFps": int(fps_cap) if fps_cap and int(fps_cap) > 0 else 60,
        "DFFlagTextureQualityOverrideEnabled": bool(low_quality),
        "DFIntTextureQualityOverride": 0,
    }
    atomic_write(local_cs, json.dumps(cfg, indent=2), 0o666)
    q_cs_dir = shlex.quote(cs_dir)
    q_cs_dst = shlex.quote(cs_dst)
    q_local_cs = shlex.quote(local_cs)
    rc2, _, _ = su(
        f"U=$(stat -c %u:%g {q_pkg_dir}) && mkdir -p {q_cs_dir} && cp {q_local_cs} {q_cs_dst} && "
        f"chown $U {q_cs_dir} {q_cs_dst} && chmod 660 {q_cs_dst} && restorecon {q_cs_dst} 2>/dev/null; true",
        10,
    )
    try:
        os.remove(local_cs)
    except OSError:
        pass
    return rc2 == 0


def set_stay_awake(enable):
    """Toggle Android screen stay-on and Termux wake-lock."""
    rc, _, _ = su("svc power stayon true" if enable else "svc power stayon false", 10)
    cmd = "termux-wake-lock" if enable else "termux-wake-unlock"
    try:
        subprocess.run([cmd], capture_output=True, timeout=5)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return rc == 0


# ───────────────────────── Discord Rich Embeds ─────────────────────────
def sanitize_discord_embed(embed):
    """Mask cookies and clamp Discord embed lengths within API limits (total text <= 5800)."""
    clean = {}
    budget = 5800

    title = mask(str(embed.get("title") or ""))[:250]
    if title:
        clean["title"] = title
        budget -= len(title)

    desc = mask(str(embed.get("description") or ""))[:min(3900, max(0, budget))]
    if desc:
        clean["description"] = desc
        budget -= len(desc)

    if "color" in embed and embed["color"] is not None:
        clean["color"] = int(embed["color"])
    if embed.get("timestamp"):
        clean["timestamp"] = str(embed["timestamp"])
    if embed.get("image"):
        clean["image"] = dict(embed["image"])

    footer_obj = embed.get("footer")
    if isinstance(footer_obj, dict) and footer_obj.get("text"):
        ft = mask(str(footer_obj["text"]))[:min(2000, max(0, budget))]
        if ft:
            clean["footer"] = {"text": ft}
            budget -= len(ft)

    raw_fields = embed.get("fields") or []
    clean_fields = []
    for f in raw_fields[:25]:
        if budget <= 10:
            break
        fname = mask(str(f.get("name", "")))[:min(250, budget)]
        budget -= len(fname)
        if budget <= 0:
            break
        fval = mask(str(f.get("value", "-")))[:min(1000, budget)]
        budget -= len(fval)
        clean_fields.append({
            "name": fname or "-",
            "value": fval or "-",
            "inline": bool(f.get("inline", True)),
        })
    if clean_fields:
        clean["fields"] = clean_fields

    return clean


def send_discord_embed(
    webhook,
    title,
    description="",
    color=0x5865F2,
    fields=None,
    image_path=None,
    ping="",
    footer="",
    max_retries=2,
):
    """Send a masked, rate-limit-aware Discord Rich Embed (with optional screenshot attachment)."""
    if not webhook:
        return False
    embed = {
        "title": title,
        "description": description,
        "color": int(color),
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if fields:
        embed["fields"] = list(fields)
    if footer:
        embed["footer"] = {"text": footer}

    clean_embed = sanitize_discord_embed(embed)
    payload = {"embeds": [clean_embed]}

    ping_str = mask(str(ping or "")).strip()[:200]
    if ping_str:
        payload["content"] = ping_str
        user_ids = re.findall(r"<@!?(\d{1,32})>", ping_str)
        allowed = {"parse": ["everyone"] if "@everyone" in ping_str or "@here" in ping_str else []}
        if user_ids:
            allowed["users"] = user_ids[:10]
        payload["allowed_mentions"] = allowed

    if image_path and os.path.exists(image_path):
        clean_embed["image"] = {"url": "attachment://screen.png"}

    for attempt in range(max_retries + 1):
        try:
            if image_path and os.path.exists(image_path):
                with open(image_path, "rb") as img:
                    r = requests.post(
                        webhook,
                        data={"payload_json": json.dumps(payload)},
                        files={"file": ("screen.png", img, "image/png")},
                        timeout=20,
                    )
            else:
                r = requests.post(webhook, json=payload, timeout=12)

            if 200 <= r.status_code < 300:
                return True
            if r.status_code == 429 and attempt < max_retries:
                ra = r.headers.get("Retry-After") or r.headers.get("X-RateLimit-Reset-After")
                try:
                    delay = min(10.0, max(0.5, float(ra)))
                except (TypeError, ValueError):
                    delay = 2.0
                time.sleep(delay)
                continue
            return False
        except Exception:
            if attempt < max_retries:
                time.sleep(1.0)
                continue
            return False
    return False


# ───────────────────────── Delta Autoexecute Manager ─────────────────────────
def list_autoexec_scripts(autoexec_dir):
    if not os.path.isdir(autoexec_dir):
        return []
    out = []
    for fname in sorted(os.listdir(autoexec_dir)):
        if fname.endswith(".lua") or fname.endswith(".lua.disabled"):
            enabled = fname.endswith(".lua")
            full = os.path.join(autoexec_dir, fname)
            if not os.path.isfile(full):
                continue
            out.append({
                "name": fname,
                "enabled": enabled,
                "size": os.path.getsize(full),
                "path": full,
                "protected": fname == PROTECTED_SCRIPT,
            })
    return out


def add_autoexec_script(autoexec_dir, name, content):
    os.makedirs(autoexec_dir, exist_ok=True)
    base = os.path.basename(name.strip())
    if not base.endswith(".lua"):
        base += ".lua"
    if base == PROTECTED_SCRIPT:
        raise ValueError(f"{PROTECTED_SCRIPT} is managed by deploy-lua")
    dest = os.path.join(autoexec_dir, base)
    atomic_write(dest, content, 0o644)
    return dest


def toggle_autoexec_script(autoexec_dir, name):
    base = os.path.basename(name.strip())
    if base == PROTECTED_SCRIPT:
        raise ValueError(f"Cannot disable protected script {PROTECTED_SCRIPT}")
    cur = os.path.join(autoexec_dir, base)
    if not os.path.exists(cur):
        if os.path.exists(cur + ".lua"):
            cur += ".lua"
        elif os.path.exists(cur + ".lua.disabled"):
            cur += ".lua.disabled"
        else:
            raise FileNotFoundError(f"Script not found: {base}")
    if os.path.basename(cur) == PROTECTED_SCRIPT:
        raise ValueError(f"Cannot disable protected script {PROTECTED_SCRIPT}")
    if cur.endswith(".lua.disabled"):
        nxt = cur[:-len(".disabled")]
    elif cur.endswith(".lua"):
        nxt = cur + ".disabled"
    else:
        raise ValueError(f"Unsupported script extension: {cur}")
    os.replace(cur, nxt)
    return nxt


def delete_autoexec_script(autoexec_dir, name):
    base = os.path.basename(name.strip())
    if base == PROTECTED_SCRIPT:
        raise ValueError(f"Cannot delete protected script {PROTECTED_SCRIPT}")
    target = os.path.join(autoexec_dir, base)
    if not os.path.isfile(target):
        return False
    os.remove(target)
    return True


# ───────────────────────── GoFile APK Downloader ─────────────────────────
class GoFileDownloader:
    """Download Roblox/Executor APKs directly from GoFile links with Rich progress bar."""

    API_BASE = "https://api.gofile.io"
    BASE_HEADERS = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/148.0.0.0 Safari/537.36",
        "Origin": "https://gofile.io",
        "Referer": "https://gofile.io/",
    }

    def get_token(self):
        if os.path.isfile(GOFILE_TOKEN):
            try:
                tok = open(GOFILE_TOKEN).read().strip()
                if tok:
                    return tok
            except OSError:
                pass
        r = requests.post(f"{self.API_BASE}/accounts", headers=self.BASE_HEADERS, timeout=20)
        d = r.json()
        if d.get("status") != "ok":
            raise RuntimeError(f"GoFile token error: {d}")
        tok = d["data"]["token"]
        atomic_write(GOFILE_TOKEN, tok, 0o600)
        return tok

    def list_files(self, url_or_id, password=""):
        m = re.search(r"gofile\.io/d/([A-Za-z0-9]+)", str(url_or_id))
        cid = m.group(1) if m else str(url_or_id).strip()
        tok = self.get_token()
        headers = {**self.BASE_HEADERS, "Authorization": f"Bearer {tok}"}
        api_url = f"{self.API_BASE}/contents/{cid}?wt=4fd6sg89d7s6&contentFilter=&page=1&pageSize=1000&sortField=name&sortDirection=1"
        if password:
            api_url += f"&password={password}"
        r = requests.get(api_url, headers=headers, timeout=20)
        d = r.json()
        if d.get("status") != "ok":
            raise RuntimeError(f"GoFile content error: {d.get('status')}")
        out = []
        children = (d.get("data") or {}).get("children") or {}
        for item in children.values():
            if item.get("type") == "file" and item.get("link"):
                safe_name = re.sub(r"[\x00-\x1f\x7f/\\]+", "_", item.get("name", "file.bin"))
                out.append({"name": safe_name, "size": int(item.get("size") or 0), "link": item["link"]})
        return out, tok

    def download(self, file_entry, token, download_dir="/storage/emulated/0/Download"):
        os.makedirs(download_dir, exist_ok=True)
        dest = os.path.join(download_dir, file_entry["name"])
        headers = {**self.BASE_HEADERS, "Cookie": f"accountToken={token}"}
        with requests.get(file_entry["link"], headers=headers, stream=True, timeout=60) as r:
            r.raise_for_status()
            total = int(r.headers.get("Content-Length") or file_entry.get("size") or 0)
            with Progress(
                SpinnerColumn(),
                TextColumn("[cyan]{task.fields[fname]}"),
                BarColumn(),
                DownloadColumn(),
                TimeRemainingColumn(),
            ) as prog:
                tid = prog.add_task("dl", total=total or None, fname=file_entry["name"])
                with open(dest, "wb") as f:
                    for chunk in r.iter_content(chunk_size=131072):
                        if chunk:
                            f.write(chunk)
                            prog.update(tid, advance=len(chunk))
        return dest
