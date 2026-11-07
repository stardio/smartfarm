"use strict";
(function () {
  var TYPES = {
    temperature: { label: "온도", unit: "°C", digits: 1 },
    humidity: { label: "습도", unit: "%", digits: 1 },
    ph: { label: "pH", unit: "", digits: 2 },
    ec: { label: "EC", unit: "mS/cm", digits: 2 }
  };
  var ORDER = ["temperature", "humidity", "ph", "ec"];
  var ROLES = {
    nutrient_supply: "양액 공급",
    drain: "배수",
    irrigation_a: "관수 A",
    irrigation_b: "관수 B"
  };
  var STATE_TEXT = {
    ok: "정상 수신 중",
    stale: "갱신 중단됨",
    unknown: "상태 확인 불가",
    error: "서버 연결 실패"
  };
  var STATUS_MS = 10000;
  var HISTORY_MS = 60000;
  var SVGNS = "http://www.w3.org/2000/svg";

  var hours = 24;
  var lastStatus = null;
  var timers = [];

  function $(id) { return document.getElementById(id); }
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }
  function svg(tag, attrs) {
    var e = document.createElementNS(SVGNS, tag);
    for (var k in attrs) e.setAttribute(k, attrs[k]);
    return e;
  }
  function sensorLabel(s) {
    var t = TYPES[s.type] ? TYPES[s.type].label : s.type;
    var m = /(\d+)$/.exec(s.name || "");
    return m ? t + " " + m[1] : t;
  }
  function fmt(v, type) {
    if (typeof v !== "number" || !isFinite(v)) return "—";
    return v.toFixed(TYPES[type] ? TYPES[type].digits : 2);
  }
  function outOfRange(v, rule) {
    if (!rule || typeof v !== "number") return false;
    return (rule.min != null && v < rule.min) || (rule.max != null && v > rule.max);
  }
  function rangeText(rule, type) {
    if (!rule) return "";
    var u = TYPES[type] ? TYPES[type].unit : "";
    if (rule.min != null && rule.max != null) return "정상 " + rule.min + "–" + rule.max + (u ? " " + u : "");
    if (rule.max != null) return "정상 ≤ " + rule.max + (u ? " " + u : "");
    if (rule.min != null) return "정상 ≥ " + rule.min + (u ? " " + u : "");
    return "";
  }
  function timeText(iso) {
    if (!iso) return "—";
    var d = new Date(iso);
    if (isNaN(d)) return "—";
    return d.toLocaleString("ko-KR", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" });
  }
  function getJSON(url) {
    return fetch(url, { cache: "no-store" }).then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    });
  }

  // ── 상태 ──────────────────────────────────────────────
  function setState(state) {
    $("state").setAttribute("data-state", state);
    $("state-text").textContent = STATE_TEXT[state] || STATE_TEXT.unknown;
  }

  function renderStatus(st) {
    lastStatus = st;
    setState(st.state);
    $("updated").textContent = "마지막 측정 " + timeText(st.updated_at);

    var sensors = (st.sensors || []).slice().sort(function (a, b) {
      return ORDER.indexOf(a.type) - ORDER.indexOf(b.type) || (a.name < b.name ? -1 : 1);
    });
    var anySim = false;
    var grid = $("sensors");
    grid.textContent = "";
    if (!sensors.length) grid.appendChild(el("p", "muted", "표시할 센서 데이터가 없습니다."));
    sensors.forEach(function (s) {
      var rule = st.rules ? st.rules[s.type] : null;
      var out = st.state === "ok" && outOfRange(s.value, rule);
      var card = el("div", "card" + (out ? " out" : ""));
      var label = el("div", "label", sensorLabel(s));
      if (s.simulated) { anySim = true; label.appendChild(el("span", "tag sim", "모의값")); }
      if (out) label.appendChild(el("span", "tag bad", "범위 이탈"));
      var value = el("div", "value", fmt(s.value, s.type));
      var unit = TYPES[s.type] ? TYPES[s.type].unit : "";
      if (unit) value.appendChild(el("span", "unit", unit));
      card.appendChild(label);
      card.appendChild(value);
      var rt = rangeText(rule, s.type);
      if (rt) card.appendChild(el("div", "range", rt));
      grid.appendChild(card);
    });
    $("sim-notice").hidden = !anySim;

    var ul = $("valves");
    ul.textContent = "";
    if (!(st.valves || []).length) ul.appendChild(el("li", "muted", "등록된 밸브가 없습니다."));
    (st.valves || []).forEach(function (v) {
      var li = el("li");
      var left = el("div");
      left.appendChild(el("div", "name", v.name));
      left.appendChild(el("div", "role", ROLES[v.role] || v.role || ""));
      li.appendChild(left);
      li.appendChild(el("span", "pill" + (v.open ? " open" : ""), v.open ? "열림" : "닫힘"));
      ul.appendChild(li);
    });
  }

  function loadStatus() {
    return getJSON("/api/public/status").then(renderStatus).catch(function () {
      // 실패 시 이전 값을 정상처럼 보이지 않게 상태를 바꾼다
      setState("error");
    });
  }

  // ── 그래프 ────────────────────────────────────────────
  var W = 600, H = 200, PL = 40, PR = 10, PT = 10, PB = 22;

  function drawChart(type, list, rule, bucket) {
    var box = el("div", "chart");
    var meta = TYPES[type];
    var h3 = el("h3", null, meta.label + (meta.unit ? " (" + meta.unit + ")" : ""));
    var rt = rangeText(rule, type);
    if (rt) h3.appendChild(el("span", "muted", rt));
    box.appendChild(h3);

    var pts = [];
    list.forEach(function (s) { s.points.forEach(function (p) { pts.push(p); }); });
    if (!pts.length) {
      box.appendChild(el("div", "empty", "이 기간의 데이터가 없습니다."));
      return box;
    }

    var t0 = Infinity, t1 = -Infinity, v0 = Infinity, v1 = -Infinity;
    pts.forEach(function (p) {
      if (p[0] < t0) t0 = p[0]; if (p[0] > t1) t1 = p[0];
      if (p[1] < v0) v0 = p[1]; if (p[1] > v1) v1 = p[1];
    });
    if (rule) {
      if (rule.min != null) { v0 = Math.min(v0, rule.min); v1 = Math.max(v1, rule.min); }
      if (rule.max != null) { v0 = Math.min(v0, rule.max); v1 = Math.max(v1, rule.max); }
    }
    var pad = (v1 - v0) * 0.08 || 1;
    v0 -= pad; v1 += pad;
    var nowSec = Date.now() / 1000;
    t0 = Math.min(t0, nowSec - hours * 3600);
    t1 = Math.max(t1, nowSec);

    function x(t) { return PL + (t - t0) / (t1 - t0 || 1) * (W - PL - PR); }
    function y(v) { return PT + (1 - (v - v0) / (v1 - v0 || 1)) * (H - PT - PB); }

    var s = svg("svg", { viewBox: "0 0 " + W + " " + H, role: "img",
      "aria-label": meta.label + " 최근 " + hours + "시간 추이" });

    if (rule && (rule.min != null || rule.max != null)) {
      var top = y(rule.max != null ? rule.max : v1);
      var bot = y(rule.min != null ? rule.min : v0);
      s.appendChild(svg("rect", { "class": "band", x: PL, y: top, width: W - PL - PR, height: Math.max(0, bot - top) }));
    }
    for (var i = 0; i <= 4; i++) {
      var v = v0 + (v1 - v0) * i / 4, yy = y(v);
      s.appendChild(svg("line", { "class": "gridline", x1: PL, x2: W - PR, y1: yy, y2: yy }));
      var lab = svg("text", { "class": "axis", x: PL - 6, y: yy + 3, "text-anchor": "end" });
      lab.textContent = v.toFixed(meta.digits);
      s.appendChild(lab);
    }
    [0, 0.5, 1].forEach(function (f) {
      var t = t0 + (t1 - t0) * f;
      var d = new Date(t * 1000);
      var txt = hours > 24
        ? (d.getMonth() + 1) + "/" + d.getDate() + " " + String(d.getHours()).padStart(2, "0") + "시"
        : String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0");
      var lab = svg("text", { "class": "axis", x: x(t), y: H - 6,
        "text-anchor": f === 0 ? "start" : f === 1 ? "end" : "middle" });
      lab.textContent = txt;
      s.appendChild(lab);
    });

    var legend = el("div", "legend");
    list.forEach(function (ser, idx) {
      var color = "var(--c" + (idx % 3 + 1) + ")";
      var d = "", prev = null;
      ser.points.forEach(function (p) {
        // 측정이 끊긴 구간은 선을 잇지 않는다
        var gap = prev && p[0] - prev[0] > bucket * 2.5;
        d += (d === "" || gap ? "M" : "L") + x(p[0]).toFixed(1) + " " + y(p[1]).toFixed(1) + " ";
        prev = p;
      });
      var path = svg("path", { d: d, fill: "none", "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" });
      path.style.stroke = color;
      s.appendChild(path);
      if (list.length > 1) {
        var item = el("span", null, sensorLabel(ser));
        var sw = el("i");
        sw.style.background = color;
        item.insertBefore(sw, item.firstChild);
        legend.appendChild(item);
      }
    });
    box.appendChild(s);
    if (list.length > 1) box.appendChild(legend);
    return box;
  }

  function renderHistory(hist) {
    var wrap = $("charts");
    wrap.textContent = "";
    var rules = (lastStatus && lastStatus.rules) || {};
    var byType = {};
    (hist.series || []).forEach(function (s) { (byType[s.type] = byType[s.type] || []).push(s); });
    var shown = 0;
    ORDER.forEach(function (t) {
      if (!byType[t]) return;
      byType[t].sort(function (a, b) { return a.name < b.name ? -1 : 1; });
      wrap.appendChild(drawChart(t, byType[t], rules[t], hist.bucket_sec || 600));
      shown++;
    });
    if (!shown) wrap.appendChild(el("p", "muted", "이 기간의 기록이 없습니다."));
  }

  function loadHistory() {
    return getJSON("/api/public/history?hours=" + hours).then(renderHistory).catch(function () {
      var wrap = $("charts");
      wrap.textContent = "";
      wrap.appendChild(el("p", "muted", "그래프 데이터를 불러오지 못했습니다."));
    });
  }

  // ── 갱신 주기 (백그라운드 탭에서는 중단) ─────────────────
  function stop() { timers.forEach(clearInterval); timers = []; }
  function start() {
    stop();
    loadStatus().then(loadHistory);
    timers.push(setInterval(loadStatus, STATUS_MS));
    timers.push(setInterval(loadHistory, HISTORY_MS));
  }
  document.addEventListener("visibilitychange", function () {
    if (document.hidden) stop(); else start();
  });

  $("range").addEventListener("click", function (e) {
    var b = e.target.closest("button[data-h]");
    if (!b) return;
    hours = parseInt(b.getAttribute("data-h"), 10);
    Array.prototype.forEach.call(this.querySelectorAll("button"), function (x) {
      x.setAttribute("aria-pressed", x === b ? "true" : "false");
    });
    loadHistory();
  });

  start();
})();

