"""Multi-instance monitor engine: dual check modes (executor/online), health classification, IPC reload, tab sorting."""
from dataclasses import dataclass, field
import glob
import os
import threading
import time

from ipc import DEFAULT_IPC_SOCK, IPCServer
from lua_deploy import deploy_heartbeat
from rjcore import (
    BLOCK_CACHE,
    CHANGE_CONFIG,
    DEFAULT_AUTOEXEC,
    DEFAULT_PROFILE,
    DEFAULT_WORKSPACE,
    PROFILES,
    TMP,
    block_roblox_user,
    build_uri,
    check_presence,
    force_stop,
    info_matches_change_config,
    inspect_account_health,
    is_running,
    launch,
    load_change_config,
    load_config,
    load_json,
    load_profile,
    parse_custom_change_trigger,
    parse_target,
    read_app_cookie,
    read_app_storage_identity,
    resolve_share,
    sanitize_switch_type,
    save_json,
    short,
    su,
    whoami,
    write_app_cookie,
)
from rules import compile_rules, evaluate_rules
from sysutils import (
    apply_low_graphics,
    capture_screenshot,
    clear_app_cache,
    get_batch_pkg_usage,
    get_pkg_ram_mb,
    get_system_stats,
    remove_package_tasks,
    scan_executor_dirs,
    send_discord_embed,
    set_stay_awake,
    sort_roblox_tabs,
    stop_package,
)


@dataclass
class InstanceState:
    name: str
    package: str
    profile_name: str
    username: str = "-"
    user_id: int | None = None
    cookie_short: str = "-"
    status: str = "IDLE"
    hb_age: float | None = None
    pid: int | None = None
    ram_mb: float | None = None
    next_rejoin_in: float | None = None
    rejoin_count: int = 0
    rotate_count: int = 0
    consecutive_fails: int = 0
    check_method: str = "executor"
    presence_strikes: int = 0
    account_state: str = "unknown"
    stats: dict = field(default_factory=dict)
    last_event: str = ""
    account_started_at: float = 0.0
    session_started_at: float = 0.0

    def to_dict(self):
        return {
            "name": self.name,
            "package": self.package,
            "profile_name": self.profile_name,
            "username": self.username,
            "user_id": self.user_id,
            "cookie_short": self.cookie_short,
            "status": self.status,
            "hb_age": self.hb_age,
            "pid": self.pid,
            "ram_mb": self.ram_mb,
            "next_rejoin_in": self.next_rejoin_in,
            "rejoin_count": self.rejoin_count,
            "rotate_count": self.rotate_count,
            "consecutive_fails": self.consecutive_fails,
            "check_method": self.check_method,
            "presence_strikes": self.presence_strikes,
            "account_state": self.account_state,
            "stats": dict(self.stats),
            "last_event": self.last_event,
            "account_started_at": self.account_started_at,
            "session_started_at": self.session_started_at,
        }


