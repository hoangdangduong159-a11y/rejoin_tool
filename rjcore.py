"""Core helpers: android/root, Roblox API, cookie DB, appStorage sync, files, logging, credential store."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import fcntl
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import threading
import time
from collections import deque
import zipfile

import requests

BASE = os.path.dirname(os.path.abspath(__file__))
TMP = os.path.join(BASE, ".tmp")
LOGDIR = os.path.join(BASE, "logs")
PROFILES = os.path.join(BASE, "profiles")
CONFIG = os.path.join(BASE, "config.json")
CHANGE_CONFIG = os.path.join(BASE, "config-change.json")
USERNAMES_CACHE = os.path.join(BASE, "usernames.json")
BLOCK_CACHE = os.path.join(BASE, "block.json")
GOFILE_TOKEN = os.path.join(BASE, ".gofile_token")
SHARED_AUTOEXEC = os.path.join(BASE, "shared", "Autoexec")
SHARED_WORKSPACE = os.path.join(BASE, "shared", "Workspace")
COOKIES = os.path.join(BASE, "cookies.txt")
DONE = os.path.join(BASE, "done.txt")
DEAD = os.path.join(BASE, "dead.txt")
STATE = os.path.join(BASE, "state.json")

DEFAULT_WORKSPACE = "/storage/emulated/0/Delta/Workspace"
DEFAULT_AUTOEXEC = "/storage/emulated/0/Delta/Autoexecute"
DEFAULT_EXPORT_PATH = "/storage/emulated/0/Download/extracted_cookies.txt"

COOKIE_DB_CANDIDATES = [
    "app_webview/Default/Cookies",
    "app_webview/Cookies",
    "databases/Cookies",
]

COOKIE_RE = re.compile(r"_\|WARNING:[^\s;\"']+")
UA = {"User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 Chrome/120 Mobile Safari/537.36"}

_EXCLUDED_PACKAGES = {
    "com.termux",
    "com.termux.boot",
    "com.quanghuy.rejoin",
    "bin.mt.plus",
    "ru.zdevs.zarchiver",
    "org.swiftapps.swiftbackup",
    "com.cloudflare.onedotonedotonedotone",
    "com.og.launcher",
    "com.og.toolcenter",
    "com.og.gamecenter",
    "com.lwi.android.flapps",
    "ahapps.controlthescreenorientation",
}
_ROBLOX_PKG_RE = re.compile(
    r"^(com\.roblox|com\.vng\.roblox|free\.noka|com\.delta|com\.codex|com\.arceus|com\.fluxus|com\.cryptic|com\.vegax|vip\.|.*samehwid|.*clone)",
    re.IGNORECASE,
)

_VNG_CACHE = {}
_VNG_LOCK = threading.Lock()
_MOD_CACHE = {}
_MOD_LOCK = threading.Lock()

for _d in (TMP, LOGDIR, PROFILES, os.path.join(PROFILES, "lua"), SHARED_AUTOEXEC, SHARED_WORKSPACE):
    os.makedirs(_d, exist_ok=True)


# ───────────────────────── logging ─────────────────────────
def mask(text):
    return COOKIE_RE.sub(lambda m: m.group(0)[:20] + "…<cookie>", str(text))


class Logger:
    def __init__(self, echo=False):
        self.lines = deque(maxlen=300)
        self.echo = echo
        self.lock = threading.Lock()

    def __call__(self, tag, msg):
        line = f"{time.strftime('%H:%M:%S')} [{tag}] {mask(msg)}"
        with self.lock:
            self.lines.append(line)
            if self.echo:
                print(line, flush=True)
            try:
                with open(os.path.join(LOGDIR, "rejoin.log"), "a") as f:
                    f.write(time.strftime("%Y-%m-%d ") + line + "\n")
            except OSError:
                pass


# ───────────────────────── json / files ─────────────────────────
def load_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def atomic_write(path, text, mode=0o600):
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(tmp, "w") as f:
        f.write(text)
    try:
        os.chmod(tmp, mode)
    except OSError:
        pass
    os.replace(tmp, path)


def save_json(path, data):
    atomic_write(path, json.dumps(data, indent=2, ensure_ascii=False), 0o644)


DEFAULT_CHANGE_CONFIG = {
    "level": 0,
    "beli": 0,
    "fragment": 0,
    "bounty/honor": 0,
    "tier": 0,
    "race_ver": 0,
    "pull_level": False,
    "god_human": False,
    "sanguine_art": False,
    "cursed_dual_katana": False,
    "true_triple_katana": False,
    "shark_anchor": False,
    "skull_guitar": False,
    "mirror_fractal": False,
    "valkyrie_helm": False,
}

_CHANGE_INT_KEYS = ("level", "beli", "fragment", "bounty/honor", "tier")
_CHANGE_BOOL_KEYS = (
    "pull_level",
    "god_human",
    "sanguine_art",
    "cursed_dual_katana",
    "true_triple_katana",
    "shark_anchor",
    "skull_guitar",
    "mirror_fractal",
    "valkyrie_helm",
)
_CHANGE_ALIASES = {
    "level": ("level",),
    "beli": ("beli",),
    "fragment": ("fragment", "fragments"),
    "bounty/honor": ("bounty/honor", "bounty", "honor"),
    "tier": ("tier",),
    "race_ver": ("race_ver", "race"),
    "pull_level": ("pull_level", "pull_lever", "pulllever"),
    "god_human": ("god_human", "godhuman"),
    "sanguine_art": ("sanguine_art", "sanguineart"),
    "cursed_dual_katana": ("cursed_dual_katana", "cdk"),
    "true_triple_katana": ("true_triple_katana", "ttk"),
    "shark_anchor": ("shark_anchor", "sharkanchor"),
    "skull_guitar": ("skull_guitar", "skullguitar", "soul_guitar"),
    "mirror_fractal": ("mirror_fractal", "mirrorfractal"),
    "valkyrie_helm": ("valkyrie_helm", "valkyriehelm"),
}


def sanitize_switch_type(raw_type):
    """Sanitize a custom account-change category name for switched/Completed-<Type>.txt."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(raw_type or "").strip()).strip("._-")


def parse_custom_change_trigger(text):
    """Parse Workspace custom account-change file (<User>.txt/.json or Yummy/Shouko trigger)."""
    empty = {"matched": False, "switch_type": "", "reason": ""}
    raw = (text or "").strip()
    if not raw:
        return empty

    try:
        obj = json.loads(raw)
    except ValueError:
        obj = None

    if isinstance(obj, dict):
        raw_type = obj.get("Type") if "Type" in obj else obj.get("type")
        done_flag = obj.get("Completed", obj.get("completed"))
        st = str(obj.get("status") or "").strip().lower()
        has_type = raw_type is not None and str(raw_type).strip() != ""
        if not (has_type or done_flag is True or st in ("done", "completed")):
            return empty
        sw = sanitize_switch_type(raw_type)
        reason = str(obj.get("Reason") or obj.get("reason") or (f"Custom ({sw})" if sw else "Custom completed")).strip()
        return {"matched": True, "switch_type": sw, "reason": reason}

    first = ""
    for line in raw.splitlines():
        s = line.strip()
        if s:
            first = s
            break
    if not first:
        return empty

    low = first.lower()
    if low.startswith("completed-"):
        sw = sanitize_switch_type(first[len("Completed-"):])
        return {"matched": True, "switch_type": sw, "reason": f"Custom ({sw})" if sw else "Custom completed"}
    if low.startswith("yummy-"):
        sw = sanitize_switch_type(first[len("Yummy-"):])
        return {"matched": True, "switch_type": sw, "reason": f"Custom ({sw})" if sw else "Custom completed"}
    if low in ("completed", "done", "yummy", "acc_ok"):
        return {"matched": True, "switch_type": "", "reason": "Custom completed"}
    return empty


