"use strict";
(function () {
  var REFRESH_MS = 2000;
  var KIND = { tcp: "Modbus TCP", rtu: "Modbus RTU", sim: "시뮬레이터" };
  var TYPE = { temperature: "온도", humidity: "습도", ph: "pH", ec: "EC" };
  var S = null, sig = "", timer = null, busEdit = null, devEdit = null, toastTimer = null;

  function $(id) { return document.getElementById(id); }
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }
  function btn(text, cls, fn) {
    var b = el("button", "btn small " + (cls || ""), text);
    b.type = "button";
    b.addEventListener("click", fn);
    return b;
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
    toastTimer = setTimeout(function () { t.textContent = ""; }, 6000);
  }
  function post(body, after) {
    return api("POST", "/admin/api/io", body).then(function (r) {
      if (r.status === 401) return location.reload();
      var ok = r.status === 200 || r.status === 202;
      toast(r.body.message || (ok ? "완료" : "실패 (" + r.status + ")"), !ok);
      if (ok && after) after(r.body);
      if (r.status === 200) load(true);
      return r;
    }).catch(function () { toast("서버에 연결할 수 없습니다.", true); });
  }
  function waitCmd(id, done) {
    var tries = 0;
    (function poll() {
      api("GET", "/admin/api/io/command?id=" + id).then(function (r) {
        var s = r.body.status;
        if (s === "done" || s === "error" || s === "expired") {
          toast(r.body.message || s, s !== "done");
          if (done) done(r.body);
          return;
        }
        if (++tries < 60) setTimeout(poll, 500);
        else toast("응답 시간 초과", true);
      });
    })();
  }
  function job(body, done) {
    post(body).then(function (r) { if (r && r.status === 202) waitCmd(r.body.command_id, done); });
  }
  function bus(id) { return S.config.buses.filter(function (b) { return b.id === id; })[0]; }
  function dev(id) { return S.config.devices.filter(function (d) { return d.id === id; })[0]; }
  function live(id) { return (S.live.devices || []).filter(function (d) { return d.id === id; })[0] || {}; }
  function kindOf(d) { return (S.profiles[d.profile] || {}).kind; }
  function chName(key) {
    var c = S.channels.filter(function (x) { return x[0] === key; })[0];
    return c ? c[1] : key;
  }
  function ago(ts) {
    if (!ts) return "—";
    var s = Math.round(Date.now() / 1000 - ts);
    return s < 60 ? s + "초 전" : Math.round(s / 60) + "분 전";
  }
  function table(t, head, rows) {
    t.textContent = "";
    var thead = el("thead"), tr = el("tr");
    head.forEach(function (h) { tr.appendChild(el("th", null, h)); });
    thead.appendChild(tr);
    t.appendChild(thead);
    var tb = el("tbody");
    if (!rows.length) {
      var r = el("tr"), c = el("td", "muted", "없음");
      c.colSpan = head.length; r.appendChild(c); tb.appendChild(r);
    }
    rows.forEach(function (row) {
      var r = el("tr");
      row.forEach(function (v) {
        var td = el("td");
        if (v instanceof Node) td.appendChild(v); else td.textContent = v == null || v === "" ? "—" : String(v);
        r.appendChild(td);
      });
      tb.appendChild(r);
    });
    t.appendChild(tb);
  }
  function pill(id, text, on) { var p = el("span", "pill" + (on ? " open" : ""), text); if (id) p.id = id; return p; }

  // ── 버스 ───────────────────────────────────────────────
  function renderBuses() {
    table($("bus-table"), ["이름", "종류", "주소", "장치 연결", "작업"], S.config.buses.map(function (b) {
      var p = b.params, where = b.kind === "tcp" ? p.host + ":" + p.port :
        (b.kind === "rtu" ? p.port + " · " + p.baudrate + "bps · " + p.parity + "/" + p.stopbits : "가상 장비");
      var acts = el("span", "row");
      acts.appendChild(btn("장치 검색", "", function () { scan(b); }));
      acts.appendChild(btn("수정", "ghost", function () { openBus(b); }));
      acts.appendChild(btn("삭제", "ghost", function () {
        if (confirm(b.name + " 버스와 소속 장치·할당을 모두 삭제합니다.")) post({ op: "delete_bus", id: b.id });
      }));
      var st = el("span"); st.id = "bus-st-" + b.id;
      return [b.name + (b.enabled ? "" : " (사용 안 함)"), KIND[b.kind], where, st, acts];
    }));
  }
  function scan(b) {
    var range = prompt("검색할 국번 범위 (최대 32개)", "1-16");
    if (!range) return;
    var m = /^(\d+)\s*-\s*(\d+)$/.exec(range.trim());
    if (!m) return toast("예: 1-16 형식으로 입력하세요", true);
    $("scan-result").textContent = "검색 중… (" + b.name + ")";
    job({ op: "scan", bus_id: b.id, start: Number(m[1]), end: Number(m[2]) }, function (res) {
      var box = $("scan-result");
      box.textContent = "";
      var card = el("div", "card");
      card.appendChild(el("strong", null, b.name + " 검색 결과: " + (res.message || "")));
      (res.found || []).forEach(function (f) {
        var row = el("div", "row");
        var used = S.config.devices.some(function (d) { return d.bus_id === b.id && d.unit === f.unit; });
        row.appendChild(el("span", null, "국번 " + f.unit + " 응답 (FC" + ("0" + f.fn).slice(-2) + (f.exception ? ", 예외 응답" : "") + ")"));
        if (used) row.appendChild(el("span", "muted", "등록됨"));
        else row.appendChild(btn("장치로 등록", "", function () { openDev(null, { bus_id: b.id, unit: f.unit }); }));
        card.appendChild(row);
      });
      if (!(res.found || []).length) card.appendChild(el("p", "muted", "응답한 장치가 없습니다. 배선(A/B), 통신 속도, 종단저항, 전원을 확인하세요."));
      box.appendChild(card);
    });
  }
  function openBus(b) {
    busEdit = b ? b.id : null;
    var f = $("bus-form"), p = (b && b.params) || {};
    $("bus-title").textContent = b ? "버스 수정" : "버스 추가";
    $("bus-msg").textContent = "";
    f.elements.bname.value = b ? b.name : "";
    f.kind.value = b ? b.kind : "tcp";
    f.host.value = p.host || "";
    f.port.value = p.port || 502;
    f.sport.value = (b && b.kind === "rtu" && p.port) || "/dev/ttyUSB0";
    f.baudrate.textContent = "";
    S.bauds.forEach(function (v) { var o = el("option", null, String(v)); o.value = v; f.baudrate.appendChild(o); });
    f.baudrate.value = p.baudrate || 9600;
    f.parity.value = p.parity || "N";
    f.stopbits.value = String(p.stopbits || 1);
    f.timeout.value = p.timeout || 0.5;
    f.enabled.checked = b ? b.enabled : true;
    busKind();
    $("bus-dlg").showModal();
  }
  function busKind() {
    var k = $("bus-form").kind.value;
    document.querySelectorAll("#bus-form .k-tcp").forEach(function (x) { x.hidden = k !== "tcp"; });
    document.querySelectorAll("#bus-form .k-rtu").forEach(function (x) { x.hidden = k !== "rtu"; });
  }
  $("bus-form").kind.addEventListener("change", busKind);
  $("add-bus").addEventListener("click", function () { openBus(null); });
  $("bus-save").addEventListener("click", function () {
    var f = $("bus-form"), k = f.kind.value;
    var b = { name: f.elements.bname.value, kind: k, timeout: Number(f.timeout.value), enabled: f.enabled.checked };
    if (k === "tcp") { b.host = f.host.value.trim(); b.port = Number(f.port.value); }
    if (k === "rtu") { b.port = f.sport.value.trim(); b.baudrate = Number(f.baudrate.value); b.parity = f.parity.value; b.stopbits = Number(f.stopbits.value); }
    api("POST", "/admin/api/io", { op: "save_bus", id: busEdit, bus: b }).then(function (r) {
      if (r.status !== 200) { $("bus-msg").textContent = r.body.message || "저장 실패 (" + r.status + ")"; return; }
      $("bus-dlg").close(); toast(r.body.message); load(true);
    });
  });

  // ── 장치 ───────────────────────────────────────────────
  function renderDevices() {
    table($("dev-table"), ["이름", "버스", "국번", "프로파일", "상태", "마지막 응답", "오류", "작업"], S.config.devices.map(function (d) {
      var b = bus(d.bus_id) || {}, acts = el("span", "row");
      acts.appendChild(btn("수정", "ghost", function () { openDev(d); }));
      acts.appendChild(btn("삭제", "ghost", function () {
        if (confirm(d.name + " 장치와 그 할당을 삭제합니다.")) post({ op: "delete_device", id: d.id });
      }));
      var st = el("span"); st.id = "dev-st-" + d.id;
      var lo = el("span"); lo.id = "dev-lo-" + d.id;
      var er = el("span", "err"); er.id = "dev-er-" + d.id;
      return [d.name + (d.enabled ? "" : " (사용 안 함)"), b.name, d.unit, (S.profiles[d.profile] || {}).name || d.profile, st, lo, er, acts];
    }));
  }
  function openDev(d, preset) {
    devEdit = d ? d.id : null;
    var f = $("dev-form"), p = preset || {};
    $("dev-title").textContent = d ? "장치 수정" : "장치 추가";
    $("dev-msg").textContent = "";
    f.bus_id.textContent = "";
    S.config.buses.forEach(function (b) { var o = el("option", null, b.name + " (" + KIND[b.kind] + ")"); o.value = b.id; f.bus_id.appendChild(o); });
    if (!S.config.buses.length) return toast("먼저 버스를 추가하세요", true);
    f.profile.textContent = "";
    Object.keys(S.profiles).forEach(function (k) { var o = el("option", null, S.profiles[k].name); o.value = k; f.profile.appendChild(o); });
    f.elements.dname.value = d ? d.name : "";
    f.bus_id.value = d ? d.bus_id : (p.bus_id || S.config.buses[0].id);
    f.unit.value = d ? d.unit : (p.unit || 1);
    f.profile.value = d ? d.profile : "relay_coil";
    f.enabled.checked = d ? d.enabled : true;
    fillOpts(d && d.profile === f.profile.value ? d.options : null);
    $("dev-dlg").showModal();
  }
  function fillOpts(opts) {
    var f = $("dev-form"), pr = S.profiles[f.profile.value], relay = pr.kind === "relay";
    $("dev-note").textContent = pr.note || "";
    $("relay-opts").hidden = !relay;
    if (!relay) return;
    var o = Object.assign({}, pr.options, opts || {});
    ["channels", "base", "on_value", "off_value"].forEach(function (k) { if (o[k] != null) f[k].value = o[k]; });
    f.write.value = o.write;
    f.readback.value = o.readback;
    regOpts();
  }
  function regOpts() {
    var reg = $("dev-form").write.value === "register";
    document.querySelectorAll("#dev-form .o-reg").forEach(function (x) { x.hidden = !reg; });
  }
  $("dev-form").profile.addEventListener("change", function () { fillOpts(null); });
  $("dev-form").write.addEventListener("change", regOpts);
  $("add-dev").addEventListener("click", function () { openDev(null); });
  $("dev-save").addEventListener("click", function () {
    var f = $("dev-form"), pr = S.profiles[f.profile.value];
    var d = { name: f.elements.dname.value, bus_id: Number(f.bus_id.value), unit: Number(f.unit.value),
              profile: f.profile.value, enabled: f.enabled.checked, options: {} };
    if (pr.kind === "relay") {
      d.options = { channels: Number(f.channels.value), write: f.write.value, base: Number(f.base.value), readback: f.readback.value };
      if (f.write.value === "register") { d.options.on_value = Number(f.on_value.value); d.options.off_value = Number(f.off_value.value); }
    }
    api("POST", "/admin/api/io", { op: "save_device", id: devEdit, device: d }).then(function (r) {
      if (r.status !== 200) { $("dev-msg").textContent = r.body.message || "저장 실패 (" + r.status + ")"; return; }
      $("dev-dlg").close(); toast(r.body.message); load(true);
    });
  });

  // ── 출력 할당 ──────────────────────────────────────────
  function renderRelays() {
    var box = $("relays"), owner = {};
    box.textContent = "";
    Object.keys(S.config.map).forEach(function (ch) { var m = S.config.map[ch]; owner[m.device_id + ":" + m.idx] = ch; });
    var relays = S.config.devices.filter(function (d) { return kindOf(d) === "relay"; });
    if (!relays.length) box.appendChild(el("p", "muted", "릴레이 장치가 없습니다. 장치를 추가하세요."));
    relays.forEach(function (d) {
      var card = el("div", "card relay-card");
      var head = el("div", "zone-head");
      head.appendChild(el("strong", null, d.name + " · " + (bus(d.bus_id) || {}).name + " · 국번 " + d.unit));
      head.appendChild(pill("rl-st-" + d.id, "—"));
      card.appendChild(head);
      var t = el("table");
      var rows = [];
      for (var i = 0; i < d.options.channels; i++) {
        (function (idx) {
          var sel = el("select");
          sel.setAttribute("aria-label", d.name + " " + (idx + 1) + "번 릴레이 할당");
          var none = el("option", null, "(할당 안 함)"); none.value = ""; sel.appendChild(none);
          S.channels.forEach(function (c) {
            var m = S.config.map[c[0]], here = m && m.device_id === d.id && m.idx === idx;
            var other = m && !here ? " — 현재 " + ((dev(m.device_id) || {}).name || "?") + " #" + (m.idx + 1) : "";
            var o = el("option", null, c[1] + other); o.value = c[0];
            sel.appendChild(o);
          });
          sel.value = owner[d.id + ":" + idx] || "";
          sel.addEventListener("change", function () {
            post({ op: "set_map", device_id: d.id, idx: idx, channel: sel.value || null });
          });
          var stc = el("span", "pill", "—"); stc.id = "rl-" + d.id + "-" + idx;
          var test = btn("시험", "ghost test-btn", function () {
            var sec = 3;
            job({ op: "pulse", device_id: d.id, idx: idx, seconds: sec });
          });
          rows.push([String(idx + 1), stc, sel, test]);
        })(i);
      }
      table(t, ["릴레이", "실제 상태", "할당", "시험(3초)"], rows);
      var wrap = el("div", "table-wrap"); wrap.appendChild(t);
      card.appendChild(wrap);
      box.appendChild(card);
    });
    var mapped = Object.keys(S.config.map).length;
    $("map-count").textContent = "할당 " + mapped + " / " + S.channels.length + "개 출력";
    var un = S.channels.filter(function (c) { return !S.config.map[c[0]]; });
    $("unassigned-count").textContent = "(" + un.length + "개 — 시뮬레이션으로 동작)";
    $("unassigned").textContent = un.map(function (c) { return c[1]; }).join(", ") || "없음";
  }

  // ── 센서 할당 ──────────────────────────────────────────
  function renderSensors() {
    var devs = S.config.devices.filter(function (d) { return kindOf(d) === "sensor"; });
    var g = $("sensor-devs");
    g.textContent = "";
    devs.forEach(function (d) {
      var c = el("div", "card");
      var l = el("div", "label", d.name + " · 국번 " + d.unit);
      l.appendChild(pill("sd-st-" + d.id, "—"));
      c.appendChild(l);
      var v = el("div", "value small"); v.id = "sd-v-" + d.id;
      c.appendChild(v);
      g.appendChild(c);
    });
    table($("sensor-table"), ["센서 이름", "장치", "종류", "레지스터", "현재값", ""], S.config.sensors.map(function (s) {
      var d = dev(s.device_id) || {}, p = s.point;
      var val = el("span"); val.id = "sv-" + s.name;
      return [s.name, d.name, TYPE[p.type] || p.type, "FC0" + p.fn + " · " + p.addr + "번지 × " + p.scale + (p.signed ? " (부호)" : ""), val,
              btn("삭제", "ghost", function () { if (confirm(s.name + " 센서 할당을 삭제합니다.")) post({ op: "delete_sensor", name: s.name }); })];
    }));
    var rf = $("read-form").device_id;
    var cur = rf.value;
    rf.textContent = "";
    S.config.devices.forEach(function (d) { var o = el("option", null, d.name + " (국번 " + d.unit + ")"); o.value = d.id; rf.appendChild(o); });
    if (cur) rf.value = cur;
  }
  function openSensor() {
    var devs = S.config.devices.filter(function (d) { return kindOf(d) === "sensor"; });
    if (!devs.length) return toast("먼저 센서 장치(온습도 센서 등)를 추가하세요", true);
    var f = $("sen-form");
    $("sen-msg").textContent = "";
    f.device_id.textContent = "";
    devs.forEach(function (d) { var o = el("option", null, d.name); o.value = d.id; f.device_id.appendChild(o); });
    f.type.textContent = "";
    S.sensor_types.forEach(function (t) { var o = el("option", null, TYPE[t] || t); o.value = t; f.type.appendChild(o); });
    f.elements.sname.value = "";
    fillPoints();
    $("sen-dlg").showModal();
  }
  function fillPoints() {
    var f = $("sen-form"), d = dev(Number(f.device_id.value)), pts = (S.profiles[d.profile] || {}).points || {};
    f.pt.textContent = "";
    Object.keys(pts).forEach(function (k) {
      var p = pts[k], o = el("option", null, (TYPE[p.type] || p.type) + " (FC0" + p.fn + " " + p.addr + "번지)");
      o.value = k; f.pt.appendChild(o);
    });
    var c = el("option", null, "직접 입력"); c.value = ""; f.pt.appendChild(c);
    customPt();
  }
  function customPt() { $("custom-pt").hidden = $("sen-form").pt.value !== ""; }
  $("sen-form").device_id.addEventListener("change", fillPoints);
  $("sen-form").pt.addEventListener("change", customPt);
  $("add-sensor").addEventListener("click", openSensor);
  $("sen-save").addEventListener("click", function () {
    var f = $("sen-form"), d = dev(Number(f.device_id.value)), point;
    if (f.pt.value) point = S.profiles[d.profile].points[f.pt.value];
    else point = { type: f.type.value, fn: Number(f.fn.value), addr: Number(f.addr.value), scale: Number(f.scale.value), signed: f.signed.checked };
    api("POST", "/admin/api/io", { op: "save_sensor", name: f.elements.sname.value.trim(), device_id: d.id, point: point }).then(function (r) {
      if (r.status !== 200) { $("sen-msg").textContent = r.body.message || "저장 실패"; return; }
      $("sen-dlg").close(); toast(r.body.message); load(true);
    });
  });
  $("read-form").addEventListener("submit", function (e) {
    e.preventDefault();
    var f = this;
    $("read-out").textContent = "읽는 중…";
    job({ op: "read", device_id: Number(f.device_id.value), fn: Number(f.fn.value), addr: Number(f.addr.value), count: Number(f.count.value) },
      function (res) {
        $("read-out").textContent = res.values ? res.values.map(function (v, i) { return (Number(f.addr.value) + i) + ": " + v; }).join(" · ") : (res.message || "");
      });
  });

  // ── 실시간 갱신 ────────────────────────────────────────
  function updateLive() {
    var ok = S.live.state === "ok";
    $("io-state").dataset.state = ok ? (S.paused ? "stale" : "ok") : "error";
    $("io-state-text").textContent = !ok ? "I/O 관리자 응답 없음" : (S.paused ? "전체 정지 · 시운전 가능" : "자동 운전 중");
    $("pause-btn").hidden = S.paused;
    $("resume-btn").hidden = !S.paused;
    $("real-notice").hidden = !S.real || S.control_allowed;
    document.querySelectorAll(".test-btn").forEach(function (b) {
      b.disabled = !S.paused;
      b.title = S.paused ? "" : "전체 정지 상태에서만 시험할 수 있습니다";
    });
    S.config.buses.forEach(function (b) {
      var ds = S.config.devices.filter(function (d) { return d.bus_id === b.id && d.enabled; });
      var on = ds.filter(function (d) { return live(d.id).online; }).length;
      var x = $("bus-st-" + b.id);
      if (x) x.textContent = ds.length ? on + " / " + ds.length : "—";
    });
    S.config.devices.forEach(function (d) {
      var L = live(d.id);
      var st = $("dev-st-" + d.id);
      if (st) { st.textContent = ""; st.appendChild(pill(null, !d.enabled ? "사용 안 함" : (L.online ? "연결됨" : (L.ever_online ? "끊김" : "응답 없음")), L.online)); }
      var lo = $("dev-lo-" + d.id); if (lo) lo.textContent = ago(L.last_ok);
      var er = $("dev-er-" + d.id); if (er) er.textContent = L.online ? "" : (L.error || "");
      var rs = $("rl-st-" + d.id);
      if (rs) { rs.textContent = L.online ? "연결됨" : "연결 안 됨"; rs.className = "pill" + (L.online ? " open" : ""); }
      for (var i = 0; L.states && i < L.states.length; i++) {
        var c = $("rl-" + d.id + "-" + i);
        if (!c) continue;
        var pulse = (L.pulse || []).indexOf(i) >= 0;
        c.textContent = L.states[i] ? (pulse ? "시험 중" : "켜짐") : "꺼짐";
        c.className = "pill" + (L.states[i] ? " open" : "");
      }
      var sv = $("sd-v-" + d.id);
      if (sv) {
        var pts = L.points || {};
        sv.textContent = Object.keys(pts).map(function (k) { return k + " " + pts[k]; }).join(" · ") || (L.online ? "측정점 없음" : "—");
        var sp = $("sd-st-" + d.id);
        sp.textContent = L.online ? "연결됨" : "연결 안 됨"; sp.className = "pill" + (L.online ? " open" : "");
      }
      Object.keys(L.values || {}).forEach(function (n) { var x = $("sv-" + n); if (x) x.textContent = String(L.values[n]); });
    });
  }

  function load(force) {
    return api("GET", "/admin/api/io/state").then(function (r) {
      if (r.status === 401) {
        stop(); $("login-needed").hidden = false; $("io-view").hidden = true; return;
      }
      if (r.status !== 200) return;
      S = r.body;
      $("login-needed").hidden = true;
      $("io-view").hidden = false;
      var s = JSON.stringify(S.config);
      if (force || s !== sig) {
        sig = s;
        renderBuses(); renderDevices(); renderRelays(); renderSensors();
      }
      updateLive();
    }).catch(function () { $("io-state-text").textContent = "연결 끊김"; });
  }
  function start() { stop(); timer = setInterval(function () { load(false); }, REFRESH_MS); }
  function stop() { if (timer) clearInterval(timer); timer = null; }

  function control(action) {
    api("POST", "/admin/api/control/command", { action: action }).then(function (r) {
      toast(r.body.message || "", r.status !== 200);
      load(false);
    });
  }
  $("pause-btn").addEventListener("click", function () {
    if (confirm("모든 출력을 끄고 자동 운전을 멈춥니다(시운전 모드). 계속할까요?")) control("pause");
  });
  $("resume-btn").addEventListener("click", function () { control("resume"); });
  document.querySelectorAll("[data-close]").forEach(function (b) {
    b.addEventListener("click", function () { b.closest("dialog").close(); });
  });
  document.addEventListener("visibilitychange", function () { if (document.hidden) stop(); else { load(false); start(); } });

  load(true).then(start);
})();
