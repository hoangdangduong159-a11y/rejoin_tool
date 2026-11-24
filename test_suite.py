"""Automated verification tests for rejoin_tool v4.0 Ultra (safe: never mutates live Roblox cookie DB or launches apps)."""
import json
import os
import sqlite3
import tempfile
import time
import urllib.error
import urllib.request

import engine
from ipc import ipc_request
import log_watch
import lua_deploy
import main
import rjcore
import rules
import shared_hub
import sysutils
import web_panel

TMP_BASE = "/data/data/com.termux/files/usr/tmp"


def test_target_parsing():
    assert rjcore.parse_target("2753915549") == {"place_id": "2753915549", "link_code": None, "share_code": None}
    p1 = rjcore.parse_target("https://www.roblox.com/games/123456?privateServerLinkCode=abc_123")
    assert p1 == {"place_id": "123456", "link_code": "abc_123", "share_code": None}
    p2 = rjcore.parse_target("https://www.roblox.com/share?code=xyz999&type=Server")
    assert p2 == {"place_id": None, "link_code": None, "share_code": "xyz999"}
    assert rjcore.build_uri("123456", "abc_123") == "roblox://placeId=123456&linkCode=abc_123"
    assert rjcore.build_uri("123456", job_id="job1") == "roblox://placeId=123456&gameInstanceId=job1"


def test_rules_engine():
    prof = {
        "rotate_on": ["status:done", "stat:level>=100", "elapsed_account:2h"],
        "rules": [
            "kicked -> rejoin",
            "stat:currency>=1000000 -> notify",
            {"when": {"all": [{"stat": {"rebirths": ">=5"}}, {"elapsed_session": "10m"}]}, "action": "stop", "reason": "all_done"},
        ],
        "max_rejoins_before_rotate": 4,
    }
    compiled = rules.compile_rules(prof)

    ctx = {"hb": {"status": "running", "stats": {"level": 10, "currency": 1500000}}, "elapsed_account_sec": 60, "elapsed_session_sec": 60, "consecutive_fails": 0}
    act, _ = rules.evaluate_rules(compiled, ctx)
    assert act == "notify"
    act2, _ = rules.evaluate_rules(compiled, ctx)
    assert act2 is None

    ctx["hb"] = {"status": "kicked", "kicked": True, "kickMsg": "Idle 20m", "stats": {}}
    act, reason = rules.evaluate_rules(compiled, ctx)
    assert act == "rejoin" and "Idle 20m" in reason

    ctx["hb"] = {"status": "done", "kicked": True, "stats": {"rebirths": 6, "level": 120}}
    ctx["elapsed_session_sec"] = 700
    act, reason = rules.evaluate_rules(compiled, ctx)
    assert act == "stop" and reason == "all_done"

    ctx["hb"] = {"status": "running", "stats": {"level": 100}}
    ctx["elapsed_session_sec"] = 60
    act, _ = rules.evaluate_rules(compiled, ctx)
    assert act == "rotate"

    ctx["hb"] = {}
    ctx["consecutive_fails"] = 4
    act, _ = rules.evaluate_rules(compiled, ctx)
    assert act == "rotate"


def test_sqlite_cookie_patch():
    with tempfile.TemporaryDirectory(dir=TMP_BASE) as td:
        db = os.path.join(td, "Cookies")
        con = sqlite3.connect(db)
        con.execute("""
            CREATE TABLE cookies (
                creation_utc INTEGER NOT NULL,
                top_frame_site_key TEXT NOT NULL,
                host_key TEXT NOT NULL,
                name TEXT NOT NULL,
                value TEXT NOT NULL,
                encrypted_value BLOB DEFAULT '',
                path TEXT NOT NULL,
                expires_utc INTEGER NOT NULL,
                is_secure INTEGER NOT NULL,
                is_httponly INTEGER NOT NULL,
                last_access_utc INTEGER NOT NULL,
                has_expires INTEGER NOT NULL DEFAULT 1,
                is_persistent INTEGER NOT NULL DEFAULT 1,
                priority INTEGER NOT NULL DEFAULT 1,
                samesite INTEGER NOT NULL DEFAULT -1,
                source_scheme INTEGER NOT NULL DEFAULT 0,
                source_port INTEGER NOT NULL DEFAULT -1,
                is_same_party INTEGER NOT NULL DEFAULT 0
            )
        """)
        con.commit()
        con.close()

        c1 = "_|WARNING:-DO-NOT-SHARE-THIS.|_TEST_COOKIE_ONE_" + ("A" * 180)
        rjcore.patch_db_cookie(db, c1)
        assert rjcore.read_db_cookie(db) == c1

        c2 = "_|WARNING:-DO-NOT-SHARE-THIS.|_TEST_COOKIE_TWO_" + ("B" * 180)
        rjcore.patch_db_cookie(db, c2)
        assert rjcore.read_db_cookie(db) == c2


def test_fresh_chromium_v14_cookie_db():
    with tempfile.TemporaryDirectory(dir=TMP_BASE) as td:
        db = os.path.join(td, "FreshCookies")
        c1 = "_|WARNING:-DO-NOT-SHARE-THIS.|_FRESH_CLONE_" + ("F" * 180)
        rjcore.patch_db_cookie(db, c1)
        assert rjcore.read_db_cookie(db) == c1
        con = sqlite3.connect(db)
        try:
            ver = con.execute("SELECT value FROM meta WHERE key='version'").fetchone()[0]
            assert ver == "14"
        finally:
            con.close()