def _normalize_race_ver(val):
    if val is None or isinstance(val, bool):
        return 0
    if isinstance(val, (int, float)):
        return max(0, int(val))
    s = str(val).strip()
    if not s or s == "0":
        return 0
    if s.isdigit():
        return max(0, int(s))
    return s


def _extract_race_tier(val):
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        iv = int(val)
        return iv if 1 <= iv <= 4 else None
    s = str(val).strip()
    if s.isdigit():
        iv = int(s)
        return iv if 1 <= iv <= 4 else None
    m = re.search(r"\bv?([1-4])\b", s, re.IGNORECASE)
    return int(m.group(1)) if m else None


def load_change_config(path=None):
    """Load config-change.json (Blox Fruits auto-change criteria), coercing invalid fields safely."""
    p = path or CHANGE_CONFIG
    raw = load_json(p, None)
    conf = dict(DEFAULT_CHANGE_CONFIG)
    if not isinstance(raw, dict):
        return conf
    for k in _CHANGE_INT_KEYS:
        try:
            conf[k] = max(0, int(raw.get(k, conf[k]) or 0))
        except (TypeError, ValueError):
            conf[k] = 0
    for k in _CHANGE_BOOL_KEYS:
        val = raw.get(k, conf[k])
        if isinstance(val, str):
            conf[k] = val.strip().lower() in ("true", "1", "yes", "on")
        else:
            conf[k] = bool(val)
    conf["race_ver"] = _normalize_race_ver(raw.get("race_ver", conf["race_ver"]))
    return conf


def save_change_config(conf, path=None):
    """Validate and save config-change.json."""
    normalized = dict(DEFAULT_CHANGE_CONFIG)
    if isinstance(conf, dict):
        for k in _CHANGE_INT_KEYS:
            try:
                normalized[k] = max(0, int(conf.get(k, 0) or 0))
            except (TypeError, ValueError):
                normalized[k] = 0
        for k in _CHANGE_BOOL_KEYS:
            val = conf.get(k, False)
            if isinstance(val, str):
                normalized[k] = val.strip().lower() in ("true", "1", "yes", "on")
            else:
                normalized[k] = bool(val)
        normalized["race_ver"] = _normalize_race_ver(conf.get("race_ver", 0))
    save_json(path or CHANGE_CONFIG, normalized)
    return normalized


def _lookup_info_value(info, key):
    low_map = {str(k).lower(): v for k, v in info.items()}
    for alias in _CHANGE_ALIASES.get(key, (key,)):
        if alias.lower() in low_map:
            return low_map[alias.lower()]
    return None


def info_matches_change_config(info, conf):
    """Match <User>-info.json or hb['stats'] against config-change.json. Returns (matched, reasons)."""
    if not isinstance(info, dict) or not info or not isinstance(conf, dict):
        return False, []

    active_checks = 0
    reasons = []

    for k in _CHANGE_INT_KEYS:
        try:
            req = int(conf.get(k) or 0)
        except (TypeError, ValueError):
            req = 0
        if req > 0:
            active_checks += 1
            actual = _lookup_info_value(info, k)
            try:
                num = float(actual)
            except (TypeError, ValueError):
                return False, []
            if num < req:
                return False, []
            reasons.append(f"{k}>={req}")

    for k in _CHANGE_BOOL_KEYS:
        if bool(conf.get(k)):
            active_checks += 1
            actual = _lookup_info_value(info, k)
            if isinstance(actual, bool):
                ok = actual
            elif isinstance(actual, (int, float)):
                ok = actual != 0
            elif isinstance(actual, str):
                ok = actual.strip().lower() in ("true", "1", "yes", "on")
            else:
                ok = False
            if not ok:
                return False, []
            reasons.append(k)

    req_race = _normalize_race_ver(conf.get("race_ver"))
    if req_race:
        active_checks += 1
        actual_race = _lookup_info_value(info, "race_ver")
        actual_tier = _extract_race_tier(actual_race)
        if actual_tier is None:
            actual_tier = _extract_race_tier(_lookup_info_value(info, "tier"))
        if isinstance(req_race, int):
            if actual_tier is None or actual_tier < req_race:
                return False, []
            reasons.append(f"race_v>={req_race}")
        else:
            req_tier = _extract_race_tier(req_race)
            req_name = re.sub(r"\bv?[1-4]\b", "", str(req_race), flags=re.IGNORECASE).strip()
            if req_name:
                if actual_race is None or req_name.lower() not in str(actual_race).lower():
                    return False, []
            if req_tier is not None:
                if actual_tier is None or actual_tier < req_tier:
                    return False, []
            reasons.append(f"race={req_race}")

    return active_checks > 0, reasons


def load_config():
    cfg = load_json(CONFIG, None)
    if cfg is None or not isinstance(cfg, dict):
        cfg = {"webhook": "", "stagger_launch_seconds": 8, "instances": []}
    cfg.setdefault("webhook", "")
    cfg.setdefault("webhook_change_url", "")
    cfg.setdefault("webhook_alert_url", "")
    cfg.setdefault("device_name", "")
    cfg.setdefault("webhook_ping", "")
    cfg.setdefault("webhook_interval_minutes", 0)
    cfg.setdefault("webhook_screenshot", False)
    cfg.setdefault("auto_clean_ram_minutes", 0)
    cfg.setdefault("stay_awake", False)
    cfg.setdefault("stagger_launch_seconds", 8)
    cfg.setdefault("open_delay_seconds", 2)
    cfg.setdefault("sleep_after_kill_seconds", 2)
    cfg.setdefault("sort_tabs", True)
    cfg.setdefault("tiny_sort_tabs", False)
    cfg.setdefault("sort_tabs_interval_seconds", 15)
    cfg.setdefault("kill_all_on_crash", False)
    cfg.setdefault("change_acc_cus", True)
    cfg.setdefault("change_acc_bf", False)
    cfg.setdefault("auto_block_accounts", False)
    cfg.setdefault("focus_before_kill", True)
    cfg.setdefault("sync_shared_scripts", True)
    cfg.setdefault("package_prefix", "")
    cfg.setdefault("web_enabled", False)
    cfg.setdefault("web_host", "127.0.0.1")
    cfg.setdefault("web_port", 8787)
    cfg.setdefault("web_token", "")
    cfg.setdefault("download_hub_sync", True)
    cfg.setdefault("download_hub_dir", "/storage/emulated/0/Download/RejoinTool")
    cfg.setdefault("auto_dismiss_anr", True)
    cfg.setdefault("server_hop_on_disconnect", False)
    cfg.setdefault("error_264_action", "cooldown")
    cfg.setdefault("instances", [])
    if os.path.exists(CONFIG) and not cfg.get("_auto_resize_v5"):
        cfg["sort_tabs"] = True
        cfg["_auto_resize_v5"] = True
        save_config(cfg)
    return cfg


def save_config(cfg):
    save_json(CONFIG, cfg)


DEFAULT_PROFILE = {
    "place_id": 0,
    "private_link": "",
    "job_id": "",
    "rejoin_after_minutes": 30,
    "rotate_after_minutes": 0,
    "heartbeat_timeout": 45,
    "launch_grace": 90,
    "poll_interval": 3,
    "max_rejoins_before_rotate": 0,
    "check_method": "hybrid",
    "check_presence": False,
    "presence_recheck_strikes": 3,
    "presence_poll_seconds": 30,
    "open_splash_first": False,
    "on_challenge": "pause",
    "retire_on_deleted": False,
    "clear_cache_on_rejoin": False,
    "custom_change_watch": True,
    "server_hop": False,
    "max_ram_mb": 0,
    "fps_cap": 0,
    "window_mode": "freeform",
    "rotate_on": ["status:done"],
    "rules": [],
    "when_no_cookies": "wait",
    "lua": "",
    "launch_extra": "",
}


def load_profile(name):
    p = dict(DEFAULT_PROFILE)
    data = load_json(os.path.join(PROFILES, f"{name}.json"), None)
    if data is None:
        raise FileNotFoundError(f"profile '{name}' not found in {PROFILES}")
    p.update(data)
    p["name"] = name
    return p


