#!/data/data/com.termux/files/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
#  Rejoin Tool v3.0 Hybrid — QUICK SETUP 1 lệnh (theo kiểu QH Rejoin)
#
#  Cài online (sau khi push repo lên GitHub, sửa DEFAULT_REPO bên dưới):
#     . <(curl -sL https://raw.githubusercontent.com/hoangdangduong159-a11y/rejoin_tool/main/setup.sh)
#
#  Cài / cài lại từ thư mục local có sẵn:
#     . ~/rejoin_tool/setup.sh
#
#  Biến tùy chọn (đặt trước lệnh):
#     REPO_URL=...  BRANCH=main  INSTALL_DIR=~/rejoin_tool
#     SKIP_UPGRADE=1   (bỏ qua pkg upgrade)    SKIP_STORAGE=1 (bỏ qua termux-setup-storage)
#     RJ_TARGET=<PlaceID | VIP link>  RJ_PROFILE=default   (gán link cho mọi clone ngay khi cài)
# ─────────────────────────────────────────────────────────────────────────────

_rj_setup() {
    local DEFAULT_REPO="https://github.com/hoangdangduong159-a11y/rejoin_tool.git"
    local REPO_URL="${REPO_URL:-$DEFAULT_REPO}"
    local BRANCH="${BRANCH:-main}"
    local TERMUX_PREFIX="/data/data/com.termux/files/usr"
    local TERMUX_HOME="/data/data/com.termux/files/home"
    local INSTALL_DIR="${INSTALL_DIR:-$TERMUX_HOME/rejoin_tool}"
    local BIN="$TERMUX_PREFIX/bin"

    if [ "$(id -u)" = "0" ]; then
        echo "[!] Hãy chạy setup trong Termux thường (KHÔNG dùng su). Tool sẽ tự xin root khi cần."
        return 1
    fi
    cd "$TERMUX_HOME" || return 1

    # 1) Quyền bộ nhớ (/sdcard) cho Autoexecute/Workspace của executor
    if [ -z "$SKIP_STORAGE" ] && [ ! -d "$TERMUX_HOME/storage/shared" ] && command -v termux-setup-storage >/dev/null 2>&1; then
        echo "[*] Xin quyền bộ nhớ — hãy bấm 'Cho phép' nếu Android hỏi..."
        termux-setup-storage
        sleep 3
    fi

    # 2) Termux packages
    echo "[*] Cập nhật Termux packages..."
    yes | pkg update -y >/dev/null 2>&1 || echo "[!] pkg update lỗi (thử 'termux-change-repo' nếu mirror chậm)."
    if [ -z "$SKIP_UPGRADE" ]; then
        yes | pkg upgrade -y -o Dpkg::Options::=--force-confold >/dev/null 2>&1 || echo "[!] pkg upgrade lỗi, tiếp tục..."
    fi
    local need=()
    command -v python >/dev/null 2>&1 || need+=(python)
    command -v git    >/dev/null 2>&1 || need+=(git)
    command -v curl   >/dev/null 2>&1 || need+=(curl)
    command -v pkill  >/dev/null 2>&1 || need+=(procps)
    if [ "${#need[@]}" -gt 0 ]; then
        echo "[*] Cài: ${need[*]}"
        yes | pkg install -y "${need[@]}" || { echo "[!] Không cài được: ${need[*]}"; return 1; }
    fi

    # 3) Mã nguồn: clone / update / dùng bản local
    if [ -d "$INSTALL_DIR/.git" ]; then
        echo "[*] Cập nhật repo $INSTALL_DIR ..."
        git -C "$INSTALL_DIR" pull --ff-only || echo "[!] Bỏ qua git pull (có thay đổi local?)."
    elif [ -f "$INSTALL_DIR/main.py" ]; then
        echo "[*] Dùng mã nguồn local có sẵn: $INSTALL_DIR"
    else
        if [[ "$REPO_URL" == *"YOUR_USER"* ]]; then
            echo "[!] Chưa cấu hình REPO_URL (vẫn là YOUR_USER) và không có bản local tại $INSTALL_DIR."
            echo "    Chạy lại với: REPO_URL=https://github.com/<user>/rejoin_tool.git . <(curl -sL <link setup.sh>)"
            return 1
        fi
        echo "[*] Clone $REPO_URL ($BRANCH) -> $INSTALL_DIR ..."
        git clone --depth 1 --single-branch --no-tags --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR" \
            || { echo "[!] git clone thất bại."; return 1; }
    fi

    # 4) Python deps
    if ! python -c "import requests, rich" >/dev/null 2>&1; then
        echo "[*] Cài Python packages (requests, rich)..."
        pip install -q -r "$INSTALL_DIR/requirements.txt" 2>/dev/null || pip install -q requests rich \
            || { echo "[!] pip install thất bại."; return 1; }
    fi

    # 5) Launcher: $INSTALL_DIR/rejoin (đường dẫn tuyệt đối, chạy được cả user lẫn su -c) + lệnh 'rejoin'
    mkdir -p "$INSTALL_DIR/logs"
    chmod 755 "$INSTALL_DIR/rejoin" "$INSTALL_DIR/setup.sh" 2>/dev/null
    cat > "$BIN/rejoin" <<EOF
#!/data/data/com.termux/files/usr/bin/env bash
exec "$INSTALL_DIR/rejoin" "\$@"
EOF
    chmod 755 "$BIN/rejoin"

    # 6) Quick Setup: quét clone + gán link/profile + deploy Lua heartbeat & shared scripts
    echo "[*] Quét clone Roblox, deploy Autoexecute heartbeat..."
    if su -c true >/dev/null 2>&1; then
        if [ -n "$RJ_TARGET" ]; then
            "$BIN/rejoin" quick-setup "$RJ_TARGET" --profile "${RJ_PROFILE:-default}" || true
        else
            "$BIN/rejoin" quick-setup --profile "${RJ_PROFILE:-default}" || true
        fi
    else
        echo "[!] Chưa có quyền root (su) — bỏ qua bước quét clone. Cấp root cho Termux rồi chạy: rejoin quick-setup <PlaceID>"
    fi

    stty echo 2>/dev/null
    stty sane 2>/dev/null
    echo ""
    echo "[+] Cài đặt xong! Các lệnh copy-paste:"
    "$BIN/rejoin" commands 2>/dev/null || true
}

_rj_setup
unset -f _rj_setup
