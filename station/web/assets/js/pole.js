/* ============================================================
   pole.js - 单杆详情页逻辑
   ------------------------------------------------------------
   信息条 / ECharts 温湿度曲线（24h·7d 切换）/ 节点表 /
   远程模式切换(POST /cmd) / 阈值与档案表单(POST /config)
   快照 15s 轮询；曲线数据按需加载。
   ============================================================ */
"use strict";

var uid = new URLSearchParams(location.search).get("uid") || "";
var chart = null;
var hours = 24;
var curSnap = null;

/* ---------- 信息条 ---------- */
function renderHeader(p) {
  document.getElementById("pName").textContent = p.name;
  document.getElementById("pUid").textContent = "UID " + p.uid;
  document.getElementById("pBadge").innerHTML = statusBadge(p);
  document.getElementById("pLast").textContent = fmtTime(p.last_ts);
  document.getElementById("pPos").textContent =
    "(" + p.x + ", " + p.y + ", " + p.z + ")";
  document.getElementById("pSpacing").textContent = p.spacing_m + " m";

  /* 电量 */
  var bat = document.getElementById("pBat");
  if (p.bat_pct === null || p.bat_pct === undefined) {
    bat.textContent = "-";
  } else {
    var pct = Math.round(p.bat_pct);
    var c = pct <= 20 ? "var(--red)" : (pct <= 50 ? "var(--yellow)" : "var(--green)");
    bat.innerHTML = "<b style='color:" + c + "'>" + pct + "%</b>";
  }

  /* 模式按钮随当前模式变化 */
  var mb = document.getElementById("modeBtn");
  var isDebug = p.snapshot && p.snapshot.mode === 1;
  mb.textContent = isDebug ? "切换为低功耗模式" : "切换为调试模式";

  /* 阈值与档案表单预填 */
  var th = p.threshold || {};
  document.getElementById("thTempHigh").value = th.temp_high;
  document.getElementById("thTempLow").value = th.temp_low;
  document.getElementById("thRhHigh").value = th.rh_high;
  document.getElementById("cName").value = p.name;
  document.getElementById("cX").value = p.x;
  document.getElementById("cY").value = p.y;
  document.getElementById("cZ").value = p.z;
  document.getElementById("cSpacing").value = p.spacing_m;
}

/* ---------- 节点表（记忆状态：曾见过的节点永久显示，失联变灰） ---------- */
function renderNodes(p) {
  var snap = p.snapshot;
  var tb = document.getElementById("nodeTable");
  var states = p.node_states || [];
  if (!states.length) {
    if (!snap || !snap.nodes || !snap.nodes.length) {
      tb.innerHTML = '<tr><td colspan="5" style="color:#7d93b8">暂无节点数据，等待杆子上报…</td></tr>';
      return;
    }
    /* 旧数据回退：按快照渲染 */
    tb.innerHTML = snap.nodes.map(function (n) {
      return "<tr><td>节点 " + n.addr + "</td><td>" +
        (n.addr * (p.spacing_m || 0.5)).toFixed(2) + " m</td><td>" +
        (n.temp === null || n.temp === undefined ? "-" : n.temp.toFixed(2)) +
        "</td><td>" + (n.rh === null || n.rh === undefined ? "-" : n.rh.toFixed(2)) +
        "</td><td><span class='badge on'>正常</span></td></tr>";
    }).join("");
    return;
  }
  /* 按记忆节点列表渲染 */
  var byAddr = {};
  (snap ? snap.nodes : []).forEach(function (n) { byAddr[n.addr] = n; });
  var rows = states.map(function (s) {
    var n = byAddr[s.addr] || {};
    var t = n.temp, h = n.rh;
    var depth = (s.addr * (p.spacing_m || 0.5)).toFixed(2);
    var tCell = t === null || t === undefined ? "-" :
      '<span style="color:' + tempColor(t) + ';font-weight:600">' + t.toFixed(2) + " °C</span>";
    var hCell = h === null || h === undefined ? "-" : h.toFixed(2) + " %";
    var stHtml;
    if (!s.online) stHtml = '<span class="badge off">失联</span>';
    else if (p.snapshot && p.snapshot.alarm === 1) stHtml = '<span class="badge alarm">超限</span>';
    else stHtml = '<span class="badge on">正常</span>';
    var style = s.online ? "" : ' style="opacity:.5"';
    return "<tr" + style + "><td>节点 " + s.addr + "</td><td>" + depth + " m</td>" +
      "<td>" + tCell + "</td><td>" + hCell + "</td><td>" + stHtml + "</td></tr>";
  }).join("");
  tb.innerHTML = rows || '<tr><td colspan="5" style="color:#7d93b8">暂无节点记录</td></tr>';
}

