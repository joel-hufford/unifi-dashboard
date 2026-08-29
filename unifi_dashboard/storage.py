"""Rolling sample history in SQLite.

One row per poll. At the default 10-second cadence an hour is 360 rows, so the
whole window is cheap to read on every request and there is no need for a
downsampling layer. Old rows are pruned on write.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    ts         REAL PRIMARY KEY,
    rx_bps     REAL,
    tx_bps     REAL,
    latency_ms REAL,
    ping_sent  INTEGER NOT NULL DEFAULT 0,
    ping_recv  INTEGER NOT NULL DEFAULT 0,
    clients    INTEGER,
    wlan_score REAL,
    wan_up     INTEGER NOT NULL DEFAULT 0,
    dns_ok     INTEGER,
    dns_ms     REAL
);
CREATE INDEX IF NOT EXISTS samples_ts ON samples (ts);
"""

COLUMNS = (
    "ts", "rx_bps", "tx_bps", "latency_ms", "ping_sent", "ping_recv",
    "clients", "wlan_score", "wan_up", "dns_ok", "dns_ms",
)

# Columns added after the first release. A dashboard that has been running for
# a while has a database without them, and dropping that history to add a
# column would be a poor trade.
ADDED_COLUMNS = (("dns_ok", "INTEGER"), ("dns_ms", "REAL"))


class History:
    def __init__(self, path: Path | str, retention_minutes: int = 180) -> None:
        self.path = Path(path).expanduser()
        self.retention_minutes = retention_minutes
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        # WAL keeps writes from blocking the read that the HTTP handler does,
        # and cuts the write amplification that eats SD cards.
        if str(self.path) != ":memory:":
            self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(SCHEMA)
        self._migrate()
        self._db.commit()

    def _migrate(self) -> None:
        present = {row["name"] for row in self._db.execute("PRAGMA table_info(samples)")}
        for column, kind in ADDED_COLUMNS:
            if column not in present:
                self._db.execute(f"ALTER TABLE samples ADD COLUMN {column} {kind}")

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # -- writing ----------------------------------------------------------

    def record(self, sample: dict) -> None:
        self.record_many([sample])

    def record_many(self, samples: list[dict]) -> None:
        """Insert a batch in one transaction. Backfilling an hour of history one
        commit at a time is slow enough to be noticeable at startup."""
        if not samples:
            return
        rows = [self._normalise(sample) for sample in samples]
        placeholders = ", ".join(f":{c}" for c in COLUMNS)
        with self._lock:
            self._db.executemany(
                f"INSERT OR REPLACE INTO samples ({', '.join(COLUMNS)}) VALUES ({placeholders})", rows
            )
            self._db.execute(
                "DELETE FROM samples WHERE ts < ?", (time.time() - self.retention_minutes * 60,)
            )
            self._db.commit()

    @staticmethod
    def _normalise(sample: dict) -> dict:
        row = {key: sample.get(key) for key in COLUMNS}
        row["ts"] = row["ts"] or time.time()
        row["ping_sent"] = int(row.get("ping_sent") or 0)
        row["ping_recv"] = int(row.get("ping_recv") or 0)
        row["wan_up"] = int(bool(row.get("wan_up")))
        dns_ok = row.get("dns_ok")
        row["dns_ok"] = None if dns_ok is None else int(bool(dns_ok))
        return row

    # -- reading ----------------------------------------------------------

    def window(self, minutes: int, *, now: float | None = None) -> list[dict]:
        cutoff = (now or time.time()) - minutes * 60
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM samples WHERE ts >= ? ORDER BY ts ASC", (cutoff,)
            ).fetchall()
        return [dict(row) for row in rows]

    def summary(self, minutes: int, *, now: float | None = None) -> dict:
        """Window aggregates: the average/max figures the dashboard headlines.

        Averages are over every sample in the window, idle time included - so
        "average download" is genuinely average throughput, not average
        throughput while busy. Max is the peak of a single poll interval.
        """
        cutoff = (now or time.time()) - minutes * 60
        with self._lock:
            row = self._db.execute(
                """
                SELECT COUNT(*)            AS samples,
                       AVG(rx_bps)         AS avg_rx_bps,
                       MAX(rx_bps)         AS max_rx_bps,
                       AVG(tx_bps)         AS avg_tx_bps,
                       MAX(tx_bps)         AS max_tx_bps,
                       AVG(latency_ms)     AS avg_latency_ms,
                       MIN(latency_ms)     AS min_latency_ms,
                       MAX(latency_ms)     AS max_latency_ms,
                       SUM(ping_sent)      AS ping_sent,
                       SUM(ping_recv)      AS ping_recv,
                       SUM(wan_up)         AS wan_up_samples,
                       SUM(dns_ok)         AS dns_ok_samples,
                       COUNT(dns_ok)       AS dns_samples,
                       AVG(dns_ms)         AS avg_dns_ms,
                       MIN(ts)             AS first_ts,
                       MAX(ts)             AS last_ts
                FROM samples WHERE ts >= ?
                """,
                (cutoff,),
            ).fetchone()

        summary = dict(row) if row else {}
        sent = summary.get("ping_sent") or 0
        recv = summary.get("ping_recv") or 0
        summary["packets_sent"] = sent
        summary["packets_lost"] = max(0, sent - recv)
        summary["loss_pct"] = round(100.0 * (sent - recv) / sent, 2) if sent else None

        samples = summary.get("samples") or 0
        up = summary.get("wan_up_samples") or 0
        summary["uptime_pct"] = round(100.0 * up / samples, 2) if samples else None

        dns_samples = summary.get("dns_samples") or 0
        dns_ok = summary.get("dns_ok_samples") or 0
        summary["dns_ok_pct"] = round(100.0 * dns_ok / dns_samples, 2) if dns_samples else None
        summary["dns_failures"] = max(0, dns_samples - dns_ok)
        summary["window_minutes"] = minutes
        return summary
