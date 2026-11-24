"""/storage/emulated/0/Download hot-folder sync & Shouko environment importer."""
from contextlib import contextmanager
import fcntl
import os
import shutil

from rjcore import (
    CHANGE_CONFIG,
    SHARED_AUTOEXEC,
    SHARED_WORKSPACE,
    atomic_write,
    load_change_config,
    load_config,
    parse_cookie_line,
    save_change_config,
    save_config,
)
from sysutils import scan_executor_dirs

DEFAULT_DOWNLOAD_DIR = "/storage/emulated/0/Download"
DEFAULT_HUB_DIR = "/storage/emulated/0/Download/RejoinTool"
DEFAULT_SHOUKO_DIR = "/storage/emulated/0/Download/Shouko"


def _parent_accessible(target_dir):
    parent = os.path.dirname(os.path.abspath(target_dir))
    return bool(parent and os.path.isdir(parent))


@contextmanager
def _pool_lock(path):
    lock_path = path + ".pool.lock"
    os.makedirs(os.path.dirname(os.path.abspath(lock_path)), exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o660)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


def ensure_hub_dirs(hub_dir=DEFAULT_HUB_DIR):
    """Create /storage/emulated/0/Download/RejoinTool/{Autoexec,Workspace,switched} if Download exists."""
    if not _parent_accessible(hub_dir):
        return False
    try:
        for sub in ("", "Autoexec", "Workspace", "switched"):
            p = os.path.join(hub_dir, sub) if sub else hub_dir
            os.makedirs(p, exist_ok=True)
            try:
                os.chmod(p, 0o770)
            except OSError:
                pass
        return True
    except OSError:
        return False


def ingest_hot_cookies(store, hot_files=None, hub_dir=DEFAULT_HUB_DIR, download_dir=DEFAULT_DOWNLOAD_DIR):
    """Atomically ingest dropped cookies from Download/cookie.txt and RejoinTool inbox files."""
    if hot_files is None:
        hot_files = [
            os.path.join(download_dir, "cookie.txt"),
            os.path.join(hub_dir, "cookies_inbox.txt"),
            os.path.join(hub_dir, "cookie.txt"),
        ]

    total_added = 0
    for path in hot_files:
        if not path or not os.path.isfile(path):
            continue
        try:
            if os.path.getsize(path) == 0:
                continue
        except OSError:
            continue

        try:
            with _pool_lock(path):
                with open(path, encoding="utf-8", errors="ignore") as f:
                    raw = f.read()
                if not raw.strip():
                    continue
                has_cookie = any(parse_cookie_line(l)["cookie"] for l in raw.splitlines())
                if not has_cookie:
                    continue
                added = store.add_cookies(raw)
                total_added += added
                atomic_write(path, "", mode=0o660)
        except OSError:
            continue
    return total_added


def _copy_if_changed(src, dst, mode=0o660):
    if not os.path.isfile(src):
        return False
    try:
        with open(src, "rb") as f:
            src_bytes = f.read()
        if os.path.isfile(dst):
            with open(dst, "rb") as f2:
                if f2.read() == src_bytes:
                    return False
        os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)
        tmp = dst + ".tmp"
        with open(tmp, "wb") as out:
            out.write(src_bytes)
        try:
            os.chmod(tmp, mode)
        except OSError:
            pass
        os.replace(tmp, dst)
        return True
    except OSError:
        return False


def mirror_outputs_to_download(store, change_config_path=None, hub_dir=DEFAULT_HUB_DIR):
    """Mirror done.txt, dead.txt, switched/*.txt, and config-change.json into Download/RejoinTool."""
    if not ensure_hub_dirs(hub_dir):
        return 0

    copied = 0
    for src, rel in (
        (store.done_path, "done.txt"),
        (store.dead_path, "dead.txt"),
        (change_config_path or CHANGE_CONFIG, "config-change.json"),
    ):
        if src and _copy_if_changed(src, os.path.join(hub_dir, rel)):
            copied += 1

    if os.path.isdir(store.switched_dir):
        dst_sw = os.path.join(hub_dir, "switched")
        for fname in sorted(os.listdir(store.switched_dir)):
            if fname.endswith(".txt"):
                if _copy_if_changed(os.path.join(store.switched_dir, fname), os.path.join(dst_sw, fname)):
                    copied += 1
    return copied


