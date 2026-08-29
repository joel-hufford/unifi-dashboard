# UniFi dashboard

A wall-panel dashboard for a UniFi network, built for a Raspberry Pi 5 with a
small touchscreen. A Python service polls the UniFi controller and pings the
internet; a single-page kiosk UI shows what the network is doing right now.

![The dashboard on a 1280x720 panel](docs/screenshot.png)

On a 1280x400 bar display the layout rearranges into three columns rather than
shrinking:

![The dashboard on a 1280x400 bar display](docs/screenshot-bar.png)

## What it shows

| Metric | Where it comes from |
|---|---|
| WAN status and public IP | controller health + the gateway's WAN interface |
| Latency to 8.8.8.8 (now, avg, min, max) | `ping` run **from the Pi**, not the controller |
| WAN packet loss over the window | the same probe, aggregated across the window |
| Connected devices, split Wi-Fi / wired / guest | `stat/sta` |
| WAN download and upload for the last hour | sampled every poll and stored locally |
| Average and peak download / upload | aggregated over the visible window |
| Wi-Fi connection quality | per-client satisfaction, or signal where the controller has none |
| Weakest clients, clients per band, UniFi device health | `stat/sta`, `stat/device` |

Latency and loss are measured from the Pi on purpose. The controller's own
figure is a slow-moving gateway health number that tells you nothing about
whether traffic is actually passing right now.

The graph window is switchable (15m / 1h / 3h) and scopes every number on the
page, so the averages always agree with the plots. Tapping a chart shows a
crosshair with all three series at that moment, and the `Table` button swaps
the plots for the same data in five-minute buckets.

## Display layouts

The page has three layouts, chosen by viewport rather than by configuration:

