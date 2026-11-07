"use strict";
(function () {
  var REFRESH_MS = 2000;
  var DAYS = ["월", "화", "수", "목", "금", "토", "일"];
  var TABS = [["overview", "개요"], ["irrigation", "관수"], ["nutrient", "양액"], ["lighting", "조명"],
              ["fan", "공조팬"], ["heater", "히터"], ["report", "리포트"]];
  var HELP = {
    irrigation: "정해진 시각에 정한 시간(분)만큼 물을 줍니다. 동시에 켤 수 있는 구역 수를 넘으면 순서대로 대기합니다.",
    lighting: "스케줄 시간대에 켭니다. 22:00~06:00 처럼 자정을 넘겨도 되고, 시작과 끝이 같으면 하루 종일 켭니다.",
    fan: "스케줄 시간대이거나, 온도가 설정온도+편차 이상이면 켜고 설정온도 이하로 내려가면 끕니다.",
    heater: "온도가 설정온도−편차 이하면 켜고 설정온도에 도달하면 끕니다. 스케줄을 넣으면 그 시간대에만 동작합니다. 센서 값이 없으면 안전을 위해 끕니다."
  };
  var TRIG = { schedule: "스케줄", manual: "수동", auto: "자동", thermo: "온도", safety: "안전" };
  var RES = { completed: "완료", stopped: "중지", interrupted: "중단(재시작)" };

  var st = null, tab = "overview", repDays = 7, timer = null, built = {}, nutFilled = false;
  var edit = null, toastTimer = null;

  function $(id) { return document.getElementById(id); }
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }
  function pad(n) { return (n < 10 ? "0" : "") + n; }
  function hm(ts) { var d = new Date(ts * 1000); return pad(d.getHours()) + ":" + pad(d.getMinutes()); }
  function mdhm(ts) { var d = new Date(ts * 1000); return (d.getMonth() + 1) + "/" + d.getDate() + " " + hm(ts); }
  function dur(sec) {
    sec = Math.round(sec || 0);
    if (sec < 60) return sec + "초";
    var h = Math.floor(sec / 3600), m = Math.floor(sec % 3600 / 60);
    return h ? h + "시간 " + m + "분" : m + "분";
  }
  function api(method, url, body) {
    var opt = { method: method, cache: "no-store", credentials: "same-origin", headers: {} };
    if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
    return fetch(url, opt).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) { return { status: r.status, body: j }; });
    });
  }
  function toast(text, bad) {
    var t = $("toast");
    t.textContent = text || "";
    t.classList.toggle("ok", !bad);
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { t.textContent = ""; }, 5000);
  }
  function sys(id) { return (st.status.systems || []).filter(function (s) { return s.id === id; })[0]; }
  function spec(id) { return st.systems.filter(function (s) { return s.id === id; })[0]; }
  function sensorsOf(type) {
    return Object.keys(st.sensors).filter(function (k) { return st.sensors[k] === type; });
  }
  function schedText(sid, c) {
    var on = (c.schedules || []).filter(function (s) { return s.enabled !== false; });
    if (!on.length) return "스케줄 없음";
    return on.map(function (s) {
      var days = s.days.length === 7 ? "매일" : s.days.map(function (d) { return DAYS[d]; }).join("");
      return s.start + (sid === "irrigation" ? " " + s.duration_min + "분" : "~" + s.end) + " " + days;
    }).join(" · ");
  }

  // ── 명령 ───────────────────────────────────────────────
  function command(body) {
    return api("POST", "/admin/api/control/command", body).then(function (r) {
      if (r.status === 401) return location.reload();
      toast(r.body.message || (r.status === 200 ? "완료" : "실패 (" + r.status + ")"), r.status >= 300 && r.status !== 202);
      load();
    }).catch(function () { toast("서버에 연결할 수 없습니다.", true); });
  }

  // ── 탭 ─────────────────────────────────────────────────
  function buildTabs() {
    var nav = $("tabs");
    nav.textContent = "";
    TABS.forEach(function (t) {
      var b = el("button", "tab-btn", t[1]);
      b.type = "button";
      b.setAttribute("role", "tab");
      b.dataset.tab = t[0];
      b.addEventListener("click", function () { setTab(t[0]); });
      nav.appendChild(b);
    });
  }
  function setTab(t) {
    tab = t;
    try { localStorage.setItem("ctl-tab", t); } catch (e) { /* 무시 */ }
    Array.prototype.forEach.call($("tabs").children, function (b) {
      b.setAttribute("aria-selected", b.dataset.tab === t ? "true" : "false");
    });
    var zoneTab = ["irrigation", "lighting", "fan", "heater"].indexOf(t) >= 0;
    $("tab-overview").hidden = t !== "overview";
    $("tab-zones").hidden = !zoneTab;
    $("tab-nutrient").hidden = t !== "nutrient";
    $("tab-report").hidden = t !== "report";
    built = {};
    render();
    if (t === "report") loadReport();
  }

  // ── 개요 ───────────────────────────────────────────────
  function renderOverview() {
    var g = $("overview");
    g.textContent = "";
    (st.status.systems || []).forEach(function (s) {
      var c = el("button", "card ov");
      c.type = "button";
      c.addEventListener("click", function () { setTab(s.id); });
      var h = el("div", "ov-head");
      h.appendChild(el("strong", null, s.name));
      h.appendChild(el("span", "pill" + (s.on_count ? " open" : ""), "켜짐 " + s.on_count + "/" + s.zones.length));
      c.appendChild(h);
      var cells = el("div", "cells");
      s.zones.forEach(function (z) {
        var x = el("span", "cell" + (z.on ? " on" : "") + (z.enabled ? "" : " off"), String(z.zone));
        x.title = z.name + (z.on ? " · 켜짐" : " · 꺼짐") + (z.reason ? " · " + z.reason : "");
        cells.appendChild(x);
      });
      c.appendChild(cells);
      var note = "";
      if (s.id === "irrigation") note = (s.pump ? "주 펌프 켜짐 · " : "") + (s.queue.length ? "대기 " + s.queue.length + "구역" : "대기 없음");
      if (s.id === "nutrient") note = (s.auto ? "자동 투입 · " : "수동 · ") + ((s.phase || {}).msg || "");
      if (note) c.appendChild(el("div", "muted", note));
      g.appendChild(c);
    });
  }

  // ── 구역 카드 ──────────────────────────────────────────
  function buildZones(sid) {
    var sp = spec(sid), cfgs = st.configs[sid], box = $("zones");
    box.textContent = "";
    $("zones-title").textContent = sp.name + " (" + sp.zones + "구역)";
    $("zones-help").textContent = HELP[sid] || "";
    cfgs.forEach(function (c, i) {
      var z = i + 1, card = el("div", "card zone");
      card.id = "z-" + z;
      var head = el("div", "zone-head");
      head.appendChild(el("strong", "zname", c.name));
      head.appendChild(el("span", "pill zpill", "—"));
      card.appendChild(head);
      card.appendChild(el("div", "zmode muted"));
      card.appendChild(el("div", "zreason"));
      card.appendChild(el("div", "zsched muted", schedText(sid, c)));
      var ctl = el("div", "zctl");
      var inp = el("input", "mins");
      inp.type = "number"; inp.min = "1";
      inp.max = sid === "irrigation" ? "120" : (sid === "heater" ? "240" : "1440");
      inp.placeholder = sid === "irrigation" || sid === "heater" ? "분" : "분(비우면 계속)";
      inp.value = sid === "irrigation" ? "10" : (sid === "heater" ? "60" : "");
      inp.setAttribute("aria-label", c.name + " 운전 시간(분)");
      ctl.appendChild(inp);
      function btn(text, cls, fn) { var b = el("button", "btn small " + cls, text); b.type = "button"; b.addEventListener("click", fn); ctl.appendChild(b); }
      btn(sid === "irrigation" ? "관수" : "켜기", "", function () {
        var m = inp.value === "" ? null : Number(inp.value);
        command({ action: "on", system: sid, zone: z, minutes: m });
      });
      btn(sid === "irrigation" ? "중지" : "끄기", "ghost", function () { command({ action: "off", system: sid, zone: z }); });
      if (sid !== "irrigation") btn("자동", "ghost", function () { command({ action: "auto", system: sid, zone: z }); });
      btn("설정", "ghost", function () { openEditor(sid, z); });
      card.appendChild(ctl);
      box.appendChild(card);
    });
  }

  function updateZones(sid) {
    var s = sys(sid);
    if (!s) return;
    var extra = "";
    if (sid === "irrigation") {
      extra = "동시 " + s.max_concurrent + "구역 · 주 펌프 " + (s.pump ? "켜짐" : "꺼짐") +
        (s.queue.length ? " · 대기: " + s.queue.map(function (z) { return z + "구역"; }).join(", ") : "");
    } else {
      extra = "켜짐 " + s.on_count + "/" + s.zones.length;
    }
    $("zones-extra").textContent = extra;
    s.zones.forEach(function (z) {
      var card = $("z-" + z.zone);
      if (!card) return;
      card.classList.toggle("on", z.on);
      card.classList.toggle("disabled", !z.enabled);
      var p = card.querySelector(".zpill");
      p.textContent = z.on ? (sid === "irrigation" ? "관수 중" : "켜짐") : (z.queued ? "대기" : "꺼짐");
      p.className = "pill zpill" + (z.on ? " open" : "");
      var mode = !z.enabled ? "사용 안 함" : (z.mode === "manual" ? "수동" : "자동");
      if (z.on) mode += " · " + dur(Date.now() / 1000 - z.since) + "째";
      if (sid === "irrigation" && z.next && !z.on) mode += " · 다음 " + mdhm(z.next);
      card.querySelector(".zmode").textContent = mode;
      card.querySelector(".zreason").textContent = z.reason || "";
    });
  }

  // ── 양액 ───────────────────────────────────────────────
  function buildNutrient() {
    var sp = spec("nutrient"), box = $("nut-channels");
    box.textContent = "";
    st.configs.nutrient.forEach(function (c, i) {
      var z = i + 1, dosing = sp.dosing_channels.indexOf(z) >= 0;
      var card = el("div", "card zone");
      card.id = "n-" + z;
      var head = el("div", "zone-head");
      head.appendChild(el("strong", "zname", c.name));
      head.appendChild(el("span", "pill zpill", "—"));
      card.appendChild(head);
      card.appendChild(el("div", "zreason"));
      var ctl = el("div", "zctl");
      var inp = el("input", "mins");
      inp.type = "number"; inp.min = "1"; inp.max = "120";
      inp.value = dosing ? "5" : "10";
      inp.placeholder = dosing ? "초" : "분";
      inp.setAttribute("aria-label", c.name + (dosing ? " 투입 시간(초)" : " 운전 시간(분)"));
      ctl.appendChild(inp);
      ctl.appendChild(el("span", "muted", dosing ? "초" : "분"));
      var on = el("button", "btn small", dosing ? "투입" : "켜기");
      on.type = "button";
      on.addEventListener("click", function () {
        var body = { action: "on", system: "nutrient", zone: z };
        body[dosing ? "seconds" : "minutes"] = Number(inp.value);
        command(body);
      });
      var off = el("button", "btn small ghost", "끄기");
      off.type = "button";
      off.addEventListener("click", function () { command({ action: "off", system: "nutrient", zone: z }); });
      ctl.appendChild(on);
      ctl.appendChild(off);
      card.appendChild(ctl);
      box.appendChild(card);
    });
  }

  function fillNutForm() {
    var f = $("nut-form"), n = st.nutrient;
    f.auto.checked = !!n.auto;
    [["ec_sensor", "ec"], ["ph_sensor", "ph"]].forEach(function (p) {
      var sel = f[p[0]];
      sel.textContent = "";
      var none = el("option", null, "(선택 안 함)"); none.value = ""; sel.appendChild(none);
      sensorsOf(p[1]).forEach(function (name) { var o = el("option", null, name); o.value = name; sel.appendChild(o); });
      sel.value = n[p[0]] || "";
    });
    ["ec_target", "ec_band", "ph_target", "ph_band", "dose_sec", "mix_sec", "max_doses_per_day"].forEach(function (k) {
      f[k].value = n[k];
    });
    nutFilled = true;
  }

  function updateNutrient() {
    var s = sys("nutrient");
    if (!s) return;
    $("nut-phase").textContent = "자동 투입: " + (s.auto ? "사용" : "꺼짐") + " · " + ((s.phase || {}).msg || "");
    s.zones.forEach(function (z) {
      var card = $("n-" + z.zone);
      if (!card) return;
      card.classList.toggle("on", z.on);
      var p = card.querySelector(".zpill");
      p.textContent = z.on ? "작동 중" : "꺼짐";
      p.className = "pill zpill" + (z.on ? " open" : "");
      card.querySelector(".zreason").textContent = z.reason || "";
    });
    api("GET", "/api/public/status").then(function (r) {
      var g = $("nut-now");
      g.textContent = "";
      (r.body.sensors || []).filter(function (x) { return x.type === "ec" || x.type === "ph"; }).forEach(function (x) {
        var c = el("div", "card");
        var l = el("div", "label", x.name + (x.type === "ec" ? " · EC" : " · pH"));
        if (x.simulated) l.appendChild(el("span", "tag sim", "모의값"));
        c.appendChild(l);
        var v = el("div", "value", x.value == null ? "—" : x.value.toFixed(2));
        if (x.type === "ec") v.appendChild(el("span", "unit", "mS/cm"));
        c.appendChild(v);
        g.appendChild(c);
      });
    });
  }

  $("nut-form").addEventListener("submit", function (e) {
    e.preventDefault();
    var f = this, cfg = { auto: f.auto.checked, ec_sensor: f.ec_sensor.value || null, ph_sensor: f.ph_sensor.value || null };
    ["ec_target", "ec_band", "ph_target", "ph_band", "dose_sec", "mix_sec", "max_doses_per_day"].forEach(function (k) {
      cfg[k] = Number(f[k].value);
    });
    api("POST", "/admin/api/control/config", { system: "nutrient", zone: 0, config: cfg }).then(function (r) {
      toast(r.status === 200 ? "양액 설정 저장됨" : (r.body.message || "저장 실패"), r.status !== 200);
      if (r.status === 200) { nutFilled = false; load(); }
    });
  });

  // ── 설정 편집 ──────────────────────────────────────────
  function schedRow(sid, s) {
    var row = el("div", "sched");
    var start = el("input"); start.type = "time"; start.value = s.start || "06:00"; start.className = "s-start";
    start.setAttribute("aria-label", "시작 시각");
    row.appendChild(start);
    if (sid === "irrigation") {
      var d = el("input"); d.type = "number"; d.min = "1"; d.max = "120"; d.value = s.duration_min || 5; d.className = "s-dur";
      d.setAttribute("aria-label", "관수 시간(분)");
      row.appendChild(d);
      row.appendChild(el("span", "muted", "분"));
    } else {
      row.appendChild(el("span", "muted", "~"));
      var end = el("input"); end.type = "time"; end.value = s.end || "18:00"; end.className = "s-end";
      end.setAttribute("aria-label", "끝 시각");
      row.appendChild(end);
    }
    var days = el("span", "days");
    DAYS.forEach(function (name, i) {
      var lab = el("label", "day");
      var cb = el("input"); cb.type = "checkbox"; cb.value = String(i);
      cb.checked = !s.days || s.days.indexOf(i) >= 0;
      lab.appendChild(cb);
      lab.appendChild(document.createTextNode(name));
      days.appendChild(lab);
    });
    row.appendChild(days);
    var en = el("label", "check");
    var ecb = el("input"); ecb.type = "checkbox"; ecb.className = "s-en"; ecb.checked = s.enabled !== false;
    en.appendChild(ecb); en.appendChild(document.createTextNode("사용"));
    row.appendChild(en);
    var del = el("button", "btn small ghost", "삭제"); del.type = "button";
    del.addEventListener("click", function () { row.remove(); });
    row.appendChild(del);
    return row;
  }

  function openEditor(sid, z) {
    var c = st.configs[sid][z - 1], f = $("ed-form"), sp = spec(sid);
    edit = { sid: sid, zone: z };
    $("ed-title").textContent = sp.name + " " + z + "구역 설정";
    $("ed-msg").textContent = "";
    f.elements.zname.value = c.name;
    f.enabled.checked = c.enabled !== false;
    $("ed-flow").hidden = sid !== "irrigation";
    if (sid === "irrigation") f.flow_lpm.value = c.flow_lpm || 0;
    $("ed-sched-help").textContent = sid === "irrigation" ? "시작 시각과 관수 시간(분). 여러 번 등록할 수 있습니다." : "켜 둘 시간대(시작~끝).";
    var box = $("ed-scheds");
    box.textContent = "";
    (c.schedules || []).forEach(function (s) { box.appendChild(schedRow(sid, s)); });
    var hasT = !!c.thermo;
    $("ed-thermo").hidden = !hasT;
    if (hasT) {
      $("ed-thermo-help").textContent = sid === "heater"
        ? "설정온도−편차 이하에서 켜고 설정온도에서 끕니다. 예: 18 °C / 1 °C → 17 °C 이하 켬, 18 °C 끔."
        : "설정온도+편차 이상에서 켜고 설정온도 이하에서 끕니다. 예: 28 °C / 1 °C → 29 °C 이상 켬, 28 °C 끔.";
      f.t_enabled.checked = !!c.thermo.enabled;
      var sel = f.t_sensor;
      sel.textContent = "";
      var none = el("option", null, "(선택 안 함)"); none.value = ""; sel.appendChild(none);
      sensorsOf("temperature").forEach(function (n) { var o = el("option", null, n); o.value = n; sel.appendChild(o); });
      sel.value = c.thermo.sensor || "";
      f.t_setpoint.value = c.thermo.setpoint;
      f.t_hysteresis.value = c.thermo.hysteresis;
      $("ed-cutoff").hidden = sid !== "heater";
      if (sid === "heater") f.cutoff.value = c.cutoff;
    }
    $("editor").showModal();
  }

  $("ed-add").addEventListener("click", function () {
    $("ed-scheds").appendChild(schedRow(edit.sid, {}));
  });
  $("ed-cancel").addEventListener("click", function () { $("editor").close(); });
  $("ed-save").addEventListener("click", function () {
    var f = $("ed-form"), sid = edit.sid;
    var cfg = { name: f.elements.zname.value, enabled: f.enabled.checked, schedules: [] };
    Array.prototype.forEach.call($("ed-scheds").children, function (row) {
      var s = { start: row.querySelector(".s-start").value, enabled: row.querySelector(".s-en").checked, days: [] };
      Array.prototype.forEach.call(row.querySelectorAll(".days input"), function (cb) { if (cb.checked) s.days.push(Number(cb.value)); });
      if (sid === "irrigation") s.duration_min = Number(row.querySelector(".s-dur").value);
      else s.end = row.querySelector(".s-end").value;
      cfg.schedules.push(s);
    });
    if (sid === "irrigation") cfg.flow_lpm = Number(f.flow_lpm.value || 0);
    if (!$("ed-thermo").hidden) {
      cfg.thermo = { enabled: f.t_enabled.checked, sensor: f.t_sensor.value || null,
                     setpoint: Number(f.t_setpoint.value), hysteresis: Number(f.t_hysteresis.value) };
      if (sid === "heater") cfg.cutoff = Number(f.cutoff.value);
    }
    api("POST", "/admin/api/control/config", { system: sid, zone: edit.zone, config: cfg }).then(function (r) {
      if (r.status !== 200) { $("ed-msg").textContent = r.body.message || "저장 실패 (" + r.status + ")"; return; }
      $("editor").close();
      toast("설정 저장됨 — 1초 안에 적용됩니다");
      built = {};
      load();
    });
  });

  // ── 리포트 ─────────────────────────────────────────────
  function loadReport() {
    $("rep-csv").href = "/admin/api/control/report.csv?days=" + repDays;
    api("GET", "/admin/api/control/report?days=" + repDays).then(function (r) {
      if (r.status === 200) renderReport(r.body);
    });
  }

  function renderReport(rep) {
    var names = rep.names || {}, sysName = {};
    st.systems.forEach(function (s) { sysName[s.id] = s.name; });
    // 일별 관수 시간 막대
    var per = {}, total = 0, liters = 0;
    rep.dates.forEach(function (d) { per[d] = 0; });
    rep.rows.forEach(function (x) {
      if (x.system !== "irrigation") return;
      per[x.date] = (per[x.date] || 0) + x.seconds;
      total += x.seconds; liters += x.liters;
    });
    $("rep-total").textContent = "합계 " + dur(total) + (liters ? " · " + liters.toFixed(1) + " L" : "");
    var W = 640, H = 160, P = 24, n = rep.dates.length, max = 60;
    rep.dates.forEach(function (d) { if (per[d] > max) max = per[d]; });
    var ns = "http://www.w3.org/2000/svg", svg = document.createElementNS(ns, "svg");
    svg.setAttribute("viewBox", "0 0 " + W + " " + (H + P));
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", "일별 관수 시간");
    var bw = (W - 2 * P) / n;
    rep.dates.forEach(function (d, i) {
      var h = per[d] / max * (H - P), x = P + i * bw;
      var rect = document.createElementNS(ns, "rect");
      rect.setAttribute("x", x + bw * 0.15); rect.setAttribute("width", Math.max(1, bw * 0.7));
      rect.setAttribute("y", H - h); rect.setAttribute("height", Math.max(0, h));
      rect.setAttribute("class", "bar");
      var tt = document.createElementNS(ns, "title"); tt.textContent = d + " · " + dur(per[d]);
      rect.appendChild(tt);
      svg.appendChild(rect);
      if (n <= 10 || i % Math.ceil(n / 8) === 0 || i === n - 1) {
        var t = document.createElementNS(ns, "text");
        t.setAttribute("x", x + bw / 2); t.setAttribute("y", H + 14); t.setAttribute("text-anchor", "middle");
        t.setAttribute("class", "axis"); t.textContent = d.slice(5).replace("-", "/");
        svg.appendChild(t);
      }
    });
    var box = $("rep-chart"); box.textContent = ""; box.appendChild(svg);

    // 구역별 합계
    var agg = {};
    rep.rows.forEach(function (x) {
      var k = x.system + ":" + x.zone, a = agg[k] || (agg[k] = { system: x.system, zone: x.zone, count: 0, seconds: 0, liters: 0 });
      a.count += x.count; a.seconds += x.seconds; a.liters += x.liters;
    });
    var order = ["irrigation", "nutrient", "lighting", "fan", "heater"];
    var rows = Object.keys(agg).map(function (k) { return agg[k]; }).sort(function (a, b) {
      return order.indexOf(a.system) - order.indexOf(b.system) || a.zone - b.zone;
    });
    table("rep-sum", ["시스템", "구역", "횟수", "운전 시간", "사용량"], rows.map(function (a) {
      return [sysName[a.system] || a.system, (names[a.system] || {})[a.zone] || a.zone, a.count + "회", dur(a.seconds),
              a.system === "irrigation" && a.liters ? a.liters.toFixed(1) + " L" : ""];
    }));
    table("rep-runs", ["시작", "시스템", "구역", "운전 시간", "방식", "결과", "사용량"], rep.runs.slice(0, 100).map(function (x) {
      return [mdhm(x.start_ts), sysName[x.system] || x.system, (names[x.system] || {})[x.zone] || x.zone,
              x.end_ts ? dur(x.duration_sec) : "진행 중", TRIG[x.trigger] || x.trigger, x.end_ts ? (RES[x.result] || x.result) : "",
              x.liters != null ? x.liters + " L" : ""];
    }));
  }

  function table(id, head, rows) {
    var t = $(id); t.textContent = "";
    var thead = el("thead"), tr = el("tr");
    head.forEach(function (h) { tr.appendChild(el("th", null, h)); });
    thead.appendChild(tr); t.appendChild(thead);
    var tb = el("tbody");
    if (!rows.length) { var r = el("tr"), c = el("td", "muted", "기록 없음"); c.colSpan = head.length; r.appendChild(c); tb.appendChild(r); }
    rows.forEach(function (row) {
      var r = el("tr");
      row.forEach(function (v) { r.appendChild(el("td", null, v == null || v === "" ? "—" : String(v))); });
      tb.appendChild(r);
    });
    t.appendChild(tb);
  }

  Array.prototype.forEach.call($("rep-days").children, function (b) {
    b.addEventListener("click", function () {
      repDays = Number(b.dataset.d);
      Array.prototype.forEach.call($("rep-days").children, function (x) { x.setAttribute("aria-pressed", x === b ? "true" : "false"); });
      loadReport();
    });
  });

  // ── 전체 렌더 ──────────────────────────────────────────
  function render() {
    if (!st) return;
    var s = st.status;
    var okState = s.state === "ok";
    $("ctl-state").dataset.state = s.paused ? "stale" : s.state;
    $("ctl-state-text").textContent = !okState ? "제어기 응답 없음" : (s.paused ? "전체 정지 중" : "자동 운전 중");
    $("pause-btn").hidden = !okState || s.paused;
    $("resume-btn").hidden = !okState || !s.paused;
    $("mock-notice").hidden = st.hardware_real;
    $("paused-notice").hidden = !s.paused;
    $("stale-notice").hidden = okState;
    $("denied-notice").hidden = st.control_allowed;
    if (tab === "overview") renderOverview();
    if (["irrigation", "lighting", "fan", "heater"].indexOf(tab) >= 0) {
      var sig = tab + JSON.stringify(st.configs[tab]);
      if (built.zones !== sig) { buildZones(tab); built.zones = sig; }
      updateZones(tab);
    }
    if (tab === "nutrient") {
      var nsig = JSON.stringify(st.configs.nutrient);
      if (built.nut !== nsig) { buildNutrient(); built.nut = nsig; }
      if (!nutFilled) fillNutForm();
      updateNutrient();
    }
  }

  function load() {
    return api("GET", "/admin/api/control/state").then(function (r) {
      if (r.status === 401) {
        stop();
        $("login-needed").hidden = false;
        $("ctl-view").hidden = true;
        return;
      }
      if (r.status !== 200) return;
      st = r.body;
      $("login-needed").hidden = true;
      $("ctl-view").hidden = false;
      render();
    }).catch(function () { $("ctl-state-text").textContent = "연결 끊김"; });
  }
  function start() { stop(); timer = setInterval(load, REFRESH_MS); }
  function stop() { if (timer) clearInterval(timer); timer = null; }

  $("pause-btn").addEventListener("click", function () {
    if (confirm("모든 출력을 끄고 스케줄·자동 운전을 멈춥니다. 계속할까요?")) command({ action: "pause" });
  });
  $("resume-btn").addEventListener("click", function () { command({ action: "resume" }); });
  document.addEventListener("visibilitychange", function () {
    if (document.hidden) stop(); else { load(); start(); }
  });

  buildTabs();
  var saved = null;
  try { saved = localStorage.getItem("ctl-tab"); } catch (e) { /* 무시 */ }
  load().then(function () {
    setTab(TABS.some(function (t) { return t[0] === saved; }) ? saved : "overview");
    start();
  });
})();
