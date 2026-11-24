"""Native Roblox Android client log parser, batched log tail watcher, and ANR/crash detector."""
import re
import shlex
import threading

from rjcore import su

ROBLOX_ERROR_CODES = {
    264: "same_account_launched",
    267: "kicked_by_script",
    268: "unexpected_client",
    273: "account_moderated_or_reauth",
    277: "lost_connection",
    279: "connect_failed",
    524: "vip_unauthorized",
    529: "roblox_service_down",
}

_UUID_RE = re.compile(r"\b([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b", re.IGNORECASE)
_PLACE_RE = re.compile(r"(?:place(?:id)?[=: ]+|place\s+)(\d{5,15})", re.IGNORECASE)
_JOB_KEY_RE = re.compile(r"(?:jobid|gameinstanceid|serverid)[=: ]+([0-9a-f-]{36})", re.IGNORECASE)
_ERR_CODE_RE = re.compile(
    r"(?:error\s*code|errorcode|disconnect(?:ed)?\s*(?:with\s*)?(?:reason|code)|reason|code)\s*[:=()]?\s*(\d{3})\b",
    re.IGNORECASE,
)

_JOIN_PATTERNS = (
    "joining game",
    "joining server",
    "gamejoinloadtime",
    "client:connect(",
    "connecting to ",
    "udpconnect",
)
_IN_GAME_PATTERNS = (
    "connection accepted from",
    "replicator created",
    "server joined",
    "received join data",
    "networkclient:connectionaccepted",
)
_DISCONNECT_PATTERNS = (
    "disconnect from",
    "sending disconnect",
    "disconnected from game",
    "error code:",
    "errorcode:",
    "connection lost",
    "lost connection",
    "failed to connect to the game",
    "same account launched",
    "kicked from this",
    "unexpected client behavior",
    "not authorized to join",
    "id = 17",
    "id=17",
    "http 529",
    "error 529",
)


def _infer_error_code(line, low):
    m = _ERR_CODE_RE.search(line)
    if m:
        return int(m.group(1))
    if "same account launched" in low:
        return 264
    if "unexpected client behavior" in low:
        return 268
    if "kicked from this" in low or "was kicked" in low:
        return 267
    if "lost connection" in low or "connection lost" in low:
        return 277
    if "failed to connect to the game" in low or "id = 17" in low or "id=17" in low:
        return 279
    if "not authorized to join" in low:
        return 524
    if "529" in low and ("service" in low or "http" in low or "error" in low):
        return 529
    if "account" in low and ("moderated" in low or "reauth" in low or "another session" in low):
        return 273
    m2 = re.search(r"\b(26[478]|27[379]|52[49])\b", line)
    if m2:
        return int(m2.group(1))
    return None


def parse_roblox_log_tail(text):
    """Parse the tail of a Roblox Android log file into the latest connection/error state.

    Events are evaluated chronologically top-to-bottom so older errors occurring before
    a newer `Joining` or `Connection accepted` event are superseded.
    """
    state = "unknown"
    error_code = None
    error_kind = None
    place_id = None
    job_id = None
    message = ""

    if not text:
        return {
            "state": state,
            "error_code": error_code,
            "error_kind": error_kind,
            "place_id": place_id,
            "job_id": job_id,
            "message": message,
        }

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        low = line.lower()

        mp = _PLACE_RE.search(line)
        if mp:
            try:
                pid_val = int(mp.group(1))
                if pid_val > 0:
                    place_id = pid_val
            except ValueError:
                pass

        mj = _JOB_KEY_RE.search(line)
        if mj:
            job_id = mj.group(1)
        elif "joining" in low or "connection accepted" in low or "job" in low:
            mu = _UUID_RE.search(line)
            if mu:
                job_id = mu.group(1)

        if any(p in low for p in _IN_GAME_PATTERNS):
            state = "in_game"
            error_code = None
            error_kind = None
            message = line[-180:]
            continue

        if any(p in low for p in _JOIN_PATTERNS):
            state = "joining"
            error_code = None
            error_kind = None
            message = line[-180:]
            continue

        if any(p in low for p in _DISCONNECT_PATTERNS):
            code = _infer_error_code(line, low)
            state = "disconnected"
            error_code = code
            error_kind = ROBLOX_ERROR_CODES.get(code, "disconnected") if code else "disconnected"
            message = line[-180:]
            continue

    return {
        "state": state,
        "error_code": error_code,
        "error_kind": error_kind,
        "place_id": place_id,
        "job_id": job_id,
        "message": message,
    }


