"""System monitor, RAM/cache cleaner, window tab sorter, multi-executor scanner, low-graphics optimizer, Discord rich embeds."""
import json
import math
import os
import re
import shlex
import struct
import subprocess
import threading
import time
import zipfile
import zlib

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
_FREEFORM_READY = False
_SET_WIN_MODE_TX = None
_SET_WIN_MODE_LOCK = threading.Lock()


def _read_uleb128(buf, pos):
    res = 0
    shift = 0
    while True:
        b = buf[pos]
        pos += 1
        res |= (b & 0x7F) << shift
        if not (b & 0x80):
            return res, pos
        shift += 7


def _get_set_windowing_mode_tx(framework_jar="/system/framework/framework.jar"):
    """Resolve Binder transaction code for IActivityTaskManager.setTaskWindowingMode(I,I,Z) from framework.jar."""
    global _SET_WIN_MODE_TX
    with _SET_WIN_MODE_LOCK:
        if _SET_WIN_MODE_TX is not None:
            return _SET_WIN_MODE_TX if _SET_WIN_MODE_TX else None
        resolved = False
        try:
            if not os.path.isfile(framework_jar):
                _SET_WIN_MODE_TX = False
                return None
            with zipfile.ZipFile(framework_jar, "r") as zf:
                dex_names = sorted(n for n in zf.namelist() if n.endswith(".dex"))
                targets = (
                    ("Landroid/app/IActivityTaskManager;", "activity_task"),
                    ("Landroid/app/IActivityManager;", "activity"),
                )
                for cls_target, svc_name in targets:
                    if resolved:
                        break
                    for dname in dex_names:
                        dex = zf.read(dname)
                        if cls_target.encode("ascii") not in dex or b"setTaskWindowingMode" not in dex:
                            continue
                        s_off, t_off, p_off = struct.unpack_from("<III", dex, 60)
                        t_off = struct.unpack_from("<I", dex, 68)[0]
                        p_off = struct.unpack_from("<I", dex, 76)[0]
                        m_off = struct.unpack_from("<I", dex, 92)[0]
                        c_cnt, c_off = struct.unpack_from("<II", dex, 96)

                        def _gstr(idx):
                            doff = struct.unpack_from("<I", dex, s_off + idx * 4)[0]
                            _, p = _read_uleb128(dex, doff)
                            end = dex.find(b"\x00", p)
                            return dex[p:end].decode("utf-8", "ignore")

                        def _gtype(idx):
                            return _gstr(struct.unpack_from("<I", dex, t_off + idx * 4)[0])

                        for c in range(c_cnt):
                            co = c_off + c * 32
                            cn = _gtype(struct.unpack_from("<I", dex, co)[0])
                            if cn != cls_target:
                                continue
                            data_off = struct.unpack_from("<I", dex, co + 24)[0]
                            if not data_off:
                                continue
                            sf, p = _read_uleb128(dex, data_off)
                            inf, p = _read_uleb128(dex, p)
                            dm, p = _read_uleb128(dex, p)
                            vm, p = _read_uleb128(dex, p)
                            for _ in range(sf + inf):
                                _, p = _read_uleb128(dex, p)
                                _, p = _read_uleb128(dex, p)
                            for _ in range(dm):
                                _, p = _read_uleb128(dex, p)
                                _, p = _read_uleb128(dex, p)
                                _, p = _read_uleb128(dex, p)
                            m_idx = 0
                            for tx in range(1, vm + 1):
                                d_idx, p = _read_uleb128(dex, p)
                                _, p = _read_uleb128(dex, p)
                                _, p = _read_uleb128(dex, p)
                                m_idx += d_idx
                                proto_idx = struct.unpack_from("<H", dex, m_off + m_idx * 8 + 2)[0]
                                name_idx = struct.unpack_from("<I", dex, m_off + m_idx * 8 + 4)[0]
                                if _gstr(name_idx) == "setTaskWindowingMode":
                                    par_off = struct.unpack_from("<I", dex, p_off + proto_idx * 12 + 8)[0]
                                    if par_off:
                                        psz = struct.unpack_from("<I", dex, par_off)[0]
                                        ptypes = [_gtype(struct.unpack_from("<H", dex, par_off + 4 + i * 2)[0]) for i in range(psz)]
                                        if ptypes == ["I", "I", "Z"]:
                                            resolved = (svc_name, tx)
                                            break
                            if resolved:
                                break
        except Exception:
            resolved = False
        _SET_WIN_MODE_TX = resolved
        return resolved if resolved else None


