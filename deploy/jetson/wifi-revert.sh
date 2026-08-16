#!/bin/bash
# Emergency safety net: clear any BSSID/band pin and reconnect to anything.
#
# Use when a WiFi change has left the device unreachable, or arm it BEFORE
# making one over SSH so a mistake heals itself instead of requiring a walk
# to the device:
#
#   sudo systemd-run --on-active=300 --unit=wifi-revert-safety \
#     /usr/local/sbin/wifi-revert.sh
#   ...make the change, confirm connectivity, then:
#   sudo systemctl stop wifi-revert-safety.timer
#
# Deliberately unconditional and dependency-free -- it has to work when the
# network is already broken. See RUNBOOK.md.
IFACE="${IFACE:-wlP1p1s0}"
# Default to whichever wifi connection is currently active, so the SSID is
# never hardcoded here (this repo is public) and the script stays portable.
CONN="${1:-$(nmcli -t -f NAME,TYPE connection show 2>/dev/null \
  | awk -F: '$2 == "802-11-wireless" { print $1; exit }')}"
[[ -n "${CONN}" ]] || { echo "error: no wifi connection found; pass one as \$1" >&2; exit 1; }

nmcli connection modify "${CONN}" 802-11-wireless.bssid ''
nmcli connection modify "${CONN}" 802-11-wireless.band ''
nmcli connection down "${CONN}" || true
sleep 2
nmcli connection up "${CONN}" || nmcli device connect "${IFACE}" || true