def test_store_and_masking():
    with tempfile.TemporaryDirectory(dir=TMP_BASE) as td:
        st = rjcore.Store(base_dir=td)
        st.ensure_files()
        c1 = "_|WARNING:-DO-NOT-SHARE-THIS.|_COOKIE_1_" + ("X" * 160)
        c2 = "_|WARNING:-DO-NOT-SHARE-THIS.|_COOKIE_2_" + ("Y" * 160)
        assert st.add_cookies(f"{c1}\n{c1}\n{c2}") == 2
        assert st.counts()["queue"] == 2
        assert st.pop() == c1
        st.push_front(c1)
        assert st.pop() == c1
        st.add_done("user1", c1, "status:done")
        st.add_dead("user2", c2, "expired")
        assert st.counts() == {"queue": 1, "done": 1, "dead": 1}

        masked = rjcore.mask(f"Injected {c1}")
        assert c1 not in masked and "<cookie>" in masked


def test_user_pass_cookie_and_switched_archive():
    with tempfile.TemporaryDirectory(dir=TMP_BASE) as td:
        st = rjcore.Store(base_dir=td)
        st.ensure_files()
        ck = "_|WARNING:-DO-NOT-SHARE-THIS.|_UPASS_" + ("Z" * 160)
        line = f"alice_acc:s3cr3t:p@ss:{ck}"
        parsed = rjcore.parse_cookie_line(line)
        assert parsed["username"] == "alice_acc"
        assert parsed["password"] == "s3cr3t:p@ss"
        assert parsed["cookie"] == ck

        assert st.add_cookies(line) == 1
        assert st.get_password("ALICE_ACC") == "s3cr3t:p@ss"
        st.add_switched("banned", "alice_acc", ck, "moderation_banned")
        sw = st.switched_counts()
        assert sw["banned"] == 1
        with open(os.path.join(st.switched_dir, "banned.txt")) as f:
            row = f.read().strip()
        assert "alice_acc|s3cr3t:p@ss|" in row


def test_dry_run_engine():
    with tempfile.TemporaryDirectory(dir=TMP_BASE) as td:
        st = rjcore.Store(base_dir=td)
        st.ensure_files()
        c1 = "_|WARNING:-DO-NOT-SHARE-THIS.|_DRY1_" + ("A" * 160)
        c2 = "_|WARNING:-DO-NOT-SHARE-THIS.|_DRY2_" + ("B" * 160)
        st.add_cookies(f"{c1}\n{c2}")

        ws = os.path.join(td, "ws")
        os.makedirs(os.path.join(ws, "rejoin"), exist_ok=True)
        cfg = {
            "webhook": "",
            "stagger_launch_seconds": 0,
            "instances": [{
                "name": "dry1",
                "package": "free.nokaA",
                "profile": "default",
                "workspace": ws,
                "autoexec": td,
                "use_cookie_queue": True,
                "enabled": True,
            }],
        }
        prof_override = {
            "name": "dry_prof",
            "place_id": 2753915549,
            "private_link": "",
            "job_id": "",
            "rejoin_after_minutes": 0.02,
            "heartbeat_timeout": 30,
            "launch_grace": 30,
            "poll_interval": 0.2,
            "max_rejoins_before_rotate": 0,
            "check_method": "executor",
            "check_presence": False,
            "rotate_on": ["status:done"],
            "rules": [],
            "when_no_cookies": "stop",
            "lua": "",
            "launch_extra": "",
        }

        logger = rjcore.Logger(echo=False)
        mgr = engine.Manager(cfg, st, logger)
        mgr.start(dry_run=True, profile_override=prof_override)
        time.sleep(0.4)

        hb_file = os.path.join(ws, "rejoin", "hb_1000.json")
        with open(hb_file, "w") as f:
            json.dump({"time": time.time(), "placeId": 2753915549, "userId": 1000, "username": "dry_run_user", "status": "running", "stats": {}}, f)

        deadline = time.time() + 5.0
        rejoined = False
        while time.time() < deadline:
            snap = mgr.snapshot()[0]
            if snap["rejoin_count"] >= 1:
                rejoined = True
                break
            time.sleep(0.2)
        assert rejoined, "Mandatory rejoin timer did not trigger"

        with open(hb_file, "w") as f:
            json.dump({"time": time.time(), "placeId": 2753915549, "userId": 1000, "username": "dry_run_user", "status": "done", "reason": "quest_complete", "stats": {}}, f)

        deadline = time.time() + 5.0
        rotated = False
        while time.time() < deadline:
            snap = mgr.snapshot()[0]
            if snap["rotate_count"] >= 1:
                rotated = True
                break
            time.sleep(0.2)
        mgr.stop(timeout=3)
        assert rotated, "status:done rule did not trigger rotation"
        assert st.counts()["done"] == 1


def test_readonly_device_checks():
    assert rjcore.has_root(), "Root not available"
    pkgs = rjcore.detect_packages()
    assert len(pkgs) >= 1, f"Expected at least 1 Roblox/clone package, got {pkgs}"
    ck = rjcore.read_app_cookie(pkgs[0])
    if ck:
        print(f"  [device] detected {pkgs}, read_app_cookie({pkgs[0]}) OK: {rjcore.short(ck)}")
    else:
        print(f"  [device] detected {pkgs} (no logged-in cookie on {pkgs[0]} yet)")


def test_auto_extract():
    pkgs = rjcore.detect_packages()
    if pkgs:
        live_recs = rjcore.extract_all_cookies([pkgs[0]])
        assert len(live_recs) == 1 and live_recs[0]["package"] == pkgs[0]

    # Deterministic export & DB extraction check using mock records
    c1 = "_|WARNING:-DO-NOT-SHARE-THIS.|_EXT1_" + ("E" * 160)
    records = [{
        "package": "vip.orsshjbd",
        "cookie": c1,
        "status": "ok",
        "user_id": 123456,
        "username": "mock_user",
    }]
    with tempfile.TemporaryDirectory(dir=TMP_BASE) as td:
        st = rjcore.Store(base_dir=td)
        out_path = os.path.join(td, "extracted.txt")
        n = st.export_extracted(records, out_path)
        assert n == 1
        assert (os.stat(out_path).st_mode & 0o777) == 0o600
        with open(out_path) as f:
            parts = f.read().strip().split("|", 3)
        assert parts == ["mock_user", "123456", "vip.orsshjbd", c1]

        uc_path = os.path.join(td, "uc.txt")
        assert st.export_extracted(records, uc_path, fmt="user_cookie") == 1
        with open(uc_path) as f:
            assert f.read().strip() == f"mock_user|{c1}"

        raw_path = os.path.join(td, "raw.txt")
        assert st.export_extracted(records, raw_path, fmt="raw") == 1
        with open(raw_path) as f:
            assert f.read().strip() == c1