def ensure_freeform_support():
    """Enable Android global freeform / resizable window flags once per process."""
    global _FREEFORM_READY
    if _FREEFORM_READY:
        return True
    rc, _, _ = su(
        "settings put global development_settings_enabled 1; "
        "settings put global enable_freeform_support 1; "
        "settings put global force_resizable_activities 1; "
        "settings put global enable_non_resizable_multi_window 1; "
        "am suppress-resize-config-changes true >/dev/null 2>&1 || true",
        8,
    )
    if rc == 0:
        _FREEFORM_READY = True
        return True
    return False


def get_display_metrics():
    """Return dict with width, height, app_bottom, top_inset, and min_size from wm/dumpsys."""
    w, h = 1080, 1920
    app_bottom = None
    min_size = 216
    rc, out, _ = su("wm size; echo '---'; dumpsys window displays", 10)
    if rc == 0 and out:
        ovr = re.search(r"Override size:\s*(\d+)x(\d+)", out)
        phy = re.search(r"Physical size:\s*(\d+)x(\d+)", out)
        if ovr:
            w, h = int(ovr.group(1)), int(ovr.group(2))
        elif phy:
            w, h = int(phy.group(1)), int(phy.group(2))
        m_app = re.search(r"mAppBounds=Rect\(\d+,\s*\d+\s*-\s*(\d+),\s*(\d+)\)", out)
        if m_app:
            app_bottom = int(m_app.group(2))
        else:
            m_info = re.search(r"app\s+(\d+)\s*x\s*(\d+)", out)
            if m_info:
                app_bottom = int(m_info.group(2))
        m_min = re.search(r"mDefaultMinSize=(\d+)", out)
        if m_min:
            min_size = max(160, int(m_min.group(1)))
    if not app_bottom or app_bottom > h or app_bottom < 320:
        app_bottom = h
    return {
        "width": w,
        "height": h,
        "app_bottom": app_bottom,
        "top_inset": 48,
        "min_size": min_size,
    }


def get_screen_resolution():
    """Return (width, height) from `wm size`, preferring Override size."""
    m = get_display_metrics()
    return m["width"], m["height"]


def _parse_stack_and_task_ids(text, packages):
    """Parse {package: {'task_id': int, 'stack_id': int | None, 'windowing_mode': str, 'top_activity': str}}."""
    results = {}
    if not text or not packages:
        return results
    pkg_set = [p for p in packages if p]
    pkg_patterns = {
        p: re.compile(rf"(?<![A-Za-z0-9_.]){re.escape(p)}(?![A-Za-z0-9_.])")
        for p in pkg_set
    }
    cur_stack_id = None
    cur_win_mode = ""
    for line in text.splitlines():
        sm = re.search(r"\bStack\s+id\s*=\s*(\d+)", line)
        if sm:
            cur_stack_id = int(sm.group(1))
            cur_win_mode = ""
        wm_m = re.search(r"mWindowingMode=([a-zA-Z0-9_-]+)", line)
        if wm_m:
            cur_win_mode = wm_m.group(1).lower()
        matched_pkg = None
        for p, pat in pkg_patterns.items():
            if pat.search(line):
                matched_pkg = p
                break
        if not matched_pkg:
            continue
        m = (
            re.search(r"\btaskId\s*=\s*(\d+)", line)
            or re.search(r"\bTask\s+id\s*#?\s*(\d+)", line, re.IGNORECASE)
            or re.search(r"\bTask\{[^\n]*?#(\d+)\b", line)
        )
        if not m:
            continue
        tid = int(m.group(1))
        top_m = re.search(r"topActivity=ComponentInfo\{([^}]+)\}", line)
        top_act = top_m.group(1) if top_m else ""
        is_trampoline = "ActivityProtocolLaunch" in line
        is_visible = "visible=true" in line
        cand = {
            "task_id": tid,
            "stack_id": cur_stack_id,
            "windowing_mode": cur_win_mode,
            "top_activity": top_act,
            "visible": is_visible,
            "trampoline": is_trampoline,
        }
        prev = results.get(matched_pkg)
        if not prev:
            results[matched_pkg] = cand
        elif (prev["trampoline"] and not cand["trampoline"]) or (not prev["visible"] and cand["visible"]):
            results[matched_pkg] = cand
    return results


