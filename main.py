"""Interactive Rich menu/dashboard + Headless CLI + Daemon/IPC + Web Panel client for multi-instance Roblox Auto-Rejoin v4.0 Ultra."""
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
from log_watch import BatchLogWatcher
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
    read_clone_metadata,
    save_change_config,
    save_config,
    save_json,
    short,
    su,
    suggest_instance_name,
    sync_installed_instances,
    validate_cookies_parallel,
    whoami,
    write_app_cookie,
)
from shared_hub import (
    DEFAULT_DOWNLOAD_DIR,
    DEFAULT_HUB_DIR,
    DEFAULT_SHOUKO_DIR,
    ensure_hub_dirs,
    import_shouko_environment,
    ingest_hot_cookies,
    mirror_outputs_to_download,
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
    optimize_cloud_device,
    scan_executor_dirs,
    send_discord_embed,
    set_stay_awake,
    sort_roblox_tabs,
    toggle_autoexec_script,
)
from web_panel import WebPanelServer

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
        used_names = set()
        for i, pkg in enumerate(pkgs, 1):
            name = suggest_instance_name(pkg, i, used_names=used_names)
            used_names.add(name)
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
        save_config({
            "webhook": "",
            "stagger_launch_seconds": 8,
            "sort_tabs": True,
            "sort_tabs_interval_seconds": 15,
            "instances": instances,
        })
    elif os.environ.get("RJ_SKIP_PKG_SYNC") != "1":
        sync_installed_instances(load_config(), Store(), auto_add=True, prune_uninstalled=True, persist=True)


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


STATUS_LABELS_VI = {
    "RUNNING": "Đang chơi",
    "LAUNCHING": "Đang mở game",
    "WAITING_COOKIE": "Chờ thêm acc",
    "REJOINING": "Đang vào lại",
    "ROTATING": "Đang đổi acc",
    "PAUSED_CHALLENGE": "Dừng (Captcha/Xác minh)",
    "RETIRED": "Đã nghỉ (Acc xoá)",
    "STOPPED": "Đã dừng",
    "ERROR": "Báo lỗi",
    "IDLE": "Đang chờ",
}