def list_profiles():
    return sorted(f[:-5] for f in os.listdir(PROFILES) if f.endswith(".json"))


# ───────────────────────── android / root ─────────────────────────
def _clean_android_env():
    env = dict(os.environ)
    env.pop("LD_LIBRARY_PATH", None)
    env.pop("LD_PRELOAD", None)
    sys_path = "/system/bin:/system/xbin:/vendor/bin"
    cur_path = env.get("PATH", "")
    if sys_path not in cur_path:
        env["PATH"] = f"{sys_path}:{cur_path}" if cur_path else sys_path
    return env


def su(cmd, timeout=30, clean_env=True):
    wrapped = (
        f"unset LD_LIBRARY_PATH LD_PRELOAD; "
        f"export PATH=/system/bin:/system/xbin:/vendor/bin:$PATH; {cmd}"
        if clean_env
        else cmd
    )
    env = _clean_android_env() if clean_env else None
    argv = ["/system/bin/sh", "-c", wrapped] if os.geteuid() == 0 else ["su", "-c", wrapped]
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=env)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except FileNotFoundError:
        return 127, "", "su not found"


def has_root():
    return su("echo ok", 15)[1] == "ok"


def force_stop(pkg, verify=True):
    res = su(f"am force-stop {shlex.quote(pkg)}")
    if not verify:
        return res
    if is_running(pkg):
        time.sleep(0.5)
        if is_running(pkg):
            q_pkg = shlex.quote(pkg)
            su(f"P=$(pidof {q_pkg}); [ -n \"$P\" ] && kill -9 $P 2>/dev/null; am force-stop {q_pkg}", 15)
    return res


def is_running(pkg):
    return su(f"pidof {shlex.quote(pkg)}", 10)[0] == 0


def launch(pkg, uri, extra="", open_splash_first=False, splash_delay=2.0):
    extra_part = f" {extra.strip()}" if extra and extra.strip() else ""
    q_pkg = shlex.quote(pkg)
    q_uri = shlex.quote(uri)
    if open_splash_first:
        splash_comp = shlex.quote(f"{pkg}/com.roblox.client.startup.ActivitySplash")
        su(f"am start{extra_part} -n {splash_comp}", 20)
        if splash_delay and float(splash_delay) > 0:
            time.sleep(float(splash_delay))
        proto_comp = shlex.quote(f"{pkg}/com.roblox.client.ActivityProtocolLaunch")
        rc, out, err = su(
            f"am start{extra_part} -a android.intent.action.VIEW -n {proto_comp} -d {q_uri}",
            30,
        )
        combined = f"{out}\n{err}".lower()
        if rc == 0 and "does not exist" not in combined and "error:" not in combined:
            return rc, out, err
    return su(
        f"am start{extra_part} -a android.intent.action.VIEW -d {q_uri} -p {q_pkg}",
        30,
    )


def detect_packages(prefix=""):
    """Packages that handle roblox:// links or match known Roblox/executor prefixes."""
    pk = []
    rc, out, _ = su(
        "cmd package query-activities --brief -a android.intent.action.VIEW -d 'roblox://placeId=1'",
        30,
    )
    if rc == 0:
        for line in out.splitlines():
            line = line.strip()
            if "/" in line and not line.startswith("priority"):
                p = line.split("/")[0]
                if p and p not in _EXCLUDED_PACKAGES and p not in pk:
                    pk.append(p)

    rc2, out2, _ = su("pm list packages -3", 20)
    if rc2 == 0:
        pref = (prefix or "").strip().lower()
        for line in out2.splitlines():
            line = line.strip()
            if not line.startswith("package:"):
                continue
            p = line[len("package:"):].strip()
            if not p or p in _EXCLUDED_PACKAGES or p in pk:
                continue
            if pref:
                if p.lower().startswith(pref):
                    pk.append(p)
            elif _ROBLOX_PKG_RE.match(p):
                pk.append(p)
    return pk


def get_installed_third_party_packages():
    """Return set of installed 3rd-party Android package names, or None if pm check is unavailable."""
    rc, out, _ = su("pm list packages -3", 15)
    if rc != 0 or not out:
        return None
    installed = set()
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("package:"):
            p = line[len("package:"):].strip()
            if p:
                installed.add(p)
    return installed if installed else None


def sync_installed_instances(cfg=None, store=None, auto_add=True, prune_uninstalled=True, persist=True):
    """Synchronize config['instances'] with actually installed Android Roblox/clone packages."""
    if os.environ.get("RJ_SKIP_PKG_SYNC") == "1" or not has_root():
        return {"added": [], "removed": [], "changed": False}

    installed = get_installed_third_party_packages()
    if installed is None:
        return {"added": [], "removed": [], "changed": False}

    cfg_obj = cfg if cfg is not None else load_config()
    store_obj = store if store is not None else Store()
    instances = list(cfg_obj.get("instances") or [])
    kept = []
    removed = []
    removed_names = []

    if prune_uninstalled:
        for inst in instances:
            pkg = (inst.get("package") or "").strip()
            if not pkg:
                kept.append(inst)
                continue
            if pkg in installed:
                kept.append(inst)
                continue
            rc_p, out_p, _ = su(f"pm path {shlex.quote(pkg)}", 8)
            if rc_p == 0 and "package:" in out_p:
                installed.add(pkg)
                kept.append(inst)
            else:
                removed.append(pkg)
                if inst.get("name"):
                    removed_names.append(inst["name"])
    else:
        kept = instances

    if removed_names:
        store_obj.ensure_files()
        kept_names = {i.get("name") for i in kept if i.get("name")}
        all_assigned = store_obj.all_assigned()
        in_use_cookies = {
            v.get("cookie")
            for k, v in all_assigned.items()
            if k in kept_names and isinstance(v, dict) and v.get("cookie")
        }
        existing_queue = set(store_obj.queue())
        for rname in removed_names:
            assigned = all_assigned.get(rname)
            if isinstance(assigned, dict):
                ck = assigned.get("cookie")
                if ck and ck not in in_use_cookies and ck not in existing_queue:
                    store_obj.return_tail(ck)
                    existing_queue.add(ck)
            store_obj.set_assign(rname, None)

    added = []
    if auto_add:
        existing_pkgs = {i.get("package") for i in kept if i.get("package")}
        used_names = {i.get("name") for i in kept if i.get("name")}
        detected = detect_packages(prefix=cfg_obj.get("package_prefix", ""))
        if detected:
            from sysutils import scan_executor_dirs
            for p in detected:
                if p not in existing_pkgs:
                    idx = len(kept) + 1
                    name = suggest_instance_name(p, idx, used_names=used_names)
                    used_names.add(name)
                    existing_pkgs.add(p)
                    execs = scan_executor_dirs(pkg=p)
                    kept.append({
                        "name": name,
                        "package": p,
                        "profile": "default",
                        "target": "",
                        "workspace": execs[0]["workspace"] if execs else DEFAULT_WORKSPACE,
                        "autoexec": execs[0]["autoexec"] if execs else DEFAULT_AUTOEXEC,
                        "use_cookie_queue": True,
                        "enabled": True,
                    })
                    added.append(p)

    changed = bool(added or removed)
    if changed:
        cfg_obj["instances"] = kept
        if persist:
            save_config(cfg_obj)
    return {"added": added, "removed": removed, "changed": changed}


def read_clone_metadata(pkg):
    """Read AppCloner cloneSettings.json metadata (app_name, clone_number, badge) if present."""
    if not pkg:
        return {}
    path = f"/data/data/{pkg}/files/cloneSettings.json"
    data = load_json(path, None)
    if data is None:
        rc, out, _ = su(f"test -f {shlex.quote(path)} && cat {shlex.quote(path)}", 8)
        if rc == 0 and out:
            try:
                data = json.loads(out)
            except ValueError:
                data = None
    if not isinstance(data, dict):
        return {}
    return {
        "app_name": str(data.get("name") or "").strip(),
        "clone_number": data.get("cloneNumber"),
        "badge": str(data.get("badge") or "").strip(),
    }