def test_sysutils():
    st = sysutils.get_system_stats()
    assert st["ram_total_mb"] > 0 and st["disk_total_gb"] > 0 and "cpu_pct" in st
    sample_xml = '<roblox><int name="FramerateCap">-1</int><int name="GraphicsQualityLevel">0</int><token name="SavedQualityLevel">0</token></roblox>'
    patched = sysutils._patch_graphics_xml(sample_xml, fps_cap=15, low_quality=True)
    assert '<int name="FramerateCap">15</int>' in patched
    assert '<int name="GraphicsQualityLevel">1</int>' in patched
    assert '<token name="SavedQualityLevel">1</token>' in patched

    with tempfile.TemporaryDirectory(dir=TMP_BASE) as td:
        lua_deploy.deploy_heartbeat(td)
        sysutils.add_autoexec_script(td, "farm", "print('hi')")
        scripts = sysutils.list_autoexec_scripts(td)
        names = {s["name"]: s for s in scripts}
        assert names[sysutils.PROTECTED_SCRIPT]["protected"] is True
        assert names["farm.lua"]["enabled"] is True

        try:
            sysutils.toggle_autoexec_script(td, sysutils.PROTECTED_SCRIPT)
            assert False, "Expected ValueError"
        except ValueError:
            pass

        sysutils.toggle_autoexec_script(td, "farm.lua")
        assert any(s["name"] == "farm.lua.disabled" and not s["enabled"] for s in sysutils.list_autoexec_scripts(td))
        assert sysutils.delete_autoexec_script(td, "farm.lua.disabled") is True

        png = os.path.join(td, "screen.png")
        assert sysutils.capture_screenshot(png), "screencap failed"
        assert os.path.getsize(png) > 1024


def test_window_grid_calculator():
    rects = sysutils.calculate_grid_positions(4, 1080, 1920, tiny=False, top_inset=48)
    assert len(rects) == 4
    for l, t, r, b in rects:
        assert 0 <= l < r <= 1080 and 48 <= t < b <= 1920
    tiny_rects = sysutils.calculate_grid_positions(4, 1080, 1920, tiny=True, top_inset=48)
    assert len(tiny_rects) == 4
    for l, t, r, b in tiny_rects:
        assert (r - l) <= 280 and (b - t) <= 280


def test_discord_sanitizer():
    ck = "_|WARNING:-DO-NOT-SHARE-THIS.|_SECRET_" + ("Q" * 160)
    raw = {
        "title": f"Leak {ck} " + ("T" * 300),
        "description": "D" * 4500,
        "fields": [{"name": f"N{i}", "value": f"{ck} " + ("V" * 1200)} for i in range(30)],
        "footer": {"text": f"Footer {ck}"},
    }
    clean = sysutils.sanitize_discord_embed(raw)
    assert ck not in json.dumps(clean)
    assert len(clean["title"]) <= 250
    assert len(clean.get("fields", [])) <= 25
    total_len = len(clean.get("title", "")) + len(clean.get("description", "")) + len(clean.get("footer", {}).get("text", ""))
    for f in clean.get("fields", []):
        total_len += len(f["name"]) + len(f["value"])
    assert total_len <= 5800


def test_multi_executor_scanner_and_lua():
    with tempfile.TemporaryDirectory(dir=TMP_BASE) as td:
        ae1 = os.path.join(td, "Delta", "Autoexecute")
        ws1 = os.path.join(td, "Delta", "Workspace")
        ae2 = os.path.join(td, "Codex", "Autoexec")
        os.makedirs(ae1)
        os.makedirs(ws1)
        os.makedirs(ae2)
        found = sysutils.scan_executor_dirs(sdcard_root=td)
        names = {e["name"] for e in found}
        assert "Delta" in names and "Codex" in names
        deployed = lua_deploy.deploy_all_executors(sdcard_root=td)
        assert len(deployed) == 2
        for p in deployed:
            assert os.path.isfile(p)


def test_ipc_server_and_online_mode():
    with tempfile.TemporaryDirectory(dir=TMP_BASE) as td:
        sock_path = os.path.join(td, "t.sock")
        st = rjcore.Store(base_dir=td)
        st.ensure_files()
        c1 = "_|WARNING:-DO-NOT-SHARE-THIS.|_IPC1_" + ("C" * 160)
        c2 = "_|WARNING:-DO-NOT-SHARE-THIS.|_IPC2_" + ("D" * 160)
        st.add_cookies(f"{c1}\n{c2}")

        cfg = {
            "webhook": "",
            "stagger_launch_seconds": 0,
            "instances": [{
                "name": "on1",
                "package": "free.nokaA",
                "profile": "default",
                "workspace": td,
                "autoexec": td,
                "use_cookie_queue": True,
                "enabled": True,
            }],
        }
        prof_override = {
            "name": "online_prof",
            "place_id": 2753915549,
            "private_link": "",
            "job_id": "",
            "rejoin_after_minutes": 0,
            "heartbeat_timeout": 30,
            "launch_grace": 0.2,
            "poll_interval": 0.2,
            "presence_poll_seconds": 0.2,
            "check_method": "online",
            "rotate_on": [],
            "rules": [],
            "when_no_cookies": "stop",
            "lua": "",
            "launch_extra": "",
        }
        logger = rjcore.Logger(echo=False)
        mgr = engine.Manager(cfg, st, logger, ipc_sock_path=sock_path, enable_ipc=True)
        mgr.start(dry_run=True, profile_override=prof_override)
        try:
            time.sleep(0.6)
            p = ipc_request("ping", sock_path=sock_path)
            assert p.get("ok") and p["result"]["alive"] is True

            s = ipc_request("status", sock_path=sock_path)
            assert s.get("ok") and s["result"]["instances"][0]["check_method"] == "online"

            r = ipc_request("rotate", {"instance": "on1", "reason": "ipc_test"}, sock_path=sock_path)
            assert r.get("ok") and r["result"]["rotated"] is True
            time.sleep(0.4)
            assert mgr.snapshot()[0]["rotate_count"] >= 1

            rel = ipc_request("reload_config", sock_path=sock_path)
            assert rel.get("ok")

            stp = ipc_request("stop", sock_path=sock_path)
            assert stp.get("ok")
        finally:
            mgr.stop(timeout=3)


