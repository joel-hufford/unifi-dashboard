# Getting an unusual panel to run at its native mode

Bar displays (1280x400, 1920x480) and other odd panels often will not come up
at their native resolution on a Pi. The fix is almost never in one place,
because two independent layers are involved:

| Layer | Owns | Tool |
|---|---|---|
| **1. The mode exists** | the kernel's DRM mode list for the connector, built from the panel's EDID | `video=` on the kernel command line |
| **2. The mode is selected** | which of those modes the session actually uses | kanshi (on Pi OS), `wlr-randr` interactively |

A layer-2 tool can only pick from what layer 1 offers. If the panel does not
advertise its native mode in its EDID, no amount of kanshi or `wlr-randr`
configuration will produce it - the profile fails to apply and you silently
fall back to the preferred mode. Fix layer 1 first, confirm, then layer 2.

## 1. Make the mode exist

`/boot/firmware/cmdline.txt` is a **single line**. Parameters are appended to
that line, space-separated. A parameter on a second line is ignored entirely -
this is the single most common reason "I set it and nothing happened".

```
... rootwait video=HDMI-A-1:1280x400M@60e
```

- `M` derives timings with the CVT formula rather than looking the mode up in
  the EDID. Without it a mode the panel does not advertise is usually refused.
- `e` forces the output enabled even when EDID or hotplug detection fails,
  which is common on cheap panels with no EDID EEPROM.

Check the connector name first - the Pi 5 has two HDMI ports:

```bash
for p in /sys/class/drm/card*-HDMI-A-*; do echo "$p: $(cat "$p"/status)"; done
```

Legacy `hdmi_group`, `hdmi_mode` and `hdmi_cvt` settings in `config.txt` do
**not** work here. They belong to the pre-KMS firmware stack; the Pi 5 is
KMS-only. Plenty of bar-display guides still recommend them.

Reboot, then confirm the mode is now on offer before going further:

```bash
cat /sys/class/drm/card*-HDMI-A-1/modes
wlr-randr
```

## 2. Select it

On Raspberry Pi OS the labwc session runs **kanshi**, which owns output
configuration and re-applies its profile on every hotplug. Put the mode in
`~/.config/kanshi/config`, not in labwc's autostart, or the two will fight:

```
profile {
	output HDMI-A-1 enable mode 1280x400 position 0,0 scale 1
}
```

**Leave the refresh rate off.** Panels rarely run at a round number - a
1280x400 bar display typically reports `59.999001` - and kanshi matches the
rate exactly. Write `@60Hz` and the profile silently fails to apply, dropping
you back to whatever you were trying to escape, with symptoms identical to
having changed nothing. Without a rate it matches on resolution and takes the
preferred timing.

Edit the *existing* profile block rather than appending a new one: kanshi uses
the first profile whose outputs match the connected set, so a block appended
after one that identifies the output by description (a quoted
`"Vendor Model Serial"`) is never reached. Newer kanshi also reads
`~/.config/kanshi/config.d/`, which outranks a hand-edited `config`.

Then `kanshictl reload`, or reboot. Pi OS's Screen Configuration tool writes
this same file, so it can overwrite hand edits - and a profile it saved
earlier, when the panel offered fewer modes, is a common reason a newly
available mode never gets used. If the mode you want is now the *preferred*
one, deleting the override entirely is the tidiest fix: with no profile, the
compositor picks the preferred mode by itself.

For a one-off interactive test - useful for checking a mode before committing
to it - `wlr-randr --output HDMI-A-1 --mode 1280x400@60`. Some builds also
have `--custom-mode`, which bypasses layer 1 for the current session only;
check `wlr-randr --help` before relying on it.

## If the mode still will not appear

The panel is not declaring a usable EDID. Capture what it does report:

```bash
sudo apt install -y edid-decode
edid-decode < /sys/class/drm/card1-HDMI-A-1/edid
```

The fix from here is to supply an EDID yourself. This is also the tidier
long-term answer even when `video=` works, because the panel ends up
self-describing: the mode survives `cmdline.txt` being rewritten by an OS
update, and it is the preferred mode rather than one forced over the top.

### Using the bundled 1280x400 EDID

`deploy/edid/pi_1280x400.bin` is a 256-byte EDID 1.3 blob for a 1280x400 bar
display:

| | |
|---|---|
| preferred timing | 1280x400 @ 60.003307 Hz |
| pixel clock | 36.290 MHz (htotal 1440, vtotal 420) |
| monitor name | `1280x400 CVT` |

```bash
sudo mkdir -p /lib/firmware/edid
sudo cp deploy/edid/pi_1280x400.bin /lib/firmware/edid/
```

Then in `/boot/firmware/cmdline.txt` - still one line - **replace** any
`video=` parameter for this connector with:

```
drm.edid_firmware=HDMI-A-1:edid/pi_1280x400.bin
```

Replace rather than add. A forced `video=` mode and an EDID-declared mode for
the same resolution produce slightly different timings (a CVT-derived
`59.999001` against this blob's `60.003307`), and keeping both leaves two
near-identical modes in the list and no easy way to tell which is live.

Reboot and confirm with `wlr-randr`. If the connector comes up with its
original modes and no error, the EDID was requested before the root
filesystem was available - the DRM driver can probe that early - and the blob
needs to go into the initramfs.

### Rolling your own

`edid-generator` (the `edid.S` / `modeline` toolchain) builds these from a
modeline. Sanity-check any blob before trusting it: both 128-byte blocks must
checksum to zero mod 256, and the first detailed timing descriptor at byte 54
is the preferred mode.

## The dashboard does not care

The layout keys off the browser viewport, not the physical panel, and the
compact three-column layout triggers on any landscape viewport 460px or
shorter. So a panel that turns out to be 1920x480 rather than 1280x400 gets
the same layout with no configuration.
