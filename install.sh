#!/data/data/com.termux/files/usr/bin/env bash
# Standalone installer wrapper for Rejoin Tool v4.0 Ultra (compatible with local execution & curl).
_rj_install_entry() {
    local TERMUX_HOME="/data/data/com.termux/files/home"
    local INSTALL_DIR="${INSTALL_DIR:-$TERMUX_HOME/rejoin_tool}"
    local DEFAULT_SETUP_URL="https://raw.githubusercontent.com/hoangdangduong159-a11y/rejoin_tool/main/setup.sh"

    if [ -f "$INSTALL_DIR/setup.sh" ]; then
        . "$INSTALL_DIR/setup.sh"
        return $?
    fi
    . <(curl -sL "${RJ_SETUP_URL:-$DEFAULT_SETUP_URL}")
}

_rj_install_entry
unset -f _rj_install_entry