def test_change_acc_cus_workspace_trigger():
    # Parser unit assertions
    p1 = rjcore.parse_custom_change_trigger('{"Type": "CDK"}')
    assert p1["matched"] is True and p1["switch_type"] == "CDK"
    p2 = rjcore.parse_custom_change_trigger("Completed-Godhuman")
    assert p2["matched"] is True and p2["switch_type"] == "Godhuman"
    p3 = rjcore.parse_custom_change_trigger("Completed")
    assert p3["matched"] is True and p3["switch_type"] == ""
    p4 = rjcore.parse_custom_change_trigger("Yummy")
    assert p4["matched"] is True and p4["switch_type"] == ""
    assert rjcore.parse_custom_change_trigger("hello world")["matched"] is False
    assert rjcore.parse_custom_change_trigger('{"fps": 60}')["matched"] is False

    # End-to-end Manager dry_run test in ONLINE mode (no heartbeat needed!)
    with tempfile.TemporaryDirectory(dir=TMP_BASE) as td:
        st = rjcore.Store(base_dir=td)
        st.ensure_files()
        c1 = "_|WARNING:-DO-NOT-SHARE-THIS.|_CUS1_" + ("E" * 160)
        c2 = "_|WARNING:-DO-NOT-SHARE-THIS.|_CUS2_" + ("F" * 160)
        c3 = "_|WARNING:-DO-NOT-SHARE-THIS.|_CUS3_" + ("J" * 160)
        st.add_cookies(f"{c1}\n{c2}\n{c3}")
        ws = os.path.join(td, "ws")
        os.makedirs(ws, exist_ok=True)
        cfg = {
            "webhook": "",
            "stagger_launch_seconds": 0,
            "change_acc_cus": True,
            "instances": [{
                "name": "cus1",
                "package": "free.nokaA",
                "profile": "default",
                "workspace": ws,
                "autoexec": td,
                "use_cookie_queue": True,
                "enabled": True,
            }],
        }
        prof_override = {
            "name": "cus_online",
            "place_id": 2753915549,
            "private_link": "",
            "job_id": "",
            "rejoin_after_minutes": 0,
            "heartbeat_timeout": 30,
            "launch_grace": 30,
            "poll_interval": 0.2,
            "presence_poll_seconds": 30,
            "check_method": "online",
            "custom_change_watch": True,
            "rotate_on": ["status:done"],
            "rules": [],
            "when_no_cookies": "stop",
            "lua": "",
            "launch_extra": "",
        }
        mgr = engine.Manager(cfg, st, rjcore.Logger(echo=False))
        mgr.start(dry_run=True, profile_override=prof_override)
        try:
            time.sleep(0.4)
            trig = os.path.join(ws, "dry_run_user.txt")
            with open(trig, "w") as f:
                f.write('{"Type": "CDK"}')

            deadline = time.time() + 5.0
            rotated = False
            while time.time() < deadline:
                if mgr.snapshot()[0]["rotate_count"] >= 1:
                    rotated = True
                    break
                time.sleep(0.15)
            assert rotated, "Workspace change_acc_cus trigger did not rotate account"
            assert not os.path.exists(trig), "Consumed Workspace trigger file was not removed"
            sw_counts = st.switched_counts()
            assert sw_counts.get("Completed-CDK") == 1, f"Expected Completed-CDK=1, got {sw_counts}"
            comp_file = os.path.join(st.switched_dir, "Completed-CDK.txt")
            with open(comp_file) as f:
                assert "dry_run_user" in f.read()

            # Second trigger using Completed-<UserId>.txt
            trig2 = os.path.join(ws, "Completed-1000.txt")
            with open(trig2, "w") as f:
                f.write("Completed-Godhuman")

            deadline2 = time.time() + 5.0
            rotated2 = False
            while time.time() < deadline2:
                if mgr.snapshot()[0]["rotate_count"] >= 2:
                    rotated2 = True
                    break
                time.sleep(0.15)
            assert rotated2, "Completed-<UserId>.txt trigger did not rotate account"
            assert not os.path.exists(trig2), "Consumed Completed-1000.txt file was not removed"
            assert st.switched_counts().get("Completed-Godhuman") == 1
        finally:
            mgr.stop(timeout=3)