| Viewport | Layout |
|---|---|
| Landscape, taller than 460px | Top bar, a row of four stat tiles, then charts beside the Wi-Fi card. |
| **Landscape, 460px or shorter** (bar displays like 1280x400) | Three columns: latency and packet loss at the left, charts in the middle, devices and Wi-Fi quality at the right. The Wi-Fi detail card is dropped and its device-health line folds into the devices tile. |
| Portrait (the Touch Display 2's native orientation) | One column: tiles two-up, then charts, then the Wi-Fi card. |

Verified at 1280x400, 1024x600, 1280x720 and 720x1280, in both palettes. On a
short landscape panel (around 1024x600) the Wi-Fi card drops its per-band
breakdown and shows three weak clients instead of five, rather than clipping.

## Hardware

- **Raspberry Pi 5** (a 4 also works; the dashboard is not demanding).
- **Raspberry Pi Touch Display 2**, 5" or 7". It connects over DSI with a
  ribbon cable plus GPIO power and needs no drivers on current Pi OS. The panel
  is natively portrait (720x1280); the layout handles both orientations.
- The **27 W USB-C supply**. A 15 W brick will brown out under the display.
- Any HDMI + USB-touch panel works too, with more fiddling over rotation and
  touch-axis mapping.

Do not run the UniFi controller itself on this Pi. It should be a plain client
of your console.

## Install

```bash
git clone https://github.com/joel-hufford/unifi-dashboard.git
cd unifi-dashboard
sudo ./deploy/install.sh
```

That installs into `/opt/unifi-dashboard`, creates a virtualenv, writes
`/etc/unifi-dashboard/config.toml`, and enables a systemd service. Then edit
the config and start it:

```bash
sudo nano /etc/unifi-dashboard/config.toml
sudo systemctl restart unifi-dashboard
journalctl -u unifi-dashboard -f
```

### Controller access

Two ways in, in order of preference:

1. **API key** (UniFi Network 9.x on UniFi OS 4.1+). In the Network app:
   *Settings -> Control Plane -> Integrations -> Create API Key*. Put it in
   `unifi.api_key`, or in `/etc/unifi-dashboard/secrets.env` as
   `UNIFI_DASHBOARD_API_KEY=...` to keep it out of the config file.
2. **A local admin account**, for older controllers. Create a **local-only**
   admin with read-only permissions and 2FA disabled, then set
   `unifi.username` and `unifi.password`. A key is better: the session cookie
   route has to log in again whenever the controller expires it.

Consoles ship a self-signed certificate, so `verify_ssl` is `false` by default.
Point it at the console's certificate file to turn verification on.

The dashboard only ever issues GETs.

### Kiosk browser

```bash
mkdir -p ~/.config/labwc
cp /etc/xdg/labwc/autostart ~/.config/labwc/autostart          # keep the desktop bits
cat /opt/unifi-dashboard/deploy/labwc-autostart.example >> ~/.config/labwc/autostart
chmod 644 ~/.config/labwc/autostart
```

Create it as your login user, not with `sudo`: 644 on the file, 755 on
`~/.config/labwc`. labwc runs it through `sh`, so no execute bit is needed.
A user autostart **replaces** `/etc/xdg/labwc/autostart` rather than adding to
it, which is why the copy comes first — skip it only if you want a bare kiosk
screen with no panel or wallpaper.

Edit the file to set (or remove) the display-mode line, then turn off screen
blanking in `sudo raspi-config` -> Display Options -> Screen Blanking. Reboot.

If your panel's native mode is missing from `wlr-randr`, a
`--custom-mode` line here is usually less brittle than a `video=` argument in
`/boot/firmware/cmdline.txt`, which OS updates can rewrite. Note that
`cmdline.txt` is a **single line** — parameters are appended to the existing
line, space-separated; a second line is ignored.

Raspberry Pi OS Bookworm and later run labwc; on an older Wayfire image the
equivalent goes in `~/.config/wayfire.ini` under `[autostart]`.

## Running it without a controller

```bash
python3 -m unifi_dashboard --demo
```

Serves synthetic data on <http://127.0.0.1:8787/> with an hour of history
already in the graph. This is how the UI was built and is the fastest way to
try layout changes.

## Configuration

Everything lives in `config.toml`; see `config.example.toml` for the annotated
version. The settings worth knowing:

| Key | Default | Notes |
|---|---|---|
| `poll_interval` | `10.0` | Seconds between controller polls. 10s gives 360 points an hour. |
| `ping.target` | `8.8.8.8` | What "the internet" means for latency and loss. |
| `ping.count` | `3` | Packets per poll. More packets, finer loss resolution. |
| `history.window_minutes` | `60` | The window the page opens on. |
| `history.retention_minutes` | `180` | How much history is kept, and the longest selectable window. |
| `wlan.weak_signal_dbm` | `-70` | At or below this, a client counts as weak. |
| `server.host` | `127.0.0.1` | Set `0.0.0.0` to reach it from other machines. |

The dashboard has no authentication. Leave it bound to localhost unless your
network is one you are happy exposing it to.

## How it fits together

```
UniFi console ──┐
                ├─> poller (every 10s) ─> SQLite ring buffer ─> /api/dashboard ─> kiosk page
ping 8.8.8.8 ───┘
```

- `unifi_client.py` — talks to the controller; handles both auth modes and both
  URL layouts (UniFi OS's `/proxy/network` prefix, and a bare controller).
- `metrics.py` — pure functions turning controller JSON into numbers. UniFi's
  payloads differ across firmware, so each field is looked for in several
  places.
- `ping.py` — the WAN probe, parsed from `ping` output.
- `storage.py` — one row per poll in SQLite, pruned to the retention window,
  with the window aggregates computed in SQL.
- `poller.py` — the loop; also differences the WAN byte counters when the
  firmware does not report instantaneous rates.
- `static/` — the page. No build step, no CDN: it has to render with the WAN
  down.

## Development

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
pytest
python -m unifi_dashboard --demo
```

## Notes and limitations

- **"Average download" includes idle time.** It is average throughput over the
  window, not average throughput while busy, so an idle hour reads near zero.
  The peak figure is the busiest single poll interval.
- Throughput is sampled at the poll interval, so a burst shorter than ~10
  seconds shows up smaller than it really was.
- Wi-Fi quality uses the controller's own satisfaction score where it exists
  and a signal-strength score where it does not, so the number is comparable
  between clients but is not a UniFi-official metric.
- SD cards do not enjoy constant writes. The history database is small and
  WAL-mode, but an SSD or USB boot drive is kinder if you plan to leave this
  running for years.