def _parse_task_ids_from_text(text, packages):
    parsed = _parse_stack_and_task_ids(text, packages)
    return {p: info["task_id"] for p, info in parsed.items()}


def get_package_task_details(packages, running_only=False):
    """Return {package: {'task_id': int, 'stack_id': int | None, 'windowing_mode': str, 'top_activity': str}}."""
    target_pkgs = [p for p in (packages or []) if p]
    if not target_pkgs:
        return {}
    if running_only:
        usage = get_batch_pkg_usage(target_pkgs)
        target_pkgs = [p for p in target_pkgs if usage.get(p, {}).get("pid") is not None]
        if not target_pkgs:
            return {}
    for cmd in ("am stack list", "am task list", "dumpsys activity activities"):
        rc, out, _ = su(cmd, 12)
        if rc == 0 and out:
            found = _parse_stack_and_task_ids(out, target_pkgs)
            if found:
                return found
    return {}


def get_package_task_ids(packages, running_only=False):
    """Return {package: task_id} for open Android tasks (optionally filtered to live PIDs)."""
    details = get_package_task_details(packages, running_only=running_only)
    return {p: info["task_id"] for p, info in details.items()}


def calculate_grid_positions(
    count,
    width,
    height,
    tiny=False,
    top_inset=48,
    mode="left_to_right",
    min_size=216,
    gap=4,
):
    """Compute (left, top, right, bottom) rects arranged cleanly left-to-right based on clone count."""
    if count <= 0:
        return []
    width = max(320, int(width))
    height = max(320, int(height))
    top_inset = max(0, min(int(top_inset), height // 4))
    usable_h = max(160, height - top_inset)
    min_sz = max(160, min(int(min_size), width, usable_h))
    gap = max(0, int(gap))

    eff_mode = "tiny" if tiny else str(mode or "left_to_right").lower()

    if eff_mode == "tiny":
        cols = max(1, min(count, max(2, width // max(180, min_sz))))
        rows = max(1, math.ceil(count / cols))
        tile_w = max(min_sz, min(280, width // cols))
        tile_h = max(min_sz, min(280, usable_h // max(2, rows)))
        rects = []
        for idx in range(count):
            r = idx // cols
            c = idx % cols
            left = min(c * tile_w, max(0, width - tile_w))
            top = min(top_inset + r * tile_h, max(top_inset, height - tile_h))
            rects.append((left, top, min(width, left + tile_w), min(height, top + tile_h)))
        return rects

    if eff_mode == "horizontal_strip":
        if width // count >= min_sz:
            cell_w = width // count
            row_h = usable_h if width >= height else max(min_sz, int(usable_h * 0.56))
            bottom = min(height, top_inset + row_h)
            rects = []
            for idx in range(count):
                left = idx * cell_w + (gap if idx > 0 else 0)
                right = width if idx == count - 1 else (idx + 1) * cell_w - gap
                if right - left < min_sz:
                    right = min(width, left + min_sz)
                rects.append((left, top_inset, right, bottom))
            return rects
        tile_w = max(min_sz, width // min(count, 3))
        tile_h = usable_h if width >= height else max(min_sz, int(usable_h * 0.52))
        bottom = min(height, top_inset + tile_h)
        span_x = max(0, width - tile_w)
        rects = []
        for idx in range(count):
            left = round(idx * span_x / max(1, count - 1)) if count > 1 else 0
            rects.append((left, top_inset, min(width, left + tile_w), bottom))
        return rects

    if eff_mode == "left_to_right":
        max_cols = max(1, width // min_sz)
        if count == 1:
            if height > width:
                h1 = max(min_sz, int(usable_h * 0.52))
                return [(gap, top_inset, max(gap + min_sz, width - gap), min(height, top_inset + h1))]
            w1 = max(min_sz, width // 2 - gap)
            return [(gap, top_inset, min(width, gap + w1), max(top_inset + min_sz, height - gap))]

        if count == 2 and max_cols >= 2:
            cols, rows = 2, 1
            eff_h = usable_h if width >= height else max(min_sz, int(usable_h * 0.58))
        elif count == 3 and max_cols >= 3:
            cols, rows = 3, 1
            eff_h = usable_h if width >= height else max(min_sz, int(usable_h * 0.52))
        elif count == 4:
            cols = 4 if (width >= height and max_cols >= 4) else min(2, max_cols)
            rows = max(1, math.ceil(count / cols))
            eff_h = usable_h
        else:
            cols = max(1, min(count, max_cols))
            rows = max(1, math.ceil(count / cols))
            eff_h = usable_h

        if eff_h // rows >= min_sz:
            cell_w = width // cols
            cell_h = eff_h // rows
            rects = []
            for idx in range(count):
                r = idx // cols
                c = idx % cols
                left = c * cell_w + (gap if c > 0 else 0)
                top = top_inset + r * cell_h + (gap if r > 0 else 0)
                right = width if c == cols - 1 else (c + 1) * cell_w - gap
                bottom = (top_inset + eff_h) if r == rows - 1 else (top_inset + (r + 1) * cell_h - gap)
                if right - left < min_sz:
                    right = min(width, left + min_sz)
                if bottom - top < min_sz:
                    bottom = min(height, top + min_sz)
                rects.append((left, top, right, bottom))
            return rects

        # Many clones: neat left-to-right cascade so every window title bar stays accessible
        tile_w = max(min_sz, width // max(1, min(max_cols, 3)))
        tile_h = max(min_sz, int(usable_h * 0.45))
        span_x = max(0, width - tile_w)
        span_y = max(0, usable_h - tile_h)
        rects = []
        for idx in range(count):
            frac = idx / max(1, count - 1)
            left = round(frac * span_x)
            top = top_inset + round((idx % max(1, min(count, 4))) * (span_y / max(1, min(count, 4) - 1)))
            rects.append((left, top, min(width, left + tile_w), min(height, top + tile_h)))
        return rects

    # Classic balanced grid mode
    if count == 2:
        cols, rows = (1, 2) if height > width else (2, 1)
    else:
        cols = max(1, math.ceil(math.sqrt(count)))
        rows = max(1, math.ceil(count / cols))
    cell_w = width // cols
    cell_h = usable_h // rows
    rects = []
    for idx in range(count):
        r = idx // cols
        c = idx % cols
        left = c * cell_w
        top = top_inset + r * cell_h
        right = width if c == cols - 1 else (c + 1) * cell_w
        bottom = height if r == rows - 1 else top_inset + (r + 1) * cell_h
        rects.append((left, top, right, bottom))
    return rects


def sort_roblox_tabs(packages=None, tiny=False, mode=None):
    """Arrange active running Roblox/Delta tasks into freeform windows left-to-right. Returns resized count."""
    ensure_freeform_support()
    target_pkgs = [p for p in (packages or []) if p]
    if not target_pkgs:
        from rjcore import detect_packages
        target_pkgs = detect_packages()
    tasks = get_package_task_details(target_pkgs, running_only=True)
    if not tasks:
        return 0
    metrics = get_display_metrics()
    w, h = metrics["width"], metrics["app_bottom"]
    ordered_pkgs = [p for p in target_pkgs if p in tasks]
    eff_mode = "tiny" if tiny else (mode or "left_to_right")
    rects = calculate_grid_positions(
        len(ordered_pkgs),
        w,
        h,
        tiny=tiny,
        top_inset=metrics["top_inset"],
        mode=eff_mode,
        min_size=metrics["min_size"],
    )
    tx_info = _get_set_windowing_mode_tx()
    cmds = []
    for pkg, (l, t, r, b) in zip(ordered_pkgs, rects):
        tid = tasks[pkg]["task_id"]
        parts = [f"am task resizeable {tid} 2 >/dev/null 2>&1"]
        if tx_info:
            svc, tx_code = tx_info
            parts.append(f"service call {svc} {tx_code} i32 {tid} i32 5 i32 1 >/dev/null 2>&1")
        parts.append(f"cmd activity task set-windowing-mode {tid} 5 >/dev/null 2>&1")
        parts.append(f"wm task windowing-mode {tid} 5 >/dev/null 2>&1")
        parts.append(f"am task resize {tid} {l} {t} {r} {b} >/dev/null 2>&1 && echo RJ_RESIZED_{tid}")
        cmds.append("; ".join(parts))

    rc, out, _ = su("; ".join(cmds), 15)
    after = get_package_task_details(ordered_pkgs, running_only=True)
    relaunch_cmds = []
    for pkg, (l, t, r, b) in zip(ordered_pkgs, rects):
        info = after.get(pkg) or tasks.get(pkg)
        if not info:
            continue
        if info.get("windowing_mode") == "fullscreen":
            top_act = info.get("top_activity") or ""
            tid = info["task_id"]
            if top_act and "ActivityProtocolLaunch" not in top_act:
                relaunch_cmds.append(
                    f"am start --windowingMode 5 --activity-reorder-to-front --activity-single-top --activity-no-animation -n {shlex.quote(top_act)} >/dev/null 2>&1; "
                    f"am task resize {tid} {l} {t} {r} {b} >/dev/null 2>&1"
                )
    if relaunch_cmds:
        su("; ".join(relaunch_cmds), 15)
        after = get_package_task_details(ordered_pkgs, running_only=True)

    freeform_cnt = sum(1 for p in ordered_pkgs if (after.get(p) or {}).get("windowing_mode") == "freeform")
    if freeform_cnt > 0:
        return freeform_cnt
    if rc == 0 and out:
        return out.count("RJ_RESIZED_")
    return 0


def focus_before_kill(target_pkg):
    """Shift Android window focus to Termux before stopping target_pkg to avoid launcher glitches."""
    tasks = get_package_task_ids([target_pkg], running_only=True)
    if target_pkg in tasks:
        rc, _, _ = su(
            "am start --activity-reorder-to-front -n com.termux/com.termux.app.TermuxActivity >/dev/null 2>&1",
            8,
        )
        return rc == 0
    return False


def stop_package(pkg, use_focus=True):
    """Stop package safely with optional pre-stop focus shift."""
    if use_focus:
        focus_before_kill(pkg)
    return force_stop(pkg)


def remove_package_tasks(pkg):
    """Remove Android task stack belonging to pkg (used when retiring deleted accounts)."""
    rc, out, _ = su("am stack list", 10)
    if rc != 0 or not out:
        return False
    parsed = _parse_stack_and_task_ids(out, [pkg])
    info = parsed.get(pkg)
    if not info:
        return False
    sid = info.get("stack_id")
    if sid is not None and sid > 0:
        rc2, _, _ = su(f"am stack remove {sid}", 8)
        return rc2 == 0
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