def test_blox_fruits_change_config():
    with tempfile.TemporaryDirectory(dir=TMP_BASE) as td:
        cc_path = os.path.join(td, "config-change.json")
        def_conf = rjcore.save_change_config(rjcore.DEFAULT_CHANGE_CONFIG, path=cc_path)
        assert rjcore.info_matches_change_config({"level": 2600, "god_human": True}, def_conf) == (False, [])

        custom_conf = rjcore.save_change_config({
            **rjcore.DEFAULT_CHANGE_CONFIG,
            "level": 2550,
            "god_human": True,
            "cursed_dual_katana": True,
        }, path=cc_path)
        assert rjcore.info_matches_change_config({"level": 2500, "god_human": True, "cdk": True}, custom_conf)[0] is False
        ok, reasons = rjcore.info_matches_change_config({"level": 2600, "god_human": True, "cdk": True}, custom_conf)
        assert ok is True and len(reasons) == 3

        st = rjcore.Store(base_dir=td)
        st.ensure_files()
        c1 = "_|WARNING:-DO-NOT-SHARE-THIS.|_BF1_" + ("G" * 160)
        c2 = "_|WARNING:-DO-NOT-SHARE-THIS.|_BF2_" + ("H" * 160)
        st.add_cookies(f"{c1}\n{c2}")
        ws = os.path.join(td, "ws")
        os.makedirs(ws, exist_ok=True)
        cfg = {
            "webhook": "",
            "stagger_launch_seconds": 0,
            "change_acc_cus": False,
            "change_acc_bf": True,
            "instances": [{
                "name": "bf1",
                "package": "free.nokaA",
                "profile": "default",
                "workspace": ws,
                "autoexec": td,
                "use_cookie_queue": True,
                "enabled": True,
            }],
        }
        prof_override = {
            "name": "bf_prof",
            "place_id": 2753915549,
            "private_link": "",
            "job_id": "",
            "rejoin_after_minutes": 0,
            "heartbeat_timeout": 30,
            "launch_grace": 30,
            "poll_interval": 0.2,
            "check_method": "executor",
            "rotate_on": ["status:done"],
            "rules": [],
            "when_no_cookies": "stop",
            "lua": "",
            "launch_extra": "",
        }
        mgr = engine.Manager(cfg, st, rjcore.Logger(echo=False), change_config_path=cc_path)
        mgr.start(dry_run=True, profile_override=prof_override)
        try:
            time.sleep(0.4)
            info_path = os.path.join(ws, "1000-info.json")
            with open(info_path, "w") as f:
                json.dump({"level": 2600, "god_human": True, "cursed_dual_katana": True, "fruit": "Kitsune"}, f)

            deadline = time.time() + 5.0
            rotated = False
            while time.time() < deadline:
                if mgr.snapshot()[0]["rotate_count"] >= 1:
                    rotated = True
                    break
                time.sleep(0.15)
            assert rotated, "Blox Fruit <uid>-info.json did not trigger rotation"
            assert not os.path.exists(info_path), "Consumed 1000-info.json was not removed"
            assert st.switched_counts()["acc_ok"] == 1
        finally:
            mgr.stop(timeout=3)


def test_hybrid_pending_cookie_auto_recovery():
    with tempfile.TemporaryDirectory(dir=TMP_BASE) as td:
        st = rjcore.Store(base_dir=td)
        st.ensure_files()
        c1 = "_|WARNING:-DO-NOT-SHARE-THIS.|_HYB1_" + ("K" * 160)
        c2 = "_|WARNING:-DO-NOT-SHARE-THIS.|_HYB2_" + ("L" * 160)
        st.add_cookies(c1)

        ws = os.path.join(td, "ws")
        os.makedirs(ws, exist_ok=True)
        cfg = {
            "webhook": "",
            "stagger_launch_seconds": 0,
            "change_acc_cus": True,
            "instances": [{
                "name": "hyb1",
                "package": "free.nokaA",
                "profile": "default",
                "workspace": ws,
                "autoexec": td,
                "use_cookie_queue": True,
                "enabled": True,
            }],
        }
        prof_override = {
            "name": "hyb_wait",
            "place_id": 2753915549,
            "private_link": "",
            "job_id": "",
            "rejoin_after_minutes": 0,
            "heartbeat_timeout": 30,
            "launch_grace": 30,
            "poll_interval": 0.2,
            "check_method": "online",
            "custom_change_watch": True,
            "rotate_on": ["status:done"],
            "rules": [],
            "when_no_cookies": "wait",
            "lua": "",
            "launch_extra": "",
        }
        mgr = engine.Manager(cfg, st, rjcore.Logger(echo=False))
        mgr.start(dry_run=True, profile_override=prof_override)
        try:
            time.sleep(0.4)
            with open(os.path.join(ws, "dry_run_user.txt"), "w") as f:
                f.write('{"Type": "CDK"}')

            deadline = time.time() + 5.0
            waiting = False
            while time.time() < deadline:
                snap = mgr.snapshot()[0]
                if snap["status"] == "WAITING_COOKIE":
                    waiting = True
                    break
                time.sleep(0.15)
            assert waiting, "Worker did not enter WAITING_COOKIE when queue became empty"
            assert st.switched_counts().get("Completed-CDK") == 1

            # Refill queue and confirm automatic cookie injection & resume!
            st.add_cookies(c2)
            deadline2 = time.time() + 5.0
            resumed = False
            while time.time() < deadline2:
                snap = mgr.snapshot()[0]
                if snap["status"] in ("LAUNCHING", "RUNNING") and snap["rotate_count"] >= 1:
                    resumed = True
                    break
                time.sleep(0.15)
            assert resumed, "Worker did not auto-recover from WAITING_COOKIE after queue refill"
        finally:
            mgr.stop(timeout=3)


