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

### WiFi power-save: must be disabled at the DRIVER level (2026-08-14)

This device is on WiFi (`wlP1p1s0`), not wired Ethernet (the onboard port
`enP8p1s0` exists but has never been cabled up). The adapter is a Realtek
RTL8822CE driven by NVIDIA's **out-of-tree `rtl8822ce`** driver —
`/etc/modprobe.d/nvidia-preferred-oot-modules.conf` deliberately blocks the
in-kernel `rtw88`/`rtw8822ce` driver in its favor. That detail matters:

> **`iw dev wlP1p1s0 get power_save` is a LIAR on this hardware.**
> NetworkManager's `wifi.powersave=2` and `iw ... set power_save off` only
> configure the nl80211/mac80211 layer. The out-of-tree Realtek driver runs
> its own independent LPS (Leisure Power Save) and IPS (Inactive Power
> Save, which powers the RF down entirely when idle) state machines that
> nl80211 does not touch. `iw` will cheerfully report `Power save: off`
> while `rtw_power_mgnt=2` and `rtw_ips_mode=1` leave the radio fully
> power-saving. We "fixed" power-save on 2026-08-13 via NetworkManager,
> verified it with `iw`, got a false pass, and the device went dark for
> another two hours the next morning.

**Always check the driver's own parameters, not `iw`:**

```bash
cat /sys/module/rtl8822ce/parameters/rtw_power_mgnt   # want 0
cat /sys/module/rtl8822ce/parameters/rtw_ips_mode     # want 0
```

Persisted in `/etc/modprobe.d/rtl8822ce-no-powersave.conf`:

```
options rtl8822ce rtw_power_mgnt=0 rtw_ips_mode=0
```

(These are load-time parameters; they're also writable at runtime via the
sysfs paths above if you need them applied without a reboot.)

The NetworkManager `wifi.powersave=2` config from the first attempt is
still in place at `/etc/NetworkManager/conf.d/wifi-powersave-off.conf`.
It's harmless and correct as far as it goes — it's just not sufficient on
its own, which is the whole lesson here.

### How the outages actually unfold

Worth understanding, because the symptom looks like a device crash and
isn't. The OS never froze in any incident — journald kept logging
continuously the whole time. Only the network died, which is why the power
light stayed on, SSH was dead, and a physical power-cycle "fixed" it (that
resets the radio; restarting the app never would have).

The failure chain, from the 2026-08-14 08:54 UTC incident:

1. Baseline is genuinely clean — **zero** roam/disconnect/channel-switch
   events for six straight hours beforehand. Signal is strong (-54 dBm,
   390 Mbit/s VHT). This is not slow signal degradation or range.
2. A discrete trigger: the AP issues an 802.11 channel-switch announcement
   (CSA). The driver drops carrier outright instead of switching in place.
3. That forces a full reassociation into a multi-AP network — SSID `<SSID>`
   has 4 BSSIDs (2 physical APs × 2 radios, OUI `<VENDOR-OUI>`). Band/mesh
   steering starts bouncing the client between them.
4. The APs begin refusing it: `CTRL-EVENT-ASSOC-REJECT status_code=1`
   (unspecified failure) ×9, plus deauths with `reason=2` (prev auth no
   longer valid), `reason=53` (invalid PMKID — stale PMK cache from the
   rapid roaming), and `reason=77`.
5. wpa_supplicant starts ignore-listing BSSIDs, which shrinks its options
   further, and the whole thing never reconverges. 55 channel switches, 27
   reconnects, 11 disconnects over ~2 hours until a reboot.

### ROOT CAUSE: a kernel bug in the driver's roam path (2026-08-14, confirmed)

Disabling driver power save did **not** stop it — a fourth outage hit at
14:38 UTC with `rtw_power_mgnt=0`/`rtw_ips_mode=0` verified active since
11:08 (sysfs writes, not just the modprobe file). That outage left a
kernel stack trace, which is the actual answer:

