"""Interactive Rich menu/dashboard + Headless CLI + Daemon/IPC client for multi-instance Roblox Auto-Rejoin v2."""
import argparse
import json
import os
import shlex
import signal
import subprocess
import sys
import time

from rich import box
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.prompt import Confirm, IntPrompt, Prompt
from rich.table import Table

from engine import Manager
from ipc import DEFAULT_IPC_SOCK, ipc_request
from lua_deploy import HEARTBEAT_FILENAME, deploy_all_executors, deploy_heartbeat
from rjcore import (
    BASE,
    CHANGE_CONFIG,
    CONFIG,
    DEFAULT_AUTOEXEC,
    DEFAULT_CHANGE_CONFIG,
    DEFAULT_EXPORT_PATH,
    DEFAULT_PROFILE,
    DEFAULT_WORKSPACE,
    LOGDIR,
    PROFILES,
    TMP,
    Store,
    build_uri,
    detect_packages,
    extract_all_cookies,
    force_stop,
    has_root,
    launch,
    list_profiles,
    load_change_config,
    load_config,
    load_profile,
    logout_package,
    parse_target,
    read_app_cookie,
    save_change_config,
    save_config,
    save_json,
    short,
    su,
    whoami,
    write_app_cookie,
)
from sysutils import (
    GoFileDownloader,
    add_autoexec_script,
    apply_low_graphics,
    capture_screenshot,
    clear_app_cache,
    delete_autoexec_script,
    get_batch_pkg_usage,
    get_system_stats,
    kill_all_packages,
    list_autoexec_scripts,
    scan_executor_dirs,
    send_discord_embed,
    set_stay_awake,
    sort_roblox_tabs,
    toggle_autoexec_script,
)

console = Console()


def bootstrap():
    """Ensure config.json, config-change.json, default profile, and store files exist."""
    Store().ensure_files()
    default_prof = os.path.join(PROFILES, "default.json")
    if not os.path.exists(default_prof):
        save_json(default_prof, dict(DEFAULT_PROFILE))
    if not os.path.exists(CHANGE_CONFIG):
        save_change_config(DEFAULT_CHANGE_CONFIG)

    if not os.path.exists(CONFIG):
        pkgs = detect_packages() or ["free.nokaA"]
        instances = []
        for i, pkg in enumerate(pkgs, 1):
            name = f"noka{i}" if "noka" in pkg.lower() else f"inst{i}"
            instances.append({
                "name": name,
                "package": pkg,
                "profile": "default",
                "target": "",
                "workspace": DEFAULT_WORKSPACE,
                "autoexec": DEFAULT_AUTOEXEC,
                "use_cookie_queue": True,
                "enabled": True,
            })
        save_config({"webhook": "", "stagger_launch_seconds": 8, "instances": instances})


def _fmt_sec(sec):
    if sec is None:
        return "-"
    s = int(max(0, sec))
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def _build_dashboard_layout(rows, counts, sys_st, log_lines, switched=None, title_suffix=""):
    sw_txt = ""
    if switched:
        parts = [f"{k}:{v}" for k, v in switched.items() if v > 0]
        if parts:
            sw_txt = "  |  Switched: " + " ".join(parts)

    table = Table(
        title=(
            f"Roblox Multi-Instance Monitor{title_suffix}  |  Queue: {counts['queue']}  Done: {counts['done']}  Dead: {counts['dead']}{sw_txt}\n"
            f"[dim]CPU: {sys_st.get('cpu_pct', 0):.0f}%  |  RAM: {sys_st['ram_used_mb']:.0f}/{sys_st['ram_total_mb']:.0f}MB ({sys_st['ram_pct']:.0f}%)  |  "
            f"Tool: {sys_st.get('tool_ram_mb', 0):.0f}MB  |  Disk Free: {sys_st['disk_free_gb']}GB  |  Load: {sys_st['load_1m']}[/dim]"
        ),
        box=box.ROUNDED,
        border_style="bright_blue",
    )
    table.add_column("Instance", style="bold cyan")
    table.add_column("Package", style="dim")
    table.add_column("Profile", style="magenta")
    table.add_column("Account", style="green")
    table.add_column("Status")
    table.add_column("RAM", justify="right")
    table.add_column("HB/Chk", justify="right")
    table.add_column("Next Rejoin", justify="right")
    table.add_column("Rejoins/Rot", justify="right")
    table.add_column("Stats / Last Event", style="dim")

    status_colors = {
        "RUNNING": "green",
        "LAUNCHING": "yellow",
        "WAITING_COOKIE": "yellow",
        "REJOINING": "bright_red",
        "ROTATING": "magenta",
        "PAUSED_CHALLENGE": "yellow",
        "RETIRED": "red",
        "STOPPED": "dim",
        "ERROR": "red",
        "IDLE": "cyan",
    }

    for row in rows:
        st = row["status"]
        col = status_colors.get(st, "white")
        if row.get("check_method") == "online":
            hb_str = f"online({row.get('presence_strikes', 0)})"
        else:
            hb_str = f"{row['hb_age']:.0f}s" if row.get("hb_age") is not None else "-"
        ram_str = f"{row['ram_mb']:.0f}MB" if row.get("ram_mb") is not None else "-"
        stats_str = ", ".join(f"{k}={v}" for k, v in list((row.get("stats") or {}).items())[:3])
        info = stats_str or row.get("last_event") or "-"
        table.add_row(
            row["name"],
            row["package"],
            row["profile_name"],
            row["username"],
            f"[{col}]{st}[/{col}]",
            ram_str,
            hb_str,
            _fmt_sec(row.get("next_rejoin_in")),
            f"{row.get('rejoin_count', 0)}/{row.get('rotate_count', 0)}",
            info[:40],
        )

    log_panel = Panel(
        "\n".join(log_lines) or "[dim]Waiting for events...[/dim]",
        title="Recent Logs (Ctrl+C to detach/stop)",
        border_style="dim",
    )

    layout = Layout()
    layout.split_column(
        Layout(table, name="top", ratio=2),
        Layout(log_panel, name="bottom", ratio=1),
    )
    return layout


def _render_dashboard(mgr, store):
    with mgr.logger.lock:
        log_lines = list(mgr.logger.lines)[-10:]
    return _build_dashboard_layout(
        mgr.snapshot(),
        store.counts(),
        get_system_stats(),
        log_lines,
        switched=store.switched_counts(),
    )


# ───────────────────────── CLI commands ─────────────────────────
def cmd_detect(args):
    cfg = load_config()
    pkgs = detect_packages(prefix=cfg.get("package_prefix", ""))
    if not pkgs:
        console.print("[yellow]No packages handling roblox:// links found.[/yellow]")
        return 1
    table = Table(title="Detected Roblox / Executor Packages", box=box.ROUNDED)
    table.add_column("#", justify="right", style="cyan")
    table.add_column("Package", style="bold green")
    for idx, p in enumerate(pkgs, 1):
        table.add_row(str(idx), p)
    console.print(table)

    if args.add:
        existing = {i["package"] for i in cfg["instances"]}
        added = 0
        for p in pkgs:
            if p not in existing:
                idx = len(cfg["instances"]) + 1
                name = f"noka{idx}" if "noka" in p.lower() else f"inst{idx}"
                execs = scan_executor_dirs(pkg=p)
                cfg["instances"].append({
                    "name": name,
                    "package": p,
                    "profile": "default",
                    "target": "",
                    "workspace": execs[0]["workspace"] if execs else DEFAULT_WORKSPACE,
                    "autoexec": execs[0]["autoexec"] if execs else DEFAULT_AUTOEXEC,
                    "use_cookie_queue": True,
                    "enabled": True,
                })
                added += 1
        save_config(cfg)
        console.print(f"[green]Added {added} new instance(s) to config.json[/green]")
    return 0


def cmd_deploy_lua(args):
    extra_path = None
    if args.profile:
        prof = load_profile(args.profile)
        if prof.get("lua"):
            extra_path = prof["lua"] if os.path.isabs(prof["lua"]) else os.path.join(PROFILES, "lua", prof["lua"])
    if getattr(args, "all", False):
        cfg = load_config()
        pkgs = [i.get("package") for i in cfg.get("instances", []) if i.get("package")]
        deployed = deploy_all_executors(extra_lua_path=extra_path, packages=pkgs)
    else:
        deployed = deploy_heartbeat(args.autoexec or DEFAULT_AUTOEXEC, extra_path)
    for d in deployed:
        console.print(f"[green]Deployed:[/green] {d}")
    return 0


def cmd_status(_args):
    cfg = load_config()
    store = Store()
    root_ok = has_root()
    counts = store.counts()
    sw = store.switched_counts()
    sys_st = get_system_stats()
    hb_ok = os.path.exists(os.path.join(DEFAULT_AUTOEXEC, HEARTBEAT_FILENAME))
    daemon_ping = ipc_request("ping", timeout=1.0)
    daemon_txt = f"[green]RUNNING (pid={daemon_ping['result'].get('pid')})[/]" if daemon_ping.get("ok") else "[dim]STOPPED[/]"
    sw_non_zero = "  ".join(f"{k}={v}" for k, v in sw.items() if v > 0) or "-"

    console.print(
        Panel(
            f"Root: [{'green' if root_ok else 'red'}]{'YES' if root_ok else 'NO'}[/]  |  "
            f"Daemon IPC: {daemon_txt}  |  "
            f"Autoexec Heartbeat: [{'green' if hb_ok else 'yellow'}]{'DEPLOYED' if hb_ok else 'MISSING'}[/]\n"
            f"Cookies Queue: [cyan]{counts['queue']}[/]  Done: [green]{counts['done']}[/]  Dead: [red]{counts['dead']}[/]  |  Switched: [dim]{sw_non_zero}[/]\n"
            f"CPU: [cyan]{sys_st['cpu_pct']:.0f}%[/]  |  RAM: [cyan]{sys_st['ram_used_mb']:.0f}/{sys_st['ram_total_mb']:.0f}MB ({sys_st['ram_pct']:.0f}%)[/]  |  "
            f"Disk Free: [cyan]{sys_st['disk_free_gb']}/{sys_st['disk_total_gb']}GB[/]  |  Load: {sys_st['load_1m']}",
            title="Rejoin Tool Status",
            border_style="cyan",
        )
    )

    table = Table(title="Configured Instances", box=box.ROUNDED)
    table.add_column("Name", style="bold cyan")
    table.add_column("Package")
    table.add_column("Profile", style="magenta")
    table.add_column("Target", style="dim")
    table.add_column("Enabled")
    table.add_column("Assigned Account", style="green")

    assigned = store.all_assigned()
    for inst in cfg["instances"]:
        a = assigned.get(inst["name"]) or {}
        acc = f"{a.get('username', '-')} ({short(a.get('cookie'))})" if a else "-"
        table.add_row(
            inst["name"],
            inst["package"],
            inst.get("profile", "default"),
            (inst.get("target") or "-")[:28],
            "[green]yes[/]" if inst.get("enabled", True) else "[dim]no[/]",
            acc,
        )
    console.print(table)
    console.print(f"[dim]Profiles: {', '.join(list_profiles()) or '-'}[/dim]")
    return 0