/* ---------- ECharts 曲线 ---------- */
function initChart() {
  chart = echarts.init(document.getElementById("tempChart"));
  chart.setOption({
    backgroundColor: "transparent",
    tooltip: { trigger: "axis" },
    legend: { textStyle: { color: "#8fa3c8" }, top: 0 },
    grid: { left: 60, right: 20, top: 34, bottom: 30 },
    xAxis: {
      type: "time",
      axisLine: { lineStyle: { color: "#23314f" } },
      axisLabel: { color: "#8fa3c8" },
      splitLine: { lineStyle: { color: "#16203a" } },
    },
    yAxis: [
      {
        type: "value", name: "温度 °C",
        axisLabel: { color: "#8fa3c8" },
        splitLine: { lineStyle: { color: "#16203a" } },
      },
      {
        type: "value", name: "湿度 %", max: 100,
        axisLabel: { color: "#8fa3c8" },
        splitLine: { show: false },
      },
    ],
    series: [],
  });
  window.addEventListener("resize", function () { chart && chart.resize(); });
}

function renderChart(data) {
  /* 按节点地址分组为多条曲线 */
  var byNode = {};
  data.forEach(function (s) {
    (s.nodes || []).forEach(function (n) {
      if (n.temp === null || n.temp === undefined) return;
      (byNode[n.addr] = byNode[n.addr] || { t: [], h: [] });
      byNode[n.addr].t.push([s.ts * 1000, n.temp]);
      if (n.rh !== null && n.rh !== undefined) byNode[n.addr].h.push([s.ts * 1000, n.rh]);
    });
  });
  var series = [];
  Object.keys(byNode).sort(function (a, b) { return a - b; }).forEach(function (addr) {
    series.push({
      name: "节点" + addr + " 温度", type: "line", showSymbol: false,
      smooth: true, lineStyle: { width: 1.5 }, yAxisIndex: 0,
      data: byNode[addr].t,
    });
    if (byNode[addr].h.length) {
      series.push({
        name: "节点" + addr + " 湿度", type: "line", showSymbol: false,
        smooth: true, lineStyle: { width: 1.2, type: "dashed" }, yAxisIndex: 1,
        data: byNode[addr].h,
      });
    }
  });
  /* 显式时间轴范围：最近 hours 小时 -> 现在（防止时间戳相同导致轴零宽不绘制）。
     注意：ECharts 5.4.3 对时间轴用 setOption(opt, true) 会崩溃，必须分两次普通合并 */
  var now = Date.now();
  chart.setOption({ series: [] });
  chart.setOption({
    xAxis: { min: now - hours * 3600 * 1000, max: now },
    series: series,
  });
}

/* ---------- 刷新 ---------- */
async function refresh() {
  try {
    var p = await apiGet("/api/v1/poles/" + encodeURIComponent(uid));
    curSnap = p.snapshot;
    renderHeader(p);
    renderNodes(p);
    document.getElementById("refreshTime").textContent = fmtTime(Math.floor(Date.now() / 1000));
  } catch (e) {
    document.getElementById("pName").textContent = "探杆不存在或中心站未启动";
  }
}

async function loadChart() {
  try {
    var data = await apiGet("/api/v1/poles/" + encodeURIComponent(uid) +
      "/data?hours=" + hours);
    if (!chart) initChart();
    renderChart(data);
  } catch (e) { /* 中心站未就绪时静默 */ }
}

/* ---------- 控制 ---------- */
document.getElementById("modeBtn").addEventListener("click", async function () {
  var target = (curSnap && curSnap.mode === 1) ? "mode=lowpower" : "mode=debug";
  try {
    var r = await apiPost("/api/v1/poles/" + encodeURIComponent(uid) + "/cmd",
      { cmd: target });
    document.getElementById("cmdMsg").textContent = r.ok
      ? "指令已缓存，最长 15 分钟内生效" : "下发失败";
  } catch (e) {
    document.getElementById("cmdMsg").textContent = "请求失败";
  }
});

document.getElementById("thSave").addEventListener("click", async function () {
  var body = {
    temp_high: document.getElementById("thTempHigh").value,
    temp_low: document.getElementById("thTempLow").value,
    rh_high: document.getElementById("thRhHigh").value,
  };
  try {
    await apiPost("/api/v1/poles/" + encodeURIComponent(uid) + "/config", body);
    document.getElementById("cmdMsg").textContent = "阈值已保存";
  } catch (e) {
    document.getElementById("cmdMsg").textContent = "保存失败";
  }
});

document.getElementById("cfgSave").addEventListener("click", async function () {
  var body = {
    name: document.getElementById("cName").value,
    x: parseFloat(document.getElementById("cX").value),
    y: parseFloat(document.getElementById("cY").value),
    z: parseFloat(document.getElementById("cZ").value),
    spacing_m: parseFloat(document.getElementById("cSpacing").value),
  };
  try {
    await apiPost("/api/v1/poles/" + encodeURIComponent(uid) + "/config", body);
    document.getElementById("cmdMsg").textContent = "档案已保存";
  } catch (e) {
    document.getElementById("cmdMsg").textContent = "保存失败";
  }
});

/* 曲线时间段切换 */
document.querySelectorAll(".chart-range button").forEach(function (b) {
  b.addEventListener("click", function () {
    document.querySelectorAll(".chart-range button")
      .forEach(function (x) { x.classList.remove("active"); });
    b.classList.add("active");
    hours = parseInt(b.dataset.hours, 10);
    loadChart();
  });
});

/* ---------- 启动 ---------- */
if (!uid) {
  document.getElementById("pName").textContent = "缺少 uid 参数";
} else {
  refresh();
  loadChart();
  setInterval(refresh, 15000);
  setInterval(loadChart, 60000);
}