class InstanceWorker(threading.Thread):
    def __init__(self, inst_cfg, manager, dry_run=False, profile_override=None):
        super().__init__(daemon=True, name=f"worker-{inst_cfg['name']}")
        self.inst = dict(inst_cfg)
        self.mgr = manager
        self.dry_run = dry_run
        self.store = manager.store
        self.log = manager.logger
        self.stop_event = manager.stop_event
        self.force_rotate_reason = None
        self._state_lock = threading.Lock()
        self._has_profile_override = profile_override is not None

        profile_name = inst_cfg.get("profile", "default")
        self.profile = dict(profile_override) if profile_override else load_profile(profile_name)
        self.rules = compile_rules(self.profile)

        self.workspace = inst_cfg.get("workspace") or DEFAULT_WORKSPACE
        self.autoexec = inst_cfg.get("autoexec") or DEFAULT_AUTOEXEC
        self.extra_workspaces = []
        if not self.dry_run:
            scanned = scan_executor_dirs(pkg=inst_cfg["package"])
            if scanned:
                if not os.path.isdir(self.workspace) and not os.path.isdir(self.autoexec):
                    self.workspace = scanned[0]["workspace"]
                    self.autoexec = scanned[0]["autoexec"]
                for entry in scanned:
                    ws_p = entry.get("workspace")
                    if ws_p and ws_p != self.workspace and ws_p not in self.extra_workspaces:
                        self.extra_workspaces.append(ws_p)

        self.use_cookie_queue = bool(inst_cfg.get("use_cookie_queue", True))

        self.cookie = None
        self.launch_ts = 0.0
        self.first_hb_seen = False
        self.last_presence_ts = 0.0
        self.notified_rules = set()
        self.challenge_notified = False
        self.target_place_id = None
        self.target_uri = None

        self.state = InstanceState(
            name=inst_cfg["name"],
            package=inst_cfg["package"],
            profile_name=self.profile.get("name", profile_name),
            check_method=str(self.profile.get("check_method") or "executor").lower(),
        )

    def _set_state(self, **kwargs):
        with self._state_lock:
            for k, v in kwargs.items():
                setattr(self.state, k, v)

    def apply_runtime_update(self, inst_cfg=None, profile_obj=None):
        """Hot-reload instance target & profile rules without restarting the worker."""
        with self._state_lock:
            if inst_cfg:
                self.inst.update(inst_cfg)
                self.use_cookie_queue = bool(self.inst.get("use_cookie_queue", True))
            if profile_obj and not self._has_profile_override:
                self.profile = dict(profile_obj)
                self.rules = compile_rules(self.profile)
                self.state.profile_name = self.profile.get("name", self.state.profile_name)
                self.state.check_method = str(self.profile.get("check_method") or "executor").lower()
        try:
            self._resolve_target()
        except Exception as e:
            self.log(self.state.name, f"Reload target warning: {e}")

    def snapshot(self):
        with self._state_lock:
            d = self.state.to_dict()
            rejoin_min = float(self.profile.get("rejoin_after_minutes") or 0)
            if rejoin_min > 0 and self.state.session_started_at > 0 and self.state.status in ("RUNNING", "LAUNCHING"):
                d["next_rejoin_in"] = max(0.0, rejoin_min * 60.0 - (time.time() - self.state.session_started_at))
            else:
                d["next_rejoin_in"] = None
            return d

    def request_rotate(self, reason="manual"):
        self.force_rotate_reason = reason

    def _hb_dir(self):
        return os.path.join(self.workspace, "rejoin")

    def _hb_path(self, user_id=None):
        uid = user_id or self.state.user_id
        if uid is not None:
            return os.path.join(self._hb_dir(), f"hb_{uid}.json")
        return None

    def _stop_pkg(self):
        return stop_package(self.state.package, use_focus=bool(self.mgr.config.get("focus_before_kill", True)))

    def _delete_old_hb(self):
        p = self._hb_path()
        if p and os.path.exists(p):
            try:
                os.remove(p)
            except OSError:
                pass

    def _workspace_dirs(self):
        dirs = []
        for ws in [self.workspace, *self.extra_workspaces]:
            if ws and ws not in dirs and os.path.isdir(ws):
                dirs.append(ws)
        return dirs

    def _cleanup_account_triggers(self, user_id, username, before_ts=None):
        names = []
        uname = str(username or "").strip()
        if uname and uname not in ("-", "?"):
            names.extend([
                f"{uname}.txt",
                f"{uname}.json",
                f"Completed-{uname}.txt",
                f"Completed-{uname}.json",
                f"{uname}-info.json",
            ])
        if user_id is not None:
            uid_s = str(user_id)
            names.extend([
                f"{uid_s}.txt",
                f"{uid_s}.json",
                f"Completed-{uid_s}.txt",
                f"Completed-{uid_s}.json",
                f"{uid_s}-info.json",
            ])
        if not names:
            return
        for ws in self._workspace_dirs():
            for fname in names:
                fpath = os.path.join(ws, fname)
                if not os.path.isfile(fpath):
                    continue
                if before_ts is not None:
                    try:
                        if os.path.getmtime(fpath) >= before_ts:
                            continue
                    except OSError:
                        pass
                try:
                    os.remove(fpath)
                except OSError:
                    pass

    def _check_workspace_account_change(self, hb=None):
        """Check Executor Workspace for custom (<User>.txt) or Blox Fruits (<UserId>-info.json) account changes."""
        uname = str(self.state.username or "").strip()
        has_uname = uname not in ("", "-", "?")
        uid = self.state.user_id

        # 1. Auto change account (Custom): Shouko/Yummy/Kaitun <Username>.txt or Completed-<Username/UserId>.txt/.json
        if self.mgr.config.get("change_acc_cus", True) and self.profile.get("custom_change_watch", True):
            comp_prefixes = []
            if has_uname:
                comp_prefixes.extend([f"Completed-{uname}.txt", f"Completed-{uname}.json"])
            if uid is not None:
                comp_prefixes.extend([f"Completed-{uid}.txt", f"Completed-{uid}.json"])

            for ws in self._workspace_dirs():
                for comp_name in comp_prefixes:
                    comp_path = os.path.join(ws, comp_name)
                    if os.path.isfile(comp_path):
                        try:
                            with open(comp_path, encoding="utf-8", errors="ignore") as f:
                                raw = f.read()
                        except OSError:
                            raw = ""
                        try:
                            os.remove(comp_path)
                        except OSError:
                            pass
                        parsed = parse_custom_change_trigger(raw)
                        sw = parsed["switch_type"] if parsed["matched"] else sanitize_switch_type(raw)
                        cat = f"Completed-{sw}" if sw else "Completed"
                        ctype = f"Custom ({sw})" if sw else "Custom"
                        reason = parsed["reason"] if parsed["matched"] else (f"Custom ({sw})" if sw else "Custom completed")
                        self.log(self.state.name, f"Custom change file detected: {comp_name} -> switched/{cat}.txt")
                        return "continue" if self._rotate(reason, dead=False, category=cat, change_type=ctype) else "break"

                candidates = []
                if has_uname:
                    candidates.extend([f"{uname}.txt", f"{uname}.json"])
                if uid is not None:
                    candidates.extend([f"{uid}.txt", f"{uid}.json"])

                for fname in candidates:
                    fpath = os.path.join(ws, fname)
                    if not os.path.isfile(fpath):
                        continue
                    try:
                        with open(fpath, encoding="utf-8", errors="ignore") as f:
                            raw = f.read()
                    except OSError:
                        continue
                    parsed = parse_custom_change_trigger(raw)
                    if not parsed["matched"]:
                        continue
                    try:
                        os.remove(fpath)
                    except OSError:
                        pass
                    sw = parsed["switch_type"]
                    cat = f"Completed-{sw}" if sw else "Completed"
                    ctype = f"Custom ({sw})" if sw else "Custom"
                    self.log(self.state.name, f"Custom change trigger {fname} -> switched/{cat}.txt")
                    return "continue" if self._rotate(parsed["reason"], dead=False, category=cat, change_type=ctype) else "break"

        # 2. Auto change account (Blox Fruit): config-change.json vs <UserId>-info.json or hb['stats']
        if self.mgr.config.get("change_acc_bf", False):
            conf = load_change_config(self.mgr.change_config_path)
            info_candidates = []
            if uid is not None:
                info_candidates.append(f"{uid}-info.json")
            if has_uname:
                info_candidates.append(f"{uname}-info.json")

            for ws in self._workspace_dirs():
                for fname in info_candidates:
                    fpath = os.path.join(ws, fname)
                    if not os.path.isfile(fpath):
                        continue
                    info = load_json(fpath, None)
                    matched, reasons = info_matches_change_config(info, conf)
                    if matched:
                        try:
                            os.remove(fpath)
                        except OSError:
                            pass
                        fruit = (info.get("fruit") or info.get("Fruit") or "I Don't Know") if isinstance(info, dict) else "I Don't Know"
                        reason_txt = ", ".join(reasons[:4]) or "Match information with config"
                        self.log(self.state.name, f"Blox Fruit config matched ({fname}: {reason_txt})")
                        ok = self._rotate(
                            reason_txt,
                            dead=False,
                            category="acc_ok",
                            change_type="Game condition matched",
                            extra_fields=[{"name": "Fruit", "value": str(fruit), "inline": True}],
                        )
                        return "continue" if ok else "break"

            if isinstance(hb, dict) and isinstance(hb.get("stats"), dict):
                stats = hb["stats"]
                matched, reasons = info_matches_change_config(stats, conf)
                if matched:
                    fruit = stats.get("fruit") or stats.get("Fruit") or "I Don't Know"
                    reason_txt = ", ".join(reasons[:4]) or "Match information with config"
                    self.log(self.state.name, f"Blox Fruit config matched via heartbeat ({reason_txt})")
                    ok = self._rotate(
                        reason_txt,
                        dead=False,
                        category="acc_ok",
                        change_type="Game condition matched",
                        extra_fields=[{"name": "Fruit", "value": str(fruit), "inline": True}],
                    )
                    return "continue" if ok else "break"

        return None

    def _read_hb(self):
        p = self._hb_path()
        if not p:
            cands = glob.glob(os.path.join(self._hb_dir(), "hb_*.json"))
            if cands:
                p = max(cands, key=os.path.getmtime)
        if p:
            try:
                mtime = os.path.getmtime(p)
                if mtime >= self.launch_ts - 2.0:
                    data = load_json(p, None)
                    if isinstance(data, dict):
                        return data, mtime
            except OSError:
                pass

        # Shouko <uid>.main sentinel fallback
        if self.state.user_id is not None:
            for ws in self._workspace_dirs():
                mp = os.path.join(ws, f"{self.state.user_id}.main")
                if os.path.isfile(mp):
                    try:
                        mtime = os.path.getmtime(mp)
                        if mtime >= self.launch_ts - 2.0:
                            data = load_json(mp, None)
                            if not isinstance(data, dict):
                                data = {"status": "running", "userId": self.state.user_id}
                            return data, mtime
                    except OSError:
                        pass
        return None, None

    def _resolve_target(self):
        inst_target = (self.inst.get("target") or "").strip()
        priv = inst_target or (self.profile.get("private_link") or "").strip()
        place_id = str(self.profile.get("place_id") or "").strip()
        job_id = (self.profile.get("job_id") or "").strip()

        parsed = parse_target(priv) if priv else {"place_id": None, "link_code": None, "share_code": None}
        if not parsed["place_id"] and place_id and place_id != "0":
            parsed["place_id"] = place_id

        if parsed["share_code"] and not parsed["link_code"]:
            if self.cookie and not self.dry_run:
                rp, rcode = resolve_share(self.cookie, parsed["share_code"])
                if rp and rcode:
                    parsed["place_id"] = rp
                    parsed["link_code"] = rcode
                else:
                    self.log(self.state.name, "Failed to resolve share link; check link or cookie")
            elif not parsed["link_code"]:
                parsed["link_code"] = parsed["share_code"]

        if not parsed["place_id"] or parsed["place_id"] == "0":
            raise ValueError(f"Profile '{self.state.profile_name}' has no valid place_id or private_link")

        self.target_place_id = int(parsed["place_id"])
        self.target_uri = build_uri(parsed["place_id"], parsed["link_code"], job_id or None)

    def _claim_or_pop_account(self):
        assigned = self.store.get_assign(self.state.name)
        if assigned and assigned.get("cookie"):
            c = assigned["cookie"]
            uid = assigned.get("user_id")
            uname = assigned.get("username")
            if not self.dry_run:
                st, v_uid, v_uname = whoami(c)
                if st == "ok":
                    self.cookie = c
                    self._set_state(
                        user_id=v_uid,
                        username=v_uname,
                        cookie_short=short(c),
                        account_state="ok",
                        account_started_at=assigned.get("started_at") or time.time(),
                    )
                    return True
                if st == "dead":
                    self.log(self.state.name, f"Assigned account {uname or '?'} expired; rotating")
                    self.store.add_dead(uname, c, "expired_at_start")
                    self.store.set_assign(self.state.name, None)
                else:
                    self.cookie = c
                    self._set_state(
                        user_id=uid,
                        username=uname or "?",
                        cookie_short=short(c),
                        account_state="ok",
                        account_started_at=assigned.get("started_at") or time.time(),
                    )
                    return True
            else:
                self.cookie = c
                self._set_state(
                    user_id=uid,
                    username=uname or "?",
                    cookie_short=short(c),
                    account_state="ok",
                    account_started_at=assigned.get("started_at") or time.time(),
                )
                return True

        # Try adopting existing app cookie if not assigned elsewhere
        if not self.dry_run:
            app_c = read_app_cookie(self.state.package)
            if app_c:
                in_use = {
                    v.get("cookie")
                    for k, v in self.store.all_assigned().items()
                    if k != self.state.name and isinstance(v, dict)
                }
                if app_c not in in_use:
                    st, uid, uname = whoami(app_c)
                    if st == "ok":
                        now = time.time()
                        self.cookie = app_c
                        self.store.set_assign(
                            self.state.name,
                            {"cookie": app_c, "user_id": uid, "username": uname, "started_at": now},
                        )
                        self._set_state(
                            user_id=uid,
                            username=uname,
                            cookie_short=short(app_c),
                            account_state="ok",
                            account_started_at=now,
                        )
                        self.log(self.state.name, f"Adopted app cookie for {uname} ({uid})")
                        return True

        if not self.use_cookie_queue:
            self._set_state(account_started_at=time.time(), last_event="no_cookie_mode")
            return True

        if self._pop_and_inject_next():
            return True
        if self.profile.get("when_no_cookies", "wait") == "wait":
            self._set_state(status="WAITING_COOKIE", last_event="waiting_cookies")
            return "wait"
        return False

    def _pop_and_inject_next(self):
        while not self.stop_event.is_set():
            nxt = self.store.pop()
            if not nxt:
                self.log(self.state.name, "No cookies left in cookies.txt")
                send_discord_embed(
                    self.mgr.config.get("webhook_alert_url") or self.mgr.config.get("webhook"),
                    f"[{self.state.name}] Out of Cookies",
                    "Queue is empty in `cookies.txt`.",
                    color=0xFEE75C,
                    ping=self.mgr.config.get("webhook_ping", ""),
                )
                return False

            if self.dry_run:
                now = time.time()
                self.cookie = nxt
                self.store.set_assign(
                    self.state.name,
                    {"cookie": nxt, "user_id": 1000, "username": "dry_run_user", "started_at": now},
                )
                self._set_state(
                    user_id=1000,
                    username="dry_run_user",
                    cookie_short=short(nxt),
                    account_state="ok",
                    account_started_at=now,
                )
                return True

            health = inspect_account_health(nxt)
            st = health["state"]
            if st in ("expired", "banned", "deleted", "warning"):
                self.log(self.state.name, f"Skipping {st} cookie {short(nxt)} -> switched/{st}.txt")
                self.store.add_dead("?", nxt, f"{st}_in_queue")
                if st != "expired":
                    self.store.add_switched(st, "?", nxt, health.get("reason") or st)
                continue
            if st in ("captcha", "faceid", "captcha_lock"):
                self.log(self.state.name, f"Skipping {st} cookie {short(nxt)} -> switched/{st}.txt")
                self.store.add_switched(st.replace("_", "-"), "?", nxt, health.get("reason") or st)
                continue
            if st == "error":
                self.log(self.state.name, f"Network error checking cookie {short(nxt)}; moving to queue tail")
                self.store.return_tail(nxt)
                self.stop_event.wait(5)
                return False

            uid, uname = health["user_id"], health["username"]
            self._stop_pkg()
            time.sleep(float(self.mgr.config.get("sleep_after_kill_seconds") or 1.0))
            if not write_app_cookie(self.state.package, nxt, uid, uname):
                self.log(self.state.name, f"Failed to inject cookie for {uname}; returning to queue tail")
                self.store.return_tail(nxt)
                return False

            now = time.time()
            self._cleanup_account_triggers(uid, uname)
            self.cookie = nxt
            self.challenge_notified = False
            self.store.set_assign(
                self.state.name,
                {"cookie": nxt, "user_id": uid, "username": uname, "started_at": now},
            )
            self._set_state(
                user_id=uid,
                username=uname,
                cookie_short=short(nxt),
                account_state="ok",
                account_started_at=now,
            )
            self.log(self.state.name, f"Switched account to {uname} ({uid})")
            return True
        return False

    def _launch(self, reason):
        with self.mgr.launch_lock:
            if self.stop_event.is_set():
                return
            stagger = float(self.mgr.config.get("stagger_launch_seconds") or 0)
            wait = stagger - (time.time() - self.mgr.last_launch_ts)
            if wait > 0 and self.mgr.last_launch_ts > 0:
                self.stop_event.wait(wait)
                if self.stop_event.is_set():
                    return

            self._set_state(status="LAUNCHING", presence_strikes=0, last_event=reason)
            self.log(self.state.name, f"Launching ({reason}) -> {self.target_uri}")
            self._delete_old_hb()
            if not self.dry_run:
                self._stop_pkg()
                if self.profile.get("clear_cache_on_rejoin"):
                    clear_app_cache(self.state.package)
                sleep_kill = float(self.mgr.config.get("sleep_after_kill_seconds") or 1.5)
                if sleep_kill > 0:
                    time.sleep(sleep_kill)
                extra = (self.profile.get("launch_extra") or "").strip()
                if self.profile.get("window_mode") == "freeform" and "--windowingMode" not in extra:
                    extra = (extra + " --windowingMode 5").strip()
                rc, _, err = launch(
                    self.state.package,
                    self.target_uri,
                    extra,
                    open_splash_first=bool(self.profile.get("open_splash_first", False)),
                    splash_delay=float(self.mgr.config.get("open_delay_seconds") or 2.0),
                )
                if rc != 0:
                    self.log(self.state.name, f"am start warning (rc={rc}): {err}")
            now = time.time()
            self.launch_ts = now
            self.first_hb_seen = False
            self.last_presence_ts = now
            self.mgr.last_launch_ts = now
            self._set_state(session_started_at=now, hb_age=None)

    def _rotate(self, reason, dead=False, category=None, change_type=None, extra_fields=None):
        self._set_state(status="ROTATING", last_event=reason)
        if not self.dry_run:
            force_stop(self.state.package)

        old_uname = self.state.username or "?"
        old_cookie = self.cookie
        archive_target = "dead.txt" if dead else "done.txt"

        if old_cookie:
            if dead:
                self.store.add_dead(old_uname, old_cookie, reason)
                if category and category != "dead":
                    self.store.add_switched(category, old_uname, old_cookie, reason)
                archive_target = f"switched/{category}.txt" if category else "dead.txt"
                self.log(self.state.name, f"Moved {old_uname} -> dead.txt ({reason})")
            elif category and category.startswith("Completed"):
                sw_part = category[len("Completed-"):] if category.startswith("Completed-") else ""
                cat_written = self.store.add_custom_completed(sw_part, old_uname, old_cookie, reason)
                archive_target = f"switched/{cat_written}.txt"
                self.log(self.state.name, f"Moved {old_uname} -> {archive_target} ({reason})")
            else:
                self.store.add_done(old_uname, old_cookie, reason)
                if category and category != "acc_ok":
                    self.store.add_switched(category, old_uname, old_cookie, reason)
                archive_target = f"switched/{category}.txt" if (category and category != "acc_ok") else "switched/acc_ok.txt"
                self.log(self.state.name, f"Moved {old_uname} -> done.txt ({reason})")

        self.store.set_assign(self.state.name, None)
        self.cookie = None
        self.notified_rules.clear()
        self.challenge_notified = False
        self._set_state(
            consecutive_fails=0,
            presence_strikes=0,
            rotate_count=self.state.rotate_count + 1,
            account_state="unknown",
            stats={},
        )

        switched_ok = False
        waiting_refill = False
        if not self.use_cookie_queue:
            self.log(self.state.name, "Queue disabled; stopping worker after rotation")
            self._set_state(status="STOPPED", last_event="queue_disabled")
        else:
            switched_ok = self._pop_and_inject_next()
            if not switched_ok:
                if self.profile.get("when_no_cookies", "wait") == "stop":
                    self._set_state(status="STOPPED", last_event="no_cookies")
                else:
                    waiting_refill = True
                    self._set_state(status="WAITING_COOKIE", last_event="waiting_cookies")

        if old_cookie:
            wh_change = self.mgr.config.get("webhook_change_url") or self.mgr.config.get("webhook")
            new_uname = self.state.username if switched_ok else ("Waiting for cookie" if waiting_refill else "None")
            fields = [
                {"name": "Old Username", "value": f"`{old_uname}`", "inline": True},
                {"name": "New Username", "value": f"`{new_uname}`", "inline": True},
                {"name": "Change Type", "value": str(change_type or ("Account Dead" if dead else "Completed")), "inline": True},
                {"name": "Archive", "value": f"`{archive_target}`", "inline": True},
                {"name": "Reason", "value": str(reason), "inline": False},
            ]
            if extra_fields:
                fields.extend(extra_fields)
            send_discord_embed(
                wh_change,
                f"[{self.state.name}] Account {'Dead' if dead else 'Changed'}",
                f"Auto change account for `{self.state.package}`.",
                color=0xED4245 if dead else 0x57F287,
                fields=fields,
            )

        if waiting_refill:
            return True
        if not switched_ok:
            return False

        self._resolve_target()
        self._launch("account_rotated")
        return True

    def _handle_passive_health_check(self):
        """Run passive inspect_account_health when launch fails/times out. Returns 'continue', 'break', or 'rejoin'."""
        if self.dry_run or not self.cookie:
            return "rejoin"
        health = inspect_account_health(self.cookie)
        st = health["state"]
        self._set_state(account_state=st)

        if st == "ok":
            self.challenge_notified = False
            return "rejoin"

        if st == "expired":
            if self._try_refresh_from_app():
                self._resolve_target()
                self._launch("cookie_refreshed")
                return "continue"
            if not self._rotate("expired_401", dead=True, category="dead"):
                return "break"
            return "continue"

        if st in ("banned", "deleted", "warning"):
            cat = st
            if st == "deleted" and self.profile.get("retire_on_deleted") and not self.store.queue():
                uname = self.state.username or "?"
                self.store.add_dead(uname, self.cookie, "account_deleted_retired")
                self.store.add_switched("deleted", uname, self.cookie, health.get("reason") or "deleted")
                self.store.set_assign(self.state.name, None)
                self._stop_pkg()
                remove_package_tasks(self.state.package)
                send_discord_embed(
                    self.mgr.config.get("webhook_alert_url") or self.mgr.config.get("webhook"),
                    f"[{self.state.name}] Account Deleted (Tab Retired)",
                    f"Account `{uname}` deleted; tab retired.",
                    color=0xED4245,
                    ping=self.mgr.config.get("webhook_ping", ""),
                )
                self._set_state(status="RETIRED", last_event="deleted_retired")
                return "break"
            if not self._rotate(f"moderation_{st}", dead=True, category=cat):
                return "break"
            return "continue"

        if st in ("captcha", "faceid", "captcha_lock"):
            policy = str(self.profile.get("on_challenge") or "pause").lower()
            cat = st.replace("_", "-")
            if policy == "rotate":
                if not self._rotate(f"challenge_{st}", dead=True, category=cat):
                    return "break"
                return "continue"
            if policy == "pause":
                if self.state.username in ("-", "?"):
                    s_uid, s_uname = read_app_storage_identity(self.state.package)
                    if s_uname:
                        self._set_state(username=s_uname, user_id=s_uid or self.state.user_id)
                self._set_state(status="PAUSED_CHALLENGE", last_event=st)
                if not self.challenge_notified:
                    self.challenge_notified = True
                    self.log(self.state.name, f"Challenge detected ({st}); pausing tab safely")
                    send_discord_embed(
                        self.mgr.config.get("webhook_alert_url") or self.mgr.config.get("webhook"),
                        f"[{self.state.name}] Challenge Detected ({st})",
                        f"Account `{self.state.username}` requires manual `{st}` verification. Rejoin paused.",
                        color=0xFEE75C,
                        ping=self.mgr.config.get("webhook_ping", ""),
                    )
                self.stop_event.wait(60.0)
                return "continue"

        return "rejoin"

    def _deploy_lua(self):
        if self.dry_run:
            return
        extra = (self.profile.get("lua") or "").strip()
        extra_path = None
        if extra:
            extra_path = extra if os.path.isabs(extra) else os.path.join(PROFILES, "lua", extra)
        deploy_heartbeat(
            self.autoexec,
            extra_path,
            workspace_dir=self.workspace,
            sync_shared=bool(self.mgr.config.get("sync_shared_scripts", True)),
        )

    def _eval_and_apply_rules(self, ctx):
        act, reason = evaluate_rules(self.rules, ctx)
        if act == "stop":
            self.log(self.state.name, f"Rule triggered STOP: {reason}")
            send_discord_embed(
                self.mgr.config.get("webhook"),
                f"[{self.state.name}] Stopped",
                f"Account `{self.state.username}` stopped by rule.",
                color=0xED4245,
                fields=[{"name": "Reason", "value": str(reason), "inline": True}],
            )
            if not self.dry_run:
                self._stop_pkg()
            self._set_state(status="STOPPED", last_event=reason)
            return "break"
        if act == "rotate":
            if not self._rotate(reason):
                return "break"
            return "continue"
        if act == "rejoin":
            self.log(self.state.name, f"Rule triggered REJOIN: {reason}")
            self._set_state(rejoin_count=self.state.rejoin_count + 1)
            self._launch(reason)
            return "continue"
        if act == "notify":
            self.log(self.state.name, f"Rule notify: {reason}")
            send_discord_embed(
                self.mgr.config.get("webhook"),
                f"[{self.state.name}] Rule Notification",
                f"Account `{self.state.username}` matched rule.",
                color=0x5865F2,
                fields=[{"name": "Event", "value": str(reason), "inline": True}],
            )
        return None

    def run(self):
        claim_res = False
        try:
            self._deploy_lua()
            fps_cap = int(self.profile.get("fps_cap") or 0)
            if fps_cap > 0 and not self.dry_run:
                apply_low_graphics(self.state.package, fps_cap=fps_cap, low_quality=True)
            claim_res = self._claim_or_pop_account()
            if not claim_res:
                self._set_state(status="STOPPED", last_event="no_account")
                return
            self._resolve_target()
        except Exception as e:
            self.log(self.state.name, f"Startup error: {e}")
            self._set_state(status="ERROR", last_event=str(e))
            return

        if claim_res != "wait":
            self._launch("initial_start")
        last_ram_ts = 0.0

        while not self.stop_event.wait(max(0.2, float(self.profile.get("poll_interval") or 3))):
            if self.force_rotate_reason:
                r = self.force_rotate_reason
                self.force_rotate_reason = None
                if not self._rotate(r):
                    break
                continue

            if self.state.status == "WAITING_COOKIE":
                if self.store.counts()["queue"] > 0 and self._pop_and_inject_next():
                    self._resolve_target()
                    self._launch("cookie_refilled")
                continue

            now = time.time()
            hb_timeout = float(self.profile.get("heartbeat_timeout") or 45)
            launch_grace = float(self.profile.get("launch_grace") or 90)
            rejoin_min = float(self.profile.get("rejoin_after_minutes") or 0)
            rotate_min = float(self.profile.get("rotate_after_minutes") or 0)
            max_ram_mb = float(self.profile.get("max_ram_mb") or 0)
            check_method = str(self.profile.get("check_method") or "executor").lower()

            if rotate_min > 0 and self.state.account_started_at > 0:
                if (now - self.state.account_started_at) >= rotate_min * 60.0:
                    self.log(self.state.name, f"Scheduled account rotation after {rotate_min:g}m")
                    if not self._rotate("scheduled_rotate"):
                        break
                    continue

            if not self.dry_run and (now - last_ram_ts) >= 15.0:
                last_ram_ts = now
                rmb = self.state.ram_mb
                if rmb is None:
                    rmb = get_pkg_ram_mb(self.state.package)
                    self._set_state(ram_mb=rmb)
                if max_ram_mb > 0 and rmb is not None and rmb > max_ram_mb:
                    self.log(self.state.name, f"RAM {rmb:.0f}MB exceeded limit {max_ram_mb:.0f}MB; rejoining")
                    self._set_state(rejoin_count=self.state.rejoin_count + 1)
                    self._launch(f"ram>{int(max_ram_mb)}MB")
                    continue

            # If paused on challenge, re-verify passively before doing anything else
            if self.state.status == "PAUSED_CHALLENGE":
                res = self._handle_passive_health_check()
                if res == "break":
                    break
                if res == "rejoin":
                    self._launch("challenge_cleared")
                continue

            # Check Workspace custom / info-file triggers in BOTH executor & online modes
            ws_act = self._check_workspace_account_change(hb=None)
            if ws_act == "break":
                break
            if ws_act == "continue":
                continue

            # ── Mode A: Online Presence Check (non-executor / VNG) ──
            if check_method == "online":
                proc_dead = (not self.dry_run) and (now - self.launch_ts > 15.0) and (not is_running(self.state.package))
                if proc_dead:
                    if self.mgr.config.get("kill_all_on_crash"):
                        self.mgr.trigger_group_restart(self.state.name)
                    res = self._handle_passive_health_check()
                    if res == "break":
                        break
                    if res == "continue":
                        continue
                    fails = self.state.consecutive_fails + 1
                    self._set_state(status="REJOINING", consecutive_fails=fails, rejoin_count=self.state.rejoin_count + 1, last_event="process_died")
                    self._launch("process_died")
                    continue

                if rejoin_min > 0 and (now - self.state.session_started_at) >= rejoin_min * 60.0:
                    self.log(self.state.name, f"Scheduled rejoin after {rejoin_min:g}m")
                    self._set_state(rejoin_count=self.state.rejoin_count + 1)
                    self._launch("scheduled_rejoin")
                    continue

                poll_p = max(10.0, float(self.profile.get("presence_poll_seconds") or 30))
                strikes_req = max(1, int(self.profile.get("presence_recheck_strikes") or 3))
                if (now - self.launch_ts) >= launch_grace and (now - self.last_presence_ts) >= poll_p:
                    self.last_presence_ts = now
                    if self.dry_run or not self.cookie or not self.state.user_id:
                        self._set_state(status="RUNNING", presence_strikes=0)
                    else:
                        ptype, api_place, _ = check_presence(self.state.user_id, self.cookie)
                        if ptype is None:
                            self.log(self.state.name, "Presence API unavailable - keeping tab")
                        elif ptype == 2 and (not api_place or not self.target_place_id or int(api_place) == int(self.target_place_id)):
                            self._set_state(status="RUNNING", presence_strikes=0, consecutive_fails=0, last_event="in_game")
                        else:
                            strikes = self.state.presence_strikes + 1
                            self._set_state(presence_strikes=strikes, last_event=f"not_ingame_{strikes}/{strikes_req}")
                            self.log(self.state.name, f"Not in-game (type={ptype}), strike {strikes}/{strikes_req}")
                            if strikes >= strikes_req:
                                res = self._handle_passive_health_check()
                                if res == "break":
                                    break
                                if res == "continue":
                                    continue
                                fails = self.state.consecutive_fails + 1
                                self._set_state(status="REJOINING", consecutive_fails=fails, rejoin_count=self.state.rejoin_count + 1)
                                self._launch("presence_not_ingame")
                                continue
                elif (now - self.launch_ts) < launch_grace:
                    self._set_state(status="LAUNCHING")
                continue

            # ── Mode B: Executor Heartbeat Check ──
            hb, mtime = self._read_hb()

            if hb is not None:
                hb_age = max(0.0, now - mtime)
                self.first_hb_seen = True
                stats = hb.get("stats") if isinstance(hb.get("stats"), dict) else {}
                updates = {
                    "hb_age": hb_age,
                    "stats": stats,
                    "consecutive_fails": 0,
                }
                if hb.get("username") and self.state.username in ("-", "?"):
                    updates["username"] = str(hb["username"])
                if hb.get("userId") and self.state.user_id is None:
                    updates["user_id"] = int(hb["userId"])
                if hb_age <= hb_timeout:
                    updates["status"] = "RUNNING"
                self._set_state(**updates)

                if self.mgr.config.get("change_acc_bf", False) or self.mgr.config.get("change_acc_cus", True):
                    ws_act2 = self._check_workspace_account_change(hb=hb)
                    if ws_act2 == "break":
                        break
                    if ws_act2 == "continue":
                        continue

                # Wrong place check
                hb_place = hb.get("placeId")
                if (
                    self.target_place_id
                    and hb_place
                    and int(hb_place) != int(self.target_place_id)
                    and hb_age < 15.0
                    and (now - self.launch_ts) > 20.0
                ):
                    self.log(self.state.name, f"Wrong placeId ({hb_place} != {self.target_place_id}); rejoining")
                    self._set_state(rejoin_count=self.state.rejoin_count + 1)
                    self._launch("wrong_place")
                    continue

                ctx = {
                    "hb": hb,
                    "account_state": self.state.account_state,
                    "elapsed_account_sec": now - (self.state.account_started_at or now),
                    "elapsed_session_sec": now - (self.state.session_started_at or now),
                    "consecutive_fails": self.state.consecutive_fails,
                    "notified_rules": self.notified_rules,
                }
                outcome = self._eval_and_apply_rules(ctx)
                if outcome == "break":
                    break
                if outcome == "continue":
                    continue

                # Mandatory X-minute rejoin timer
                if rejoin_min > 0 and (now - self.state.session_started_at) >= rejoin_min * 60.0:
                    self.log(self.state.name, f"Scheduled rejoin after {rejoin_min:g}m")
                    self._set_state(rejoin_count=self.state.rejoin_count + 1)
                    self._launch("scheduled_rejoin")
                    continue

                # Stale heartbeat
                if hb_age > hb_timeout:
                    fails = self.state.consecutive_fails + 1
                    self._set_state(
                        status="REJOINING",
                        consecutive_fails=fails,
                        rejoin_count=self.state.rejoin_count + 1,
                        last_event=f"stale_{int(hb_age)}s",
                    )
                    self.log(self.state.name, f"Heartbeat stale ({hb_age:.0f}s > {hb_timeout:.0f}s)")
                    ctx["consecutive_fails"] = fails
                    outcome2 = self._eval_and_apply_rules(ctx)
                    if outcome2 == "break":
                        break
                    if outcome2 == "continue":
                        continue
                    h_res = self._handle_passive_health_check()
                    if h_res == "break":
                        break
                    if h_res == "continue":
                        continue
                    backoff = min(60.0, 5.0 * fails)
                    if not self.dry_run:
                        self.stop_event.wait(backoff)
                    self._launch("heartbeat_timeout")
                    continue

                # Optional Presence secondary check
                if self.profile.get("check_presence") and not self.dry_run and self.cookie and self.state.user_id:
                    if now - self.last_presence_ts >= 60.0:
                        self.last_presence_ts = now
                        ptype, _, _ = check_presence(self.state.user_id, self.cookie)
                        if ptype is not None and ptype != 2 and hb_age > 15.0:
                            self.log(self.state.name, f"Presence offline/non-game (type={ptype}); rejoining")
                            self._set_state(rejoin_count=self.state.rejoin_count + 1)
                            self._launch("presence_not_ingame")
                            continue

            else:
                proc_dead = (not self.dry_run) and (now - self.launch_ts > 15.0) and (not is_running(self.state.package))
                grace_expired = (now - self.launch_ts) > launch_grace
                if proc_dead or grace_expired:
                    why = "process_died" if proc_dead else "launch_grace_expired"
                    if proc_dead and self.mgr.config.get("kill_all_on_crash"):
                        self.mgr.trigger_group_restart(self.state.name)
                    fails = self.state.consecutive_fails + 1
                    self._set_state(
                        status="REJOINING",
                        consecutive_fails=fails,
                        rejoin_count=self.state.rejoin_count + 1,
                        last_event=why,
                    )
                    self.log(self.state.name, f"No heartbeat ({why}, fails={fails})")

                    h_res = self._handle_passive_health_check()
                    if h_res == "break":
                        break
                    if h_res == "continue":
                        continue

                    ctx = {
                        "hb": {},
                        "account_state": self.state.account_state,
                        "elapsed_account_sec": now - (self.state.account_started_at or now),
                        "elapsed_session_sec": now - (self.state.session_started_at or now),
                        "consecutive_fails": fails,
                        "notified_rules": self.notified_rules,
                    }
                    outcome = self._eval_and_apply_rules(ctx)
                    if outcome == "break":
                        break
                    if outcome == "continue":
                        continue

                    backoff = min(60.0, 5.0 * fails)
                    if not self.dry_run:
                        self.stop_event.wait(backoff)
                    self._launch(why)

        if self.state.status not in ("STOPPED", "ERROR", "RETIRED"):
            self._set_state(status="STOPPED")

    def _try_refresh_from_app(self):
        """Re-extract cookie from app DB if user re-logged in or app refreshed token."""
        if self.dry_run:
            return False
        app_c = read_app_cookie(self.state.package)
        if not app_c or app_c == self.cookie:
            return False
        in_use = {
            v.get("cookie")
            for k, v in self.store.all_assigned().items()
            if k != self.state.name and isinstance(v, dict)
        }
        if app_c in in_use:
            return False
        st, uid, uname = whoami(app_c)
        if st != "ok":
            return False
        now = time.time()
        self.cookie = app_c
        self.store.set_assign(
            self.state.name,
            {"cookie": app_c, "user_id": uid, "username": uname, "started_at": now},
        )
        self._set_state(
            user_id=uid,
            username=uname,
            cookie_short=short(app_c),
            consecutive_fails=0,
            account_state="ok",
            account_started_at=now,
            last_event="cookie_refreshed",
        )
        self.log(self.state.name, f"Auto re-extracted fresh cookie from app for {uname} ({uid})")
        return True


