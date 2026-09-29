(function (root, factory) {
  "use strict";
  var api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.GrainDataScene = api;
})(typeof window !== "undefined" ? window : globalThis, function () {
  "use strict";

  function numberOrNull(value) {
    if (value === null || value === undefined || value === "") return null;
    var n = Number(value);
    return Number.isFinite(n) ? n : null;
  }

  function escapeHtml(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, function (ch) {
      return ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[ch];
    });
  }

  function finitePositive(value) {
    var n = numberOrNull(value);
    return n !== null && n > 0;
  }

  function buildModel(input) {
    input = input || {};
    var allPiles = Array.isArray(input.piles) ? input.piles : [];
    var selectedId = input.selectedPileId == null ? null : String(input.selectedPileId);
    var piles = selectedId === null ? allPiles.slice() : allPiles.filter(function (pile) {
      return String(pile.id) === selectedId;
    });
    var allPoles = Array.isArray(input.poles) ? input.poles : [];
    var poles = selectedId === null ? allPoles.slice() : allPoles.filter(function (pole) {
      return pole.pile_id != null && String(pole.pile_id) === selectedId;
    });
    var config = input.config || {};
    var dimensions = {
      length: finitePositive(config.wh_l) ? Number(config.wh_l) : 20,
      width: finitePositive(config.wh_w) ? Number(config.wh_w) : 10,
      height: finitePositive(config.wh_h) ? Number(config.wh_h) : 8,
    };
    poles = poles.map(function (pole) {
      var readings = pole.snapshot && Array.isArray(pole.snapshot.nodes) ? pole.snapshot.nodes : [];
      var states = Array.isArray(pole.node_states) ? pole.node_states : [];
      var nodes = readings.map(function (reading) {
        var state = states.filter(function (candidate) { return Number(candidate.addr) === Number(reading.addr); })[0];
        return {
          addr: numberOrNull(reading.addr),
          temp: numberOrNull(reading.temp),
          rh: numberOrNull(reading.rh),
          online: state && typeof state.online === "boolean" ? state.online : null,
        };
      });
      var x = numberOrNull(pole.x), z = numberOrNull(pole.z);
      if (x === null || x < 0 || x > dimensions.length) x = null;
      if (z === null || z < 0 || z > dimensions.width) z = null;
      return {
        uid: String(pole.uid || ""),
        name: String(pole.name || (pole.uid ? "探杆 " + pole.uid : "未命名探杆")),
        x: x,
        z: z,
        online: Number(pole.status) === 1,
        pileId: pole.pile_id == null ? null : String(pole.pile_id),
        nodes: nodes,
      };
    });
    var configuredPiles = piles.filter(function (pile) {
      var x = numberOrNull(pile.x), z = numberOrNull(pile.z);
      var l = numberOrNull(pile.l), w = numberOrNull(pile.w);
      return finitePositive(pile.volume) && finitePositive(pile.height) && finitePositive(l) && finitePositive(w) &&
        x !== null && z !== null && x - l / 2 >= 0 && x + l / 2 <= dimensions.length &&
        z - w / 2 >= 0 && z + w / 2 <= dimensions.width;
    });
    var heatmap = input.heatmap || {};
    var field = heatmap.field || {};
    var fieldPoints = Array.isArray(heatmap.points) ? heatmap.points.filter(function (point) {
      return numberOrNull(point.x) !== null && numberOrNull(point.z) !== null &&
        numberOrNull(point.d) !== null &&
        numberOrNull(input.metric === "rh" ? point.rh : point.temp) !== null;
    }) : [];
    /* The current API derives depth from node address/spacing. Only an explicit
       backend validation can authorize a 3D field; addresses are not heights. */
    var hasSpatialHeatField = configuredPiles.length > 0 && field.method === "idw" &&
      field.interpolated === true && field.spatially_validated === true && fieldPoints.length > 0;
    var nodeCount = poles.reduce(function (sum, pole) { return sum + pole.nodes.length; }, 0);
    var onlinePoleCount = poles.filter(function (pole) { return pole.online; }).length;
    return {
      config: dimensions,
      piles: piles,
      configuredPiles: configuredPiles,
      poles: poles,
      nodeCount: nodeCount,
      onlinePoleCount: onlinePoleCount,
      selectedPileId: selectedId,
      metric: input.metric === "rh" ? "rh" : "temp",
      probesVisible: input.probesVisible !== false,
      hasConfiguredPileGeometry: configuredPiles.length > 0,
      hasSpatialHeatField: hasSpatialHeatField,
      fieldPoints: hasSpatialHeatField ? fieldPoints : [],
      fieldMethod: hasSpatialHeatField ? "IDW 空间估算" : "暂无可用温度场",
    };
  }

  function project(x, z, y, config) {
    return {
      x: 245 + x * (520 / config.length) - z * (200 / config.width),
      y: 330 + x * (90 / config.length) + z * (105 / config.width) - y * (272 / config.height),
    };
  }

  function pointString(points) {
    return points.map(function (point) { return point.x.toFixed(1) + "," + point.y.toFixed(1); }).join(" ");
  }

  function sceneTemperatureColor(value) {
    if (value === null) return "#91a6a1";
    if (value < 18) return "#2870da";
    if (value < 23) return "#2daed0";
    if (value < 27) return "#43b985";
    if (value < 30) return "#e4b940";
    return "#e47842";
  }

  function sceneHumidityColor(value) {
    if (value === null) return "#91a6a1";
    if (value < 50) return "#2787c8";
    if (value < 65) return "#31a69a";
    return "#d28c34";
  }

  function renderSvg(model) {
    var cfg = model.config;
    var floor = [project(0, 0, 0, cfg), project(cfg.length, 0, 0, cfg),
      project(cfg.length, cfg.width, 0, cfg), project(0, cfg.width, 0, cfg)];
    var roof = floor.map(function (point) { return { x: point.x, y: point.y - 230 }; });
    var markup = [
      '<svg class="gds-svg" viewBox="0 0 960 570" role="img" aria-labelledby="gdsTitle gdsDesc">',
      '<title id="gdsTitle">由仓房配置和实时探杆数据生成的粮仓空间视图</title>',
      '<desc id="gdsDesc">显示仓房结构、已配置粮堆几何、探杆位置标记和实测节点数据。插值场仅在空间坐标经过验证后显示。</desc>',
      '<defs><linearGradient id="gdsWall" x2="0" y2="1"><stop stop-color="#f7fbf8"/><stop offset="1" stop-color="#e5efea"/></linearGradient>',
      '<linearGradient id="gdsFloor" x2="0" y2="1"><stop stop-color="#dce8e2"/><stop offset="1" stop-color="#c7d6cf"/></linearGradient>',
      '<linearGradient id="gdsGrain" x2="0" y2="1"><stop stop-color="#e4c887"/><stop offset="1" stop-color="#b78642"/></linearGradient></defs>',
      '<rect width="960" height="570" fill="#f1f7f3"/>',
      '<polygon points="' + pointString([roof[0], roof[1], floor[1], floor[0]]) + '" fill="url(#gdsWall)" stroke="#9eb6ad" stroke-width="2"/>',
      '<polygon points="' + pointString([roof[0], roof[3], floor[3], floor[0]]) + '" fill="#eaf2ed" stroke="#9eb6ad" stroke-width="2"/>',
      '<polygon points="' + pointString(floor) + '" fill="url(#gdsFloor)" stroke="#8da79c" stroke-width="2"/>',
    ];
    for (var i = 1; i < 6; i++) {
      var x = cfg.length * i / 6;
      var a = project(x, 0, 0, cfg), b = project(x, 0, cfg.height, cfg);
      markup.push('<line x1="' + a.x.toFixed(1) + '" y1="' + a.y.toFixed(1) + '" x2="' + b.x.toFixed(1) + '" y2="' + b.y.toFixed(1) + '" stroke="#c5d5ce" stroke-width="4"/>');
      var windowBase = project(x, 0, cfg.height * 0.22, cfg);
      markup.push('<rect x="' + (windowBase.x - 18).toFixed(1) + '" y="' + (windowBase.y - 22).toFixed(1) + '" width="32" height="28" rx="2" fill="#d9edf0" stroke="#a9c5c8"/>');
    }
    for (var j = 1; j < 4; j++) {
      var z = cfg.width * j / 4;
      var c = project(0, z, 0, cfg), d = project(0, z, cfg.height, cfg);
      markup.push('<line x1="' + c.x.toFixed(1) + '" y1="' + c.y.toFixed(1) + '" x2="' + d.x.toFixed(1) + '" y2="' + d.y.toFixed(1) + '" stroke="#c6d6cf" stroke-width="3"/>');
    }
    for (var gx = 1; gx < 8; gx++) {
      var startX = project(cfg.length * gx / 8, 0, 0, cfg);
      var endX = project(cfg.length * gx / 8, cfg.width, 0, cfg);
      markup.push('<line x1="' + startX.x.toFixed(1) + '" y1="' + startX.y.toFixed(1) + '" x2="' + endX.x.toFixed(1) + '" y2="' + endX.y.toFixed(1) + '" stroke="#b6c8c0" stroke-width="1" opacity=".55"/>');
    }

    if (model.hasConfiguredPileGeometry) {
      model.configuredPiles.forEach(function (pile, index) {
        var cx = numberOrNull(pile.x) == null ? cfg.length / 2 : Number(pile.x);
        var cz = numberOrNull(pile.z) == null ? cfg.width / 2 : Number(pile.z);
        var halfL = Math.min(Number(pile.l), cfg.length) / 2;
        var halfW = Math.min(Number(pile.w), cfg.width) / 2;
        var h = Math.min(Number(pile.height), cfg.height);
        var corners = [project(cx - halfL, cz - halfW, 0, cfg), project(cx + halfL, cz - halfW, 0, cfg),
          project(cx + halfL, cz + halfW, 0, cfg), project(cx - halfL, cz + halfW, 0, cfg)];
        var crown = project(cx, cz, h, cfg);
        markup.push('<g class="gds-pile" data-pile-id="' + escapeHtml(pile.id) + '" tabindex="0" role="button" aria-label="选择粮堆 ' + escapeHtml(pile.name) + '">');
        markup.push('<polygon points="' + pointString(corners) + '" fill="#c69a54" opacity=".42" stroke="#9c763f" stroke-width="1.5"/>');
        for (var side = 0; side < corners.length; side++) {
          var next = corners[(side + 1) % corners.length];
          markup.push('<polygon points="' + pointString([corners[side], next, crown]) + '" fill="url(#gdsGrain)" stroke="#aa8148" stroke-width="1.4"/>');
        }
        markup.push('<text x="' + crown.x.toFixed(1) + '" y="' + (crown.y - 11).toFixed(1) + '" text-anchor="middle" class="gds-svg-label">' + escapeHtml(pile.name || "粮堆 " + (index + 1)) + '</text>');
        markup.push('<text x="' + crown.x.toFixed(1) + '" y="' + (crown.y + 8).toFixed(1) + '" text-anchor="middle" class="gds-svg-sub">' + Number(pile.volume).toFixed(1) + ' m³ · 配置几何示意</text></g>');
      });
    } else {
      markup.push('<g class="gds-empty-geometry"><rect x="290" y="205" width="380" height="88" rx="14" fill="#ffffff" fill-opacity=".92" stroke="#d3e2da"/>');
      markup.push('<text x="480" y="240" text-anchor="middle" class="gds-svg-label">粮堆几何未配置</text><text x="480" y="266" text-anchor="middle" class="gds-svg-sub">请填写体积与高度；未配置前不绘制虚构粮堆</text></g>');
    }

    if (model.hasSpatialHeatField) {
      var colorFn = model.metric === "rh" ? sceneHumidityColor : sceneTemperatureColor;
      model.fieldPoints.slice(0, 600).forEach(function (point) {
        var p = project(Number(point.x), Number(point.z), Number(point.d), cfg);
        var value = numberOrNull(model.metric === "rh" ? point.rh : point.temp);
        markup.push('<circle class="gds-field-point" cx="' + p.x.toFixed(1) + '" cy="' + p.y.toFixed(1) + '" r="4.2" fill="' + colorFn(value) + '" fill-opacity=".78" stroke="#fff" stroke-opacity=".72" stroke-width=".8"><title>' + escapeHtml(model.metric === "rh" ? "相对湿度 " + value + "%RH" : "温度 " + value + "°C") + '</title></circle>');
      });
      markup.push('<rect x="34" y="42" width="190" height="32" rx="16" fill="#fff" stroke="#9ac8b4"/><text x="129" y="63" text-anchor="middle" class="gds-svg-sub">IDW 空间估算样点（非 CFD）</text>');
    } else {
      markup.push('<rect x="34" y="42" width="214" height="32" rx="16" fill="#fff" stroke="#d4e2db"/><text x="141" y="63" text-anchor="middle" class="gds-svg-sub">暂无可用温度场 · 仅显示实测</text>');
    }

    if (model.probesVisible) {
      model.poles.forEach(function (pole) {
        if (pole.x === null || pole.z === null) return;
        var ground = project(pole.x, pole.z, 0, cfg);
        var height = Math.max(34, Math.min(56, 36 + pole.nodes.length * 3));
        var pinColor = pole.online ? "#138b76" : "#8b9c96";
        markup.push('<g class="gds-probe" data-pole-uid="' + escapeHtml(pole.uid) + '" tabindex="0" role="button" aria-label="打开探杆 ' + escapeHtml(pole.name) + ' 详情">');
        markup.push('<line x1="' + ground.x.toFixed(1) + '" y1="' + (ground.y - height).toFixed(1) + '" x2="' + ground.x.toFixed(1) + '" y2="' + (ground.y - 3).toFixed(1) + '" stroke="' + pinColor + '" stroke-width="5" stroke-linecap="round"/>');
        markup.push('<circle cx="' + ground.x.toFixed(1) + '" cy="' + (ground.y - height).toFixed(1) + '" r="8" fill="' + pinColor + '" stroke="#fff" stroke-width="3"/>');
        markup.push('<rect x="' + (ground.x - 48).toFixed(1) + '" y="' + (ground.y - height - 30).toFixed(1) + '" width="96" height="20" rx="10" fill="#fff" stroke="#c8d9d1"/>');
        markup.push('<text x="' + ground.x.toFixed(1) + '" y="' + (ground.y - height - 16).toFixed(1) + '" text-anchor="middle" class="gds-svg-sub">' + escapeHtml(pole.uid || pole.name) + '</text></g>');
      });
    }
    markup.push('<text x="28" y="544" class="gds-svg-sub">仓房尺寸 ' + cfg.length.toFixed(1) + ' × ' + cfg.width.toFixed(1) + ' × ' + cfg.height.toFixed(1) + ' m · 探杆符号仅表示配置位置，不代表节点物理高度</text>');
    markup.push('</svg>');
    return markup.join("");
  }

  function renderMeasurements(model) {
    if (!model.poles.length) return '<div class="gds-empty">暂无探杆数据</div>';
    return model.poles.map(function (pole) {
      var rows = pole.nodes.length ? pole.nodes.map(function (node) {
        var stateText = node.online === true ? "在线" : (node.online === false ? "离线" : "状态未知");
        var stateClass = node.online === true ? "online" : (node.online === false ? "offline" : "unknown");
        var activeValue = model.metric === "rh" ? node.rh : node.temp;
        var activeUnit = model.metric === "rh" ? "%RH" : "°C";
        var secondaryValue = model.metric === "rh" ? node.temp : node.rh;
        var secondaryUnit = model.metric === "rh" ? "°C" : "%RH";
        var activeText = activeValue === null ? "—" : activeValue.toFixed(2) + " " + activeUnit;
        var secondaryText = secondaryValue === null ? "—" : secondaryValue.toFixed(2) + " " + secondaryUnit;
        return '<div class="gds-node-row"><span class="gds-node-id">节点 ' + escapeHtml(node.addr == null ? "?" : node.addr) + '</span><b>' + activeText + '</b><span class="gds-node-secondary">' + secondaryText + '</span><span class="gds-node-state ' + stateClass + '">' + stateText + '</span></div>';
      }).join("") : '<div class="gds-empty">探杆暂无有效节点测值</div>';
      return '<section class="gds-pole-card"><header><div><span class="gds-pole-kicker">实测探杆</span><h3>' + escapeHtml(pole.name) + '</h3></div><span class="gds-pole-status ' + (pole.online ? "online" : "offline") + '">' + (pole.online ? "在线" : "离线") + '</span></header><p class="gds-pole-coord">位置配置：' + (pole.x === null || pole.z === null ? "未设置" : "X " + pole.x.toFixed(2) + " m · Z " + pole.z.toFixed(2) + " m") + '</p>' + rows + '<p class="gds-mapping-note">节点地址用于标识，不据此推断上/中/下位置。</p></section>';
    }).join("");
  }

  function renderMarkup(model) {
    var pileCount = model.piles.length;
    var onlineNodes = model.poles.reduce(function (sum, pole) {
      return sum + pole.nodes.filter(function (node) { return node.online === true; }).length;
    }, 0);
    var geometryStatus = model.hasConfiguredPileGeometry ? "粮堆几何已配置" : "粮堆几何待配置";
    var fieldStatus = model.hasSpatialHeatField ? model.fieldMethod : "空间热场待坐标校验";
    return '<div class="gds-layout"><div class="gds-viewport">' +
      '<div class="gds-view-caption"><span>仓房空间 · 数据映射</span><span>' + escapeHtml(geometryStatus) + '</span></div>' +
      renderSvg(model) +
      '<div class="gds-viewport-foot"><span>' + pileCount + ' 个粮堆</span><span>' + model.poles.length + ' 根探杆</span><span>' + model.nodeCount + ' 个测点</span><span>' + onlineNodes + ' 个测点在线</span><span>' + escapeHtml(fieldStatus) + '</span></div></div>' +
      '<aside class="gds-data-rail"><div class="gds-data-heading"><div><span class="gds-pole-kicker">LIVE MEASUREMENTS</span><h3>实时测点数据</h3></div><span class="gds-data-count">' + model.nodeCount + '</span></div>' +
      '<p class="gds-data-note">数值来自探杆最近一次上报。温度与相对湿度均为实测；缺失值显示为“—”。</p>' +
      '<div class="gds-measurements">' + renderMeasurements(model) + '</div>' +
      '<div class="gds-field-notice"><b>' + escapeHtml(fieldStatus) + '</b><span>' + (model.hasSpatialHeatField ? "显示后端提供且标记为空间坐标有效的 IDW 样点；不表示 CFD 流体求解。" : "当前 API 未提供经过校验的三维节点位置，暂不绘制插值色场或等温线。") + '</span></div></aside></div>';
  }

  function render(container, input, callbacks) {
    if (typeof container === "string") container = document.getElementById(container);
    if (!container) return null;
    var model = buildModel(input);
    container.innerHTML = renderMarkup(model);
    callbacks = callbacks || {};
    container.querySelectorAll("[data-pile-id]").forEach(function (element) {
      var activate = function () { if (callbacks.onPileClick) callbacks.onPileClick(element.getAttribute("data-pile-id")); };
      element.addEventListener("click", activate);
      element.addEventListener("keydown", function (event) {
        if (event.key === "Enter" || event.key === " ") { event.preventDefault(); activate(); }
      });
    });
    container.querySelectorAll("[data-pole-uid]").forEach(function (element) {
      var activate = function () { if (callbacks.onPoleClick) callbacks.onPoleClick(element.getAttribute("data-pole-uid")); };
      element.addEventListener("click", activate);
      element.addEventListener("keydown", function (event) {
        if (event.key === "Enter" || event.key === " ") { event.preventDefault(); activate(); }
      });
    });
    return model;
  }

  return { buildModel: buildModel, renderSvg: renderSvg, renderMarkup: renderMarkup, render: render };
});