def suggest_instance_name(pkg, index=1, metadata=None, used_names=None):
    """Choose a readable instance name from AppCloner metadata or package name."""
    meta = metadata if metadata is not None else read_clone_metadata(pkg)
    used = set(used_names or ())
    cnum = meta.get("clone_number")
    badge = str(meta.get("badge") or "").strip()
    if isinstance(cnum, int) and cnum > 0:
        cand = f"clone{cnum}"
    elif badge.isdigit():
        cand = f"clone{badge}"
    elif "noka" in (pkg or "").lower():
        cand = f"noka{index}"
    else:
        cand = f"inst{index}"
    if cand not in used:
        return cand
    suffix = 2
    while f"{cand}_{suffix}" in used:
        suffix += 1
    return f"{cand}_{suffix}"


def is_vng_package(pkg):
    """Detect whether pkg is a Roblox VNG build (uses robloxapp.vnggames.com_appStorage.json)."""
    if not pkg:
        return False
    with _VNG_LOCK:
        if pkg in _VNG_CACHE:
            return _VNG_CACHE[pkg]

    vng = False
    vng_file = f"/data/data/{pkg}/files/appData/LocalStorage/robloxapp.vnggames.com_appStorage.json"
    rc, out, _ = su(f"test -f {shlex.quote(vng_file)} && echo yes", 10)
    if rc == 0 and out == "yes":
        vng = True
    elif "vng" in pkg.lower():
        vng = True
    else:
        rc2, out2, _ = su(f"pm path {shlex.quote(pkg)}", 10)
        if rc2 == 0 and out2:
            for line in out2.splitlines():
                apk_path = line.replace("package:", "").strip()
                if apk_path and os.path.isfile(apk_path):
                    try:
                        with zipfile.ZipFile(apk_path, "r") as zf:
                            for name in zf.namelist():
                                if name.endswith(".dex"):
                                    if b"robloxapp.vnggames.com" in zf.read(name):
                                        vng = True
                                        break
                    except (OSError, zipfile.BadZipFile):
                        pass
                if vng:
                    break

    with _VNG_LOCK:
        _VNG_CACHE[pkg] = vng
    return vng


def get_app_storage_filename(pkg):
    return "robloxapp.vnggames.com_appStorage.json" if is_vng_package(pkg) else "appStorage.json"


# ───────────────────────── cookie parsing ─────────────────────────
def extract_cookie(text):
    m = COOKIE_RE.search(text)
    if m:
        return m.group(0)
    t = text.strip()
    if len(t) > 150 and " " not in t and "|" not in t and ":" not in t:
        return t
    return None


def parse_cookie_line(line):
    """Parse raw cookie, user|cookie, user|id|pkg|cookie, or user:pass:cookie formats."""
    raw = (line or "").strip()
    out = {"username": None, "password": None, "cookie": None, "raw": raw}
    if not raw or raw.startswith("#"):
        return out

    m = COOKIE_RE.search(raw)
    if m:
        ck = m.group(0)
        out["cookie"] = ck
        prefix = raw[:m.start()]
        if "|" in prefix:
            parts = [p.strip() for p in prefix.split("|") if p.strip()]
            if parts:
                out["username"] = parts[0]
        elif prefix.endswith(":"):
            parts = prefix[:-1].split(":")
            if len(parts) >= 2:
                out["username"] = parts[0].strip() or None
                out["password"] = ":".join(parts[1:]).strip() or None
            elif len(parts) == 1 and parts[0].strip():
                out["username"] = parts[0].strip()
        return out

    # Fallback: pipe or colon separated without standard warning header
    if "|" in raw:
        parts = [p.strip() for p in raw.split("|")]
        for p in parts:
            if len(p) > 150 and " " not in p:
                out["cookie"] = p
                if parts[0] and parts[0] != p:
                    out["username"] = parts[0]
                return out
    if ":" in raw:
        parts = raw.split(":")
        if len(parts) >= 3 and len(parts[-1].strip()) > 150 and " " not in parts[-1]:
            out["username"] = parts[0].strip() or None
            out["password"] = ":".join(parts[1:-1]).strip() or None
            out["cookie"] = parts[-1].strip()
            return out

    ck = extract_cookie(raw)
    if ck:
        out["cookie"] = ck
    return out


def short(cookie):
    return (cookie[:12] + "…" + cookie[-4:]) if cookie else "-"


# ───────────────────────── Roblox API ─────────────────────────
def _ck(cookie):
    h = dict(UA)
    h["Cookie"] = f".ROBLOSECURITY={cookie}"
    return h


def whoami(cookie):
    """Return (status, user_id, username): status in ok/dead/error."""
    try:
        r = requests.get("https://users.roblox.com/v1/users/authenticated", headers=_ck(cookie), timeout=15)
        if r.status_code == 200:
            d = r.json()
            return "ok", int(d["id"]), d["name"]
        if r.status_code in (401, 403):
            return "dead", None, None
        return "error", None, None
    except Exception:
        return "error", None, None


def _classify_moderation(cookie, timeout=12):
    """Passively inspect Roblox usermoderation endpoints on 403 (cached 60s, never reactivates or solves)."""
    now = time.time()
    ck_key = (cookie or "")[:40]
    with _MOD_LOCK:
        cached = _MOD_CACHE.get(ck_key)
        if cached and (now - cached[0]) < 60.0:
            return cached[1]

    headers = _ck(cookie)
    res = None
    for url in (
        "https://usermoderation.roblox.com/v2/not-approved",
        "https://usermoderation.roblox.com/v1/not-approved",
    ):
        try:
            r = requests.get(url, headers=headers, timeout=timeout)
        except Exception:
            continue
        if r.status_code != 200:
            continue
        try:
            d = r.json() if r.content else {}
        except ValueError:
            d = {}
        if not isinstance(d, dict) or not d:
            continue
        p_type = str(d.get("punishmentTypeDescription") or d.get("punishmentType") or "").lower()
        reason = str(d.get("reason") or d.get("messageToUser") or p_type or "")
        end_date = d.get("endDate")
        if "delete" in p_type or "terminat" in p_type:
            res = {"state": "deleted", "reason": reason or "account_deleted", "end_date": end_date}
            break
        if "ban" in p_type:
            res = {"state": "banned", "reason": reason or "account_banned", "end_date": end_date}
            break
        if "warn" in p_type:
            res = {"state": "warning", "reason": reason or "account_warning", "end_date": end_date}
            break

    with _MOD_LOCK:
        _MOD_CACHE[ck_key] = (now, res)
    return res