class BatchLogWatcher:
    """Batched root log-tail watcher for Roblox Android packages."""

    def __init__(self, tail_bytes=16384):
        self.tail_bytes = max(4096, int(tail_bytes))
        self._lock = threading.Lock()
        self._cache = {}  # pkg -> {"path": str, "mtime": float, "size": int, "parsed": dict}

    def reset_package(self, pkg):
        with self._lock:
            self._cache.pop(pkg, None)

    def poll_packages(self, packages, min_mtime=None, min_mtime_map=None):
        """Poll newest log_*.txt across all packages in ONE su() call."""
        pkgs = []
        for p in packages or []:
            if p and p not in pkgs:
                pkgs.append(p)
        if not pkgs:
            return {}

        cmds = []
        for p in pkgs:
            qp = shlex.quote(p)
            log_dir = shlex.quote(f"/data/data/{p}/files/appData/logs")
            min_ts = int(
                (min_mtime_map or {}).get(p, min_mtime if min_mtime is not None else 0)
            )
            cmds.append(
                f'D={log_dir}; '
                f'F=$(ls -1t "$D"/log_*.txt "$D"/*.log 2>/dev/null | head -n 1); '
                f'if [ -n "$F" ] && [ -f "$F" ]; then '
                f'MT=$(stat -c %Y "$F" 2>/dev/null || echo 0); '
                f'SZ=$(stat -c %s "$F" 2>/dev/null || echo 0); '
                f'if [ "$MT" -ge "{min_ts}" ] 2>/dev/null; then '
                f'echo "<<<RJ_LOG_HDR|{p}|$F|$MT|$SZ>>>"; '
                f'tail -c {self.tail_bytes} "$F" 2>/dev/null; '
                f'echo ""; echo "<<<RJ_LOG_END|{p}>>>"; '
                f'fi; fi'
            )

        rc, out, _ = su("; ".join(cmds), timeout=15)
        if rc != 0 or not out:
            return {}

        results = {}
        cur_pkg = None
        cur_meta = None
        buf = []
        for line in out.splitlines():
            if line.startswith("<<<RJ_LOG_HDR|") and line.endswith(">>>"):
                parts = line[len("<<<RJ_LOG_HDR|"):-3].split("|", 3)
                if len(parts) == 4:
                    cur_pkg = parts[0]
                    cur_meta = (parts[1], parts[2], parts[3])
                    buf = []
                continue
            if cur_pkg and line == f"<<<RJ_LOG_END|{cur_pkg}>>>":
                fpath, mt_s, sz_s = cur_meta
                try:
                    mtime = float(mt_s)
                except ValueError:
                    mtime = 0.0
                try:
                    size = int(sz_s)
                except ValueError:
                    size = 0

                with self._lock:
                    prev = self._cache.get(cur_pkg)
                    if prev and prev["path"] == fpath and prev["mtime"] == mtime and prev["size"] == size:
                        parsed = dict(prev["parsed"])
                    else:
                        parsed = parse_roblox_log_tail("\n".join(buf))
                        self._cache[cur_pkg] = {
                            "path": fpath,
                            "mtime": mtime,
                            "size": size,
                            "parsed": dict(parsed),
                        }
                parsed["log_path"] = fpath
                parsed["mtime"] = mtime
                results[cur_pkg] = parsed
                cur_pkg = None
                cur_meta = None
                buf = []
                continue
            if cur_pkg is not None:
                buf.append(line)

        return results


def parse_anr_windows(dumpsys_text, packages):
    """Parse `dumpsys window windows` or `dumpsys activity processes` output for ANR/crash dialogs."""
    crashing = set()
    if not dumpsys_text or not packages:
        return crashing

    pkg_patterns = {
        p: re.compile(rf"(?<![A-Za-z0-9_.]){re.escape(p)}(?![A-Za-z0-9_.])")
        for p in packages
        if p
    }
    crash_markers = (
        "application not responding",
        "apperrordialog",
        "anr ",
        "not responding:",
        "application error:",
    )

    for line in dumpsys_text.splitlines():
        low = line.lower()
        if any(m in low for m in crash_markers):
            for p, pat in pkg_patterns.items():
                if pat.search(line):
                    crashing.add(p)
    return crashing


def detect_anr_or_crash_packages(packages, dismiss_dialogs=True):
    """Detect frozen/ANR Roblox packages in 1 su() call and optionally close stuck system dialogs."""
    pkgs = [p for p in (packages or []) if p]
    if not pkgs:
        return set()

    crashing = set()
    rc, out, _ = su("dumpsys window windows", timeout=8)
    if rc == 0 and out:
        crashing.update(parse_anr_windows(out, pkgs))

    if not crashing:
        rc2, out2, _ = su("dumpsys activity processes", timeout=8)
        if rc2 == 0 and out2:
            crashing.update(parse_anr_windows(out2, pkgs))

    if crashing and dismiss_dialogs:
        su("am broadcast -a android.intent.action.CLOSE_SYSTEM_DIALOGS", timeout=5)

    return crashing