def test_hybrid_batch_proc_and_shared_sync():
    st = sysutils.get_system_stats()
    assert st["tool_ram_mb"] > 0
    pkgs = rjcore.detect_packages() or ["vip.orsshjbd"]
    batch = sysutils.get_batch_pkg_usage([pkgs[0]])
    assert pkgs[0] in batch

    with tempfile.TemporaryDirectory(dir=TMP_BASE) as td:
        s_ae = os.path.join(td, "shared", "Autoexec")
        s_ws = os.path.join(td, "shared", "Workspace")
        os.makedirs(s_ae)
        os.makedirs(s_ws)
        with open(os.path.join(s_ae, "custom_helper.lua"), "w") as f:
            f.write("print('shared helper')")
        with open(os.path.join(s_ws, "config.txt"), "w") as f:
            f.write("ok")

        ex1_ae = os.path.join(td, "sd", "Delta", "Autoexecute")
        ex1_ws = os.path.join(td, "sd", "Delta", "Workspace")
        os.makedirs(ex1_ae)
        os.makedirs(ex1_ws)

        deployed = lua_deploy.deploy_all_executors(
            sdcard_root=os.path.join(td, "sd"),
            sync_shared=True,
            shared_autoexec=s_ae,
            shared_workspace=s_ws,
        )
        assert any(p.endswith("custom_helper.lua") for p in deployed)
        assert os.path.isfile(os.path.join(ex1_ae, "custom_helper.lua"))
        assert os.path.isfile(os.path.join(ex1_ws, "config.txt"))


def test_quick_setup_commands():
    import subprocess as sp
    rc = main.root_command("run")
    assert rc.startswith('su -c "export PATH=\\$PATH:/data/data/com.termux/files/usr/bin')
    assert rc.endswith(os.path.join(rjcore.BASE, "rejoin") + ' run"')
    assert "[m]ain.py" in main.stop_command()
    boot = main.build_boot_script("--profile default")
    assert "termux-wake-lock" in boot and "rejoin run --profile default" in boot
    path = os.path.join(TMP_BASE, "rj_boot_test.sh")
    with open(path, "w") as f:
        f.write(boot)
    try:
        assert sp.run(["bash", "-n", path]).returncode == 0
    finally:
        os.remove(path)
    assert sp.run(["bash", "-n", os.path.join(rjcore.BASE, "setup.sh")]).returncode == 0
    assert sp.run(["bash", "-n", os.path.join(rjcore.BASE, "rejoin")]).returncode == 0
    assert main.detect_setup_url().endswith("/setup.sh")


def test_log_watch_parser_and_error_codes():
    # Old error followed by newer Connection accepted -> should report in_game with no error
    log_ok = "\n".join([
        "2026-11-25T01:00:00Z [FLog::Network] Disconnect from server, Error code: 277",
        "2026-11-25T01:01:00Z [FLog::Output] Joining game '11111111-2222-3333-4444-555555555555' place 2753915549",
        "2026-11-25T01:01:05Z [FLog::Network] Connection accepted from 128.116.0.1|50000",
    ])
    p_ok = log_watch.parse_roblox_log_tail(log_ok)
    assert p_ok["state"] == "in_game"
    assert p_ok["error_code"] is None
    assert p_ok["place_id"] == 2753915549
    assert p_ok["job_id"] == "11111111-2222-3333-4444-555555555555"

    for code, sample in (
        (264, "[FLog::Network] Same account launched experience from different device. Error code: 264"),
        (267, "[FLog::Network] You were kicked from this experience (Error Code: 267)"),
        (268, "[FLog::Network] You have been kicked due to unexpected client behavior. (Error Code: 268)"),
        (277, "[FLog::Network] Lost connection to the game server (Error code: 277)"),
        (279, "[FLog::Network] Failed to connect to the Game. (ID = 17: Connection attempt failed.)"),
        (524, "[FLog::Network] Not authorized to join this experience (Error Code: 524)"),
        (529, "[FLog::Network] Roblox HTTP 529 service unavailable"),
    ):
        res = log_watch.parse_roblox_log_tail(sample)
        assert res["state"] == "disconnected", f"Expected disconnected for {code}: {res}"
        assert res["error_code"] == code, f"Expected {code}, got {res}"

    anr_txt = "  Window #2 Window{abc u0 Application Not Responding: vip.orsshjbd}:\n"
    assert log_watch.parse_anr_windows(anr_txt, ["vip.orsshjbd", "vip.ytcjk"]) == {"vip.orsshjbd"}


def test_race_ver_numeric_and_string_matching():
    with tempfile.TemporaryDirectory(dir=TMP_BASE) as td:
        cc_path = os.path.join(td, "config-change.json")
        c0 = rjcore.save_change_config({"level": 100, "race_ver": 0}, path=cc_path)
        assert c0["race_ver"] == 0
        assert rjcore.info_matches_change_config({"level": 100}, c0)[0] is True

        c1 = rjcore.save_change_config({"race_ver": 1}, path=cc_path)
        assert c1["race_ver"] == 1
        assert rjcore.info_matches_change_config({"race_ver": "Human V3"}, c1)[0] is True
        assert rjcore.info_matches_change_config({"tier": 2}, c1)[0] is True

        c3 = rjcore.save_change_config({"race_ver": 3}, path=cc_path)
        assert rjcore.info_matches_change_config({"race_ver": "Human V2"}, c3)[0] is False
        assert rjcore.info_matches_change_config({"race_ver": "Cyborg V4"}, c3)[0] is True

        c_str = rjcore.save_change_config({"race_ver": "Ghoul V3"}, path=cc_path)
        assert rjcore.info_matches_change_config({"race_ver": "Ghoul V4"}, c_str)[0] is True
        assert rjcore.info_matches_change_config({"race_ver": "Ghoul V2"}, c_str)[0] is False
        assert rjcore.info_matches_change_config({"race_ver": "Human V4"}, c_str)[0] is False