def cmd_cookies(args):
    store = Store()
    if args.action == "list":
        q = store.queue()
        c = store.counts()
        console.print(f"Queue: {c['queue']} | Done: {c['done']} | Dead: {c['dead']}")
        for idx, ck in enumerate(q, 1):
            console.print(f"  {idx}. {short(ck)}")
        return 0
    if args.action == "switched":
        sw = store.switched_counts()
        table = Table(title="Categorized Switched Accounts (switched/*.txt)", box=box.ROUNDED)
        table.add_column("Category", style="cyan")
        table.add_column("Count", justify="right", style="green")
        for k, v in sw.items():
            table.add_row(k, str(v))
        console.print(table)
        return 0
    if args.action == "add":
        if args.file:
            with open(args.file) as f:
                text = f.read()
        elif args.cookie:
            text = args.cookie
        else:
            text = sys.stdin.read()
        n = store.add_cookies(text)
        console.print(f"[green]Added {n} unique cookie(s) to cookies.txt[/green]")
        return 0
    if args.action == "validate":
        q = store.queue()
        valid = 0
        for ck in q:
            st, _uid, uname = whoami(ck)
            console.print(f"  {short(ck)} -> {st} ({uname or '-'})")
            if st == "ok":
                valid += 1
        console.print(f"Valid: {valid}/{len(q)}")
        return 0
    if args.action == "fix":
        cfg = load_config()
        donor = getattr(args, "donor", None)
        pkgs = [i["package"] for i in cfg["instances"] if i.get("package")]
        if donor:
            ck = read_app_cookie(donor)
            if not ck:
                console.print(f"[red]No cookie found in donor {donor}[/red]")
                return 1
            st, uid, uname = whoami(ck)
            if st != "ok":
                console.print(f"[red]Donor cookie invalid (status={st})[/red]")
                return 1
            for pkg in pkgs:
                if pkg != donor:
                    force_stop(pkg)
                    ok = write_app_cookie(pkg, ck, uid, uname)
                    console.print(f"  {pkg} <- {uname}: {'[green]OK[/]' if ok else '[red]FAIL[/]'}")
            return 0
        # Otherwise re-sync appStorage for all valid logged-in packages
        for r in extract_all_cookies(pkgs):
            if r["status"] == "ok":
                from rjcore import sync_app_storage
                ok = sync_app_storage(r["package"], r["user_id"], r["username"])
                console.print(f"  Synced appStorage for {r['package']} ({r['username']}): {'[green]OK[/]' if ok else '[red]FAIL[/]'}")
        return 0
    if args.action == "extract":
        pkg_arg = getattr(args, "package", None)
        records = extract_all_cookies([pkg_arg] if pkg_arg else None)
        if not records:
            console.print("[yellow]No Roblox/Delta packages found.[/yellow]")
            return 1

        table = Table(title="Auto-Extracted Cookies", box=box.ROUNDED)
        table.add_column("Package", style="cyan")
        table.add_column("Status")
        table.add_column("Username", style="green")
        table.add_column("User ID", justify="right")
        table.add_column("Cookie")

        show_raw = bool(getattr(args, "show", False))
        for r in records:
            st = r["status"]
            col = "green" if st == "ok" else ("yellow" if st == "missing" else "red")
            ck_disp = (r["cookie"] if show_raw else short(r["cookie"])) if r["cookie"] else "-"
            table.add_row(
                r["package"],
                f"[{col}]{st}[/{col}]",
                r["username"] or "-",
                str(r["user_id"] or "-"),
                ck_disp,
            )
        console.print(table)

        valid_recs = [r for r in records if r["status"] == "ok" and r["cookie"]]
        if not valid_recs:
            return 1

        if getattr(args, "save", False):
            added = store.add_cookies("\n".join(r["cookie"] for r in valid_recs))
            console.print(f"[green]Added {added} new cookie(s) to cookies.txt[/green]")

        out_path = getattr(args, "out", None) or (DEFAULT_EXPORT_PATH if getattr(args, "export", False) else None)
        if out_path:
            fmt = getattr(args, "format", "full") or "full"
            n = store.export_extracted(records, out_path, fmt=fmt)
            console.print(f"[green]Exported {n} cookie(s) ({fmt}) -> {out_path}[/green]")

        if getattr(args, "assign", False):
            cfg = load_config()
            by_pkg = {r["package"]: r for r in valid_recs}
            assigned_cnt = 0
            now = time.time()
            for inst in cfg["instances"]:
                rec = by_pkg.get(inst["package"])
                if rec:
                    store.set_assign(
                        inst["name"],
                        {
                            "cookie": rec["cookie"],
                            "user_id": rec["user_id"],
                            "username": rec["username"],
                            "started_at": now,
                        },
                    )
                    assigned_cnt += 1
            console.print(f"[green]Bound {assigned_cnt} instance(s) in state.json[/green]")

        return 0
    return 0


def cmd_profile(args):
    if args.action == "list":
        for p in list_profiles():
            d = load_profile(p)
            console.print(
                f"  [cyan]{p}[/cyan] -> place_id={d['place_id']} mode={d.get('check_method','executor')} "
                f"rejoin={d['rejoin_after_minutes']}m rotate_on={d['rotate_on']}"
            )
        return 0
    if args.action == "show":
        name = args.name or "default"
        console.print_json(json.dumps(load_profile(name)))
        return 0
    if args.action == "create":
        name = args.name
        if not name:
            console.print("[red]--name is required[/red]")
            return 1
        prof = dict(DEFAULT_PROFILE)
        if args.target:
            parsed = parse_target(args.target)
            if parsed["place_id"]:
                prof["place_id"] = int(parsed["place_id"])
            if parsed["link_code"] or parsed["share_code"] or "http" in args.target:
                prof["private_link"] = args.target
        if args.rejoin_min is not None:
            prof["rejoin_after_minutes"] = args.rejoin_min
        save_json(os.path.join(PROFILES, f"{name}.json"), prof)
        console.print(f"[green]Saved profile '{name}'[/green]")
        return 0
    return 0


def cmd_scripts(args):
    autoexec = getattr(args, "autoexec", None) or DEFAULT_AUTOEXEC
    if args.action == "list":
        items = list_autoexec_scripts(autoexec)
        table = Table(title=f"Executor Autoexecute Scripts ({autoexec})", box=box.ROUNDED)
        table.add_column("Name", style="cyan")
        table.add_column("State")
        table.add_column("Size", justify="right")
        table.add_column("Protected", justify="center")
        for it in items:
            table.add_row(
                it["name"],
                "[green]ON[/]" if it["enabled"] else "[dim]OFF[/]",
                f"{it['size']} B",
                "yes" if it["protected"] else "-",
            )
        console.print(table)
        return 0
    if args.action == "add":
        name = getattr(args, "name", None)
        if not name:
            console.print("[red]--name is required[/red]")
            return 1
        if getattr(args, "file", None):
            with open(args.file) as f:
                code = f.read()
        elif getattr(args, "code", None):
            code = args.code
        else:
            code = sys.stdin.read()
        dest = add_autoexec_script(autoexec, name, code)
        console.print(f"[green]Added script:[/green] {dest}")
        return 0
    if args.action == "toggle":
        name = getattr(args, "name", None)
        if not name:
            console.print("[red]--name is required[/red]")
            return 1
        nxt = toggle_autoexec_script(autoexec, name)
        console.print(f"[green]Toggled ->[/green] {nxt}")
        return 0
    if args.action == "delete":
        name = getattr(args, "name", None)
        if not name:
            console.print("[red]--name is required[/red]")
            return 1
        ok = delete_autoexec_script(autoexec, name)
        console.print("[green]Deleted.[/green]" if ok else "[yellow]Script not found.[/yellow]")
        return 0 if ok else 1
    return 0


def cmd_optimize(args):
    cfg = load_config()
    pkgs = [args.package] if getattr(args, "package", None) else (
        [i["package"] for i in cfg["instances"] if i.get("package")] or detect_packages()
    )
    if getattr(args, "kill_all", False):
        n = kill_all_packages(pkgs)
        console.print(f"[yellow]Kill Mode: stopped {n} package(s) + am kill-all[/yellow]")
    if getattr(args, "clear_cache", False):
        for p in pkgs:
            clear_app_cache(p)
            console.print(f"[green]Cleared cache:[/green] {p}")
    if getattr(args, "fps", None):
        for p in pkgs:
            ok = apply_low_graphics(p, fps_cap=args.fps, low_quality=True)
            console.print(f"[{'green' if ok else 'red'}]Low-Graphics + FPS={args.fps}: {p} ({'OK' if ok else 'FAILED'})[/]")
    return 0


def cmd_tabs(args):
    cfg = load_config()
    pkgs = [args.package] if getattr(args, "package", None) else (
        [i["package"] for i in cfg["instances"] if i.get("enabled", True) and i.get("package")] or detect_packages()
    )
    if args.action in ("sort", "tiny"):
        tiny = args.action == "tiny"
        n = sort_roblox_tabs(pkgs, tiny=tiny)
        console.print(f"[green]Resized {n} active tab(s) ({'tiny' if tiny else 'grid'} mode).[/green]")
        return 0
    if args.action == "open-all":
        stagger = float(cfg.get("stagger_launch_seconds") or 4)
        for inst in cfg["instances"]:
            if not inst.get("enabled", True):
                continue
            pkg = inst["package"]
            if args.package and pkg != args.package:
                continue
            prof = load_profile(inst.get("profile", "default"))
            raw_t = (inst.get("target") or prof.get("private_link") or str(prof.get("place_id") or "")).strip()
            parsed = parse_target(raw_t)
            pid = parsed["place_id"] or str(prof.get("place_id") or "")
            if not pid or pid == "0":
                console.print(f"[yellow]Skipped {inst['name']}: no place_id/target[/yellow]")
                continue
            uri = build_uri(pid, parsed["link_code"], prof.get("job_id") or None)
            extra = (prof.get("launch_extra") or "").strip()
            if prof.get("window_mode") == "freeform" and "--windowingMode" not in extra:
                extra = (extra + " --windowingMode 5").strip()
            launch(pkg, uri, extra, open_splash_first=bool(prof.get("open_splash_first", False)))
            console.print(f"[green]Launched {inst['name']} ({pkg}) -> {uri}[/green]")
            time.sleep(stagger)
        return 0
    if args.action == "logout":
        call_api = bool(getattr(args, "call_api", False))
        for p in pkgs:
            logout_package(p, call_api=call_api)
            console.print(f"[yellow]Logged out package: {p}[/yellow]")
        return 0
    return 0


