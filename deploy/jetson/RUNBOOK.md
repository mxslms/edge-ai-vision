# Jetson field-device runbook

Everything here is config that lives only on the physical `jetson-field`
device (or in Portainer/Tailscale's own state) and is **not** reproduced by
cloning this repo or running the installer alone. If this Jetson dies and
gets replaced, this is the list of things to redo by hand. Dates mark when
something was discovered/decided, for context, not because it's expected to
change again.

## 1. Base OS / hardware prerequisites

- JetPack 6.x flashed, `aarch64`.
- USB camera present as `/dev/video0`.
- Docker + nvidia-container-toolkit configured (`docker info | grep -i
  runtime` shows `nvidia`).
- Tailscale installed and connected (`tailscale status` shows this host).
- Portainer agent container running and this device added as an
  environment/endpoint in Portainer (see homelab-infrastructure repo for
  the Portainer server itself, which runs on `homelab-server`).

## 2. Deploy the app: Portainer only, not systemd (2026-08-14)

**The `edge-ai-vision` Portainer stack is the one and only thing that should
ever run `docker compose` for this app on this host.**

We originally deployed via `install-jetson.sh`'s systemd unit
(`edge-ai-vision.service`, `docker compose` against a local checkout at
`/opt/edge-ai-vision`), then *later* also added a Portainer git-stack for
the same container. Nobody noticed both existed until captured training
images stopped landing on the host disk — the systemd path was running a
stale, pre-feature copy of `docker-compose.jetson.yml` and silently winning
the fight (whichever one redeploys most recently wins, since both target a
container with the same name). The systemd deployment has been disabled
and removed; `install-jetson.sh` now defaults to **not** installing it
(`--with-systemd` to opt back in, only for a Jetson Portainer does not
manage at all).

**To deploy/update the app now:** Portainer → Stacks → `edge-ai-vision` →
Pull and redeploy (check "re-pull image" to force a fresh pull rather than
relying on the cached local image).

### GHCR pull credentials

The repo/image is private. Portainer's redeploys pull successfully because
`root`'s Docker credential store on this host (`/root/.docker/config.json`)
has a GHCR login — copied from `mxslms`'s own `~/.docker/config.json`
(`docker login ghcr.io` done once, manually, as `mxslms`). If this ever
stops working (`unauthorized` in the stack's pull step), re-run:

```bash
sudo mkdir -p /root/.docker
sudo cp ~/.docker/config.json /root/.docker/config.json
sudo chmod 600 /root/.docker/config.json
```

## 3. Networking

### WiFi power-save: off (2026-08-14)

This device is on WiFi (`wlP1p1s0`), not wired Ethernet (the onboard port
`enP8p1s0` exists but has never been cabled up — running a cable is the
single most reliable fix if this recurs and hasn't been done). WiFi
power-save caused repeated multi-hour connectivity outages (rapid
carrier-loss flapping, NetworkManager stuck in a reconnect loop, Jetson
fully unreachable but still powered on) once the app moved to continuous,
always-on operation. Fixed via:

```bash
sudo tee /etc/NetworkManager/conf.d/wifi-powersave-off.conf > /dev/null <<'EOF'
[connection]
wifi.powersave = 2
EOF
sudo systemctl restart NetworkManager
```

Verify with `iw dev wlP1p1s0 get power_save` → should say `off`.

**Gotcha:** restarting NetworkManager can wipe the iptables NAT rules
Docker set up for a running container's published ports, without crashing
the container. Symptom: `docker port <container>` shows nothing, app is
unreachable, but `docker logs` shows it running fine internally. A plain
`docker restart` does **not** fix this (it reuses the broken network
plumbing) — the container has to be fully removed and recreated
(`docker stop` + `docker rm`, then redeploy from Portainer).

### Port publishing: 0.0.0.0 + host firewall, not IP-pinned (2026-08-14)

`docker-compose.jetson.yml` used to publish port 5000 pinned to this
device's specific Tailscale IP (`100.90.234.112:5000:5000`). That races
against Tailscale coming up during boot — if Docker starts the container
before `tailscale0` has the IP assigned, the bind can silently fail, and
the stream stays unreachable until something fully recreates the
container. It now publishes on `0.0.0.0` instead, and tailnet-only access
is enforced by a host firewall rule (matching how the captures browser is
also secured):

- `deploy/jetson/edge-ai-vision-firewall.sh` — idempotent iptables rule,
  restricts ports 5000 and 8000 to traffic arriving via `tailscale0`.
- `deploy/jetson/edge-ai-vision-firewall.service` — systemd unit that runs
  it at boot. `install-jetson.sh` installs and enables this automatically.

**Must use the `DOCKER-USER` chain, not `INPUT`.** First attempt at this
put the rules in `INPUT` and they silently never matched: Docker-published
container ports are DNAT'd in `PREROUTING` and then routed through
`FORWARD`, not `INPUT`, so an `INPUT` rule has no effect on them. Port 5000
was actually reachable from the LAN for a few minutes on 2026-08-14 despite
an `INPUT` rule that looked correct and had tested fine — the earlier
"LAN blocked" check had only ever been true because the old IP-pinned
Docker publish never listened on the LAN interface at all, not because any
firewall rule worked. `DOCKER-USER` is the chain Docker guarantees runs
before its own rules and never modifies itself; that's what the script
uses now.

Verify: `sudo iptables -L DOCKER-USER -n -v | grep -E 'dpt:5000|dpt:8000'`
should show an ACCEPT on `tailscale0` and a DROP on `!tailscale0` for each
port, and packet counters should climb on the ACCEPT rule as you use the
app normally. Trust the counters, not just rule presence — confirm from an
**actual LAN client** (not the Jetson itself; a host curling its own
Tailscale IP can route via loopback rather than the physical interface a
rule matches on, giving a false pass/hang either way).

## 4. Training-data capture browser (host-level, not Docker)

A plain Python process, not a container, so it survives independently of
the app stack:

```bash
sudo cp deploy/jetson/edge-ai-vision-captures-browser.service \
  /etc/systemd/system/edge-ai-vision-captures-browser.service
sudo systemctl daemon-reload
sudo systemctl enable --now edge-ai-vision-captures-browser
```

Browse at `http://<jetson-tailscale-ip>:8000/`. Serves
`/var/lib/edge-ai-vision/captures` read-only — the same host directory the
app's `docker-compose.jetson.yml` bind-mounts into `/data/captures`. Access
is tailnet-only via the same firewall rule as above (§3), not this
service's own binding.

## 5. App tunables and capture/retention settings

Documented in `deploy/jetson/env.example` (motion gating, save/retention
thresholds, etc.) — copy to `/opt/.../ .env` or set as Portainer stack
environment variables. Not duplicated here since that file is the source
of truth and is already version-controlled.

## 6. Monitoring

node-exporter, cAdvisor, Promtail, GPU exporter, and the persistent
journald logging config (needed to investigate incidents like the WiFi one
above, since it retains logs across reboots) live in the
`jetson-monitoring` Portainer stack, defined in the `homelab-infrastructure`
repo — not this one. See that repo for anything monitoring-related.
