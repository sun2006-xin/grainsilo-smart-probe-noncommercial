/* 作物 EMC 模型页：前端只采集输入与展示结果，算法唯一实现位于 station/crop_emc_model.py。 */
(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };
  var pileSelect = $("cropPile");
  var cropSelect = $("cropType");
  var wheatSelect = $("wheatType");
  var paddySelect = $("paddyPath");
  var resultBox = $("resultContent");
  var pageStatus = $("pageStatus");
  var piles = [];
  var profileLoadToken = 0;
  var probeTargets = [];
  var latestProbeForecast = null;
  var targetLoadToken = 0;
  var forecastRenderToken = 0;

  function setPageStatus(message, state) {
    pageStatus.textContent = message;
    var colorState = state === "ok" ? "live" : (state === "error" ? "error" : "pending");
    pageStatus.className = "health-pill " + colorState;
  }

  function make(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function showEmpty(title, message) {
    var empty = make("div", "result-empty");
    empty.appendChild(make("span", "empty-glyph", "≈"));
    empty.appendChild(make("h3", "", title));
    empty.appendChild(make("p", "", message));
    resultBox.replaceChildren(empty);
  }

  function updateConditionalFields() {
    $("wheatOptions").hidden = cropSelect.value !== "wheat";
    $("paddyOptions").hidden = cropSelect.value !== "paddy";
    $("saveProfileBtn").disabled = !pileSelect.value || !cropSelect.value;
  }

  function addPileOptions() {
    pileSelect.replaceChildren();
    if (!piles.length) {
      pileSelect.appendChild(make("option", "", "尚无粮堆，请先到粮堆管理中创建"));
      pileSelect.disabled = true;
      $("saveProfileBtn").disabled = true;
      showEmpty("还没有可配置的粮堆", "先在仓库总览中创建粮堆，再为它选择作物模型。");
      return;
    }
    pileSelect.disabled = false;
    pileSelect.appendChild(make("option", "", "请选择粮堆"));
    piles.forEach(function (pile) {
      var option = make("option", "", pile.name || ("粮堆 " + pile.id));
      option.value = String(pile.id);
      pileSelect.appendChild(option);
    });
    var requested = new URLSearchParams(location.search).get("pile_id");
    if (requested && piles.some(function (pile) { return String(pile.id) === requested; })) {
      pileSelect.value = requested;
    } else if (piles.length === 1) {
      pileSelect.value = String(piles[0].id);
    }
  }

  async function loadProfile() {
    var token = ++profileLoadToken;
    var pileId = pileSelect.value;
    cropSelect.value = "";
    wheatSelect.value = "soft";
    paddySelect.value = "both";
    updateConditionalFields();
    $("profileStatus").textContent = "每个粮堆可保存自己的作物与曲线选项。";
    showEmpty("等待计算", "选择作物，再输入空气温度与 RH。");
    if (!pileId) return;
    try {
      var response = await apiGet("/api/v1/crop-profile?pile_id=" + encodeURIComponent(pileId));
      if (token !== profileLoadToken) return;
      if (response.profile) {
        cropSelect.value = response.profile.crop_type;
        wheatSelect.value = response.profile.wheat_type;
        paddySelect.value = response.profile.paddy_path;
        $("profileStatus").textContent = "已读取此粮堆保存的作物模型设置。";
      } else {
        $("profileStatus").textContent = "此粮堆还没有保存作物设置；可选择作物后保存。";
      }
      updateConditionalFields();
      if (latestProbeForecast) renderProbeForecast(latestProbeForecast);
    } catch (error) {
      if (token === profileLoadToken) {
        $("profileStatus").textContent = "读取设置失败：" + error.message;
      }
    }
  }

  function liveEmpty(message) {
    var host = $("probeForecastResults");
    host.replaceChildren(make("div", "crop-live-empty", message));
  }

  async function calculateForecastEmc(temp, rh) {
    if (!cropSelect.value) return "选择作物后显示 EMC 参考";
    try {
      var result = await apiPost("/api/v1/crop-emc", {
        crop_type: cropSelect.value,
        wheat_type: wheatSelect.value,
        paddy_path: paddySelect.value,
        temp_c: temp,
        rh_pct: rh,
      });
      var values = (result.estimates || []).map(function (row) {
        return Number(row.wet_basis_pct);
      }).filter(Number.isFinite);
      if (!values.length) return "该情景没有有效 EMC 结果";
      var min = Math.min.apply(null, values).toFixed(2);
      var max = Math.max.apply(null, values).toFixed(2);
      return "EMC 参考 " + min + (max !== min ? "–" + max : "") + "% 湿基";
    } catch (error) {
      return error.message.indexOf("HTTP 400") >= 0
        ? "超出所选作物来源范围，未外推"
        : "EMC 参考暂不可用";
    }
  }

  async function renderProbeForecast(data) {
    latestProbeForecast = data;
    var token = ++forecastRenderToken;
    var host = $("probeForecastResults");
    host.replaceChildren();
    if (!data || !data.ok) {
      liveEmpty("暂时没有可显示的探杆预测。");
      return;
    }
    var station = data.station_prediction || {};
    var s3 = data.s3_prediction || {};
    var sources = [
      { title: "电脑端独立预测", status: station.status,
        meta: "由 Station 历史数据库独立重算。",
        points: station.status === "ready" ? (station.forecast || []) : [] },
      { title: "S3 探杆端预测", status: s3.status,
        meta: "由 S3 计算并随快照上报；旧短窗结果会被过滤。",
        points: s3.status === "ready" ? (s3.points || []).map(function (point) {
          return { hour: point.hour, temperature_c: point.temp, rh_pct: point.rh };
        }) : [] },
    ];
    var hasAny = sources.some(function (source) { return source.points.length > 0; });
    if (!hasAny) {
      var statusLine = sources.map(function (source) {
        if (source.status === "stale") return source.title + "数据已过期或设备离线";
        if (source.status === "warming_up") return source.title + "正在积累有效历史";
        if (source.status === "ready") return source.title + "暂无可用时域";
        return source.title + "尚无上报记录";
      }).join("；") + "。";
      $("probeForecastStatus").textContent = statusLine;
      liveEmpty(statusLine + "+1h 至少需要 2.5h、+3h 需要 6h、+6h 需要 12h 历史；短时间斜率不会伪装成小时预测。");
      return;
    }
    $("probeForecastStatus").textContent = "已读取探杆历史预测；仅代表测点附近空气状态。";
    for (var source of sources) {
      var article = make("article", "crop-live-source");
      article.appendChild(make("h3", "", source.title));
      article.appendChild(make("p", "crop-live-source-meta", source.meta));
      var pointsHost = make("div", "crop-live-points");
      if (!source.points.length) {
        var emptyMessage = source.status === "stale"
          ? "快照已过期或探杆离线，当前值不能作为有效预测。"
          : source.status === "warming_up"
            ? "该来源的有效历史尚不足以开放此时域。"
            : "该来源尚未产生有效预测。";
        pointsHost.appendChild(make("div", "crop-live-empty", emptyMessage));
      } else {
        var rows = await Promise.all(source.points.map(async function (point) {
          return { point: point, emc: await calculateForecastEmc(
            point.temperature_c, point.rh_pct) };
        }));
        if (token !== forecastRenderToken) return;
        rows.forEach(function (row) {
          var point = row.point;
          var card = make("div", "crop-live-point");
          card.appendChild(make("b", "", "+" + Number(point.hour) + " 小时"));
          card.appendChild(make("strong", "", Number(point.temperature_c).toFixed(1) + " °C"));
          card.appendChild(make("span", "", Number(point.rh_pct).toFixed(1) + " %RH"));
          card.appendChild(make("small", "", row.emc));
          pointsHost.appendChild(card);
        });
      }
      article.appendChild(pointsHost);
      host.appendChild(article);
    }
    var risk = data.risk || {};
    if (risk.active) {
      host.appendChild(make("div", "crop-live-empty",
        "存在实测超限或某来源的趋势预测越限；请查看电脑端告警中心并核对原始测量。预测提示与实测告警分开。"));
    }
  }

  async function loadForecastTargets() {
    var token = ++targetLoadToken;
    var select = $("forecastProbe");
    var pileId = Number(pileSelect.value);
    probeTargets = [];
    latestProbeForecast = null;
    select.replaceChildren(make("option", "", pileId ? "读取探杆节点…" : "先选择粮堆"));
    select.disabled = true;
    $("probeForecastStatus").textContent = "正在读取该粮堆的绑定节点…";
    liveEmpty("正在查询电脑中心站登记的探杆和节点。");
    if (!pileId) return;
    try {
      var poles = await apiGet("/api/v1/poles");
      if (token !== targetLoadToken) return;
      poles.filter(function (pole) { return Number(pole.pile_id) === pileId; })
        .forEach(function (pole) {
          var nodes = Array.isArray(pole.node_states) && pole.node_states.length
            ? pole.node_states
            : (pole.snapshot && Array.isArray(pole.snapshot.nodes) ? pole.snapshot.nodes : []);
          nodes.forEach(function (node) {
            var addr = Number(node.addr);
            if (!Number.isInteger(addr) || addr < 1 || addr > 247) return;
            probeTargets.push({ uid: String(pole.uid), name: String(pole.name || pole.uid),
              addr: addr, online: !!node.online });
          });
        });
      select.replaceChildren();
      if (!probeTargets.length) {
        select.appendChild(make("option", "", "此粮堆还没有已上报节点"));
        $("probeForecastStatus").textContent = "无可查询的探杆节点";
        liveEmpty("先让探杆向电脑中心站成功上报，再到粮堆管理中将探杆绑定到此粮堆。");
        return;
      }
      probeTargets.forEach(function (target, index) {
        var option = make("option", "", target.name + " · 节点 " + target.addr +
          (target.online ? " · 在线" : " · 最近有记录"));
        option.value = target.uid + "|" + target.addr;
        select.appendChild(option);
        if (index === 0) select.value = option.value;
      });
      select.disabled = false;
      await loadProbeForecast();
    } catch (error) {
      if (token !== targetLoadToken) return;
      select.replaceChildren(make("option", "", "读取探杆列表失败"));
      $("probeForecastStatus").textContent = "中心站暂不可用";
      liveEmpty("读取节点列表失败：" + error.message);
    }
  }

  async function loadProbeForecast() {
    var selected = $("forecastProbe").value;
    if (!selected) return;
    var split = selected.lastIndexOf("|");
    var uid = selected.slice(0, split);
    var addr = selected.slice(split + 1);
    $("probeForecastStatus").textContent = "正在读取 S3 与电脑端预测…";
    try {
      var data = await apiGet("/api/v1/probe-forecast?uid=" +
        encodeURIComponent(uid) + "&addr=" + encodeURIComponent(addr));
      if ($("forecastProbe").value !== selected) return;
      await renderProbeForecast(data);
    } catch (error) {
      $("probeForecastStatus").textContent = "读取预测失败";
      liveEmpty("电脑端暂时无法重算此节点：" + error.message);
    }
  }

  function sourceLink(url) {
    try {
      var parsed = new URL(url);
      if (parsed.protocol !== "https:" ||
          (parsed.hostname !== "files.ontario.ca" && parsed.hostname !== "doi.org")) return null;
      var link = make("a", "", parsed.hostname === "doi.org" ? "稻谷论文" : "作物 EMC 表");
      link.href = parsed.href;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      return link;
    } catch (_) {
      return null;
    }
  }

  function pathLabel(path) {
    return ({ adsorption: "吸附曲线", desorption: "解吸曲线", table: "来源表格插值" })[path] || path;
  }

  function renderResult(result) {
    var estimates = Array.isArray(result.estimates) ? result.estimates : [];
    if (!estimates.length) throw new Error("模型没有返回有效估算值");
    resultBox.replaceChildren();
    var values = estimates.map(function (row) { return Number(row.wet_basis_pct); });
    var headline = make("div", "result-summary");
    var label = values.length > 1 ? "稻谷吸附 / 解吸参考范围" : "平衡含水率参考估算";
    headline.appendChild(make("span", "result-label", label));
    var value = make("div", "result-value");
    var min = Math.min.apply(null, values).toFixed(2);
    var max = Math.max.apply(null, values).toFixed(2);
    value.appendChild(document.createTextNode(min + (values.length > 1 ? "–" + max : "")));
    value.appendChild(make("small", "", "% 湿基"));
    headline.appendChild(value);
    var subtitle = values.length > 1
      ? "吸附与解吸路径差异；不是统计置信区间，也不是粮食实测水分。"
      : "假设粮食与该空气状态充分接近平衡时的参考值。";
    headline.appendChild(make("p", "result-subtitle", subtitle));
    resultBox.appendChild(headline);

    if (estimates.length > 1) {
      var branches = make("div", "result-branches");
      estimates.forEach(function (row) {
        var branch = make("div", "result-branch");
        branch.appendChild(make("b", "", pathLabel(row.path)));
        branch.appendChild(make("strong", "", Number(row.wet_basis_pct).toFixed(2) + "%"));
        branch.appendChild(make("small", "", "湿基 EMC 参考值"));
        branches.appendChild(branch);
      });
      resultBox.appendChild(branches);
    } else if (estimates[0].dry_basis_pct !== undefined) {
      resultBox.appendChild(make("p", "result-subtitle",
        pathLabel(estimates[0].path) + "：" + Number(estimates[0].dry_basis_pct).toFixed(2) + "% 干基"));
    }

    var meta = make("div", "result-meta");
    meta.appendChild(make("span", "", result.model));
    meta.appendChild(make("span", "", "输入 " + $("tempC").value + "°C / " + $("rhPct").value + "% RH"));
    var link = sourceLink(result.source_url);
    if (link) meta.appendChild(link);
    resultBox.appendChild(meta);
  }

  async function calculate() {
    if (!cropSelect.value) {
      showEmpty("先选择作物", "稻谷、小麦或玉米使用不同的参考曲线。");
      return;
    }
    var temp = $("tempC").value.trim();
    var rh = $("rhPct").value.trim();
    if (temp === "" || rh === "") {
      showEmpty("还缺少输入", "请输入空气温度和相对湿度；结果只适用于页面显示的来源范围。");
      return;
    }
    var button = $("calculateBtn");
    button.disabled = true;
    button.textContent = "正在计算…";
    setPageStatus("正在计算参考值", "pending");
    try {
      var result = await apiPost("/api/v1/crop-emc", {
        crop_type: cropSelect.value,
        wheat_type: wheatSelect.value,
        paddy_path: paddySelect.value,
        temp_c: temp,
        rh_pct: rh,
      });
      renderResult(result);
      setPageStatus("模型参考值已计算", "ok");
    } catch (error) {
      showEmpty("无法计算", error.message.indexOf("HTTP 400") >= 0
        ? "输入超出当前作物来源数据的适用范围，或数值无效。页面不会对来源范围外的数据外推。"
        : "计算服务暂不可用，请确认电脑端中心站正在运行。(" + error.message + ")");
      setPageStatus("需要检查输入或连接", "pending");
    } finally {
      button.disabled = false;
      button.replaceChildren(document.createTextNode("计算参考 EMC "));
      button.appendChild(make("span", "", "→"));
    }
  }

  async function saveProfile() {
    var pileId = Number(pileSelect.value);
    if (!pileId || !cropSelect.value) return;
    var button = $("saveProfileBtn");
    button.disabled = true;
    $("profileStatus").textContent = "正在保存此粮堆的模型设置…";
    try {
      await apiPost("/api/v1/crop-profile", {
        pile_id: pileId,
        crop_type: cropSelect.value,
        wheat_type: wheatSelect.value,
        paddy_path: paddySelect.value,
      });
      $("profileStatus").textContent = "已保存；下次选择此粮堆时会自动恢复。";
      setPageStatus("粮堆模型设置已保存", "ok");
    } catch (error) {
      $("profileStatus").textContent = "保存失败：" + error.message;
    } finally {
      updateConditionalFields();
    }
  }

  async function init() {
    cropSelect.addEventListener("change", function () {
      updateConditionalFields();
      if (latestProbeForecast) renderProbeForecast(latestProbeForecast);
    });
    wheatSelect.addEventListener("change", function () {
      if (latestProbeForecast) renderProbeForecast(latestProbeForecast);
    });
    paddySelect.addEventListener("change", function () {
      if (latestProbeForecast) renderProbeForecast(latestProbeForecast);
    });
    pileSelect.addEventListener("change", async function () {
      await loadProfile();
      await loadForecastTargets();
    });
    $("calculateBtn").addEventListener("click", calculate);
    $("saveProfileBtn").addEventListener("click", saveProfile);
    $("refreshProbeForecast").addEventListener("click", loadProbeForecast);
    $("forecastProbe").addEventListener("change", loadProbeForecast);
    try {
      piles = await apiGet("/api/v1/piles");
      addPileOptions();
      if (pileSelect.value) await loadProfile();
      if (pileSelect.value) await loadForecastTargets();
      setPageStatus(piles.length ? "模型估算 · 本地中心站" : "尚无粮堆", piles.length ? "ok" : "pending");
      window.setInterval(function () {
        if (pileSelect.value && !$("forecastProbe").disabled) loadProbeForecast();
      }, 60_000);
    } catch (error) {
      pileSelect.replaceChildren(make("option", "", "粮堆列表加载失败"));
      pileSelect.disabled = true;
      $("saveProfileBtn").disabled = true;
      showEmpty("读取失败", "请确认电脑端中心站正在运行后重试。(" + error.message + ")");
      setPageStatus("中心站连接失败", "pending");
    }
  }

  document.addEventListener("DOMContentLoaded", init);
}());
