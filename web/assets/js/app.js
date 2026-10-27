/* SAHM | شهم — web app (no build step). */
(function () {
  "use strict";

  // ---------- utilities ----------
  var $ = function (sel, root) { return (root || document).querySelector(sel); };
  var $$ = function (sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); };
  function esc(t) { return String(t == null ? "" : t).replace(/[&<>"']/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]; }); }
  function fmtDate(ts, withTime) {
    if (!ts) return "";
    var d = new Date(ts * 1000);
    var o = withTime === false ? { dateStyle: "medium" } : { dateStyle: "medium", timeStyle: "short" };
    try { return d.toLocaleString(LANG === "ar" ? "ar-SA-u-ca-gregory-nu-latn" : "en-GB", Object.assign({ timeZone: "Asia/Riyadh" }, o)); } catch (e) { return d.toLocaleString(); }
  }
  function ago(ts) {
    var s = Math.floor(Date.now() / 1000) - ts;
    if (s < 60) return t("الآن", "now");
    if (s < 3600) return Math.floor(s / 60) + t(" د", "m");
    if (s < 86400) return Math.floor(s / 3600) + t(" س", "h");
    return fmtDate(ts, false);
  }
  function localInput(ts) { // epoch → value for <input type=datetime-local> in Riyadh time
    var d = new Date((ts + 3 * 3600) * 1000);
    return d.toISOString().slice(0, 16);
  }

  var LANG = (function () { try { return localStorage.getItem("sahm_lang") || "ar"; } catch (e) { return "ar"; } })();
  function t(ar, en) { return LANG === "en" && en ? en : ar; }
  function applyLang() {
    document.documentElement.lang = LANG;
    document.documentElement.dir = LANG === "ar" ? "rtl" : "ltr";
  }
  applyLang();

  function toast(msg, bad) {
    var el = document.createElement("div");
    el.className = "toast" + (bad ? " bad" : "");
    el.textContent = msg;
    document.body.appendChild(el);
    setTimeout(function () { el.remove(); }, 4200);
  }

  function api(method, path, body, opts) {
    opts = opts || {};
    var init = { method: method, headers: { "x-sahm": "1" }, credentials: "same-origin" };
    if (body instanceof FormData) init.body = body;
    else if (body !== undefined) { init.headers["content-type"] = "application/json"; init.body = JSON.stringify(body); }
    return fetch(path, init).then(function (r) {
      if (opts.raw) return r;
      return r.text().then(function (txt) {
        var data = null;
        try { data = txt ? JSON.parse(txt) : null; } catch (e) { data = { message: txt }; }
        if (r.status === 401 && !opts.allow401) { STATE.me = null; go("#/login"); throw { status: 401 }; }
        if (!r.ok) {
          var err = { status: r.status, code: data && data.error, message: (data && data.message) || t("حدث خطأ، حاول مرة أخرى.", "Something went wrong.") };
          throw err;
        }
        return data;
      });
    });
  }
  function fail(e) { if (e && e.status !== 401) toast(e.message || t("حدث خطأ", "Error"), true); }

  // ---------- markdown (safe subset) ----------
  function md(src) {
    var blocks = [];
    var text = String(src || "").replace(/```(\w*)\n?([\s\S]*?)```/g, function (_, lang, code) {
      blocks.push('<pre class="code">' + esc(code.replace(/\n$/, "")) + "</pre>");
      return "\u0000" + (blocks.length - 1) + "\u0000";
    });
    var out = [], list = null;
    esc(text).split("\n").forEach(function (line) {
      var ul = line.match(/^\s*[-•*]\s+(.*)/), ol = line.match(/^\s*\d+[.)]\s+(.*)/);
      if (ul || ol) {
        var kind = ul ? "ul" : "ol";
        if (list !== kind) { if (list) out.push("</" + list + ">"); out.push("<" + kind + ">"); list = kind; }
        out.push("<li>" + (ul || ol)[1] + "</li>");
      } else {
        if (list) { out.push("</" + list + ">"); list = null; }
        if (line.trim()) out.push("<p>" + line.replace(/^#{1,6}\s*/, "") + "</p>");
      }
    });
    if (list) out.push("</" + list + ">");
    return out.join("")
      .replace(/\*\*(.+?)\*\*/g, "<b>$1</b>")
      .replace(/`([^`]+)`/g, '<code class="code" style="display:inline;padding:1px 5px">$1</code>')
      .replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g, '$1<a href="$2" target="_blank" rel="noopener noreferrer">$2</a>')
      .replace(/\u0000(\d+)\u0000/g, function (_, i) { return blocks[+i]; });
  }

  // ---------- state ----------
  var STATE = { me: null, personas: [], approvals: 0, unread: 0 };
  var PERS = {};
  function persona(id) { return PERS[id] || { id: id, name: id, role: "", color: "#888" }; }
  function avatar(id, cls) {
    var p = persona(id);
    if (id === "me") return '<span class="av ' + (cls || "") + '" style="display:inline-grid;place-items:center;background:var(--bg3);font-size:12px">' + t("أنت", "You") + "</span>";
    return '<img class="av ' + (cls || "") + '" style="--c:' + p.color + '" src="/assets/img/team/' + id + '.jpg" alt="">';
  }

  var ICONS = {
    home: '<path d="M3 11l9-7 9 7v9a1 1 0 0 1-1 1h-5v-6h-6v6H4a1 1 0 0 1-1-1z"/>',
    team: '<circle cx="9" cy="8" r="3.2"/><circle cx="17" cy="9" r="2.6"/><path d="M3 20c0-3.3 2.7-6 6-6s6 2.7 6 6M14 20c.3-2.6 2-4.6 4.5-4.6 2 0 3.5 1.6 3.5 4.6"/>',
    check: '<path d="M4 12l5 5L20 6"/>',
    folder: '<path d="M3 6h6l2 2h10v11H3z"/>',
    pen: '<path d="M4 20h4L19 9l-4-4L4 16z"/>',
    globe: '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c3 3 3 15 0 18M12 3c-3 3-3 15 0 18"/>',
    bolt: '<path d="M13 2L4 14h7l-1 8 9-12h-7z"/>',
    link: '<path d="M10 14a4 4 0 0 0 6 0l3-3a4 4 0 0 0-6-6l-1 1M14 10a4 4 0 0 0-6 0l-3 3a4 4 0 0 0 6 6l1-1"/>',
    users: '<circle cx="12" cy="8" r="3.5"/><path d="M5 20c0-3.9 3.1-7 7-7s7 3.1 7 7"/>',
    store: '<path d="M4 9l2-5h12l2 5M4 9v11h16V9M4 9h16M9 20v-6h6v6"/>',
    card: '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M3 10h18"/>',
    gear: '<circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3M5 5l2 2M17 17l2 2M5 19l2-2M17 7l2-2"/>',
    bell: '<path d="M6 16V11a6 6 0 0 1 12 0v5l2 2H4zM10 20a2 2 0 0 0 4 0"/>',
    menu: '<path d="M4 6h16M4 12h16M4 18h16"/>'
  };
  function icon(n) { return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">' + (ICONS[n] || "") + "</svg>"; }
  var LOGO = '<svg style="direction:ltr" viewBox="0 0 168 64" role="img" aria-label="SAHM شهم"><defs><linearGradient id="lgA" x1="0" y1=".7" x2="1" y2=".3"><stop offset=".15" stop-color="#34d399"/><stop offset=".6" stop-color="#8b5cf6"/><stop offset="1" stop-color="#6d3df5"/></linearGradient></defs><path d="M40 2 12 36h15L19 62 50 24H34z" fill="url(#lgA)"/><text x="60" y="28" fill="#fff" font-family="IBM Plex Sans Arabic,sans-serif" font-size="25" font-weight="600" letter-spacing="1.5">SAHM</text><text x="60" y="58" fill="#fff" font-family="IBM Plex Sans Arabic,sans-serif" font-size="25" font-weight="600" direction="ltr">شهم</text></svg>';

  var NAV = [
    ["#/", "home", "الرئيسية", "Home"],
    ["#/team", "team", "فريق شهم", "SAHM Team"],
    ["#/approvals", "check", "بانتظار موافقتك", "Approvals"],
    ["sep", "", "التنفيذ", "Work"],
    ["#/studio", "pen", "استوديو سلاف", "Sulaf's Studio"],
    ["#/projects", "folder", "مشاريع نجد", "Najd's Projects"],
    ["#/sites", "globe", "مواقع ساري", "Sari's Sites"],
    ["#/automations", "bolt", "أتمتة سند", "Sanad's Automations"],
    ["sep", "", "الحساب", "Account"],
    ["#/channels", "link", "الحسابات المربوطة", "Connections"],
    ["#/contacts", "users", "العملاء", "Contacts"],
    ["#/profile", "store", "ملف النشاط", "Business profile"],
    ["#/billing", "card", "الاشتراك", "Billing"],
    ["#/settings", "gear", "الإعدادات", "Settings"]
  ];

  // ---------- router ----------
  function go(h) { if (location.hash !== h) location.hash = h; else route(); }
  function parse() {
    var h = location.hash.replace(/^#/, "") || "/";
    var q = {}, i = h.indexOf("?");
    if (i >= 0) { h.slice(i + 1).split("&").forEach(function (kv) { var p = kv.split("="); if (p[0]) q[decodeURIComponent(p[0])] = decodeURIComponent(p[1] || ""); }); h = h.slice(0, i); }
    return { parts: h.split("/").filter(Boolean), q: q };
  }

  var cleanup = null;
  function route() {
    if (cleanup) { try { cleanup(); } catch (e) {} cleanup = null; }
    var r = parse(), p = r.parts;
    if (!STATE.me) {
      if (p[0] === "register") return pageAuth("register", r.q);
      return pageAuth("login", r.q);
    }
    if (p[0] === "login" || p[0] === "register") return go(r.q.next || "#/");
    var pages = {
      "": pageDashboard, team: pageTeam, chat: pageChat, approvals: pageApprovals, projects: pageProjects, studio: pageStudio,
      sites: pageSites, automations: pageAutomations, channels: pageChannels, contacts: pageContacts, profile: pageProfile,
      billing: pageBilling, settings: pageSettings
    };
    var fn = pages[p[0] || ""] || pageDashboard;
    shell(p[0] || "");
    fn(p, r.q);
  }

  function shell(active) {
    if (!$(".shell")) {
      $("#root").innerHTML =
        '<div class="shell"><aside class="side" id="side"><a class="brand" href="#/">' + LOGO + '</a><nav class="nav" id="nav"></nav>' +
        '<div style="margin-top:auto;padding:14px 8px 4px" class="small muted" id="planbox"></div></aside>' +
        '<div class="main"><header class="top"><button class="iconbtn mnav" id="menuBtn" aria-label="menu">' + icon("menu") + '</button>' +
        '<div class="title" id="ttl"></div><div class="sp"></div>' +
        '<button class="iconbtn" id="langBtn" title="Language" style="width:auto;padding:0 10px;font-size:13px">' + (LANG === "ar" ? "EN" : "ع") + "</button>" +
        '<button class="iconbtn" id="bellBtn" aria-label="notifications">' + icon("bell") + '<span class="dot hidden" id="bellDot"></span></button></header>' +
        '<div id="view"></div></div></div>';
      $("#menuBtn").onclick = function () { $("#side").classList.toggle("open"); };
      $("#langBtn").onclick = function () { LANG = LANG === "ar" ? "en" : "ar"; try { localStorage.setItem("sahm_lang", LANG); } catch (e) {} applyLang(); $("#root").innerHTML = ""; route(); };
      $("#bellBtn").onclick = openNotifications;
      refreshCounts();
    }
    $("#side").classList.remove("open");
    $("#nav").innerHTML = NAV.map(function (n) {
      if (n[0] === "sep") return '<div class="sep"></div><div class="lbl">' + t(n[2], n[3]) + "</div>";
      var key = n[0].replace("#/", "");
      var on = key === active || (key === "team" && active === "chat");
      var badge = key === "approvals" && STATE.approvals ? '<span class="badge">' + STATE.approvals + "</span>" : "";
      return '<a href="' + n[0] + '" class="' + (on ? "on" : "") + '">' + icon(n[1]) + "<span>" + t(n[2], n[3]) + "</span>" + badge + "</a>";
    }).join("");
    $("#planbox").innerHTML = esc(STATE.me.name) + "<br>" + t("باقة", "Plan") + ' <a href="#/billing">' + esc(STATE.me.plan_name) + "</a>";
  }
  function setTitle(s) { var el = $("#ttl"); if (el) el.textContent = s; document.title = s + " — شهم | SAHM"; }
  function view(html) { var v = $("#view"); v.innerHTML = html; v.scrollTop = 0; window.scrollTo(0, 0); return v; }

  function refreshCounts() {
    api("GET", "/api/approvals").then(function (a) { STATE.approvals = a.length; var r = parse(); if ($("#nav")) shell(r.parts[0] || ""); }).catch(function () {});
    api("GET", "/api/notifications").then(function (n) { STATE.unread = n.unread; var d = $("#bellDot"); if (d) d.classList.toggle("hidden", !n.unread); }).catch(function () {});
  }
  setInterval(function () { if (STATE.me && document.visibilityState === "visible") refreshCounts(); }, 45000);

  function openNotifications() {
    api("POST", "/api/notifications").then(function (n) {
      STATE.unread = 0; $("#bellDot").classList.add("hidden");
      modal('<div class="row between"><h2>' + t("الإشعارات", "Notifications") + '</h2><button class="btn g" data-close>✕</button></div><div class="list" style="margin-top:10px">' +
        (n.items.length ? n.items.map(function (x) {
          return '<a class="item" href="' + esc(x.link || "#/") + '" data-close><div class="grow"><div class="t">' + esc(x.title) + '</div><div class="small muted">' + esc(x.body) + '</div></div><span class="small muted nowrap">' + ago(x.created_at) + "</span></a>";
        }).join("") : '<div class="empty">' + t("لا توجد إشعارات بعد.", "No notifications yet.") + "</div>") + "</div>");
    }).catch(fail);
  }

  function modal(html, onMount) {
    var m = document.createElement("div");
    m.className = "modal";
    m.innerHTML = '<div class="box">' + html + "</div>";
    m.addEventListener("click", function (e) { if (e.target === m || e.target.closest("[data-close]")) m.remove(); });
    document.body.appendChild(m);
    if (onMount) onMount(m);
    return m;
  }

  function statusPill(s) {
    var map = {
      draft: ["مسودة", "Draft", ""], pending_approval: ["بانتظار موافقتك", "Awaiting approval", "warn"], scheduled: ["مجدول", "Scheduled", "pu"],
      publishing: ["جاري النشر", "Publishing", "pu"], published: ["منشور", "Published", "ok"], partial: ["منشور جزئيًا", "Partly published", "warn"],
      failed: ["تعذّر", "Failed", "bad"], active: ["مفعّلة", "Active", "ok"], paused: ["متوقفة", "Paused", ""], pending: ["بانتظار موافقتك", "Pending", "warn"],
      approved: ["تم التنفيذ", "Done", "ok"], rejected: ["مرفوض", "Rejected", ""], executing: ["جاري التنفيذ", "Running", "pu"],
      succeeded: ["نجح", "Succeeded", "ok"], running: ["يعمل", "Running", "pu"], connected: ["متصل", "Connected", "ok"],
      new: ["جديدة", "New", ""], planning: ["قيد التخطيط", "Planning", ""], in_progress: ["قيد التنفيذ", "In progress", "pu"], review: ["بانتظار المراجعة", "Review", "warn"],
      done: ["مكتملة", "Done", "ok"], blocked: ["متعثرة", "Blocked", "bad"], paid: ["مدفوعة", "Paid", "ok"], initiated: ["بانتظار الدفع", "Unpaid", "warn"]
    };
    var m = map[s] || [s, s, ""];
    return '<span class="pill ' + m[2] + '">' + esc(t(m[0], m[1])) + "</span>";
  }
  var PLATFORM = { facebook: "فيسبوك", instagram: "إنستقرام", tiktok: "تيك توك", telegram: "تيليجرام", whatsapp: "واتساب", n8n: "n8n", snapchat: "سناب شات" };

  // ---------- auth ----------
  function pageAuth(mode, q) {
    var reg = mode === "register";
    $("#root").innerHTML = '<div class="auth"><div class="box"><a class="brand" href="/">' + LOGO + '</a><div class="card"><h1 style="margin-bottom:6px">' +
      (reg ? t("أنشئ حسابك", "Create your account") : t("أهلًا بعودتك", "Welcome back")) + '</h1><p class="muted" style="margin:0 0 18px">' +
      (reg ? t("ابدأ مجانًا، وفريق شهم جاهز يشتغل معك.", "Start free — the SAHM team is ready.") : t("سجّل دخولك لتكمل مع فريقك.", "Sign in to continue.")) + "</p>" +
      '<form class="form" id="af">' + (reg ? '<label class="f">' + t("الاسم", "Name") + '<input class="in" name="name" required autocomplete="name"></label>' : "") +
      '<label class="f">' + t("البريد الإلكتروني", "Email") + '<input class="in" name="email" type="email" required autocomplete="email" dir="ltr"></label>' +
      '<label class="f">' + t("كلمة المرور", "Password") + '<input class="in" name="password" type="password" required minlength="8" autocomplete="' + (reg ? "new-password" : "current-password") + '" dir="ltr"></label>' +
      '<button class="btn" type="submit">' + (reg ? t("إنشاء الحساب", "Create account") : t("دخول", "Sign in")) + "</button></form>" +
      '<p class="small muted" style="margin:16px 0 0;text-align:center">' + (reg ? t("عندك حساب؟", "Have an account?") + ' <a href="#/login">' + t("سجّل دخولك", "Sign in") + "</a>"
        : t("ما عندك حساب؟", "No account?") + ' <a href="#/register">' + t("أنشئ حسابًا", "Create one") + "</a>") + "</p>" +
      (reg ? '<p class="small muted" style="margin:12px 0 0;text-align:center">' + t("بالتسجيل توافق على ", "By signing up you accept the ") + '<a href="/terms" target="_blank">' + t("شروط الاستخدام", "terms") + "</a>" + t(" و", " and ") + '<a href="/privacy" target="_blank">' + t("سياسة الخصوصية", "privacy policy") + "</a>" + "</p>" : "") +
      "</div></div></div>";
    document.title = (reg ? "إنشاء حساب" : "تسجيل الدخول") + " — شهم | SAHM";
    $("#af").onsubmit = function (e) {
      e.preventDefault();
      var fd = new FormData(e.target), body = {};
      fd.forEach(function (v, k) { body[k] = v; });
      var b = $("button[type=submit]", e.target); b.disabled = true;
      api("POST", reg ? "/api/auth/register" : "/api/auth/login", body, { allow401: true }).then(function (me) {
        STATE.me = me;
        return boot().then(function () {
          var target = q.next || (reg ? (q.plan && q.plan !== "free" ? "#/billing?plan=" + q.plan : "#/profile?welcome=1") : "#/");
          $("#root").innerHTML = ""; go(target);
        });
      }).catch(function (err) { b.disabled = false; fail(err); });
    };
  }

  // ---------- dashboard ----------
  function pageDashboard() {
    setTitle(t("الرئيسية", "Home"));
    view('<div class="page"><span class="spin"></span></div>');
    api("GET", "/api/dashboard").then(function (d) {
      var c = d.counts;
      var hello = t("هلا ", "Hi ") + esc(d.user.name.split(" ")[0]);
      var html = '<div class="page"><div class="head"><div><h1>' + hello + '</h1><p>' + t("هذا ملخص شغلك مع فريق شهم.", "Here's your work with the SAHM team.") + "</p></div>" +
        '<a class="btn" href="#/team">' + t("ابدأ مع الفريق", "Start with the team") + "</a></div>";
      if (!d.profile_complete) {
        html += '<div class="card" style="border-color:var(--pu2);margin-bottom:16px"><div class="row between"><div><h3>' + t("أكمل ملف نشاطك", "Complete your business profile") + '</h3><p class="muted small" style="margin:4px 0 0">' +
          t("الفريق يعتمد عليه في كل شيء: المحتوى، والمواقع، وردود البوت.", "The team uses it for content, sites and bot replies.") + '</p></div><a class="btn sm" href="#/profile">' + t("أكمل الآن", "Complete") + "</a></div></div>";
      }
      if (c.approvals) {
        html += '<a class="card row between" href="#/approvals" style="margin-bottom:16px;border-color:color-mix(in srgb,var(--warn) 50%,transparent)"><div><h3>' +
          c.approvals + " " + t("بانتظار موافقتك", "awaiting your approval") + '</h3><p class="muted small" style="margin:4px 0 0">' + t("نشر، ردود، مواقع، أتمتة — راجعها واعتمدها.", "Posts, replies, sites, automations — review them.") + "</p></div><span class=\"btn sm\">" + t("مراجعة", "Review") + "</span></a>";
      }
      html += '<div class="grid g4" style="margin-bottom:16px">' +
        statCard(c.published_posts, t("منشورات منشورة", "Published posts"), "#/studio") + statCard(c.scheduled_posts, t("منشورات مجدولة", "Scheduled posts"), "#/studio") +
        statCard(c.open_tasks, t("مهام مفتوحة", "Open tasks"), "#/projects") + statCard(c.automations, t("أتمتة مفعّلة", "Active automations"), "#/automations") +
        statCard(c.sites, t("مواقع", "Sites"), "#/sites") + statCard(c.channels, t("حسابات مربوطة", "Connections"), "#/channels") +
        statCard(c.contacts, t("عملاء", "Contacts"), "#/contacts") + statCard(c.done_tasks, t("مهام مكتملة", "Done tasks"), "#/projects") + "</div>";
      html += '<div class="grid g2"><div class="card pad0"><div class="item"><h3 class="grow">' + t("آخر المحادثات", "Recent conversations") + '</h3><a href="#/team" class="small">' + t("الكل", "All") + "</a></div>" +
        (d.conversations.length ? d.conversations.map(function (cv) {
          return '<a class="item" href="#/chat/' + cv.persona + "/" + cv.id + '">' + avatar(cv.persona) + '<div class="grow"><div class="t">' + esc(cv.title || t("محادثة جديدة", "New conversation")) + '</div><div class="small muted">' + esc(persona(cv.persona).name) + " · " + ago(cv.updated_at) + "</div></div></a>";
        }).join("") : '<div class="empty">' + t("ما بدأت محادثة بعد.", "No conversations yet.") + "</div>") + "</div>" +
        '<div class="card pad0"><div class="item"><h3 class="grow">' + t("آخر النشاطات", "Activity") + "</h3></div>" +
        (d.activity.length ? d.activity.map(function (a) { return '<div class="item"><div class="grow small">' + esc(a.summary) + '</div><span class="small muted nowrap">' + ago(a.created_at) + "</span></div>"; }).join("")
          : '<div class="empty">' + t("لا يوجد نشاط بعد.", "Nothing yet.") + "</div>") + "</div></div>";
      html += '<div class="card" style="margin-top:16px"><div class="row between" style="margin-bottom:12px"><h3>' + t("استخدامك هذا الشهر", "Usage this month") + '</h3><a class="small" href="#/billing">' + esc(d.user.plan_name) + "</a></div>" + usageBars(d.usage) + "</div></div>";
      view(html);
    }).catch(fail);
  }
  function statCard(n, label, href) { return '<a class="card stat" href="' + href + '"><b>' + (n || 0) + "</b><span>" + label + "</span></a>"; }
  function usageBars(u) {
    return '<div class="grid g2">' + u.map(function (x) {
      var pct = x.limit == null ? 4 : x.limit === 0 ? 100 : Math.min(100, Math.round(100 * x.used / x.limit));
      return '<div><div class="row between small"><span>' + esc(x.label) + '</span><span class="muted">' + x.used + " / " + (x.limit == null ? "∞" : x.limit) + '</span></div><div class="bar' + (pct >= 90 ? " full" : "") + '"><i style="width:' + pct + '%"></i></div></div>';
    }).join("") + "</div>";
  }

  // ---------- team ----------
  function pageTeam() {
    setTitle(t("فريق شهم", "SAHM Team"));
    var html = '<div class="page"><div class="head"><div><h1>' + t("فريق شهم", "SAHM Team") + "</h1><p>" +
      t("كل واحد ينفّذ في تخصصه. اختر خدمة وابدأ، أو خلّ الفريق كامل يشتغل على هدفك.", "Each teammate executes in their specialty. Pick a service, or put the whole team on your goal.") + "</p></div></div>" +
      '<div class="card" style="margin-bottom:18px;border-color:var(--pu2)"><h3>' + t("وضع الفريق — هدف واحد، أربعة منفذين", "Team mode — one goal, four executors") + '</h3><p class="muted small" style="margin:4px 0 12px">' +
      t("اكتب هدفك (مثال: أبي أطلق متجر عطور خلال شهر). نجد تحوله لمشروع ومهام، وكل مهمة تروح للمختص وينفّذها.", "Describe your goal; Najd turns it into a project and each task goes to the right teammate.") + "</p>" +
      '<form class="row" id="teamF"><input class="in" name="goal" required minlength="5" placeholder="' + t("وش هدفك؟", "What's your goal?") + '" style="flex:1;min-width:220px"><button class="btn" type="submit">' + t("خطط ووزّع", "Plan & assign") + "</button></form></div>" +
      '<div class="grid g2">' + STATE.personas.map(function (p) {
        return '<div class="card pcard" style="--c:' + p.color + '"><div class="who">' + avatar(p.id, "lg") + '<div class="grow"><h2>' + esc(p.name) + ' <span class="small muted">' + p.name_en + '</span></h2><div style="color:' + p.color + ';font-weight:600">' + esc(p.role) + '</div></div><a class="btn sm" href="#/chat/' + p.id + '">' + t("تحدث معي الآن", "Talk to me") + "</a></div>" +
          '<p class="muted small" style="margin:0">«' + esc(p.tagline) + '»</p><div class="grid g2" style="gap:8px">' + p.services.map(function (s, i) {
            return '<button class="svc" data-p="' + p.id + '" data-i="' + i + '"><b>' + esc(s.title) + "</b><span>" + esc(s.desc) + "</span></button>";
          }).join("") + "</div></div>";
      }).join("") + "</div></div>";
    var v = view(html);
    v.addEventListener("click", function (e) {
      var b = e.target.closest(".svc"); if (!b) return;
      var p = PERS[b.dataset.p]; startChat(p.id, p.services[+b.dataset.i].prompt);
    });
    $("#teamF").onsubmit = function (e) {
      e.preventDefault();
      var btn = $("button", e.target); btn.disabled = true; btn.innerHTML = '<span class="spin"></span> ' + t("نجد ترتّب الخطة…", "Najd is planning…");
      api("POST", "/api/core/team", { goal: e.target.goal.value }).then(function (r) { toast(t("جاهزة! ", "Ready! ") + r.tasks + t(" مهمة موزعة على الفريق.", " tasks assigned.")); go("#/projects/" + r.project_id); })
        .catch(function (err) { btn.disabled = false; btn.textContent = t("خطط ووزّع", "Plan & assign"); fail(err); });
    };
  }

  var PENDING_PROMPT = null;
  function startChat(pid, prompt) { PENDING_PROMPT = prompt || null; go("#/chat/" + pid + "/new"); }

  // ---------- chat ----------
  function pageChat(parts) {
    var pid = parts[1] || "sulaf", cid = parts[2];
    var p = persona(pid);
    if (!PERS[pid]) return go("#/team");
    setTitle(p.name + " — " + p.role);
    var v = view('<div class="chat" style="--c:' + p.color + '"><aside class="convs" id="convs"><a class="btn sm" style="width:100%;margin-bottom:10px" href="#/chat/' + pid + '/new">+ ' + t("محادثة جديدة", "New chat") + '</a><div id="convList"><span class="spin"></span></div></aside>' +
      '<section class="pane"><div class="hdr">' + avatar(pid) + '<div class="grow"><b>' + esc(p.name) + " | " + p.name_en + '</b><div class="small muted">' + esc(p.role) + '</div></div>' +
      '<select class="in" id="switchP" style="width:auto">' + STATE.personas.map(function (x) { return '<option value="' + x.id + '"' + (x.id === pid ? " selected" : "") + ">" + esc(x.name) + "</option>"; }).join("") + "</select></div>" +
      '<div class="msgs" id="msgs"></div><form class="composer" id="cf"><div class="file-queue" id="fileQueue" hidden></div>' +
      '<textarea class="in" id="tx" rows="1" placeholder="' + t("اكتب رسالتك… أو أرفق صورًا وملفات", "Type a message or attach files") + '"></textarea>' +
      '<input id="fileInput" type="file" multiple hidden accept="image/jpeg,image/png,image/webp,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,application/vnd.openxmlformats-officedocument.presentationml.presentation,text/plain,text/markdown,text/csv,.jpg,.jpeg,.png,.webp,.pdf,.docx,.xlsx,.pptx,.txt,.md,.csv">' +
      '<button class="btn sm attach-btn" id="attachB" type="button" title="' + t("إرفاق صور أو ملفات", "Attach images or files") + '">📎 ' + t("إرفاق", "Attach") + '</button>' +
      '<button class="btn" id="sendB" type="submit">' + t("إرسال", "Send") + "</button></form></section></div>");
    $("#switchP").onchange = function (e) { go("#/chat/" + e.target.value); };
    var msgs = $("#msgs"), tx = $("#tx"), sendB = $("#sendB"), fileInput = $("#fileInput"), attachB = $("#attachB"), fileQueue = $("#fileQueue");
    var conv = null, busy = null, selectedFiles = [];

    api("GET", "/api/conversations?persona=" + pid).then(function (list) {
      $("#convList").innerHTML = list.map(function (c) { return '<a href="#/chat/' + pid + "/" + c.id + '" class="' + (c.id === cid ? "on" : "") + '">' + esc(c.title || t("محادثة", "Chat")) + "</a>"; }).join("") || '<p class="small muted">' + t("لا توجد محادثات سابقة.", "No previous chats.") + "</p>";
      if (!cid && list.length && !PENDING_PROMPT) return go("#/chat/" + pid + "/" + list[0].id);
      if (cid && cid !== "new") return load(cid);
      welcome();
    }).catch(fail);

    function welcome() {
      msgs.innerHTML = '<div class="m a">' + md(p.welcome) + '</div><div class="starters">' + p.services.map(function (s) {
        return '<button class="svc" style="--c:' + p.color + '" data-prompt="' + esc(s.prompt) + '"><b>' + esc(s.title) + "</b><span>" + esc(s.desc) + "</span></button>";
      }).join("") + "</div>";
      if (PENDING_PROMPT) { var pp = PENDING_PROMPT; PENDING_PROMPT = null; send(pp); }
      else tx.focus();
    }
    function load(id) {
      api("GET", "/api/conversations/" + id).then(function (d) {
        conv = d.conversation;
        msgs.innerHTML = '<div class="m a">' + md(p.welcome) + "</div>";
        d.messages.forEach(function (m) {
          if (m.role === "user") addUserBubble(m.text, m.attachments || [], []);
          else { var b = bubble("a"); b.innerHTML = actsHtml(m.actions) + md(m.text); }
        });
        msgs.scrollTop = msgs.scrollHeight;
        if (PENDING_PROMPT) { var pp = PENDING_PROMPT; PENDING_PROMPT = null; send(pp); }
      }).catch(function (e) { fail(e); go("#/chat/" + pid); });
    }
    function bubble(kind) { var d = document.createElement("div"); d.className = "m " + kind; msgs.appendChild(d); msgs.scrollTop = msgs.scrollHeight; return d; }
    function showAttachments(attachments, localFiles) {
      var html = "";
      (attachments || []).forEach(function (a) {
        var href = esc(a.url || "#"), name = esc(a.filename || t("ملف مرفق", "Attached file"));
        if (String(a.content_type || "").indexOf("image/") === 0) {
          html += '<a class="chat-attachment image" href="' + href + '" target="_blank" rel="noopener noreferrer"><img src="' + href + '" alt="' + name + '" loading="lazy"><span>' + name + '</span></a>';
        } else {
          html += '<a class="chat-attachment" href="' + href + '" target="_blank" rel="noopener noreferrer">📎 <span>' + name + '</span></a>';
        }
      });
      (localFiles || []).forEach(function (f) {
        html += '<span class="chat-attachment">📎 <span>' + esc(f.name) + '</span></span>';
      });
      return html ? '<div class="chat-attachments">' + html + '</div>' : "";
    }
    function addUserBubble(text, attachments, localFiles) {
      var el = bubble("u");
      if (text) { var txt = document.createElement("div"); txt.textContent = text; el.appendChild(txt); }
      var filesHtml = showAttachments(attachments, localFiles);
      if (filesHtml) { var holder = document.createElement("div"); holder.innerHTML = filesHtml; el.appendChild(holder); }
      return el;
    }
    function renderFileQueue() {
      if (!selectedFiles.length) { fileQueue.hidden = true; fileQueue.innerHTML = ""; return; }
      fileQueue.hidden = false;
      fileQueue.innerHTML = selectedFiles.map(function (f, i) {
        return '<span class="file-chip">📎 ' + esc(f.name) + ' <button type="button" data-remove-file="' + i + '" aria-label="' + t("حذف الملف", "Remove file") + '">×</button></span>';
      }).join("");
    }
    attachB.onclick = function () { if (!busy) fileInput.click(); };
    fileInput.onchange = function () {
      var incoming = Array.prototype.slice.call(fileInput.files || []), total = selectedFiles.reduce(function (n, f) { return n + f.size; }, 0);
      incoming.forEach(function (f) {
        if (selectedFiles.length >= 5) { toast(t("5 ملفات كحد أقصى للرسالة.", "Up to 5 files per message."), true); return; }
        if (f.size > 8 * 1024 * 1024) { toast(t("حجم الملف يتجاوز 8 ميغابايت.", "Each file must be 8 MB or less."), true); return; }
        if (total + f.size > 16 * 1024 * 1024) { toast(t("إجمالي المرفقات يجب ألا يتجاوز 16 ميغابايت.", "Combined attachments must be 16 MB or less."), true); return; }
        selectedFiles.push(f); total += f.size;
      });
      fileInput.value = ""; renderFileQueue();
    };
    fileQueue.onclick = function (e) {
      var b = e.target.closest("[data-remove-file]");
      if (b) { selectedFiles.splice(Number(b.dataset.removeFile), 1); renderFileQueue(); }
    };
    function actsHtml(acts) {
      if (!acts || !acts.length) return "";
      return '<div class="acts">' + acts.map(function (a) {
        return '<div class="act ' + (a.ok ? "done" : "failed") + '">' + esc(a.label) + (a.approval ? ' <a href="#/approvals">' + t("بانتظار موافقتك ←", "Awaiting approval →") + "</a>" : "") + "</div>";
      }).join("") + "</div>";
    }
    function ensureConv() {
      if (conv) return Promise.resolve(conv);
      return api("POST", "/api/conversations", { persona: pid }).then(function (c) { conv = c; history.replaceState(null, "", "#/chat/" + pid + "/" + c.id); return c; });
    }
    function send(text) {
      text = (text || "").trim();
      if ((!text && !selectedFiles.length) || busy) return;
      var filesToSend = selectedFiles.slice();
      if (!text && filesToSend.length) text = t("يرجى فحص المرفقات ومساعدتي بناءً عليها.", "Please review the attached files and help me based on them.");
      var st = $(".starters", msgs); if (st) st.remove();
      addUserBubble(text, [], filesToSend);
      tx.value = ""; tx.style.height = ""; selectedFiles = []; renderFileQueue();
      var b = bubble("a"); b.innerHTML = '<span class="typing"><i></i><i></i><i></i></span>';
      var acts = document.createElement("div"); acts.className = "acts";
      var body = document.createElement("div");
      var full = "", actMap = {};
      busy = new AbortController();
      sendB.textContent = t("إيقاف", "Stop");
      ensureConv().then(function (c) {
        var form = new FormData(); form.append("text", text);
        filesToSend.forEach(function (file) { form.append("files", file, file.name); });
        return fetch("/api/conversations/" + c.id + "/messages", { method: "POST", headers: { "x-sahm": "1" }, body: form, signal: busy.signal, credentials: "same-origin" });
      }).then(function (resp) {
        if (!resp.ok) return resp.json().then(function (d) { throw { message: d.message || t("تعذّر الإرسال", "Could not send") }; });
        b.innerHTML = ""; b.appendChild(acts); b.appendChild(body);
        var reader = resp.body.getReader(), dec = new TextDecoder(), buf = "";
        function pump() {
          return reader.read().then(function (r) {
            if (r.done) return;
            buf += dec.decode(r.value, { stream: true });
            var lines = buf.split("\n"); buf = lines.pop();
            lines.forEach(function (line) {
              if (!line.trim()) return;
              var ev = JSON.parse(line);
              if (ev.type === "text") { full += ev.delta; body.innerHTML = md(full); }
              else if (ev.type === "tool") {
                var el;
                if (ev.status === "running") {
                  el = document.createElement("div"); el.className = "act"; el.innerHTML = '<span class="spin"></span> ' + esc(ev.label) + "…"; acts.appendChild(el); actMap.last = el;
                } else {
                  el = actMap.last || document.createElement("div");
                  el.className = "act " + (ev.status === "done" ? "done" : "failed");
                  var extra = "";
                  if (ev.approval) extra = ' <a href="#/approvals">' + t("بانتظار موافقتك ←", "Awaiting approval →") + "</a>";
                  else if (ev.result && ev.result.preview_url) extra = '<img src="' + esc(ev.result.preview_url) + '" alt="">';
                  else if (ev.result && ev.result.preview) extra = ' <a href="' + esc(ev.result.preview) + '">' + t("معاينة ←", "Preview →") + "</a>";
                  else if (ev.result && ev.result.link) extra = ' <a href="' + esc(ev.result.link) + '">' + t("فتح ←", "Open →") + "</a>";
                  el.innerHTML = esc(ev.label) + extra;
                  if (!el.parentNode) acts.appendChild(el);
                  actMap.last = null;
                  if (ev.approval) refreshCounts();
                }
              } else if (ev.type === "error") {
                body.innerHTML = md(full) + '<p style="color:var(--bad);margin-top:8px">' + esc(ev.message) + (ev.code === "limit_reached" ? ' <a href="#/billing">' + t("ترقية الباقة", "Upgrade") + "</a>" : "") + "</p>";
              }
              msgs.scrollTop = msgs.scrollHeight;
            });
            return pump();
          });
        }
        return pump();
      }).catch(function (e) {
        if (e && e.name === "AbortError") { body.innerHTML = md(full) || '<p class="muted">' + t("أوقفت الرد.", "Stopped.") + "</p>"; if (!b.contains(body)) { b.innerHTML = ""; b.appendChild(body); } return; }
        selectedFiles = filesToSend.concat(selectedFiles).slice(0, 5); renderFileQueue();
        b.innerHTML = '<p style="color:var(--bad)">' + esc(e.message || t("تعذّر الإرسال", "Could not send")) + "</p>";
      }).then(function () {
        busy = null; sendB.textContent = t("إرسال", "Send"); tx.focus();
        api("GET", "/api/conversations?persona=" + pid).then(function (list) {
          $("#convList").innerHTML = list.map(function (c) { return '<a href="#/chat/' + pid + "/" + c.id + '" class="' + (conv && c.id === conv.id ? "on" : "") + '">' + esc(c.title || t("محادثة", "Chat")) + "</a>"; }).join("");
        }).catch(function () {});
      });
    }
    $("#cf").onsubmit = function (e) { e.preventDefault(); if (busy) { busy.abort(); return; } send(tx.value); };
    tx.addEventListener("keydown", function (e) { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); if (!busy) send(tx.value); } });
    tx.addEventListener("input", function () { tx.style.height = ""; tx.style.height = Math.min(tx.scrollHeight, 180) + "px"; });
    msgs.addEventListener("click", function (e) { var s = e.target.closest("[data-prompt]"); if (s) send(s.dataset.prompt); });
    cleanup = function () { if (busy) busy.abort(); };
  }

  // ---------- approvals ----------
  function pageApprovals(parts, q) {
    setTitle(t("بانتظار موافقتك", "Approvals"));
    var tab = q.status || "pending";
    view('<div class="page"><div class="head"><div><h1>' + t("بانتظار موافقتك", "Awaiting your approval") + "</h1><p>" +
      t("أي إجراء يطلع برّا شهم يمر عليك أولًا: نشر، رد، رسالة، تفعيل.", "Anything that leaves SAHM waits for you: posts, replies, messages, activations.") + '</p></div></div><div class="tabs">' +
      [["pending", "بانتظار", "Pending"], ["all", "الكل", "All"]].map(function (x) { return '<button data-tab="' + x[0] + '" class="' + (tab === x[0] ? "on" : "") + '">' + t(x[1], x[2]) + "</button>"; }).join("") +
      '</div><div id="apList"><span class="spin"></span></div></div>');
    $(".tabs").onclick = function (e) { var b = e.target.closest("[data-tab]"); if (b) go("#/approvals?status=" + b.dataset.tab); };
    api("GET", "/api/approvals?status=" + tab).then(function (list) {
      $("#apList").innerHTML = list.length ? list.map(apCard).join("") : '<div class="card empty"><h3>' + t("ما فيه شيء ينتظرك", "Nothing waiting") + "</h3><p>" + t("لما يجهز الفريق شيء يحتاج موافقتك، بيظهر هنا.", "When the team prepares something, it shows here.") + "</p></div>";
    }).catch(fail);
    $("#apList").onclick = function (e) {
      var b = e.target.closest("[data-dec]"); if (!b) return;
      var card = b.closest(".card"); $$("button", card).forEach(function (x) { x.disabled = true; });
      b.innerHTML = '<span class="spin"></span>';
      api("POST", "/api/approvals/" + b.dataset.id + "/" + b.dataset.dec).then(function (r) {
        if (r.status === "approved") toast(t("تم التنفيذ ✓", "Done ✓"));
        else if (r.status === "failed") toast(t("تعذّر التنفيذ: ", "Failed: ") + (r.result && r.result.error || ""), true);
        refreshCounts(); pageApprovals(parts, q);
      }).catch(function (err) { fail(err); pageApprovals(parts, q); });
    };
  }
  function apCard(a) {
    var p = persona(a.persona), body = "";
    if (a.post) {
      var m = a.post.media[0];
      body = '<div class="post" style="padding:12px 0 0;border:0">' + (m ? (m.kind === "video" ? '<video class="pm" src="' + esc(m.url) + '" muted></video>' : '<img class="pm" src="' + esc(m.url) + '" alt="">') : '<div class="pm"></div>') +
        '<div><div class="cap" style="max-height:none">' + esc(a.post.caption) + '</div><div class="row small muted" style="margin-top:8px">' + a.post.channels.map(function (c) { return '<span class="pill">' + esc(PLATFORM[c.platform] || c.platform) + " · " + esc(c.name) + "</span>"; }).join("") +
        (a.post.scheduled_at ? '<span class="pill pu">' + fmtDate(a.post.scheduled_at) + "</span>" : "") + "</div></div></div>";
    } else if (a.site) {
      body = '<p class="small" style="margin:10px 0 0">' + t("الرابط بعد النشر: ", "Public link: ") + '<span dir="ltr">' + esc(a.site.url) + '</span> · <a href="/api/sites/' + a.site.id + '/preview" target="_blank" rel="noopener">' + t("معاينة", "Preview") + "</a></p>";
    } else if (a.automation) {
      body = '<p class="small muted" style="margin:10px 0 0">' + esc(triggerText(a.automation.trigger)) + " · " + a.automation.steps.length + t(" خطوات", " steps") + ' · <a href="#/automations/' + a.automation.id + '">' + t("التفاصيل", "Details") + "</a></p>";
    } else if (a.message) {
      body = '<div class="m a" style="max-width:100%;margin-top:10px">' + esc(a.message) + "</div>";
    } else if (a.campaign) {
      body = '<p class="small muted" style="margin:10px 0 0">' + t("القالب: ", "Template: ") + esc(a.campaign.template) + (a.campaign.tag ? " · " + t("الوسم: ", "Tag: ") + esc(a.campaign.tag) : "") + "</p>";
    }
    var res = a.result && a.result.error ? '<p class="small" style="color:var(--bad);margin:8px 0 0">' + esc(a.result.error) + "</p>" : "";
    return '<div class="card" style="margin-bottom:12px"><div class="row">' + avatar(a.persona) + '<div class="grow"><b>' + esc(a.title) + '</b><div class="small muted">' + esc(p.name) + " · " + ago(a.created_at) + "</div></div>" + statusPill(a.status) + "</div>" + body + res +
      (a.status === "pending" ? '<div class="row" style="margin-top:14px"><button class="btn ok" data-dec="approve" data-id="' + a.id + '">' + t("اعتمد ونفّذ", "Approve") + '</button><button class="btn bad" data-dec="reject" data-id="' + a.id + '">' + t("رفض", "Reject") + "</button></div>" : "") + "</div>";
  }
  function triggerText(tr) {
    tr = tr || {};
    return ({ schedule: t("مجدولة", "Scheduled") + " (" + (tr.cron || "") + ")", webhook: "Webhook", manual: t("يدوي", "Manual"), whatsapp_inbound: t("عند رسالة واتساب", "On WhatsApp message"),
      telegram_inbound: t("عند رسالة تيليجرام", "On Telegram message"), site_form: t("عند طلب من الموقع", "On site form") })[tr.type] || tr.type;
  }

  // ---------- projects ----------
  var COLS = [["new", "جديدة", "New"], ["planning", "قيد التخطيط", "Planning"], ["in_progress", "قيد التنفيذ", "In progress"], ["review", "بانتظار المراجعة", "Review"], ["done", "مكتملة", "Done"], ["blocked", "متعثرة", "Blocked"]];
  function pageProjects(parts) {
    if (parts[1]) return pageProject(parts[1]);
    setTitle(t("مشاريع نجد", "Najd's Projects"));
    view('<div class="page"><div class="head"><div><h1>' + t("مشاريع نجد", "Najd's Projects") + "</h1><p>" + t("خطط ومهام بمواعيد، والفريق ينفّذ نصيبه منها.", "Plans with dated tasks; the team executes its part.") +
      '</p></div><div class="row"><button class="btn o" id="newP">' + t("مشروع فارغ", "Blank project") + '</button><button class="btn" id="askNajd">' + t("خطة مع نجد", "Plan with Najd") + '</button></div></div><div id="pl"><span class="spin"></span></div></div>');
    $("#askNajd").onclick = function () { startChat("najd", "عندي مشروع جديد وأبي خطة كاملة له"); };
    $("#newP").onclick = function () {
      modal('<h2>' + t("مشروع جديد", "New project") + '</h2><form class="form" id="npf" style="margin-top:12px"><label class="f">' + t("الاسم", "Name") + '<input class="in" name="name" required></label><label class="f">' + t("الهدف", "Goal") + '<textarea class="in" name="goal"></textarea></label><button class="btn">' + t("إنشاء", "Create") + "</button></form>", function (m) {
        $("#npf", m).onsubmit = function (e) { e.preventDefault(); api("POST", "/api/projects", { name: e.target.name.value, goal: e.target.goal.value }).then(function (p) { m.remove(); go("#/projects/" + p.id); }).catch(fail); };
      });
    };
    api("GET", "/api/projects").then(function (list) {
      $("#pl").innerHTML = list.length ? '<div class="grid g3">' + list.map(function (p) {
        var done = (p.counts.done || 0), pct = p.total ? Math.round(100 * done / p.total) : 0;
        return '<a class="card" href="#/projects/' + p.id + '"><h3>' + esc(p.name) + '</h3><p class="small muted" style="margin:4px 0 12px;min-height:2.4em">' + esc((p.goal || p.description || "").slice(0, 110)) + '</p><div class="bar"><i style="width:' + pct + '%"></i></div><div class="row between small muted" style="margin-top:6px"><span>' + done + "/" + p.total + t(" مهمة", " tasks") + "</span><span>" + pct + "%</span></div></a>";
      }).join("") + "</div>" : '<div class="card empty"><h3>' + t("ما فيه مشاريع بعد", "No projects yet") + "</h3><p>" + t("قل لنجد وش هدفك وهي ترتب لك خطة كاملة.", "Tell Najd your goal and she'll build a full plan.") + "</p></div>";
    }).catch(fail);
  }
  function pageProject(id) {
    view('<div class="page wide"><span class="spin"></span></div>');
    var poll = null;
    function load() {
      api("GET", "/api/projects/" + id).then(function (d) {
        setTitle(d.project.name);
        var running = d.tasks.some(function (x) { return x.status === "in_progress" && x.output === ""; });
        var html = '<div class="page wide"><div class="head"><div><a class="small" href="#/projects">← ' + t("المشاريع", "Projects") + "</a><h1>" + esc(d.project.name) + "</h1><p>" + esc(d.project.goal || d.project.description) + '</p></div><div class="row">' +
          '<a class="btn o sm" href="' + esc(d.calendar_url) + '">' + t("أضف للتقويم", "Add to calendar") + '</a><button class="btn o sm" id="addT">+ ' + t("مهمة", "Task") + '</button><button class="btn sm" id="askN">' + t("اسأل نجد عن المشروع", "Ask Najd") + "</button></div></div>" +
          '<div class="kan">' + COLS.map(function (c) {
            var items = d.tasks.filter(function (x) { return x.status === c[0]; });
            return '<div class="col"><h4><span>' + t(c[1], c[2]) + "</span><span>" + items.length + "</span></h4>" + items.map(function (x) {
              return '<div class="task" data-id="' + x.id + '"><div class="t">' + esc(x.title) + '</div><div class="meta">' + (x.assignee === "me" ? '<span class="pill">' + t("أنت", "You") + "</span>" : '<img src="/assets/img/team/' + x.assignee + '.jpg" alt="">' + esc(persona(x.assignee).name)) +
                (x.due_date ? "<span>· " + esc(x.due_date) + "</span>" : "") + (x.priority === "high" ? '<span class="pill bad">' + t("عاجلة", "High") + "</span>" : "") + "</div></div>";
            }).join("") + "</div>";
          }).join("") + "</div></div>";
        view(html);
        $("#askN").onclick = function () { startChat("najd", "وين وصلنا في مشروع «" + d.project.name + "»؟ وش المتأخر ووش الخطوة الجاية؟"); };
        $("#addT").onclick = function () { taskModal(null, d.project.id, load); };
        $(".kan").onclick = function (e) { var el = e.target.closest(".task"); if (el) taskModal(d.tasks.filter(function (x) { return x.id === el.dataset.id; })[0], d.project.id, load); };
        clearTimeout(poll);
        if (running) poll = setTimeout(load, 5000);
      }).catch(fail);
    }
    load();
    cleanup = function () { clearTimeout(poll); };
  }
  function taskModal(task, pid, reload) {
    var x = task || { title: "", details: "", assignee: "me", status: "new", priority: "medium", due_date: "" };
    var opts = function (list, cur) { return list.map(function (o) { return '<option value="' + o[0] + '"' + (o[0] === cur ? " selected" : "") + ">" + esc(o[1]) + "</option>"; }).join(""); };
    var people = [["me", t("أنا", "Me")]].concat(STATE.personas.map(function (p) { return [p.id, p.name + " — " + p.role]; }));
    var canRun = task && x.assignee !== "me" && x.status !== "in_progress";
    modal('<div class="row between"><h2>' + (task ? t("المهمة", "Task") : t("مهمة جديدة", "New task")) + '</h2><button class="btn g" data-close>✕</button></div><form class="form" id="tf" style="margin-top:12px">' +
      '<label class="f">' + t("العنوان", "Title") + '<input class="in" name="title" required value="' + esc(x.title) + '"></label>' +
      '<label class="f">' + t("التفاصيل", "Details") + '<textarea class="in" name="details">' + esc(x.details) + "</textarea></label>" +
      '<div class="grid g2"><label class="f">' + t("المسؤول", "Assignee") + '<select class="in" name="assignee">' + opts(people, x.assignee) + "</select></label>" +
      '<label class="f">' + t("الحالة", "Status") + '<select class="in" name="status">' + opts(COLS.map(function (c) { return [c[0], t(c[1], c[2])]; }), x.status) + "</select></label>" +
      '<label class="f">' + t("الموعد", "Due date") + '<input class="in" type="date" name="due_date" value="' + esc(x.due_date || "") + '"></label>' +
      '<label class="f">' + t("الأولوية", "Priority") + '<select class="in" name="priority">' + opts([["low", t("منخفضة", "Low")], ["medium", t("متوسطة", "Medium")], ["high", t("عاجلة", "High")]], x.priority) + "</select></label></div>" +
      (x.output ? '<div><div class="small muted" style="margin-bottom:6px">' + t("ما أنجزه الفريق", "Team output") + '</div><div class="m a" style="max-width:100%">' + md(x.output) + "</div></div>" : "") +
      '<div class="row between"><div class="row"><button class="btn" type="submit">' + t("حفظ", "Save") + "</button>" + (canRun ? '<button class="btn ok" type="button" id="runT">' + t("خلّ ", "Let ") + esc(persona(x.assignee).name) + t(" ينفّذها", " do it") + "</button>" : "") + "</div>" +
      (task ? '<button class="btn bad sm" type="button" id="delT">' + t("حذف", "Delete") + "</button>" : "") + "</div></form>", function (m) {
      $("#tf", m).onsubmit = function (e) {
        e.preventDefault();
        var f = e.target, body = { title: f.title.value, details: f.details.value, assignee: f.assignee.value, status: f.status.value, due_date: f.due_date.value, priority: f.priority.value };
        (task ? api("PATCH", "/api/tasks/" + task.id, body) : api("POST", "/api/projects/" + pid + "/tasks", body).then(function (nt) { return body.status !== "new" ? api("PATCH", "/api/tasks/" + nt.id, { status: body.status }) : nt; }))
          .then(function () { m.remove(); reload(); }).catch(fail);
      };
      var run = $("#runT", m);
      if (run) run.onclick = function () { run.disabled = true; api("POST", "/api/tasks/" + task.id + "/run").then(function () { toast(t("بدأ التنفيذ، النتيجة بتظهر في المهمة.", "Started — the result will appear on the task.")); m.remove(); reload(); }).catch(function (e) { run.disabled = false; fail(e); }); };
      var del = $("#delT", m);
      if (del) del.onclick = function () { if (confirm(t("حذف المهمة؟", "Delete task?"))) api("DELETE", "/api/tasks/" + task.id).then(function () { m.remove(); reload(); }).catch(fail); };
    });
  }

  // ---------- studio (Sulaf) ----------
  function pageStudio(parts, q) {
    setTitle(t("استوديو سلاف", "Sulaf's Studio"));
    var tab = q.tab || "all";
    view('<div class="page"><div class="head"><div><h1>' + t("استوديو سلاف", "Sulaf's Studio") + "</h1><p>" + t("منشوراتك وجدولتها ونتائجها على كل حساباتك.", "Your posts, schedule and results across accounts.") +
      '</p></div><div class="row"><button class="btn o" id="newPost">' + t("منشور يدوي", "Manual post") + '</button><button class="btn" id="askS">' + t("خطة محتوى مع سلاف", "Content plan with Sulaf") + "</button></div></div>" +
      '<div class="tabs">' + [["all", "الكل", "All"], ["pending_approval", "بانتظار موافقتك", "Pending"], ["scheduled", "مجدول", "Scheduled"], ["published", "منشور", "Published"], ["failed", "تعذّر", "Failed"]].map(function (x) {
        return '<button data-tab="' + x[0] + '" class="' + (tab === x[0] ? "on" : "") + '">' + t(x[1], x[2]) + "</button>";
      }).join("") + '</div><div class="card pad0" id="postList"><div class="empty"><span class="spin"></span></div></div></div>');
    $("#askS").onclick = function () { startChat("sulaf", "سوّي لي تقويم محتوى للأسبوعين الجاية لنشاطي وجهزيه للنشر"); };
    $("#newPost").onclick = function () { postComposer(function () { pageStudio(parts, q); }); };
    $(".tabs").onclick = function (e) { var b = e.target.closest("[data-tab]"); if (b) go("#/studio?tab=" + b.dataset.tab); };
    api("GET", "/api/posts").then(function (list) {
      if (tab !== "all") list = list.filter(function (p) { return p.status === tab || (tab === "published" && p.status === "partial"); });
      $("#postList").innerHTML = list.length ? list.map(postRow).join("") : '<div class="empty"><h3>' + t("لا توجد منشورات", "No posts") + "</h3><p>" + t("اطلب من سلاف تقويم محتوى، أو أنشئ منشورًا يدويًا.", "Ask Sulaf for a content plan, or create one manually.") + "</p></div>";
    }).catch(fail);
    $("#postList").onclick = function (e) {
      var b = e.target.closest("[data-pa]"); if (!b) return;
      var id = b.dataset.id, a = b.dataset.pa;
      if (a === "delete" && !confirm(t("حذف المنشور؟", "Delete post?"))) return;
      b.disabled = true;
      var req = a === "delete" ? api("DELETE", "/api/posts/" + id) : api("POST", "/api/posts/" + id + "/" + a);
      req.then(function () { toast(a === "publish" ? t("تمت محاولة النشر", "Publish attempted") : t("تم", "Done")); pageStudio(parts, q); }).catch(function (err) { b.disabled = false; fail(err); });
    };
  }
  function postRow(p) {
    var m = p.media[0];
    var stats = Object.keys(p.insights || {}).map(function (k) {
      var s = p.insights[k]; if (s.error) return "";
      return '<span>' + esc(PLATFORM[s.platform] || s.platform) + ": ❤ " + (s.likes || 0) + " · 💬 " + (s.comments || 0) + (s.reach != null ? " · " + t("وصول ", "reach ") + s.reach : "") + (s.url ? ' · <a href="' + esc(s.url) + '" target="_blank" rel="noopener">' + t("عرض", "View") + "</a>" : "") + "</span>";
    }).join("");
    var errs = Object.keys(p.results || {}).map(function (k) { var r = p.results[k]; return r.ok ? "" : '<div class="small" style="color:var(--bad)">' + esc(PLATFORM[r.platform] || r.platform || "") + ": " + esc(r.error || "") + "</div>"; }).join("");
    var actions = "";
    if (p.status === "draft" || p.status === "failed") actions += '<button class="btn sm" data-pa="publish" data-id="' + p.id + '">' + t("انشر الآن", "Publish now") + "</button>";
    if (p.status === "published" || p.status === "partial") actions += '<button class="btn o sm" data-pa="insights" data-id="' + p.id + '">' + t("تحديث الأرقام", "Refresh stats") + "</button>";
    if (p.status === "pending_approval") actions += '<a class="btn sm" href="#/approvals">' + t("راجع واعتمد", "Review") + "</a>";
    if (p.status !== "publishing") actions += '<button class="btn g sm" data-pa="delete" data-id="' + p.id + '">' + t("حذف", "Delete") + "</button>";
    return '<div class="post">' + (m ? (m.kind === "video" ? '<video class="pm" src="' + esc(m.url) + '" muted preload="metadata"></video>' : '<img class="pm" src="' + esc(m.url) + '" alt="" loading="lazy">') : '<div class="pm"></div>') +
      '<div style="min-width:0"><div class="row" style="margin-bottom:6px">' + statusPill(p.status) + p.channels.map(function (c) { return '<span class="pill">' + esc(PLATFORM[c.platform] || c.platform) + "</span>"; }).join("") +
      '<span class="small muted">' + (p.published_at ? fmtDate(p.published_at) : p.scheduled_at ? fmtDate(p.scheduled_at) : "") + '</span></div><div class="cap">' + esc(p.caption) + '</div><div class="stats" style="margin-top:6px">' + stats + "</div>" + errs + "</div>" +
      '<div class="row acts2" style="flex-direction:column;align-items:stretch">' + actions + "</div></div>";
  }
  function postComposer(done) {
    Promise.all([api("GET", "/api/channels"), api("GET", "/api/media")]).then(function (r) {
      var chans = r[0].channels.filter(function (c) { return ["facebook", "instagram", "tiktok", "telegram"].indexOf(c.platform) >= 0; });
      var mediaList = r[1], sel = {};
      modal('<div class="row between"><h2>' + t("منشور جديد", "New post") + '</h2><button class="btn g" data-close>✕</button></div><form class="form" id="pf" style="margin-top:12px">' +
        '<label class="f">' + t("النص", "Caption") + '<textarea class="in" name="caption" required rows="5"></textarea></label>' +
        '<div><div class="small muted" style="margin-bottom:6px">' + t("الحسابات", "Accounts") + '</div><div class="chk">' + (chans.length ? chans.map(function (c) { return '<label><input type="checkbox" name="ch" value="' + c.id + '">' + esc(PLATFORM[c.platform]) + " · " + esc(c.name) + "</label>"; }).join("") : '<a href="#/channels" data-close>' + t("اربط حساباتك أولًا", "Connect accounts first") + "</a>") + "</div></div>" +
        '<div><div class="row between small muted" style="margin-bottom:6px"><span>' + t("الصورة أو الفيديو", "Media") + '</span><span><label class="btn g sm" style="cursor:pointer">' + t("رفع ملف", "Upload") + '<input type="file" id="up" accept="image/*,video/mp4,video/quicktime" hidden></label> <button type="button" class="btn g sm" id="dz">' + t("تصميم بالنص", "Design from text") + "</button></span></div>" +
        '<div class="thumbs" id="th">' + mediaList.slice(0, 18).map(thumb).join("") + "</div></div>" +
        '<label class="f">' + t("موعد النشر (توقيت الرياض) — اتركه فارغًا للنشر الآن", "Schedule (Riyadh time) — empty = now") + '<input class="in" type="datetime-local" name="when"></label>' +
        '<button class="btn" type="submit">' + t("اعتمد", "Submit") + "</button></form>", function (m) {
        function thumbs() { $("#th", m).innerHTML = mediaList.slice(0, 18).map(thumb).join(""); }
        $("#th", m).onclick = function (e) { var el = e.target.closest("[data-mid]"); if (!el) return; sel[el.dataset.mid] = !sel[el.dataset.mid]; el.classList.toggle("sel", !!sel[el.dataset.mid]); };
        $("#up", m).onchange = function (e) {
          var f = e.target.files[0]; if (!f) return;
          var fd = new FormData(); fd.append("file", f);
          toast(t("جاري الرفع…", "Uploading…"));
          api("POST", "/api/media", fd).then(function (row) { mediaList.unshift(row); sel = {}; sel[row.id] = true; thumbs(); $('[data-mid="' + row.id + '"]', m).classList.add("sel"); }).catch(fail);
        };
        $("#dz", m).onclick = function () {
          var h = prompt(t("العنوان الرئيسي للتصميم", "Headline for the design")); if (!h) return;
          var sub = prompt(t("سطر فرعي (اختياري)", "Subline (optional)")) || "";
          api("POST", "/api/media/design", { headline: h, subline: sub, style: "bold" }).then(function (row) { mediaList.unshift(row); sel = {}; sel[row.id] = true; thumbs(); $('[data-mid="' + row.id + '"]', m).classList.add("sel"); }).catch(fail);
        };
        $("#pf", m).onsubmit = function (e) {
          e.preventDefault();
          var f = e.target, ids = $$('input[name=ch]:checked', f).map(function (x) { return x.value; });
          if (!ids.length) return toast(t("اختر حسابًا واحدًا على الأقل", "Pick at least one account"), true);
          var mids = Object.keys(sel).filter(function (k) { return sel[k]; });
          api("POST", "/api/posts", { caption: f.caption.value, channel_ids: ids, media_ids: mids, scheduled_at: f.when.value ? f.when.value.replace("T", " ") : null, submit: true })
            .then(function () { m.remove(); toast(f.when.value ? t("تمت الجدولة ✓", "Scheduled ✓") : t("سيُنشر خلال لحظات ✓", "Publishing shortly ✓")); done(); }).catch(fail);
        };
      });
    }).catch(fail);
  }
  function thumb(x) { return x.kind === "video" ? '<video data-mid="' + x.id + '" src="' + esc(x.url) + '" muted preload="metadata"></video>' : '<img data-mid="' + x.id + '" src="' + esc(x.url) + '" alt="" loading="lazy">'; }

  // ---------- sites (Sari) ----------
  function pageSites(parts) {
    if (parts[1]) return pageSite(parts[1]);
    setTitle(t("مواقع ساري", "Sari's Sites"));
    view('<div class="page"><div class="head"><div><h1>' + t("مواقع ساري", "Sari's Sites") + "</h1><p>" + t("مواقع وصفحات هبوط حقيقية بروابط عامة ونماذج طلبات توصلك.", "Real sites with public links and lead forms.") +
      '</p></div><button class="btn" id="askSari">' + t("اطلب موقعًا من ساري", "Ask Sari for a site") + '</button></div><div id="sl"><span class="spin"></span></div></div>');
    $("#askSari").onclick = function () { startChat("sari", "أبي صفحة هبوط لنشاطي فيها نموذج طلب"); };
    api("GET", "/api/sites").then(function (list) {
      $("#sl").innerHTML = list.length ? '<div class="grid g3">' + list.map(function (s) {
        return '<a class="card" href="#/sites/' + s.id + '"><div class="row between"><h3>' + esc(s.title) + "</h3>" + statusPill(s.status === "published" ? "published" : "draft") + '</div><p class="small muted" dir="ltr" style="margin:8px 0 0;text-align:start">' + esc(s.url) + '</p><p class="small muted" style="margin:4px 0 0">' + t("نسخة ", "v") + s.version + " · " + ago(s.updated_at) + "</p></a>";
      }).join("") + "</div>" : '<div class="card empty"><h3>' + t("ما عندك مواقع بعد", "No sites yet") + "</h3><p>" + t("قل لساري عن نشاطك، ويبني لك صفحة تقدر تنشرها اليوم.", "Tell Sari about your business; he'll build a page you can publish today.") + "</p></div>";
    }).catch(fail);
  }
  function pageSite(id) {
    api("GET", "/api/sites/" + id).then(function (s) {
      setTitle(s.title);
      view('<div class="page wide"><div class="head"><div><a class="small" href="#/sites">← ' + t("المواقع", "Sites") + "</a><h1>" + esc(s.title) + "</h1><p>" + statusPill(s.status === "published" ? "published" : "draft") + ' <span dir="ltr" class="small">' + esc(s.url) + "</span></p></div>" +
        '<div class="row">' + (s.status === "published" ? '<a class="btn o sm" href="' + esc(s.url) + '" target="_blank" rel="noopener">' + t("فتح الموقع", "Open site") + '</a><button class="btn o sm" data-sa="unpublish">' + t("إيقاف النشر", "Unpublish") + "</button>" : "") +
        '<button class="btn sm" data-sa="publish">' + (s.status === "published" ? t("نشر آخر نسخة", "Publish latest") : t("انشر الموقع", "Publish")) + '</button><a class="btn o sm" href="/api/sites/' + s.id + '/zip">' + t("تحميل الكود", "Download code") + '</a><button class="btn o sm" id="editS">' + t("عدّل مع ساري", "Edit with Sari") + '</button><button class="btn g sm" data-sa="delete">' + t("حذف", "Delete") + "</button></div></div>" +
        '<iframe class="frame" sandbox="allow-scripts allow-forms" src="/api/sites/' + s.id + '/preview" title="preview"></iframe>' +
        '<p class="small muted" style="margin-top:10px">' + t("نماذج الموقع ترسل إلى: ", "Forms post to: ") + '<span dir="ltr">' + esc(s.form_endpoint) + "</span> — " + t("الطلبات توصل صفحة العملاء.", "leads arrive in Contacts.") + "</p></div>");
      $("#editS").onclick = function () { startChat("sari", "أبي أعدّل موقع «" + s.title + "» (رقم " + s.id + "): "); };
      $("#view").onclick = function (e) {
        var b = e.target.closest("[data-sa]"); if (!b) return;
        var a = b.dataset.sa;
        if (a === "delete") { if (!confirm(t("حذف الموقع نهائيًا؟", "Delete site?"))) return; return api("DELETE", "/api/sites/" + id).then(function () { go("#/sites"); }).catch(fail); }
        b.disabled = true;
        api("POST", "/api/sites/" + id + "/" + a).then(function () { toast(a === "publish" ? t("الموقع منشور ✓", "Published ✓") : t("تم إيقاف النشر", "Unpublished")); pageSite(id); }).catch(function (err) { b.disabled = false; fail(err); });
      };
    }).catch(function (e) { fail(e); go("#/sites"); });
  }

  // ---------- automations (Sanad) ----------
  function pageAutomations(parts) {
    if (parts[1]) return pageAutomation(parts[1]);
    setTitle(t("أتمتة سند", "Sanad's Automations"));
    view('<div class="page"><div class="head"><div><h1>' + t("أتمتة سند", "Sanad's Automations") + "</h1><p>" + t("بوتات وأتمتة تشتغل لحالها: مجدولة، وعند الرسائل والطلبات.", "Bots and automations that run on their own.") +
      '</p></div><button class="btn" id="askSanad">' + t("ابنِ أتمتة مع سند", "Build with Sanad") + '</button></div><div class="card pad0" id="al"><div class="empty"><span class="spin"></span></div></div></div>');
    $("#askSanad").onclick = function () { startChat("sanad", "وش المهام اللي أقدر أأتمتها في نشاطي؟"); };
    api("GET", "/api/automations").then(function (list) {
      $("#al").innerHTML = list.length ? list.map(function (a) {
        return '<a class="item" href="#/automations/' + a.id + '"><div class="grow"><div class="t">' + esc(a.name) + '</div><div class="small muted">' + esc(triggerText(a.trigger)) + " · " + a.steps.length + t(" خطوات", " steps") + (a.last_run_at ? " · " + t("آخر تشغيل ", "last run ") + ago(a.last_run_at) : "") + "</div></div>" + statusPill(a.status) + "</a>";
      }).join("") : '<div class="empty"><h3>' + t("لا توجد أتمتة بعد", "No automations yet") + "</h3><p>" + t("مثال: بوت واتساب يرد على عملائك، أو تقرير يومي الساعة 9.", "e.g. a WhatsApp bot, or a daily 9am report.") + "</p></div>";
    }).catch(fail);
  }
  function pageAutomation(id) {
    api("GET", "/api/automations/" + id).then(function (a) {
      setTitle(a.name);
      var steps = a.steps.map(function (s, i) { return '<div class="act done">' + (i + 1) + ". " + esc(s.type) + ' <span class="small muted" style="margin-inline-start:auto;max-width:60%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + esc(s.title || s.text || s.subject || s.url || s.instructions || s.template || "") + "</span></div>"; }).join("");
      var runs = a.runs.length ? a.runs.map(function (r) {
        return '<div class="item"><div class="grow"><div class="row">' + statusPill(r.status) + '<span class="small muted">' + fmtDate(r.started_at) + " · " + esc(r.trigger_kind) + "</span></div>" +
          r.log.map(function (l) { return '<div class="small ' + (l.ok ? "muted" : "") + '" style="' + (l.ok ? "" : "color:var(--bad)") + '">' + (l.step || "") + ". " + esc(l.type || "") + " — " + esc(l.ok ? l.result : l.error) + "</div>"; }).join("") + "</div></div>";
      }).join("") : '<div class="empty">' + t("لم تُشغّل بعد.", "No runs yet.") + "</div>";
      view('<div class="page"><div class="head"><div><a class="small" href="#/automations">← ' + t("الأتمتة", "Automations") + "</a><h1>" + esc(a.name) + "</h1><p>" + statusPill(a.status) + " " + esc(triggerText(a.trigger)) + (a.next_run_at ? " · " + t("التشغيل القادم ", "next run ") + fmtDate(a.next_run_at) : "") + "</p></div>" +
        '<div class="row">' + (a.status === "active" ? '<button class="btn o sm" data-aa="run">' + t("تشغيل تجريبي", "Test run") + '</button><button class="btn o sm" data-aa="pause">' + t("إيقاف", "Pause") + "</button>" : '<button class="btn ok sm" data-aa="activate">' + t("تفعيل", "Activate") + "</button>") +
        '<button class="btn o sm" id="askD">' + t("اسأل سند", "Ask Sanad") + '</button><button class="btn g sm" data-aa="delete">' + t("حذف", "Delete") + "</button></div></div>" +
        (a.webhook_url ? '<div class="card" style="margin-bottom:14px"><div class="small muted" style="margin-bottom:6px">' + t("رابط الـWebhook — ضعه في متجرك أو أداتك:", "Webhook URL — paste it in your store/tool:") + '</div><div class="code">' + esc(a.webhook_url) + "</div></div>" : "") +
        '<div class="grid g2"><div class="card"><h3 style="margin-bottom:10px">' + t("الخطوات", "Steps") + '</h3><div class="acts">' + steps + '</div>' + (a.description ? '<p class="small muted">' + esc(a.description) + "</p>" : "") + '</div><div class="card pad0"><div class="item"><h3>' + t("سجل التشغيل", "Run log") + "</h3></div>" + runs + "</div></div></div>");
      $("#askD").onclick = function () { startChat("sanad", "راجع أتمتة «" + a.name + "» (رقم " + a.id + ") وسجل تشغيلها وقل لي إذا فيه مشكلة"); };
      $("#view").onclick = function (e) {
        var b = e.target.closest("[data-aa]"); if (!b) return;
        var act = b.dataset.aa;
        if (act === "delete") { if (!confirm(t("حذف الأتمتة؟", "Delete automation?"))) return; return api("DELETE", "/api/automations/" + id).then(function () { go("#/automations"); }).catch(fail); }
        b.disabled = true;
        api("POST", "/api/automations/" + id + "/" + act).then(function (r) { if (act === "run") toast(r.status === "succeeded" ? t("نجح التشغيل ✓", "Run succeeded ✓") : t("تعثّر التشغيل، شوف السجل", "Run failed — see log"), r.status !== "succeeded"); pageAutomation(id); }).catch(function (err) { b.disabled = false; fail(err); });
      };
    }).catch(function (e) { fail(e); go("#/automations"); });
  }

  // ---------- channels ----------
  function pageChannels(parts, q) {
    setTitle(t("الحسابات المربوطة", "Connections"));
    if (q.connected) toast(t("تم ربط ", "Connected ") + q.connected + t(" حساب ✓", " account(s) ✓"));
    if (q.error) toast(t("تعذّر الربط، حاول مرة أخرى.", "Connection failed, try again."), true);
    view('<div class="page"><div class="head"><div><h1>' + t("الحسابات المربوطة", "Connections") + "</h1><p>" + t("اربط حساباتك مرة وحدة، والفريق يشتغل عليها بعد موافقتك. مفاتيحك مشفرة.", "Connect once; the team works on them after your approval. Keys are encrypted.") + '</p></div></div><div id="chl"><span class="spin"></span></div></div>');
    api("GET", "/api/channels").then(function (d) {
      var av = d.available;
      var cards = [
        ["meta", "إنستقرام + فيسبوك", "Instagram + Facebook", "سلاف تنشر وتجدول وتقرأ التعليقات والأرقام.", "Sulaf publishes, schedules, reads comments & stats.", av.meta ? '<button class="btn sm" data-connect="meta">' + t("ربط عبر Meta", "Connect with Meta") + "</button>" : '<span class="pill">' + t("يتطلب إعداد الخادم", "Needs server setup") + "</span>"],
        ["tiktok", "تيك توك", "TikTok", "نشر الفيديوهات والصور. (قبل اعتماد تطبيقك من تيك توك يكون النشر خاصًا فقط)", "Video & photo posting (private-only until TikTok audits the app).", av.tiktok ? '<button class="btn sm" data-connect="tiktok">' + t("ربط تيك توك", "Connect TikTok") + "</button>" : '<span class="pill">' + t("يتطلب إعداد الخادم", "Needs server setup") + "</span>"],
        ["telegram", "تيليجرام", "Telegram", "نشر في قناتك، أو بوت يرد على عملائك.", "Post to your channel, or run a customer bot.", '<button class="btn sm" data-form="telegram">' + t("ربط بوت", "Connect bot") + "</button>"],
        ["whatsapp", "واتساب للأعمال", "WhatsApp Business", "بوت خدمة عملاء وحملات بقوالب معتمدة.", "Customer bot and template campaigns.", '<button class="btn sm" data-form="whatsapp">' + t("ربط رقم", "Connect number") + "</button>"],
        ["n8n", "n8n", "n8n", "سند يرسل الأتمتة إلى n8n الخاص بك.", "Sanad exports automations to your n8n.", '<button class="btn sm" data-form="n8n">' + t("ربط", "Connect") + "</button>"],
        ["snapchat", "سناب شات", "Snapchat", "النشر الآلي على سناب متاح فقط لشركاء سناب المعتمدين؛ سلاف تجهز لك المحتوى والتصاميم لتنشرها بنفسك.", "Snap only allows automated posting for approved partners; Sulaf prepares content for you to post.", '<span class="pill">' + t("نشر يدوي", "Manual posting") + "</span>"]
      ];
      var html = '<div class="grid g3" style="margin-bottom:22px">' + cards.map(function (c) {
        return '<div class="card"><h3>' + t(c[1], c[2]) + '</h3><p class="small muted" style="margin:6px 0 14px;min-height:3.4em">' + t(c[3], c[4]) + "</p>" + c[5] + "</div>";
      }).join("") + "</div>";
      html += '<div class="card pad0"><div class="item"><h3>' + t("حساباتك", "Your connections") + "</h3></div>" + (d.channels.length ? d.channels.map(function (c) {
        return '<div class="item"><div class="grow"><div class="t">' + esc(PLATFORM[c.platform] || c.platform) + " · " + esc(c.name) + "</div>" + (c.last_error ? '<div class="small" style="color:var(--bad)">' + esc(c.last_error) + "</div>" : '<div class="small muted">' + t("رُبط ", "since ") + fmtDate(c.created_at, false) + "</div>") + "</div>" + statusPill(c.status) +
          '<button class="btn g sm" data-del="' + c.id + '">' + t("فصل", "Disconnect") + "</button></div>";
      }).join("") : '<div class="empty">' + t("لا توجد حسابات مربوطة.", "Nothing connected yet.") + "</div>") + "</div>";
      if (d.available.whatsapp_webhook) html += '<p class="small muted" style="margin-top:12px">' + t("رابط Webhook واتساب (للمسؤول): ", "WhatsApp webhook (admin): ") + '<span dir="ltr">' + esc(d.available.whatsapp_webhook) + "</span></p>";
      $("#chl").innerHTML = html;
    }).catch(fail);
    $("#view").onclick = function (e) {
      var b = e.target.closest("[data-connect],[data-form],[data-del]"); if (!b) return;
      if (b.dataset.connect) { b.disabled = true; api("POST", "/api/channels/" + b.dataset.connect + "/start").then(function (r) { location.href = r.url; }).catch(function (err) { b.disabled = false; fail(err); }); }
      else if (b.dataset.del) { if (confirm(t("فصل الحساب؟", "Disconnect?"))) api("DELETE", "/api/channels/" + b.dataset.del).then(function () { pageChannels(parts, {}); }).catch(fail); }
      else connectForm(b.dataset.form, function () { pageChannels(parts, {}); });
    };
  }
  function connectForm(kind, done) {
    var forms = {
      telegram: [t("ربط تيليجرام", "Connect Telegram"), '<label class="f">' + t("توكن البوت (من @BotFather)", "Bot token (from @BotFather)") + '<input class="in" name="bot_token" required dir="ltr"></label><label class="f">' + t("القناة أو المجموعة (اختياري) — مثل @mychannel، والبوت لازم يكون مشرفًا فيها", "Channel/group (optional) — e.g. @mychannel; the bot must be admin") + '<input class="in" name="chat_id" dir="ltr"></label><p class="hint">' + t("بدون قناة: يصير بوت خدمة عملاء يستقبل الرسائل الخاصة (تفعّله من أتمتة سند).", "Without a channel it works as a customer-service bot (activate via Sanad).") + "</p>"],
      whatsapp: [t("ربط واتساب للأعمال", "Connect WhatsApp Business"), '<p class="hint">' + t("من Meta Business: أنشئ تطبيقًا بمنتج WhatsApp، ثم انسخ Phone Number ID، وأنشئ توكن دائم لمستخدم نظام بصلاحية whatsapp_business_messaging.", "From Meta Business: app with WhatsApp product → Phone Number ID → permanent System User token with whatsapp_business_messaging.") + '</p><label class="f">Phone Number ID<input class="in" name="phone_number_id" required dir="ltr"></label><label class="f">WhatsApp Business Account ID<input class="in" name="waba_id" dir="ltr"></label><label class="f">' + t("التوكن الدائم", "Permanent token") + '<input class="in" name="access_token" required dir="ltr" type="password"></label>'],
      n8n: [t("ربط n8n", "Connect n8n"), '<label class="f">' + t("رابط n8n", "n8n URL") + '<input class="in" name="base_url" required dir="ltr" placeholder="https://n8n.example.com"></label><label class="f">API key<input class="in" name="api_key" required dir="ltr" type="password"></label>']
    };
    var f = forms[kind];
    modal('<div class="row between"><h2>' + f[0] + '</h2><button class="btn g" data-close>✕</button></div><form class="form" id="cfm" style="margin-top:12px">' + f[1] + '<button class="btn">' + t("ربط", "Connect") + "</button></form>", function (m) {
      $("#cfm", m).onsubmit = function (e) {
        e.preventDefault();
        var body = {}; new FormData(e.target).forEach(function (v, k) { body[k] = v; });
        var b = $("button", e.target); b.disabled = true; b.innerHTML = '<span class="spin"></span>';
        api("POST", "/api/channels/" + kind, body).then(function () { m.remove(); toast(t("تم الربط ✓", "Connected ✓")); done(); }).catch(function (err) { b.disabled = false; b.textContent = t("ربط", "Connect"); fail(err); });
      };
    });
  }

  // ---------- contacts ----------
  function pageContacts() {
    setTitle(t("العملاء", "Contacts"));
    view('<div class="page"><div class="head"><div><h1>' + t("العملاء", "Contacts") + "</h1><p>" + t("عملاؤك من نماذج المواقع ورسائل واتساب والاستيراد. حملات واتساب تُرسل فقط لمن وافق على الاستلام.", "From site forms, WhatsApp and imports. Campaigns go only to opted-in contacts.") +
      '</p></div><div class="row"><button class="btn o" id="imp">' + t("استيراد CSV", "Import CSV") + '</button><button class="btn" id="addC">+ ' + t("عميل", "Contact") + '</button></div></div><div class="card pad0" id="cl"><div class="empty"><span class="spin"></span></div></div></div>');
    function load() {
      api("GET", "/api/contacts").then(function (list) {
        $("#cl").innerHTML = list.length ? list.map(function (c) {
          return '<div class="item"><div class="grow"><div class="t">' + esc(c.name || c.phone || c.email) + '</div><div class="small muted" dir="ltr" style="text-align:start">' + esc([c.phone, c.email].filter(Boolean).join(" · ")) + "</div>" + (c.note ? '<div class="small">' + esc(c.note) + "</div>" : "") +
            '<div class="row small" style="margin-top:4px">' + (c.tags ? c.tags.split(",").map(function (x) { return '<span class="pill">' + esc(x) + "</span>"; }).join("") : "") + '<span class="muted">' + esc(c.source) + " · " + ago(c.created_at) + "</span></div></div>" +
            '<label class="small row nowrap"><input type="checkbox" data-opt="' + c.id + '"' + (c.opted_in ? " checked" : "") + "> " + t("موافق على رسائل واتساب", "WhatsApp opt-in") + '</label><button class="btn g sm" data-delc="' + c.id + '">✕</button></div>';
        }).join("") : '<div class="empty"><h3>' + t("لا يوجد عملاء بعد", "No contacts yet") + "</h3><p>" + t("أي طلب من موقعك أو رسالة واتساب يضاف هنا تلقائيًا.", "Site leads and WhatsApp messages land here automatically.") + "</p></div>";
      }).catch(fail);
    }
    load();
    $("#cl").onchange = function (e) { var x = e.target.closest("[data-opt]"); if (x) api("PATCH", "/api/contacts/" + x.dataset.opt, { opted_in: x.checked }).catch(fail); };
    $("#cl").onclick = function (e) { var x = e.target.closest("[data-delc]"); if (x && confirm(t("حذف العميل؟", "Delete?"))) api("DELETE", "/api/contacts/" + x.dataset.delc).then(load).catch(fail); };
    $("#addC").onclick = function () {
      modal('<h2>' + t("عميل جديد", "New contact") + '</h2><form class="form" id="acf" style="margin-top:12px"><label class="f">' + t("الاسم", "Name") + '<input class="in" name="name"></label><label class="f">' + t("الجوال (بالصيغة الدولية مثل 9665…)", "Phone (international, e.g. 9665…)") + '<input class="in" name="phone" dir="ltr"></label><label class="f">' + t("البريد", "Email") + '<input class="in" name="email" type="email" dir="ltr"></label><label class="f">' + t("وسوم (مفصولة بفاصلة)", "Tags (comma separated)") + '<input class="in" name="tags"></label><label class="row small"><input type="checkbox" name="opted_in"> ' + t("وافق على استلام رسائل واتساب", "Agreed to WhatsApp messages") + '</label><button class="btn">' + t("إضافة", "Add") + "</button></form>", function (m) {
        $("#acf", m).onsubmit = function (e) { e.preventDefault(); var f = e.target; api("POST", "/api/contacts", { name: f.name.value, phone: f.phone.value, email: f.email.value, tags: f.tags.value, opted_in: f.opted_in.checked ? "1" : "" }).then(function () { m.remove(); load(); }).catch(fail); };
      });
    };
    $("#imp").onclick = function () {
      modal('<h2>' + t("استيراد CSV", "Import CSV") + '</h2><p class="hint">' + t("الأعمدة: name, phone, email, tags, opted_in (1 للموافقين)", "Columns: name, phone, email, tags, opted_in (1 = opted in)") + '</p><form class="form" id="icf"><input type="file" accept=".csv,text/csv" class="in" name="file" required><button class="btn">' + t("استيراد", "Import") + "</button></form>", function (m) {
        $("#icf", m).onsubmit = function (e) { e.preventDefault(); var file = e.target.file.files[0]; file.text().then(function (txt) { return api("POST", "/api/contacts", { csv: txt }); }).then(function (r) { m.remove(); toast(t("أضيف ", "Added ") + r.added); load(); }).catch(fail); };
      });
    };
  }

  // ---------- business profile ----------
  function pageProfile(parts, q) {
    setTitle(t("ملف النشاط", "Business profile"));
    api("GET", "/api/profile").then(function (p) {
      var F = [["business_name", "اسم النشاط", "Business name", "in"], ["industry", "المجال", "Industry", "in"], ["city", "المدينة", "City", "in"], ["contact", "طرق التواصل (جوال، إيميل، موقع)", "Contact details", "in"],
        ["description", "وصف النشاط", "Description", "ta"], ["products", "المنتجات والخدمات والأسعار", "Products, services & prices", "ta"], ["audience", "الجمهور المستهدف", "Target audience", "ta"],
        ["tone", "أسلوب الكلام (رسمي، ودود، شبابي…)", "Brand tone", "in"], ["faq", "أسئلة شائعة وأجوبتها (يعتمد عليها بوت خدمة العملاء)", "FAQ (used by the customer bot)", "ta"]];
      view('<div class="page" style="max-width:820px"><div class="head"><div><h1>' + t("ملف النشاط", "Business profile") + "</h1><p>" +
        (q.welcome ? t("هلا وغلا! عبّ هذا الملف مرة وحدة، والفريق كله يعتمد عليه: سلاف في المحتوى، ساري في الموقع، سند في ردود البوت.", "Welcome! Fill this once — the whole team relies on it.") : t("الفريق كله يعتمد على هذه المعلومات.", "The whole team relies on this.")) +
        '</p></div></div><form class="card form" id="prf">' + F.map(function (f) {
          return '<label class="f">' + t(f[1], f[2]) + (f[3] === "ta" ? '<textarea class="in" name="' + f[0] + '">' + esc(p[f[0]]) + "</textarea>" : '<input class="in" name="' + f[0] + '" value="' + esc(p[f[0]]) + '">') + "</label>";
        }).join("") + '<label class="f">' + t("لون الهوية", "Brand color") + '<input type="color" name="brand_color" value="' + esc(p.brand_color || "#6d3df5") + '" style="width:80px;height:40px;background:none;border:0"></label>' +
        '<div class="row"><button class="btn">' + t("حفظ", "Save") + "</button>" + (q.welcome ? '<a class="btn o" href="#/team">' + t("لاحقًا", "Later") + "</a>" : "") + "</div></form></div>");
      $("#prf").onsubmit = function (e) {
        e.preventDefault();
        var body = {}; new FormData(e.target).forEach(function (v, k) { body[k] = v; });
        api("PUT", "/api/profile", body).then(function () { toast(t("تم الحفظ ✓", "Saved ✓")); if (q.welcome) go("#/team"); }).catch(fail);
      };
    }).catch(fail);
  }

  // ---------- billing ----------
  function pageBilling(parts, q) {
    setTitle(t("الاشتراك", "Billing"));
    if (q.paid) toast(t("شكرًا! سيُفعَّل اشتراكك خلال لحظات بعد تأكيد الدفع.", "Thanks! Your plan activates once payment is confirmed."));
    api("GET", "/api/billing").then(function (b) {
      var cur = b.subscription.plan;
      var LBL = { messages: "رسالة/شهر", posts: "منشور/شهر", channels: "حساب مربوط", sites_published: "موقع منشور", automations_active: "أتمتة مفعّلة", whatsapp_sends: "رسالة واتساب/شهر", projects: "مشروع" };
      view('<div class="page"><div class="head"><div><h1>' + t("الاشتراك", "Billing") + "</h1><p>" + t("باقتك الحالية: ", "Current plan: ") + "<b>" + esc(STATE.me.plan_name) + "</b>" + (b.subscription.current_period_end ? t(" — تنتهي ", " — renews ") + fmtDate(b.subscription.current_period_end, false) : "") + "</p></div></div>" +
        '<div class="grid g3" style="margin-bottom:18px">' + b.plans.map(function (p) {
          return '<div class="card" style="' + (p.id === (q.plan || cur) ? "border-color:var(--pu2)" : "") + '"><div class="row between"><h2>' + esc(p.name) + "</h2>" + (p.id === cur ? '<span class="pill ok">' + t("باقتك", "Current") + "</span>" : "") + '</div><div style="font-size:32px;font-weight:700;margin:6px 0">' + (p.price_sar ? p.price_sar + ' <span class="small muted">' + t("ريال/شهر", "SAR/mo") + "</span>" : t("مجانًا", "Free")) + "</div>" +
            '<ul class="small" style="padding-inline-start:18px;color:var(--ag)">' + Object.keys(LBL).filter(function (k) { return p.limits[k] !== 0; }).map(function (k) { return "<li>" + (p.limits[k] == null ? (k === "projects" ? t("مشاريع غير محدودة", "Unlimited projects") : t("غير محدود: ", "Unlimited: ") + LBL[k]) : p.limits[k] + " " + LBL[k]) + "</li>"; }).join("") + "</ul>" +
            (p.price_sar ? '<button class="btn" style="width:100%" data-plan="' + p.id + '"' + (b.payments_enabled ? "" : " disabled") + ">" + (p.id === cur ? t("جدّد شهرًا", "Renew a month") : t("اشترك", "Subscribe")) + "</button>" : "") + "</div>";
        }).join("") + "</div>" + (b.payments_enabled ? "" : '<p class="small muted">' + t("الدفع الإلكتروني سيُفعَّل قريبًا.", "Online payment is coming soon.") + "</p>") +
        '<div class="card" style="margin-bottom:18px"><h3 style="margin-bottom:12px">' + t("استخدامك هذا الشهر", "Usage this month") + "</h3>" + usageBars(b.usage) + "</div>" +
        '<div class="card pad0"><div class="item"><h3>' + t("الفواتير", "Invoices") + "</h3></div>" + (b.invoices.length ? b.invoices.map(function (i) {
          return '<div class="item"><div class="grow"><div class="t">' + (i.amount_halalas / 100) + t(" ريال", " SAR") + " · " + esc(i.plan) + '</div><div class="small muted">' + fmtDate(i.created_at) + "</div></div>" + statusPill(i.status) + (i.status !== "paid" && i.url ? '<a class="btn sm" href="' + esc(i.url) + '">' + t("ادفع", "Pay") + "</a>" : "") + "</div>";
        }).join("") : '<div class="empty">' + t("لا توجد فواتير.", "No invoices.") + "</div>") + "</div></div>");
      $("#view").onclick = function (e) {
        var x = e.target.closest("[data-plan]"); if (!x) return;
        x.disabled = true; x.innerHTML = '<span class="spin"></span>';
        api("POST", "/api/billing/checkout", { plan: x.dataset.plan }).then(function (r) { location.href = r.url; }).catch(function (err) { x.disabled = false; x.textContent = t("اشترك", "Subscribe"); fail(err); });
      };
    }).catch(fail);
  }

  // ---------- settings ----------
  function pageSettings() {
    setTitle(t("الإعدادات", "Settings"));
    var me = STATE.me;
    view('<div class="page" style="max-width:720px"><div class="head"><h1>' + t("الإعدادات", "Settings") + "</h1></div>" +
      '<form class="card form" id="sf" style="margin-bottom:16px"><h3>' + t("الحساب", "Account") + '</h3><label class="f">' + t("الاسم", "Name") + '<input class="in" name="name" value="' + esc(me.name) + '"></label><label class="f">' + t("البريد", "Email") + '<input class="in" value="' + esc(me.email) + '" disabled dir="ltr"></label>' +
      '<label class="f">' + t("لغة الواجهة", "Language") + '<select class="in" name="lang"><option value="ar"' + (LANG === "ar" ? " selected" : "") + '>العربية</option><option value="en"' + (LANG === "en" ? " selected" : "") + '>English</option></select></label><div><button class="btn">' + t("حفظ", "Save") + "</button></div></form>" +
      '<form class="card form" id="pwf" style="margin-bottom:16px"><h3>' + t("كلمة المرور", "Password") + '</h3><label class="f">' + t("الحالية", "Current") + '<input class="in" type="password" name="current" required autocomplete="current-password"></label><label class="f">' + t("الجديدة", "New") + '<input class="in" type="password" name="new" required minlength="8" autocomplete="new-password"></label><div><button class="btn">' + t("تغيير", "Change") + "</button></div></form>" +
      '<div class="card" style="margin-bottom:16px"><h3>' + t("الخروج", "Sign out") + '</h3><p class="small muted">' + t("تسجيل الخروج من هذا الجهاز.", "Sign out of this device.") + '</p><button class="btn o" id="lo">' + t("تسجيل الخروج", "Sign out") + "</button></div>" +
      '<div class="card" style="border-color:#5a2a2a"><h3>' + t("حذف الحساب", "Delete account") + '</h3><p class="small muted">' + t("يحذف حسابك وكل بياناتك نهائيًا: المحادثات، المشاريع، المواقع، الأتمتة، العملاء، والملفات.", "Permanently deletes your account and all data.") + '</p><button class="btn bad" id="del">' + t("حذف حسابي", "Delete my account") + "</button></div></div>");
    $("#sf").onsubmit = function (e) {
      e.preventDefault(); var f = e.target;
      api("PATCH", "/api/me", { name: f.name.value, lang: f.lang.value }).then(function (u) {
        STATE.me = u; toast(t("تم الحفظ ✓", "Saved ✓"));
        if (f.lang.value !== LANG) { LANG = f.lang.value; try { localStorage.setItem("sahm_lang", LANG); } catch (x) {} applyLang(); $("#root").innerHTML = ""; route(); }
      }).catch(fail);
    };
    $("#pwf").onsubmit = function (e) { e.preventDefault(); var f = e.target; api("POST", "/api/me/password", { current: f.current.value, new: f.new.value }).then(function () { f.reset(); toast(t("تم تغيير كلمة المرور، وخرجت الأجهزة الأخرى.", "Password changed; other devices signed out.")); }).catch(fail); };
    $("#lo").onclick = function () { api("POST", "/api/auth/logout").then(function () { STATE.me = null; location.href = "/"; }); };
    $("#del").onclick = function () {
      var pw = prompt(t("للتأكيد اكتب كلمة المرور:", "Enter your password to confirm:")); if (!pw) return;
      api("POST", "/api/me/delete", { password: pw }).then(function () { location.href = "/"; }).catch(fail);
    };
  }

  // ---------- boot ----------
  function boot() {
    return api("GET", "/api/personas").then(function (list) {
      STATE.personas = list; list.forEach(function (p) { PERS[p.id] = p; });
    });
  }
  window.addEventListener("hashchange", route);
  api("GET", "/api/me", undefined, { allow401: true }).then(function (me) { STATE.me = me; }).catch(function () { STATE.me = null; })
    .then(boot).then(function () {
      if (!STATE.me) {
        var p = parse();
        if (p.parts[0] !== "login" && p.parts[0] !== "register") { history.replaceState(null, "", "#/login?next=" + encodeURIComponent(location.hash || "#/")); }
      }
      route();
    }).catch(function () { $("#root").innerHTML = '<div class="auth"><div class="card">' + t("تعذّر الاتصال بالخادم.", "Cannot reach the server.") + "</div></div>"; });
})();
