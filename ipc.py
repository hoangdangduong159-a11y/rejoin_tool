"""Local Unix-domain socket JSON-line IPC server & client for background daemon control."""
import json
import os
import socket
import struct
import threading

DEFAULT_IPC_SOCK = "/data/data/com.termux/files/home/.rejoin_ipc.sock"
IPC_MAX_MSG_BYTES = 262144  # 256 KiB


def _read_json_line(conn, cap=IPC_MAX_MSG_BYTES):
    buf = bytearray()
    while True:
        chunk = conn.recv(4096)
        if not chunk:
            break
        nl = chunk.find(b"\n")
        if nl != -1:
            buf.extend(chunk[:nl])
            break
        buf.extend(chunk)
        if len(buf) > cap:
            raise ValueError("ipc_message_too_large")
    if len(buf) > cap:
        raise ValueError("ipc_message_too_large")
    if not buf:
        return None
    return json.loads(buf.decode("utf-8"))


def _peer_allowed(conn):
    """Allow connections from the same EUID or root (UID 0) via SO_PEERCRED."""
    if not hasattr(socket, "SO_PEERCRED"):
        return True
    try:
        cred = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        _pid, uid, _gid = struct.unpack("3i", cred)
        return uid in (os.geteuid(), 0)
    except OSError:
        return False


class IPCServer:
    """Thread-pooled Unix domain socket JSON-line server."""

    def __init__(self, sock_path=DEFAULT_IPC_SOCK, handlers=None, max_workers=16):
        self.sock_path = sock_path
        self.handlers = dict(handlers or {})
        self.sem = threading.BoundedSemaphore(max_workers)
        self.stop_event = threading.Event()
        self._sock = None
        self._thread = None

    def start(self):
        parent = os.path.dirname(os.path.abspath(self.sock_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        if os.path.exists(self.sock_path):
            try:
                os.unlink(self.sock_path)
            except OSError:
                pass

        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(self.sock_path)
        try:
            os.chmod(self.sock_path, 0o600)
        except OSError:
            pass
        srv.listen(16)
        srv.settimeout(0.5)
        self._sock = srv
        self._thread = threading.Thread(target=self._accept_loop, daemon=True, name="rejoin-ipc")
        self._thread.start()

    def _accept_loop(self):
        while not self.stop_event.is_set():
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break

            if not _peer_allowed(conn):
                try:
                    conn.sendall(b'{"ok":false,"error":"unauthorized_peer"}\n')
                except OSError:
                    pass
                conn.close()
                continue

            if not self.sem.acquire(blocking=False):
                try:
                    conn.sendall(b'{"ok":false,"error":"server_busy"}\n')
                except OSError:
                    pass
                conn.close()
                continue

            t = threading.Thread(target=self._handle_conn, args=(conn,), daemon=True)
            t.start()

    def _handle_conn(self, conn):
        try:
            conn.settimeout(5.0)
            req = _read_json_line(conn)
            if not isinstance(req, dict):
                resp = {"ok": False, "error": "invalid_request"}
            else:
                cmd = req.get("cmd")
                args = req.get("args") or {}
                if not isinstance(cmd, str) or not cmd:
                    resp = {"ok": False, "error": "invalid_command"}
                elif not isinstance(args, dict):
                    resp = {"ok": False, "error": "invalid_arguments"}
                elif cmd not in self.handlers:
                    resp = {"ok": False, "error": f"unknown_command:{cmd}"}
                else:
                    result = self.handlers[cmd](args)
                    resp = {"ok": True, "result": result}
            conn.sendall((json.dumps(resp, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8"))
        except Exception as e:
            try:
                conn.sendall((json.dumps({"ok": False, "error": str(e)}) + "\n").encode("utf-8"))
            except OSError:
                pass
        finally:
            try:
                conn.close()
            except OSError:
                pass
            self.sem.release()

    def stop(self):
        self.stop_event.set()
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        if os.path.exists(self.sock_path):
            try:
                os.unlink(self.sock_path)
            except OSError:
                pass


def ipc_request(cmd, args=None, sock_path=DEFAULT_IPC_SOCK, timeout=5.0):
    """Send a single JSON-line IPC request to the running daemon."""
    if not isinstance(cmd, str) or not cmd:
        return {"ok": False, "error": "ipc_invalid_command"}
    if args is None:
        args = {}
    if not isinstance(args, dict):
        return {"ok": False, "error": "ipc_invalid_arguments"}
    if not os.path.exists(sock_path):
        return {"ok": False, "error": "ipc_unavailable"}

    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        conn.settimeout(timeout)
        conn.connect(sock_path)
        payload = json.dumps({"cmd": cmd, "args": args}, separators=(",", ":"), ensure_ascii=False) + "\n"
        conn.sendall(payload.encode("utf-8"))
        resp = _read_json_line(conn)
        if not isinstance(resp, dict):
            return {"ok": False, "error": "ipc_response_invalid"}
        return resp
    except (OSError, ValueError) as e:
        return {"ok": False, "error": f"ipc_dead:{e}"}
    finally:
        try:
            conn.close()
        except OSError:
            pass
