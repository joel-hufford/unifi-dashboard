# Moving the history database off the SD card

## First: you probably do not need to

Measured on the default 10-second poll, with three hours of retention:

| | |
|---|---|
| Steady-state database size | ~4 MiB |
| Written per commit | ~3.9 KiB |
| Commits per day | 8,640 |
| **Written per day** | **~34 MiB** |
| **Written per year** | **~12 GiB** |

A reputable 32 GB card's rated write endurance is measured in tens of
terabytes, so 12 GiB a year is not what will kill it. The database is also
already tuned to be gentle: WAL journalling with `synchronous=NORMAL`, so
commits are not each forcing an fsync, and old rows are pruned rather than
accumulating.

Two things that genuinely do shorten an SD card's life on a Pi, both bigger
than this application:

- **systemd's journal**, if it is persistent. `sudo journalctl --disk-usage`
  will tell you. Capping it is a one-liner:
  `sudo mkdir -p /etc/systemd/journald.conf.d && printf '[Journal]\nSystemMaxUse=64M\n' | sudo tee /etc/systemd/journald.conf.d/size.conf`
- **A cheap or counterfeit card.** These fail far below spec and corrupt
  rather than degrading gracefully.

So: worth doing if the card is old, of unknown provenance, or if you simply
want the panel to survive a card failure with its history intact. Not worth
doing to save the card from this workload.

## Doing it anyway

### 1. Format the drive as ext4

**Not exFAT or FAT32.** SQLite needs real file locking, and WAL mode needs
shared-memory mapping; on a FAT filesystem the database will misbehave under
concurrent access rather than failing cleanly. This is the step people get
wrong.

```bash
lsblk -o NAME,SIZE,FSTYPE,LABEL,MOUNTPOINT       # identify the drive
sudo umount /dev/sda1 2>/dev/null || true
sudo mkfs.ext4 -L unifi-data /dev/sda1           # DESTROYS the drive's contents
```

Check `lsblk` output carefully before running `mkfs` - naming the wrong device
will erase it.

### 2. Mount it at boot

```bash
sudo blkid /dev/sda1                             # note the UUID
sudo mkdir -p /mnt/unifi-data
```

Add to `/etc/fstab`, by UUID rather than `/dev/sda1`, which is not stable
across reboots or USB port changes:

```
UUID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx  /mnt/unifi-data  ext4  defaults,noatime,nofail,x-systemd.device-timeout=10  0  2
```

`noatime` avoids a write on every read. `nofail` and the device timeout stop a
missing drive from holding up boot - the Pi should still come up headless if
someone unplugs the stick.

```bash
sudo mount -a && df -h /mnt/unifi-data
sudo install -d -o "$USER" -g "$USER" /mnt/unifi-data/history
```

### 3. Let the service write there

The unit sets `ProtectSystem=strict`, so everything outside its
`StateDirectory` is read-only. Grant the new path with a drop-in, which
survives re-running `install.sh`:

```bash
sudo mkdir -p /etc/systemd/system/unifi-dashboard.service.d
sudo cp deploy/usb-storage.conf.example \
        /etc/systemd/system/unifi-dashboard.service.d/storage.conf
sudo systemctl daemon-reload
```

### 4. Point the config at it

In `/etc/unifi-dashboard/config.toml`:

```toml
[history]
db_path = "/mnt/unifi-data/history/history.db"
```

Optionally move the existing history across first, so the graphs do not start
empty:

```bash
sudo systemctl stop unifi-dashboard
sudo cp /var/lib/unifi-dashboard/history.db* /mnt/unifi-data/history/ 2>/dev/null || true
sudo chown -R "$USER" /mnt/unifi-data/history
sudo systemctl start unifi-dashboard
```

### 5. Verify it actually landed there

```bash
ls -la /mnt/unifi-data/history/
journalctl -u unifi-dashboard -n 20
```

A growing `history.db` on the mount is the confirmation. If the service failed
to start, `RequiresMountsFor` did its job and told you the drive was missing -
which is the point of it, rather than quietly falling back to the SD card.
