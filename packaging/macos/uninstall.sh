#!/bin/sh
set -eu

BIN="/usr/local/lib/zhunt/zhunt"
INSTALL_ROOT="/usr/local/lib/zhunt"
LAUNCHER="/usr/local/bin/zhunt"
UNINSTALLER="/usr/local/bin/zhunt-uninstall"
RECEIPT="com.kindredwildcat.zhunt"

if [ "${1:-}" != "--yes" ]; then
    printf '%s' 'Remove Zhunt and restore its managed app configurations? [y/N] '
    read answer
    case "$answer" in
        y|Y|yes|YES) ;;
        *) echo 'Cancelled.'; exit 0 ;;
    esac
fi

if [ "$(id -u)" -ne 0 ]; then
    if [ ! -x "$BIN" ]; then
        echo "Zhunt executable not found at $BIN" >&2
        exit 1
    fi
    "$BIN" uninstall --all
    exec sudo "$0" --yes --skip-config
fi

if [ "${2:-}" != "--skip-config" ] && [ -x "$BIN" ]; then
    "$BIN" uninstall --all || true
fi
rm -rf "$INSTALL_ROOT" "$LAUNCHER" "$UNINSTALLER"
pkgutil --forget "$RECEIPT" >/dev/null 2>&1 || true
echo 'Zhunt was removed. ~/.zhunt was retained; remove it separately if you want to delete keys and telemetry.'
