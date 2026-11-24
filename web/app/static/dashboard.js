/* BGP Dashboard frontend – plain JavaScript, no external dependencies. */
(function () {
  "use strict";

  const CONFIG = JSON.parse(document.getElementById("config").textContent);
  const API = "/bgp/api/v1.0";
  const POLL_MS = 20000;
  const MAX_ROWS = 1000;

  // ------------------------------------------------------------------ helpers
  const $ = (sel) => document.querySelector(sel);
  const nf = new Intl.NumberFormat("en-US");
  const fmt = (n) => (n === null || n === undefined || Number.isNaN(n) ? "–" : nf.format(n));
  const compact = (n) => {
    const a = Math.abs(n);
    if (a >= 1e6) return (n / 1e6).toFixed(a >= 1e7 ? 0 : 1).replace(/\.0$/, "") + "M";
    if (a >= 1e4) return Math.round(n / 1e3) + "K";
    return nf.format(n);
  };

  /** Create an element. Text is always inserted with textContent (API data is untrusted). */
  function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === null || v === undefined || v === false) continue;
      if (k === "class") node.className = v;
      else if (k === "text") node.textContent = v;
      else if (k === "style") node.style.cssText = v;
      else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, v === true ? "" : v);
    }
    for (const c of children.flat()) {
      if (c === null || c === undefined || c === false) continue;
      node.append(c instanceof Node ? c : document.createTextNode(String(c)));
    }
    return node;
  }
  /** replaceChildren() that skips null/false (optional parts). */
  function fill(node, ...children) {
    node.replaceChildren(...children.flat().filter((c) => c !== null && c !== undefined && c !== false));
  }
  const SVGNS = "http://www.w3.org/2000/svg";
  function svgEl(tag, attrs) {
    const node = document.createElementNS(SVGNS, tag);
    for (const [k, v] of Object.entries(attrs || {})) node.setAttribute(k, v);
    return node;
  }

  async function getJSON(url) {
    const res = await fetch(url, { headers: { Accept: "application/json" } });
    let body = null;
    try { body = await res.json(); } catch (e) { /* ignore */ }
    if (!res.ok) {
      const msg = body && (body.error || (body.detail && JSON.stringify(body.detail)));
      throw new Error(msg || `HTTP ${res.status}`);
    }
    return body;
  }

  function timeAgo(iso) {
    if (!iso) return "never";
    const s = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 1000));
    if (s < 90) return `${s}s ago`;
    if (s < 5400) return `${Math.round(s / 60)} min ago`;
    return `${Math.round(s / 3600)} h ago`;
  }
  const shortTime = (iso) => new Date(iso).toLocaleString(undefined, {
    month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
  });

  // ------------------------------------------------------------------ tooltip
  const tip = $("#tooltip");
  function showTip(evt, value, label) {
    fill(tip, el("strong", { text: value }), el("span", { text: label }));
    tip.hidden = false;
    const r = tip.getBoundingClientRect();
    let x, y;
    if (evt.clientX !== undefined && evt.type !== "focus") { x = evt.clientX + 14; y = evt.clientY + 14; }
    else { const b = evt.target.getBoundingClientRect(); x = b.left + b.width / 2; y = b.top - r.height - 8; }
    if (x + r.width > window.innerWidth - 8) x = window.innerWidth - r.width - 8;
    if (y + r.height > window.innerHeight - 8) y = y - r.height - 28;
    tip.style.left = Math.max(8, x) + "px";
    tip.style.top = Math.max(8, y) + "px";
  }
  const hideTip = () => { tip.hidden = true; };

  // ------------------------------------------------------------------ theme
  $("#themeToggle").addEventListener("click", () => {
    const root = document.documentElement;
    const dark = root.getAttribute("data-theme") === "dark" ||
      (!root.getAttribute("data-theme") && matchMedia("(prefers-color-scheme: dark)").matches);
    const next = dark ? "light" : "dark";
    root.setAttribute("data-theme", next);
    localStorage.setItem("bgp-theme", next);
  });

  // ------------------------------------------------------------------ stat tiles
  const TILES = [
    { key: "peer_count", label: "Peer ASNs", sub: "Active next-hop ASNs" },
    { key: "ipv4_table_size", label: "IPv4 prefixes", sub: "IPv4 BGP table size" },
    { key: "ipv6_table_size", label: "IPv6 prefixes", sub: "IPv6 BGP table size" },
    { key: "nexthop_ip_count", label: "Next-hop addresses", sub: "Unique egress IPs" },
    { key: "avg_as_path_length", label: "Average AS path length", sub: "Prepends ignored", decimals: 2 },
    ...(CONFIG.customersEnabled ? [
      { key: "customer_ipv4_prefixes", label: "Customer IPv4 prefixes", sub: "Originated by customers" },
      { key: "customer_ipv6_prefixes", label: "Customer IPv6 prefixes", sub: "Originated by customers" },
      { key: "customer_count", label: "BGP customers", sub: `Tagged ${CONFIG.customerCommunity}` },
    ] : [
      { key: "own_ipv4_prefixes", label: `Own IPv4 prefixes`, sub: `Originated in AS${CONFIG.asn}` },
      { key: "own_ipv6_prefixes", label: `Own IPv6 prefixes`, sub: `Originated in AS${CONFIG.asn}` },
      { key: "route_changes_1h", label: "Route changes", sub: "In the last hour" },
    ]),
  ];
  const tileNodes = {};
  for (const t of TILES) {
    const value = el("div", { class: "value", text: "–" });
    const delta = el("span", { class: "delta" });
    const spark = el("div", { class: "spark" });
    const node = el("div", { class: "card tile loading" },
      el("div", { class: "label", text: t.label }), value,
      el("div", { class: "sub" }, el("span", { text: t.sub }), delta), spark);
    $("#tiles").append(node);
    tileNodes[t.key] = { node, value, delta, spark, def: t };
  }
  const fmtTile = (t, v) => (t.decimals ? Number(v).toFixed(t.decimals) : fmt(v));

  function renderTiles(stats, history) {
    for (const key of Object.keys(tileNodes)) {
      const { node, value, delta, spark, def } = tileNodes[key];
      node.classList.toggle("loading", !stats.ready);
      value.textContent = stats.ready ? fmtTile(def, stats[key]) : "–";
      const series = history.map((h) => ({ t: h.ts, v: h[key] })).filter((p) => p.v !== null && p.v !== undefined);
      if (series.length > 1) {
        const diff = series[series.length - 1].v - series[0].v;
        const d = def.decimals ? Math.abs(diff).toFixed(def.decimals) : fmt(Math.abs(diff));
        delta.textContent = diff === 0 ? `±0 in ${rangeLabel()}` : `${diff > 0 ? "▲ +" : "▼ −"}${d} in ${rangeLabel()}`;
      } else {
        delta.textContent = "";
      }
      sparkline(spark, series, def);
    }
  }

  function sparkline(container, series, def) {
    fill(container, );
    if (series.length < 2) {
      container.append(el("div", { class: "muted small", text: "Collecting trend data…" }));
      return;
    }
    const W = Math.max(120, container.clientWidth || 240), H = 44, PAD = 5;
    const vs = series.map((p) => p.v);
    let min = Math.min(...vs), max = Math.max(...vs);
    if (min === max) { min -= 1; max += 1; }
    const x = (i) => PAD + (i / (series.length - 1)) * (W - 2 * PAD);
    const y = (v) => PAD + (1 - (v - min) / (max - min)) * (H - 2 * PAD);
    const pts = series.map((p, i) => `${x(i).toFixed(1)},${y(p.v).toFixed(1)}`);
    const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, height: H, role: "img",
      "aria-label": `${def.label} trend, from ${fmtTile(def, vs[0])} to ${fmtTile(def, vs[vs.length - 1])}` });
    svg.append(svgEl("path", { class: "area", d: `M${x(0)},${H} L${pts.join(" L")} L${x(series.length - 1)},${H} Z` }));
    svg.append(svgEl("polyline", { class: "line", points: pts.join(" ") }));
    const last = series.length - 1;
    svg.append(svgEl("circle", { class: "enddot", cx: x(last), cy: y(vs[last]), r: 4 }));
    const cross = svgEl("line", { class: "crosshair", y1: 0, y2: H, visibility: "hidden" });
    const dot = svgEl("circle", { class: "enddot", r: 4, visibility: "hidden" });
    svg.append(cross, dot);
    const hit = svgEl("rect", { class: "hit", x: 0, y: 0, width: W, height: H });
    svg.append(hit);
    hit.addEventListener("pointermove", (evt) => {
      const box = svg.getBoundingClientRect();
      const rel = ((evt.clientX - box.left) / box.width) * W;
      const i = Math.min(last, Math.max(0, Math.round(((rel - PAD) / (W - 2 * PAD)) * last)));
      cross.setAttribute("x1", x(i)); cross.setAttribute("x2", x(i)); cross.setAttribute("visibility", "visible");
      dot.setAttribute("cx", x(i)); dot.setAttribute("cy", y(vs[i])); dot.setAttribute("visibility", "visible");
      showTip(evt, fmtTile(def, vs[i]), shortTime(series[i].t));
    });
    hit.addEventListener("pointerleave", () => {
      cross.setAttribute("visibility", "hidden"); dot.setAttribute("visibility", "hidden"); hideTip();
    });
    container.append(svg);
  }

  // ------------------------------------------------------------------ sortable tables
  function sortableTable(table, columns, rowFn, filterInput) {
    const state = { rows: [], key: columns.defaultKey, dir: columns.defaultDir || 1 };
    const tbody = table.querySelector("tbody");
    const ths = table.querySelectorAll("th[data-key]");
    ths.forEach((th) => th.addEventListener("click", () => {
      const key = th.dataset.key;
      state.dir = state.key === key ? -state.dir : (th.classList.contains("num") ? -1 : 1);
      state.key = key;
      render();
    }));
    if (filterInput) filterInput.addEventListener("input", () => render());

    function render() {
      ths.forEach((th) => {
        th.classList.toggle("sorted-asc", th.dataset.key === state.key && state.dir === 1);
        th.classList.toggle("sorted-desc", th.dataset.key === state.key && state.dir === -1);
      });
      const q = filterInput ? filterInput.value.trim().toLowerCase() : "";
      let rows = state.rows;
      if (q) rows = rows.filter((r) => Object.values(r).some((v) => String(v ?? "").toLowerCase().includes(q)));
      rows = [...rows].sort((a, b) => {
        const va = a[state.key], vb = b[state.key];
        if (typeof va === "number" && typeof vb === "number") return (va - vb) * state.dir;
        return String(va ?? "").localeCompare(String(vb ?? ""), undefined, { numeric: true }) * state.dir;
      });
      const cols = ths.length;
      fill(tbody, ...(rows.length ? rows.slice(0, MAX_ROWS).map(rowFn)
        : [el("tr", {}, el("td", { class: "empty", colspan: cols, text: state.rows.length ? "No match" : "No data yet" }))]));
    }
    return { set(rows) { state.rows = rows || []; render(); } };
  }

  const lookupLink = (label, query, title) =>
    el("a", { href: `#lookup=${encodeURIComponent(query)}`, title: title || `Look up ${query}`, text: label });
  const listLink = (value, asn, kind, version) => el("a", {
    href: `#list=${asn}/${kind}${version ? "/" + version : ""}`, text: fmt(value),
  });

  const peerTable = sortableTable($("#peerTable"), { defaultKey: "ipv4_nexthop_count", defaultDir: -1 }, (p) => el("tr", {},
    el("td", { class: "num" }, lookupLink(String(p.asn), `AS${p.asn}`)),
    el("td", { text: p.name }),
    el("td", { class: "num" }, listLink(p.ipv4_origin_count, p.asn, "originated", "ipv4")),
    el("td", { class: "num" }, listLink(p.ipv4_nexthop_count, p.asn, "nexthop", "ipv4")),
    el("td", { class: "num" }, listLink(p.ipv6_origin_count, p.asn, "originated", "ipv6")),
    el("td", { class: "num" }, listLink(p.ipv6_nexthop_count, p.asn, "nexthop", "ipv6")),
    el("td", { class: "num" }, el("a", { href: `#downstream=${p.asn}`, text: fmt(p.asn_count) })),
  ), $("#peerFilter"));

  const communityTable = sortableTable($("#communityTable"), { defaultKey: "count", defaultDir: -1 }, (c) => el("tr", {},
    el("td", { text: c.community }), el("td", { text: c.name || "" }), el("td", { class: "num", text: fmt(c.count) }),
  ), $("#communityFilter"));

  // ------------------------------------------------------------------ bar charts
  function hbars(container, items, emptyText) {
    if (!items.length) { fill(container, el("div", { class: "empty", text: emptyText })); return; }
    const max = Math.max(...items.map((i) => i.value)) || 1;
    fill(container, ...items.map((i) => {
      const row = el("div", { class: "hbar", tabindex: 0 },
        el("div", { class: "name" }, i.link || i.label, i.note ? el("small", { text: i.note }) : null),
        el("div", { class: "val", text: fmt(i.value) }),
        el("div", { class: "track" }, el("div", { class: "bar", style: `width:${(i.value / max) * 100}%` })));
      return row;
    }));
  }

  function niceTicks(max, count = 4) {
    if (max <= 0) return [0, 1];
    const raw = max / count;
    const mag = Math.pow(10, Math.floor(Math.log10(raw)));
    const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw);
    const ticks = [];
    for (let v = 0; v <= max + step * 0.999; v += step) ticks.push(Math.round(v));
    return ticks;
  }

  /** Column chart: one series, 4px rounded data end, max 24px wide, value on the tallest column. */
  function columnChart(container, data, label) {
    fill(container, );
    if (!data.length) { container.append(el("div", { class: "empty", text: "No prefixes yet" })); return; }
    const W = Math.max(300, container.clientWidth || 500), H = 260;
    const M = { top: 22, right: 8, bottom: 26, left: 48 };
    const pw = W - M.left - M.right, ph = H - M.top - M.bottom;
    const ticks = niceTicks(Math.max(...data.map((d) => d.count)));
    const ymax = ticks[ticks.length - 1];
    const y = (v) => M.top + ph - (v / ymax) * ph;
    const band = pw / data.length;
    const bw = Math.max(2, Math.min(24, band - 2));
    const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, height: H, role: "img", "aria-label": label });
    const tickFmt = ymax >= 10000 ? compact : fmt;
    for (const t of ticks) {
      svg.append(svgEl("line", { class: t === 0 ? "baseline" : "grid", x1: M.left, x2: W - M.right, y1: y(t), y2: y(t) }));
      const tx = svgEl("text", { class: "tick", x: M.left - 6, y: y(t) + 4, "text-anchor": "end" });
      tx.textContent = tickFmt(t);
      svg.append(tx);
    }
    const every = Math.ceil(data.length / Math.max(1, Math.floor(pw / 30)));
    const maxIdx = data.reduce((m, d, i) => (d.count > data[m].count ? i : m), 0);
    data.forEach((d, i) => {
      const cx = M.left + band * i + band / 2;
      const top = y(d.count), base = y(0), h = base - top;
      const r = Math.min(4, h, bw / 2);
      const x0 = cx - bw / 2, x1 = cx + bw / 2;
      const path = h <= 0 ? "" : `M${x0},${base} L${x0},${top + r} Q${x0},${top} ${x0 + r},${top} L${x1 - r},${top} Q${x1},${top} ${x1},${top + r} L${x1},${base} Z`;
      const col = path ? svgEl("path", { class: "col", d: path }) : null;
      if (col) svg.append(col);
      if (i % every === 0) {
        const tx = svgEl("text", { class: "tick", x: cx, y: H - 8, "text-anchor": "middle" });
        tx.textContent = `/${d.mask}`;
        svg.append(tx);
      }
      if (i === maxIdx) {
        const tx = svgEl("text", { class: "vlabel", x: cx, y: top - 6, "text-anchor": "middle" });
        tx.textContent = compact(d.count);
        svg.append(tx);
      }
      const hit = svgEl("rect", { class: "hit", x: M.left + band * i, y: M.top, width: band, height: ph, tabindex: 0 });
      const enter = (evt) => { if (col) col.classList.add("hover"); showTip(evt, fmt(d.count) + " prefixes", `/${d.mask}`); };
      const leave = () => { if (col) col.classList.remove("hover"); hideTip(); };
      hit.addEventListener("pointermove", enter);
      hit.addEventListener("focus", enter);
      hit.addEventListener("pointerleave", leave);
      hit.addEventListener("blur", leave);
      svg.append(hit);
    });
    container.append(svg);
  }

  function transitShare(container, stats) {
    const total = stats.ipv4_table_size + stats.ipv6_table_size;
    const transit = (stats.communities.find((c) => c.community === CONFIG.transitCommunity) || {}).count || 0;
    if (!total) { fill(container, el("div", { class: "empty", text: "No prefixes yet" })); return; }
    const other = Math.max(0, total - transit);
    const parts = [
      { name: "Peering & other", value: other, color: "var(--series-1)" },
      { name: `Transit (${CONFIG.transitCommunity})`, value: transit, color: "var(--series-2)" },
    ];
    const bar = el("div", { class: "share", role: "img",
      "aria-label": parts.map((p) => `${p.name}: ${Math.round((p.value / total) * 100)}%`).join(", ") });
    for (const p of parts) {
      if (!p.value) continue;
      const pct = (p.value / total) * 100;
      const part = el("div", { class: "part", style: `width:${pct}%;background:${p.color}`, tabindex: 0 },
        pct >= 12 ? `${Math.round(pct)}%` : "");
      const enter = (evt) => showTip(evt, `${fmt(p.value)} prefixes · ${pct.toFixed(1)}%`, p.name);
      part.addEventListener("pointermove", enter); part.addEventListener("focus", enter);
      part.addEventListener("pointerleave", hideTip); part.addEventListener("blur", hideTip);
      bar.append(part);
    }
    fill(container, 
      bar,
      el("div", { class: "legend" }, ...parts.map((p) => el("span", { style: `--swatch:${p.color}`, text: p.name }))),
      el("div", { class: "share-numbers" }, ...parts.map((p) => el("div", {},
        el("div", { class: "value", text: fmt(p.value) }),
        el("div", { class: "muted small", text: `${p.name} · ${((p.value / total) * 100).toFixed(1)}%` })))),
    );
  }

  // ------------------------------------------------------------------ monitoring
  function fmtDuration(sec) {
    if (sec === null || sec === undefined) return "–";
    if (sec < 90) return `${sec}s`;
    const m = Math.floor(sec / 60), h = Math.floor(m / 60), d = Math.floor(h / 24);
    if (d > 0) return `${d}d ${h % 24}h`;
    if (h > 0) return `${h}h ${m % 60}m`;
    return `${m}m`;
  }
  /** Status = icon + label + dot, never colour alone. */
  function stateBadge(level, label) {
    const icon = { ok: "✓", err: "✕", warn: "!", muted: "–" }[level] || "";
    return el("span", { class: "state" }, el("span", { class: `dot ${level}`, "aria-hidden": "true" }),
      el("span", { class: "state-icon", "aria-hidden": "true", text: icon }), el("span", { text: label }));
  }
  const STATE_LABELS = { established: "Established", idle: "Idle", connect: "Connect", active: "Active",
    opensent: "OpenSent", openconfirm: "OpenConfirm", unknown: "Unknown" };
  const asLink = (asn) => (asn === null || asn === undefined ? "local" : lookupLink(`AS${asn}`, `AS${asn}`));

  let sessions = null;
  async function refreshSessions() {
    try { sessions = await getJSON(`${API}/neighbors`); }
    catch (e) { sessions = null; }
    renderSessions();
    if ($("#sessionEvents").open) loadSessionEvents();
  }

  function renderSessions() {
    const tbody = $("#sessionTable tbody");
    if (!sessions) { fill(tbody, el("tr", {}, el("td", { colspan: 8, class: "empty", text: "Session state unavailable" }))); return; }
    const sm = sessions.summary;
    $("#sessionSummary").textContent = sm.total ? `${sm.established} of ${sm.total} established` : "";
    $("#sessionPolled").textContent = sessions.stale
      ? `⚠ no update from the collector since ${sessions.polled ? timeAgo(sessions.polled) : "start"}`
      : `polled ${timeAgo(sessions.polled)}`;
    if (!sessions.neighbors.length) {
      fill(tbody, el("tr", {}, el("td", { colspan: 8, class: "empty",
        text: sessions.error ? `Collector cannot read gobgpd: ${sessions.error}` : "No BGP neighbors configured / reported yet" })));
      return;
    }
    const rank = (n) => (n.established ? 2 : n.admin_down ? 1 : 0);
    const rows = [...sessions.neighbors].sort((a, b) => rank(a) - rank(b) || a.address.localeCompare(b.address, undefined, { numeric: true }));
    const fam = (n, f) => {
      const c = n.families[f];
      return c ? `${fmt(c.received)} / ${fmt(c.accepted)}` : "–";
    };
    fill(tbody, ...rows.map((n) => {
      const level = n.established ? "ok" : n.admin_down ? "muted" : "err";
      const label = n.admin_down && !n.established ? "Admin down" : (STATE_LABELS[n.state] || n.state);
      return el("tr", { class: level === "err" ? "problem" : null },
        el("td", {}, stateBadge(sessions.stale ? "warn" : level, label)),
        el("td", {}, n.address, n.description ? el("span", { class: "cell-sub", text: n.description }) : null),
        el("td", {}, asLink(n.peer_asn), n.peer_name && n.peer_name !== "(DNS Error)" ? el("span", { class: "cell-sub", text: n.peer_name }) : null),
        el("td", { title: n.since ? new Date(n.since).toLocaleString() : "" }, n.never_established ? "never up" : `${n.established ? "up" : "down"} ${fmtDuration(n.duration_seconds)}`),
        el("td", { class: "num", text: fmt(n.session_drops) }),
        el("td", { class: "num", text: fam(n, "ipv4") }),
        el("td", { class: "num", text: fam(n, "ipv6") }),
        el("td", { class: "small", text: [n.disconnect_reason, n.disconnect_message].filter(Boolean).join(": ") || "" }));
    }));
  }

  async function loadSessionEvents() {
    const box = $("#sessionEventList");
    try {
      const events = await getJSON(`${API}/neighbors/events?hours=168&limit=100`);
      if (!events.length) { fill(box, el("div", { class: "muted small", text: "No session events in the last 7 days." })); return; }
      fill(box, el("ol", {}, ...events.map((e) => {
        const down = e.to_state !== "established";
        const what = e.from_state
          ? `${STATE_LABELS[e.from_state] || e.from_state} → ${STATE_LABELS[e.to_state] || e.to_state}`
          : `first seen: ${STATE_LABELS[e.to_state] || e.to_state}`;
        return el("li", {},
          el("time", { datetime: e.ts, text: shortTime(e.ts) }),
          el("span", {}, stateBadge(down ? "err" : "ok", `${e.neighbor} (AS${e.peer_asn}${e.description ? ", " + e.description : ""})`),
            el("span", { class: "cell-sub", text: what + (e.reason ? ` – ${e.reason}` : "") + (e.message ? `: ${e.message}` : "") })));
      })));
    } catch (err) { fill(box, el("div", { class: "error", text: err.message })); }
  }
  $("#sessionEvents").addEventListener("toggle", () => { if ($("#sessionEvents").open) loadSessionEvents(); });

  const PROBLEM_LABELS = {
    missing: "Missing", wrong_origin: "Foreign origin", foreign_more_specific: "Foreign more-specific",
    invalid_config: "Invalid config", rpki_invalid: "RPKI invalid", rpki_not_found: "No ROA",
  };
  const RPKI_LABELS = { valid: "RPKI valid", invalid: "RPKI invalid", "not-found": "No ROA (not found)" };
  const RPKI_LEVEL = { valid: "ok", invalid: "err", "not-found": "muted" };
  const RPKI_REASON = { as: "origin AS not authorised", length: "more specific than the ROA allows" };
  function rpkiBadge(r) {
    if (!r) return null;
    const text = RPKI_LABELS[r.state] + (r.reason ? ` – ${RPKI_REASON[r.reason] || r.reason}` : "");
    return stateBadge(RPKI_LEVEL[r.state] || "muted", text);
  }
  const vrpChips = (vrps) => el("div", { class: "chips" }, ...(vrps || []).map((v) =>
    el("span", { class: "chip" }, `${v.prefix} max /${v.max_length}`, el("small", { text: `AS${v.asn}` }))));
  function renderOwnPrefixes(s) {
    const box = $("#ownPrefixes");
    if (!CONFIG.ownPrefixesConfigured) {
      fill(box, el("p", { class: "hint" }, "No own prefixes configured. Add them to ",
        el("code", { text: "config/dashboard.toml" }), " → ", el("code", { text: "[monitoring] own_prefixes" }),
        " to get alerted when they disappear or someone else announces them."));
      $("#ownSummary").textContent = "";
      return;
    }
    const list = s.own_prefixes || [];
    $("#ownSummary").textContent = s.ready ? `${list.filter((p) => p.ok).length} of ${list.length} OK` : "";
    fill(box, el("div", { class: "own-list" }, ...list.map((p) => {
      const foreign = p.more_specifics.filter((m) => !m.allowed);
      const label = p.ok ? "OK" : p.problems.map((x) => PROBLEM_LABELS[x] || x).join(" · ");
      let detail;
      if (p.route) {
        detail = el("div", { class: "detail" }, "Origin ", p.route.origin_asn === null ? `local (AS${CONFIG.asn})` : asLink(p.route.origin_asn),
          p.route.as_path && p.route.as_path.length ? ` · path ${p.route.as_path.join(" ")}` : "", p.route.age ? ` · since ${p.route.age}` : "");
      } else {
        detail = el("div", { class: "detail", text: p.problems.includes("invalid_config") ? "Not a valid prefix" : "Not in the BGP table" });
      }
      return el("div", { class: "own-item" + (p.ok ? "" : " problem") },
        el("div", { class: "own-head" }, lookupLink(p.prefix, p.prefix.split("/")[0]), stateBadge(p.ok ? "ok" : "err", label)),
        detail,
        p.rpki ? el("div", { class: "detail" }, rpkiBadge(p.rpki)) : null,
        p.more_specifics.length ? el("div", { class: "chips" }, ...p.more_specifics.slice(0, 30).map((m) =>
          el("a", { class: "chip" + (m.allowed ? "" : " bad"), href: `#lookup=${encodeURIComponent(m.prefix.split("/")[0])}`,
            title: m.allowed ? "allowed origin" : "foreign origin" }, m.prefix, el("small", { text: m.origin_asn === null ? "local" : `AS${m.origin_asn}` })))) : null,
        foreign.length > 30 ? el("div", { class: "detail", text: `${foreign.length} foreign more-specifics in total` }) : null);
    })));
  }

  function renderUnstable(s) {
    const u = s.unstable;
    $("#unstableWindowHead").replaceChildren(`Changes`, el("br"), `${u.window_hours}h`);
    $("#unstableSummary").textContent =
      `${fmt(u.route_changes)} route changes in ${u.window_hours}h · ${fmt(u.route_changes_1h)} in the last hour · ` +
      `${fmt(u.unstable_prefixes)} prefixes with ≥ ${u.min_changes} changes`;
    const max = Math.max(1, ...u.top.map((t) => t.changes));
    const tbody = $("#unstableTable tbody");
    if (!u.top.length) { fill(tbody, el("tr", {}, el("td", { colspan: 7, class: "empty", text: `No route changes in the last ${u.window_hours}h` }))); return; }
    fill(tbody, ...u.top.map((t) => el("tr", {},
      el("td", {}, lookupLink(t.prefix, t.prefix.split("/")[0])),
      el("td", { class: "num" }, el("span", { class: "minibar", style: `width:${Math.round((t.changes / max) * 80)}px` }), fmt(t.changes)),
      el("td", { class: "num", text: fmt(t.changes_1h) }),
      el("td", {}, asLink(t.origin_asn), t.origin_name && t.origin_name !== "(DNS Error)" ? el("span", { class: "cell-sub", text: t.origin_name }) : null),
      el("td", { class: "num" }, t.nexthop_asn === null ? "–" : asLink(t.nexthop_asn)),
      el("td", {}, stateBadge(t.active ? "ok" : "muted", t.active ? "announced" : "withdrawn")),
      el("td", { title: new Date(t.last_change).toLocaleString(), text: timeAgo(t.last_change) }))));
  }

  // ------------------------------------------------------------------ exits
  const EXIT_TYPES = [
    { key: "transit", label: "Transit", color: "var(--series-1)" },
    { key: "peering", label: "Peering", color: "var(--series-2)" },
    { key: "originated", label: "Own (originated)", color: "var(--series-3)" },
    { key: "other", label: "Other", color: "var(--series-4)", darkInk: true },
  ];
  const shortLoc = (name) => String(name || "").replace(/^Location\s+/i, "");

  function renderExits(s) {
    const box = $("#exits");
    const ex = s.exits || {};
    if (!CONFIG.exitsConfigured || !ex.configured) {
      fill(box, el("p", { class: "hint" }, "Not configured. Set ", el("code", { text: "[exits]" }),
        " in ", el("code", { text: "config/dashboard.toml" }),
        " (location community prefix and the community for routes learned from peers)."));
      return;
    }
    const sum = (loc, key) => loc[key]["4"] + loc[key]["6"];
    const used = EXIT_TYPES.filter((t) => ex.locations.some((l) => sum(l, t.key) > 0));

    // stacked bar per location (tall segmented bar: share printed inside when it fits)
    const bars = ex.locations.map((loc) => {
      const bar = el("div", { class: "share", role: "img",
        "aria-label": `${shortLoc(loc.name)}: ` + used.map((t) => `${t.label} ${fmt(sum(loc, t.key))}`).join(", ") });
      for (const t of used) {
        const value = sum(loc, t.key);
        if (!value) continue;
        const pct = (value / loc.total) * 100;
        const part = el("div", { class: "part" + (t.darkInk ? " dark-ink" : ""), tabindex: 0,
          style: `width:${pct}%;background:${t.color}` }, pct >= 9 ? `${Math.round(pct)}%` : "");
        const enter = (evt) => showTip(evt, `${fmt(value)} prefixes · ${pct.toFixed(1)}%`,
          `${shortLoc(loc.name)} · ${t.label} (IPv4 ${fmt(loc[t.key]["4"])}, IPv6 ${fmt(loc[t.key]["6"])})`);
        part.addEventListener("pointermove", enter); part.addEventListener("focus", enter);
        part.addEventListener("pointerleave", hideTip); part.addEventListener("blur", hideTip);
        bar.append(part);
      }
      return el("div", { class: "exit-row" },
        el("div", { class: "exit-head" }, el("strong", { text: shortLoc(loc.name) }), el("span", { class: "muted", text: `${fmt(loc.total)} prefixes` })),
        bar);
    });

    // table view with all numbers
    const table = el("table", { class: "data" },
      el("thead", {}, el("tr", {}, el("th", { text: "Location" }),
        ...used.map((t) => el("th", { class: "num" }, t.label, el("br"), "IPv4 / IPv6")),
        el("th", { class: "num", text: "Total" }))),
      el("tbody", {}, ...ex.locations.map((loc) => el("tr", {},
        el("td", { text: shortLoc(loc.name) }),
        ...used.map((t) => el("td", { class: "num", text: `${fmt(loc[t.key]["4"])} / ${fmt(loc[t.key]["6"])}` })),
        el("td", { class: "num", text: fmt(loc.total) })))));

    // peers / exchanges per location
    const locCodes = ex.locations.map((l) => l.location).filter(Boolean);
    const viaTable = ex.via.length ? el("div", { class: "table-wrap short" }, el("table", { class: "data" },
      el("thead", {}, el("tr", {}, el("th", { text: "Peer / exchange" }),
        ...ex.locations.filter((l) => l.location).map((l) => el("th", { class: "num", text: shortLoc(l.name) })),
        el("th", { class: "num", text: "Total" }))),
      el("tbody", {}, ...ex.via.map((v) => el("tr", {},
        el("td", {}, v.name || v.community, v.name ? el("span", { class: "cell-sub", text: v.community }) : null),
        ...locCodes.map((c) => el("td", { class: "num", text: v.locations[c] ? fmt(v.locations[c]) : "–" })),
        el("td", { class: "num", text: fmt(v.total) })))))) : el("div", { class: "muted small", text: "No routes with a peer community." });

    // via transit although the origin AS is a direct peer
    const tip = ex.transit_instead_of_peering;
    let tipBox = null;
    if (tip && tip.available) {
      tipBox = el("div", {},
        el("h3", { text: `Via transit although the origin AS is a direct peer · ${fmt(tip.total_prefixes)} prefixes` }),
        el("p", { class: "explain", text: "These networks peer with you, but the listed prefixes are reached via transit: the peer does not announce them to you (or not at that location), or your policy prefers transit." }),
        tip.asns.length ? el("div", { class: "table-wrap short" }, el("table", { class: "data" },
          el("thead", {}, el("tr", {}, ...["Origin AS", "Via transit", "Via peering", "Peering at", "Examples"].map((h, i) => el("th", { class: i === 1 || i === 2 ? "num" : null, text: h })))),
          el("tbody", {}, ...tip.asns.map((a) => el("tr", {},
            el("td", {}, asLink(a.asn), a.name && a.name !== "(DNS Error)" ? el("span", { class: "cell-sub", text: a.name }) : null),
            el("td", { class: "num", text: fmt(a.prefixes_via_transit) }),
            el("td", { class: "num", text: fmt(a.prefixes_via_peering) }),
            el("td", { class: "small", text: a.peering_locations.map(shortLoc).join(", ") || "–" }),
            el("td", {}, el("div", { class: "chips" }, ...a.samples.map((p) =>
              el("a", { class: "chip", href: `#lookup=${encodeURIComponent(p.split("/")[0])}`, text: p }))))))))) 
          : el("div", { class: "muted small", text: "None – every prefix of your peers is reached via peering." }));
    }

    fill(box, el("div", { class: "exits-grid" },
      el("div", {},
        el("h3", { text: "Exit by location" }),
        el("div", { class: "legend" }, ...used.map((t) => el("span", { style: `--swatch:${t.color}`, text: t.label }))),
        ...bars,
        table),
      el("div", {}, el("h3", { text: "Peers & exchanges by location" }), viaTable)),
      tipBox ? el("div", { style: "margin-top:16px" }, tipBox) : null);
  }

  // ------------------------------------------------------------------ rpki
  async function refreshRpki() {
    if (!CONFIG.rpkiEnabled) return;
    $("#rpkiCard").hidden = false;
    let d;
    try { d = await getJSON(`${API}/rpki`); } catch (e) { return; }
    const box = $("#rpki");
    $("#rpkiUpdated").textContent = d.updated ? `validated ${timeAgo(d.updated)} · ${fmt(d.vrps)} VRPs` : "";
    if (!d.counts) {
      fill(box, el("div", { class: d.error ? "error" : "muted", text: d.error ? `Routinator not reachable: ${d.error}` : "Loading VRPs…" }));
      return;
    }
    const total = (s) => d.counts[s]["4"] + d.counts[s]["6"];
    const all = Math.max(1, total("valid") + total("invalid") + total("not-found"));
    const parts = [
      { key: "valid", color: "var(--good)" }, { key: "invalid", color: "var(--critical)" },
      { key: "not-found", color: "var(--text-muted)" }];
    const bar = el("div", { class: "share", role: "img", "aria-label": parts.map((p) => `${RPKI_LABELS[p.key]}: ${fmt(total(p.key))}`).join(", ") },
      ...parts.filter((p) => total(p.key)).map((p) => {
        const pct = (total(p.key) / all) * 100;
        return el("div", { class: "part", style: `width:${pct}%;background:${p.color}` }, pct >= 8 ? `${Math.round(pct)}%` : "");
      }));
    fill(box,
      d.error ? el("div", { class: "error", text: `Last refresh failed (showing previous result): ${d.error}` }) : null,
      el("div", { class: "rpki-summary" }, ...parts.map((p) => el("div", {},
        stateBadge(RPKI_LEVEL[p.key], RPKI_LABELS[p.key]),
        el("div", { class: "value", text: fmt(total(p.key)) }),
        el("div", { class: "muted small", text: `IPv4 ${fmt(d.counts[p.key]["4"])} · IPv6 ${fmt(d.counts[p.key]["6"])}` })))),
      bar,
      el("h3", { text: `Invalid routes (${fmt(d.invalid_total)})`, style: "font-size:13px;margin:14px 0 6px;color:var(--text-secondary)" }),
      d.invalid.length ? el("div", { class: "table-wrap short" }, el("table", { class: "data" },
        el("thead", {}, el("tr", {}, ...["Prefix", "Origin AS", "Reason", "Next-hop AS", "Covering ROAs"].map((h) => el("th", { text: h })))),
        el("tbody", {}, ...d.invalid.map((i) => el("tr", {},
          el("td", {}, lookupLink(i.prefix, i.prefix.split("/")[0])),
          el("td", {}, asLink(i.origin_asn), i.origin_name && i.origin_name !== "(DNS Error)" ? el("span", { class: "cell-sub", text: i.origin_name }) : null),
          el("td", { class: "small", text: RPKI_REASON[i.reason] || i.reason }),
          el("td", { class: "num" }, i.nexthop_asn === null || i.nexthop_asn === undefined ? "–" : asLink(i.nexthop_asn)),
          el("td", {}, vrpChips(i.vrps)))))))
        : el("div", { class: "muted small", text: "No invalid routes." }),
      d.invalid_total > d.invalid.length ? el("div", { class: "pager", text: `Showing ${d.invalid.length} of ${fmt(d.invalid_total)}.` }) : null);
  }

  // ------------------------------------------------------------------ data refresh
  let stats = null, history = [], hours = 24, lastBasic = null, lastAdvanced = null;
  const rangeLabel = () => (hours >= 48 ? `${hours / 24}d` : `${hours}h`);

  $("#rangePicker").addEventListener("click", async (evt) => {
    const btn = evt.target.closest("button[data-hours]");
    if (!btn) return;
    hours = Number(btn.dataset.hours);
    $("#rangePicker").querySelectorAll("button").forEach((b) => b.classList.toggle("active", b === btn));
    await loadHistory();
    if (stats) renderTiles(stats, history);
  });

  async function loadHistory() {
    try { history = await getJSON(`${API}/stats/history?hours=${hours}&points=240`); }
    catch (e) { history = []; }
  }

  function setStatus(level, text) {
    $("#statusDot").className = "dot " + level;
    $("#statusText").textContent = text;
  }

  function renderAdvanced(s) {
    peerTable.set(s.peers);
    communityTable.set(s.communities);
    hbars($("#topPeers"), s.top_n_peers.map((p) => ({
      value: p.count, link: lookupLink(`AS${p.asn}`, `AS${p.asn}`), note: p.name,
    })), "No peers yet");
    const peering = s.communities
      .filter((c) => c.community.startsWith(CONFIG.peerCommunityPrefix))
      .sort((a, b) => b.count - a.count)
      .map((c) => ({ value: c.count, label: c.name || c.community, note: c.name ? c.community : null }));
    hbars($("#peeringBars"), peering, `No prefixes with community ${CONFIG.peerCommunityPrefix}* yet`);
    transitShare($("#transitShare"), s);
    columnChart($("#cidr4"), s.cidr_breakdown.filter((c) => c.ip_version === 4), "IPv4 prefixes per prefix length");
    columnChart($("#cidr6"), s.cidr_breakdown.filter((c) => c.ip_version === 6), "IPv6 prefixes per prefix length");
    renderUnstable(s);
    renderExits(s);
    refreshRpki();
  }

  async function refresh() {
    try {
      const s = await getJSON(`${API}/stats`);
      stats = s;
      if (s.basic_updated !== lastBasic) {
        lastBasic = s.basic_updated;
        await loadHistory();
        renderTiles(s, history);
        renderOwnPrefixes(s);
      }
      if (s.advanced_updated !== lastAdvanced) {
        lastAdvanced = s.advanced_updated;
        renderAdvanced(s);
      }
      await refreshSessions();
      const age = s.basic_updated ? (Date.now() - new Date(s.basic_updated).getTime()) / 1000 : Infinity;
      const problems = [];
      if (sessions && sessions.summary.down) problems.push(`${sessions.summary.down} session${sessions.summary.down > 1 ? "s" : ""} down`);
      if (s.own_prefix_problems) problems.push(`${s.own_prefix_problems} own prefix problem${s.own_prefix_problems > 1 ? "s" : ""}`);
      if (!s.ready) setStatus("warn", "Computing statistics…");
      else if (problems.length) setStatus("err", problems.join(" · "));
      else if (age > CONFIG.statsInterval * 3 + 30) setStatus("warn", `Stale – last update ${timeAgo(s.basic_updated)}`);
      else if (sessions && sessions.stale) setStatus("warn", "Collector not reporting");
      else setStatus("ok", "Live");
      $("#updated").textContent = s.ready
        ? `Counters ${timeAgo(s.basic_updated)} · tables ${timeAgo(s.advanced_updated)} (every ${CONFIG.advancedInterval}s)`
        : "";
    } catch (e) {
      setStatus("err", "Backend unreachable");
    }
  }

  let resizeTimer = null;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => { if (stats) { renderTiles(stats, history); renderAdvanced(stats); } }, 200);
  });

  // ------------------------------------------------------------------ looking glass
  const lookup = $("#lookup"), lookupBody = $("#lookupBody");

  function kvTable(rows) {
    return el("table", { class: "data kv" }, el("tbody", {}, ...rows.filter(Boolean).map(([k, v]) =>
      el("tr", {}, el("th", { text: k }), el("td", {}, v ?? "–")))));
  }
  function asPathChips(path) {
    if (!path || !path.length) return el("span", { class: "muted", text: "(empty – locally originated)" });
    return el("div", { class: "chips" }, ...path.map((asn, i) =>
      el("a", { class: "chip" + (i > 0 && path[i - 1] === asn ? " dup" : ""), href: `#lookup=AS${asn}`, text: String(asn) })));
  }
  function communityChips(list, names) {
    if (!list || !list.length) return el("span", { class: "muted", text: "none" });
    return el("div", { class: "chips" }, ...list.map((c) =>
      el("span", { class: "chip" }, c, names && names[c] ? el("small", { text: names[c] }) : null)));
  }

  function openLookup(title, rawUrl) {
    lookup.hidden = false;
    $("#lookupTitle").textContent = title;
    $("#lookupRaw").href = rawUrl;
    fill(lookupBody, el("div", { class: "muted", text: "Loading…" }));
    lookup.scrollIntoView({ behavior: "smooth", block: "start" });
  }
  const lookupError = (e) => fill(lookupBody, el("div", { class: "error", text: e.message }));

  async function lookupIP(query) {
    const url = `${API}/ip/${encodeURIComponent(query)}`;
    openLookup(`Route for ${query}`, url);
    try {
      const r = await getJSON(url);
      if (!r || !r.prefix) { fill(lookupBody, el("div", { class: "empty", text: `No active route covers ${query}.` })); return; }
      fill($("#lookupTitle"), r.prefix, el("span", { class: "badge", text: r.is_transit ? "transit" : "not transit" }));
      const historyBox = el("div", {}, el("button", { class: "ghost", type: "button", text: "Show route history ▸", onclick: () => loadRouteHistory(query, historyBox) }));
      fill(lookupBody, 
        kvTable([
          ["Queried", query],
          ["Origin AS", r.origin_asn !== null ? el("span", {}, lookupLink(`AS${r.origin_asn}`, `AS${r.origin_asn}`), `  ${r.name}`) : `local (AS${CONFIG.asn})`],
          ["AS path", asPathChips(r.as_path)],
          ["Next hop", el("span", {}, r.nexthop || "–", r.nexthop_ip_dns && r.nexthop_ip_dns !== "(DNS Error)" ? el("span", { class: "muted", text: `  ${r.nexthop_ip_dns}` }) : null)],
          ["Next-hop AS", r.nexthop_asn !== null ? lookupLink(`AS${r.nexthop_asn}`, `AS${r.nexthop_asn}`) : "–"],
          ["Communities", communityChips(r.communities, r.community_names)],
          ["Local pref / MED", `${r.local_pref} / ${r.med}`],
          ["Origin", r.route_origin],
          ["Originator", r.originator_id ? `${r.originator_id}${r.originator_id_dns && r.originator_id_dns !== "(DNS Error)" ? "  " + r.originator_id_dns : ""}` : null],
          ["Cluster list", (r.cluster_list || []).join(", ") || null],
          r.aggregator_as ? ["Aggregator", `AS${r.aggregator_as} ${r.aggregator_address || ""}`] : null,
          r.atomic_aggregate ? ["Atomic aggregate", "yes"] : null,
          r.rpki ? ["RPKI", el("div", {}, rpkiBadge(r.rpki), r.rpki.vrps && r.rpki.vrps.length ? vrpChips(r.rpki.vrps) : null)] : null,
          ["Last change", r.age],
        ]),
        historyBox,
      );
    } catch (e) { lookupError(e); }
  }

  async function loadRouteHistory(query, box) {
    fill(box, el("div", { class: "muted", text: "Loading history…" }));
    try {
      const r = await getJSON(`${API}/ip/${encodeURIComponent(query)}/history`);
      const h = r.history || [];
      if (!h.length) { fill(box, el("h3", { text: "Route history" }), el("div", { class: "muted", text: "No earlier versions recorded." })); return; }
      fill(box, el("h3", { text: `Route history (${h.length} earlier versions, newest first)` }),
        el("div", { class: "table-wrap short" }, el("table", { class: "data" },
          el("thead", {}, el("tr", {}, ...["Changed", "State", "AS path", "Next hop", "Communities", "LP / MED"].map((t) => el("th", { text: t })))),
          el("tbody", {}, ...h.map((v) => el("tr", {},
            el("td", { text: v.age }),
            el("td", { text: v.withdrawal ? "withdrawn" : "announced" }),
            el("td", { text: (v.as_path || []).join(" ") }),
            el("td", { text: v.nexthop || "–" }),
            el("td", { text: (v.communities || []).join(" ") }),
            el("td", { class: "num", text: `${v.local_pref} / ${v.med}` })))))));
    } catch (e) { fill(box, el("div", { class: "error", text: e.message })); }
  }

  async function lookupASN(asn) {
    const url = `${API}/asn/${asn}`;
    openLookup(`AS${asn}`, url);
    try {
      const r = await getJSON(url);
      fill($("#lookupTitle"), `AS${asn} `, el("span", { class: "muted", text: r.name }),
        r.is_peer ? el("span", { class: "badge", text: "direct peer" }) : null);
      const prefixes = r.origin_prefix_list || [];
      fill(lookupBody, 
        el("div", { class: "lookup-links" },
          el("span", { text: `${fmt(r.origin_prefix_count)} originated prefixes` }),
          el("a", { href: `#downstream=${asn}`, text: "Downstream ASNs" }),
          el("a", { href: `#list=${asn}/nexthop`, text: "Prefixes via this next-hop AS" }),
          el("a", { href: `#list=${asn}/transit`, text: "Prefixes transiting this AS" })),
        prefixes.length ? el("div", { class: "table-wrap short" }, el("table", { class: "data" },
          el("thead", {}, el("tr", {}, ...["Prefix", "AS path", "Next hop", "Communities", "Updated"].map((t) => el("th", { text: t })))),
          el("tbody", {}, ...prefixes.slice(0, MAX_ROWS).map((p) => el("tr", {},
            el("td", {}, lookupLink(p.prefix, p.prefix.split("/")[0])),
            el("td", { text: (p.as_path || []).join(" ") }),
            el("td", { text: p.nexthop_ip || "–", title: p.nexthop_ip_dns || "" }),
            el("td", { text: (p.communities || []).join(" ") }),
            el("td", { text: p.updated })))))) : el("div", { class: "empty", text: "This AS originates no active prefixes." }),
        prefixes.length > MAX_ROWS ? el("div", { class: "pager", text: `Showing ${MAX_ROWS} of ${fmt(prefixes.length)} – see JSON for all.` }) : null,
      );
    } catch (e) { lookupError(e); }
  }

  async function lookupList(spec) {
    const [asn, kind, version] = spec.split("/");
    const url = `${API}/asn/${asn}/${kind}${version ? "/" + version : ""}`;
    const titles = { originated: "originated by", nexthop: "with next-hop", transit: "transiting" };
    openLookup(`${version ? version.replace("ip", "IP") + " p" : "P"}refixes ${titles[kind] || kind} AS${asn}`, url);
    try {
      const r = await getJSON(url);
      const list = r[`${kind}_prefix_list`] || [];
      fill(lookupBody, 
        el("div", { class: "lookup-links" }, el("span", { text: `${fmt(list.length)} prefixes` }), lookupLink(`AS${asn} ${r.name}`, `AS${asn}`)),
        list.length ? el("div", { class: "chips" }, ...list.slice(0, MAX_ROWS).map((p) => el("a", { class: "chip", href: `#lookup=${encodeURIComponent(p.split("/")[0])}`, text: p })))
          : el("div", { class: "empty", text: "No prefixes." }),
        list.length > MAX_ROWS ? el("div", { class: "pager", text: `Showing ${MAX_ROWS} of ${fmt(list.length)} – see JSON for all.` }) : null,
      );
    } catch (e) { lookupError(e); }
  }

  async function lookupDownstream(asn) {
    const url = `${API}/asn/${asn}/downstream`;
    openLookup(`Downstream ASNs of AS${asn}`, url);
    try {
      const r = await getJSON(url);
      fill(lookupBody, 
        el("div", { class: "lookup-links" }, el("span", { text: `${fmt(r.downstream_asns_count)} ASNs directly behind AS${asn} (${r.name})` })),
        r.downstream_asns.length ? el("div", { class: "chips" }, ...r.downstream_asns.slice(0, MAX_ROWS).map((d) =>
          el("a", { class: "chip", href: `#lookup=AS${d.asn}`, title: d.name, text: `AS${d.asn}` })))
          : el("div", { class: "empty", text: "None." }));
    } catch (e) { lookupError(e); }
  }

  function route() {
    const hash = decodeURIComponent(location.hash.slice(1));
    const [kind, ...rest] = hash.split("=");
    const value = rest.join("=").trim();
    if (!value) { lookup.hidden = true; return; }
    if (kind === "lookup") {
      $("#searchInput").value = value;
      const m = value.match(/^(?:AS)?(\d+)$/i);
      if (m && !value.includes(".")) lookupASN(Number(m[1]));
      else lookupIP(value);
    } else if (kind === "list") lookupList(value);
    else if (kind === "downstream") lookupDownstream(Number(value));
  }

  $("#search").addEventListener("submit", (evt) => {
    evt.preventDefault();
    const q = $("#searchInput").value.trim();
    if (!q) return;
    const target = `#lookup=${encodeURIComponent(q)}`;
    if (location.hash === target) route(); else location.hash = target;
  });
  $("#lookupClose").addEventListener("click", () => {
    lookup.hidden = true;
    window.history.replaceState(null, "", location.pathname + location.search);
  });
  window.addEventListener("hashchange", route);

  // ------------------------------------------------------------------ start
  route();
  refresh();
  setInterval(refresh, POLL_MS);
})();
