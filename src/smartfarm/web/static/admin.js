"use strict";
(function () {
  var REFRESH_MS = 30000;
  var timer = null;

  function $(id) { return document.getElementById(id); }
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }
  function show(id) {
    ["login-view", "setup-view", "admin-view"].forEach(function (v) { $(v).hidden = v !== id; });
    $("logout").hidden = id !== "admin-view";
  }
  function bytes(n) {
    if (typeof n !== "number") return "—";
    var u = ["B", "KB", "MB", "GB", "TB"], i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return n.toFixed(i ? 1 : 0) + " " + u[i];
  }
  function dur(sec) {
    if (typeof sec !== "number") return "—";
    var d = Math.floor(sec / 86400), h = Math.floor(sec % 86400 / 3600), m = Math.floor(sec % 3600 / 60);
    return (d ? d + "일 " : "") + (d || h ? h + "시간 " : "") + m + "분";
  }
  function time(iso) {
    if (!iso) return "—";
    var d = new Date(iso);
    return isNaN(d) ? "—" : d.toLocaleString("ko-KR");
  }
  function api(method, url, body) {
    var opt = { method: method, cache: "no-store", credentials: "same-origin", headers: {} };
    if (body !== undefined) {
      opt.headers["Content-Type"] = "application/json";
      opt.body = JSON.stringify(body);
    }
    return fetch(url, opt).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) { return { status: r.status, body: j }; });
    });
  }

  // ── 표 ─────────────────────────────────────────────
  function table(id, head, rows) {
    var t = $(id);
    t.textContent = "";
    var thead = el("thead"), tr = el("tr");
    head.forEach(function (h) { tr.appendChild(el("th", null, h)); });
    thead.appendChild(tr);
    t.appendChild(thead);
    var tb = el("tbody");
    if (!rows.length) {
      var r = el("tr"), c = el("td", "muted", "없음");
      c.colSpan = head.length;
      r.appendChild(c);
      tb.appendChild(r);
    }
    rows.forEach(function (row) {
      var r = el("tr");
      row.forEach(function (v) { r.appendChild(el("td", null, v == null || v === "" ? "—" : String(v))); });
      tb.appendChild(r);
    });
    t.appendChild(tb);
  }

  function card(label, value, cls) {
    var c = el("div", "card" + (cls ? " " + cls : ""));
    c.appendChild(el("div", "label", label));
    c.appendChild(el("div", "value small", value));
    return c;
  }

  function render(d) {
    var sys = d.system || {}, db = d.db || {}, cfg = d.config || {}, sv = d.services || {};
    var st = d.status || {};
    var g = $("sys");
    g.textContent = "";
    Object.keys(sv).forEach(function (k) {
      g.appendChild(card(k, sv[k] === "active" ? "실행 중" : sv[k], sv[k] === "active" ? "" : "out"));
    });
    var stText = { ok: "정상 수신", stale: "갱신 중단", unknown: "확인 불가" }[st.state] || st.state;
    g.appendChild(card("센서 수신", stText, st.state === "ok" ? "" : "out"));
    g.appendChild(card("CPU 온도", sys.cpu_temp_c != null ? sys.cpu_temp_c + " °C" : "—", sys.cpu_temp_c >= 75 ? "out" : ""));
    g.appendChild(card("가동 시간", dur(sys.uptime_sec)));
    g.appendChild(card("부하 (1/5/15분)", sys.load ? sys.load.join(" / ") : "—"));
    g.appendChild(card("메모리 여유", bytes(sys.mem_available) + " / " + bytes(sys.mem_total)));
    g.appendChild(card("디스크 여유", bytes(sys.disk_free) + " / " + bytes(sys.disk_total)));
    g.appendChild(card("DB", (db.rows != null ? db.rows.toLocaleString() + "건 · " : "") + bytes(db.size_bytes)));
    g.appendChild(card("DB 기록 기간", time(db.first_ts) + " ~ " + time(db.last_ts)));

    var cur = {};
    (st.sensors || []).forEach(function (s) { cur[s.name] = s; });
    table("sensor-table", ["이름", "종류", "인터페이스", "주소", "채널", "현재값", "모의값"],
      (cfg.sensors || []).map(function (s) {
        var c = cur[s.name] || {};
        return [s.name, s.type, s.interface, s.address, s.channel, c.value, c.simulated ? "예" : (c.name ? "아니오" : "")];
      }));

    var vs = {};
    (st.valves || []).forEach(function (v) { vs[v.name] = v; });
    table("valve-table", ["이름", "역할", "GPIO (BCM)", "구동 방식", "현재 상태"],
      (cfg.valves || []).map(function (v) {
        var s = vs[v.name];
        return [v.name, v.role, v.pin, v.active_high ? "active-high" : "active-low", s ? (s.open ? "열림" : "닫힘") : ""];
      }));

    var rows = [["측정 주기", (cfg.sampling_interval_sec || "—") + " 초"]];
    Object.keys(cfg.rules || {}).forEach(function (k) {
      var r = cfg.rules[k] || {};
      rows.push(["기준: " + k, [r.min != null ? "최소 " + r.min : "", r.max != null ? "최대 " + r.max : ""].filter(Boolean).join(" · ")]);
    });
    Object.keys(cfg.alerts || {}).forEach(function (k) { rows.push(["알림: " + k, String(cfg.alerts[k])]); });
    table("rule-table", ["항목", "값"], rows);

    var log = $("logs");
    log.textContent = (d.logs || []).join("\n") || "(로그 없음)";
    log.scrollTop = log.scrollHeight;
    $("refreshed").textContent = "갱신 " + new Date().toLocaleTimeString("ko-KR");
  }

  function loadOverview() {
    return api("GET", "/admin/api/overview").then(function (r) {
      if (r.status === 401) { stopTimer(); return init(); }
      if (r.status === 200) render(r.body);
    });
  }
  function startTimer() { stopTimer(); timer = setInterval(loadOverview, REFRESH_MS); }
  function stopTimer() { if (timer) clearInterval(timer); timer = null; }

  var pinMode = false;
  function setPinMode(on) {
    pinMode = !!on;
    var i = $("password");
    $("pw-label").textContent = pinMode ? "PIN (숫자 4자리)" : "비밀번호";
    i.classList.toggle("pin", pinMode);
    if (pinMode) {
      i.setAttribute("inputmode", "numeric");
      i.setAttribute("maxlength", "4");
      i.setAttribute("pattern", "[0-9]{4}");
      i.setAttribute("autocomplete", "off");
    } else {
      i.removeAttribute("inputmode");
      i.removeAttribute("maxlength");
      i.removeAttribute("pattern");
      i.setAttribute("autocomplete", "current-password");
    }
  }

  function init() {
    return api("GET", "/admin/api/session").then(function (r) {
      var s = r.body || {};
      if (!s.configured) return show("setup-view");
      setPinMode(s.pin);
      if (!s.logged_in) {
        show("login-view");
        if (s.locked_sec) setMsg("로그인 시도가 너무 많아 잠겼습니다. " + waitText(s.locked_sec) + " 후 다시 시도하세요.");
        $("password").focus();
        return;
      }
      show("admin-view");
      loadOverview();
      startTimer();
    });
  }

  function setMsg(t) { $("login-msg").textContent = t || ""; }
  function waitText(sec) {
    var m = Math.ceil(sec / 60);
    return m >= 60 ? Math.floor(m / 60) + "시간 " + (m % 60 ? (m % 60) + "분" : "") : m + "분";
  }

  $("password").addEventListener("input", function () {
    if (!pinMode) return;
    var v = this.value.replace(/[^0-9]/g, "").slice(0, 4);
    if (v !== this.value) this.value = v;
    if (v.length === 4 && !$("login-btn").disabled) $("login-form").requestSubmit();
  });

  $("login-form").addEventListener("submit", function (e) {
    e.preventDefault();
    var pw = $("password").value;
    if (!pw) return;
    if (pinMode && !/^[0-9]{4}$/.test(pw)) return setMsg("숫자 4자리를 입력하세요.");
    $("login-btn").disabled = true;
    setMsg("확인 중…");
    api("POST", "/admin/api/login", { password: pw }).then(function (r) {
      $("login-btn").disabled = false;
      $("password").value = "";
      if (r.status === 200) { setMsg(""); return init(); }
      if (r.status === 429) return setMsg("로그인 시도가 너무 많아 잠겼습니다. " + waitText(r.body.locked_sec || 900) + " 후 다시 시도하세요.");
      if (r.status === 401) { $("password").focus(); return setMsg(pinMode ? "PIN 이 올바르지 않습니다." : "비밀번호가 올바르지 않습니다."); }
      if (r.status === 503) return init();
      setMsg("로그인 실패 (" + r.status + ")");
    }).catch(function () {
      $("login-btn").disabled = false;
      setMsg("서버에 연결할 수 없습니다.");
    });
  });

  $("logout").addEventListener("click", function () {
    stopTimer();
    api("POST", "/admin/api/logout", {}).then(init);
  });

  document.addEventListener("visibilitychange", function () {
    if (document.hidden) stopTimer();
    else if (!$("admin-view").hidden) { loadOverview(); startTimer(); }
  });

  init();
})();
