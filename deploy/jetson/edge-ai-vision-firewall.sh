#!/bin/bash
# Restricts edge-ai-vision's host-exposed ports to the tailnet only:
#   5000 - fish-detection-app (video stream / metrics / healthz)
#   8000 - captures browser (read-only training-data image viewer)
#
# Both are published on 0.0.0.0 rather than pinned to the Jetson's
# Tailscale IP -- an IP-pinned publish races with Tailscale coming up on
# boot and can silently fail to bind, breaking the stream after every
# reboot (discovered 2026-08-14). This firewall rule enforces tailnet-only
# access at the host level instead. See RUNBOOK.md.
set -euo pipefail
for port in 5000 8000; do
  if ! iptables -C INPUT -p tcp --dport "${port}" -i tailscale0 -j ACCEPT 2>/dev/null; then
    iptables -A INPUT -p tcp --dport "${port}" -i tailscale0 -j ACCEPT
  fi
  if ! iptables -C INPUT -p tcp --dport "${port}" -j DROP 2>/dev/null; then
    iptables -A INPUT -p tcp --dport "${port}" -j DROP
  fi
done