def inspect_account_health(cookie, timeout=12):
    """Passively classify account health: ok / expired / banned / deleted / warning / captcha / faceid / captcha_lock / error."""
    base = {"state": "error", "user_id": None, "username": None, "reason": "", "end_date": None}
    if not cookie:
        base["state"] = "expired"
        base["reason"] = "empty_cookie"
        return base

    try:
        r = requests.get("https://users.roblox.com/v1/users/authenticated", headers=_ck(cookie), timeout=timeout)
    except Exception as e:
        base["reason"] = str(e)
        return base

    ch_type = str(r.headers.get("rblx-challenge-type") or "").strip().lower()
    if ch_type:
        if "biometric" in ch_type or "face" in ch_type:
            return {**base, "state": "faceid", "reason": ch_type}
        if "captchalock" in ch_type:
            return {**base, "state": "captcha_lock", "reason": ch_type}
        if any(k in ch_type for k in ("captcha", "proofofwork", "proofofspace")):
            return {**base, "state": "captcha", "reason": ch_type}

    if r.status_code == 200:
        try:
            d = r.json()
            return {
                "state": "ok",
                "user_id": int(d["id"]),
                "username": d["name"],
                "reason": "",
                "end_date": None,
            }
        except (ValueError, KeyError, TypeError):
            return {**base, "state": "error", "reason": "invalid_json"}

    if r.status_code == 401:
        return {**base, "state": "expired", "reason": "http_401"}

    if r.status_code == 403:
        mod = _classify_moderation(cookie, timeout=timeout)
        if mod:
            return {**base, **mod}
        # Secondary check against account-unlock endpoint headers
        try:
            r2 = requests.post(
                "https://apis.roblox.com/account-unlock-api/v1/unlock",
                headers={**_ck(cookie), "Accept": "application/json", "Content-Type": "application/json"},
                json={},
                timeout=timeout,
            )
            ch2 = str(r2.headers.get("rblx-challenge-type") or "").strip().lower()
            if "biometric" in ch2 or "face" in ch2:
                return {**base, "state": "faceid", "reason": ch2}
            if "captchalock" in ch2:
                return {**base, "state": "captcha_lock", "reason": ch2}
            if "captcha" in ch2:
                return {**base, "state": "captcha", "reason": ch2}
        except Exception:
            pass
        return {**base, "state": "expired", "reason": "http_403"}

    if r.status_code == 429 or r.status_code >= 500:
        return {**base, "state": "error", "reason": f"http_{r.status_code}"}

    return {**base, "state": "error", "reason": f"http_{r.status_code}"}


def check_presence(user_id, cookie):
    """Return (userPresenceType, placeId, gameId) or (None, None, None) on error (with RoProxy fallback)."""
    payload = {"userIds": [int(user_id)]}
    headers = {**_ck(cookie), "Content-Type": "application/json"}
    for url in (
        "https://presence.roblox.com/v1/presence/users",
        "https://presence.roproxy.com/v1/presence/users",
    ):
        try:
            r = requests.post(url, json=payload, headers=headers, timeout=15)
            if r.status_code == 200:
                presences = r.json().get("userPresences", [])
                if presences:
                    p = presences[0]
                    return p.get("userPresenceType", 0), p.get("placeId"), p.get("gameId")
            elif r.status_code not in (429, 500, 502, 503, 504):
                break
        except Exception:
            continue
    return None, None, None


def get_username_by_id(user_id):
    """Resolve username from usernames.json cache or Roblox API with RoProxy fallback."""
    uid_s = str(int(user_id))
    cache = load_json(USERNAMES_CACHE, {})
    if isinstance(cache, dict) and cache.get(uid_s):
        return cache[uid_s]
    for url in (
        f"https://users.roblox.com/v1/users/{uid_s}",
        f"https://users.roproxy.com/v1/users/{uid_s}",
    ):
        try:
            r = requests.get(url, headers=UA, timeout=12)
            if r.status_code == 200:
                d = r.json()
                if d.get("name"):
                    if not isinstance(cache, dict):
                        cache = {}
                    cache[uid_s] = d["name"]
                    save_json(USERNAMES_CACHE, cache)
                    return d["name"]
        except Exception:
            continue
    return None


def block_roblox_user(cookie, target_user_id, timeout=12):
    """Block target_user_id so multiple clones avoid matching the same public server."""
    if not cookie or not target_user_id:
        return False
    h = {
        **_ck(cookie),
        "Accept": "application/json",
        "Origin": "https://www.roblox.com",
        "Referer": "https://www.roblox.com/",
    }
    url = f"https://apis.roblox.com/user-blocking-api/v1/users/{int(target_user_id)}/block-user"
    try:
        r = requests.post(url, headers=h, timeout=timeout)
        if r.status_code == 403 and r.headers.get("x-csrf-token"):
            h["X-CSRF-Token"] = r.headers["x-csrf-token"]
            r = requests.post(url, headers=h, timeout=timeout)
        return r.status_code in (200, 204)
    except Exception:
        return False


def logout_cookie(cookie):
    """Invalidate a cookie session via Roblox logout endpoint (with X-CSRF-Token retry)."""
    if not cookie:
        return False
    h = _ck(cookie)
    url = "https://auth.roblox.com/v2/logout"
    try:
        r = requests.post(url, headers=h, timeout=15)
        if r.status_code == 403 and r.headers.get("x-csrf-token"):
            h["x-csrf-token"] = r.headers["x-csrf-token"]
            r = requests.post(url, headers=h, timeout=15)
        return r.status_code == 200
    except Exception:
        return False


def resolve_share(cookie, code):
    """share?code=... (type=Server) -> (place_id, link_code) or (None, None)."""
    h = _ck(cookie)
    h["Content-Type"] = "application/json"
    body = {"linkId": code, "linkType": "Server"}
    try:
        r = requests.post("https://apis.roblox.com/sharelinks/v1/resolve-link", json=body, headers=h, timeout=15)
        if r.status_code == 403 and r.headers.get("x-csrf-token"):
            h["x-csrf-token"] = r.headers["x-csrf-token"]
            r = requests.post("https://apis.roblox.com/sharelinks/v1/resolve-link", json=body, headers=h, timeout=15)
        if r.status_code == 200:
            d = r.json().get("privateServerInviteData") or {}
            if d.get("placeId") and d.get("linkCode"):
                return str(d["placeId"]), d["linkCode"]
    except Exception:
        pass
    return None, None


def pick_public_server(place_id, cookie=None, exclude_jobs=None, prefer_low_players=True, timeout=12):
    """Pick an available public server job_id for place_id (with RoProxy fallback), avoiding exclude_jobs."""
    if not place_id:
        return None
    excluded = {str(j) for j in (exclude_jobs or ()) if j}
    headers = _ck(cookie) if cookie else dict(UA)
    order = "Asc" if prefer_low_players else "Desc"
    for base_host in ("https://games.roblox.com", "https://games.roproxy.com"):
        url = f"{base_host}/v1/games/{int(place_id)}/servers/Public?sortOrder={order}&limit=100"
        try:
            r = requests.get(url, headers=headers, timeout=timeout)
            if r.status_code != 200:
                continue
            servers = r.json().get("data") or []
            candidates = []
            for srv in servers:
                sid = str(srv.get("id") or "").strip()
                playing = int(srv.get("playing") or 0)
                max_p = int(srv.get("maxPlayers") or 0)
                if not sid or sid in excluded:
                    continue
                if max_p > 1 and playing >= max_p - 1:
                    continue
                candidates.append((playing, sid))
            if candidates:
                candidates.sort(key=lambda x: x[0], reverse=not prefer_low_players)
                return candidates[0][1]
        except Exception:
            continue
    return None