def cmd_links(args):
    cfg = load_config()
    if args.action == "list":
        table = Table(title="Per-Instance Targets", box=box.ROUNDED)
        table.add_column("#", justify="right", style="yellow")
        table.add_column("Instance", style="cyan")
        table.add_column("Package")
        table.add_column("Profile", style="magenta")
        table.add_column("Override Target", style="green")
        for idx, inst in enumerate(cfg["instances"], 1):
            table.add_row(str(idx), inst["name"], inst["package"], inst.get("profile", "default"), inst.get("target") or "(profile default)")
        console.print(table)
        return 0
    if args.action == "import-shouko":
        src = getattr(args, "file", None)
        if not src or not os.path.isfile(src):
            console.print(f"[red]Shouko server_links.txt not found: {src}[/red]")
            return 1
        imported = {}
        with open(src, encoding="utf-8") as f:
            for raw in f:
                s = raw.strip()
                if not s or s.startswith("#") or "," not in s:
                    continue
                p, uri = [x.strip() for x in s.split(",", 1)]
                if p and uri:
                    imported[p] = uri
        updated = 0
        added = 0
        by_pkg = {i["package"]: i for i in cfg["instances"] if i.get("package")}
        for p, uri in imported.items():
            if p in by_pkg:
                by_pkg[p]["target"] = uri
                updated += 1
            else:
                idx = len(cfg["instances"]) + 1
                execs = scan_executor_dirs(pkg=p)
                cfg["instances"].append({
                    "name": f"noka{idx}" if "noka" in p.lower() else f"inst{idx}",
                    "package": p,
                    "profile": "default",
                    "target": uri,
                    "workspace": execs[0]["workspace"] if execs else DEFAULT_WORKSPACE,
                    "autoexec": execs[0]["autoexec"] if execs else DEFAULT_AUTOEXEC,
                    "use_cookie_queue": True,
                    "enabled": True,
                })
                added += 1
        save_config(cfg)
        ipc_request("reload_config", timeout=1.5)
        console.print(f"[green]Imported Shouko links: updated {updated}, added {added} instance(s).[/green]")
        return 0
    if not args.target:
        console.print("[red]--target is required[/red]")
        return 1
    if args.action == "set-all":
        for inst in cfg["instances"]:
            inst["target"] = args.target
        save_config(cfg)
        ipc_request("reload_config", timeout=1.5)
        console.print(f"[green]Updated target for all {len(cfg['instances'])} instance(s).[/green]")
        return 0
    if args.action == "set":
        if not args.instance:
            console.print("[red]--instance is required[/red]")
            return 1
        found = False
        for inst in cfg["instances"]:
            if inst["name"] == args.instance or inst["package"] == args.instance:
                inst["target"] = args.target
                found = True
        if not found:
            console.print(f"[red]Instance '{args.instance}' not found.[/red]")
            return 1
        save_config(cfg)
        ipc_request("reload_config", timeout=1.5)
        console.print(f"[green]Updated target for {args.instance}.[/green]")
        return 0
    return 0


def cmd_daemon(args):
    if args.action == "start":
        ping = ipc_request("ping", timeout=1.0)
        if ping.get("ok"):
            console.print(f"[yellow]Daemon is already running (pid={ping['result'].get('pid')}).[/yellow]")
            return 0
        run_cmd = [sys.executable, os.path.join(BASE, "main.py"), "run"]
        if getattr(args, "profile", None):
            run_cmd.extend(["--profile", args.profile])
        if getattr(args, "target", None):
            run_cmd.extend(["--target", args.target])
        if getattr(args, "rejoin_min", None) is not None:
            run_cmd.extend(["--rejoin-min", str(args.rejoin_min)])
        os.makedirs(LOGDIR, exist_ok=True)
        log_path = os.path.join(LOGDIR, "daemon.log")
        with open(log_path, "a") as lf:
            proc = subprocess.Popen(
                run_cmd,
                stdin=subprocess.DEVNULL,
                stdout=lf,
                stderr=lf,
                start_new_session=True,
                close_fds=True,
            )
        for _ in range(15):
            time.sleep(0.2)
            p = ipc_request("ping", timeout=1.0)
            if p.get("ok"):
                console.print(f"[green]Daemon started (pid={proc.pid}, sock={DEFAULT_IPC_SOCK}).[/green]")
                return 0
        console.print(f"[yellow]Daemon spawned (pid={proc.pid}), check {log_path} if IPC not ready.[/yellow]")
        return 0

    if args.action == "stop":
        res = ipc_request("stop", timeout=2.0)
        if res.get("ok"):
            console.print("[yellow]Stop requested via IPC.[/yellow]")
            return 0
        r = subprocess.run(["pkill", "-INT", "-f", "/rejoin_tool/main.py"], capture_output=True, text=True)
        if r.returncode != 0:
            su(f"pkill -INT -f '{PKILL_PATTERN}'", 10)
        console.print("[yellow]Sent stop signal (SIGINT) to daemon.[/yellow]")
        return 0

    if args.action == "reload":
        res = ipc_request("reload_config", timeout=3.0)
        if res.get("ok"):
            console.print(f"[green]Reloaded daemon config: {res['result']}[/green]")
            return 0
        console.print(f"[red]Daemon not reachable ({res.get('error')}).[/red]")
        return 1

    if args.action == "status":
        res = ipc_request("status", timeout=2.0)
        if not res.get("ok"):
            console.print(f"[dim]Daemon not running ({res.get('error')}).[/dim]")
            return 1
        data = res["result"]
        layout = _build_dashboard_layout(
            data["instances"],
            data["counts"],
            data["system"],
            data.get("logs") or [],
            switched=data.get("switched"),
            title_suffix=" [Daemon]",
        )
        console.print(layout)
        return 0

    if args.action == "attach":
        res = ipc_request("status", timeout=2.0)
        if not res.get("ok"):
            console.print(f"[red]Cannot attach: daemon not running ({res.get('error')}).[/red]")
            return 1
        try:
            with Live(
                _build_dashboard_layout(
                    res["result"]["instances"],
                    res["result"]["counts"],
                    res["result"]["system"],
                    res["result"].get("logs") or [],
                    switched=res["result"].get("switched"),
                    title_suffix=" [Daemon Attached]",
                ),
                console=console,
                refresh_per_second=1,
            ) as live:
                while True:
                    time.sleep(1.0)
                    cur = ipc_request("status", timeout=2.0)
                    if not cur.get("ok"):
                        break
                    d = cur["result"]
                    live.update(
                        _build_dashboard_layout(
                            d["instances"],
                            d["counts"],
                            d["system"],
                            d.get("logs") or [],
                            switched=d.get("switched"),
                            title_suffix=" [Daemon Attached]",
                        )
                    )
        except KeyboardInterrupt:
            pass
        console.print("[dim]Detached from daemon (monitor continues running).[/dim]")
        return 0
    return 0


def cmd_quick_setup(args):
    """Shouko-style 1-step setup: auto-detect all packages, assign link/profile, deploy Lua + shared scripts."""
    cfg = load_config()
    pkgs = detect_packages(prefix=cfg.get("package_prefix", ""))
    if not pkgs:
        console.print("[red]No Roblox/Executor packages detected.[/red]")
        return 1
    prof_name = getattr(args, "profile", None) or "default"
    try:
        prof = load_profile(prof_name)
    except FileNotFoundError as e:
        console.print(f"[red]{e}[/red]")
        return 1

    target = (getattr(args, "target", None) or "").strip()
    by_pkg = {i["package"]: i for i in cfg["instances"] if i.get("package")}
    for p in pkgs:
        if p in by_pkg:
            by_pkg[p]["profile"] = prof_name
            if target:
                by_pkg[p]["target"] = target
            by_pkg[p]["enabled"] = True
        else:
            idx = len(cfg["instances"]) + 1
            execs = scan_executor_dirs(pkg=p)
            cfg["instances"].append({
                "name": f"noka{idx}" if "noka" in p.lower() else f"inst{idx}",
                "package": p,
                "profile": prof_name,
                "target": target,
                "workspace": execs[0]["workspace"] if execs else DEFAULT_WORKSPACE,
                "autoexec": execs[0]["autoexec"] if execs else DEFAULT_AUTOEXEC,
                "use_cookie_queue": True,
                "enabled": True,
            })
    save_config(cfg)
    ipc_request("reload_config", timeout=1.5)

    extra_lua = None
    if prof.get("lua"):
        extra_lua = prof["lua"] if os.path.isabs(prof["lua"]) else os.path.join(PROFILES, "lua", prof["lua"])
    dep = deploy_all_executors(
        extra_lua_path=extra_lua,
        packages=pkgs,
        sync_shared=bool(cfg.get("sync_shared_scripts", True)),
    )
    console.print(f"[green]Quick Setup complete: {len(pkgs)} package(s), profile='{prof_name}', deployed {len(dep)} Lua/shared file(s).[/green]")
    if getattr(args, "start", False):
        return cmd_run(argparse.Namespace(instance=None, profile=None, target=None, rejoin_min=None, dry_run=False, dashboard=True))
    console.print("[dim]Xem lệnh chạy root / Termux:Boot / Stop (kiểu QH Rejoin): rejoin commands[/dim]")
    return 0


def cmd_benchmark(_args=None):
    """Benchmark batched /proc telemetry, package scan, executor discovery, and SQLite cookie read."""
    cfg = load_config()
    t0 = time.perf_counter()
    pkgs = detect_packages(prefix=cfg.get("package_prefix", ""))
    t_pkg = (time.perf_counter() - t0) * 1000.0

    t1 = time.perf_counter()
    sys_st = get_system_stats()
    batch = get_batch_pkg_usage(pkgs)
    t_sys = (time.perf_counter() - t1) * 1000.0

    t2 = time.perf_counter()
    execs = scan_executor_dirs(pkg=pkgs[0] if pkgs else None)
    t_exec = (time.perf_counter() - t2) * 1000.0

    ck_ms = None
    if pkgs:
        t3 = time.perf_counter()
        ck = read_app_cookie(pkgs[0])
        ck_ms = (time.perf_counter() - t3) * 1000.0 if ck is not None else None

    tbl = Table(title="Rejoin v3.0 Hybrid Performance Benchmark", box=box.ROUNDED)
    tbl.add_column("Subsystem", style="cyan")
    tbl.add_column("Latency", justify="right", style="green")
    tbl.add_column("Result")
    tbl.add_row("detect_packages", f"{t_pkg:.1f} ms", f"{len(pkgs)} package(s): {', '.join(pkgs) or '-'}")
    tbl.add_row("batched /proc + system stats", f"{t_sys:.1f} ms", f"CPU {sys_st['cpu_pct']:.0f}% | RAM {sys_st['ram_used_mb']:.0f}MB | Tool {sys_st.get('tool_ram_mb', 0):.1f}MB | Tracked {len(batch)}")
    tbl.add_row("scan_executor_dirs", f"{t_exec:.1f} ms", f"{len(execs)} executor(s)")
    tbl.add_row("read_app_cookie[0]", f"{ck_ms:.1f} ms" if ck_ms is not None else "n/a", "OK" if ck_ms is not None else "no cookie")
    console.print(tbl)
    return 0


