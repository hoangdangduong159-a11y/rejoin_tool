"""Automated verification tests for rejoin_tool v2.0 (safe: never mutates live Roblox cookie DB or launches apps)."""
import json
import os
import sqlite3
import tempfile
import time

import engine
from ipc import ipc_request
import lua_deploy
import main
import rjcore
import rules
import sysutils

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
    assert "free.nokaA" in pkgs, f"Expected free.nokaA in {pkgs}"
    ck = rjcore.read_app_cookie("free.nokaA")
    assert ck and len(ck) > 100, "Failed to read cookie from free.nokaA"
    print(f"  [device] read_app_cookie(free.nokaA) OK: {rjcore.short(ck)}")


def test_auto_extract():
    records = rjcore.extract_all_cookies(["free.nokaA"])
    assert len(records) == 1
    rec = records[0]
    assert rec["package"] == "free.nokaA"
    assert rec["status"] == "ok", f"Unexpected status: {rec['status']}"
    assert isinstance(rec["user_id"], int) and rec["username"]
    assert rec["cookie"] and len(rec["cookie"]) > 100

    with tempfile.TemporaryDirectory(dir=TMP_BASE) as td:
        st = rjcore.Store(base_dir=td)
        out_path = os.path.join(td, "extracted.txt")
        n = st.export_extracted(records, out_path)
        assert n == 1
        assert (os.stat(out_path).st_mode & 0o777) == 0o600
        with open(out_path) as f:
            parts = f.read().strip().split("|", 3)
        assert len(parts) == 4
        assert parts[0] == rec["username"]
        assert parts[1] == str(rec["user_id"])
        assert parts[2] == "free.nokaA"
        assert parts[3] == rec["cookie"]

        uc_path = os.path.join(td, "uc.txt")
        assert st.export_extracted(records, uc_path, fmt="user_cookie") == 1
        with open(uc_path) as f:
            assert f.read().strip() == f"{rec['username']}|{rec['cookie']}"

        raw_path = os.path.join(td, "raw.txt")
        assert st.export_extracted(records, raw_path, fmt="raw") == 1
        with open(raw_path) as f:
            assert f.read().strip() == rec["cookie"]


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
    batch = sysutils.get_batch_pkg_usage(["free.nokaA"])
    assert "free.nokaA" in batch

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
    ):
        fn()
        print(f"[PASS] {fn.__name__}")
    print("ALL TESTS PASSED")