def validate_cookies_parallel(cookies, max_workers=8):
    """Validate a list of cookies concurrently via inspect_account_health()."""
    items = [c for c in (cookies or []) if c]
    if not items:
        return []
    workers = max(1, min(int(max_workers or 8), len(items)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(inspect_account_health, items))
    return [{"cookie": ck, **res} for ck, res in zip(items, results)]


def parse_target(text):
    """Parse game id / games URL / private-server URL / share URL / roblox:// URI."""
    t = (text or "").strip()
    out = {"place_id": None, "link_code": None, "share_code": None}
    if not t:
        return out
    if t.isdigit():
        out["place_id"] = t
        return out
    m = re.search(r"placeId=(\d+)", t, re.IGNORECASE)
    if m:
        out["place_id"] = m.group(1)
    if not out["place_id"]:
        m = re.search(r"roblox\.com/(?:[a-z]{2}/)?games/(\d+)", t)
        if m:
            out["place_id"] = m.group(1)
    m = re.search(r"(?:privateServerLinkCode|linkCode)=([^&\s]+)", t)
    if m:
        out["link_code"] = m.group(1)
    m = re.search(r"share\?code=([^&\s]+)", t)
    if m:
        out["share_code"] = m.group(1)
    return out


def build_uri(place_id, link_code=None, job_id=None):
    uri = f"roblox://placeId={place_id}"
    if link_code:
        uri += f"&linkCode={link_code}"
    elif job_id:
        uri += f"&gameInstanceId={job_id}"
    return uri


def notify(webhook, text):
    if not webhook:
        return
    try:
        requests.post(webhook, json={"content": mask(text)[:1900]}, timeout=10)
    except Exception:
        pass


# ───────────────────────── cookie DB & appStorage ─────────────────────────
_CHROMIUM_V14_SCHEMA = """
CREATE TABLE IF NOT EXISTS cookies (
    creation_utc INTEGER NOT NULL,
    host_key TEXT NOT NULL,
    name TEXT NOT NULL,
    value TEXT NOT NULL,
    path TEXT NOT NULL,
    expires_utc INTEGER NOT NULL,
    is_secure INTEGER NOT NULL,
    is_httponly INTEGER NOT NULL,
    last_access_utc INTEGER NOT NULL,
    has_expires INTEGER NOT NULL DEFAULT 1,
    is_persistent INTEGER NOT NULL DEFAULT 1,
    priority INTEGER NOT NULL DEFAULT 1,
    encrypted_value BLOB DEFAULT '',
    samesite INTEGER NOT NULL DEFAULT -1,
    source_scheme INTEGER NOT NULL DEFAULT 0,
    source_port INTEGER NOT NULL DEFAULT -1,
    is_same_party INTEGER NOT NULL DEFAULT 0,
    top_frame_site_key TEXT NOT NULL DEFAULT '',
    UNIQUE (host_key, name, path)
);
CREATE TABLE IF NOT EXISTS meta (
    key LONGVARCHAR NOT NULL UNIQUE PRIMARY KEY,
    value LONGVARCHAR
);
INSERT OR IGNORE INTO meta VALUES ('mmap_status', '-1');
INSERT OR IGNORE INTO meta VALUES ('version', '14');
INSERT OR IGNORE INTO meta VALUES ('last_compatible_version', '14');
"""


def cookie_db_path(pkg):
    for rel in COOKIE_DB_CANDIDATES:
        cand = f"/data/data/{pkg}/{rel}"
        rc, out, _ = su(f"test -f {shlex.quote(cand)} && echo yes", 10)
        if rc == 0 and out == "yes":
            return cand
    return f"/data/data/{pkg}/app_webview/Default/Cookies"


def _cleanup_local_db(local):
    for ext in ("", "-journal", "-wal", "-shm", ".staged"):
        try:
            os.remove(local + ext)
        except OSError:
            pass


def _fetch_db(pkg):
    local = os.path.join(TMP, f"{pkg}_{os.getpid()}_{threading.get_ident()}.db")
    _cleanup_local_db(local)
    src = cookie_db_path(pkg)
    q_src = shlex.quote(src)
    q_local = shlex.quote(local)
    rc, _, _ = su(f"cp {q_src} {q_local} && chmod 666 {q_local}")
    if rc != 0 or not os.path.exists(local):
        return None
    for ext in ("-journal", "-wal", "-shm"):
        su(
            f"test -f {shlex.quote(src + ext)} && "
            f"cp {shlex.quote(src + ext)} {shlex.quote(local + ext)} && "
            f"chmod 666 {shlex.quote(local + ext)}"
        )
    return local


def read_db_cookie(local):
    con = sqlite3.connect(local)
    try:
        rows = con.execute(
            "SELECT value, length(encrypted_value) FROM cookies "
            "WHERE name='.ROBLOSECURITY' ORDER BY last_access_utc DESC, length(value) DESC"
        ).fetchall()
    finally:
        con.close()
    for v, _ in rows:
        if v:
            return v
    return None


def patch_db_cookie(local, cookie):
    """Set .ROBLOSECURITY inside a local copy (or brand-new Chromium v14 DB) of Cookies."""
    con = sqlite3.connect(local)
    try:
        has_tbl = con.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='cookies'"
        ).fetchone()[0]
        if not has_tbl:
            con.executescript(_CHROMIUM_V14_SCHEMA)

        cols = {r[1]: r for r in con.execute("PRAGMA table_info(cookies)")}
        exp = int((time.time() + 365 * 86400 + 11644473600) * 1_000_000)
        now = int((time.time() + 11644473600) * 1_000_000)
        n = con.execute("SELECT COUNT(*) FROM cookies WHERE name='.ROBLOSECURITY'").fetchone()[0]
        if n:
            sets = {
                "host_key": ".roblox.com",
                "value": cookie,
                "encrypted_value": b"",
                "creation_utc": now,
                "last_access_utc": now,
                "expires_utc": exp,
                "has_expires": 1,
                "is_persistent": 1,
            }
            sets = {k: v for k, v in sets.items() if k in cols}
            q = "UPDATE cookies SET " + ",".join(f"{k}=?" for k in sets) + " WHERE name='.ROBLOSECURITY'"
            con.execute(q, list(sets.values()))
        else:
            vals = {
                "creation_utc": now,
                "host_key": ".roblox.com",
                "name": ".ROBLOSECURITY",
                "value": cookie,
                "encrypted_value": b"",
                "path": "/",
                "expires_utc": exp,
                "is_secure": 1,
                "is_httponly": 1,
                "last_access_utc": now,
                "has_expires": 1,
                "is_persistent": 1,
                "priority": 1,
                "samesite": -1,
                "source_scheme": 2,
                "source_port": 443,
                "last_update_utc": now,
                "top_frame_site_key": "",
            }
            for c, r in cols.items():
                if c not in vals and r[3] and r[4] is None:
                    ctype = (r[2] or "").upper()
                    vals[c] = "" if ("TEXT" in ctype or "CHAR" in ctype) else 0
            vals = {k: v for k, v in vals.items() if k in cols}
            con.execute(
                f"INSERT INTO cookies ({','.join(vals)}) VALUES ({','.join('?' * len(vals))})",
                list(vals.values()),
            )
        con.commit()
        try:
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            con.execute("PRAGMA journal_mode=DELETE")
        except sqlite3.Error:
            pass
    finally:
        con.close()


def read_app_cookie(pkg):
    local = _fetch_db(pkg)
    if not local:
        return None
    try:
        return read_db_cookie(local)
    except sqlite3.Error:
        return None
    finally:
        _cleanup_local_db(local)


def read_app_storage_identity(pkg):
    """Read (user_id, username) from appStorage.json / vng_appStorage.json."""
    ls_dir = f"/data/data/{pkg}/files/appData/LocalStorage"
    dst = f"{ls_dir}/{get_app_storage_filename(pkg)}"
    local = os.path.join(TMP, f"{pkg}_{os.getpid()}_{threading.get_ident()}_readas.json")
    rc, out, _ = su(f"test -f {shlex.quote(dst)} && cp {shlex.quote(dst)} {shlex.quote(local)} && chmod 666 {shlex.quote(local)} && echo yes", 10)
    try:
        if rc == 0 and out == "yes" and os.path.exists(local):
            d = load_json(local, {})
            if isinstance(d, dict) and d.get("UserId") and str(d["UserId"]) != "-1":
                uid = int(d["UserId"]) if str(d["UserId"]).isdigit() else None
                return uid, (d.get("Username") or None)
    finally:
        try:
            os.remove(local)
        except OSError:
            pass
    return None, None