def _build_dashboard_layout(rows, counts, sys_st, log_lines, switched=None, title_suffix=""):
    sw_txt = ""
    if switched:
        parts = [f"{k}:{v}" for k, v in switched.items() if v > 0]
        if parts:
            sw_txt = "  |  Đã phân loại: " + " ".join(parts)

    table = Table(
        title=(
            f"📊 Bảng Theo Dõi Auto Rejoin Roblox{title_suffix}  |  Kho Acc chờ: {counts['queue']}  Đã xong: {counts['done']}  Acc lỗi: {counts['dead']}{sw_txt}\n"
            f"[dim]CPU: {sys_st.get('cpu_pct', 0):.0f}%  |  RAM: {sys_st['ram_used_mb']:.0f}/{sys_st['ram_total_mb']:.0f}MB ({sys_st['ram_pct']:.0f}%)  |  "
            f"Tool: {sys_st.get('tool_ram_mb', 0):.0f}MB  |  Bộ nhớ trống: {sys_st['disk_free_gb']}GB  |  Tải máy: {sys_st['load_1m']}[/dim]"
        ),
        box=box.ROUNDED,
        border_style="bright_blue",
    )
    table.add_column("Tab", style="bold cyan")
    table.add_column("Gói App", style="dim")
    table.add_column("Cấu hình", style="magenta")
    table.add_column("Tài khoản", style="green")
    table.add_column("Trạng thái")
    table.add_column("RAM", justify="right")
    table.add_column("Tín hiệu", justify="right")
    table.add_column("Vào lại sau", justify="right")
    table.add_column("Vào lại/Đổi acc", justify="right")
    table.add_column("Thông tin / Sự kiện", style="dim")

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
        st_label = STATUS_LABELS_VI.get(st, st)
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
            f"[{col}]{st_label}[/{col}]",
            ram_str,
            hb_str,
            _fmt_sec(row.get("next_rejoin_in")),
            f"{row.get('rejoin_count', 0)}/{row.get('rotate_count', 0)}",
            info[:40],
        )

    log_panel = Panel(
        "\n".join(log_lines) or "[dim]Đang chờ sự kiện...[/dim]",
        title="Nhật ký hoạt động (Bấm Ctrl+C để thoát bảng)",
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
        console.print("[yellow]Không tìm thấy app Roblox / Executor nào đang cài trên máy.[/yellow]")
        return 1
    table = Table(title="Danh sách Tab Roblox / Executor đang cài trên máy", box=box.ROUNDED)
    table.add_column("STT", justify="right", style="cyan")
    table.add_column("Gói App", style="bold green")
    table.add_column("Thông tin Clone", style="dim")
    metas = {}
    for idx, p in enumerate(pkgs, 1):
        meta = read_clone_metadata(p)
        metas[p] = meta
        info = meta.get("app_name") or "-"
        if meta.get("clone_number"):
            info = f"#{meta['clone_number']} {info}".strip()
        table.add_row(str(idx), p, info)
    console.print(table)

    if getattr(args, "add", False):
        res = sync_installed_instances(cfg, Store(), auto_add=True, prune_uninstalled=True, persist=True)
        console.print(
            f"[green]Đã đồng bộ {len(cfg.get('instances', []))} Tab Roblox đang cài "
            f"(thêm mới {len(res['added'])}, gỡ bỏ {len(res['removed'])} tab cũ đã xoá).[/green]"
        )
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
    bg_ping = ipc_request("ping", timeout=1.0)
    bg_txt = f"[green]ĐANG CHẠY (PID {bg_ping['result'].get('pid')})[/]" if bg_ping.get("ok") else "[dim]ĐANG TẮT[/]"
    resize_txt = "[green]Thu nhỏ[/]" if cfg.get("tiny_sort_tabs") else ("[green]Tự chia ô[/]" if cfg.get("sort_tabs", True) else "[yellow]Đang tắt[/]")
    sw_non_zero = "  ".join(f"{k}={v}" for k, v in sw.items() if v > 0) or "-"

    console.print(
        Panel(
            f"Quyền Root: [{'green' if root_ok else 'red'}]{'ĐÃ CẤP' if root_ok else 'CHƯA CÓ'}[/]  |  "
            f"Treo ngầm 24/7: {bg_txt}  |  "
            f"Tự xếp cửa sổ: {resize_txt}  |  "
            f"Script Heartbeat: [{'green' if hb_ok else 'yellow'}]{'ĐÃ CÀI' if hb_ok else 'CHƯA CÀI'}[/]\n"
            f"Kho Acc chờ: [cyan]{counts['queue']}[/]  Đã xong: [green]{counts['done']}[/]  Acc lỗi: [red]{counts['dead']}[/]  |  Đã phân loại: [dim]{sw_non_zero}[/]\n"
            f"CPU: [cyan]{sys_st['cpu_pct']:.0f}%[/]  |  RAM: [cyan]{sys_st['ram_used_mb']:.0f}/{sys_st['ram_total_mb']:.0f}MB ({sys_st['ram_pct']:.0f}%)[/]  |  "
            f"Bộ nhớ trống: [cyan]{sys_st['disk_free_gb']}/{sys_st['disk_total_gb']}GB[/]  |  Tải máy: {sys_st['load_1m']}",
            title="Trạng thái Hệ thống Auto Rejoin",
            border_style="cyan",
        )
    )

    table = Table(title="Danh sách Tab Roblox đã cấu hình", box=box.ROUNDED)
    table.add_column("Tên Tab", style="bold cyan")
    table.add_column("Gói App")
    table.add_column("Cấu hình", style="magenta")
    table.add_column("Link Game / Place ID", style="dim")
    table.add_column("Trạng thái")
    table.add_column("Tài khoản đang gắn", style="green")

    assigned = store.all_assigned()
    for inst in cfg["instances"]:
        a = assigned.get(inst["name"]) or {}
        acc = f"{a.get('username', '-')} ({short(a.get('cookie'))})" if a else "-"
        table.add_row(
            inst["name"],
            inst["package"],
            inst.get("profile", "default"),
            (inst.get("target") or "(theo cấu hình)")[:28],
            "[green]Bật[/]" if inst.get("enabled", True) else "[dim]Tắt[/]",
            acc,
        )
    console.print(table)
    console.print(f"[dim]Danh sách cấu hình game: {', '.join(list_profiles()) or '-'}[/dim]")
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
        if getattr(args, "clean", False):
            res = store.prune_invalid_cookies(max_workers=8)
            console.print(
                f"[green]Pruned queue ({res['total']} checked):[/green] "
                f"OK=[green]{res['ok']}[/] | Dead=[red]{res['dead']}[/] | "
                f"Challenge=[yellow]{res['challenge']}[/] | Error={res['error']}"
            )
            return 0
        q = store.queue()
        if not q:
            console.print("[dim]Cookie queue is empty.[/dim]")
            return 0
        results = validate_cookies_parallel(q, max_workers=8)
        valid = 0
        for r in results:
            st = r["state"]
            uname = r.get("username") or "-"
            console.print(f"  {short(r['cookie'])} -> {st} ({uname})")
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
    if getattr(args, "cloud_tune", False):
        res = optimize_cloud_device(pkgs)
        console.print(
            f"[green]Cloud Tune:[/green] animations_off={res['animations_disabled']} | "
            f"doze_whitelisted={res['doze_whitelisted']} | trimmed={res['trimmed']}"
        )
    return 0


def cmd_web(args):
    cfg = load_config()
    host = getattr(args, "host", None) or cfg.get("web_host") or "127.0.0.1"
    port = int(getattr(args, "port", None) or cfg.get("web_port") or 8787)
    token = getattr(args, "token", None)
    if token is None:
        token = cfg.get("web_token") or ""
    if args.action in ("url", "status"):
        ping = ipc_request("ping", timeout=1.0)
        console.print(f"Web Panel URL: [cyan]http://{host}:{port}[/cyan] (auto-start={bool(cfg.get('web_enabled'))})")
        console.print(f"Background Daemon: {'[green]RUNNING[/]' if ping.get('ok') else '[dim]STOPPED (standalone mode available)[/]'}")
        return 0
    if args.action == "start":
        srv = WebPanelServer(host=host, port=port, token=token)
        url = srv.start()
        console.print(f"[green]Web Control Panel running at[/green] [bold cyan]{url}[/bold cyan] (Ctrl+C to stop)")
        try:
            while True:
                time.sleep(1.0)
        except KeyboardInterrupt:
            pass
        srv.stop()
        console.print("[yellow]Web Control Panel stopped.[/yellow]")
        return 0
    return 0


def cmd_hub(args):
    cfg = load_config()
    store = Store()
    store.ensure_files()
    hub_dir = cfg.get("download_hub_dir") or DEFAULT_HUB_DIR
    if args.action == "sync":
        ensure_hub_dirs(hub_dir)
        added = ingest_hot_cookies(store, hub_dir=hub_dir)
        mirrored = mirror_outputs_to_download(store, CHANGE_CONFIG, hub_dir=hub_dir)
        console.print(f"[green]Hub Sync ({hub_dir}):[/green] ingested {added} cookie(s), mirrored {mirrored} file(s).")
        return 0
    if args.action == "import-shouko":
        res = import_shouko_environment(store, cfg, shouko_dir=DEFAULT_SHOUKO_DIR, download_dir=DEFAULT_DOWNLOAD_DIR)
        save_config(cfg)
        ipc_request("reload_config", timeout=1.5)
        tbl = Table(title="Shouko / Download Import Summary", box=box.ROUNDED)
        tbl.add_column("Item", style="cyan")
        tbl.add_column("Result", style="green")
        tbl.add_row("Cookies imported", str(res["cookies_added"]))
        tbl.add_row("config-change.json", "YES" if res["change_config_imported"] else "not found")
        tbl.add_row("Autoexec Lua copied", str(res["autoexec_copied"]))
        tbl.add_row("Workspace files copied", str(res["workspace_copied"]))
        tbl.add_row("Server links imported", str(res["links_imported"]))
        console.print(tbl)
        return 0
    if args.action == "status":
        exists = os.path.isdir(hub_dir)
        ck_drop = os.path.join(DEFAULT_DOWNLOAD_DIR, "cookie.txt")
        console.print(Panel(
            f"Hub Dir: [cyan]{hub_dir}[/] ({'[green]READY[/]' if exists else '[dim]MISSING[/]'})\n"
            f"Hot Cookie Drop: [cyan]{ck_drop}[/] ({os.path.getsize(ck_drop) if os.path.isfile(ck_drop) else 0} B)\n"
            f"Auto-Sync in Engine: {'[green]ON[/]' if cfg.get('download_hub_sync', True) else '[red]OFF[/]'}",
            title="Download Hub Status",
            border_style="cyan",
        ))
        return 0
    return 0


def cmd_logs(_args=None):
    cfg = load_config()
    pkgs = [i["package"] for i in cfg.get("instances", []) if i.get("package")] or detect_packages()
    if not pkgs:
        console.print("[yellow]No Roblox packages found.[/yellow]")
        return 1
    res = BatchLogWatcher().poll_packages(pkgs)
    tbl = Table(title="Native Roblox Client Log Telemetry", box=box.ROUNDED)
    tbl.add_column("Package", style="cyan")
    tbl.add_column("State")
    tbl.add_column("Err", justify="right")
    tbl.add_column("Place ID", justify="right")
    tbl.add_column("Job ID", style="dim")
    tbl.add_column("Last Message", style="dim")
    for p in pkgs:
        d = res.get(p) or {}
        st = d.get("state") or "no_recent_log"
        col = "green" if st == "in_game" else ("yellow" if st == "joining" else ("red" if st == "disconnected" else "dim"))
        tbl.add_row(
            p,
            f"[{col}]{st}[/{col}]",
            str(d.get("error_code") or "-"),
            str(d.get("place_id") or "-"),
            (d.get("job_id") or "-")[:14],
            (d.get("message") or "-")[:45],
        )
    console.print(tbl)
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
        used_names = {i.get("name") for i in cfg["instances"] if i.get("name")}
        for p, uri in imported.items():
            if p in by_pkg:
                by_pkg[p]["target"] = uri
                updated += 1
            else:
                idx = len(cfg["instances"]) + 1
                name = suggest_instance_name(p, idx, used_names=used_names)
                used_names.add(name)
                execs = scan_executor_dirs(pkg=p)
                cfg["instances"].append({
                    "name": name,
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
            console.print(f"[yellow]Tool treo ngầm đang chạy sẵn rồi (PID {ping['result'].get('pid')}).[/yellow]")
            return 0
        run_cmd = [sys.executable, os.path.join(BASE, "main.py"), "run"]
        if getattr(args, "profile", None):
            run_cmd.extend(["--profile", args.profile])
        if getattr(args, "target", None):
            run_cmd.extend(["--target", args.target])
        if getattr(args, "rejoin_min", None) is not None:
            run_cmd.extend(["--rejoin-min", str(args.rejoin_min)])
        if getattr(args, "web", False):
            run_cmd.append("--web")
        if getattr(args, "web_port", None):
            run_cmd.extend(["--web-port", str(args.web_port)])
        if getattr(args, "web_host", None):
            run_cmd.extend(["--web-host", str(args.web_host)])
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
                console.print(f"[green]Đã bật treo ngầm 24/7 thành công (PID {proc.pid}).[/green]")
                return 0
        console.print(f"[yellow]Đã khởi động tiến trình chạy nền (PID {proc.pid}), xem chi tiết tại {log_path} nếu chưa kết nối.[/yellow]")
        return 0

    if args.action == "stop":
        res = ipc_request("stop", timeout=2.0)
        if res.get("ok"):
            console.print("[yellow]Đã gửi lệnh dừng treo ngầm an toàn.[/yellow]")
            return 0
        r = subprocess.run(["pkill", "-INT", "-f", "/rejoin_tool/main.py"], capture_output=True, text=True)
        if r.returncode != 0:
            su(f"pkill -INT -f '{PKILL_PATTERN}'", 10)
        console.print("[yellow]Đã gửi tín hiệu dừng tool đang chạy nền.[/yellow]")
        return 0

    if args.action == "reload":
        res = ipc_request("reload_config", timeout=3.0)
        if res.get("ok"):
            console.print(f"[green]Đã áp dụng cài đặt mới cho tool đang chạy: {res['result']}[/green]")
            return 0
        console.print(f"[red]Chưa thấy tool treo ngầm đang chạy ({res.get('error')}).[/red]")
        return 1

    if args.action == "status":
        res = ipc_request("status", timeout=2.0)
        if not res.get("ok"):
            console.print(f"[dim]Tool treo ngầm đang tắt ({res.get('error')}).[/dim]")
            return 1
        data = res["result"]
        layout = _build_dashboard_layout(
            data["instances"],
            data["counts"],
            data["system"],
            data.get("logs") or [],
            switched=data.get("switched"),
            title_suffix=" [Đang treo ngầm]",
        )
        console.print(layout)
        return 0

    if args.action == "attach":
        res = ipc_request("status", timeout=2.0)
        if not res.get("ok"):
            console.print(f"[red]Không thể xem bảng: tool treo ngầm chưa chạy ({res.get('error')}).[/red]")
            return 1
        try:
            with Live(
                _build_dashboard_layout(
                    res["result"]["instances"],
                    res["result"]["counts"],
                    res["result"]["system"],
                    res["result"].get("logs") or [],
                    switched=res["result"].get("switched"),
                    title_suffix=" [Đang treo ngầm]",
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
                            title_suffix=" [Đang treo ngầm]",
                        )
                    )
        except KeyboardInterrupt:
            pass
        console.print("[dim]Đã thoát bảng xem (tool vẫn tiếp tục treo ngầm bình thường).[/dim]")
        return 0
    return 0


def cmd_quick_setup(args):
    """Shouko-style 1-step setup: auto-detect all packages, assign link/profile, deploy Lua + shared scripts."""
    cfg = load_config()
    store = Store()
    sync_installed_instances(cfg, store, auto_add=True, prune_uninstalled=True, persist=False)
    pkgs = detect_packages(prefix=cfg.get("package_prefix", ""))
    if not pkgs:
        console.print("[red]Không tìm thấy app Roblox / Executor nào trên máy.[/red]")
        return 1
    prof_name = getattr(args, "profile", None) or "default"
    try:
        prof = load_profile(prof_name)
    except FileNotFoundError as e:
        console.print(f"[red]Không tìm thấy cấu hình '{prof_name}': {e}[/red]")
        return 1

    target = (getattr(args, "target", None) or "").strip()
    by_pkg = {i["package"]: i for i in cfg["instances"] if i.get("package")}
    for p in pkgs:
        if p in by_pkg:
            by_pkg[p]["profile"] = prof_name
            if target:
                by_pkg[p]["target"] = target
            by_pkg[p]["enabled"] = True
    save_config(cfg)
    ipc_request("reload_config", timeout=1.5)

    hub_dir = cfg.get("download_hub_dir") or DEFAULT_HUB_DIR
    ensure_hub_dirs(hub_dir)
    store.ensure_files()
    hot_added = ingest_hot_cookies(store, hub_dir=hub_dir)
    if hot_added:
        console.print(f"[green]Đã tự nạp {hot_added} cookie từ Download/cookie.txt.[/green]")

    extra_lua = None
    if prof.get("lua"):
        extra_lua = prof["lua"] if os.path.isabs(prof["lua"]) else os.path.join(PROFILES, "lua", prof["lua"])
    dep = deploy_all_executors(
        extra_lua_path=extra_lua,
        packages=pkgs,
        sync_shared=bool(cfg.get("sync_shared_scripts", True)),
    )
    console.print(f"[green]Cài đặt nhanh xong: {len(pkgs)} Tab Roblox, cấu hình='{prof_name}', đã chép {len(dep)} file Lua/Script.[/green]")
    if getattr(args, "start", False):
        return cmd_run(argparse.Namespace(instance=None, profile=None, target=None, rejoin_min=None, dry_run=False, dashboard=True, web=bool(cfg.get("web_enabled")), web_port=None, web_host=None))
    console.print("[dim]Xem lệnh chạy nhanh / Tự mở khi bật máy / Dừng tool: rejoin commands[/dim]")
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
    mgr = Manager(
        cfg,
        store,
        logger,
        enable_ipc=not args.dry_run,
        enable_web=True if getattr(args, "web", False) else None,
        web_host=getattr(args, "web_host", None),
        web_port=getattr(args, "web_port", None),
    )

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
    ("level", "Cấp độ tối thiểu (0=bỏ qua)", "int"),
    ("beli", "Số Beli tối thiểu (0=bỏ qua)", "int"),
    ("fragment", "Số Điểm F tối thiểu (0=bỏ qua)", "int"),
    ("bounty/honor", "Bounty / Honor tối thiểu (0=bỏ qua)", "int"),
    ("tier", "Cấp Tộc V4 tối thiểu (0=bỏ qua)", "int"),
    ("race_ver", "Tên Tộc / Phiên bản yêu cầu (trống=bất kỳ)", "str"),
    ("pull_level", "Đã gạt cần (Pull Lever)", "bool"),
    ("god_human", "Đã có Godhuman", "bool"),
    ("sanguine_art", "Đã có Sanguine Art", "bool"),
    ("cursed_dual_katana", "Đã có Song Kiếm (CDK)", "bool"),
    ("true_triple_katana", "Đã có Tam Kiếm (TTK)", "bool"),
    ("shark_anchor", "Đã có Mỏ Neo (Shark Anchor)", "bool"),
    ("skull_guitar", "Đã có Đàn (Skull Guitar)", "bool"),
    ("mirror_fractal", "Đã có Gương (Mirror Fractal)", "bool"),
    ("valkyrie_helm", "Đã có Mũ Valkyrie", "bool"),
]


def _menu_change_config():
    conf = load_change_config()
    while True:
        console.clear()
        table = Table(
            title="[bold cyan]Điều Kiện Tự Đổi Acc Blox Fruits (config-change.json)[/bold cyan]",
            box=box.ROUNDED,
            show_lines=True,
        )
        table.add_column("#", justify="right", style="bright_magenta", width=4)
        table.add_column("Điều kiện kiểm tra", style="cyan")
        table.add_column("Giá trị yêu cầu")
        for idx, (key, label, ftype) in enumerate(_CHANGE_BF_FIELDS, 1):
            val = conf.get(key)
            if ftype == "bool":
                disp = _yn(val)
            elif ftype == "int":
                disp = f"[yellow]{val}[/yellow]" if val else "[dim]0 (bỏ qua)[/dim]"
            else:
                disp = f"[yellow]{val}[/yellow]" if val else "[dim]bất kỳ (bỏ qua)[/dim]"
            table.add_row(str(idx), label, disp)
        table.add_row("R", "[yellow]Đặt lại mặc định (tắt mọi điều kiện)[/yellow]", "")
        table.add_row("S", "[bold green]Lưu & áp dụng ngay[/bold green]", "")
        table.add_row("X", "[bold red]Quay lại[/bold red]", "")
        console.print(table)

        ch = Prompt.ask("Chọn số (hoặc R=đặt lại, S=lưu, X=quay lại)", default="X").strip().upper()
        if ch == "X":
            return 0
        if ch == "R":
            conf = dict(DEFAULT_CHANGE_CONFIG)
            continue
        if ch == "S":
            save_change_config(conf)
            res = ipc_request("reload_config", timeout=1.5)
            console.print("[green]Đã lưu config-change.json" + (" & áp dụng ngay cho tool đang chạy.[/green]" if res.get("ok") else ".[/green]"))
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
        if key == "race_ver":
            conf[key] = str(raw_v or "").strip()
        elif isinstance(DEFAULT_CHANGE_CONFIG[key], bool):
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
        table = Table(title="[bold cyan]⚙ Cài Đặt Chung Auto Rejoin[/bold cyan]", box=box.ROUNDED, show_lines=True)
        table.add_column("#", justify="right", style="bright_magenta", width=4)
        table.add_column("Mục cài đặt", style="cyan")
        table.add_column("Trạng thái hiện tại")

        table.add_row("", "[bold blue]── TỰ XẾP CỬA SỔ & GIẢM LAG ──[/bold blue]", "")
        table.add_row("1", "Tự động chia ô cửa sổ Roblox (Auto Resize Tab)", _yn(cfg.get("sort_tabs", True)))
        table.add_row("2", "Xếp cửa sổ dạng ô thu nhỏ (Tiny Tabs)", _yn(cfg.get("tiny_sort_tabs")))
        table.add_row("3", "Chu kỳ tự xếp lại cửa sổ (giây)", f"[yellow]{cfg.get('sort_tabs_interval_seconds', 15)}[/yellow] giây")
        table.add_row("4", "Đóng toàn bộ tab nếu 1 tab bị văng", _yn(cfg.get("kill_all_on_crash")))
        table.add_row("5", "Chuyển về Termux trước khi tắt tab Roblox", _yn(cfg.get("focus_before_kill", True)))
        table.add_row("6", "Giữ màn hình luôn sáng (chống tắt màn)", _yn(cfg.get("stay_awake")))
        table.add_row("7", "Tự dọn RAM máy định kỳ (phút, 0=tắt)", f"[yellow]{cfg.get('auto_clean_ram_minutes', 0)}[/yellow] phút")

        table.add_row("", "[bold blue]── TÀI KHOẢN & TỰ ĐỔI ACC ──[/bold blue]", "")
        table.add_row("8", "Tự đổi acc kiểu Custom (<TênAcc>.txt)", _yn(cfg.get("change_acc_cus", True)))
        table.add_row("9", "Tự đổi acc Blox Fruits theo chỉ số", _yn(cfg.get("change_acc_bf", False)))
        table.add_row("10", "Chỉnh điều kiện đổi acc Blox Fruits (config-change.json)", "[dim]mở bảng chỉnh[/dim]")
        table.add_row("11", "Tự chặn chéo các acc (tránh trùng server thường)", _yn(cfg.get("auto_block_accounts", False)))
        table.add_row("12", "Tự đồng bộ thư mục Script & Workspace dùng chung", _yn(cfg.get("sync_shared_scripts", True)))
        table.add_row("13", "Lọc tên gói App (để trống = tự nhận tất cả)", cfg.get("package_prefix") or "[dim]tự động[/dim]")

        table.add_row("", "[bold blue]── THỜI GIAN MỞ TAB ──[/bold blue]", "")
        table.add_row("14", "Khoảng nghỉ giữa mỗi lần mở tab (giây)", f"[yellow]{cfg.get('stagger_launch_seconds', 8)}[/yellow] giây")
        table.add_row("15", "Thời gian chờ màn hình chờ -> vào game (giây)", f"[yellow]{cfg.get('open_delay_seconds', 2)}[/yellow] giây")
        table.add_row("16", "Thời gian nghỉ sau khi tắt app (giây)", f"[yellow]{cfg.get('sleep_after_kill_seconds', 2)}[/yellow] giây")

        table.add_row("", "[bold blue]── BÁO CÁO DISCORD WEBHOOK ──[/bold blue]", "")
        table.add_row("17", "Tên máy hiển thị trên Discord", cfg.get("device_name") or "[dim]chưa đặt[/dim]")
        table.add_row("18", "Link Discord Webhook (theo dõi chính)", "[green]đã gắn[/green]" if cfg.get("webhook") else "[red]chưa có[/red]")
        table.add_row("19", "Webhook riêng khi đổi acc", "[green]đã gắn[/green]" if cfg.get("webhook_change_url") else "[dim]dùng chung[/dim]")
        table.add_row("20", "Webhook riêng báo lỗi / Captcha", "[green]đã gắn[/green]" if cfg.get("webhook_alert_url") else "[dim]dùng chung[/dim]")
        table.add_row("21", "Gửi báo cáo định kỳ mỗi X phút (0=tắt)", f"[yellow]{cfg.get('webhook_interval_minutes', 0)}[/yellow] phút")
        table.add_row("22", "Chụp ảnh màn hình gửi kèm báo cáo Discord", _yn(cfg.get("webhook_screenshot")))
        table.add_row("23", "Tag người nhận thông báo (VD: @everyone)", cfg.get("webhook_ping") or "[dim]không[/dim]")

        table.add_row("", "[bold blue]── BẢNG WEB & CHỐNG TREO ──[/bold blue]", "")
        table.add_row("24", "Tự mở Bảng Điều Khiển Web khi chạy tool", _yn(cfg.get("web_enabled", False)))
        table.add_row("25", "Địa chỉ : Cổng Web", f"[cyan]{cfg.get('web_host', '127.0.0.1')}:{cfg.get('web_port', 8787)}[/cyan]")
        table.add_row("26", "Mật khẩu truy cập Bảng Web", "[green]đã đặt[/green]" if cfg.get("web_token") else "[dim]không (chỉ trên máy)[/dim]")
        table.add_row("27", "Tự hút Cookie & xuất file ở thư mục Download", _yn(cfg.get("download_hub_sync", True)))
        table.add_row("28", "Tự đóng bảng lỗi 'Ứng dụng không phản hồi'", _yn(cfg.get("auto_dismiss_anr", True)))
        table.add_row("29", "Tự nhảy server công khai khác khi mất kết nối", _yn(cfg.get("server_hop_on_disconnect", False)))
        table.add_row("30", "Xử lý khi gặp lỗi 264 (chờ/đổi acc)", f"[yellow]{'đổi acc' if cfg.get('error_264_action') == 'rotate' else 'nghỉ chờ'}[/yellow]")

        table.add_row("S", "[bold green]Lưu & áp dụng ngay[/bold green]", "")
        table.add_row("X", "[bold red]Huỷ & quay lại[/bold red]", "")
        console.print(table)

        ch = Prompt.ask("Chọn số (hoặc S=lưu, X=quay lại)", default="X").strip().upper()
        if ch == "X":
            return 0
        if ch == "S":
            save_config(cfg)
            if cfg.get("stay_awake"):
                set_stay_awake(True)
            res = ipc_request("reload_config", timeout=1.5)
            msg = "[green]Đã lưu cài đặt & áp dụng ngay cho tool đang chạy.[/green]" if res.get("ok") else "[green]Đã lưu cài đặt thành công.[/green]"
            console.print(msg)
            time.sleep(1)
            return 0
        if ch == "1":
            cfg["sort_tabs"] = not cfg.get("sort_tabs", True)
        elif ch == "2":
            cfg["tiny_sort_tabs"] = not cfg.get("tiny_sort_tabs", False)
        elif ch == "3":
            cfg["sort_tabs_interval_seconds"] = max(10, IntPrompt.ask("Chu kỳ tự xếp cửa sổ (giây, tối thiểu 10)", default=int(cfg.get("sort_tabs_interval_seconds") or 15)))
        elif ch == "4":
            cfg["kill_all_on_crash"] = not cfg.get("kill_all_on_crash", False)
        elif ch == "5":
            cfg["focus_before_kill"] = not cfg.get("focus_before_kill", True)
        elif ch == "6":
            cfg["stay_awake"] = not cfg.get("stay_awake", False)
        elif ch == "7":
            cfg["auto_clean_ram_minutes"] = max(0, IntPrompt.ask("Tự dọn RAM mỗi X phút (0=tắt)", default=int(cfg.get("auto_clean_ram_minutes") or 0)))
        elif ch == "8":
            cfg["change_acc_cus"] = not cfg.get("change_acc_cus", True)
        elif ch == "9":
            cfg["change_acc_bf"] = not cfg.get("change_acc_bf", False)
        elif ch == "10":
            _menu_change_config()
        elif ch == "11":
            cfg["auto_block_accounts"] = not cfg.get("auto_block_accounts", False)
        elif ch == "12":
            cfg["sync_shared_scripts"] = not cfg.get("sync_shared_scripts", True)
        elif ch == "13":
            cfg["package_prefix"] = Prompt.ask("Tiền tố tên gói app (để trống=tự nhận)", default=cfg.get("package_prefix") or "")
        elif ch == "14":
            cfg["stagger_launch_seconds"] = max(0, IntPrompt.ask("Khoảng nghỉ giữa mỗi lần mở tab (giây)", default=int(cfg.get("stagger_launch_seconds") or 8)))
        elif ch == "15":
            cfg["open_delay_seconds"] = max(0, IntPrompt.ask("Chờ màn hình chờ -> vào game (giây)", default=int(cfg.get("open_delay_seconds") or 2)))
        elif ch == "16":
            cfg["sleep_after_kill_seconds"] = max(0, IntPrompt.ask("Nghỉ sau khi tắt app (giây)", default=int(cfg.get("sleep_after_kill_seconds") or 2)))
        elif ch == "17":
            cfg["device_name"] = Prompt.ask("Tên máy hiển thị Discord (trống=xoá)", default=cfg.get("device_name") or "")
        elif ch == "18":
            cfg["webhook"] = Prompt.ask("Link Discord Webhook chính (trống=xoá)", default=cfg.get("webhook") or "")
        elif ch == "19":
            cfg["webhook_change_url"] = Prompt.ask("Link Webhook đổi acc (trống=dùng chung)", default=cfg.get("webhook_change_url") or "")
        elif ch == "20":
            cfg["webhook_alert_url"] = Prompt.ask("Link Webhook cảnh báo (trống=dùng chung)", default=cfg.get("webhook_alert_url") or "")
        elif ch == "21":
            cfg["webhook_interval_minutes"] = max(0, IntPrompt.ask("Gửi báo cáo định kỳ mỗi X phút (0=tắt)", default=int(cfg.get("webhook_interval_minutes") or 0)))
        elif ch == "22":
            cfg["webhook_screenshot"] = not cfg.get("webhook_screenshot", False)
        elif ch == "23":
            cfg["webhook_ping"] = Prompt.ask("Tag Discord khi báo lỗi (VD: @everyone, trống=không)", default=cfg.get("webhook_ping") or "")
        elif ch == "24":
            cfg["web_enabled"] = not cfg.get("web_enabled", False)
        elif ch == "25":
            cfg["web_host"] = Prompt.ask("Địa chỉ Web (127.0.0.1=trên máy, 0.0.0.0=LAN)", default=cfg.get("web_host") or "127.0.0.1").strip()
            cfg["web_port"] = max(1, min(65535, IntPrompt.ask("Cổng Web", default=int(cfg.get("web_port") or 8787))))
        elif ch == "26":
            cfg["web_token"] = Prompt.ask("Mật khẩu Web (trống=không đặt)", default=cfg.get("web_token") or "").strip()
        elif ch == "27":
            cfg["download_hub_sync"] = not cfg.get("download_hub_sync", True)
        elif ch == "28":
            cfg["auto_dismiss_anr"] = not cfg.get("auto_dismiss_anr", True)
        elif ch == "29":
            cfg["server_hop_on_disconnect"] = not cfg.get("server_hop_on_disconnect", False)
        elif ch == "30":
            cfg["error_264_action"] = Prompt.ask("Xử lý lỗi 264 (cooldown=nghỉ chờ, rotate=đổi acc)", choices=["cooldown", "rotate"], default=cfg.get("error_264_action") or "cooldown")
    return 0


# ───────────────────────── Interactive Menu ─────────────────────────
def _ensure_target_configured(cfg):
    """Prompt user for Game ID / VIP link if enabled tabs still have no target or place_id."""
    enabled_insts = [i for i in cfg.get("instances") or [] if i.get("enabled", True)]
    if not enabled_insts:
        console.print("[yellow]Chưa có Tab Roblox nào được bật. Hãy chọn 'Quản lý Tab Roblox' để thêm tab.[/yellow]")
        time.sleep(1.5)
        return False

    missing = []
    for inst in enabled_insts:
        if (inst.get("target") or "").strip():
            continue
        try:
            prof = load_profile(inst.get("profile", "default"))
        except FileNotFoundError:
            prof = dict(DEFAULT_PROFILE)
        if (prof.get("private_link") or "").strip():
            continue
        if int(prof.get("place_id") or 0) > 0:
            continue
        missing.append(inst)

    if not missing:
        return True

    console.print("\n[yellow]⚠ Bạn chưa điền ID Game hoặc Link Server VIP (VD: 2753915549 cho Blox Fruits).[/yellow]")
    raw = Prompt.ask("Nhập ID Game hoặc Link Server VIP ngay để bắt đầu (để trống = huỷ)", default="").strip()
    if not raw:
        return False

    parsed = parse_target(raw)
    if not parsed["place_id"] and not parsed["link_code"] and not parsed["share_code"]:
        console.print("[red]Link hoặc ID Game chưa đúng định dạng.[/red]")
        time.sleep(1.2)
        return False

    for inst in missing:
        inst["target"] = raw
    save_config(cfg)
    try:
        def_prof = load_profile("default")
        if int(def_prof.get("place_id") or 0) <= 0 and not (def_prof.get("private_link") or "").strip():
            if parsed["place_id"]:
                def_prof["place_id"] = int(parsed["place_id"])
            if parsed["link_code"] or parsed["share_code"] or "http" in raw:
                def_prof["private_link"] = raw
            def_prof.pop("name", None)
            save_json(os.path.join(PROFILES, "default.json"), def_prof)
    except Exception:
        pass
    console.print("[green]Đã gắn Link Game thành công![/green]")
    return True


def _menu_instances():
    while True:
        cfg = load_config()
        sync_installed_instances(cfg, Store(), auto_add=False, prune_uninstalled=True, persist=True)
        console.clear()
        table = Table(title="🎮 Danh sách Tab Roblox & Link Server", box=box.ROUNDED)
        table.add_column("#", justify="right", style="yellow")
        table.add_column("Tên Tab", style="cyan")
        table.add_column("Gói App")
        table.add_column("Cấu hình", style="magenta")
        table.add_column("Link Server / ID Game", style="green")
        table.add_column("Đổi acc", justify="center")
        table.add_column("Trạng thái", justify="center")
        for idx, i in enumerate(cfg["instances"], 1):
            table.add_row(
                str(idx),
                i["name"],
                i["package"],
                i.get("profile", "default"),
                (i.get("target") or "(theo cấu hình)")[:24],
                "Có" if i.get("use_cookie_queue", True) else "Không",
                "[green]Bật[/]" if i.get("enabled", True) else "[red]Tắt[/]",
            )
        console.print(table)
        console.print("\n[1] Tự quét & đồng bộ Tab  [2] Thêm thủ công  [3] Chọn cấu hình  [4] Gắn link cho 1 Tab  [5] Gắn 1 link cho TẤT CẢ  [6] Bật/Tắt Tab  [7] Xoá Tab  [0] Quay lại")
        c = Prompt.ask("Chọn", choices=["0", "1", "2", "3", "4", "5", "6", "7"], default="0")
        if c == "0":
            return
        if c == "1":
            cmd_detect(argparse.Namespace(add=True))
            Prompt.ask("Bấm Enter để tiếp tục", default="")
        elif c == "2":
            name = Prompt.ask("Tên Tab")
            pkg = Prompt.ask("Tên gói Android (package)")
            profs = list_profiles()
            prof = Prompt.ask("Cấu hình", choices=profs, default=profs[0] if profs else "default")
            use_q = Confirm.ask("Dùng kho Cookie để tự đổi acc?", default=True)
            cfg["instances"].append({
                "name": name, "package": pkg, "profile": prof, "target": "",
                "workspace": DEFAULT_WORKSPACE, "autoexec": DEFAULT_AUTOEXEC,
                "use_cookie_queue": use_q, "enabled": True,
            })
            save_config(cfg)
        elif c == "3" and cfg["instances"]:
            idx = IntPrompt.ask("Số thứ tự Tab #", default=1) - 1
            if 0 <= idx < len(cfg["instances"]):
                profs = list_profiles()
                cfg["instances"][idx]["profile"] = Prompt.ask("Cấu hình", choices=profs, default=profs[0])
                save_config(cfg)
        elif c == "4" and cfg["instances"]:
            idx = IntPrompt.ask("Số thứ tự Tab #", default=1) - 1
            if 0 <= idx < len(cfg["instances"]):
                t = Prompt.ask("ID Game / Link VIP / Link Share (để trống = dùng mặc định)", default=cfg["instances"][idx].get("target") or "")
                cfg["instances"][idx]["target"] = t.strip()
                save_config(cfg)
        elif c == "5" and cfg["instances"]:
            t = Prompt.ask("Nhập ID Game / Link VIP áp dụng cho TẤT CẢ Tab")
            cmd_links(argparse.Namespace(action="set-all", target=t.strip(), instance=None))
            time.sleep(1)
        elif c == "6" and cfg["instances"]:
            idx = IntPrompt.ask("Số thứ tự Tab #", default=1) - 1
            if 0 <= idx < len(cfg["instances"]):
                cfg["instances"][idx]["enabled"] = not cfg["instances"][idx].get("enabled", True)
                save_config(cfg)
        elif c == "7" and cfg["instances"]:
            idx = IntPrompt.ask("Số thứ tự Tab cần xoá #", default=1) - 1
            if 0 <= idx < len(cfg["instances"]):
                cfg["instances"].pop(idx)
                save_config(cfg)


def _menu_profiles():
    while True:
        console.clear()
        profs = list_profiles()
        table = Table(title="🎯 Cấu hình Game & Tự Đổi Acc", box=box.ROUNDED)
        table.add_column("Tên cấu hình", style="cyan")
        table.add_column("ID Game")
        table.add_column("Kiểu kiểm tra", style="yellow")
        table.add_column("Link VIP")
        table.add_column("Vào lại (phút)", justify="right")
        table.add_column("Điều kiện đổi acc")
        for p in profs:
            d = load_profile(p)
            table.add_row(
                p,
                str(d.get("place_id") or "-"),
                str(d.get("check_method") or "hybrid"),
                "Có" if d.get("private_link") else "-",
                str(d.get("rejoin_after_minutes")),
                ", ".join(d.get("rotate_on") or [])[:35],
            )
        console.print(table)
        console.print("\n[1] Tạo / Sửa cấu hình  [2] Xem chi tiết JSON  [0] Quay lại")
        c = Prompt.ask("Chọn", choices=["0", "1", "2"], default="0")
        if c == "0":
            return
        if c == "1":
            name = Prompt.ask("Tên cấu hình", default="default")
            base = load_profile(name) if name in profs else dict(DEFAULT_PROFILE)
            target = Prompt.ask("ID Game hoặc Link VIP/Share", default=base.get("private_link") or str(base.get("place_id") or ""))
            parsed = parse_target(target)
            if parsed["place_id"]:
                base["place_id"] = int(parsed["place_id"])
            if parsed["link_code"] or parsed["share_code"] or "http" in target:
                base["private_link"] = target
            else:
                base["private_link"] = ""
            base["check_method"] = Prompt.ask("Cách kiểm tra (hybrid=tổng hợp, executor=script, online=chỉ check mạng)", choices=["hybrid", "executor", "online"], default=base.get("check_method") or "hybrid")
            base["rejoin_after_minutes"] = IntPrompt.ask("Tự vào lại game định kỳ sau X phút (0=tắt)", default=int(base.get("rejoin_after_minutes") or 30))
            base["rotate_after_minutes"] = IntPrompt.ask("Hẹn giờ đổi acc sau X phút (0=tắt)", default=int(base.get("rotate_after_minutes") or 0))
            base["heartbeat_timeout"] = IntPrompt.ask("Thời gian mất tín hiệu script tối đa (giây)", default=int(base.get("heartbeat_timeout") or 45))
            base["launch_grace"] = IntPrompt.ask("Thời gian chờ mở game lúc mới bật (giây)", default=int(base.get("launch_grace") or 90))
            base["on_challenge"] = Prompt.ask("Khi gặp Captcha / Xác minh (pause=tạm dừng, rotate=đổi acc)", choices=["pause", "rotate", "ignore"], default=base.get("on_challenge") or "pause")
            base["open_splash_first"] = Confirm.ask("Mở màn hình chờ trước khi vào link game?", default=bool(base.get("open_splash_first", False)))
            rot = Prompt.ask("Điều kiện đổi acc (cách nhau dấu phẩy, VD: status:done,elapsed_account:120m)", default=",".join(base.get("rotate_on") or ["status:done"]))
            base["rotate_on"] = [x.strip() for x in rot.split(",") if x.strip()]
            rules_raw = Prompt.ask("Quy tắc thêm (cách nhau dấu phẩy, VD: kicked -> rejoin)", default=",".join(x for x in (base.get("rules") or []) if isinstance(x, str)))
            base["rules"] = [x.strip() for x in rules_raw.split(",") if x.strip()]
            base["lua"] = Prompt.ask("File Lua riêng trong profiles/lua/ (trống=không)", default=base.get("lua") or "")
            base["clear_cache_on_rejoin"] = Confirm.ask("Xoá cache app mỗi lần vào lại game?", default=bool(base.get("clear_cache_on_rejoin", False)))
            base["max_ram_mb"] = IntPrompt.ask("Tự vào lại nếu RAM tab vượt quá MB (0=tắt)", default=int(base.get("max_ram_mb") or 0))
            base["fps_cap"] = IntPrompt.ask("Giới hạn FPS + giảm đồ họa khi mở (0=tắt)", default=int(base.get("fps_cap") or 0))
            base["window_mode"] = Prompt.ask("Kiểu cửa sổ (freeform=chia ô nổi, default=mặc định)", choices=["freeform", "default"], default=base.get("window_mode") or "freeform")
            base.pop("name", None)
            save_json(os.path.join(PROFILES, f"{name}.json"), base)
            console.print(f"[green]Đã lưu cấu hình '{name}'[/green]")
            time.sleep(1)
        elif c == "2":
            name = Prompt.ask("Tên cấu hình", choices=profs, default=profs[0] if profs else "default")
            console.print_json(json.dumps(load_profile(name)))
            Prompt.ask("Bấm Enter để tiếp tục", default="")


def _menu_cookies():
    store = Store()
    while True:
        console.clear()
        cnt = store.counts()
        console.print(Panel(f"Kho Acc chờ: [cyan]{cnt['queue']}[/]  |  Đã cày xong: [green]{cnt['done']}[/]  |  Acc lỗi/die: [red]{cnt['dead']}[/]", title="🍪 Quản lý Tài khoản & Cookie"))
        console.print("[1] Dán thêm Cookie (cookie hoặc user:pass:cookie)  [2] Lọc kiểm tra Acc sống/chết  [3] Lấy Cookie đang đăng nhập trên máy  [4] Bơm Cookie kế tiếp vào Tab  [5] Xem acc đã phân loại  [6] Sửa / Đồng bộ đăng nhập  [0] Quay lại")
        c = Prompt.ask("Chọn", choices=["0", "1", "2", "3", "4", "5", "6"], default="0")
        if c == "0":
            return
        if c == "1":
            console.print("[dim]Dán cookie hoặc user:pass:cookie (mỗi acc 1 dòng, bấm Enter dòng trống để hoàn tất):[/dim]")
            lines = []
            while True:
                line = input()
                if not line.strip():
                    break
                lines.append(line)
            n = store.add_cookies("\n".join(lines))
            console.print(f"[green]Đã thêm {n} cookie vào kho chờ[/green]")
            time.sleep(1)
        elif c == "2":
            clean = Confirm.ask("Tự động loại bỏ acc die/ban/captcha khỏi hàng chờ luôn không?", default=True)
            cmd_cookies(argparse.Namespace(action="validate", clean=clean))
            Prompt.ask("Bấm Enter để tiếp tục", default="")
        elif c == "3":
            rc = cmd_cookies(argparse.Namespace(action="extract", package=None, save=False, out=None, export=False, assign=False, show=False))
            if rc == 0:
                save_q = Confirm.ask("Lưu các cookie sống vào kho chờ (cookies.txt)?", default=False)
                export_f = Confirm.ask(f"Xuất ra file {DEFAULT_EXPORT_PATH}?", default=False)
                bind_inst = Confirm.ask("Gắn từng cookie vào đúng Tab tương ứng?", default=False)
                if save_q or export_f or bind_inst:
                    out_file = DEFAULT_EXPORT_PATH if export_f else None
                    cmd_cookies(argparse.Namespace(action="extract", package=None, save=save_q, out=out_file, export=False, assign=bind_inst, show=False))
            Prompt.ask("Bấm Enter để tiếp tục", default="")
        elif c == "4":
            cfg = load_config()
            if not cfg["instances"]:
                continue
            pkg = Prompt.ask("Gói App nhận cookie", default=cfg["instances"][0]["package"])
            ck = store.pop()
            if not ck:
                console.print("[red]Kho Cookie đang trống[/red]")
                time.sleep(1)
                continue
            st, uid, uname = whoami(ck)
            if st != "ok":
                console.print(f"[red]Cookie lỗi trạng thái: {st}[/red]")
                if st == "dead":
                    store.add_dead("?", ck, "manual_inject_dead")
                else:
                    store.push_front(ck)
                time.sleep(1)
                continue
            force_stop(pkg)
            time.sleep(1)
            ok = write_app_cookie(pkg, ck, uid, uname)
            console.print(f"[{'green' if ok else 'red'}]Bơm acc {uname} ({uid}): {'THÀNH CÔNG' if ok else 'THẤT BẠI'}[/]")
            time.sleep(1.5)
        elif c == "5":
            cmd_cookies(argparse.Namespace(action="switched"))
            Prompt.ask("Bấm Enter để tiếp tục", default="")
        elif c == "6":
            donor = Prompt.ask("Gói app mẫu để chép sang máy khác (trống = chỉ đồng bộ lại tên acc)", default="")
            cmd_cookies(argparse.Namespace(action="fix", donor=donor.strip() or None))
            Prompt.ask("Bấm Enter để tiếp tục", default="")


def _menu_start():
    cfg = load_config()
    sync_installed_instances(cfg, Store(), auto_add=True, prune_uninstalled=True, persist=True)
    if not _ensure_target_configured(cfg):
        return
    cfg = load_config()
    store = Store()
    from rjcore import Logger
    logger = Logger(echo=False)
    mgr = Manager(cfg, store, logger, enable_ipc=True)
    try:
        mgr.start()
    except Exception as e:
        console.print(f"[red]Không thể khởi động: {e}[/red]")
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
    console.print("[yellow]Đã dừng Auto Rejoin.[/yellow]")
    time.sleep(1)


def _menu_scripts():
    while True:
        console.clear()
        execs = scan_executor_dirs()
        ae_dir = execs[0]["autoexec"] if execs else DEFAULT_AUTOEXEC
        cmd_scripts(argparse.Namespace(action="list", autoexec=ae_dir))
        console.print("\n[1] Bật/Tắt script  [2] Thêm script từ file  [3] Xoá script  [4] Cài script kết nối cho TẤT CẢ Executor  [0] Quay lại")
        c = Prompt.ask("Chọn", choices=["0", "1", "2", "3", "4"], default="0")
        if c == "0":
            return
        try:
            if c == "1":
                name = Prompt.ask("Tên file script (VD: farm.lua)")
                cmd_scripts(argparse.Namespace(action="toggle", name=name, autoexec=ae_dir))
            elif c == "2":
                name = Prompt.ask("Tên file muốn lưu (VD: farm.lua)")
                src = Prompt.ask("Đường dẫn file .lua nguồn")
                cmd_scripts(argparse.Namespace(action="add", name=name, file=src, code=None, autoexec=ae_dir))
            elif c == "3":
                name = Prompt.ask("Tên file script cần xoá")
                cmd_scripts(argparse.Namespace(action="delete", name=name, autoexec=ae_dir))
            elif c == "4":
                cmd_deploy_lua(argparse.Namespace(autoexec=ae_dir, profile=None, all=True))
        except Exception as e:
            console.print(f"[red]{e}[/red]")
        Prompt.ask("Bấm Enter để tiếp tục", default="")


def _menu_optimizer():
    while True:
        console.clear()
        st = get_system_stats()
        console.print(Panel(
            f"CPU: [cyan]{st['cpu_pct']:.0f}%[/]  |  RAM: [cyan]{st['ram_used_mb']:.0f}/{st['ram_total_mb']:.0f}MB ({st['ram_pct']:.0f}%)[/]  |  "
            f"Bộ nhớ trống: [cyan]{st['disk_free_gb']}/{st['disk_total_gb']}GB[/]  |  Tải máy: {st['load_1m']}",
            title="🪟 Xếp Cửa Sổ, Giảm Lag & Dọn Máy",
            border_style="green",
        ))
        console.print("[1] Chia ô cửa sổ Roblox (Lưới)  [2] Xếp cửa sổ thu nhỏ (Tiny)  [3] Mở tất cả Tab Roblox  [4] Tắt toàn bộ Tab (Kill Mode)  [5] Dọn bộ nhớ đệm (Cache)  [6] Giảm đồ họa + Khoá FPS  [7] Tối ưu máy Cloud (Tắt hiệu ứng + Chống ngủ)  [8] Đăng xuất toàn bộ Tab  [0] Quay lại")
        c = Prompt.ask("Chọn", choices=["0", "1", "2", "3", "4", "5", "6", "7", "8"], default="0")
        if c == "0":
            return
        if c == "1":
            cmd_tabs(argparse.Namespace(action="sort", package=None))
        elif c == "2":
            cmd_tabs(argparse.Namespace(action="tiny", package=None))
        elif c == "3":
            cmd_tabs(argparse.Namespace(action="open-all", package=None))
        elif c == "4":
            cmd_optimize(argparse.Namespace(package=None, kill_all=True, clear_cache=False, fps=None, cloud_tune=False))
        elif c == "5":
            cmd_optimize(argparse.Namespace(package=None, kill_all=False, clear_cache=True, fps=None, cloud_tune=False))
        elif c == "6":
            fps = IntPrompt.ask("Giới hạn FPS mục tiêu", default=15)
            cmd_optimize(argparse.Namespace(package=None, kill_all=False, clear_cache=False, fps=fps, cloud_tune=False))
        elif c == "7":
            cmd_optimize(argparse.Namespace(package=None, kill_all=False, clear_cache=False, fps=None, cloud_tune=True))
        elif c == "8":
            if Confirm.ask("Đăng xuất và xoá cookie khỏi TẤT CẢ các Tab Roblox?", default=False):
                cmd_tabs(argparse.Namespace(action="logout", package=None, call_api=False))
        Prompt.ask("Bấm Enter để tiếp tục", default="")


def _menu_daemon():
    while True:
        console.clear()
        ping = ipc_request("ping", timeout=1.0)
        alive = ping.get("ok")
        status_str = f"[green]ĐANG TREO NGẦM (PID {ping['result'].get('pid')})[/green]" if alive else "[dim]ĐANG TẮT[/dim]"
        console.print(Panel(f"Trạng thái chạy nền: {status_str}\n[dim]Chế độ này giúp tool tự động canh và vào lại game liên tục kể cả khi bạn đóng bảng này.[/dim]", title="🔄 Treo Ngầm 24/7 (Chạy Nền)"))
        console.print("[1] Bật treo ngầm ngay  [2] Xem bảng theo dõi trực tiếp  [3] Tắt treo ngầm  [4] Áp dụng cài đặt mới  [0] Quay lại")
        c = Prompt.ask("Chọn", choices=["0", "1", "2", "3", "4"], default="0")
        if c == "0":
            return
        if c == "1":
            cfg = load_config()
            sync_installed_instances(cfg, Store(), auto_add=True, prune_uninstalled=True, persist=True)
            if _ensure_target_configured(cfg):
                cmd_daemon(argparse.Namespace(action="start", profile=None, target=None, rejoin_min=None, web=bool(cfg.get("web_enabled"))))
        elif c == "2":
            cmd_daemon(argparse.Namespace(action="attach"))
        elif c == "3":
            cmd_daemon(argparse.Namespace(action="stop"))
        elif c == "4":
            cmd_daemon(argparse.Namespace(action="reload"))
        Prompt.ask("Bấm Enter để tiếp tục", default="")


def interactive_menu():
    while True:
        cfg = load_config()
        store = Store()
        sync_installed_instances(cfg, store, auto_add=True, prune_uninstalled=True, persist=True)
        cfg = load_config()
        cnt = store.counts()
        st = get_system_stats()
        enabled = sum(1 for i in cfg["instances"] if i.get("enabled", True))
        ping = ipc_request("ping", timeout=0.6)
        bg_badge = "[green]ĐANG CHẠY[/]" if ping.get("ok") else "[dim]ĐANG TẮT[/]"
        if cfg.get("tiny_sort_tabs"):
            resize_badge = "[green]Thu nhỏ[/]"
        elif cfg.get("sort_tabs", True):
            resize_badge = "[green]Đang bật[/]"
        else:
            resize_badge = "[yellow]Đang tắt[/]"
        web_badge = f"[green]Bật (:{cfg.get('web_port', 8787)})[/]" if cfg.get("web_enabled") else "[dim]Tắt[/]"
        console.clear()
        console.print(Panel(
            f"[bold cyan]🛠 BẢNG ĐIỀU KHIỂN AUTO REJOIN ROBLOX[/bold cyan]\n"
            f"Quyền Root: {'[green]Đã cấp[/]' if has_root() else '[red]Chưa có[/]'}  •  "
            f"Treo ngầm: {bg_badge}  •  "
            f"Tự xếp cửa sổ: {resize_badge}  •  "
            f"Bảng Web: {web_badge}\n"
            f"Tab Roblox: [cyan]{enabled}/{len(cfg['instances'])} đang bật[/]  •  "
            f"Kho Acc chờ: [yellow]{cnt['queue']}[/]  •  Đã cày xong: [green]{cnt['done']}[/]  •  Acc lỗi/die: [red]{cnt['dead']}[/]\n"
            f"CPU: [cyan]{st['cpu_pct']:.0f}%[/]  •  RAM: [cyan]{st['ram_used_mb']:.0f}/{st['ram_total_mb']:.0f}MB ({st['ram_pct']:.0f}%)[/]  •  "
            f"Bộ nhớ trống: [cyan]{st['disk_free_gb']}GB[/]",
            border_style="bright_blue",
        ))
        menu = Table(box=box.ROUNDED, show_header=False)
        menu.add_column(style="bold yellow", justify="center", width=4)
        menu.add_column()
        menu.add_row("1", "[bold green]▶ Bắt đầu Auto Rejoin (Chạy ngay & xem bảng trực tiếp)[/bold green]")
        menu.add_row("2", "🔄 Treo ngầm 24/7 (Chạy nền khi tắt bảng / Xem lại / Dừng tool)")
        menu.add_row("3", "⚡ Cài đặt nhanh 1 bước (Nhận Tab + Gắn Link Game + Chép Script)")
        menu.add_row("4", "🎮 Quản lý Tab Roblox & Link Server (ID Game / Link VIP)")
        menu.add_row("5", "🍪 Quản lý Tài khoản & Cookie (Thêm acc / Lọc acc die / Lấy cookie)")
        menu.add_row("6", "🎯 Tự Đổi Acc & Cấu hình Game (Blox Fruits / Kaitun / Hẹn giờ)")
        menu.add_row("7", "🪟 Xếp Cửa Sổ & Giảm Lag (Chia ô / Thu nhỏ / Khoá FPS / Dọn RAM)")
        menu.add_row("8", "📜 Quản lý Script Tự Chạy (Autoexec Delta / Codex / Arceus)")
        menu.add_row("9", "🌐 Bảng Điều Khiển Web (:8787) & Báo cáo Discord Webhook")
        menu.add_row("10", "🧰 Tiện ích mở rộng (Hút Cookie Download / Xem lỗi văng / Tự mở khi bật máy)")
        menu.add_row("11", "[cyan]⚙ Cài đặt chung[/cyan]")
        menu.add_row("0", "[red]✖ Thoát[/red]")
        console.print(menu)

        choice = Prompt.ask("Nhập lựa chọn", choices=["0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11"], default="1")
        if choice == "0":
            break
        if choice == "1":
            _menu_start()
        elif choice == "2":
            _menu_daemon()
        elif choice == "3":
            t = Prompt.ask("Nhập ID Game hoặc Link VIP/Share (để trống = giữ cũ)", default="")
            profs = list_profiles()
            pr = Prompt.ask("Chọn cấu hình", choices=profs, default="default" if "default" in profs else profs[0])
            cmd_quick_setup(argparse.Namespace(target=t, profile=pr, start=False))
            cmd_commands()
            Prompt.ask("Bấm Enter để tiếp tục", default="")
        elif choice == "4":
            _menu_instances()
        elif choice == "5":
            _menu_cookies()
        elif choice == "6":
            sub_c = Prompt.ask(
                "[1] Cấu hình Game & Tự đổi acc theo thời gian / cấp độ (Profiles)  [2] Điều kiện đổi acc Blox Fruits (config-change.json)  [0] Quay lại",
                choices=["0", "1", "2"],
                default="1",
            )
            if sub_c == "1":
                _menu_profiles()
            elif sub_c == "2":
                _menu_change_config()
        elif choice == "7":
            _menu_optimizer()
        elif choice == "8":
            _menu_scripts()
        elif choice == "9":
            sub_w = Prompt.ask(
                "[1] Mở Bảng Điều Khiển Web (:8787)  [2] Gửi tin nhắn thử Discord Webhook  [0] Quay lại",
                choices=["0", "1", "2"],
                default="1",
            )
            if sub_w == "1":
                cmd_web(argparse.Namespace(action="start", host=None, port=None, token=None))
            elif sub_w == "2":
                cmd_webhook_test(argparse.Namespace(screenshot=False))
                Prompt.ask("Bấm Enter để tiếp tục", default="")
        elif choice == "10":
            act = Prompt.ask(
                "[1] Đồng bộ Cookie & File ở Download  [2] Nhập từ thư mục Shouko cũ  [3] Xem lỗi văng game Roblox  [4] Bật/Tắt tự chạy khi mở máy  [5] Đo tốc độ máy  [6] Tải APK từ GoFile",
                choices=["1", "2", "3", "4", "5", "6"],
                default="1",
            )
            if act == "1":
                cmd_hub(argparse.Namespace(action="sync"))
            elif act == "2":
                cmd_hub(argparse.Namespace(action="import-shouko"))
            elif act == "3":
                cmd_logs()
            elif act == "4":
                b_act = Prompt.ask("Tự mở khi khởi động máy", choices=["enable", "disable", "status"], default="status")
                cmd_boot(argparse.Namespace(action=b_act, profile=None, target=None, rejoin_min=None))
            elif act == "5":
                cmd_benchmark()
            else:
                u = Prompt.ask("Dán link GoFile")
                cmd_gofile(argparse.Namespace(url=u, password="", download=True, install=Confirm.ask("Tự cài APK sau khi tải xong?", default=False)))
            Prompt.ask("Bấm Enter để tiếp tục", default="")
        elif choice == "11":
            cmd_config()


def build_parser():
    p = argparse.ArgumentParser(prog="rejoin", description="Multi-instance Roblox Auto-Rejoin v4.0 Ultra (Termux + Root)")
    sub = p.add_subparsers(dest="cmd")

    pr = sub.add_parser("run", help="Run monitor (headless by default, or --dashboard)")
    pr.add_argument("--instance", "-i", action="append", help="Instance name(s) to run")
    pr.add_argument("--profile", "-p", help="Override profile name")
    pr.add_argument("--target", "-t", help="Override Game ID or VIP/share link")
    pr.add_argument("--rejoin-min", type=float, help="Override mandatory rejoin_after_minutes")
    pr.add_argument("--dry-run", action="store_true", help="Dry run without killing/launching apps")
    pr.add_argument("--dashboard", action="store_true", help="Use Rich live dashboard UI")
    pr.add_argument("--web", action="store_true", help="Enable Web Control Panel while running")
    pr.add_argument("--web-port", type=int, help="Web Panel port (default 8787)")
    pr.add_argument("--web-host", help="Web Panel bind host (default 127.0.0.1)")

    pd = sub.add_parser("detect", help="Detect installed Roblox/Executor packages")
    pd.add_argument("--add", action="store_true", help="Add newly detected packages to config.json")

    pl = sub.add_parser("deploy-lua", help="Deploy Autoexecute heartbeat script")
    pl.add_argument("--autoexec", help="Autoexecute directory path")
    pl.add_argument("--profile", help="Also deploy profile's custom Lua")
    pl.add_argument("--all", action="store_true", help="Deploy across all discovered executor directories")

    sub.add_parser("status", help="Show status of instances, profiles, daemon, system, and cookie queue")
    sub.add_parser("config", help="Open v4.0 numbered configuration table")
    sub.add_parser("logs", help="Inspect native Roblox client log states & error codes")

    pweb = sub.add_parser("web", help="Manage standalone Web Control Panel")
    pweb.add_argument("action", choices=["start", "status", "url"])
    pweb.add_argument("--host", help="Bind host (127.0.0.1 or 0.0.0.0)")
    pweb.add_argument("--port", type=int, help="Port number (default 8787)")
    pweb.add_argument("--token", help="Optional Bearer API token")

    phub = sub.add_parser("hub", help="Manage /storage/emulated/0/Download hot-folder & Shouko import")
    phub.add_argument("action", choices=["sync", "import-shouko", "status"])

    pcc = sub.add_parser("change-config", help="Manage Blox Fruits auto-change config (config-change.json)")
    pcc.add_argument("action", choices=["show", "edit", "reset", "set"])
    pcc.add_argument("--key", "-k", help="Condition key name (e.g. level, god_human)")
    pcc.add_argument("--value", "-v", help="Target value (e.g. 2550, true)")

    pc = sub.add_parser("cookies", help="Manage cookie queue & switched archives")
    pc.add_argument("action", choices=["list", "add", "validate", "extract", "switched", "fix"])
    pc.add_argument("--file", "-f", help="Read cookies from file")
    pc.add_argument("--cookie", "-c", help="Single cookie string")
    pc.add_argument("--clean", action="store_true", help="Prune dead/banned/challenged cookies during validate")
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
    po.add_argument("--cloud-tune", action="store_true", help="Disable OS animations + whitelist Doze + trim background")

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
    pdm.add_argument("--web", action="store_true", help="Enable Web Control Panel inside daemon")
    pdm.add_argument("--web-port", type=int, help="Web Panel port")
    pdm.add_argument("--web-host", help="Web Panel host")

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
    if args.cmd == "logs":
        return cmd_logs(args)
    if args.cmd == "web":
        return cmd_web(args)
    if args.cmd == "hub":
        return cmd_hub(args)
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