def import_shouko_environment(
    store,
    cfg=None,
    shouko_dir=DEFAULT_SHOUKO_DIR,
    download_dir=DEFAULT_DOWNLOAD_DIR,
    change_config_path=None,
    shared_autoexec=None,
    shared_workspace=None,
):
    """Safely import plain-text configs, cookies, links, and Lua scripts from Shouko/Download."""
    summary = {
        "cookies_added": 0,
        "change_config_imported": False,
        "autoexec_copied": 0,
        "workspace_copied": 0,
        "links_imported": 0,
    }

    cc_dst = change_config_path or CHANGE_CONFIG
    for cc_cand in (
        os.path.join(download_dir, "config-change.json"),
        os.path.join(shouko_dir, "config-change.json"),
    ):
        if os.path.isfile(cc_cand):
            conf = load_change_config(cc_cand)
            save_change_config(conf, path=cc_dst)
            summary["change_config_imported"] = True
            break

    summary["cookies_added"] += ingest_hot_cookies(
        store,
        hot_files=[
            os.path.join(download_dir, "cookie.txt"),
            os.path.join(shouko_dir, "cookie.txt"),
            os.path.join(shouko_dir, "cookies.txt"),
        ],
    )

    s_ae = shared_autoexec or SHARED_AUTOEXEC
    sh_ae = os.path.join(shouko_dir, "Autoexec")
    if os.path.isdir(sh_ae):
        os.makedirs(s_ae, exist_ok=True)
        for fname in sorted(os.listdir(sh_ae)):
            if not (fname.endswith(".lua") or fname.endswith(".lua.disabled")):
                continue
            if fname == "rejoin_heartbeat.lua":
                continue
            src = os.path.join(sh_ae, fname)
            if os.path.isfile(src):
                shutil.copy2(src, os.path.join(s_ae, fname))
                summary["autoexec_copied"] += 1

    s_ws = shared_workspace or SHARED_WORKSPACE
    sh_ws = os.path.join(shouko_dir, "Workspace")
    if os.path.isdir(sh_ws):
        for root, _dirs, files in os.walk(sh_ws):
            rel = os.path.relpath(root, sh_ws)
            dst_dir = s_ws if rel == "." else os.path.join(s_ws, rel)
            os.makedirs(dst_dir, exist_ok=True)
            for fname in sorted(files):
                if fname.endswith((".lock", ".log", ".tmp")):
                    continue
                src = os.path.join(root, fname)
                shutil.copy2(src, os.path.join(dst_dir, fname))
                summary["workspace_copied"] += 1

    if cfg is not None:
        for links_cand in (
            os.path.join(shouko_dir, "server_links.txt"),
            os.path.join(download_dir, "server_links.txt"),
        ):
            if not os.path.isfile(links_cand):
                continue
            by_pkg = {i["package"]: i for i in cfg.get("instances", []) if i.get("package")}
            with open(links_cand, encoding="utf-8", errors="ignore") as f:
                for raw in f:
                    s = raw.strip()
                    if not s or s.startswith("#") or "," not in s:
                        continue
                    pkg, uri = [x.strip() for x in s.split(",", 1)]
                    if not pkg or not uri:
                        continue
                    if pkg in by_pkg:
                        by_pkg[pkg]["target"] = uri
                        summary["links_imported"] += 1
                    else:
                        idx = len(cfg["instances"]) + 1
                        execs = scan_executor_dirs(pkg=pkg)
                        cfg["instances"].append({
                            "name": f"inst{idx}",
                            "package": pkg,
                            "profile": "default",
                            "target": uri,
                            "workspace": execs[0]["workspace"] if execs else "",
                            "autoexec": execs[0]["autoexec"] if execs else "",
                            "use_cookie_queue": True,
                            "enabled": True,
                        })
                        summary["links_imported"] += 1
            if summary["links_imported"] > 0:
                save_config(cfg)
            break

    return summary