class Manager:
    def __init__(self, config, store, logger, ipc_sock_path=None, enable_ipc=False, change_config_path=None):
        self.config = config
        self.store = store
        self.logger = logger
        self.change_config_path = change_config_path or CHANGE_CONFIG
        self.launch_lock = threading.Lock()
        self._group_restart_lock = threading.Lock()
        self._last_group_restart = 0.0
        self.last_launch_ts = 0.0
        self.stop_event = threading.Event()
        self.workers = {}
        self._maint_thread = None
        self.dry_run = False
        self.enable_ipc = bool(enable_ipc or ipc_sock_path)
        self.ipc_sock_path = ipc_sock_path or DEFAULT_IPC_SOCK
        self._ipc = None

    def start(self, instance_names=None, dry_run=False, profile_override=None):
        self.stop_event.clear()
        self.dry_run = bool(dry_run)
        self.store.ensure_files()
        instances = self.config.get("instances") or []
        selected = []
        for inst in instances:
            if instance_names:
                if inst.get("name") in instance_names:
                    selected.append(inst)
            elif inst.get("enabled", True):
                selected.append(inst)

        if not selected:
            raise ValueError("No enabled instances to run.")

        if self.config.get("stay_awake") and not dry_run:
            set_stay_awake(True)

        self.workers = {}
        for inst in selected:
            w = InstanceWorker(inst, self, dry_run=dry_run, profile_override=profile_override)
            self.workers[inst["name"]] = w
            w.start()

        if self.enable_ipc:
            handlers = {
                "ping": lambda _a: {"alive": True, "pid": os.getpid(), "workers": len(self.workers)},
                "status": lambda _a: {
                    "instances": self.snapshot(),
                    "counts": self.store.counts(),
                    "switched": self.store.switched_counts(),
                    "system": get_system_stats(),
                    "logs": self._recent_logs(20),
                },
                "reload_config": lambda _a: self.reload_config(),
                "rotate": lambda a: {"rotated": self.force_rotate(a.get("instance", ""), a.get("reason", "ipc"))},
                "sort_tabs": lambda a: {
                    "resized": sort_roblox_tabs(
                        [w.state.package for w in self.workers.values()],
                        tiny=bool(a.get("tiny", self.config.get("tiny_sort_tabs", False))),
                    )
                },
                "stop": lambda _a: (self.stop_event.set() or {"stopping": True}),
            }
            self._ipc = IPCServer(self.ipc_sock_path, handlers=handlers)
            self._ipc.start()

        self._maint_thread = threading.Thread(
            target=self._maintenance_loop,
            args=(dry_run,),
            daemon=True,
            name="mgr-maintenance",
        )
        self._maint_thread.start()

    def _recent_logs(self, n=20):
        with self.logger.lock:
            return list(self.logger.lines)[-n:]

    def reload_config(self):
        """Hot-reload config.json & profiles onto running workers."""
        new_cfg = load_config()
        self.config = new_cfg
        by_name = {i.get("name"): i for i in new_cfg.get("instances") or [] if i.get("name")}
        updated = 0
        for name, w in self.workers.items():
            icfg = by_name.get(name)
            prof_name = (icfg or {}).get("profile") or w.profile.get("name") or "default"
            try:
                prof = load_profile(prof_name)
            except FileNotFoundError:
                prof = None
            w.apply_runtime_update(inst_cfg=icfg, profile_obj=prof)
            updated += 1
        self.logger("SYS", f"Hot-reloaded configuration for {updated} worker(s)")
        return {"reloaded": updated}

    def trigger_group_restart(self, source=""):
        """Stop all active packages when kill_all_on_crash triggers (debounced 10s)."""
        if self.dry_run:
            return
        with self._group_restart_lock:
            now = time.time()
            if (now - self._last_group_restart) < 10.0:
                return
            self._last_group_restart = now
            self.logger("SYS", f"Kill-all-on-crash triggered by {source or 'worker'}")
            for w in self.workers.values():
                force_stop(w.state.package)

    def _maintenance_loop(self, dry_run):
        last_wh = time.time()
        last_clean = time.time()
        last_sort = 0.0
        last_proc = 0.0
        last_block = 0.0
        while not self.stop_event.wait(5.0):
            now = time.time()

            # Batched /proc PID + VmRSS telemetry across all worker packages (1 su call)
            if not dry_run and (now - last_proc) >= 5.0:
                last_proc = now
                pkgs = [w.state.package for w in self.workers.values() if w.state.package]
                usage = get_batch_pkg_usage(pkgs)
                for w in self.workers.values():
                    u = usage.get(w.state.package)
                    if u:
                        w._set_state(pid=u.get("pid"), ram_mb=u.get("mem_mb"))

            # Cross-block active accounts so clones split onto distinct public servers
            if not dry_run and self.config.get("auto_block_accounts") and (now - last_block) >= 20.0:
                last_block = now
                active = [
                    (str(w.state.user_id), w.cookie)
                    for w in self.workers.values()
                    if w.state.user_id and w.cookie and w.state.status in ("RUNNING", "LAUNCHING")
                ]
                if len(active) >= 2:
                    blocked = load_json(BLOCK_CACHE, {})
                    if not isinstance(blocked, dict):
                        blocked = {}
                    changed = False
                    for ua, cka in active:
                        blocked.setdefault(ua, [])
                        for ub, _ in active:
                            if ua == ub or ub in blocked[ua]:
                                continue
                            if block_roblox_user(cka, ub):
                                blocked[ua].append(ub)
                                changed = True
                    if changed:
                        save_json(BLOCK_CACHE, blocked)

            wh_min = float(self.config.get("webhook_interval_minutes") or 0)
            wh_url = (self.config.get("webhook") or "").strip()
            if wh_min > 0 and wh_url and (now - last_wh) >= wh_min * 60.0:
                last_wh = now
                self.send_status_embed(dry_run=dry_run)

            clean_min = float(self.config.get("auto_clean_ram_minutes") or 0)
            if clean_min > 0 and not dry_run and (now - last_clean) >= clean_min * 60.0:
                last_clean = now
                su("am kill-all", 15)
                self.logger("SYS", "Auto-cleaned background RAM (am kill-all)")

            if not dry_run and (self.config.get("sort_tabs") or self.config.get("tiny_sort_tabs")):
                sort_sec = max(10.0, float(self.config.get("sort_tabs_interval_seconds") or 20))
                if (now - last_sort) >= sort_sec:
                    last_sort = now
                    pkgs = [w.state.package for w in self.workers.values()]
                    sort_roblox_tabs(pkgs, tiny=bool(self.config.get("tiny_sort_tabs", False)))

    def send_status_embed(self, dry_run=False):
        wh_url = (self.config.get("webhook") or "").strip()
        if not wh_url:
            return False
        st = get_system_stats()
        cnt = self.store.counts()
        sw = self.store.switched_counts()
        dev = (self.config.get("device_name") or "").strip()
        title = f"Rejoin Tool Status Report ({dev})" if dev else "Rejoin Tool Status Report"
        sw_summary = " | ".join(f"{k}:{v}" for k, v in sw.items() if v > 0)
        fields = [
            {
                "name": "System",
                "value": (
                    f"CPU {st['cpu_pct']:.0f}% • RAM {st['ram_used_mb']:.0f}/{st['ram_total_mb']:.0f}MB ({st['ram_pct']:.0f}%) "
                    f"• Disk Free {st['disk_free_gb']}GB • Load {st['load_1m']}"
                ),
                "inline": False,
            },
            {
                "name": "Cookies",
                "value": f"Queue: {cnt['queue']} | Done: {cnt['done']} | Dead: {cnt['dead']}" + (f"\nSwitched: {sw_summary}" if sw_summary else ""),
                "inline": False,
            },
        ]
        for row in self.snapshot():
            ram_s = f" | {row['ram_mb']:.0f}MB" if row.get("ram_mb") is not None else ""
            fields.append({
                "name": f"{row['name']} ({row['status']})",
                "value": f"Acc: `{row['username']}` | Rejoin: {row['rejoin_count']} | Rot: {row['rotate_count']}{ram_s}",
                "inline": True,
            })

        img_path = None
        if self.config.get("webhook_screenshot") and not dry_run:
            cand = os.path.join(TMP, "status_screen.png")
            if capture_screenshot(cand):
                img_path = cand

        try:
            return send_discord_embed(
                wh_url,
                title,
                f"Active instances: {len(self.workers)}",
                color=0x5865F2,
                fields=fields,
                image_path=img_path,
            )
        finally:
            if img_path and os.path.exists(img_path):
                try:
                    os.remove(img_path)
                except OSError:
                    pass

    def stop(self, timeout=10):
        self.stop_event.set()
        if self._ipc:
            self._ipc.stop()
            self._ipc = None
        for w in self.workers.values():
            w.join(timeout=timeout)
        if self._maint_thread and self._maint_thread.is_alive():
            self._maint_thread.join(timeout=3)

    def snapshot(self):
        return [w.snapshot() for w in self.workers.values()]

    def force_rotate(self, inst_name, reason="manual"):
        w = self.workers.get(inst_name)
        if w:
            w.request_rotate(reason)
            return True
        return False