def test_shared_hub_download_hot_ingest_and_shouko_import():
    with tempfile.TemporaryDirectory(dir=TMP_BASE) as td:
        dl = os.path.join(td, "Download")
        hub = os.path.join(dl, "RejoinTool")
        shk = os.path.join(dl, "Shouko")
        s_ae = os.path.join(td, "shared_ae")
        s_ws = os.path.join(td, "shared_ws")
        os.makedirs(os.path.join(shk, "Autoexec"), exist_ok=True)
        os.makedirs(os.path.join(shk, "Workspace"), exist_ok=True)

        c1 = "_|WARNING:-DO-NOT-SHARE-THIS.|_HUB1_" + ("M" * 160)
        c2 = "_|WARNING:-DO-NOT-SHARE-THIS.|_HUB2_" + ("N" * 160)
        with open(os.path.join(dl, "cookie.txt"), "w") as f:
            f.write(c1 + "\n")
        with open(os.path.join(dl, "config-change.json"), "w") as f:
            json.dump({"race_ver": 1, "level": 2600, "god_human": True}, f)
        with open(os.path.join(shk, "Autoexec", "farm.lua"), "w") as f:
            f.write("print('shouko_lua')")
        with open(os.path.join(shk, "Workspace", "settings.txt"), "w") as f:
            f.write("ok")
        with open(os.path.join(shk, "server_links.txt"), "w") as f:
            f.write("vip.orsshjbd,roblox://placeId=2753915549\n")

        st = rjcore.Store(base_dir=os.path.join(td, "store"))
        st.ensure_files()
        cfg = {"instances": [{"name": "clone1", "package": "vip.orsshjbd", "target": ""}]}
        cc_dst = os.path.join(td, "cc.json")

        summary = shared_hub.import_shouko_environment(
            st,
            cfg,
            shouko_dir=shk,
            download_dir=dl,
            change_config_path=cc_dst,
            shared_autoexec=s_ae,
            shared_workspace=s_ws,
        )
        assert summary["cookies_added"] == 1
        assert summary["change_config_imported"] is True
        assert summary["autoexec_copied"] == 1
        assert summary["workspace_copied"] == 1
        assert summary["links_imported"] == 1
        assert cfg["instances"][0]["target"] == "roblox://placeId=2753915549"
        assert os.path.getsize(os.path.join(dl, "cookie.txt")) == 0
        assert rjcore.load_change_config(cc_dst)["race_ver"] == 1

        # Test hot-folder ingest & output mirror
        assert shared_hub.ensure_hub_dirs(hub) is True
        with open(os.path.join(hub, "cookies_inbox.txt"), "w") as f:
            f.write(c2 + "\n")
        assert shared_hub.ingest_hot_cookies(st, hub_dir=hub, download_dir=dl) == 1
        assert st.counts()["queue"] == 2
        st.add_done("alice", c1, "completed")
        mirrored = shared_hub.mirror_outputs_to_download(st, change_config_path=cc_dst, hub_dir=hub)
        assert mirrored >= 2
        assert os.path.isfile(os.path.join(hub, "switched", "acc_ok.txt"))