// ── 구역 현황 (보기 전용) ──────────────────────────────
(function () {
  var box = document.getElementById("control");
  if (!box) return;
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }
  function load() {
    if (document.hidden) return;
    fetch("/api/public/control", { cache: "no-store" }).then(function (r) { return r.json(); }).then(function (d) {
      box.textContent = "";
      document.getElementById("control-note").textContent =
        d.state !== "ok" ? "제어기 응답 없음" : (d.paused ? "전체 정지 중" : "");
      (d.systems || []).forEach(function (s) {
        var c = el("div", "card ov static");
        var h = el("div", "ov-head");
        var on = s.zones.filter(function (z) { return z.on; }).length;
        h.appendChild(el("strong", null, s.name));
        h.appendChild(el("span", "pill" + (on ? " open" : ""), "켜짐 " + on + "/" + s.zones.length));
        c.appendChild(h);
        var cells = el("div", "cells");
        s.zones.forEach(function (z) {
          var x = el("span", "cell" + (z.on ? " on" : "") + (z.enabled ? "" : " off"), String(z.zone));
          x.title = z.name + (z.on ? " · 켜짐" : " · 꺼짐");
          cells.appendChild(x);
        });
        c.appendChild(cells);
        box.appendChild(c);
      });
    }).catch(function () { /* 다음 주기에 재시도 */ });
  }
  load();
  setInterval(load, 10000);
  document.addEventListener("visibilitychange", load);
})();
