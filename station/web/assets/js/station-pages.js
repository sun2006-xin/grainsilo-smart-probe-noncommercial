/* Independent PC Station pages; all values are read from Station APIs. */
(function () {
  "use strict";

  const page = document.body.dataset.stationPage;
  const settingKeys = ["temp_high", "temp_low", "rh_high", "rh_low",
    "normal_interval_sec", "fast_interval_sec"];
  const labels = {
    temp_high: "温度上限", temp_low: "温度下限", rh_high: "相对湿度上限",
    rh_low: "相对湿度下限", normal_interval_sec: "常规采样/上报周期",
    fast_interval_sec: "风险采样/上报周期"
  };
  const units = { temp_high: "°C", temp_low: "°C", rh_high: "%RH",
    rh_low: "%RH", normal_interval_sec: "秒", fast_interval_sec: "秒" };
  let poles = [];
  let piles = [];
  let activeAlertIds = new Set();
  let alertSoundOn = readSoundPreference();
  let audioContext = null;

  async function requestJSON(path, method, payload) {
    const response = await fetch(path, {
      method: method || "GET",
      headers: payload === undefined ? {} : { "Content-Type": "application/json" },
      body: payload === undefined ? undefined : JSON.stringify(payload)
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.err || ("HTTP " + response.status));
    return data;
  }

  function showMessage(id, message, error) {
    const node = document.getElementById(id);
    if (!node) return;
    node.textContent = message || "";
    node.classList.toggle("error", Boolean(error));
  }

  function health() {
    requestJSON("/api/v1/health").then(data => {
      document.querySelectorAll("[data-health]").forEach(node => {
        node.dataset.state = "online";
        node.textContent = "中心站在线 · " + (data.poles?.online || 0) + "/" +
          (data.poles?.total || 0) + " 根探杆在线";
      });
    }).catch(() => document.querySelectorAll("[data-health]").forEach(node => {
      node.dataset.state = "error";
      node.textContent = "中心站暂不可用";
    }));
  }

  function refreshButton(action) {
    document.querySelectorAll("[data-refresh]").forEach(button => {
      button.addEventListener("click", action);
    });
  }

  function loadWarehousePage() {
    const list = document.getElementById("warehouseList");
    const createPanel = document.getElementById("warehouseCreatePanel");
    const createForm = document.getElementById("warehouseCreateForm");
    const editForm = document.getElementById("warehouseEditForm");
    if (!list || !createPanel || !createForm || !editForm) return;
    const selectedId = () => localStorage.getItem("grainsilo.activeWarehouseId") || "1";
    const syncShape = (form, shape) => {
      form.querySelectorAll("[data-warehouse-flat]").forEach(node => {
        node.hidden = shape === "silo";
      });
      form.querySelectorAll("[data-warehouse-silo]").forEach(node => {
        node.hidden = shape !== "silo";
      });
      const height = form.querySelector('input[id$="Height"]');
      if (form === createForm && height && shape === "silo" && height.value === "8") height.value = "18";
      if (form === createForm && height && shape === "flat" && height.value === "18") height.value = "8";
    };
    const payloadFrom = form => {
      const shape = form.querySelector('select[id$="Shape"]').value;
      const body = {
        name: form.querySelector('input[id$="Name"]').value.trim(),
        shape_type: shape,
        height_m: Number(form.querySelector('input[id$="Height"]').value),
        repose_angle_deg: Number(form.querySelector('input[id$="Repose"]').value)
      };
      if (shape === "silo") body.diameter_m = Number(form.querySelector('input[id$="Diameter"]').value);
      else {
        body.length_m = Number(form.querySelector('input[id$="Length"]').value);
        body.width_m = Number(form.querySelector('input[id$="Width"]').value);
      }
      return body;
    };
    const shapeName = shape => shape === "silo" ? "圆筒仓" : "平房仓";
    async function load() {
      try {
        const [warehouses, piles, poles] = await Promise.all([
          requestJSON("/api/v1/warehouses"),
          requestJSON("/api/v1/piles"), requestJSON("/api/v1/poles")
        ]);
        const records = warehouses.data || [];
        const current = records.find(row => String(row.id) === selectedId());
        if (!current) throw new Error("当前仓库档案不存在，请从顶部仓库选择器重新选择");
        document.getElementById("warehouseTotal").textContent = records.length;
        document.getElementById("warehouseShapeSummary").textContent = shapeName(current.shape_type);
        document.getElementById("warehousePileTotal").textContent = piles.length;
        document.getElementById("warehouseProbeTotal").textContent = poles.length;
        document.getElementById("warehouseCurrentLabel").textContent = current.name + " · 档案 #" + current.id;
        document.getElementById("warehouseListMeta").textContent = records.length + " 个活动仓库档案";
        document.getElementById("warehouseName").value = current.name;
        document.getElementById("warehouseShape").value = current.shape_type;
        document.getElementById("warehouseLength").value = current.length_m;
        document.getElementById("warehouseWidth").value = current.width_m;
        document.getElementById("warehouseDiameter").value = current.diameter_m;
        document.getElementById("warehouseHeight").value = current.height_m;
        document.getElementById("warehouseRepose").value = current.repose_angle_deg ?? 30;
        syncShape(editForm, current.shape_type);
        list.innerHTML = records.map(row => `<article class="station-item ${String(row.id) === selectedId() ? "active" : ""}">
          <div class="station-item-head"><div><h3>${esc(row.name)} <span class="station-badge">${shapeName(row.shape_type)}</span></h3>
          <div class="station-item-meta">${row.shape_type === "silo" ? `直径 ${Number(row.diameter_m).toFixed(1)} × 高 ${Number(row.height_m).toFixed(1)} m` : `${Number(row.length_m).toFixed(1)} × ${Number(row.width_m).toFixed(1)} × 高 ${Number(row.height_m).toFixed(1)} m`} · 估算休止角 ${Number(row.repose_angle_deg ?? 30).toFixed(0)}°</div></div>
          ${String(row.id) === selectedId() ? '<span class="station-badge ok">当前仓库</span>' : `<button class="station-button" type="button" data-warehouse-select="${row.id}">切换到此仓</button>`}</div>
        </article>`).join("") || '<div class="station-empty">没有可用仓库档案</div>';
        list.querySelectorAll("[data-warehouse-select]").forEach(button => button.addEventListener("click", () => {
          localStorage.setItem("grainsilo.activeWarehouseId", button.dataset.warehouseSelect);
          window.location.reload();
        }));
      } catch (error) {
        list.innerHTML = `<div class="station-empty">读取失败：${esc(error.message)}</div>`;
      }
    }
    document.getElementById("warehouseCreateToggle").addEventListener("click", () => {
      createPanel.hidden = !createPanel.hidden;
      if (!createPanel.hidden) createPanel.scrollIntoView({ behavior: "smooth", block: "start" });
    });
    document.getElementById("warehouseCreateClose").addEventListener("click", () => { createPanel.hidden = true; });
    document.getElementById("newWarehouseShape").addEventListener("change", event => syncShape(createForm, event.target.value));
    document.getElementById("warehouseShape").addEventListener("change", event => syncShape(editForm, event.target.value));
    createForm.addEventListener("submit", async event => {
      event.preventDefault();
      try {
        const result = await requestJSON("/api/v1/warehouses", "POST", payloadFrom(createForm));
        localStorage.setItem("grainsilo.activeWarehouseId", String(result.id));
        window.location.reload();
      } catch (error) { showMessage("warehouseCreateMessage", error.message, true); }
    });
    editForm.addEventListener("submit", async event => {
      event.preventDefault();
      try {
        await requestJSON(`/api/v1/warehouses/${encodeURIComponent(selectedId())}`, "POST", payloadFrom(editForm));
        showMessage("warehouseEditMessage", "档案已保存；尺寸和仓型均以仓库档案为准。", false);
        await load();
      } catch (error) { showMessage("warehouseEditMessage", error.message, true); }
    });
    refreshButton(load);
    syncShape(createForm, document.getElementById("newWarehouseShape").value);
    load();
  }

  function formatReading(value, unit, digits) {
    if (value === null || value === undefined || !Number.isFinite(Number(value))) return "—";
    return Number(value).toFixed(digits === undefined ? 1 : digits) + unit;
  }

  function loadPilePage() {
    const list = document.getElementById("pilesList");
    const form = document.getElementById("pileForm");
    if (!list || !form) return;
    let warehouseConfig = null;
    const reset = () => {
      form.reset();
      document.getElementById("pileId").value = "";
      document.getElementById("pileFormTitle").textContent = "新增粮堆";
      document.getElementById("pileFormReset").textContent = "清空";
      showMessage("pileFormMessage", "");
    };
    async function load() {
      try {
        [piles, poles, warehouseConfig] = await Promise.all([
          requestJSON("/api/v1/piles"), requestJSON("/api/v1/poles"),
          requestJSON("/api/v1/config")
        ]);
        const silo = warehouseConfig.warehouse?.shape_type === "silo";
        form.querySelectorAll("[data-pile-flat]").forEach(node => { node.hidden = silo; });
        document.getElementById("pileGeometryNote").textContent = silo
          ? `当前为圆筒仓：粮堆按仓房直径 ${Number(warehouseConfig.warehouse.diameter_m).toFixed(1)} m 建模，一个圆筒仓档案对应一个粮堆。体积按登记粮重 ÷ 登记容重估算；若估算形状超出休止角/仓高假设，会显示警告。`
          : "系统按“登记粮重 ÷ 登记容重”估算体积；平房仓使用登记平面尺寸。几何为参数化估算，不是现场测绘或 CFD；超出当前仓房参数时会明确提示。";
        const poleSelect = document.getElementById("pilePoles");
        poleSelect.innerHTML = poles.map(p => `<option value="${esc(p.uid)}">${esc(p.name)} · ${esc(p.uid)}</option>`).join("");
        const totalVolume = piles.reduce((sum, pile) => sum + Number(pile.volume || 0), 0);
        const totalWeight = piles.reduce((sum, pile) => sum + Number(pile.weight_kg || 0), 0);
        document.getElementById("pileCount").textContent = piles.length;
        document.getElementById("pileVolume").textContent = totalVolume.toFixed(1);
        document.getElementById("pileWeight").textContent = (totalWeight / 1000).toFixed(1);
        document.getElementById("pilePoleCount").textContent = piles.reduce((sum, pile) => sum + pile.pole_count, 0);
        document.getElementById("pileListMeta").textContent = piles.length + " 个粮堆档案";
        list.innerHTML = piles.length ? piles.map(pile => `<article class="station-item">
          <div class="station-item-head"><div><h3>${esc(pile.name)} <span class="station-badge">粮堆 ${esc(pile.id)}</span></h3>
          <div class="station-item-meta">粮重 ${(Number(pile.weight_kg) / 1000).toFixed(1)} 吨 · 估算体积 ${Number(pile.volume).toFixed(1)} m³ · ${silo ? `圆形底面直径 ${Number(pile.l).toFixed(1)} m` : `登记平面 ${Number(pile.l).toFixed(1)} × ${Number(pile.w).toFixed(1)} m`}</div></div>
          <span class="station-badge">${pile.pole_count} 根探杆</span></div>
          <div class="station-item-meta">估算高度 ${Number(pile.height).toFixed(2)} m · ${pile.geometry_status === "over_capacity" ? '<span class="station-badge warn">超出仓高假设</span>' : pile.geometry_status === "slope_exceeds_assumed_repose" ? '<span class="station-badge warn">休止角假设不匹配</span>' : "参数化估算几何"} · 阈值/周期来源：${scopeSummary(pile.threshold_sources, pile.interval_sources)}</div>
          <div class="station-actions"><button class="station-button" data-pile-edit="${pile.id}">编辑粮堆</button><button class="station-button danger" data-pile-delete="${pile.id}">删除</button></div>
        </article>`).join("") : '<div class="station-empty">尚未登记粮堆。可以先录入吨数、容重与平面估算尺寸。</div>';
        list.querySelectorAll("[data-pile-edit]").forEach(button => button.addEventListener("click", () => {
          const pile = piles.find(row => row.id === Number(button.dataset.pileEdit));
          if (!pile) return;
          document.getElementById("pileId").value = pile.id;
          document.getElementById("pileName").value = pile.name;
          document.getElementById("pileWeightInput").value = (pile.weight_kg / 1000).toFixed(1);
          document.getElementById("pileDensity").value = pile.density;
          document.getElementById("pileLength").value = pile.l;
          document.getElementById("pileWidth").value = pile.w;
          Array.from(poleSelect.options).forEach(option => {
            option.selected = pile.poles.some(p => p.uid === option.value);
          });
          document.getElementById("pileFormTitle").textContent = "编辑粮堆 · " + pile.name;
          document.getElementById("pileFormTitle").scrollIntoView({ behavior: "smooth", block: "center" });
        }));
        list.querySelectorAll("[data-pile-delete]").forEach(button => button.addEventListener("click", async () => {
          const pile = piles.find(row => row.id === Number(button.dataset.pileDelete));
          if (!pile || !confirm(`删除“${pile.name}”？探杆会解绑，历史数据保留。`)) return;
          try { await requestJSON(`/api/v1/piles/${pile.id}`, "DELETE"); await load(); }
          catch (error) { alert("删除失败：" + error.message); }
        }));
      } catch (error) {
        list.innerHTML = `<div class="station-empty">读取失败：${esc(error.message)}</div>`;
      }
    }
    form.addEventListener("submit", async event => {
      event.preventDefault();
      const id = document.getElementById("pileId").value;
      const body = {
        name: document.getElementById("pileName").value.trim(),
        weight_kg: Number(document.getElementById("pileWeightInput").value || 0) * 1000,
        density: Number(document.getElementById("pileDensity").value || 750),
        l: warehouseConfig?.warehouse?.shape_type === "silo" ? Number(warehouseConfig.warehouse.diameter_m) : Number(document.getElementById("pileLength").value),
        w: warehouseConfig?.warehouse?.shape_type === "silo" ? Number(warehouseConfig.warehouse.diameter_m) : Number(document.getElementById("pileWidth").value),
        pole_uids: Array.from(document.getElementById("pilePoles").selectedOptions).map(option => option.value)
      };
      try {
        await requestJSON(id ? `/api/v1/piles/${id}` : "/api/v1/piles", "POST", body);
        showMessage("pileFormMessage", "粮堆档案已保存。设备配置如有变化会等待下次 S3 上报。");
        reset(); await load();
      } catch (error) { showMessage("pileFormMessage", error.message, true); }
    });
    document.getElementById("pileFormReset").addEventListener("click", reset);
    document.getElementById("pileFormReset").addEventListener("click", load);
    refreshButton(load);
    load();
  }

  function loadProbePage() {
    const list = document.getElementById("probesList");
    if (!list) return;
    const panel = document.getElementById("probeCreatePanel");
    const editPanel = document.getElementById("probeEditPanel");
    const fillPileSelect = (select, selected) => {
      select.innerHTML = '<option value="">暂不绑定</option>' + piles.map(p =>
        `<option value="${p.id}">${esc(p.name)} · 粮堆 ${p.id}</option>`).join("");
      select.value = selected || "";
    };
    async function load() {
      try {
        [poles, piles] = await Promise.all([requestJSON("/api/v1/poles"), requestJSON("/api/v1/piles")]);
        document.getElementById("probeTotal").textContent = poles.length;
        document.getElementById("probeOnline").textContent = poles.filter(p => p.status === 1).length;
        document.getElementById("probeOffline").textContent = poles.filter(p => p.status !== 1).length;
        document.getElementById("probeNodeTotal").textContent = poles.reduce((n, p) => n + (p.snapshot?.nodes?.length || 0), 0);
        document.getElementById("probeListMeta").textContent = poles.length + " 根探杆";
        fillPileSelect(document.getElementById("newPolePile"));
        list.innerHTML = poles.length ? poles.map(p => {
          const remote = p.remote_settings;
          const remoteBadge = remote?.status === "applied" ? '<span class="station-badge ok">设备已确认配置</span>' : '<span class="station-badge warn">配置待设备确认</span>';
          const nodes = p.snapshot?.nodes || [];
          const nodePositions = p.node_positions || {};
          const positionCards = nodes.length ? nodes.map(n => {
            const position = nodePositions[String(n.addr)];
            const val = axis => position && Number.isFinite(Number(position[axis])) ? Number(position[axis]).toFixed(2) : "";
            return `<div class="station-node-calibration" data-uid="${esc(p.uid)}" data-addr="${esc(n.addr)}">
              <div class="station-node-calibration-head"><strong>通信节点 ${esc(n.addr)}</strong><span>${formatReading(n.temp, "°C")} · ${formatReading(n.rh, "%RH")}</span><span class="station-badge ${position ? "ok" : "warn"}">${position ? "位置已校准" : "位置未校准"}</span></div>
              <div class="station-node-coordinate-grid">${["x", "y", "z"].map(axis => `<label>${axis.toUpperCase()}（m）<input type="number" step="0.01" data-node-axis="${axis}" value="${val(axis)}" placeholder="现场测量值"></label>`).join("")}
                <button class="station-button primary" type="button" data-node-save>保存节点位置</button>${position ? '<button class="station-button" type="button" data-node-clear>清除校准</button>' : ""}</div>
              <span class="station-card-message" data-node-message role="status">${position ? `坐标 X ${val("x")} · Y ${val("y")} · Z ${val("z")} m；${fmtTime(position.calibrated_ts)}` : "未校准：不会按通信地址或节点间距推断高度，也不会进入温度场插值。"}</span>
            </div>`;
          }).join("") : '<span class="station-badge warn">暂无实测节点数据</span>';
          return `<article class="station-item"><div class="station-item-head"><div><h3>${esc(p.name)}</h3><div class="station-item-meta">UID ${esc(p.uid)} · ${p.pile_name ? esc(p.pile_name) : "未绑定粮堆"} · ${p.status === 1 ? "在线" : "离线/无近期数据"}</div></div>${statusBadge(p)}</div>
            <div class="station-item-meta">坐标 ${Number(p.x).toFixed(2)}, ${Number(p.y).toFixed(2)}, ${Number(p.z).toFixed(2)} m · 节点地址仅为通信身份，不推断物理高度</div>
            <div class="station-node-calibration-list">${positionCards}</div>
            <div class="station-remote-state ${remote?.status === "applied" ? "applied" : "pending"}">${remoteBadge} · 版本 ${remote?.revision ?? "—"} / 已确认 ${remote?.applied_revision ?? "—"} · 上次数据 ${fmtTime(p.last_ts)}</div>
            <div class="station-actions"><a class="station-button" href="/pole.html?uid=${encodeURIComponent(p.uid)}">节点与历史详情</a><button class="station-button" data-probe-edit="${esc(p.uid)}">编辑档案</button></div></article>`;
        }).join("") : '<div class="station-empty">尚无登记探杆。设备第一次上报后会自动建档，也可以按真实 UID 手动登记。</div>';
        list.querySelectorAll("[data-probe-edit]").forEach(button => button.addEventListener("click", () => {
          const p = poles.find(row => row.uid === button.dataset.probeEdit);
          if (!p) return;
          document.getElementById("editPoleUid").value = p.uid;
          document.getElementById("editPoleName").value = p.name || "";
          fillPileSelect(document.getElementById("editPolePile"), p.pile_id);
          ["X", "Y", "Z"].forEach(axis => { document.getElementById("editPole" + axis).value = p[axis.toLowerCase()] ?? 0; });
          document.getElementById("editPoleSpacing").value = p.spacing_m || 0.5;
          editPanel.hidden = false;
          editPanel.scrollIntoView({ behavior: "smooth", block: "start" });
        }));
        list.querySelectorAll("[data-node-save]").forEach(button => button.addEventListener("click", async () => {
          const card = button.closest("[data-node-calibration]") || button.closest(".station-node-calibration");
          const uid = card.dataset.uid, addr = card.dataset.addr;
          const coords = Object.fromEntries(["x", "y", "z"].map(axis => [axis, card.querySelector(`[data-node-axis="${axis}"]`).value.trim()]));
          const message = card.querySelector("[data-node-message]");
          if (Object.values(coords).some(value => value === "" || !Number.isFinite(Number(value)))) { message.textContent = "请填写现场测得的 X、Y、Z 坐标。"; message.classList.add("error"); return; }
          try { await requestJSON(`/api/v1/poles/${encodeURIComponent(uid)}/nodes/${encodeURIComponent(addr)}/position`, "POST", Object.fromEntries(Object.entries(coords).map(([key, value]) => [key, Number(value)]))); await load(); }
          catch (error) { message.textContent = "校准未保存：" + error.message; message.classList.add("error"); }
        }));
        list.querySelectorAll("[data-node-clear]").forEach(button => button.addEventListener("click", async () => {
          const card = button.closest(".station-node-calibration"), message = card.querySelector("[data-node-message]");
          try { await requestJSON(`/api/v1/poles/${encodeURIComponent(card.dataset.uid)}/nodes/${encodeURIComponent(card.dataset.addr)}/position`, "DELETE"); await load(); }
          catch (error) { message.textContent = "清除校准失败：" + error.message; message.classList.add("error"); }
        }));
      } catch (error) { list.innerHTML = `<div class="station-empty">读取失败：${esc(error.message)}</div>`; }
    }
    document.getElementById("probeCreateToggle").addEventListener("click", () => { panel.hidden = !panel.hidden; });
    document.getElementById("probeCreateClose").addEventListener("click", () => { panel.hidden = true; });
    document.getElementById("probeEditClose").addEventListener("click", () => { editPanel.hidden = true; });
    document.getElementById("probeCreateForm").addEventListener("submit", async event => {
      event.preventDefault();
      const body = { uid: document.getElementById("newPoleUid").value.trim(), name: document.getElementById("newPoleName").value.trim(), pile_id: Number(document.getElementById("newPolePile").value) || null, spacing_m: Number(document.getElementById("newPoleSpacing").value), x: Number(document.getElementById("newPoleX").value), y: Number(document.getElementById("newPoleY").value), z: Number(document.getElementById("newPoleZ").value) };
      try { await requestJSON("/api/v1/poles", "POST", body); showMessage("probeCreateMessage", "探杆档案已登记"); event.target.reset(); panel.hidden = true; await load(); }
      catch (error) { showMessage("probeCreateMessage", error.message, true); }
    });
    document.getElementById("probeEditForm").addEventListener("submit", async event => {
      event.preventDefault();
      const uid = document.getElementById("editPoleUid").value;
      const body = { name: document.getElementById("editPoleName").value.trim(), pile_id: Number(document.getElementById("editPolePile").value) || null, x: Number(document.getElementById("editPoleX").value), y: Number(document.getElementById("editPoleY").value), z: Number(document.getElementById("editPoleZ").value), spacing_m: Number(document.getElementById("editPoleSpacing").value) };
      try { await requestJSON(`/api/v1/poles/${encodeURIComponent(uid)}/config`, "POST", body); showMessage("probeEditMessage", "档案已保存"); await load(); }
      catch (error) { showMessage("probeEditMessage", error.message, true); }
    });
    refreshButton(load); load();
  }

  async function loadWeather() {
    const location = await requestJSON("/api/v1/weather/location");
    const latInput = document.getElementById("weatherLat");
    const lonInput = document.getElementById("weatherLon");
    if (latInput && document.activeElement !== latInput) latInput.value = location.lat ?? "";
    if (lonInput && document.activeElement !== lonInput) lonInput.value = location.lon ?? "";
    const [data, history] = await Promise.all([
      requestJSON("/api/v1/forecast?hours=24"),
      requestJSON("/api/v1/weather/history?hours=720")
    ]);
    const weather = data.weather || {};
    const status = document.getElementById("weatherStatus");
    if (!status) return;
    if (weather.status === "ready") {
      status.className = "station-badge ok";
      status.textContent = "天气模型已更新";
      const current = weather.current;
      document.getElementById("weatherCurrent").innerHTML = current ? `<div class="station-weather-current-grid">
        <div><span>模型当前气温 · ${fmtTime(current.ts)}</span><b>${formatReading(current.temperature_c, "°C")}</b></div>
        <div><span>模型当前相对湿度</span><b>${formatReading(current.rh_pct, "%RH", 0)}</b></div></div>` : '<div class="station-empty">天气模型未返回当前值</div>';
      const rows = weather.series || [];
      document.getElementById("weatherRows").innerHTML = rows.length ? rows.map(row => `<tr><td>${fmtShort(row.ts)}</td><td>${formatReading(row.temperature_c, "°C")}</td><td>${formatReading(row.rh_pct, "%RH", 0)}</td></tr>`).join("") : '<tr><td colspan="3" class="station-empty">没有可显示的天气点</td></tr>';
      const daily = weather.daily_series || [];
      const weatherLabel = code => ({0:"晴",1:"大致晴",2:"多云",3:"阴",45:"雾",48:"雾凇",51:"毛毛雨",53:"毛毛雨",55:"毛毛雨",61:"小雨",63:"中雨",65:"大雨",71:"小雪",73:"中雪",75:"大雪",80:"阵雨",81:"阵雨",82:"强阵雨",95:"雷雨",96:"雷雨",99:"雷雨"})[Number(code)] || "天气变化";
      document.getElementById("weatherDailyRows").innerHTML = daily.length ? daily.map(day => `<article class="station-weather-day"><span>${esc(day.date || "—")}</span><b>${weatherLabel(day.weather_code)}</b><strong>${formatReading(day.temperature_min_c, "°C")} <i>—</i> ${formatReading(day.temperature_max_c, "°C")}</strong><small>降水概率 ${formatReading(day.precipitation_probability_max_pct, "%", 0)} · 降水量 ${formatReading(day.precipitation_sum_mm, " mm")}</small></article>`).join("") : '<div class="station-empty">天气服务没有返回逐日数据</div>';
      document.getElementById("weatherMeta").textContent = `坐标 ${weather.latitude}, ${weather.longitude} · 时区 ${weather.timezone || "按地点"} · 来源 ${esc(weather.source)}。天气快照在电脑站运行期间按小时留档，仅为模型数据，不是现场气象站实测。`;
    } else {
      status.className = "station-badge warn";
      status.textContent = weather.status === "not_configured" ? "未配置位置" : "天气暂不可用";
      document.getElementById("weatherCurrent").innerHTML = `<div class="station-empty">${weather.status === "not_configured" ? "请填写所在地纬度与经度。" : `天气读取失败：${esc(weather.error || "网络不可用")}`}</div>`;
      document.getElementById("weatherRows").innerHTML = '<tr><td colspan="3" class="station-empty">暂无天气预报</td></tr>';
      document.getElementById("weatherDailyRows").innerHTML = '<div class="station-empty">配置所选仓库的天气坐标后显示 7 天逐日预报。</div>';
    }
    const observations = history.observations || [];
    document.getElementById("weatherHistoryCount").textContent = `${observations.length} 条`;
    document.getElementById("weatherHistoryRows").innerHTML = observations.length ? observations.slice(-720).map(row =>
      `<tr><td>${fmtShort(row.ts)}</td><td>${formatReading(row.temperature_c, "°C")}</td><td>${formatReading(row.rh_pct, "%RH", 0)}</td><td>${formatReading(row.dew_point_c, "°C")}</td><td>${esc(row.source || "历史天气模型")}</td></tr>`
    ).join("") : '<tr><td colspan="5" class="station-empty">尚无当前仓库坐标对应的历史天气；选择日期并点击“回填所选日期”。</td></tr>';
  }

  function durationText(seconds) {
    const value = Math.max(0, Number(seconds) || 0);
    const hours = Math.floor(value / 3600);
    const minutes = Math.floor((value % 3600) / 60);
    return hours ? `${hours}小时${minutes}分` : `${minutes}分`;
  }

  function renderProbePrediction(result) {
    const host = document.getElementById("probeForecast");
    const station = result.station_prediction || {};
    const s3 = result.s3_prediction || {};
    const weather = result.weather_prediction || {};
    const byHour = new Map((station.forecast || []).map(point => [Number(point.hour), point]));
    const s3ByHour = new Map((s3.points || []).map(point => [Number(point.hour), point]));
    const weatherByHour = new Map((weather.forecast || []).map(point => [Number(point.hour), point]));
    const coverage = new Map((station.horizon_coverage || []).map(item => [Number(item.hour), item]));
    const weatherReady = weather.status === "ready";
    const pairCount = Number(weather.pair_count || 0);
    const weatherState = weatherReady ? "已通过本地留出验证" : ({
      warming_up: `历史配对不足（温度/水汽各需 72 个逐小时转移；当前 ${pairCount}）`,
      not_validated: "当前历史未通过留出验证，暂不显示天气辅助预测数值",
      not_configured: "尚未配置当前仓库天气位置",
      unavailable: "天气预报暂不可用",
      stale: "探杆最新读数已过期"
    }[weather.status] || "天气辅助模型尚未就绪");
    const explained = [1, 3, 6].map(hour => {
      const a = byHour.get(hour), b = s3ByHour.get(hour), c = weatherByHour.get(hour);
      const diag = coverage.get(hour);
      const stationValues = a ? `${formatReading(a.temperature_c, "°C")} · ${formatReading(a.rh_pct, "%RH", 0)}` : "尚未达到该时段门槛";
      const stationCoverage = diag ? `${diag.sample_count} 个半小时点（至少 6 个） · ${durationText(diag.history_span_seconds)} / ${durationText(diag.required_span_seconds)}` : `${station.sample_count || 0} 个点 · 总覆盖 ${durationText(station.span_seconds)}`;
      const s3Values = b ? `${formatReading(b.temp, "°C")} · ${formatReading(b.rh, "%RH", 0)}` : (s3.status === "stale" ? "设备端样本已过期" : "设备端历史预热中");
      const s3Coverage = `${s3.sample_count || 0} 个 RAM 样本 · 覆盖 ${durationText(s3.span_s)}`;
      const weatherValues = c ? `${formatReading(c.temperature_c, "°C")} · ${formatReading(c.rh_pct, "%RH", 0)}` : weatherState;
      return `<article class="station-forecast-card"><span>未来 +${hour} 小时</span><b>${stationValues}</b><small>电脑端探杆历史趋势 · ${stationCoverage}</small><small>${s3Values} · S3 设备端预测 · ${s3Coverage}</small><small class="station-weather-prediction-value">天气辅助模型：${weatherValues}</small></article>`;
    }).join("");
    const tempDiag = weather.temperature_calibration || {};
    const rhDiag = weather.vapor_pressure_calibration || {};
    const skill = [tempDiag.skill_score, rhDiag.skill_score].filter(value => Number.isFinite(Number(value)));
    const modelMeta = weatherReady && skill.length ? ` 温度/水汽留出技巧分数：${skill.map(value => `${(Number(value) * 100).toFixed(1)}%`).join(" / ")}。` : "";
    host.innerHTML = `<div class="station-forecast-explainer"><b>三路预测分别显示 · 天气辅助状态：${esc(weatherState)}</b><span>电脑端探杆趋势只用节点历史；S3 预测来自设备；天气辅助模型将探杆空气温湿度、历史逐小时天气与未来逐小时天气作为拟合输入。温度和水汽各需至少 72 个有效逐小时配对转移，并通过按时间留出的持续性基线验证后才显示。验证达标且预测越限时，会形成独立天气辅助预警，并可影响 S3 采样/上报周期目标；设备端收到并确认后才视为应用。${esc(weather.note || "天气数据是网格模型，不是现场气象站；目标是探杆周围空气，不是整堆粮温或粮食含水率。")}${esc(modelMeta)}</span></div>${explained}`;
  }

  function loadEnvironmentPage() {
    const poleSelect = document.getElementById("environmentProbe");
    if (!poleSelect) return;
    let currentUid = "";
    let currentAddr = "";
    async function load() {
      try {
        const [healthData, envResult, registryResult, poleRows] = await Promise.all([
          requestJSON("/api/v1/health"), requestJSON("/api/v1/env/latest"),
          requestJSON("/api/v1/env/registry"), requestJSON("/api/v1/poles")
        ]);
        poles = poleRows;
        const previousUid = currentUid || poleSelect.value;
        poleSelect.innerHTML = poles.map(p => `<option value="${esc(p.uid)}">${esc(p.name)} · ${esc(p.uid)}${p.status === 1 ? " · 在线" : " · 离线"}</option>`).join("") || '<option value="">无已登记探杆</option>';
        currentUid = poles.some(p => p.uid === previousUid) ? previousUid : (poles[0]?.uid || "");
        poleSelect.value = currentUid;
        document.getElementById("onlineProbeCount").textContent = `${healthData.poles.online}/${healthData.poles.total}`;
        const snap = envResult.data;
        const registry = registryResult.data || [];
        const temp = snap?.temp;
        const rh = snap?.rh;
        document.getElementById("roomTemp").textContent = formatReading(temp, "°C");
        document.getElementById("roomRh").textContent = formatReading(rh, "%RH", 0);
        document.getElementById("roomSensorMeta").textContent = snap ? "仓内环境上报 " + fmtTime(envResult.ts) : "等待独立环境传感器上报";
        document.getElementById("environmentUpdated").textContent = "更新 " + fmtTime(healthData.ts);
        if (snap) {
          const unitsByKey = Object.fromEntries(registry.map(row => [row.key, row.unit || ""]));
          const enabled = registry.filter(row => row.enabled && Object.prototype.hasOwnProperty.call(snap, row.key));
          document.getElementById("warehouseEnvironment").innerHTML = enabled.length ? enabled.map(row => `<div class="station-reading"><span>${esc(row.label)}</span><b>${esc(snap[row.key])}${esc(unitsByKey[row.key])}</b><small>仓内环境传感器上报 · ${esc(row.source)}</small></div>`).join("") : '<div class="station-empty">没有可显示的仓内环境传感器数据；探杆读数仍单独展示。</div>';
        } else {
          document.getElementById("warehouseEnvironment").innerHTML = '<div class="station-empty">暂无独立仓内环境传感器数据。</div>';
        }
        const selected = poles.find(p => p.uid === currentUid);
        const nodes = selected?.snapshot?.nodes || [];
        document.getElementById("measuredNodeCount").textContent = nodes.length;
        const nodeSelect = document.getElementById("environmentNode");
        nodeSelect.innerHTML = nodes.map(node => `<option value="${esc(node.addr)}">通信地址 ${esc(node.addr)}</option>`).join("") || '<option value="">无节点数据</option>';
        currentAddr = nodes.some(n => String(n.addr) === String(currentAddr)) ? String(currentAddr) : (nodes[0] ? String(nodes[0].addr) : "");
        nodeSelect.value = currentAddr;
        const nd = nodes.find(n => String(n.addr) === String(currentAddr));
        document.getElementById("probeSampleMeta").textContent = "最近采样：" + fmtTime(selected?.last_ts);
        document.getElementById("probeReadings").innerHTML = nd ? `<div class="station-reading"><span>探杆实测温度 · 通信地址 ${esc(nd.addr)}</span><b>${formatReading(nd.temp, "°C")}</b><small>${fmtTime(selected.last_ts)} · ${nd.temp == null ? "无有效读数" : "实测"}</small></div><div class="station-reading"><span>探杆实测相对湿度</span><b>${formatReading(nd.rh, "%RH", 0)}</b><small>不是粮食含水率</small></div>` : '<div class="station-empty">此探杆没有有效节点快照</div>';
        await loadWeather();
        if (selected && nd) {
          const prediction = await requestJSON(`/api/v1/probe-forecast?uid=${encodeURIComponent(selected.uid)}&addr=${encodeURIComponent(nd.addr)}`);
          renderProbePrediction(prediction);
        } else {
          document.getElementById("probeForecast").innerHTML = '<div class="station-empty">选择有历史数据的节点后查看预测；数据不足会明确提示。</div>';
        }
        const reporting = selected?.snapshot?.reporting || {};
        const remote = selected?.remote_settings;
        const cadence = remote?.weather_cadence || {};
        const cadenceMode = { normal: "常规", watch: "天气观察", risk: "天气风险" }[cadence.mode] || "未改变周期";
        const cadenceStatus = {
          waiting_for_validated_forecast: "等待天气辅助模型通过验证",
          waiting_for_all_probe_forecasts: "部分节点预测未验证，不据此恢复周期",
          validated_limit_crossing: "预测越限，已请求加快采样/上报",
          validated_safe_forecast: "有效预测暂未越限",
          awaiting_safe_forecasts_to_recover: `等待连续安全预测 ${cadence.safe_reports || 0}/3`,
          recovered_after_validated_safe_forecasts: "连续安全预测后恢复配置周期",
        }[cadence.status] || cadence.status || "等待有效预测";
        const cadenceReason = {
          temperature_high: "未来温度预计超过上限",
          temperature_low: "未来温度预计低于下限",
          humidity_high: "未来相对湿度预计超过上限",
          humidity_low: "未来相对湿度预计低于下限",
        }[cadence.reason] || "";
        const readyNodes = Number(cadence.ready_nodes) || 0;
        const expectedNodes = Number(cadence.expected_nodes) || 0;
        const cadenceAck = remote?.status === "applied" ? `S3 已确认 v${remote.revision}` : `等待 S3 确认 v${remote?.revision ?? "—"}`;
        document.getElementById("remoteConfigBadge").className = "station-badge " + (remote?.status === "applied" ? "ok" : "warn");
        document.getElementById("remoteConfigBadge").textContent = remote?.status === "applied" ? `S3 已确认 v${remote.revision}` : `等待 S3 确认 v${remote?.revision ?? "—"}`;
        document.getElementById("cadenceReadings").innerHTML = selected ? `<div class="station-reading"><span>S3 实际采样周期</span><b>${reporting.sample_interval_ms ? (reporting.sample_interval_ms / 1000).toFixed(1) + " 秒" : "—"}</b><small>设备最近一次上报</small></div><div class="station-reading"><span>S3 实际上报周期</span><b>${reporting.report_interval_ms ? (reporting.report_interval_ms / 1000).toFixed(1) + " 秒" : "—"}</b><small>${reporting.adaptive_fast ? "设备本地风险快档" : "设备本地常规档"}</small></div><div class="station-reading"><span>常规档目标周期</span><b>${remote?.normal_interval_sec ?? selected.intervals?.normal_interval_sec ?? "—"} 秒</b><small>${esc(remote?.sources?.normal_interval_sec || "继承配置")} · 配置 v${remote?.revision ?? "—"}</small></div><div class="station-reading"><span>风险档目标周期</span><b>${remote?.fast_interval_sec ?? selected.intervals?.fast_interval_sec ?? "—"} 秒</b><small>采样与上报联动</small></div><div class="station-reading"><span>天气预测周期策略 · ${esc(cadenceMode)}</span><b>${esc(cadenceStatus)}</b><small>${expectedNodes ? `有效预测节点 ${readyNodes}/${expectedNodes} · ` : ""}${esc(cadenceAck)}${cadenceReason ? ` · ${cadenceReason}` : ""}</small></div>` : '<div class="station-empty">暂无探杆档案</div>';
      } catch (error) {
        document.getElementById("probeForecast").innerHTML = `<div class="station-empty">环境数据加载失败：${esc(error.message)}</div>`;
      }
    }
    poleSelect.addEventListener("change", () => { currentUid = poleSelect.value; currentAddr = ""; load(); });
    document.getElementById("environmentNode").addEventListener("change", event => { currentAddr = event.target.value; load(); });
    const utcDate = date => date.toISOString().slice(0, 10);
    const backfillStart = document.getElementById("weatherBackfillStart");
    const backfillEnd = document.getElementById("weatherBackfillEnd");
    const today = new Date();
    const utcToday = new Date(Date.UTC(today.getUTCFullYear(), today.getUTCMonth(), today.getUTCDate()));
    const startDay = new Date(utcToday.getTime() - 29 * 86400000);
    if (backfillStart && !backfillStart.value) backfillStart.value = utcDate(startDay);
    if (backfillEnd && !backfillEnd.value) backfillEnd.value = utcDate(utcToday);
    if (backfillStart && backfillEnd) {
      backfillStart.max = utcDate(utcToday);
      backfillEnd.max = utcDate(utcToday);
    }
    const locateButton = document.getElementById("weatherLocateButton");
    locateButton.addEventListener("click", () => {
      const geolocation = navigator.geolocation;
      if (!geolocation) {
        showMessage("weatherMessage", "当前浏览器不支持定位；请搜索城市或手动填坐标。", true);
        return;
      }
      locateButton.disabled = true;
      showMessage("weatherMessage", "等待浏览器定位授权…");
      geolocation.getCurrentPosition(position => {
        document.getElementById("weatherLat").value = position.coords.latitude.toFixed(6);
        document.getElementById("weatherLon").value = position.coords.longitude.toFixed(6);
        locateButton.disabled = false;
        showMessage("weatherMessage", "已填入本机定位候选；请核对并点击“保存位置并更新天气”。");
      }, error => {
        locateButton.disabled = false;
        const message = error.code === 1 ? "定位权限被拒绝；可搜索城市或手动填坐标。" : "浏览器定位不可用；可搜索城市或手动填坐标。";
        showMessage("weatherMessage", message, true);
      }, { enableHighAccuracy: false, timeout: 10000, maximumAge: 300000 });
    });
    const candidateHost = document.getElementById("weatherSearchResults");
    document.getElementById("weatherSearchButton").addEventListener("click", async () => {
      const name = document.getElementById("weatherSearchName").value.trim();
      if (name.length < 2) {
        showMessage("weatherMessage", "请输入至少 2 个字符的城市或地区名称。", true);
        return;
      }
      try {
        const result = await requestJSON(`/api/v1/weather/geocode?q=${encodeURIComponent(name)}`);
        const places = result.results || [];
        candidateHost.innerHTML = places.length ? places.map(place => `<button class="station-weather-candidate" type="button" data-lat="${Number(place.latitude)}" data-lon="${Number(place.longitude)}"><b>${esc(place.name)}</b><span>${esc([place.admin1, place.country].filter(Boolean).join(" · "))}</span><small>${Number(place.latitude).toFixed(4)}, ${Number(place.longitude).toFixed(4)} · ${esc(place.timezone || "")}</small></button>`).join("") : '<div class="station-empty">没有找到匹配地点，请换成城市或区县名称。</div>';
        showMessage("weatherMessage", "请选择地点候选；选择只填入坐标，不会自动保存。");
      } catch (error) { showMessage("weatherMessage", error.message, true); }
    });
    candidateHost.addEventListener("click", event => {
      const button = event.target.closest("[data-lat][data-lon]");
      if (!button) return;
      document.getElementById("weatherLat").value = Number(button.dataset.lat).toFixed(6);
      document.getElementById("weatherLon").value = Number(button.dataset.lon).toFixed(6);
      showMessage("weatherMessage", "候选坐标已填入；点击“保存位置并更新天气”应用到当前仓库。");
    });
    document.getElementById("weatherLocationForm").addEventListener("submit", async event => {
      event.preventDefault();
      try {
        const lat = Number(document.getElementById("weatherLat").value);
        const lon = Number(document.getElementById("weatherLon").value);
        await requestJSON("/api/v1/weather/location", "POST", { lat, lon });
        showMessage("weatherMessage", "位置已保存，正在更新天气模型");
        await loadWeather();
      } catch (error) { showMessage("weatherMessage", error.message, true); }
    });
    document.getElementById("weatherBackfillButton").addEventListener("click", async event => {
      const button = event.currentTarget;
      const startDate = backfillStart.value;
      const endDate = backfillEnd.value;
      if (!startDate || !endDate || startDate > endDate) {
        showMessage("weatherHistoryMessage", "请填写有效的开始和结束日期。", true);
        return;
      }
      button.disabled = true;
      showMessage("weatherHistoryMessage", "正在获取当前仓库坐标对应的逐小时天气模型历史…");
      try {
        const result = await requestJSON("/api/v1/weather/backfill", "POST", {
          start_date: startDate, end_date: endDate
        });
        showMessage("weatherHistoryMessage", `已保存 ${result.inserted} 个逐小时天气模型记录（${result.start_date} 至 ${result.end_date}）。`);
        await load();
      } catch (error) { showMessage("weatherHistoryMessage", error.message, true); }
      finally { button.disabled = false; }
    });
    refreshButton(load); load();
  }

  function scopeSummary(thresholds, intervals) {
    const values = Object.values({ ...(thresholds || {}), ...(intervals || {}) });
    return values.length ? "按继承规则" : "仓库默认";
  }

  function readSoundPreference() {
    try { return localStorage.getItem("grainsilo.alertSound.enabled") === "true"; }
    catch (_) { return false; }
  }

  function writeSoundPreference(enabled) {
    alertSoundOn = Boolean(enabled);
    try { localStorage.setItem("grainsilo.alertSound.enabled", alertSoundOn ? "true" : "false"); } catch (_) {}
    updateSoundButton();
  }

  function updateSoundButton() {
    const button = document.getElementById("alertSoundToggle");
    if (!button) return;
    button.textContent = alertSoundOn ? "提示音：开" : "提示音：关";
    button.setAttribute("aria-pressed", alertSoundOn ? "true" : "false");
  }

  function beepOnce() {
    if (!alertSoundOn) return;
    try {
      audioContext = audioContext || new (window.AudioContext || window.webkitAudioContext)();
      const oscillator = audioContext.createOscillator();
      const gain = audioContext.createGain();
      oscillator.type = "sine";
      oscillator.frequency.value = 740;
      gain.gain.setValueAtTime(0.0001, audioContext.currentTime);
      gain.gain.exponentialRampToValueAtTime(0.08, audioContext.currentTime + 0.015);
      gain.gain.exponentialRampToValueAtTime(0.0001, audioContext.currentTime + 0.22);
      oscillator.connect(gain); gain.connect(audioContext.destination);
      oscillator.start(); oscillator.stop(audioContext.currentTime + 0.23);
    } catch (_) { /* Browser may block sound; the visible alert remains authoritative. */ }
  }

  function selectedScopeInfo() {
    const scope = document.getElementById("thresholdScope").value;
    const target = document.getElementById("thresholdTarget").value;
    if (scope === "warehouse") return { scope, target: null };
    return { scope, target: target ? Number(target) : null };
  }

  function selectTargetOptions() {
    const scope = document.getElementById("thresholdScope").value;
    const label = document.getElementById("thresholdTargetLabel");
    const target = document.getElementById("thresholdTarget");
    label.hidden = scope === "warehouse";
    target.innerHTML = scope === "pile"
      ? piles.map(p => `<option value="${p.id}">${esc(p.name)} · 粮堆 ${p.id}</option>`).join("")
      : poles.map(p => `<option value="${esc(p.uid)}">${esc(p.name)} · ${esc(p.uid)}</option>`).join("");
  }

  async function loadScopeSettings() {
    const form = document.getElementById("thresholdForm");
    if (!form) return;
    try {
      const state = selectedScopeInfo();
      const scopeLabel = { warehouse: "仓库默认", pile: "粮堆覆盖", probe: "探杆覆盖" }[state.scope];
      document.getElementById("thresholdScopeLabel").textContent = scopeLabel;
      let effective, overrides, thresholdSources, intervalSources, remote;
      if (state.scope === "warehouse") {
        const config = await requestJSON("/api/v1/config");
        effective = config; overrides = config;
        thresholdSources = {}; intervalSources = {};
      } else if (state.scope === "pile") {
        const pile = piles.find(row => row.id === state.target);
        if (!pile) return;
        effective = { ...pile.thresholds, ...pile.intervals };
        overrides = { ...pile.threshold_overrides, ...pile.interval_overrides };
        thresholdSources = pile.threshold_sources; intervalSources = pile.interval_sources;
      } else {
        const pole = poles.find(row => row.uid === String(document.getElementById("thresholdTarget").value));
        if (!pole) return;
        effective = { ...pole.threshold, ...pole.intervals };
        overrides = { ...pole.threshold_overrides, ...pole.interval_overrides };
        thresholdSources = pole.threshold_sources; intervalSources = pole.interval_sources;
        remote = pole.remote_settings;
      }
      const fields = Array.from(form.querySelectorAll("[data-setting]"));
      fields.forEach(field => {
        const key = field.dataset.setting;
        const inherited = state.scope !== "warehouse" && (overrides[key] === null || overrides[key] === undefined);
        field.required = state.scope === "warehouse";
        field.value = state.scope === "warehouse" ? (effective[key] ?? "") : (inherited ? "" : overrides[key]);
        field.placeholder = inherited ? `继承当前 ${effective[key] ?? "—"}${units[key]}` : "";
      });
      const sourceFor = key => state.scope === "warehouse" ? "仓库默认" :
        ((thresholdSources || {})[key] || (intervalSources || {})[key] || "warehouse");
      document.getElementById("thresholdEffective").innerHTML = settingKeys.map(key =>
        `<div><strong>${labels[key]}：</strong>${esc(effective[key] ?? "—")}${units[key]} · 来源 ${esc(sourceFor(key))}</div>`).join("") +
        (remote ? `<div class="station-remote-state ${remote.status === "applied" ? "applied" : "pending"}">设备配置 v${remote.revision} · 已确认 v${remote.applied_revision} · ${remote.status === "applied" ? "S3 已应用" : "等待下一次上报确认"}</div>` : "");
    } catch (error) { showMessage("thresholdMessage", error.message, true); }
  }

  function loadAlertsPage() {
    const host = document.getElementById("stationAlertList");
    if (!host) return;
    async function load() {
      try {
        [poles, piles] = await Promise.all([requestJSON("/api/v1/poles"), requestJSON("/api/v1/piles")]);
        selectTargetOptions();
        await loadScopeSettings();
        const alerts = await requestJSON("/api/v1/alerts");
        const active = alerts.filter(row => row.active);
        document.getElementById("activeAlertCount").textContent = active.length;
        document.getElementById("measuredAlertCount").textContent = active.filter(row => row.source === "measured").length;
        document.getElementById("forecastAlertCount").textContent = active.filter(row => row.source === "forecast" || row.source === "weather_forecast").length;
        document.getElementById("pendingDeviceConfigs").textContent = poles.filter(p => p.remote_settings?.status !== "applied").length;
        const activeIds = new Set(active.map(row => String(row.id)));
        const newAlert = Array.from(activeIds).some(id => !activeAlertIds.has(id));
        activeAlertIds = activeIds;
        if (newAlert) beepOnce();
        const showAll = document.getElementById("alertFilter").value === "all";
        const visible = showAll ? alerts : active;
        host.innerHTML = visible.length ? visible.map(row => {
          const pole = poles.find(p => p.uid === row.uid);
          const source = row.source === "weather_forecast" ? `天气辅助预测 +${row.forecast_hour}h` : row.source === "forecast" ? `探杆趋势预测 +${row.forecast_hour}h` : "实测越限";
          const kind = row.ch === "temp" ? "温度" : "探杆周围相对湿度";
          return `<article class="station-alert-item ${row.active ? "active" : ""}"><div><h3>${row.active ? "告警中" : "已恢复"} · ${esc(pole?.name || row.uid)} · 节点通信地址 ${esc(row.node_addr)}</h3><p>${source} · ${kind} · 开始 ${fmtTime(row.begin_ts)}${row.end_ts ? ` · 恢复 ${fmtTime(row.end_ts)}` : ""} · 阈值 ${formatReading(row.th, row.ch === "temp" ? "°C" : "%RH", 2)}</p></div><div class="station-alert-item-value">${formatReading(row.value, row.ch === "temp" ? "°C" : "%RH", 2)}</div></article>`;
        }).join("") : '<div class="station-empty">当前没有活动告警</div>';
      } catch (error) { host.innerHTML = `<div class="station-empty">告警读取失败：${esc(error.message)}</div>`; }
    }
    const scopeSelect = document.getElementById("thresholdScope");
    scopeSelect.addEventListener("change", () => { selectTargetOptions(); loadScopeSettings(); });
    document.getElementById("thresholdTarget").addEventListener("change", loadScopeSettings);
    document.getElementById("alertFilter").addEventListener("change", load);
    document.getElementById("alertSoundToggle").addEventListener("click", () => {
      writeSoundPreference(!alertSoundOn);
      if (alertSoundOn && activeAlertIds.size) beepOnce();
    });
    document.getElementById("thresholdReset").addEventListener("click", () => {
      document.querySelectorAll("[data-setting]").forEach(input => { input.value = ""; });
    });
    document.getElementById("thresholdForm").addEventListener("submit", async event => {
      event.preventDefault();
      const state = selectedScopeInfo();
      const values = Object.fromEntries(settingKeys.map(key => {
        const value = document.querySelector(`[data-setting="${key}"]`).value.trim();
        return [key, value === "" && state.scope !== "warehouse" ? null : Number(value)];
      }));
      try {
        if (state.scope === "warehouse") await requestJSON("/api/v1/config", "POST", values);
        else if (state.scope === "pile") await requestJSON(`/api/v1/piles/${state.target}`, "POST", values);
        else await requestJSON(`/api/v1/poles/${encodeURIComponent(document.getElementById("thresholdTarget").value)}/config`, "POST", values);
        showMessage("thresholdMessage", "已保存；远程配置等待 S3 下一次上报并回执。", false);
        await load();
      } catch (error) { showMessage("thresholdMessage", error.message, true); }
    });
    updateSoundButton();
    refreshButton(load);
    load();
  }

  window.addEventListener("storage", event => {
    if (event.key === "grainsilo.alertSound.enabled") {
      alertSoundOn = event.newValue === "true";
      updateSoundButton();
    }
  });
  health();
  if (page === "warehouses") loadWarehousePage();
  else if (page === "piles") loadPilePage();
  else if (page === "probes") loadProbePage();
  else if (page === "environment") loadEnvironmentPage();
  else if (page === "alerts") loadAlertsPage();
})();