def sync_app_storage(pkg, user_id, username, display_name=None):
    """Sync Roblox cached LocalStorage/(vng_)appStorage.json with the injected account."""
    ls_dir = f"/data/data/{pkg}/files/appData/LocalStorage"
    fname = get_app_storage_filename(pkg)
    dst = f"{ls_dir}/{fname}"
    q_dst = shlex.quote(dst)
    q_ls_dir = shlex.quote(ls_dir)
    q_pkg_dir = shlex.quote(f"/data/data/{pkg}")

    local = os.path.join(TMP, f"{pkg}_{os.getpid()}_{threading.get_ident()}_appStorage.json")
    q_local = shlex.quote(local)
    try:
        os.remove(local)
    except OSError:
        pass

    rc, out, _ = su(f"test -f {q_dst} && echo yes", 10)
    data = {}
    if rc == 0 and out == "yes":
        rc_cp, _, _ = su(f"cp {q_dst} {q_local} && chmod 666 {q_local}", 15)
        if rc_cp == 0 and os.path.exists(local):
            try:
                with open(local) as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    data = loaded
            except (OSError, ValueError):
                data = {}

    data["UserId"] = str(user_id)
    data["Username"] = str(username)
    data["DisplayName"] = str(display_name or username)
    data["AccountBlob"] = ""

    try:
        atomic_write(local, json.dumps(data, separators=(",", ":"), ensure_ascii=False), 0o666)
    except OSError:
        return False

    q_staged = shlex.quote(dst + ".staged")
    rc2, _, _ = su(
        f"U=$(stat -c %u:%g {q_pkg_dir}) && mkdir -p {q_ls_dir} && "
        f"chown -R $U {shlex.quote(f'/data/data/{pkg}/files')} 2>/dev/null || true && "
        f"cp {q_local} {q_staged} && chown $U {q_staged} && chmod 660 {q_staged} && "
        f"mv -f {q_staged} {q_dst} && restorecon {q_dst} 2>/dev/null; true",
        15,
    )
    try:
        os.remove(local)
    except OSError:
        pass
    return rc2 == 0


def _ensure_template_cookie_db():
    """Create and cache a clean Chromium v14 template SQLite DB for instant fresh-clone injection."""
    tpl = os.path.join(TMP, "template_cookies_v14.db")
    if os.path.isfile(tpl) and os.path.getsize(tpl) > 0:
        return tpl
    tmp_tpl = f"{tpl}.{os.getpid()}.{threading.get_ident()}.tmp"
    con = sqlite3.connect(tmp_tpl)
    try:
        con.executescript(_CHROMIUM_V14_SCHEMA)
        con.commit()
    finally:
        con.close()
    os.replace(tmp_tpl, tpl)
    return tpl


def write_app_cookie(pkg, cookie, user_id=None, username=None, display_name=None):
    """Inject cookie atomically into app's WebView DB (even on fresh clones) and sync appStorage. App MUST be stopped."""
    local = _fetch_db(pkg)
    fresh = False
    if not local:
        fresh = True
        local = os.path.join(TMP, f"{pkg}_{os.getpid()}_{threading.get_ident()}_fresh.db")
        _cleanup_local_db(local)
        try:
            tpl = _ensure_template_cookie_db()
            shutil.copy2(tpl, local)
        except (OSError, sqlite3.Error):
            pass
    try:
        if not fresh:
            bak = os.path.join(TMP, f"{pkg}.db.bak")
            with open(local, "rb") as a, open(bak, "wb") as b:
                b.write(a.read())
        patch_db_cookie(local, cookie)
    except sqlite3.Error:
        _cleanup_local_db(local)
        return False

    dst = cookie_db_path(pkg)
    dst_dir = os.path.dirname(dst)
    wv_dir = os.path.dirname(dst_dir)
    q_dst = shlex.quote(dst)
    q_staged = shlex.quote(dst + ".staged")
    q_local = shlex.quote(local)
    q_pkg_dir = shlex.quote(f"/data/data/{pkg}")
    su(
        f"U=$(stat -c %u:%g {q_pkg_dir}) && "
        f"mkdir -p {shlex.quote(dst_dir)} && "
        f"chown $U {shlex.quote(wv_dir)} {shlex.quote(dst_dir)} 2>/dev/null; "
        f"chmod 770 {shlex.quote(wv_dir)} {shlex.quote(dst_dir)} 2>/dev/null; "
        f"rm -f {shlex.quote(dst + '-journal')} {shlex.quote(dst + '-wal')} {shlex.quote(dst + '-shm')} && "
        f"cp {q_local} {q_staged} && chown $U {q_staged} && chmod 660 {q_staged} && "
        f"mv -f {q_staged} {q_dst} && restorecon {q_dst} 2>/dev/null; true"
    )
    _cleanup_local_db(local)
    ok = read_app_cookie(pkg) == cookie
    if ok and user_id is not None and username:
        sync_app_storage(pkg, user_id, username, display_name)
    return ok


def logout_package(pkg, call_api=False):
    """Stop package, optionally invalidate server session, remove .ROBLOSECURITY and clear appStorage identity."""
    force_stop(pkg)
    if call_api:
        ck = read_app_cookie(pkg)
        if ck:
            logout_cookie(ck)

    local = _fetch_db(pkg)
    if local:
        try:
            con = sqlite3.connect(local)
            try:
                con.execute("DELETE FROM cookies WHERE name='.ROBLOSECURITY'")
                con.commit()
                con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                con.execute("PRAGMA journal_mode=DELETE")
            finally:
                con.close()
            dst = cookie_db_path(pkg)
            q_dst = shlex.quote(dst)
            q_staged = shlex.quote(dst + ".staged")
            q_pkg_dir = shlex.quote(f"/data/data/{pkg}")
            su(
                f"U=$(stat -c %u:%g {q_pkg_dir}) && "
                f"rm -f {shlex.quote(dst + '-journal')} {shlex.quote(dst + '-wal')} {shlex.quote(dst + '-shm')} && "
                f"cp {shlex.quote(local)} {q_staged} && chown $U {q_staged} && chmod 660 {q_staged} && "
                f"mv -f {q_staged} {q_dst} && restorecon {q_dst} 2>/dev/null; true"
            )
        except sqlite3.Error:
            pass
        finally:
            _cleanup_local_db(local)

    # Clear UserId / Username / AccountBlob if appStorage exists
    sync_app_storage(pkg, "-1", "", "")
    return True


def extract_all_cookies(packages=None):
    """Auto-extract and validate cookies from all detected/configured Roblox/Delta packages."""
    if packages is None:
        seen = []
        for p in detect_packages() + [i.get("package") for i in load_config().get("instances", [])]:
            if p and p not in seen:
                seen.append(p)
        packages = seen

    results = []
    for pkg in packages:
        ck = read_app_cookie(pkg)
        if not ck:
            results.append({
                "package": pkg,
                "cookie": None,
                "status": "missing",
                "user_id": None,
                "username": None,
            })
            continue
        st, uid, uname = whoami(ck)
        results.append({
            "package": pkg,
            "cookie": ck,
            "status": st,
            "user_id": uid,
            "username": uname,
        })
    return results


# ───────────────────────── cookie / account store ─────────────────────────
@contextmanager
def _file_lock(lock_path, timeout=10.0):
    os.makedirs(os.path.dirname(os.path.abspath(lock_path)), exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"Lock timeout on {lock_path}")
                time.sleep(0.05)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