def test_web_panel_rest_api_and_auth():
    with tempfile.TemporaryDirectory(dir=TMP_BASE) as td:
        st = rjcore.Store(base_dir=td)
        st.ensure_files()
        c1 = "_|WARNING:-DO-NOT-SHARE-THIS.|_WEB1_" + ("W" * 160)
        c2 = "_|WARNING:-DO-NOT-SHARE-THIS.|_WEB2_" + ("V" * 160)
        st.add_cookies(c1)
        cfg = {
            "webhook": "",
            "stagger_launch_seconds": 0,
            "instances": [{
                "name": "clone1",
                "package": "vip.orsshjbd",
                "profile": "default",
                "workspace": td,
                "autoexec": td,
                "use_cookie_queue": True,
                "enabled": True,
            }],
        }
        prof_override = {
            **rjcore.DEFAULT_PROFILE,
            "name": "web_prof",
            "place_id": 2753915549,
            "check_method": "hybrid",
            "poll_interval": 0.2,
            "when_no_cookies": "wait",
        }
        cc_path = os.path.join(td, "cc.json")
        rjcore.save_change_config(rjcore.DEFAULT_CHANGE_CONFIG, path=cc_path)
        mgr = engine.Manager(cfg, st, rjcore.Logger(echo=False), change_config_path=cc_path)
        mgr.start(dry_run=True, profile_override=prof_override)
        srv = web_panel.WebPanelServer(
            host="127.0.0.1",
            port=0,
            token="secret_tok_42",
            manager=mgr,
            store=st,
            change_config_path=cc_path,
        )
        url = srv.start()
        try:
            # 1. Unauthorized without token
            try:
                urllib.request.urlopen(url + "/api/status", timeout=3)
                assert False, "Expected 401"
            except urllib.error.HTTPError as e:
                assert e.code == 401

            # 2. HTML dashboard loads
            with urllib.request.urlopen(url + "/", timeout=3) as resp:
                assert resp.status == 200
                html = resp.read().decode("utf-8")
                assert "Rejoin Tool v4.0 Ultra" in html

            # 3. Authorized status does not leak raw cookie
            req = urllib.request.Request(url + "/api/status", headers={"Authorization": "Bearer secret_tok_42"})
            with urllib.request.urlopen(req, timeout=3) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                assert data["ok"] is True
                assert c1 not in json.dumps(data)

            # 4. POST /api/cookies & POST /api/change-config & POST /api/action
            def _post(path, payload):
                r = urllib.request.Request(
                    url + path,
                    data=json.dumps(payload).encode("utf-8"),
                    headers={"Authorization": "Bearer secret_tok_42", "Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(r, timeout=3) as resp:
                    return json.loads(resp.read().decode("utf-8"))

            res_ck = _post("/api/cookies", {"text": c2, "prune": False})
            assert res_ck["ok"] is True and res_ck["added"] == 1

            res_bf = _post("/api/change-config", {"level": 2550, "god_human": True, "race_ver": 3})
            assert res_bf["ok"] is True and res_bf["change_config"]["race_ver"] == 3

            res_rj = _post("/api/action", {"action": "rejoin_instance", "instance": "clone1"})
            assert res_rj["ok"] is True
            time.sleep(0.5)
            assert mgr.snapshot()[0]["rejoin_count"] >= 1
        finally:
            srv.stop()
            mgr.stop(timeout=3)


def test_hybrid_mode_log_disconnect_and_ipc_rejoin():
    with tempfile.TemporaryDirectory(dir=TMP_BASE) as td:
        sock_path = os.path.join(td, "hyb.sock")
        st = rjcore.Store(base_dir=td)
        st.ensure_files()
        c1 = "_|WARNING:-DO-NOT-SHARE-THIS.|_HYB_LOG1_" + ("R" * 160)
        st.add_cookies(c1)
        cfg = {
            "webhook": "",
            "stagger_launch_seconds": 0,
            "instances": [{
                "name": "clone1",
                "package": "vip.orsshjbd",
                "profile": "default",
                "workspace": td,
                "autoexec": td,
                "use_cookie_queue": True,
                "enabled": True,
            }],
        }
        prof_override = {
            **rjcore.DEFAULT_PROFILE,
            "name": "hyb_test",
            "place_id": 2753915549,
            "check_method": "hybrid",
            "poll_interval": 0.2,
            "launch_grace": 30,
        }
        mgr = engine.Manager(cfg, st, rjcore.Logger(echo=False), ipc_sock_path=sock_path, enable_ipc=True)
        mgr.start(dry_run=True, profile_override=prof_override)
        try:
            time.sleep(0.5)
            res = ipc_request("rejoin", {"instance": "clone1", "reason": "ipc_rj"}, sock_path=sock_path)
            assert res.get("ok") and res["result"]["rejoined"] is True
            time.sleep(0.5)
            assert mgr.snapshot()[0]["rejoin_count"] >= 1

            # Feed simulated Error 277 disconnect after launch grace (dry_run grace is 1.5s)
            time.sleep(1.7)
            w = mgr.workers["clone1"]
            before_rj = mgr.snapshot()[0]["rejoin_count"]
            w.update_log_state({"state": "disconnected", "error_code": 277, "message": "Lost connection"})
            deadline = time.time() + 4.0
            rejoined = False
            while time.time() < deadline:
                if mgr.snapshot()[0]["rejoin_count"] > before_rj:
                    rejoined = True
                    break
                time.sleep(0.15)
            assert rejoined, "Hybrid worker did not rejoin on simulated log Error 277"
        finally:
            mgr.stop(timeout=3)


def test_appcloner_metadata_and_vip_pkg_detection():
    assert rjcore._ROBLOX_PKG_RE.match("vip.orsshjbd")
    assert rjcore._ROBLOX_PKG_RE.match("vip.ytcjk")
    assert rjcore._ROBLOX_PKG_RE.match("com.vng.roblox.client")
    assert "com.quanghuy.rejoin" in rjcore._EXCLUDED_PACKAGES
    name1 = rjcore.suggest_instance_name("vip.orsshjbd", 1, metadata={"clone_number": 1}, used_names=set())
    assert name1 == "clone1"
    name2 = rjcore.suggest_instance_name("vip.ytcjk", 2, metadata={"clone_number": 1}, used_names={"clone1"})
    assert name2 == "clone1_2"


def test_cookie_prune_parallel():
    with tempfile.TemporaryDirectory(dir=TMP_BASE) as td:
        st = rjcore.Store(base_dir=td)
        st.ensure_files()
        ck_ok = "_|WARNING:-DO-NOT-SHARE-THIS.|_OK_" + ("A" * 160)
        ck_dead = "_|WARNING:-DO-NOT-SHARE-THIS.|_DEAD_" + ("B" * 160)
        ck_cap = "_|WARNING:-DO-NOT-SHARE-THIS.|_CAP_" + ("C" * 160)
        st.add_cookies(f"{ck_ok}\n{ck_dead}\n{ck_cap}")

        def _mock_val(ck):
            if ck == ck_ok:
                return {"state": "ok", "user_id": 1, "username": "ok_user", "reason": ""}
            if ck == ck_dead:
                return {"state": "banned", "user_id": None, "username": "ban_user", "reason": "banned"}
            return {"state": "captcha", "user_id": None, "username": "cap_user", "reason": "captcha"}

        res = st.prune_invalid_cookies(validator=_mock_val)
        assert res["ok"] == 1 and res["dead"] == 1 and res["challenge"] == 1
        assert st.queue() == [ck_ok]
        assert st.switched_counts()["banned"] == 1
        assert st.switched_counts()["captcha"] == 1


if __name__ == "__main__":
    for fn in (
        test_quick_setup_commands,
        test_target_parsing,
        test_rules_engine,
        test_sqlite_cookie_patch,
        test_fresh_chromium_v14_cookie_db,
        test_store_and_masking,
        test_user_pass_cookie_and_switched_archive,
        test_dry_run_engine,
        test_readonly_device_checks,
        test_auto_extract,
        test_sysutils,
        test_window_grid_calculator,
        test_discord_sanitizer,
        test_multi_executor_scanner_and_lua,
        test_ipc_server_and_online_mode,
        test_change_acc_cus_workspace_trigger,
        test_blox_fruits_change_config,
        test_hybrid_pending_cookie_auto_recovery,
        test_hybrid_batch_proc_and_shared_sync,
        test_log_watch_parser_and_error_codes,
        test_race_ver_numeric_and_string_matching,
        test_shared_hub_download_hot_ingest_and_shouko_import,
        test_web_panel_rest_api_and_auth,
        test_hybrid_mode_log_disconnect_and_ipc_rejoin,
        test_appcloner_metadata_and_vip_pkg_detection,
        test_cookie_prune_parallel,
    ):
        fn()
        print(f"[PASS] {fn.__name__}")
    print("ALL TESTS PASSED")
