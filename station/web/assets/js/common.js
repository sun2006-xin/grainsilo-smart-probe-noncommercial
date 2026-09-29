/* ============================================================
   common.js - 公共工具（API 封装 + 格式化 + 温度颜色映射）
   ============================================================ */

/* ---------- 仓库上下文 ---------- */
(function () {
  "use strict";
  const key = "grainsilo.activeWarehouseId";
  const nativeFetch = window.fetch.bind(window);
  window.fetch = function (input, init) {
    const requestUrl = typeof input === "string" ? input : input.url;
    const url = new URL(requestUrl, window.location.href);
    if (url.origin === window.location.origin && url.pathname.startsWith("/api/v1/") &&
        url.pathname !== "/api/v1/warehouses") {
      const baseHeaders = init && init.headers !== undefined ? init.headers :
        (input instanceof Request ? input.headers : undefined);
      const headers = new Headers(baseHeaders || {});
      headers.set("X-Warehouse-ID", localStorage.getItem(key) || "1");
      init = Object.assign({}, init || {}, { headers: headers });
    }
    return nativeFetch(input, init);
  };

  function addWarehouseNavigation() {
    const nav = document.querySelector(".app-nav, .station-page-nav");
    if (!nav || nav.querySelector('[data-page="warehouses"]')) return;
    const link = document.createElement("a");
    link.href = "/warehouses.html";
    link.dataset.page = "warehouses";
    link.textContent = "仓库档案";
    if (document.body.dataset.stationPage === "warehouses") {
      link.className = "active";
      link.setAttribute("aria-current", "page");
    }
    const overview = nav.querySelector('[data-page="overview"]');
    if (overview) overview.insertAdjacentElement("afterend", link);
    else nav.appendChild(link);
  }

  function mountWarehouseSelector() {
    let select = document.getElementById("warehouseSelect");
    if (!select) {
      const title = document.querySelector(".station-page-title-row");
      if (!title) return null;
      const control = document.createElement("label");
      control.className = "station-warehouse-switcher";
      control.innerHTML = '<span>当前仓库</span><select id="warehouseSelect" aria-label="选择仓库"></select>';
      title.appendChild(control);
      select = control.querySelector("select");
    }
    return select;
  }

  async function initWarehouseContext() {
    addWarehouseNavigation();
    const select = mountWarehouseSelector();
    if (!select) return;
    select.disabled = true;
    try {
      const response = await nativeFetch("/api/v1/warehouses", { cache: "no-store" });
      if (!response.ok) throw new Error("HTTP " + response.status);
      const result = await response.json();
      const warehouses = Array.isArray(result.data) ? result.data : [];
      if (!warehouses.length) throw new Error("没有可用仓库档案");
      let activeId = localStorage.getItem(key) || "1";
      if (!warehouses.some(warehouse => String(warehouse.id) === activeId)) {
        activeId = String(warehouses[0].id);
        localStorage.setItem(key, activeId);
      }
      select.innerHTML = warehouses.map(warehouse =>
        '<option value="' + esc(warehouse.id) + '">' + esc(warehouse.name) +
        ' · ' + (warehouse.shape_type === "silo" ? "圆筒仓" : "平房仓") + "</option>"
      ).join("");
      select.value = activeId;
      select.disabled = false;
      select.addEventListener("change", function () {
        localStorage.setItem(key, select.value);
        window.location.reload();
      });
    } catch (error) {
      select.innerHTML = '<option value="1">仓库列表读取失败</option>';
      select.disabled = true;
      select.title = error.message;
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initWarehouseContext, { once: true });
  } else {
    initWarehouseContext();
  }
})();

/* ---------- API 封装 ---------- */
async function apiGet(path) {
  const r = await fetch(path);
  if (!r.ok) throw new Error("HTTP " + r.status);
  return r.json();
}

async function apiPost(path, obj) {
  const r = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(obj),
  });
  if (!r.ok) throw new Error("HTTP " + r.status);
  return r.json();
}

/* ---------- 格式化 ---------- */
function esc(s) {
  return String(s).replace(/[&<>"']/g,
    c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function fmtTime(ts) {
  if (!ts) return "-";
  const d = new Date(ts * 1000);
  const p = n => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

function fmtShort(ts) {
  if (!ts) return "-";
  const d = new Date(ts * 1000);
  const p = n => String(n).padStart(2, "0");
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

/* ---------- 温度颜色映射（连续渐变：18°C 蓝 -> 32°C 红，区间外取端点色） ---------- */
var TEMP_STOPS = [
  [18, "#2b6bf3"], [22, "#33b7f0"], [25, "#37d3a0"],
  [28, "#f2d34e"], [30, "#f2943e"], [32, "#e23c3c"]
];
function tempColor(t) {
  if (t === null || t === undefined) return "#5b6b8c";
  if (t <= TEMP_STOPS[0][0]) return TEMP_STOPS[0][1];
  for (var i = 1; i < TEMP_STOPS.length; i++) {
    if (t <= TEMP_STOPS[i][0]) {
      var a = TEMP_STOPS[i - 1], b = TEMP_STOPS[i];
      var p = (t - a[0]) / (b[0] - a[0]);
      var ca = [parseInt(a[1].slice(1, 3), 16), parseInt(a[1].slice(3, 5), 16), parseInt(a[1].slice(5, 7), 16)];
      var cb = [parseInt(b[1].slice(1, 3), 16), parseInt(b[1].slice(3, 5), 16), parseInt(b[1].slice(5, 7), 16)];
      var c = [0, 1, 2].map(function (k) { return Math.round(ca[k] + (cb[k] - ca[k]) * p); });
      return "#" + c.map(function (v) { return ("0" + v.toString(16)).slice(-2); }).join("");
    }
  }
  return TEMP_STOPS[TEMP_STOPS.length - 1][1];
}

/* ---------- 状态徽章 ---------- */
function statusBadge(pole) {
  if (!pole.snapshot) return '<span class="badge warn">无数据</span>';
  const offline = pole.status === 0;
  const alarm = pole.snapshot.alarm === 1;
  let s = "";
  if (offline) s += '<span class="badge off">离线</span> ';
  else if (alarm) s += '<span class="badge alarm">超限</span> ';
  else s += '<span class="badge on">在线</span> ';
  s += pole.snapshot.mode === 1
    ? '<span class="badge warn">调试</span>'
    : '<span class="badge on">低功耗</span>';
  return s;
}