class Store:
    SWITCHED_CATEGORIES = ("acc_ok", "dead", "banned", "deleted", "captcha", "faceid", "captcha-lock", "warning")

    def __init__(self, base_dir=None):
        self.lock = threading.RLock()
        base = base_dir or BASE
        self.base_dir = base
        self.cookies_path = os.path.join(base, "cookies.txt")
        self.done_path = os.path.join(base, "done.txt")
        self.dead_path = os.path.join(base, "dead.txt")
        self.state_path = os.path.join(base, "state.json")
        self.switched_dir = os.path.join(base, "switched")
        self.passwords_db = os.path.join(base, "passwords.db")
        self.lock_file = os.path.join(base, "cookies.txt.lock")

    def ensure_files(self):
        with self.lock:
            os.makedirs(self.switched_dir, exist_ok=True)
            for p in (self.cookies_path, self.done_path, self.dead_path):
                if not os.path.exists(p):
                    atomic_write(p, "", 0o600)

    def _open_pw_db(self):
        os.makedirs(self.base_dir, exist_ok=True)
        con = sqlite3.connect(self.passwords_db, timeout=10.0)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute(
            "CREATE TABLE IF NOT EXISTS credentials ("
            "username_key TEXT PRIMARY KEY, "
            "username TEXT NOT NULL, "
            "password TEXT NOT NULL, "
            "updated_at INTEGER NOT NULL)"
        )
        return con

    def save_password(self, username, password):
        if not username or not password:
            return
        with self.lock:
            con = self._open_pw_db()
            try:
                con.execute(
                    "INSERT INTO credentials (username_key, username, password, updated_at) "
                    "VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(username_key) DO UPDATE SET "
                    "username=excluded.username, password=excluded.password, updated_at=excluded.updated_at",
                    (username.lower(), username, password, int(time.time())),
                )
                con.commit()
            finally:
                con.close()

    def get_password(self, username):
        if not username or not os.path.exists(self.passwords_db):
            return None
        with self.lock:
            con = self._open_pw_db()
            try:
                row = con.execute(
                    "SELECT password FROM credentials WHERE username_key = ?",
                    (username.lower(),),
                ).fetchone()
                return row[0] if row else None
            finally:
                con.close()

    def _lines(self, path):
        try:
            with open(path) as f:
                return f.read().splitlines()
        except OSError:
            return []

    def queue(self):
        with self.lock:
            return [c for c in (parse_cookie_line(l)["cookie"] for l in self._lines(self.cookies_path)) if c]

    def counts(self):
        with self.lock:
            return {
                "queue": len(self.queue()),
                "done": sum(1 for l in self._lines(self.done_path) if l.strip()),
                "dead": sum(1 for l in self._lines(self.dead_path) if l.strip()),
            }

    def switched_counts(self):
        with self.lock:
            out = {}
            for cat in self.SWITCHED_CATEGORIES:
                p = os.path.join(self.switched_dir, f"{cat}.txt")
                out[cat] = sum(1 for l in self._lines(p) if l.strip())
            if os.path.isdir(self.switched_dir):
                for fname in sorted(os.listdir(self.switched_dir)):
                    if fname.endswith(".txt"):
                        st_name = fname[:-4]
                        if st_name not in out:
                            p = os.path.join(self.switched_dir, fname)
                            out[st_name] = sum(1 for l in self._lines(p) if l.strip())
            return out

    def add_cookies(self, text):
        with self.lock, _file_lock(self.lock_file):
            existing = set(self.queue())
            new = []
            for l in text.splitlines():
                parsed = parse_cookie_line(l)
                c = parsed["cookie"]
                if parsed["username"] and parsed["password"]:
                    self.save_password(parsed["username"], parsed["password"])
                if c and c not in existing:
                    new.append(c)
                    existing.add(c)
            if new:
                lines = self._lines(self.cookies_path) + new
                atomic_write(self.cookies_path, "\n".join(lines) + "\n")
            return len(new)

    def pop(self):
        with self.lock, _file_lock(self.lock_file):
            lines = self._lines(self.cookies_path)
            for i, l in enumerate(lines):
                c = parse_cookie_line(l)["cookie"]
                if c:
                    del lines[i]
                    atomic_write(self.cookies_path, "\n".join(lines) + ("\n" if lines else ""))
                    return c
            return None

    def push_front(self, cookie):
        with self.lock, _file_lock(self.lock_file):
            c = extract_cookie(cookie)
            if not c:
                return
            lines = [l for l in self._lines(self.cookies_path) if parse_cookie_line(l)["cookie"] != c]
            lines.insert(0, c)
            atomic_write(self.cookies_path, "\n".join(lines) + "\n")

    def return_tail(self, cookie):
        with self.lock, _file_lock(self.lock_file):
            c = extract_cookie(cookie)
            if not c:
                return
            lines = [l for l in self._lines(self.cookies_path) if parse_cookie_line(l)["cookie"] != c]
            lines.append(c)
            atomic_write(self.cookies_path, "\n".join(lines) + "\n")

    def _append(self, path, line):
        with self.lock:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            with open(path, "a") as f:
                f.write(line + "\n")
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass

    def add_switched(self, category, username, cookie, reason="", preserve_case=False):
        if preserve_case:
            cat = sanitize_switch_type(category) or "Completed"
        else:
            cat = re.sub(r"[^a-z0-9_-]+", "_", (category or "acc_ok").lower()).strip("_") or "acc_ok"
        pw = self.get_password(username) if username and username != "?" else None
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        if pw:
            line = f"{username}|{pw}|{cookie}|{ts}|{reason}"
        else:
            line = f"{username or '?'}|{cookie}|{ts}|{reason}"
        self._append(os.path.join(self.switched_dir, f"{cat}.txt"), line)

    def add_done(self, username, cookie, reason, also_acc_ok=True):
        self._append(self.done_path, f"{username}|{cookie}|{time.strftime('%Y-%m-%d %H:%M:%S')}|{reason}")
        if also_acc_ok:
            self.add_switched("acc_ok", username, cookie, reason)

    def add_custom_completed(self, switch_type, username, cookie, reason=""):
        sw = sanitize_switch_type(switch_type)
        cat = f"Completed-{sw}" if sw else "Completed"
        self.add_done(username, cookie, reason, also_acc_ok=False)
        self.add_switched(cat, username, cookie, reason, preserve_case=True)
        return cat

    def add_dead(self, username, cookie, reason):
        self._append(self.dead_path, f"{username or '?'}|{cookie}|{time.strftime('%Y-%m-%d %H:%M:%S')}|{reason}")
        self.add_switched("dead", username, cookie, reason)

    def export_extracted(self, records, out_path, fmt="full"):
        lines = []
        for r in records:
            if r.get("status") == "ok" and r.get("cookie"):
                if fmt == "raw":
                    lines.append(r["cookie"])
                elif fmt == "user_cookie":
                    lines.append(f"{r.get('username') or '?'}|{r['cookie']}")
                else:
                    lines.append(f"{r.get('username') or '?'}|{r.get('user_id') or ''}|{r.get('package') or ''}|{r['cookie']}")
        atomic_write(out_path, "\n".join(lines) + ("\n" if lines else ""), mode=0o600)
        return len(lines)

    def _state(self):
        return load_json(self.state_path, {"assigned": {}})

    def all_assigned(self):
        with self.lock:
            return dict(self._state().get("assigned", {}))

    def get_assign(self, inst):
        with self.lock:
            return self._state().get("assigned", {}).get(inst)

    def set_assign(self, inst, data):
        with self.lock:
            st = self._state()
            st.setdefault("assigned", {})
            if data is None:
                st["assigned"].pop(inst, None)
            else:
                st["assigned"][inst] = data
            atomic_write(self.state_path, json.dumps(st, indent=2))

    def prune_invalid_cookies(self, max_workers=8, validator=None):
        """Concurrently validate cookies in cookies.txt and archive dead/challenged entries."""
        q = self.queue()
        if not q:
            return {"total": 0, "ok": 0, "dead": 0, "challenge": 0, "error": 0, "details": []}

        if validator is None:
            details = validate_cookies_parallel(q, max_workers=max_workers)
        else:
            details = [{"cookie": ck, **validator(ck)} for ck in q]

        kept = []
        summary = {"total": len(details), "ok": 0, "dead": 0, "challenge": 0, "error": 0, "details": details}
        for item in details:
            ck = item["cookie"]
            st = item.get("state") or "error"
            uname = item.get("username") or "?"
            reason = item.get("reason") or st
            if st == "ok":
                kept.append(ck)
                summary["ok"] += 1
            elif st == "error":
                kept.append(ck)
                summary["error"] += 1
            elif st in ("expired", "banned", "deleted", "warning"):
                summary["dead"] += 1
                self.add_dead(uname, ck, f"prune_{reason}")
                if st != "expired":
                    self.add_switched(st, uname, ck, reason)
            elif st in ("captcha", "faceid", "captcha_lock"):
                summary["challenge"] += 1
                self.add_switched(st.replace("_", "-"), uname, ck, reason)
            else:
                kept.append(ck)
                summary["error"] += 1

        with self.lock, _file_lock(self.lock_file):
            atomic_write(self.cookies_path, "\n".join(kept) + ("\n" if kept else ""))
        return summary
