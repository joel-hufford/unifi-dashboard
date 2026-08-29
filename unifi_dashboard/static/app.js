/* UniFi status dashboard - kiosk front end.
   No build step and no CDN: the Pi has to render this with the WAN down. */

(() => {
  "use strict";

  const SVG_NS = "http://www.w3.org/2000/svg";
  const REFRESH_FLOOR_MS = 4000;
  const PAD = { top: 16, right: 10, bottom: 4, left: 8 };
  const AXIS_BAND = 18;

  const state = {
    minutes: 60,
    selectedWan: null,      // null = follow whichever link is active
    data: null,
    geometry: new Map(),   // panel id -> { x(i), y(v), plot box, count }
    hoverIndex: null,
    timer: null,
    touchTimer: null,
  };

  const $ = (id) => document.getElementById(id);

  /* ---------------------------------------------------------------- format */

  const RATE_UNITS = [
    { limit: 1e9, div: 1e9, unit: "Gbps", digits: 2 },
    { limit: 1e6, div: 1e6, unit: "Mbps", digits: 1 },
    { limit: 1e3, div: 1e3, unit: "kbps", digits: 0 },
    { limit: 0,   div: 1,   unit: "bps",  digits: 0 },
  ];

  // Controller counters are bytes/second; people talk about their internet in
  // bits, so everything on screen is bits.
  const toBits = (bytesPerSecond) => (bytesPerSecond == null ? null : bytesPerSecond * 8);

  function rateUnit(bytesPerSecond) {
    const bits = toBits(bytesPerSecond) || 0;
    return RATE_UNITS.find((u) => bits >= u.limit) || RATE_UNITS[RATE_UNITS.length - 1];
  }

  function rateIn(bytesPerSecond, unit) {
    if (bytesPerSecond == null || !isFinite(bytesPerSecond)) return "—";
    const scaled = (bytesPerSecond * 8) / unit.div;
    const digits = scaled >= 100 ? 0 : unit.digits;
    return scaled.toFixed(digits);
  }

  function rateText(bytesPerSecond) {
    if (bytesPerSecond == null || !isFinite(bytesPerSecond)) return "—";
    const unit = rateUnit(bytesPerSecond);
    return `${rateIn(bytesPerSecond, unit)} ${unit.unit}`;
  }

  const ms = (value, digits = 1) =>
    value == null || !isFinite(value) ? "—" : value.toFixed(digits);

  function duration(seconds) {
    if (seconds == null || !isFinite(seconds) || seconds <= 0) return "—";
    const days = Math.floor(seconds / 86400);
    const hours = Math.floor((seconds % 86400) / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    if (days) return `${days}d ${hours}h`;
    if (hours) return `${hours}h ${minutes}m`;
    return `${minutes}m`;
  }

  const clockText = (date) =>
    date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });

  const num = (value) => (value == null ? "—" : value.toLocaleString());

  /* ------------------------------------------------------------------ data */

  async function refresh() {
    document.body.classList.add("is-refreshing");
    let payload;
    try {
      const response = await fetch(`/api/dashboard?minutes=${state.minutes}`, { cache: "no-store" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      payload = await response.json();
    } catch (error) {
      showBanner("critical", `Dashboard server unreachable (${error.message})`);
      return;
    } finally {
      document.body.classList.remove("is-refreshing");
    }

    // Rendering is a separate failure with a separate cause. Reporting a
    // rendering crash as "server unreachable" sends you to look at the wrong
    // machine entirely.
    try {
      const first = state.data === null;
      state.data = payload;
      if (first && payload.config && payload.config.theme) {
        document.documentElement.dataset.theme = payload.config.theme;
      }
      render();
    } catch (error) {
      console.error("render failed", error);
      showBanner(
        "critical",
        `Display error - try a hard refresh (Ctrl+Shift+R): ${error.message}`,
      );
    }
  }

  function scheduleRefresh() {
    clearInterval(state.timer);
    const interval = Math.max(REFRESH_FLOOR_MS, (state.data?.poll_interval || 10) * 1000);
    state.timer = setInterval(refresh, interval);
  }

  /* ---------------------------------------------------------------- render */

  function render() {
    const data = state.data;
    if (!data) return;
    renderAlarm(data);
    renderWanChips(data);
    renderHero(data);
    renderKpis(data);
    renderCharts(data);
    renderWlan(data);
    if ($("table-toggle").getAttribute("aria-expanded") === "true") renderTable(data);
  }

  function showBanner(status, text) {
    const banner = $("banner");
    banner.hidden = false;
    banner.dataset.status = status;
    banner.textContent = text;
  }

  function renderAlarm(data) {
    const banner = $("banner");
    const frame = $("alarm-frame");
    const alarm = data.alarm || {};
    const level = !data.ok ? (alarm.level || "warning") : (alarm.level || "ok");

    frame.dataset.level = level;

    if (level === "ok") {
      banner.hidden = true;
      return;
    }

    // The headline is the most fundamental failure, not the most recent one.
    const reasons = (alarm.reasons || []).slice();
    if (!data.ok && data.error) {
      const seen = data.generated_at ? clockText(new Date(data.generated_at * 1000)) : "never";
      reasons.push(`last good reading ${seen}`);
    }
    banner.hidden = false;
    banner.dataset.status = level;
    banner.textContent = reasons.join(" · ") || "Something is wrong";
  }

  const LINK_STATE = (link) =>
    !link ? "unknown" : !link.up ? "critical" : link.active ? "ok" : "idle";

  function selectedLink(data) {
    const links = data.wan_links || [];
    if (!links.length) return null;
    if (state.selectedWan) {
      const chosen = links.find((link) => link.key === state.selectedWan);
      if (chosen) return chosen;
    }
    return links.find((link) => link.active) || links[0];
  }

  function renderWanChips(data) {
    const host = $("wan-chips");
    const links = data.wan_links || [];
    const current = selectedLink(data);
    host.replaceChildren();

    if (!links.length) {
      const fallback = document.createElement("span");
      fallback.className = "wan-chip";
      const lamp = document.createElement("span");
      lamp.className = "lamp";
      lamp.dataset.state = data.wan?.online ? "ok" : "critical";
      const label = document.createElement("span");
      label.textContent = "WAN";
      fallback.append(lamp, label);
      host.appendChild(fallback);
      return;
    }

    for (const link of links) {
      const chip = document.createElement("button");
      chip.type = "button";
      chip.className = "wan-chip";
      chip.setAttribute("aria-pressed", String(current != null && link.key === current.key));

      const lamp = document.createElement("span");
      lamp.className = "lamp";
      lamp.dataset.state = LINK_STATE(link);

      const label = document.createElement("span");
      label.textContent = link.label || link.key.toUpperCase();

      const note = document.createElement("span");
      note.className = "chip-note";
      note.textContent = !link.up ? "down" : link.active ? "active" : "standby";

      chip.append(lamp, label, note);
      chip.addEventListener("click", () => {
        state.selectedWan = link.key;
        render();
      });
      host.appendChild(chip);
    }
  }

  function renderHero(data) {
    const link = selectedLink(data);
    const wan = data.wan || {};
    const win = data.window || {};
    const dns = data.dns || {};
    const viewingActive = !link || link.active;

    $("hero-label").textContent = link ? (link.label || link.key.toUpperCase()) : "WAN";
    const badge = $("hero-badge");
    badge.hidden = !(link && link.cellular);
    // The radio technology is more informative than the word "cellular", and
    // the gateway reports it.
    badge.textContent = (link && link.rat) || "Cellular";

    const lamp = $("hero-lamp");
    lamp.dataset.state = LINK_STATE(link);
    $("hero-state-word").textContent = !link
      ? (wan.online ? "Online" : "Offline")
      : !link.up ? "Offline" : link.active ? "Online" : "Standby";

    $("hero-ip").textContent = (link ? link.ip : wan.ip) || "no address";

    // The ICMP and DNS probes run from the Pi, so they describe whichever link
    // is actually carrying traffic - never a standby one. Say so rather than
    // implying the numbers belong to the link being viewed.
    const netLamp = $("check-net-lamp");
    const dnsLamp = $("check-dns-lamp");
    const lossLamp = $("check-loss-lamp");

    if (!viewingActive) {
      for (const el of [netLamp, dnsLamp, lossLamp]) el.dataset.state = "idle";
      $("check-net-value").textContent = "—";
      $("check-dns-value").textContent = "—";
      $("check-loss-value").textContent = "—";
      $("hero-foot").textContent =
        [standbyDetail(link), "checks below run over the active WAN"].filter(Boolean).join(" · ");
      return;
    }

    netLamp.dataset.state = wan.reachable === false ? "critical" : wan.reachable ? "ok" : "unknown";
    $("check-net-value").textContent =
      wan.reachable === false ? "no reply" : `${ms(wan.latency_ms)} ms`;

    dnsLamp.dataset.state = dns.ok === false ? "critical" : dns.ok ? "ok" : "unknown";
    $("check-dns-value").textContent =
      dns.ok === false ? "failing" : dns.elapsed_ms == null ? "—" : `${ms(dns.elapsed_ms, 0)} ms`;

    const loss = win.loss_pct;
    lossLamp.dataset.state = lossStatus(loss) === "good" ? "ok" : lossStatus(loss);
    $("check-loss-value").textContent = loss == null ? "—" : `${loss.toFixed(loss >= 10 ? 0 : 1)}%`;

    const parts = [`uptime ${duration(link ? link.uptime_s : wan.uptime_s)}`];
    if (link && link.isp) parts.push(link.isp);
    if (link && link.signal_pct != null) parts.push(`${link.rat || "signal"} ${Math.round(link.signal_pct)}%`);
    parts.push(`${wan.ping_target || "8.8.8.8"} · ${dns.host || "dns"}`);
    $("hero-foot").textContent = parts.join(" · ");
  }

  function standbyDetail(link) {
    const bits = ["Standby link"];
    if (link && link.signal_pct != null) bits.push(`${link.rat || "signal"} ${Math.round(link.signal_pct)}%`);
    if (link && link.ip) bits.push(link.ip);
    return bits.join(" · ");
  }

  function renderKpis(data) {
    const clients = data.clients || {};
    const wlan = data.wlan || {};

    $("kpi-clients").textContent = num(clients.total);
    $("kpi-clients-sub").textContent =
      `${num(clients.wireless)} Wi-Fi · ${num(clients.wired)} wired${clients.guest ? ` · ${clients.guest} guest` : ""}`;

    const score = wlan.score;
    const band = qualityBand(score);
    $("kpi-wlan").textContent = score == null ? "—" : Math.round(score);
    $("kpi-wlan-word").textContent = band.label;
    const meter = $("kpi-wlan-meter");
    meter.style.setProperty("--meter-color", `var(--${band.token})`);
    $("kpi-wlan-fill").style.width = `${score == null ? 0 : Math.max(2, Math.min(100, score))}%`;
    meter.setAttribute(
      "aria-label",
      `Wi-Fi quality score ${score == null ? "unknown" : Math.round(score)} out of 100, ${band.label}`,
    );
    $("kpi-wlan-sub").textContent =
      wlan.rated ? `${wlan.weak} weak of ${wlan.rated} clients · mean ${ms(wlan.mean_signal_dbm, 0)} dBm` : "no wireless clients";
  }

  function lossStatus(loss) {
    if (loss == null) return "good";
    if (loss >= 5) return "critical";
    if (loss >= 1) return "serious";
    if (loss > 0) return "warning";
    return "good";
  }

  function qualityBand(score) {
    if (score == null) return { label: "unknown", token: "text-muted" };
    if (score >= 70) return { label: "good", token: "good" };
    if (score >= 45) return { label: "fair", token: "warning" };
    return { label: "poor", token: "critical" };
  }

  /* ---------------------------------------------------------------- charts */

  function renderCharts(data) {
    const series = data.series || { ts: [] };
    const win = data.window || {};
    const wan = data.wan || {};

    $("charts-sub").textContent = windowLabel(state.minutes);

    const downUnit = rateUnit(Math.max(win.max_rx_bps || 0, wan.rx_bps || 0));
    $("hero-down").textContent = rateIn(wan.rx_bps, RATE_UNITS[1]);

    $("stats-down").textContent =
      `avg ${rateIn(win.avg_rx_bps, downUnit)} · max ${rateIn(win.max_rx_bps, downUnit)} ${downUnit.unit}`;
    const upUnit = rateUnit(Math.max(win.max_tx_bps || 0, wan.tx_bps || 0));
    $("stats-up").textContent =
      `avg ${rateIn(win.avg_tx_bps, upUnit)} · max ${rateIn(win.max_tx_bps, upUnit)} ${upUnit.unit}`;
    $("stats-latency").textContent =
      `avg ${ms(win.avg_latency_ms)} · max ${ms(win.max_latency_ms)} ms · ${win.loss_pct == null ? "—" : win.loss_pct.toFixed(1)}% loss`;

    const scale = data.config?.throughput_scale || "log";
    const decades = data.config?.log_decades || 3;

    drawPanel("plot-down", {
      ts: series.ts,
      values: series.rx_bps,
      color: "var(--series-down)",
      format: (v) => rateIn(v, downUnit),
      unitLabel: downUnit.unit,
      scale,
      decades,
    });
    drawPanel("plot-up", {
      ts: series.ts,
      values: series.tx_bps,
      color: "var(--series-up)",
      format: (v) => rateIn(v, upUnit),
      unitLabel: upUnit.unit,
      scale,
      decades,
    });
    drawPanel("plot-latency", {
      ts: series.ts,
      values: series.latency_ms,
      color: "var(--series-latency)",
      format: (v) => ms(v, v >= 100 ? 0 : 1),
      unitLabel: "ms",
      loss: series.loss_pct,
      axis: true,
    });
  }

  const windowLabel = (minutes) =>
    minutes >= 60 ? `last ${minutes / 60} hour${minutes === 60 ? "" : "s"}` : `last ${minutes} minutes`;

  // A fine-grained ladder, so the plot is not squashed into the bottom third
  // whenever the peak lands just above a round number.
  const NICE_STEPS = [1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10];

  function niceCeiling(value) {
    if (!isFinite(value) || value <= 0) return 1;
    const magnitude = Math.pow(10, Math.floor(Math.log10(value)));
    const scaled = value / magnitude;
    const step = NICE_STEPS.find((candidate) => scaled <= candidate + 1e-9) || 10;
    return step * magnitude;
  }

  function drawPanel(id, opts) {
    const host = $(id);
    const width = host.clientWidth;
    const height = host.clientHeight;
    host.replaceChildren();
    if (width < 40 || height < 30) return;

    const svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
    svg.setAttribute("preserveAspectRatio", "none");
    svg.setAttribute("role", "img");

    const bottom = PAD.bottom + (opts.axis ? AXIS_BAND : 0);
    const plot = {
      left: PAD.left,
      right: width - PAD.right,
      top: PAD.top,
      bottom: height - bottom,
    };
    plot.width = plot.right - plot.left;
    plot.height = plot.bottom - plot.top;
    if (plot.width <= 0 || plot.height <= 0) return;

    const values = opts.values || [];
    const count = values.length;
    const finite = values.filter((v) => v != null && isFinite(v));
    const peak = finite.length ? Math.max(...finite) : 0;
    const top = niceCeiling(peak * 1.12) || 1;

    // A WAN that is idle most of the time with rare bursts has a dynamic
    // range a linear axis cannot show: the line flattens onto the baseline and
    // one spike owns the scale. A log axis spanning a few decades keeps
    // ordinary traffic legible without clipping the peak.
    const useLog = opts.scale === "log" && peak > 0;
    const floor = useLog ? top / Math.pow(10, opts.decades || 3) : 0;

    const x = (i) => (count <= 1 ? plot.left : plot.left + (i / (count - 1)) * plot.width);
    const y = (v) => {
      if (!useLog) return plot.bottom - Math.max(0, Math.min(1, v / top)) * plot.height;
      if (v == null || !isFinite(v) || v <= floor) return plot.bottom;
      return plot.bottom - Math.min(1, Math.log(v / floor) / Math.log(top / floor)) * plot.height;
    };

    // Recessive chrome: solid hairlines, one step off the surface. On a log
    // axis they mark the decades, which is what makes it readable as one.
    const gridValues = [];
    if (useLog) {
      for (let value = top; value > floor * 1.0001; value /= 10) gridValues.push(y(value));
    } else {
      gridValues.push(plot.top, plot.top + 0.5 * plot.height);
    }
    for (const yy of gridValues) {
      const line = document.createElementNS(SVG_NS, "line");
      line.setAttribute("x1", plot.left);
      line.setAttribute("x2", plot.right);
      line.setAttribute("y1", yy);
      line.setAttribute("y2", yy);
      line.setAttribute("class", "grid-line");
      svg.appendChild(line);
    }

    // Area wash + line, split into subpaths so gaps stay gaps.
    const segments = [];
    let current = [];
    values.forEach((value, index) => {
      if (value == null || !isFinite(value)) {
        if (current.length) segments.push(current);
        current = [];
      } else {
        current.push([x(index), y(value)]);
      }
    });
    if (current.length) segments.push(current);

    for (const points of segments) {
      if (points.length > 1 && !useLog) {
        const area = document.createElementNS(SVG_NS, "path");
        const d =
          `M ${points[0][0]} ${plot.bottom} ` +
          points.map(([px, py]) => `L ${px} ${py}`).join(" ") +
          ` L ${points[points.length - 1][0]} ${plot.bottom} Z`;
        area.setAttribute("d", d);
        area.setAttribute("class", "series-area");
        area.setAttribute("fill", opts.color);
        svg.appendChild(area);
      }
      const line = document.createElementNS(SVG_NS, "path");
      const d = points.map(([px, py], i) => `${i ? "L" : "M"} ${px} ${py}`).join(" ");
      line.setAttribute("d", d);
      line.setAttribute("class", "series-line");
      line.setAttribute("stroke", opts.color);
      svg.appendChild(line);
    }

    // Baseline last, so it sits over the area wash.
    const baseline = document.createElementNS(SVG_NS, "line");
    baseline.setAttribute("x1", plot.left);
    baseline.setAttribute("x2", plot.right);
    baseline.setAttribute("y1", plot.bottom);
    baseline.setAttribute("y2", plot.bottom);
    baseline.setAttribute("class", "axis-line");
    svg.appendChild(baseline);

    // Packet loss rides the latency panel's baseline as a red tick, with its
    // own key in the panel header - never colour alone.
    if (opts.loss) {
      opts.loss.forEach((loss, index) => {
        if (!loss) return;
        const tick = document.createElementNS(SVG_NS, "line");
        tick.setAttribute("x1", x(index));
        tick.setAttribute("x2", x(index));
        tick.setAttribute("y1", plot.bottom);
        tick.setAttribute("y2", plot.bottom - Math.max(5, (loss / 100) * 14));
        tick.setAttribute("class", "loss-tick");
        svg.appendChild(tick);
      });
    }

    // The scale top, labelled directly - so no value depends on the tooltip.
    // Saying "log" matters: an unlabelled log axis misleads about magnitude.
    const topLabel = `${opts.format(top)} ${opts.unitLabel}${useLog ? " · log" : ""}`;
    label(svg, plot.left + 2, plot.top - 4, topLabel, "start");

    // One selective direct label: the window peak.
    const peakIndex = values.reduce(
      (best, value, index) => (value != null && isFinite(value) && (best < 0 || value > values[best]) ? index : best),
      -1,
    );
    if (peakIndex >= 0 && peak > 0) {
      const px = x(peakIndex);
      const py = y(values[peakIndex]);
      const dot = document.createElementNS(SVG_NS, "circle");
      dot.setAttribute("cx", px);
      dot.setAttribute("cy", py);
      dot.setAttribute("r", 4);
      dot.setAttribute("class", "peak-dot");
      dot.setAttribute("stroke", opts.color);
      svg.appendChild(dot);
      const anchor = px > plot.right - 60 ? "end" : "start";
      const offset = anchor === "end" ? -8 : 8;
      label(svg, px + offset, Math.max(plot.top + 9, py - 7), `peak ${opts.format(values[peakIndex])}`, anchor);
    }

    // Current value: an end dot with a surface ring so it stays legible.
    const lastIndex = values.length - 1;
    if (lastIndex >= 0 && values[lastIndex] != null && isFinite(values[lastIndex])) {
      const dot = document.createElementNS(SVG_NS, "circle");
      dot.setAttribute("cx", x(lastIndex));
      dot.setAttribute("cy", y(values[lastIndex]));
      dot.setAttribute("r", 4);
      dot.setAttribute("fill", opts.color);
      dot.setAttribute("class", "end-dot");
      svg.appendChild(dot);
    }

    if (opts.axis && opts.ts && opts.ts.length > 1) {
      const ticks = 4;
      for (let i = 0; i <= ticks; i += 1) {
        const index = Math.round((i / ticks) * (opts.ts.length - 1));
        const anchor = i === 0 ? "start" : i === ticks ? "end" : "middle";
        label(
          svg,
          x(index),
          plot.bottom + AXIS_BAND - 4,
          clockText(new Date(opts.ts[index] * 1000)),
          anchor,
          "axis-text",
        );
      }
    }

    const crosshair = document.createElementNS(SVG_NS, "line");
    crosshair.setAttribute("class", "crosshair");
    crosshair.setAttribute("y1", plot.top);
    crosshair.setAttribute("y2", plot.bottom);
    crosshair.setAttribute("visibility", "hidden");
    svg.appendChild(crosshair);

    host.appendChild(svg);
    state.geometry.set(id, { plot, count, x, crosshair });
  }

  function label(svg, x, y, text, anchor = "start", className = "value-label") {
    const node = document.createElementNS(SVG_NS, "text");
    node.setAttribute("x", x);
    node.setAttribute("y", y);
    node.setAttribute("text-anchor", anchor);
    node.setAttribute("class", className);
    node.textContent = text;                 // series/axis text is data: never innerHTML
    svg.appendChild(node);
    return node;
  }

  // The panels are sized by flex inside a grid, so their real height is not
  // known during the first synchronous render - the SVGs would be drawn a few
  // pixels tall and thrown away. Redraw whenever the box actually changes.
  function observePanels() {
    if (!window.ResizeObserver) return;
    const host = $("panels");
    let last = { width: 0, height: 0 };
    const observer = new ResizeObserver(() => {
      const width = host.clientWidth;
      const height = host.clientHeight;
      if (Math.abs(width - last.width) < 2 && Math.abs(height - last.height) < 2) return;
      last = { width, height };
      if (state.data) renderCharts(state.data);
    });
    observer.observe(host);
  }

  /* ------------------------------------------------------------- crosshair */

  function indexFromPointer(clientX) {
    const geometry = state.geometry.get("plot-latency");
    if (!geometry || geometry.count < 2) return null;
    const box = $("plot-latency").getBoundingClientRect();
    const ratio = (clientX - box.left - geometry.plot.left) / geometry.plot.width;
    return Math.max(0, Math.min(geometry.count - 1, Math.round(ratio * (geometry.count - 1))));
  }

  function moveCrosshair(event) {
    const index = indexFromPointer(event.clientX);
    if (index == null) return;
    state.hoverIndex = index;
    for (const [id, geometry] of state.geometry) {
      if (!geometry.crosshair) continue;
      const x = geometry.x(index);
      geometry.crosshair.setAttribute("x1", x);
      geometry.crosshair.setAttribute("x2", x);
      geometry.crosshair.setAttribute("visibility", "visible");
      void id;
    }
    showTooltip(index, event);
  }

  function hideCrosshair() {
    state.hoverIndex = null;
    for (const [, geometry] of state.geometry) {
      geometry.crosshair?.setAttribute("visibility", "hidden");
    }
    $("tooltip").hidden = true;
  }

  function showTooltip(index, event) {
    const data = state.data;
    const tooltip = $("tooltip");
    if (!data) return;
    const series = data.series || {};
    const stamp = series.ts?.[index];
    tooltip.replaceChildren();

    const time = document.createElement("div");
    time.className = "tooltip-time";
    time.textContent = stamp ? new Date(stamp * 1000).toLocaleTimeString([], { hour12: false }) : "—";
    tooltip.appendChild(time);

    const rows = [
      ["Download", rateText(series.rx_bps?.[index]), "var(--series-down)"],
      ["Upload", rateText(series.tx_bps?.[index]), "var(--series-up)"],
      ["Latency", series.latency_ms?.[index] == null ? "no reply" : `${ms(series.latency_ms[index])} ms`, "var(--series-latency)"],
    ];
    const loss = series.loss_pct?.[index];
    if (loss) rows.push(["Packet loss", `${loss.toFixed(0)}%`, "var(--critical)"]);

    for (const [name, value, color] of rows) {
      const row = document.createElement("div");
      row.className = "tooltip-row";
      const key = document.createElement("span");
      key.className = "key";
      key.style.background = color;
      const label = document.createElement("span");
      label.className = "tooltip-name";
      label.textContent = name;
      const figure = document.createElement("span");
      figure.className = "tooltip-value";
      figure.textContent = value;
      row.append(key, label, figure);
      tooltip.appendChild(row);
    }

    tooltip.hidden = false;
    const panels = $("panels").getBoundingClientRect();
    const width = tooltip.offsetWidth;
    let left = event.clientX - panels.left + 14;
    if (left + width > panels.width) left = event.clientX - panels.left - width - 14;
    tooltip.style.left = `${Math.max(0, left)}px`;
    tooltip.style.top = `${Math.max(0, event.clientY - panels.top - tooltip.offsetHeight - 12)}px`;
  }

  /* ----------------------------------------------------------------- Wi-Fi */

  const BAND_TOKENS = { good: "good", fair: "warning", poor: "critical" };
  const BAND_TEXT = {
    good: "Good (70-100)",
    fair: "Fair (45-69)",
    poor: "Poor (0-44)",
  };

  function renderWlan(data) {
    const wlan = data.wlan || {};
    const devices = data.devices || {};
    const histogram = wlan.histogram || {};
    const total = Object.values(histogram).reduce((sum, n) => sum + n, 0);

    $("wlan-sub").textContent = wlan.rated
      ? `${wlan.rated} wireless clients · score is the controller's satisfaction rating, or signal strength where it has none`
      : "no wireless clients connected";

    const dist = $("dist");
    dist.replaceChildren();
    for (const key of ["good", "fair", "poor"]) {
      const count = histogram[key] || 0;
      if (!count) continue;
      const segment = document.createElement("span");
      segment.style.flex = `${count} 0 0`;
      segment.style.background = `var(--${BAND_TOKENS[key]})`;
      dist.appendChild(segment);
    }
    dist.setAttribute(
      "aria-label",
      total ? `Clients by connection quality: ${["good", "fair", "poor"].map((k) => `${histogram[k] || 0} ${k}`).join(", ")}` : "No wireless clients",
    );

    const legend = $("dist-legend");
    legend.replaceChildren();
    for (const key of ["good", "fair", "poor"]) {
      const item = document.createElement("li");
      const swatch = document.createElement("span");
      swatch.className = "swatch";
      swatch.style.background = `var(--${BAND_TOKENS[key]})`;
      const text = document.createElement("span");
      text.textContent = BAND_TEXT[key];
      const count = document.createElement("span");
      count.className = "count";
      count.textContent = histogram[key] || 0;
      item.append(swatch, text, count);
      legend.appendChild(item);
    }

    const list = $("client-list");
    list.replaceChildren();
    for (const client of wlan.worst || []) {
      const item = document.createElement("li");
      const mark = document.createElement("span");
      mark.className = "status-mark";
      mark.dataset.status = BAND_TOKENS[qualityBand(client.score).label] || "good";
      const name = document.createElement("span");
      name.className = "client-name";
      name.textContent = client.name || client.mac;
      const meta = document.createElement("span");
      meta.className = "client-meta";
      meta.textContent = [client.band, client.ap].filter(Boolean).join(" · ");
      const signal = document.createElement("span");
      signal.className = "client-signal";
      signal.textContent = client.signal_dbm == null ? `${Math.round(client.score)}` : `${Math.round(client.signal_dbm)} dBm`;
      item.append(mark, name, meta, signal);
      list.appendChild(item);
    }
    if (!list.childElementCount) {
      const empty = document.createElement("li");
      empty.className = "client-meta";
      empty.textContent = "Nothing to report — every client is above the weak threshold.";
      list.appendChild(empty);
    }

    const bandList = $("band-list");
    bandList.replaceChildren();
    const bands = wlan.bands || {};
    const busiest = Math.max(1, ...Object.values(bands));
    for (const [name, count] of Object.entries(bands).sort((a, b) => b[1] - a[1])) {
      const item = document.createElement("li");
      const label = document.createElement("span");
      label.className = "band-name";
      label.textContent = name;
      const track = document.createElement("span");
      track.className = "band-track";
      const bar = document.createElement("span");
      bar.className = "band-bar";
      bar.style.display = "block";
      bar.style.width = `${(count / busiest) * 100}%`;
      track.appendChild(bar);
      const value = document.createElement("span");
      value.className = "band-count";
      value.textContent = count;
      item.append(label, track, value);
      bandList.appendChild(item);
    }

    const gear =
      `${num(devices.online)} of ${num(devices.total)} UniFi devices online` +
      (devices.offline ? ` · ${devices.offline} offline` : "") +
      (devices.upgradable ? ` · ${devices.upgradable} update available` : "");
    $("gear").textContent = gear;
    // Shown only on bar displays, where the Wi-Fi card (and its footer) is
    // dropped for want of vertical room.
    $("kpi-gear").textContent = gear;
  }

  /* ----------------------------------------------------------------- table */

  function renderTable(data) {
    const body = document.querySelector("#data-table tbody");
    body.replaceChildren();
    const series = data.series || { ts: [] };
    const buckets = new Map();
    const bucketSeconds = 300;

    (series.ts || []).forEach((stamp, index) => {
      const key = Math.floor(stamp / bucketSeconds) * bucketSeconds;
      if (!buckets.has(key)) buckets.set(key, { rx: [], tx: [], latency: [], sent: 0, lost: 0 });
      const bucket = buckets.get(key);
      const rx = series.rx_bps?.[index];
      const tx = series.tx_bps?.[index];
      const latency = series.latency_ms?.[index];
      if (rx != null) bucket.rx.push(rx);
      if (tx != null) bucket.tx.push(tx);
      if (latency != null) bucket.latency.push(latency);
      const loss = series.loss_pct?.[index];
      if (loss != null) {
        bucket.sent += 1;
        bucket.lost += loss / 100;
      }
    });

    const mean = (list) => (list.length ? list.reduce((a, b) => a + b, 0) / list.length : null);
    const peak = (list) => (list.length ? Math.max(...list) : null);

    for (const [key, bucket] of [...buckets.entries()].reverse()) {
      const row = document.createElement("tr");
      const cells = [
        clockText(new Date(key * 1000)),
        rateText(mean(bucket.rx)),
        rateText(peak(bucket.rx)),
        rateText(mean(bucket.tx)),
        rateText(peak(bucket.tx)),
        mean(bucket.latency) == null ? "—" : `${ms(mean(bucket.latency))} ms`,
        bucket.sent ? `${((bucket.lost / bucket.sent) * 100).toFixed(1)}%` : "—",
      ];
      for (const value of cells) {
        const cell = document.createElement("td");
        cell.textContent = value;
        row.appendChild(cell);
      }
      body.appendChild(row);
    }
  }

  /* ------------------------------------------------------------------ init */

  // Deliberately not persisted. On a wall panel the toggle is easy to catch
  // by accident, and a display that quietly stays in the wrong theme until
  // someone notices is worse than one that reverts on the next refresh.
  function setTheme(theme) {
    document.documentElement.dataset.theme = theme;
    render();
  }

  function init() {

    for (const button of document.querySelectorAll(".range button")) {
      button.addEventListener("click", () => {
        state.minutes = Number(button.dataset.minutes);
        for (const other of document.querySelectorAll(".range button")) {
          other.setAttribute("aria-pressed", String(other === button));
        }
        refresh();
      });
    }

    $("theme-toggle").addEventListener("click", () => {
      setTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");
    });

    // The table is the charts' twin view, not a second thing on screen: showing
    // both would squash the plots into unreadable slivers.
    $("table-toggle").addEventListener("click", (event) => {
      const open = event.currentTarget.getAttribute("aria-expanded") !== "true";
      event.currentTarget.setAttribute("aria-expanded", String(open));
      event.currentTarget.textContent = open ? "Charts" : "Table";
      $("table-wrap").hidden = !open;
      $("panels").hidden = open;
      if (open && state.data) renderTable(state.data);
      else if (state.data) renderCharts(state.data);
    });

    const panels = $("panels");
    panels.addEventListener("pointermove", moveCrosshair);
    panels.addEventListener("pointerdown", (event) => {
      moveCrosshair(event);
      clearTimeout(state.touchTimer);
      if (event.pointerType !== "mouse") state.touchTimer = setTimeout(hideCrosshair, 5000);
    });
    panels.addEventListener("pointerleave", hideCrosshair);

    observePanels();

    const clock = () => { $("clock").textContent = clockText(new Date()); };
    clock();
    setInterval(clock, 1000);

    let resizeTimer = null;
    window.addEventListener("resize", () => {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(render, 120);
    });

    refresh().then(scheduleRefresh);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