def cmd_gofile(args):
    gf = GoFileDownloader()
    try:
        files, tok = gf.list_files(args.url, password=getattr(args, "password", "") or "")
    except Exception as e:
        console.print(f"[red]GoFile error: {e}[/red]")
        return 1
    if not files:
        console.print("[yellow]No downloadable files found in GoFile link.[/yellow]")
        return 1
    for idx, fe in enumerate(files, 1):
        console.print(f"  [{idx}] {fe['name']} ({fe['size']} B)")
    if not getattr(args, "download", False) and not getattr(args, "install", False):
        return 0
    dest = gf.download(files[0], tok)
    console.print(f"[green]Downloaded -> {dest}[/green]")
    if getattr(args, "install", False) and dest.endswith(".apk"):
        rc, out, err = su(f"pm install -r {shlex.quote(dest)}", 120)
        console.print(f"[{'green' if rc == 0 else 'red'}]pm install: {out or err}[/]")
    return 0


def cmd_webhook_test(args):
    cfg = load_config()
    wh = (cfg.get("webhook") or "").strip()
    if not wh:
        console.print("[red]No webhook URL configured in config.json[/red]")
        return 1
    st = get_system_stats()
    img = None
    if getattr(args, "screenshot", False):
        cand = os.path.join(TMP, "test_screen.png")
        if capture_screenshot(cand):
            img = cand
    try:
        ok = send_discord_embed(
            wh,
            "Rejoin Tool Test Webhook",
            "Connection test from Termux Rejoin Tool v2.",
            color=0x57F287,
            fields=[
                {"name": "CPU", "value": f"{st['cpu_pct']:.0f}%", "inline": True},
                {"name": "RAM", "value": f"{st['ram_used_mb']:.0f}/{st['ram_total_mb']:.0f}MB ({st['ram_pct']:.0f}%)", "inline": True},
                {"name": "Disk Free", "value": f"{st['disk_free_gb']}GB", "inline": True},
            ],
            image_path=img,
            ping=cfg.get("webhook_ping", ""),
        )
        console.print("[green]Webhook sent![/green]" if ok else "[red]Webhook failed.[/red]")
        return 0 if ok else 1
    finally:
        if img and os.path.exists(img):
            os.remove(img)


# ───────────────────────── Quick Setup commands (QH Rejoin style) ─────────────────────────
TERMUX_PREFIX = "/data/data/com.termux/files/usr"
TERMUX_HOME = "/data/data/com.termux/files/home"
BOOT_SCRIPT = os.path.join(TERMUX_HOME, ".termux", "boot", "rejoin_boot.sh")
LAUNCHER = os.path.join(BASE, "rejoin")
# Regex "[m]ain.py" khớp ".../main.py" nhưng KHÔNG khớp chính dòng lệnh `sh -c "pkill ..."` (tránh tự kill).
PKILL_PATTERN = os.path.join(BASE, "[m]ain.py")
ROOT_ENV = (
    f"export PATH=\\$PATH:{TERMUX_PREFIX}/bin && export TERM=xterm-256color && "
    f"export TMPDIR={TERMUX_PREFIX}/tmp && export LD_LIBRARY_PATH={TERMUX_PREFIX}/lib"
)
SETUP_URL_PLACEHOLDER = "https://raw.githubusercontent.com/YOUR_USER/rejoin_tool/main/setup.sh"


def root_command(run_args=""):
    """`su -c "<env> && <launcher> [args]"` — copy-paste được từ Termux (giống lệnh 'Run tool' của QH Rejoin)."""
    tail = f" {run_args}" if run_args else ""
    return f'su -c "{ROOT_ENV} && {LAUNCHER}{tail}"'


def stop_command():
    return f"su -c \"pkill -f '{PKILL_PATTERN}'\""