```
WARNING: CPU: 0 PID: 0 at net/wireless/sme.c:1202 cfg80211_roamed+0x460/0x4fc
Call trace:
  cfg80211_roamed                      [cfg80211]
  rtw_cfg80211_indicate_connect        [rtl8822ce]
  rtw_os_indicate_connect              [rtl8822ce]
  rtw_indicate_connect                 [rtl8822ce]
  rtw_joinbss_event_prehandle          [rtl8822ce]
  report_join_res                      [rtl8822ce]
  OnAssocRsp                           [rtl8822ce]
```

`cfg80211_roamed()` warns and **returns early** when a driver reports a
roam for an interface the kernel does not consider connected. The
out-of-tree `rtl8822ce` driver desyncs its state machine from cfg80211's
during a roam and trips exactly that check. Once it aborts, the radio is
associated at the RF layer but the kernel never registers the connection
— the interface is up, `iw dev ... link` looks healthy, and nothing can
route. There is no recovery path in the driver; it stays wedged until a
reboot resets both state machines. That is the multi-hour "frozen Jetson"
symptom, and why only a physical power-cycle ever cleared it.

**The bug is reachable only via the roam path.** No roam → `cfg80211_roamed`
is never called → it cannot trigger. Pinning the client to a single BSSID
is therefore a targeted fix, not a workaround. Applied 2026-08-14:

```bash
nmcli connection modify <SSID> 802-11-wireless.bssid <AP1-5GHZ>
nmcli connection modify <SSID> 802-11-wireless.band a
```

Why it roamed so much in the first place: SSID `<SSID>` is a multi-AP mesh
and this spot hears six BSSIDs, including far nodes at -71/-72 dBm that
the client kept selecting over a -46/-56 dBm AP in the same room. 161
associations across 6 BSSIDs in 3 days, for a device that never moves.
Disconnects were `locally_generated=1` — the client's own roaming logic,
not AP steering.

**Moving the device:** the pin does not fail over. After repositioning
(or if that AP is replaced), re-pin to the best local BSSID with:

```bash
sudo /usr/local/sbin/wifi-relock.sh          # scan, pick best, pin, verify
sudo /usr/local/sbin/wifi-relock.sh --clear  # unpin entirely (roams again)
```

Source of truth is `deploy/jetson/wifi-relock.sh`. It prefers the
strongest 5 GHz radio at or above -65 dBm (5 GHz is far less congested
here), else the strongest overall, and sets `band` to match so the client
cannot be band-steered off the pin.

**Always arm an auto-revert before changing WiFi config over SSH** — a bad
BSSID means physically walking to the device:

```bash
sudo systemd-run --on-active=300 --unit=wifi-revert-safety \
  /usr/local/sbin/wifi-revert.sh
# ...make the change, confirm connectivity, then:
sudo systemctl stop wifi-revert-safety.timer
```

Longer-term alternatives if pinning ever becomes untenable: update the
Realtek OOT driver, or drop
`/etc/modprobe.d/nvidia-preferred-oot-modules.conf`'s block on the
in-kernel `rtw88`/`rtw8822ce` driver and use that instead — it does not
share this bug, but it is not NVIDIA's supported configuration on JetPack.

Useful forensics for a future incident (persistent journald across reboots
is already configured, so the previous boot's logs survive):

```bash
# Did the OS actually die, or just the network? Look for real journal gaps:
journalctl --since '<start>' --until '<end>' -o short-unix --no-pager \
  | awk '{split($1,a,"."); t=a[1]; if (prev && (t-prev)>120) \
      print "GAP:", (t-prev), "sec ending", strftime("%F %T",t); prev=t}'

# What the supplicant saw (reason/status codes are the real diagnosis):
journalctl --since '<start>' --no-pager \
  | grep -oE 'CTRL-EVENT-[A-Z-]+|status_code=[0-9]+|reason=[0-9]+' \
  | sort | uniq -c | sort -rn
```

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
