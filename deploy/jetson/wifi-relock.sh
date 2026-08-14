#!/bin/bash
# Re-pin the Jetson's WiFi to the best BSSID at its CURRENT physical location.
#
# Run this after moving the device. Pinning is required because the
# rtl8822ce driver has a kernel bug in its roam path (WARNING at
# net/wireless/sme.c:1202 in cfg80211_roamed) that permanently wedges the
# network stack -- see RUNBOOK.md. Never roaming is what keeps it alive, so
# the lock has to be re-pointed by hand whenever the radio environment
# changes rather than left to fail over on its own.
#
# Usage:  sudo ./wifi-relock.sh [connection-name]     (default: <SSID>)
#         sudo ./wifi-relock.sh --clear [connection-name]   # unpin only
set -euo pipefail

IFACE="${IFACE:-wlP1p1s0}"
# 5 GHz is far less congested; prefer it unless it's weaker than this.
FIVE_GHZ_FLOOR="${FIVE_GHZ_FLOOR:--65}"

CLEAR_ONLY=0
if [[ "${1:-}" == "--clear" ]]; then CLEAR_ONLY=1; shift; fi
CONN="${1:-<SSID>}"

log() { printf '==> %s\n' "$*"; }

log "Clearing existing BSSID/band lock on '${CONN}'"
nmcli connection modify "${CONN}" 802-11-wireless.bssid ''
nmcli connection modify "${CONN}" 802-11-wireless.band ''
nmcli connection down "${CONN}" >/dev/null 2>&1 || true
sleep 2
nmcli connection up "${CONN}" >/dev/null
sleep 5

if [[ "${CLEAR_ONLY}" -eq 1 ]]; then
  log "Lock cleared; connected unpinned. Re-run without --clear to re-pin."
  iw dev "${IFACE}" link | head -3
  exit 0
fi

SSID="$(nmcli -t -f 802-11-wireless.ssid connection show "${CONN}" | cut -d: -f2-)"
[[ -n "${SSID}" ]] || { echo "error: could not read SSID for '${CONN}'" >&2; exit 1; }
log "Scanning for SSID '${SSID}'"

# Flush each BSS block when the next one starts; print "<signal> <bssid> <freq>".
CANDIDATES="$(iw dev "${IFACE}" scan 2>/dev/null | awk -v want="${SSID}" '
  function flush() { if (ssid == want && sig != "") print sig, bss, freq }
  /^BSS /      { flush(); bss=$2; sub(/\(.*/, "", bss); sig=""; ssid=""; freq="" }
  /^\tsignal:/ { sig=$2+0 }
  /^\tfreq:/   { freq=$2+0 }
  /^\tSSID: /  { ssid=substr($0, index($0, ": ")+2) }
  END          { flush() }
' | sort -rn)"

[[ -n "${CANDIDATES}" ]] || { echo "error: no BSSIDs found for '${SSID}'" >&2; exit 1; }
log "Visible BSSIDs (strongest first):"
echo "${CANDIDATES}" | awk '{printf "      %-20s %6s dBm  %s MHz\n", $2, $1, $3}'

# Prefer the best 5 GHz radio if it clears the floor, else the strongest overall.
BEST="$(echo "${CANDIDATES}" | awk -v floor="${FIVE_GHZ_FLOOR}" \
  '$3 > 5000 && $1 >= floor { print $2; exit }')"
if [[ -z "${BEST}" ]]; then
  BEST="$(echo "${CANDIDATES}" | awk 'NR==1 { print $2 }')"
  log "No 5 GHz BSSID at or above ${FIVE_GHZ_FLOOR} dBm; using strongest overall"
fi

BAND="$(echo "${CANDIDATES}" | awk -v b="${BEST}" '$2 == b { print ($3 > 5000 ? "a" : "bg"); exit }')"
log "Pinning to ${BEST} (band ${BAND})"

nmcli connection modify "${CONN}" 802-11-wireless.bssid "${BEST}"
nmcli connection modify "${CONN}" 802-11-wireless.band "${BAND}"
nmcli connection down "${CONN}" >/dev/null 2>&1 || true
sleep 2
nmcli connection up "${CONN}" >/dev/null

sleep 3
log "Now associated with:"
iw dev "${IFACE}" link | head -6