def detect_setup_url():
    """Suy ra link raw setup.sh từ git remote (GitHub) nếu có, ngược lại trả placeholder."""
    url = os.environ.get("RJ_SETUP_URL", "").strip()
    if url:
        return url
    try:
        r = subprocess.run(["git", "-C", BASE, "remote", "get-url", "origin"], capture_output=True, text=True, timeout=5)
        b = subprocess.run(["git", "-C", BASE, "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True, timeout=5)
        remote = r.stdout.strip()
        branch = b.stdout.strip() or "main"
        if r.returncode == 0 and "github.com" in remote:
            path = remote.split("github.com", 1)[1].lstrip(":/")
            if path.endswith(".git"):
                path = path[:-4]
            return f"https://raw.githubusercontent.com/{path}/{branch}/setup.sh"
    except Exception:
        pass
    return SETUP_URL_PLACEHOLDER


def build_boot_script(run_args=""):
    """Nội dung ~/.termux/boot/rejoin_boot.sh: chạy headless bằng root (fallback user nếu chưa có su)."""
    log_file = shlex.quote(os.path.join(BASE, "logs", "boot.log"))
    tail = f" {run_args}" if run_args else ""
    return (
        "#!/data/data/com.termux/files/usr/bin/bash\n"
        "command -v termux-wake-lock >/dev/null 2>&1 && termux-wake-lock\n"
        "sleep 15\n"
        "if su -c true >/dev/null 2>&1; then\n"
        f"    {root_command('run' + tail)} >> {log_file} 2>&1\n"
        "else\n"
        f"    {shlex.quote(LAUNCHER)} run{tail} >> {log_file} 2>&1\n"
        "fi\n"
    )


def cmd_commands(_args=None):
    """In các lệnh copy-paste giống trang QH Rejoin SETUP: Setup / Run root / Termux boot / Stop."""
    setup_url = detect_setup_url()
    boot_dir = os.path.dirname(BOOT_SCRIPT)
    manual_boot = (
        f"mkdir -p {boot_dir}\n"
        "echo '#!/data/data/com.termux/files/usr/bin/bash\n"
        "termux-wake-lock\n"
        "sleep 15\n"
        f"{root_command('run')}' > {BOOT_SCRIPT}\n"
        f"chmod 700 {BOOT_SCRIPT}"
    )
    blocks = [
        ("Setup (cài / cập nhật 1 lệnh)", f". <(curl -sL {setup_url})"),
        ("Run tool — menu (all cloud with root)", root_command()),
        ("Run tool — headless auto-rejoin (root)", root_command("run")),
        ("For termux boot (tự tạo script)", "rejoin boot enable"),
        ("For termux boot (thủ công, giống QH Rejoin)", manual_boot),
        ("Stop termux boot / tool đang chạy", stop_command()),
    ]
    for title, cmd in blocks:
        console.print(f"\n[bold cyan]▶ {title}[/bold cyan]")
        print(cmd)
    if setup_url == SETUP_URL_PLACEHOLDER:
        console.print(
            "\n[yellow]Lưu ý:[/yellow] chưa có git remote GitHub → link Setup đang là placeholder. "
            "Push repo lên GitHub rồi sửa DEFAULT_REPO trong setup.sh (hoặc đặt biến RJ_SETUP_URL).",
            highlight=False,
        )
    console.print("[dim]Termux:Boot cần cài app Termux:Boot (F-Droid) và mở 1 lần.[/dim]")
    return 0


def cmd_boot(args):
    if args.action == "enable":
        os.makedirs(os.path.dirname(BOOT_SCRIPT), exist_ok=True)
        run_args = []
        if getattr(args, "profile", None):
            run_args.extend(["--profile", shlex.quote(args.profile)])
        if getattr(args, "target", None):
            run_args.extend(["--target", shlex.quote(args.target)])
        if getattr(args, "rejoin_min", None) is not None:
            run_args.extend(["--rejoin-min", str(args.rejoin_min)])
        os.makedirs(os.path.join(BASE, "logs"), exist_ok=True)
        with open(BOOT_SCRIPT, "w") as f:
            f.write(build_boot_script(" ".join(run_args)))
        os.chmod(BOOT_SCRIPT, 0o700)
        console.print(f"[green]Enabled Termux:Boot auto-start (root mode):[/green] {BOOT_SCRIPT}")
        console.print(f"[dim]Stop: rejoin boot stop   hoặc   {stop_command()}[/dim]", markup=False, highlight=False)
        console.print("[dim]Note: Install & open the Termux:Boot app once so Android allows boot execution.[/dim]")
        return 0

    if args.action == "disable":
        if os.path.exists(BOOT_SCRIPT):
            os.remove(BOOT_SCRIPT)
            console.print(f"[yellow]Removed boot script:[/yellow] {BOOT_SCRIPT}")
        else:
            console.print("[dim]Boot script is not enabled.[/dim]")
        return 0

    if args.action == "stop":
        res = ipc_request("stop", timeout=1.5)
        if not res.get("ok"):
            r = subprocess.run(["pkill", "-INT", "-f", "/rejoin_tool/main.py"], capture_output=True, text=True)
            if r.returncode != 0:
                su(f"pkill -INT -f '{PKILL_PATTERN}'", 10)
        console.print("[yellow]Sent stop signal (SIGINT) to background monitor(s).[/yellow]")
        return 0

    if args.action == "status":
        enabled = os.path.exists(BOOT_SCRIPT)
        pg = subprocess.run(["pgrep", "-af", "/rejoin_tool/main.py"], capture_output=True, text=True)
        running_lines = [l for l in pg.stdout.splitlines() if "boot status" not in l and "pgrep" not in l]
        console.print(Panel(
            f"Boot Script: [{'green' if enabled else 'dim'}]{'ENABLED' if enabled else 'DISABLED'}[/] ({BOOT_SCRIPT})\n"
            f"Background Monitor: [{'green' if running_lines else 'dim'}]{'RUNNING' if running_lines else 'NOT RUNNING'}[/]",
            title="Termux:Boot Status",
            border_style="cyan",
        ))
        if enabled:
            with open(BOOT_SCRIPT) as f:
                console.print(f"[dim]{f.read().strip()}[/dim]")
        return 0
    return 0


def cmd_run(args):
    cfg = load_config()
    store = Store()
    from rjcore import Logger
    logger = Logger(echo=not args.dashboard)
    mgr = Manager(cfg, store, logger, enable_ipc=not args.dry_run)

    profile_override = None
    if args.profile or args.target or args.rejoin_min is not None:
        base_prof = load_profile(args.profile or "default")
        if args.target:
            parsed = parse_target(args.target)
            if parsed["place_id"]:
                base_prof["place_id"] = int(parsed["place_id"])
            if parsed["link_code"] or parsed["share_code"] or "http" in args.target:
                base_prof["private_link"] = args.target
        if args.rejoin_min is not None:
            base_prof["rejoin_after_minutes"] = args.rejoin_min
        profile_override = base_prof

    def _sig_handler(_signum, _frame):
        mgr.stop_event.set()

    signal.signal(signal.SIGINT, _sig_handler)
    signal.signal(signal.SIGTERM, _sig_handler)

    mgr.start(instance_names=args.instance, dry_run=args.dry_run, profile_override=profile_override)

    if args.dashboard:
        try:
            with Live(_render_dashboard(mgr, store), console=console, refresh_per_second=1) as live:
                while not mgr.stop_event.is_set():
                    live.update(_render_dashboard(mgr, store))
                    if all(not w.is_alive() for w in mgr.workers.values()):
                        break
                    time.sleep(1)
        except KeyboardInterrupt:
            pass
    else:
        try:
            while not mgr.stop_event.is_set():
                if all(not w.is_alive() for w in mgr.workers.values()):
                    break
                time.sleep(1)
        except KeyboardInterrupt:
            pass

    mgr.stop()
    console.print("[yellow]Monitor stopped cleanly.[/yellow]")
    return 0


# ───────────────────────── Shouko-Style Configuration Menu ─────────────────────────
def _yn(val):
    return "[bold green]ON[/bold green]" if val else "[bold red]OFF[/bold red]"


_CHANGE_BF_FIELDS = [
    ("level", "Min Level (0=ignore)", "int"),
    ("beli", "Min Beli (0=ignore)", "int"),
    ("fragment", "Min Fragments (0=ignore)", "int"),
    ("bounty/honor", "Min Bounty / Honor (0=ignore)", "int"),
    ("tier", "Min Race Tier (0=ignore)", "int"),
    ("race_ver", "Required Race / Version (blank=any)", "str"),
    ("pull_level", "Pull Lever completed", "bool"),
    ("god_human", "Godhuman", "bool"),
    ("sanguine_art", "Sanguine Art", "bool"),
    ("cursed_dual_katana", "Cursed Dual Katana (CDK)", "bool"),
    ("true_triple_katana", "True Triple Katana (TTK)", "bool"),
    ("shark_anchor", "Shark Anchor", "bool"),
    ("skull_guitar", "Skull Guitar", "bool"),
    ("mirror_fractal", "Mirror Fractal", "bool"),
    ("valkyrie_helm", "Valkyrie Helm", "bool"),
]


def _menu_change_config():
    conf = load_change_config()
    while True:
        console.clear()
        table = Table(
            title="[bold cyan]Blox Fruits Auto-Change Criteria (config-change.json)[/bold cyan]",
            box=box.ROUNDED,
            show_lines=True,
        )
        table.add_column("#", justify="right", style="bright_magenta", width=4)
        table.add_column("Condition", style="cyan")
        table.add_column("Target Value")
        for idx, (key, label, ftype) in enumerate(_CHANGE_BF_FIELDS, 1):
            val = conf.get(key)
            if ftype == "bool":
                disp = _yn(val)
            elif ftype == "int":
                disp = f"[yellow]{val}[/yellow]" if val else "[dim]0 (ignored)[/dim]"
            else:
                disp = f"[yellow]{val}[/yellow]" if val else "[dim]any (ignored)[/dim]"
            table.add_row(str(idx), label, disp)
        table.add_row("R", "[yellow]Reset all to defaults (disable all checks)[/yellow]", "")
        table.add_row("S", "[bold green]Save & hot-reload daemon[/bold green]", "")
        table.add_row("X", "[bold red]Back[/bold red]", "")
        console.print(table)

        ch = Prompt.ask("Select # (or R=reset, S=save, X=back)", default="X").strip().upper()
        if ch == "X":
            return 0
        if ch == "R":
            conf = dict(DEFAULT_CHANGE_CONFIG)
            continue
        if ch == "S":
            save_change_config(conf)
            res = ipc_request("reload_config", timeout=1.5)
            console.print("[green]Saved config-change.json" + (" & reloaded daemon.[/green]" if res.get("ok") else ".[/green]"))
            time.sleep(1)
            return 0
        if ch.isdigit() and 1 <= int(ch) <= len(_CHANGE_BF_FIELDS):
            key, label, ftype = _CHANGE_BF_FIELDS[int(ch) - 1]
            if ftype == "bool":
                conf[key] = not bool(conf.get(key))
            elif ftype == "int":
                conf[key] = max(0, IntPrompt.ask(label, default=int(conf.get(key) or 0)))
            else:
                conf[key] = Prompt.ask(label, default=str(conf.get(key) or "")).strip()


def cmd_change_config(args):
    act = args.action
    if act == "show":
        console.print_json(json.dumps(load_change_config()))
        return 0
    if act == "reset":
        conf = save_change_config(DEFAULT_CHANGE_CONFIG)
        ipc_request("reload_config", timeout=1.5)
        console.print("[green]Reset config-change.json to defaults.[/green]")
        console.print_json(json.dumps(conf))
        return 0
    if act == "set":
        key = (args.key or "").strip()
        if key not in DEFAULT_CHANGE_CONFIG:
            console.print(f"[red]Unknown key '{key}'. Valid: {', '.join(DEFAULT_CHANGE_CONFIG)}[/red]")
            return 1
        conf = load_change_config()
        raw_v = args.value
        if isinstance(DEFAULT_CHANGE_CONFIG[key], bool):
            conf[key] = str(raw_v).strip().lower() in ("true", "1", "yes", "on")
        elif isinstance(DEFAULT_CHANGE_CONFIG[key], int):
            conf[key] = max(0, int(raw_v))
        else:
            conf[key] = str(raw_v or "").strip()
        saved = save_change_config(conf)
        ipc_request("reload_config", timeout=1.5)
        console.print(f"[green]Updated {key} -> {saved[key]!r}[/green]")
        return 0
    if act == "edit":
        return _menu_change_config()
    return 0


def cmd_config(_args=None):
    cfg = load_config()
    while True:
        console.clear()
        table = Table(title="[bold cyan]Rejoin Configuration (Shouko-Style)[/bold cyan]", box=box.ROUNDED, show_lines=True)
        table.add_column("#", justify="right", style="bright_magenta", width=4)
        table.add_column("Setting", style="cyan")
        table.add_column("Current Value")

        table.add_row("", "[bold blue]── ACCOUNT / CHANGE ACCOUNT ──[/bold blue]", "")
        table.add_row("1", "Device name (Webhook header)", cfg.get("device_name") or "[dim]not set[/dim]")
        table.add_row("2", "Package prefix filter", cfg.get("package_prefix") or "[dim]auto[/dim]")
        table.add_row("3", "Keep screen awake (stayon)", _yn(cfg.get("stay_awake")))
        table.add_row("4", "Auto clean background RAM (min, 0=off)", f"[yellow]{cfg.get('auto_clean_ram_minutes', 0)}[/yellow] min")
        table.add_row("5", "Auto change account (Custom - <User>.txt)", _yn(cfg.get("change_acc_cus", True)))
        table.add_row("6", "Auto change account (Blox Fruit)", _yn(cfg.get("change_acc_bf", False)))
        table.add_row("7", "Edit Blox Fruit change conditions (config-change.json)", "[dim]open editor[/dim]")
        table.add_row("8", "Auto cross-block accounts (split servers)", _yn(cfg.get("auto_block_accounts", False)))

        table.add_row("", "[bold blue]── TABS / PROCESS / SHARED LUA ──[/bold blue]", "")
        table.add_row("9", "Auto sort Roblox tabs (grid)", _yn(cfg.get("sort_tabs")))
        table.add_row("10", "Tiny sort tabs (smallest tiles)", _yn(cfg.get("tiny_sort_tabs")))
        table.add_row("11", "Kill all tabs if 1 crashes", _yn(cfg.get("kill_all_on_crash")))
        table.add_row("12", "Safe focus switch before force-stop", _yn(cfg.get("focus_before_kill", True)))
        table.add_row("13", "Auto-sync shared/Autoexec & Workspace", _yn(cfg.get("sync_shared_scripts", True)))

        table.add_row("", "[bold blue]── TIMING ──[/bold blue]", "")
        table.add_row("14", "Stagger delay between launches (sec)", f"[yellow]{cfg.get('stagger_launch_seconds', 8)}[/yellow] sec")
        table.add_row("15", "Splash -> protocol open delay (sec)", f"[yellow]{cfg.get('open_delay_seconds', 2)}[/yellow] sec")
        table.add_row("16", "Sleep after kill (sec)", f"[yellow]{cfg.get('sleep_after_kill_seconds', 2)}[/yellow] sec")
        table.add_row("17", "Tab sort interval (sec)", f"[yellow]{cfg.get('sort_tabs_interval_seconds', 20)}[/yellow] sec")

        table.add_row("", "[bold blue]── WEBHOOK ──[/bold blue]", "")
        table.add_row("18", "Monitor Webhook URL", "[green]set[/green]" if cfg.get("webhook") else "[red]off[/red]")
        table.add_row("19", "Account Change Webhook URL", "[green]set[/green]" if cfg.get("webhook_change_url") else "[dim]inherit monitor[/dim]")
        table.add_row("20", "Alert / Challenge Webhook URL", "[green]set[/green]" if cfg.get("webhook_alert_url") else "[dim]inherit monitor[/dim]")
        table.add_row("21", "Webhook status interval (min, 0=off)", f"[yellow]{cfg.get('webhook_interval_minutes', 0)}[/yellow] min")
        table.add_row("22", "Attach screenshot to status webhook", _yn(cfg.get("webhook_screenshot")))
        table.add_row("23", "Discord ping target (e.g. @everyone)", cfg.get("webhook_ping") or "[dim]none[/dim]")

        table.add_row("S", "[bold green]Save & apply (hot-reload daemon)[/bold green]", "")
        table.add_row("X", "[bold red]Discard & back[/bold red]", "")
        console.print(table)

        ch = Prompt.ask("Select # (or S=save, X=discard)", default="X").strip().upper()
        if ch == "X":
            return 0
        if ch == "S":
            save_config(cfg)
            if cfg.get("stay_awake"):
                set_stay_awake(True)
            res = ipc_request("reload_config", timeout=1.5)
            msg = "[green]Configuration saved & hot-reloaded daemon.[/green]" if res.get("ok") else "[green]Configuration saved.[/green]"
            console.print(msg)
            time.sleep(1)
            return 0
        if ch == "1":
            cfg["device_name"] = Prompt.ask("Device name (blank=clear)", default=cfg.get("device_name") or "")
        elif ch == "2":
            cfg["package_prefix"] = Prompt.ask("Package prefix (blank=auto)", default=cfg.get("package_prefix") or "")
        elif ch == "3":
            cfg["stay_awake"] = not cfg.get("stay_awake", False)
        elif ch == "4":
            cfg["auto_clean_ram_minutes"] = max(0, IntPrompt.ask("Auto clean RAM every X minutes (0=off)", default=int(cfg.get("auto_clean_ram_minutes") or 0)))
        elif ch == "5":
            cfg["change_acc_cus"] = not cfg.get("change_acc_cus", True)
        elif ch == "6":
            cfg["change_acc_bf"] = not cfg.get("change_acc_bf", False)
        elif ch == "7":
            _menu_change_config()
        elif ch == "8":
            cfg["auto_block_accounts"] = not cfg.get("auto_block_accounts", False)
        elif ch == "9":
            cfg["sort_tabs"] = not cfg.get("sort_tabs", False)
        elif ch == "10":
            cfg["tiny_sort_tabs"] = not cfg.get("tiny_sort_tabs", False)
        elif ch == "11":
            cfg["kill_all_on_crash"] = not cfg.get("kill_all_on_crash", False)
        elif ch == "12":
            cfg["focus_before_kill"] = not cfg.get("focus_before_kill", True)
        elif ch == "13":
            cfg["sync_shared_scripts"] = not cfg.get("sync_shared_scripts", True)
        elif ch == "14":
            cfg["stagger_launch_seconds"] = max(0, IntPrompt.ask("Stagger launch delay (sec)", default=int(cfg.get("stagger_launch_seconds") or 8)))
        elif ch == "15":
            cfg["open_delay_seconds"] = max(0, IntPrompt.ask("Splash -> protocol open delay (sec)", default=int(cfg.get('open_delay_seconds') or 2)))
        elif ch == "16":
            cfg["sleep_after_kill_seconds"] = max(0, IntPrompt.ask("Sleep after kill (sec)", default=int(cfg.get('sleep_after_kill_seconds') or 2)))
        elif ch == "17":
            cfg["sort_tabs_interval_seconds"] = max(10, IntPrompt.ask("Tab sort interval (sec, min 10)", default=int(cfg.get('sort_tabs_interval_seconds') or 20)))
        elif ch == "18":
            cfg["webhook"] = Prompt.ask("Monitor Webhook URL (blank=clear)", default=cfg.get("webhook") or "")
        elif ch == "19":
            cfg["webhook_change_url"] = Prompt.ask("Account Change Webhook URL (blank=inherit)", default=cfg.get("webhook_change_url") or "")
        elif ch == "20":
            cfg["webhook_alert_url"] = Prompt.ask("Alert/Challenge Webhook URL (blank=inherit)", default=cfg.get("webhook_alert_url") or "")
        elif ch == "21":
            cfg["webhook_interval_minutes"] = max(0, IntPrompt.ask("Webhook status interval (min, 0=off)", default=int(cfg.get("webhook_interval_minutes") or 0)))
        elif ch == "22":
            cfg["webhook_screenshot"] = not cfg.get("webhook_screenshot", False)
        elif ch == "23":
            cfg["webhook_ping"] = Prompt.ask("Discord ping target (e.g. @everyone, blank=none)", default=cfg.get("webhook_ping") or "")
    return 0


# ───────────────────────── Interactive Menu ─────────────────────────
def _menu_instances():
    while True:
        cfg = load_config()
        console.clear()
        table = Table(title="Instances & Per-Clone Targets", box=box.ROUNDED)
        table.add_column("#", justify="right", style="yellow")
        table.add_column("Name", style="cyan")
        table.add_column("Package")
        table.add_column("Profile", style="magenta")
        table.add_column("Override Target", style="green")
        table.add_column("Queue", justify="center")
        table.add_column("Enabled", justify="center")
        for idx, i in enumerate(cfg["instances"], 1):
            table.add_row(
                str(idx),
                i["name"],
                i["package"],
                i.get("profile", "default"),
                (i.get("target") or "-")[:24],
                "yes" if i.get("use_cookie_queue", True) else "no",
                "[green]ON[/]" if i.get("enabled", True) else "[red]OFF[/]",
            )
        console.print(table)
        console.print("\n[1] Auto-detect & add  [2] Add manual  [3] Assign profile  [4] Set target link  [5] Set 1 link for ALL  [6] Toggle ON/OFF  [7] Delete  [0] Back")
        c = Prompt.ask("Select", choices=["0", "1", "2", "3", "4", "5", "6", "7"], default="0")
        if c == "0":
            return
        if c == "1":
            cmd_detect(argparse.Namespace(add=True))
            Prompt.ask("Press Enter", default="")
        elif c == "2":
            name = Prompt.ask("Instance name")
            pkg = Prompt.ask("Android package name")
            profs = list_profiles()
            prof = Prompt.ask("Profile", choices=profs, default=profs[0] if profs else "default")
            use_q = Confirm.ask("Use cookie rotation queue?", default=True)
            cfg["instances"].append({
                "name": name, "package": pkg, "profile": prof, "target": "",
                "workspace": DEFAULT_WORKSPACE, "autoexec": DEFAULT_AUTOEXEC,
                "use_cookie_queue": use_q, "enabled": True,
            })
            save_config(cfg)
        elif c == "3" and cfg["instances"]:
            idx = IntPrompt.ask("Instance #", default=1) - 1
            if 0 <= idx < len(cfg["instances"]):
                profs = list_profiles()
                cfg["instances"][idx]["profile"] = Prompt.ask("Profile", choices=profs, default=profs[0])
                save_config(cfg)
        elif c == "4" and cfg["instances"]:
            idx = IntPrompt.ask("Instance #", default=1) - 1
            if 0 <= idx < len(cfg["instances"]):
                t = Prompt.ask("Game ID / VIP / Share link (blank=use profile)", default=cfg["instances"][idx].get("target") or "")
                cfg["instances"][idx]["target"] = t.strip()
                save_config(cfg)
        elif c == "5" and cfg["instances"]:
            t = Prompt.ask("Game ID / VIP / Share link for ALL instances")
            cmd_links(argparse.Namespace(action="set-all", target=t.strip(), instance=None))
            time.sleep(1)
        elif c == "6" and cfg["instances"]:
            idx = IntPrompt.ask("Instance #", default=1) - 1
            if 0 <= idx < len(cfg["instances"]):
                cfg["instances"][idx]["enabled"] = not cfg["instances"][idx].get("enabled", True)
                save_config(cfg)
        elif c == "7" and cfg["instances"]:
            idx = IntPrompt.ask("Instance # to delete", default=1) - 1
            if 0 <= idx < len(cfg["instances"]):
                cfg["instances"].pop(idx)
                save_config(cfg)


def _menu_profiles():
    while True:
        console.clear()
        profs = list_profiles()
        table = Table(title="Game Profiles", box=box.ROUNDED)
        table.add_column("Name", style="cyan")
        table.add_column("Place ID")
        table.add_column("Mode", style="yellow")
        table.add_column("VIP Link")
        table.add_column("Rejoin (min)", justify="right")
        table.add_column("Rotate On")
        for p in profs:
            d = load_profile(p)
            table.add_row(
                p,
                str(d.get("place_id") or "-"),
                str(d.get("check_method") or "executor"),
                "yes" if d.get("private_link") else "-",
                str(d.get("rejoin_after_minutes")),
                ", ".join(d.get("rotate_on") or [])[:35],
            )
        console.print(table)
        console.print("\n[1] Create / Edit profile  [2] View profile JSON  [0] Back")
        c = Prompt.ask("Select", choices=["0", "1", "2"], default="0")
        if c == "0":
            return
        if c == "1":
            name = Prompt.ask("Profile name", default="default")
            base = load_profile(name) if name in profs else dict(DEFAULT_PROFILE)
            target = Prompt.ask("Game ID or VIP/Share Link", default=base.get("private_link") or str(base.get("place_id") or ""))
            parsed = parse_target(target)
            if parsed["place_id"]:
                base["place_id"] = int(parsed["place_id"])
            if parsed["link_code"] or parsed["share_code"] or "http" in target:
                base["private_link"] = target
            else:
                base["private_link"] = ""
            base["check_method"] = Prompt.ask("Check method", choices=["executor", "online"], default=base.get("check_method") or "executor")
            base["rejoin_after_minutes"] = IntPrompt.ask("Mandatory rejoin after X minutes (0=disable)", default=int(base.get("rejoin_after_minutes") or 30))
            base["rotate_after_minutes"] = IntPrompt.ask("Scheduled rotate account every X minutes (0=disable)", default=int(base.get("rotate_after_minutes") or 0))
            base["heartbeat_timeout"] = IntPrompt.ask("Heartbeat stale timeout (sec)", default=int(base.get("heartbeat_timeout") or 45))
            base["launch_grace"] = IntPrompt.ask("Launch grace period (sec)", default=int(base.get("launch_grace") or 90))
            base["on_challenge"] = Prompt.ask("On captcha/faceid/lock", choices=["pause", "rotate", "ignore"], default=base.get("on_challenge") or "pause")
            base["open_splash_first"] = Confirm.ask("Use 2-step ActivitySplash -> ProtocolLaunch?", default=bool(base.get("open_splash_first", False)))
            rot = Prompt.ask("Rotate conditions (comma separated, e.g. status:done,elapsed_account:120m,stat:level>=100)", default=",".join(base.get("rotate_on") or ["status:done"]))
            base["rotate_on"] = [x.strip() for x in rot.split(",") if x.strip()]
            rules_raw = Prompt.ask("Extra rules (comma separated, e.g. kicked -> rejoin)", default=",".join(x for x in (base.get("rules") or []) if isinstance(x, str)))
            base["rules"] = [x.strip() for x in rules_raw.split(",") if x.strip()]
            base["lua"] = Prompt.ask("Optional custom Lua file in profiles/lua/ (blank=none)", default=base.get("lua") or "")
            base["clear_cache_on_rejoin"] = Confirm.ask("Clear app cache on every rejoin?", default=bool(base.get("clear_cache_on_rejoin", False)))
            base["max_ram_mb"] = IntPrompt.ask("Auto-rejoin if instance RAM exceeds MB (0=disable)", default=int(base.get("max_ram_mb") or 0))
            base["fps_cap"] = IntPrompt.ask("Force FPS cap + low graphics on start (0=disable)", default=int(base.get("fps_cap") or 0))
            base["window_mode"] = Prompt.ask("Window mode", choices=["default", "freeform"], default=base.get("window_mode") or "default")
            base.pop("name", None)
            save_json(os.path.join(PROFILES, f"{name}.json"), base)
            console.print(f"[green]Saved profile '{name}'[/green]")
            time.sleep(1)
        elif c == "2":
            name = Prompt.ask("Profile name", choices=profs, default=profs[0] if profs else "default")
            console.print_json(json.dumps(load_profile(name)))
            Prompt.ask("Press Enter", default="")


def _menu_cookies():
    store = Store()
    while True:
        console.clear()
        cnt = store.counts()
        console.print(Panel(f"Queue: [cyan]{cnt['queue']}[/]  |  Done: [green]{cnt['done']}[/]  |  Dead: [red]{cnt['dead']}[/]", title="Accounts & Cookies"))
        console.print("[1] Paste cookie(s) (cookie or user:pass:cookie)  [2] Validate queue  [3] Auto-extract cookies  [4] Inject next cookie  [5] View switched/*.txt  [6] Fix/Sync cookies  [0] Back")
        c = Prompt.ask("Select", choices=["0", "1", "2", "3", "4", "5", "6"], default="0")
        if c == "0":
            return
        if c == "1":
            console.print("[dim]Paste cookies or user:pass:cookie (one per line, empty line to finish):[/dim]")
            lines = []
            while True:
                line = input()
                if not line.strip():
                    break
                lines.append(line)
            n = store.add_cookies("\n".join(lines))
            console.print(f"[green]Added {n} cookie(s)[/green]")
            time.sleep(1)
        elif c == "2":
            cmd_cookies(argparse.Namespace(action="validate"))
            Prompt.ask("Press Enter", default="")
        elif c == "3":
            rc = cmd_cookies(argparse.Namespace(action="extract", package=None, save=False, out=None, export=False, assign=False, show=False))
            if rc == 0:
                save_q = Confirm.ask("Save valid cookies to cookies.txt queue?", default=False)
                export_f = Confirm.ask(f"Export to {DEFAULT_EXPORT_PATH}?", default=False)
                bind_inst = Confirm.ask("Bind each cookie to its matching instance?", default=False)
                if save_q or export_f or bind_inst:
                    out_file = DEFAULT_EXPORT_PATH if export_f else None
                    cmd_cookies(argparse.Namespace(action="extract", package=None, save=save_q, out=out_file, export=False, assign=bind_inst, show=False))
            Prompt.ask("Press Enter", default="")
        elif c == "4":
            cfg = load_config()
            if not cfg["instances"]:
                continue
            pkg = Prompt.ask("Package", default=cfg["instances"][0]["package"])
            ck = store.pop()
            if not ck:
                console.print("[red]Queue is empty[/red]")
                time.sleep(1)
                continue
            st, uid, uname = whoami(ck)
            if st != "ok":
                console.print(f"[red]Cookie status={st}[/red]")
                if st == "dead":
                    store.add_dead("?", ck, "manual_inject_dead")
                else:
                    store.push_front(ck)
                time.sleep(1)
                continue
            force_stop(pkg)
            time.sleep(1)
            ok = write_app_cookie(pkg, ck, uid, uname)
            console.print(f"[{'green' if ok else 'red'}]Inject {uname} ({uid}): {'OK' if ok else 'FAILED'}[/]")
            time.sleep(1.5)
        elif c == "5":
            cmd_cookies(argparse.Namespace(action="switched"))
            Prompt.ask("Press Enter", default="")
        elif c == "6":
            donor = Prompt.ask("Donor package to clone cookie from (blank=sync appStorage only)", default="")
            cmd_cookies(argparse.Namespace(action="fix", donor=donor.strip() or None))
            Prompt.ask("Press Enter", default="")


def _menu_start():
    cfg = load_config()
    store = Store()
    from rjcore import Logger
    logger = Logger(echo=False)
    mgr = Manager(cfg, store, logger, enable_ipc=True)
    try:
        mgr.start()
    except Exception as e:
        console.print(f"[red]Cannot start: {e}[/red]")
        time.sleep(2)
        return

    try:
        with Live(_render_dashboard(mgr, store), console=console, refresh_per_second=1) as live:
            while not mgr.stop_event.is_set():
                live.update(_render_dashboard(mgr, store))
                if all(not w.is_alive() for w in mgr.workers.values()):
                    break
                time.sleep(1)
    except KeyboardInterrupt:
        pass
    mgr.stop()
    console.print("[yellow]Stopped.[/yellow]")
    time.sleep(1)


def _menu_scripts():
    while True:
        console.clear()
        execs = scan_executor_dirs()
        ae_dir = execs[0]["autoexec"] if execs else DEFAULT_AUTOEXEC
        cmd_scripts(argparse.Namespace(action="list", autoexec=ae_dir))
        console.print("\n[1] Toggle script ON/OFF  [2] Add script from file  [3] Delete script  [4] Deploy heartbeat to ALL executors  [0] Back")
        c = Prompt.ask("Select", choices=["0", "1", "2", "3", "4"], default="0")
        if c == "0":
            return
        try:
            if c == "1":
                name = Prompt.ask("Script filename (e.g. farm.lua)")
                cmd_scripts(argparse.Namespace(action="toggle", name=name, autoexec=ae_dir))
            elif c == "2":
                name = Prompt.ask("Target filename (e.g. farm.lua)")
                src = Prompt.ask("Source .lua file path")
                cmd_scripts(argparse.Namespace(action="add", name=name, file=src, code=None, autoexec=ae_dir))
            elif c == "3":
                name = Prompt.ask("Script filename to delete")
                cmd_scripts(argparse.Namespace(action="delete", name=name, autoexec=ae_dir))
            elif c == "4":
                cmd_deploy_lua(argparse.Namespace(autoexec=ae_dir, profile=None, all=True))
        except Exception as e:
            console.print(f"[red]{e}[/red]")
        Prompt.ask("Press Enter", default="")


def _menu_optimizer():
    while True:
        console.clear()
        st = get_system_stats()
        console.print(Panel(
            f"CPU: [cyan]{st['cpu_pct']:.0f}%[/]  |  RAM: [cyan]{st['ram_used_mb']:.0f}/{st['ram_total_mb']:.0f}MB ({st['ram_pct']:.0f}%)[/]  |  "
            f"Disk Free: [cyan]{st['disk_free_gb']}/{st['disk_total_gb']}GB[/]  |  Load: {st['load_1m']}",
            title="Window Tab Sorter, Optimizer & Cleaner",
            border_style="green",
        ))
        console.print("[1] Sort Roblox Tabs (Grid)  [2] Tiny Sort Tabs (Smallest Tiles)  [3] Open All Tabs  [4] Kill Mode  [5] Clear Cache  [6] Low-Graphics + FPS Cap  [7] Logout All Packages  [0] Back")
        c = Prompt.ask("Select", choices=["0", "1", "2", "3", "4", "5", "6", "7"], default="0")
        if c == "0":
            return
        if c == "1":
            cmd_tabs(argparse.Namespace(action="sort", package=None))
        elif c == "2":
            cmd_tabs(argparse.Namespace(action="tiny", package=None))
        elif c == "3":
            cmd_tabs(argparse.Namespace(action="open-all", package=None))
        elif c == "4":
            cmd_optimize(argparse.Namespace(package=None, kill_all=True, clear_cache=False, fps=None))
        elif c == "5":
            cmd_optimize(argparse.Namespace(package=None, kill_all=False, clear_cache=True, fps=None))
        elif c == "6":
            fps = IntPrompt.ask("Target FPS cap", default=15)
            cmd_optimize(argparse.Namespace(package=None, kill_all=False, clear_cache=False, fps=fps))
        elif c == "7":
            if Confirm.ask("Logout and remove cookies from ALL configured packages?", default=False):
                cmd_tabs(argparse.Namespace(action="logout", package=None, call_api=False))
        Prompt.ask("Press Enter", default="")


def _menu_daemon():
    while True:
        console.clear()
        ping = ipc_request("ping", timeout=1.0)
        alive = ping.get("ok")
        status_str = f"[green]RUNNING (pid={ping['result'].get('pid')})[/green]" if alive else "[dim]STOPPED[/dim]"
        console.print(Panel(f"Background Daemon IPC: {status_str}\nSocket: {DEFAULT_IPC_SOCK}", title="Daemon & IPC Manager"))
        console.print("[1] Start Daemon  [2] Attach Live Dashboard  [3] Stop Daemon  [4] Hot-Reload Config  [0] Back")
        c = Prompt.ask("Select", choices=["0", "1", "2", "3", "4"], default="0")
        if c == "0":
            return
        if c == "1":
            cmd_daemon(argparse.Namespace(action="start", profile=None, target=None, rejoin_min=None))
        elif c == "2":
            cmd_daemon(argparse.Namespace(action="attach"))
        elif c == "3":
            cmd_daemon(argparse.Namespace(action="stop"))
        elif c == "4":
            cmd_daemon(argparse.Namespace(action="reload"))
        Prompt.ask("Press Enter", default="")


def interactive_menu():
    while True:
        cfg = load_config()
        cnt = Store().counts()
        st = get_system_stats()
        enabled = sum(1 for i in cfg["instances"] if i.get("enabled", True))
        ping = ipc_request("ping", timeout=0.6)
        daemon_badge = "[green]ON[/]" if ping.get("ok") else "[dim]OFF[/]"
        console.clear()
        console.print(Panel(
            f"[bold cyan]Roblox Multi-Instance Auto-Rejoin v3.0 Hybrid (Rejoin + Shouko)[/bold cyan]\n"
            f"Root: {'[green]OK[/]' if has_root() else '[red]NO[/]'}  •  "
            f"Daemon: {daemon_badge}  •  "
            f"Instances: [cyan]{enabled}/{len(cfg['instances'])}[/]  •  "
            f"Queue: [yellow]{cnt['queue']}[/]  Done: [green]{cnt['done']}[/]  Dead: [red]{cnt['dead']}[/]\n"
            f"CPU: [cyan]{st['cpu_pct']:.0f}%[/]  •  RAM: [cyan]{st['ram_used_mb']:.0f}/{st['ram_total_mb']:.0f}MB ({st['ram_pct']:.0f}%)[/]  •  "
            f"Tool: [cyan]{st.get('tool_ram_mb', 0):.0f}MB[/]  •  Disk Free: [cyan]{st['disk_free_gb']}GB[/]",
            border_style="bright_blue",
        ))
        menu = Table(box=box.ROUNDED, show_header=False)
        menu.add_column(style="bold yellow", justify="center")
        menu.add_column()
        menu.add_row("1", "[green]Start Rejoin Monitor (Live Dashboard)[/green]")
        menu.add_row("2", "Background Daemon & Attach (IPC)")
        menu.add_row("3", "Quick Setup (Detect All Clones + Set Link + Sync Lua)")
        menu.add_row("4", "Manage Instances & Per-Clone Server Links")
        menu.add_row("5", "Manage Game Profiles & Rules (Executor/Online, Blox Fruits)")
        menu.add_row("6", "Manage Accounts & Cookies (user:pass:cookie / Extract / Switched)")
        menu.add_row("7", "Window Tab Sorter, Optimizer & Cleaner")
        menu.add_row("8", "Autoexecute Script Manager (Multi-Executor + Shared)")
        menu.add_row("9", "Download APK from GoFile / Run Benchmark")
        menu.add_row("10", "[cyan]Configuration (Shouko-Style Table)[/cyan]")
        menu.add_row("0", "[red]Exit[/red]")
        console.print(menu)

        choice = Prompt.ask("Choose", choices=["0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "10"], default="1")
        if choice == "0":
            break
        if choice == "1":
            _menu_start()
        elif choice == "2":
            _menu_daemon()
        elif choice == "3":
            t = Prompt.ask("Game ID or VIP/Share link (blank=keep profile)", default="")
            profs = list_profiles()
            pr = Prompt.ask("Profile", choices=profs, default="default" if "default" in profs else profs[0])
            cmd_quick_setup(argparse.Namespace(target=t, profile=pr, start=False))
            cmd_commands()
            Prompt.ask("Press Enter", default="")
        elif choice == "4":
            _menu_instances()
        elif choice == "5":
            _menu_profiles()
        elif choice == "6":
            _menu_cookies()
        elif choice == "7":
            _menu_optimizer()
        elif choice == "8":
            _menu_scripts()
        elif choice == "9":
            act = Prompt.ask("[1] Run Latency Benchmark  [2] Download APK from GoFile", choices=["1", "2"], default="1")
            if act == "1":
                cmd_benchmark()
            else:
                u = Prompt.ask("GoFile URL")
                cmd_gofile(argparse.Namespace(url=u, password="", download=True, install=Confirm.ask("Auto-install APK if downloaded?", default=False)))
            Prompt.ask("Press Enter", default="")
        elif choice == "10":
            cmd_config()


def build_parser():
    p = argparse.ArgumentParser(prog="rejoin", description="Multi-instance Roblox Auto-Rejoin v2.0 (Termux + Root)")
    sub = p.add_subparsers(dest="cmd")

    pr = sub.add_parser("run", help="Run monitor (headless by default, or --dashboard)")
    pr.add_argument("--instance", "-i", action="append", help="Instance name(s) to run")
    pr.add_argument("--profile", "-p", help="Override profile name")
    pr.add_argument("--target", "-t", help="Override Game ID or VIP/share link")
    pr.add_argument("--rejoin-min", type=float, help="Override mandatory rejoin_after_minutes")
    pr.add_argument("--dry-run", action="store_true", help="Dry run without killing/launching apps")
    pr.add_argument("--dashboard", action="store_true", help="Use Rich live dashboard UI")

    pd = sub.add_parser("detect", help="Detect installed Roblox/Executor packages")
    pd.add_argument("--add", action="store_true", help="Add newly detected packages to config.json")

    pl = sub.add_parser("deploy-lua", help="Deploy Autoexecute heartbeat script")
    pl.add_argument("--autoexec", help="Autoexecute directory path")
    pl.add_argument("--profile", help="Also deploy profile's custom Lua")
    pl.add_argument("--all", action="store_true", help="Deploy across all discovered executor directories")

    sub.add_parser("status", help="Show status of instances, profiles, daemon, system, and cookie queue")
    sub.add_parser("config", help="Open Shouko-style numbered configuration table")

    pcc = sub.add_parser("change-config", help="Manage Blox Fruits auto-change config (config-change.json)")
    pcc.add_argument("action", choices=["show", "edit", "reset", "set"])
    pcc.add_argument("--key", "-k", help="Condition key name (e.g. level, god_human)")
    pcc.add_argument("--value", "-v", help="Target value (e.g. 2550, true)")

    pc = sub.add_parser("cookies", help="Manage cookie queue & switched archives")
    pc.add_argument("action", choices=["list", "add", "validate", "extract", "switched", "fix"])
    pc.add_argument("--file", "-f", help="Read cookies from file")
    pc.add_argument("--cookie", "-c", help="Single cookie string")
    pc.add_argument("--package", "-k", help="Package name for extract (default: all)")
    pc.add_argument("--donor", help="Donor package for 'fix' action")
    pc.add_argument("--save", action="store_true", help="Save extracted valid cookies to cookies.txt")
    pc.add_argument("--export", action="store_true", help=f"Export extracted valid cookies to {DEFAULT_EXPORT_PATH}")
    pc.add_argument("--out", "-o", help="Export extracted valid cookies to a custom file path")
    pc.add_argument("--format", choices=["full", "user_cookie", "raw"], default="full", help="Export line format")
    pc.add_argument("--assign", action="store_true", help="Bind extracted cookies to matching instances")
    pc.add_argument("--show", action="store_true", help="Show full unmasked cookies in output")

    pp = sub.add_parser("profile", help="Manage game profiles")
    pp.add_argument("action", choices=["list", "show", "create"])
    pp.add_argument("--name", "-n", help="Profile name")
    pp.add_argument("--target", "-t", help="Place ID or VIP/share link")
    pp.add_argument("--rejoin-min", type=float, help="rejoin_after_minutes")

    ps = sub.add_parser("scripts", help="Manage Executor Autoexecute scripts")
    ps.add_argument("action", choices=["list", "add", "toggle", "delete"])
    ps.add_argument("--name", "-n", help="Script filename")
    ps.add_argument("--file", "-f", help="Source .lua file path")
    ps.add_argument("--code", help="Inline Lua code")
    ps.add_argument("--autoexec", help="Autoexecute directory path")

    po = sub.add_parser("optimize", help="Optimizer & cleaner tools")
    po.add_argument("--package", "-k", help="Target package (default: all)")
    po.add_argument("--fps", type=int, help="Apply low-graphics + FPS cap")
    po.add_argument("--clear-cache", action="store_true", help="Clear app & WebView cache only")
    po.add_argument("--kill-all", action="store_true", help="Kill Mode: stop all Roblox clones + am kill-all")

    pt = sub.add_parser("tabs", help="Arrange/open/logout Roblox window tabs")
    pt.add_argument("action", choices=["sort", "tiny", "open-all", "logout"])
    pt.add_argument("--package", "-k", help="Target package")
    pt.add_argument("--call-api", action="store_true", help="Also invalidate Roblox session via /v2/logout on logout")

    plk = sub.add_parser("links", help="Manage per-instance target game/VIP links")
    plk.add_argument("action", choices=["list", "set", "set-all", "import-shouko"])
    plk.add_argument("--instance", "-i", help="Instance name or package")
    plk.add_argument("--target", "-t", help="Game ID, VIP link, or share link")
    plk.add_argument("--file", "-f", help="Path to Shouko server_links.txt for import-shouko")

    pqs = sub.add_parser("quick-setup", help="1-step Shouko-style setup for all clones")
    pqs.add_argument("target", nargs="?", default="", help="Game ID, VIP link, or share link")
    pqs.add_argument("--profile", "-p", default="default", help="Profile name (default: default)")
    pqs.add_argument("--start", action="store_true", help="Start live dashboard immediately after setup")

    sub.add_parser("benchmark", help="Benchmark batched /proc, package detection, executor scan, and cookie read")

    pgf = sub.add_parser("gofile", help="List or download APK/files from GoFile")
    pgf.add_argument("url", help="GoFile share URL or folder ID")
    pgf.add_argument("--password", default="", help="Optional folder password")
    pgf.add_argument("--download", "-d", action="store_true", help="Download first file to /storage/emulated/0/Download")
    pgf.add_argument("--install", action="store_true", help="Download and run pm install -r if .apk")

    pdm = sub.add_parser("daemon", help="Manage background monitor daemon via local IPC")
    pdm.add_argument("action", choices=["start", "stop", "status", "attach", "reload"])
    pdm.add_argument("--profile", "-p", help="Profile for daemon start")
    pdm.add_argument("--target", "-t", help="Game ID or VIP link for daemon start")
    pdm.add_argument("--rejoin-min", type=float, help="rejoin_after_minutes for daemon start")

    pw = sub.add_parser("webhook-test", help="Send a test Discord Rich Embed")
    pw.add_argument("--screenshot", action="store_true", help="Attach current device screenshot")

    sub.add_parser("commands", help="Print QH-Rejoin-style copy-paste commands (Setup / Run root / Termux boot / Stop)")

    pb = sub.add_parser("boot", help="Manage Termux:Boot auto-start & background monitor")
    pb.add_argument("action", choices=["enable", "disable", "stop", "status"])
    pb.add_argument("--profile", "-p", help="Profile for boot auto-start")
    pb.add_argument("--target", "-t", help="Game ID or VIP link for boot auto-start")
    pb.add_argument("--rejoin-min", type=float, help="rejoin_after_minutes for boot auto-start")

    return p


def main():
    bootstrap()
    parser = build_parser()
    args = parser.parse_args()
    if not args.cmd:
        interactive_menu()
        return 0
    if args.cmd == "detect":
        return cmd_detect(args)
    if args.cmd == "deploy-lua":
        return cmd_deploy_lua(args)
    if args.cmd == "status":
        return cmd_status(args)
    if args.cmd == "config":
        return cmd_config(args)
    if args.cmd == "change-config":
        return cmd_change_config(args)
    if args.cmd == "boot":
        return cmd_boot(args)
    if args.cmd == "commands":
        return cmd_commands(args)
    if args.cmd == "cookies":
        return cmd_cookies(args)
    if args.cmd == "profile":
        return cmd_profile(args)
    if args.cmd == "scripts":
        return cmd_scripts(args)
    if args.cmd == "optimize":
        return cmd_optimize(args)
    if args.cmd == "tabs":
        return cmd_tabs(args)
    if args.cmd == "links":
        return cmd_links(args)
    if args.cmd == "quick-setup":
        return cmd_quick_setup(args)
    if args.cmd == "benchmark":
        return cmd_benchmark(args)
    if args.cmd == "gofile":
        return cmd_gofile(args)
    if args.cmd == "daemon":
        return cmd_daemon(args)
    if args.cmd == "webhook-test":
        return cmd_webhook_test(args)
    if args.cmd == "run":
        return cmd_run(args)
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
