(function () {
  "use strict";
  var TEAM = [
    { id: "sulaf", n: "سلاف", e: "SULAF", r: "التسويق وإدارة الحسابات", c: "#d27bff", q: "معك من أول فكرة، إلى أن توصل رسالتك للناس الصح.",
      s: ["تنشر وتجدول على حساباتك", "تصمم منشوراتك بهويتك", "تقارير أداء وحملات واتساب"] },
    { id: "najd", n: "نجد", e: "NAJD", r: "إدارة المشاريع والعمليات", c: "#9b7bff", q: "أرتّب معك خطواتك، ونمشي نحو هدفك بثبات.",
      s: ["خطط بمهام ومواعيد حقيقية", "توزع المهام على الفريق", "تذكيرات وتقارير أسبوعية"] },
    { id: "sari", n: "ساري", e: "SARI", r: "البرمجة والحلول التقنية", c: "#6d9bff", q: "خلّنا نحول فكرتك إلى حل تقني يشتغل على أرض الواقع.",
      s: ["يبني موقعك وينشره", "نماذج طلبات توصلك", "مراجعة كود وإصلاح أخطاء"] },
    { id: "sanad", n: "سند", e: "SANAD", r: "الأتمتة والدعم التقني", c: "#b8b8cc", q: "أنا سندك، أساعدك تختصر وقتك وتخلّي شغلك يمشي بسلاسة.",
      s: ["بوت خدمة عملاء واتساب وتيليجرام", "أتمتة مجدولة وعند الطلبات", "ربط متجرك وn8n"] }
  ];

  function el(tag, attrs, html) {
    var x = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) { x.setAttribute(k, attrs[k]); });
    if (html != null) x.innerHTML = html;
    return x;
  }
  function esc(t) { return String(t).replace(/[&<>"]/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]; }); }

  var grid = document.getElementById("grid");
  if (grid) {
    TEAM.forEach(function (p) {
      var a = el("article", { class: "card", style: "--c:" + p.c });
      a.innerHTML =
        '<div class="ph"><img src="/assets/img/team/' + p.id + '.jpg" alt="' + esc(p.n) + '" loading="lazy"></div>' +
        '<div class="cb"><h3>' + esc(p.n) + "<small>" + p.e + '</small></h3><div class="role">' + esc(p.r) + "</div>" +
        "<q>«" + esc(p.q) + "»</q><ul>" + p.s.map(function (x) { return "<li>" + esc(x) + "</li>"; }).join("") + "</ul>" +
        '<a class="btn" href="/app#/chat/' + p.id + '">تحدث معي الآن</a></div>';
      grid.appendChild(a);
    });
  }

  var ft = document.getElementById("ftTeam");
  if (ft) {
    ft.innerHTML = TEAM.map(function (p) {
      return '<li><a class="who" href="/app#/chat/' + p.id + '"><img src="/assets/img/team/' + p.id + '.jpg" alt=""><span>' + esc(p.n) + "<small>" + esc(p.r) + "</small></span></a></li>";
    }).join("");
  }

  var plans = document.getElementById("plans");
  if (plans) {
    var LABEL = { messages: "رسالة شهريًا مع الفريق", posts: "منشور شهريًا", channels: "حساب مربوط", sites_published: "موقع منشور",
      automations_active: "أتمتة مفعّلة", whatsapp_sends: "رسالة واتساب شهريًا", projects: "مشروع" };
    fetch("/api/plans").then(function (r) { return r.json(); }).then(function (list) {
      plans.innerHTML = list.map(function (p) {
        var items = Object.keys(LABEL).filter(function (k) { return p.limits[k] !== 0; }).map(function (k) {
          var v = p.limits[k];
          return "<li>" + (v == null ? (k === "projects" ? "مشاريع غير محدودة" : "غير محدود: " + LABEL[k]) : v.toLocaleString("ar-SA") + " " + LABEL[k]) + "</li>";
        }).join("");
        return '<div class="plan' + (p.id === "pro" ? " hot" : "") + '"><h3>' + esc(p.name) + '</h3><div class="price">' +
          (p.price_sar ? p.price_sar.toLocaleString("ar-SA") + " <small>ريال / شهريًا</small>" : "مجانًا") + "</div><ul>" + items +
          '</ul><a class="btn' + (p.id === "pro" ? "" : " o") + '" href="/app#/register?plan=' + p.id + '">' + (p.price_sar ? "اشترك الآن" : "ابدأ مجانًا") + "</a></div>";
      }).join("");
    }).catch(function () { plans.closest("section").hidden = true; });
  }
})();
