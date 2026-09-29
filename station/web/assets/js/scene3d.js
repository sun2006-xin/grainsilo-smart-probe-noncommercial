/* ============================================================
   scene3d.js - 3D 粮仓部署场景（Three.js r128）
   ------------------------------------------------------------
   只负责 3D 渲染，对外接口：
     Scene3D.init(containerId)              初始化场景
     Scene3D.setConfig(cfg)                 全局配置（仓库尺寸）
     Scene3D.setPoleList(list)              全量重建杆（按 uid diff 增量刷新用 updatePoles）
     Scene3D.updatePoles(poleList)          增量刷新杆/节点（按 uid diff）
     Scene3D.setPileList(piles)             粮堆列表 -> 多锥台渲染（与后端 piles API 对齐）
     Scene3D.setSelectedPile(id)            选中粮堆（锥台高亮）
     Scene3D.setSelectedPole(uid)           选中探杆（杆身高亮）
     Scene3D.setHeatmap(hm)                 连续切片/体积场（IDW 插值 + 流动动画）
     Scene3D.setDragEnd(fn)                 拖杆松手回调 fn(uid, x, z)
     Scene3D.setOnPileClick(fn)             点击粮堆回调 fn(pileId)
     Scene3D.setOnPoleClick(fn)             点击探杆回调 fn(uid)
   状态表达：
     杆离线（45min 无上报）-> 整杆变灰
     节点失联（超 node_offline_sec 无上报，记忆保留）-> 节点球变灰
     告警节点 -> 脉冲放大
   多粮堆：每根杆按 pile_id 归属粮堆，节点深度按杆位处粮面高度计算
   （与后端 _heap_height 同公式：顶面居中缩小 s=0.2 的四棱锥台）
   ============================================================ */
window.Scene3D = (function () {
  "use strict";

  var WH = { L: 20, W: 10, H: 8 };        // 粮仓尺寸（米），setConfig 更新
  var POLE_R = 0.07;                      // 杆半径
  var NODE_R = 0.16;                      // 节点球半径
  var POLE_ABOVE = 0.6;                   // 杆露出地面高度（米，无粮堆时）
  var NODE_TOP_OFF = 0.3;                 // 最上层节点距粮面距离（300mm 标准）
  var ALARM_PULSE = 0.30;                 // 告警脉冲幅度
  var DRAG_TH = 6;                        // 拖动判定像素阈值（小于=点击）
  var HEAP_S = 0.2;                       // 粮堆顶面收缩比例（与后端一致）

  var scene, camera, renderer, raycaster, mouse;
  var controls = null;                    // OrbitControls（旋转/缩放）
  var container, tooltip;
  var staticGroup = null;                 // 仓库线框/刻度/地面
  var pileGroup = null;                   // 粮堆锥台组（多堆）
  var pileList = [];                      // 粮堆数据 [{id,x,z,l,w,height,...}]
  var pileMeshes = {};                    // pile id -> {group, mesh, line}
  var selectedPile = null;                // 选中粮堆 id
  var selectedPole = null;                // 选中杆 uid
  var poleGroup = null;                   // 杆容器
  var poleMeshes = {};                    // uid -> 杆渲染组
  var poleData = {};                      // uid -> 杆数据（增量更新用）
  var heatGroup = null;                   // 温度体积 + 示意流线 + 示踪粒子
  var probeGroup = null;                  // 后端实测探针叠加层（与插值场分离）
  var cloud = null;                       // 温度体积采样点
  var voxelField = null;                  // 半透明体素场，避免只显示散点
  var cloudPhase = [];                    // 体积点轻微流动相位
  var flowTracers = [];                   // 沿后端 pathlines 移动的示踪粒子
  var scaleEl = null;                     // 右下角温度色标条 DOM
  var lastHover = null;
  var drag = null;                        // 拖动状态
  var onDragEnd = null;
  var onPileClick = null;
  var onPoleClick = null;
  var cfg = null;
  var fieldMetric = "temp";
  var lastHeatmap = null;
  var probesVisible = true;
  var homeCamera = null;
  var grainTextureCache = null;
  var grainFieldTextureCache = null;
  var wallTextureCache = null;
  var floorTextureCache = null;
  /* 纯 Three.js 结构模式：仓房、粮堆、热场、探杆均为可旋转实体。 */
  var photoMode = false;

  function grainTexture() {
    if (grainTextureCache) return grainTextureCache;
    var canvas = document.createElement("canvas");
    canvas.width = 256; canvas.height = 256;
    var ctx = canvas.getContext("2d");
    var base = ctx.createLinearGradient(0, 0, 256, 256);
    base.addColorStop(0, "#d4ad69");
    base.addColorStop(1, "#ae7e3f");
    ctx.fillStyle = base;
    ctx.fillRect(0, 0, 256, 256);
    /* 确定性细颗粒纹理：只表达粮粒质感，不制造任何测量数据。 */
    var seed = 0x61c88647;
    var next = function () {
      seed = (seed * 1664525 + 1013904223) >>> 0;
      return seed / 4294967296;
    };
    for (var i = 0; i < 4200; i++) {
      var x = next() * 256, y = next() * 256;
      var r = 0.45 + next() * 1.8;
      var a = 0.22 + next() * 0.30;
      ctx.fillStyle = (next() > 0.52 ? "rgba(255,232,171," : "rgba(91,54,21,") + a.toFixed(3) + ")";
      ctx.beginPath();
      ctx.ellipse(x, y, r * 1.45, r, next() * Math.PI, 0, Math.PI * 2);
      ctx.fill();
    }
    grainTextureCache = new THREE.CanvasTexture(canvas);
    grainTextureCache.wrapS = THREE.RepeatWrapping;
    grainTextureCache.wrapT = THREE.RepeatWrapping;
    grainTextureCache.repeat.set(3.8, 2.8);
    grainTextureCache.needsUpdate = true;
    return grainTextureCache;
  }

  function grainReliefTexture() {
    if (grainFieldTextureCache) return grainFieldTextureCache;
    var canvas = document.createElement("canvas");
    canvas.width = 256; canvas.height = 256;
    var ctx = canvas.getContext("2d");
    ctx.fillStyle = "#f5f5f5";
    ctx.fillRect(0, 0, 256, 256);
    var seed = 0x243f6a88;
    var next = function () {
      seed = (seed * 1664525 + 1013904223) >>> 0;
      return seed / 4294967296;
    };
    for (var i = 0; i < 3200; i++) {
      var x = next() * 256, y = next() * 256;
      var r = 0.5 + next() * 1.5;
      ctx.fillStyle = (next() > 0.5 ? "rgba(255,255,255," : "rgba(40,32,20,") +
        (0.025 + next() * 0.085).toFixed(3) + ")";
      ctx.beginPath();
      ctx.ellipse(x, y, r * 1.7, r, next() * Math.PI, 0, Math.PI * 2);
      ctx.fill();
    }
    grainFieldTextureCache = new THREE.CanvasTexture(canvas);
    grainFieldTextureCache.wrapS = THREE.RepeatWrapping;
    grainFieldTextureCache.wrapT = THREE.RepeatWrapping;
    grainFieldTextureCache.needsUpdate = true;
    return grainFieldTextureCache;
  }

  function warehouseWallTexture() {
    if (wallTextureCache) return wallTextureCache;
    var canvas = document.createElement("canvas");
    canvas.width = 512; canvas.height = 512;
    var ctx = canvas.getContext("2d");
    var base = ctx.createLinearGradient(0, 0, 0, 512);
    base.addColorStop(0, "#687781");
    base.addColorStop(0.48, "#52636d");
    base.addColorStop(1, "#344550");
    ctx.fillStyle = base;
    ctx.fillRect(0, 0, 512, 512);
    /* 细窄压型肋 + 横向拼板缝：只提供金属材质，不引入场景数据。 */
    for (var x = 0; x < 512; x += 6) {
      ctx.fillStyle = "rgba(220,232,239,.11)";
      ctx.fillRect(x, 0, 1, 512);
      ctx.fillStyle = "rgba(10,20,28,.18)";
      ctx.fillRect(x + 3, 0, 1, 512);
    }
    for (var y = 0; y < 512; y += 128) {
      ctx.fillStyle = "rgba(8,18,26,.26)";
      ctx.fillRect(0, y, 512, 2);
      ctx.fillStyle = "rgba(213,227,235,.08)";
      ctx.fillRect(0, y + 2, 512, 1);
    }
    wallTextureCache = new THREE.CanvasTexture(canvas);
    wallTextureCache.wrapS = THREE.RepeatWrapping;
    wallTextureCache.wrapT = THREE.RepeatWrapping;
    wallTextureCache.needsUpdate = true;
    return wallTextureCache;
  }

  function concreteTexture() {
    if (floorTextureCache) return floorTextureCache;
    var canvas = document.createElement("canvas");
    canvas.width = 512; canvas.height = 512;
    var ctx = canvas.getContext("2d");
    var base = ctx.createLinearGradient(0, 0, 512, 512);
    base.addColorStop(0, "#727c81");
    base.addColorStop(1, "#515c63");
    ctx.fillStyle = base;
    ctx.fillRect(0, 0, 512, 512);
    var seed = 0x9e3779b9;
    var next = function () {
      seed = (seed * 1664525 + 1013904223) >>> 0;
      return seed / 4294967296;
    };
    for (var i = 0; i < 2600; i++) {
      var shade = next() > 0.5 ? "rgba(224,231,232," : "rgba(21,31,37,";
      ctx.fillStyle = shade + (0.025 + next() * 0.07).toFixed(3) + ")";
      ctx.fillRect(next() * 512, next() * 512, 1 + next() * 4, 1 + next() * 3);
    }
    floorTextureCache = new THREE.CanvasTexture(canvas);
    floorTextureCache.wrapS = THREE.RepeatWrapping;
    floorTextureCache.wrapT = THREE.RepeatWrapping;
    floorTextureCache.needsUpdate = true;
    return floorTextureCache;
  }

  function fieldTempColor(t, lo, hi) {
    if (t === null || t === undefined || !isFinite(t)) return "#5b6b8c";
    var span = Math.max(hi - lo, 1.0);
    var p = Math.max(0, Math.min(1, (t - lo) / span));
    /* 参考图的色带：蓝→青→绿→黄→橙→红。
       网格顶点仍来自离散 IDW 采样，但显示颜色在相邻色带之间连续插值，
       避免透明剖面把三角形边界放大成“锯齿”。 */
    var stops = ["#2146c7", "#2b72e4", "#23a9ee", "#1bd0d0",
      "#18d8a0", "#20dc32", "#7ce519", "#b8ed1a", "#f1df24",
      "#ffc72e", "#ff8a23", "#f2451f", "#d71920"];
    var scaled = p * (stops.length - 1);
    var idx = Math.min(stops.length - 2, Math.floor(scaled));
    var f = scaled - idx;
    var a = stops[idx].slice(1), b = stops[idx + 1].slice(1);
    var ar = parseInt(a.slice(0, 2), 16), ag = parseInt(a.slice(2, 4), 16), ab = parseInt(a.slice(4, 6), 16);
    var br = parseInt(b.slice(0, 2), 16), bg = parseInt(b.slice(2, 4), 16), bb = parseInt(b.slice(4, 6), 16);
    var r = Math.round(ar + (br - ar) * f).toString(16).padStart(2, "0");
    var g = Math.round(ag + (bg - ag) * f).toString(16).padStart(2, "0");
    var bl = Math.round(ab + (bb - ab) * f).toString(16).padStart(2, "0");
    return "#" + r + g + bl;
  }

  /* ---------- 容器尺寸（布局为准，兜底视口） ---------- */
  function box() {
    var el = container;
    var r = el.getBoundingClientRect();
    if ((r.width < 10 || r.height < 10) && el.parentElement) {
      var pr = el.parentElement.getBoundingClientRect();
      return { w: pr.width, h: pr.height };
    }
    return {
      w: r.width || container.clientWidth || window.innerWidth,
      h: r.height || container.clientHeight || window.innerHeight,
    };
  }

  function fit() {
    var b = box();
    if (b.w < 10 || b.h < 10) return;
    camera.aspect = b.w / b.h;
    camera.updateProjectionMatrix();
    renderer.setSize(b.w, b.h);
  }

  /* ---------- 文字精灵 ---------- */
  function makeTextSprite(text, color) {
    var canvas = document.createElement("canvas");
    canvas.width = 256; canvas.height = 64;
    var ctx = canvas.getContext("2d");
    ctx.font = "bold 34px sans-serif";
    ctx.fillStyle = color || "#8aa2c9";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText(text, 128, 32);
    var tex = new THREE.CanvasTexture(canvas);
    var mat = new THREE.SpriteMaterial({ map: tex, transparent: true, depthTest: false });
    var sp = new THREE.Sprite(mat);
    sp.scale.set(2.2, 0.55, 1);
    return sp;
  }

  function makeLabel(pos, text, color) {
    if (photoMode) return;
    var sp = makeTextSprite(text, color);
    sp.position.copy(pos);
    scene.add(sp);
  }

  /* ---------- 场景坐标换算：米 -> three 坐标（粮仓原点居中） ---------- */
  function sx(x) { return x - WH.L / 2; }
  function sz(z) { return z - WH.W / 2; }
  function unX(v) { return v + WH.L / 2; }
  function unZ(v) { return v + WH.W / 2; }

  /* ---------- 粮堆几何（与后端 _heap_height 同公式） ---------- */
  /* heap: {x0,z0,l,w,h,s}  x/z 为绝对坐标（米） */
  function heapHeight(h, x, z) {
    if (!h || !h.h || h.h <= 0) return 0;
    var l = h.l, w = h.w, hh = h.h, s = h.s || 0.2;
    var nx = Math.abs(x - (h.x0 + l / 2)) / (l / 2);
    var nz = Math.abs(z - (h.z0 + w / 2)) / (w / 2);
    var r = Math.max(nx, nz);
    if (r >= 1) return 0;
    if (r <= s) return hh;
    return hh * (1 - r) / (1 - s);
  }

  /* 杆所属粮堆：按 pile_id 找；找不到回退第一个粮堆；都没有 -> null（地面模式） */
  function heapOf(p) {
    if (p.pile_id !== null && p.pile_id !== undefined) {
      for (var i = 0; i < pileList.length; i++) {
        if (pileList[i].id === p.pile_id) return pileList[i];
      }
    }
    return pileList.length ? pileList[0] : null;
  }

  /* ---------- 节点在线判定 ---------- */
  function nodeOnline(p, addr) {
    if (!p.node_states || !p.node_states.length) return p.status === 1;
    var st = null;
    for (var i = 0; i < p.node_states.length; i++) {
      if (p.node_states[i].addr === addr) { st = p.node_states[i]; break; }
    }
    return st ? st.online : true;
  }

  /* ---------- 节点/杆的世界 Y 坐标（不插地） ----------
     粮堆模式：最浅节点（addr=1）在粮面下 NODE_TOP_OFF，向下逐节点间距排列；
               若节点深度超出粮面高度，最低 clamp 到 0.15m（绝不插入地下）。
     地面模式（无粮堆）：杆从地面立起，杆顶高出地面 POLE_ABOVE，节点全在地面以上。 */
  function nodeY(p, addr, spacing) {
    var h = heapOf(p);
    if (h) {
      var top = Math.max(heapHeight(h, p.x, p.z) - NODE_TOP_OFF, 0.2);
      return Math.max(top - (addr - 1) * spacing, 0.15);
    }
    var n = (p.snapshot && p.snapshot.nodes.length) || 1;
    return POLE_ABOVE + (n - addr) * spacing + 0.15;   /* addr=1 最高（贴近粮面位置） */
  }
  function poleBottom(p) {
    var h = heapOf(p);
    var n = (p.snapshot && p.snapshot.nodes.length) || 1;
    var sp = p.spacing_m || 0.5;
    var b;
    if (h) {
      var top = Math.max(heapHeight(h, p.x, p.z) - NODE_TOP_OFF, 0.2);
      b = top - (n - 1) * sp - 0.15;
    } else {
      b = POLE_ABOVE + 0.15;                            /* 地面模式：杆立在 0 以上 */
    }
    return Math.max(b, 0.0);
  }
  function poleTop(p) {
    var h = heapOf(p);
    if (h) return heapHeight(h, p.x, p.z) + POLE_ABOVE;
    var n = (p.snapshot && p.snapshot.nodes.length) || 1;
    return POLE_ABOVE + (n - 1) * (p.spacing_m || 0.5) + POLE_ABOVE * 2;
  }

  /* ---------- 初始化 ---------- */
  function init(containerId) {
    container = document.getElementById(containerId);
    tooltip = document.getElementById("tooltip");
    if (!container) return;

    scene = new THREE.Scene();
    scene.background = null;

    camera = new THREE.PerspectiveCamera(48, 1.6, 0.1, 200);
    /* 主视点从敞开的仓门向仓内看；OrbitControls 可绕粮堆检查不同侧面。 */
    camera.position.set(WH.L * 0.82, Math.max(3.1, WH.H * 0.62), WH.W * 0.25);
    camera.lookAt(0, Math.max(1.1, WH.H * 0.30), 0);

    renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
    renderer.setClearColor(0x000000, 0);
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    container.appendChild(renderer.domElement);
    fit();

    /* OrbitControls：鼠标左键旋转 / 右键平移 / 滚轮缩放；
       命中探杆时临时禁用（见 onPointerDown/onPointerUp），拖杆不旋转 */
    controls = new THREE.OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    controls.dampingFactor = 0.08;
    controls.minDistance = 6;
    controls.maxDistance = 60;
    controls.maxPolarAngle = Math.PI * 0.49;
    homeCamera = { position: camera.position.clone(), target: controls.target.clone() };

    scene.add(new THREE.AmbientLight(0xffffff, 0.75));
    var dir = new THREE.DirectionalLight(0xffffff, 0.85);
    dir.position.set(12, 22, 10);
    scene.add(dir);

    poleGroup = new THREE.Group();
    /* 热场是解释层；实体探杆必须始终在解释层上方可见。 */
    poleGroup.renderOrder = 30;
    scene.add(poleGroup);
    pileGroup = new THREE.Group();
    scene.add(pileGroup);
    heatGroup = new THREE.Group();
    heatGroup.name = "thermal-field";
    heatGroup.renderOrder = 1;
    scene.add(heatGroup);
    probeGroup = new THREE.Group();
    probeGroup.name = "measured-probes";
    probeGroup.renderOrder = 50;
    scene.add(probeGroup);

    raycaster = new THREE.Raycaster();
    mouse = new THREE.Vector2();
    renderer.domElement.addEventListener("pointermove", onPointerMove);
    renderer.domElement.addEventListener("pointerdown", onPointerDown);
    renderer.domElement.addEventListener("pointerup", onPointerUp);
    window.addEventListener("resize", onResize);

    buildScaleBar();

    animate();
  }

  /* ---------- 右下角温度色标条（参考图风格：渐变条 + 端点温度） ---------- */
  function buildScaleBar() {
    var host = container.parentElement || container;
    if (!host) return;
    var wrap = document.createElement("div");
    wrap.style.cssText =
      "position:absolute;right:14px;bottom:14px;z-index:12;user-select:none;" +
      "background:rgba(11,18,32,.85);border:1px solid rgba(120,150,200,.25);" +
      "border-radius:10px;padding:9px 12px;font-family:'Segoe UI','Microsoft YaHei',sans-serif;";
    wrap.innerHTML =
      '<div style="font-size:11px;letter-spacing:1px;color:#8aa2c9;font-weight:600;margin-bottom:6px">' +
      "温度 · ℃</div>" +
      '<div style="height:9px;border-radius:5px;width:150px;' +
      'background:linear-gradient(to right,#2b6bf3,#33b7f0,#37d3a0,#f2d34e,#f2943e,#e23c3c)"></div>' +
      '<div style="display:flex;justify-content:space-between;margin-top:4px;' +
      'font-size:11px;color:#8aa2c9">' +
      '<span>18</span><span>32</span></div>';
    host.appendChild(wrap);
    scaleEl = wrap;
  }

  /* ---------- 可旋转的仓房实体模型 ---------- */
  function rebuildStatic() {
    if (staticGroup) { scene.remove(staticGroup); }
    staticGroup = new THREE.Group();
    var floorMat = new THREE.MeshPhongMaterial({
      color: 0xffffff, map: concreteTexture(), specular: 0x343b40, shininess: 7 });
    var wallMat = new THREE.MeshPhongMaterial({
      color: 0xc6d1d7, map: warehouseWallTexture(), specular: 0x596871, shininess: 18 });
    var steelMat = new THREE.MeshPhongMaterial({ color: 0x526879, shininess: 70 });
    var glassMat = new THREE.MeshPhongMaterial({ color: 0x8fc6dc,
      emissive: 0x183b4b, transparent: true, opacity: 0.64, shininess: 90 });
    var lampMat = new THREE.MeshPhongMaterial({ color: 0xfff1c2,
      emissive: 0xffbf55, emissiveIntensity: 0.55 });
    var addBox = function (w, h, d, x, y, z, mat, bevel) {
      var geo = new THREE.BoxGeometry(w, h, d);
      var m = new THREE.Mesh(geo, mat);
      m.position.set(x, y, z);
      staticGroup.add(m);
      if (bevel) {
        var e = new THREE.LineSegments(new THREE.EdgesGeometry(geo),
          new THREE.LineBasicMaterial({ color: 0x8ea2b2, transparent: true, opacity: 0.18 }));
        e.position.copy(m.position);
        staticGroup.add(e);
      }
      return m;
    };

    /* 由短端敞口进入粮仓；两侧长墙、远端墙和地面均为可旋转实体。 */
    addBox(WH.L + 4, 0.16, WH.W + 4, 0, -0.10, 0, floorMat, false);
    addBox(WH.L, 1.35, 0.22, 0, 0.68, -WH.W / 2, wallMat, true);
    addBox(WH.L, 1.35, 0.22, 0, 0.68, WH.W / 2, wallMat, true);
    addBox(0.22, 1.35, WH.W, -WH.L / 2, 0.68, 0, wallMat, true);
    addBox(WH.L, WH.H - 1.35, 0.18, 0, (WH.H + 1.35) / 2, -WH.W / 2, wallMat, false);
    addBox(WH.L, WH.H - 1.35, 0.18, 0, (WH.H + 1.35) / 2, WH.W / 2, wallMat, false);
    addBox(0.18, WH.H - 1.35, WH.W, -WH.L / 2, (WH.H + 1.35) / 2, 0, wallMat, false);

    /* 两侧长墙的采光窗与窗框：连续排列时自然体现粮仓纵深。 */
    var addSideWindow = function (centerX, sideZ) {
      var paneZ = sideZ - Math.sign(sideZ) * 0.12;
      var frameZ = paneZ - Math.sign(sideZ) * 0.035;
      var pane = addBox(1.36, 1.42, 0.045, centerX, 4.15, paneZ, glassMat, false);
      pane.name = "warehouse-side-window";
      addBox(0.045, 1.48, 0.08, centerX - 0.68, 4.15, frameZ, steelMat, false);
      addBox(0.045, 1.48, 0.08, centerX + 0.68, 4.15, frameZ, steelMat, false);
      addBox(1.40, 0.045, 0.08, centerX, 3.44, frameZ, steelMat, false);
      addBox(1.40, 0.045, 0.08, centerX, 4.86, frameZ, steelMat, false);
      addBox(0.035, 1.40, 0.085, centerX, 4.15, frameZ, steelMat, false);
      addBox(1.38, 0.035, 0.085, centerX, 4.15, frameZ, steelMat, false);
    };
    for (var bwi = -4; bwi <= 4; bwi++) {
      var bwx = bwi * WH.L / 9;
      addSideWindow(bwx, WH.W / 2);
      addSideWindow(bwx, -WH.W / 2);
    }

    /* 远端墙采光窗作为空间消失点；前端保持敞开，露出粮堆和探杆。 */
    for (var ewi = -1; ewi <= 1; ewi++) {
      var ewz = ewi * WH.W / 4;
      var endPane = addBox(0.045, 1.30, 1.28, -WH.L / 2 + 0.12, 4.1, ewz, glassMat, false);
      endPane.name = "warehouse-back-window";
      addBox(0.08, 1.36, 0.045, -WH.L / 2 + 0.15, 4.1, ewz - 0.64, steelMat, false);
      addBox(0.08, 1.36, 0.045, -WH.L / 2 + 0.15, 4.1, ewz + 0.64, steelMat, false);
      addBox(0.08, 0.045, 1.32, -WH.L / 2 + 0.15, 3.45, ewz, steelMat, false);
      addBox(0.08, 0.045, 1.32, -WH.L / 2 + 0.15, 4.75, ewz, steelMat, false);
    }

    /* 立柱 + 屋架：用规则钢梁构造出真实仓房透视。 */
    for (var ci = -2; ci <= 2; ci++) {
      var cx = ci * WH.L * 0.20;
      addBox(0.16, WH.H - 1.1, 0.16, cx, (WH.H - 1.1) / 2 + 1.1,
        -WH.W / 2 + 0.18, steelMat, false);
      addBox(0.16, WH.H - 1.1, 0.16, cx, (WH.H - 1.1) / 2 + 1.1,
        WH.W / 2 - 0.18, steelMat, false);
      addBox(0.12, 0.12, WH.W - 0.35, cx, WH.H - 0.55, 0, steelMat, false);
    }
    for (var ri = -2; ri <= 2; ri++) {
      var rz = ri * 1.75;
      addBox(WH.L - 0.5, 0.10, 0.10, 0, WH.H - 0.46, rz, steelMat, false);
    }

    /* 半透双坡屋面补全仓房体积；保留敞开的前端和室内视线。 */
    var roofMat = new THREE.MeshPhongMaterial({
      color: 0x8a969d, map: warehouseWallTexture(), emissive: 0x101820, specular: 0x52616a,
      shininess: 22, transparent: true, opacity: 0.38,
      side: THREE.DoubleSide, depthWrite: false,
    });
    var addRoofPanel = function (z0, y0, z1, y1) {
      var geo = new THREE.BufferGeometry();
      geo.setAttribute("position", new THREE.Float32BufferAttribute([
        -WH.L / 2, y0, sz(z0), WH.L / 2, y0, sz(z0),
        WH.L / 2, y1, sz(z1), -WH.L / 2, y1, sz(z1),
      ], 3));
      geo.setAttribute("uv", new THREE.Float32BufferAttribute([0, 0, 1, 0, 1, 1, 0, 1], 2));
      geo.setIndex([0, 1, 2, 0, 2, 3]);
      geo.computeVertexNormals();
      var panel = new THREE.Mesh(geo, roofMat);
      panel.name = "warehouse-gable-roof";
      panel.renderOrder = -2;
      staticGroup.add(panel);
    };
    var eaveY = WH.H - 0.82;
    var ridgeY = WH.H - 0.10;
    addRoofPanel(-WH.W / 2, eaveY, 0, ridgeY);
    addRoofPanel(0, ridgeY, WH.W / 2, eaveY);

    var addRoofBeam = function (a, b, radius) {
      var from = new THREE.Vector3(a[0], a[1], a[2]);
      var to = new THREE.Vector3(b[0], b[1], b[2]);
      var delta = new THREE.Vector3().subVectors(to, from);
      var beam = new THREE.Mesh(
        new THREE.CylinderGeometry(radius, radius, delta.length(), 8), steelMat);
      beam.position.copy(from).add(to).multiplyScalar(0.5);
      beam.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), delta.normalize());
      staticGroup.add(beam);
    };
    for (var frame = 0; frame <= 4; frame++) {
      var fx = -WH.L / 2 + WH.L * frame / 4;
      addRoofBeam([fx, eaveY, -WH.W / 2], [fx, ridgeY, 0], 0.055);
      addRoofBeam([fx, ridgeY, 0], [fx, eaveY, WH.W / 2], 0.055);
      addRoofBeam([fx, eaveY, -WH.W / 2], [fx, eaveY, WH.W / 2], 0.042);
    }

    /* 顶灯发光体 + 点光源，让粮堆坡面产生真实明暗。 */
    for (var li = -2; li <= 2; li++) {
      var lx = li * 3.5;
      addBox(0.82, 0.08, 0.28, lx, WH.H - 0.52, 0, lampMat, false);
      var point = new THREE.PointLight(0xffd88a, 0.45, 10, 2);
      point.position.set(lx, WH.H - 0.35, 0);
      staticGroup.add(point);
    }

    var grid = new THREE.GridHelper(Math.max(WH.L, WH.W) + 4, 20, 0x6f8493, 0x3b4b57);
    var gridMaterials = Array.isArray(grid.material) ? grid.material : [grid.material];
    gridMaterials.forEach(function (material) {
      material.transparent = true;
      material.opacity = 0.06;
      material.depthWrite = false;
    });
    grid.position.y = 0.006;
    staticGroup.add(grid);
    staticGroup.visible = true;
    scene.add(staticGroup);
  }

  /* ---------- 多粮堆实体曲面（与后端 heap_geom 同公式） ---------- */
  function rebuildPiles() {
    if (pileGroup) { scene.remove(pileGroup); }
    pileGroup = new THREE.Group();
    pileMeshes = {};
    for (var i = 0; i < pileList.length; i++) {
      var pl = pileList[i];
      if (!pl.volume || pl.volume <= 0 || !pl.height || pl.height < 0.05) continue;
      var l = pl.l, w = pl.w, h = pl.height;
      var x0 = pl.x - l / 2, z0 = pl.z - w / 2;
      /* 连续细分粮面：每个格点直接使用与后端相同的 heapHeight，
         让坡面由真实几何承载，而不是一张低多边形锥台。 */
      var nx = Math.max(16, Math.min(36, Math.round(l * 2.4)));
      var nz = Math.max(12, Math.min(28, Math.round(w * 2.4)));
      if (nz % 2) nz += 1;
      var v = [], uv = [], idx = [];
      var verts = [];
      for (var gx = 0; gx <= nx; gx++) {
        verts[gx] = [];
        var xx = x0 + l * gx / nx;
        for (var gz = 0; gz <= nz; gz++) {
          var zz = z0 + w * gz / nz;
          var yy = heapHeight({ x0: x0, z0: z0, l: l, w: w, h: h, s: HEAP_S }, xx, zz);
          verts[gx][gz] = v.length / 3;
          v.push(sx(xx), yy, sz(zz));
          uv.push(gx / nx * 4.0, gz / nz * 3.0);
        }
      }
      for (var ix = 0; ix < nx; ix++) {
        for (var iz = 0; iz < nz; iz++) {
          var a0 = verts[ix][iz], a1 = verts[ix + 1][iz];
          var a2 = verts[ix + 1][iz + 1], a3 = verts[ix][iz + 1];
          var y0 = v[a0 * 3 + 1], y1 = v[a1 * 3 + 1];
          var y2 = v[a2 * 3 + 1], y3 = v[a3 * 3 + 1];
          /* 只丢弃完全在底面外的格子；边缘格子保留，形成连续坡脚。 */
          if (Math.max(y0, y1, y2, y3) <= 0.01) continue;
          idx.push(a0, a1, a3, a1, a2, a3);
        }
      }
      var geo = new THREE.BufferGeometry();
      geo.setAttribute("position", new THREE.Float32BufferAttribute(v, 3));
      geo.setAttribute("uv", new THREE.Float32BufferAttribute(uv, 2));
      geo.setIndex(idx);
      geo.computeVertexNormals();
      /* 粮堆始终是完整实体；热场作为半透明解释层覆盖其连续表面。 */
      var mat = new THREE.MeshPhongMaterial({
        color: 0xb78648, map: grainTexture(), transparent: true, opacity: 0.98,
        shininess: 12, side: THREE.DoubleSide, depthWrite: true,
        polygonOffset: true, polygonOffsetFactor: 1, polygonOffsetUnits: 1 });
      var mesh = new THREE.Mesh(geo, mat);
      mesh.renderOrder = -1;
      mesh.userData = { pileId: pl.id, name: pl.name, pile: true };
      var line = new THREE.LineSegments(new THREE.EdgesGeometry(geo),
        new THREE.LineBasicMaterial({ color: 0x8e6a3d, transparent: true, opacity: 0.12 }));
      line.userData = { pileId: pl.id, name: pl.name, pile: true };

      var g = new THREE.Group();
      g.add(mesh);
      g.add(line);
      g.userData = { pileId: pl.id };
      pileGroup.add(g);
      pileMeshes[pl.id] = { group: g, mesh: mesh, line: line, pile: pl };
    }
    /* 粮堆实体始终保留；有效热场只对当前粮堆打开剖切窗口。 */
    pileGroup.visible = true;
    scene.add(pileGroup);
    applyPileSelect();
  }

  function applyPileSelect() {
    for (var id in pileMeshes) {
      var pm = pileMeshes[id];
      var sel = (String(id) === String(selectedPile));
      pm.mesh.material.opacity = sel ? 0.97 : 0.93;
      pm.mesh.material.color.setHex(sel ? 0xc69656 : 0xa9743b);
      pm.line.material.color.setHex(sel ? 0xf4d08a : 0x8e6a3d);
      pm.line.material.opacity = sel ? 0.22 : 0.10;
    }
  }

  /* ---------- 渲染一组杆（参考图风格：金属杆身 + 圆头 + 节点球串杆） ---------- */
  function buildPole(p) {
    var g = new THREE.Group();
    var uid = p.uid, snap = p.snapshot || null;
    var online = p.status === 1 && snap;
    var sel = uid === selectedPole;
    var rodColor = sel ? 0x9ecbff : (online ? 0xe3edf8 : 0x7285a3);

    var nodes = snap ? snap.nodes : [];
    var spacing = p.spacing_m || 0.5;

    /* 杆身：从杆底到粮面上方（上细下粗 + 金属高光） */
    var bY = poleBottom(p);
    var tY = poleTop(p);
    var len = Math.max(tY - bY, 0.6);
    var cylGeo = new THREE.CylinderGeometry(0.095, 0.125, len, 12);
    var cylMat = new THREE.MeshPhongMaterial({
      color: rodColor,
      specular: sel ? 0x88bbff : 0x77889a,
      shininess: 55,
      transparent: true, opacity: photoMode ? (sel ? 0.78 : 0.62) : (sel ? 1 : 0.95),
      depthTest: false, depthWrite: false });
    var cyl = new THREE.Mesh(cylGeo, cylMat);
    cyl.position.set(sx(p.x), bY + len / 2, sz(p.z));
    cyl.userData = { uid: uid, name: p.name, pole: true };
    g.add(cyl);

    /* 顶部圆头（参考图风格） */
    var cap = new THREE.Mesh(new THREE.SphereGeometry(0.09, 10, 8), cylMat);
    cap.position.set(sx(p.x), tY, sz(p.z));
    cap.userData = { uid: uid, name: p.name, pole: true };
    g.add(cap);

    /* 节点球：嵌在杆身上，颜色 = 温度色，发光 0.18（参考图） */
    var nodeMeshes = [];
    for (var i = 0; i < nodes.length; i++) {
      var nd = nodes[i];
      var addr = nd.addr || (i + 1);
      var y = nodeY(p, addr, spacing);
      var lost = !nodeOnline(p, addr);
      var t = nd.temp;
      var hex;
      if (!online || lost) hex = 0x4a5a75;
      else hex = parseInt(tempColor(t).slice(1), 16);
      var spGeo = new THREE.SphereGeometry(NODE_R, 18, 14);
      var spMat = new THREE.MeshPhongMaterial({
        color: hex,
        emissive: new THREE.Color(hex).multiplyScalar(online && !lost ? 0.18 : 0),
        specular: 0x333333, shininess: 30,
        transparent: true, opacity: online && !lost ? 1 : 0.55,
        depthTest: false, depthWrite: false });
      var m = new THREE.Mesh(spGeo, spMat);
      m.position.set(sx(p.x), y, sz(p.z));
      m.userData = {
        uid: uid, name: p.name, addr: addr,
        temp: t, rh: nd.rh, alarm: online && snap.alarm === 1, lost: lost
      };
      g.add(m);
      nodeMeshes.push(m);
    }

    /* 杆顶名称标签 */
    var top = makeTextSprite(p.name, online ? "#dbe6f8" : "#7d93b8");
    top.position.set(sx(p.x), tY + 0.55, sz(p.z));
    g.add(top);

    g.userData = { uid: uid, nodes: nodeMeshes, nameTag: top, pole: true };
    g.renderOrder = 31;
    for (var ci = 0; ci < g.children.length; ci++) g.children[ci].renderOrder = 31;
    poleMeshes[uid] = g;
    poleData[uid] = p;
    poleGroup.add(g);
    return g;
  }

  /* ---------- 全量重建杆 ---------- */
  function setPoleList(list) {
    for (var uid in poleMeshes) poleGroup.remove(poleMeshes[uid]);
    poleMeshes = {};
    poleData = {};
    updatePoles(list);
  }

  /* ---------- 增量更新 ---------- */
  function updatePoles(list) {
    var seen = {};
    for (var k = 0; k < list.length; k++) {
      var p = list[k];
      seen[p.uid] = true;
      var g = poleMeshes[p.uid];
      if (!g) { buildPole(p); continue; }
      if (needsRebuild(poleData[p.uid], p)) {
        poleGroup.remove(g);
        delete poleMeshes[p.uid];
        buildPole(p);
        continue;
      }
      applyPoleDiff(g, p);
    }
    for (var uid in poleMeshes) {
      if (!seen[uid]) {
        poleGroup.remove(poleMeshes[uid]);
        delete poleMeshes[uid];
        delete poleData[uid];
      }
    }
  }

  function needsRebuild(oldP, newP) {
    if (!oldP) return true;
    var o = oldP.snapshot ? oldP.snapshot.nodes : [];
    var n = newP.snapshot ? newP.snapshot.nodes : [];
    if (o.length !== n.length) return true;
    if (oldP.spacing_m !== newP.spacing_m) return true;
    if (oldP.pile_id !== newP.pile_id) return true;
    if (Math.abs(oldP.x - newP.x) > 1e-6 || Math.abs(oldP.z - newP.z) > 1e-6) return true;
    var set = {};
    for (var i = 0; i < n.length; i++) set[n[i].addr] = 1;
    for (var j = 0; j < o.length; j++) if (!set[o[j].addr]) return true;
    return false;
  }

  function applyPoleDiff(g, p) {
    var online = p.status === 1 && p.snapshot;
    var sel = p.uid === selectedPole;
    poleData[p.uid] = p;
    var nodes = p.snapshot ? p.snapshot.nodes : [];
    for (var i = 0; i < nodes.length; i++) {
      var nd = nodes[i];
      var addr = nd.addr || (i + 1);
      var m = g.userData.nodes[i];
      if (!m) continue;
      var lost = !nodeOnline(p, addr);
      var hex = (!online || lost) ? 0x4a5a75 : parseInt(tempColor(nd.temp).slice(1), 16);
      m.material.color.setHex(hex);
      m.material.emissive.setHex(hex).multiplyScalar(online && !lost ? 0.18 : 0);
      m.material.opacity = online && !lost ? 1 : 0.55;
      m.userData.temp = nd.temp;
      m.userData.rh = nd.rh;
      m.userData.alarm = online && p.snapshot.alarm === 1;
      m.userData.lost = lost;
    }
    /* 杆身 + 顶部圆头共享材质（children[0] 杆身 / children[1] 圆头） */
    var cyl = g.children[0];
    if (cyl && cyl.userData.pole) {
      var c = sel ? 0x9ecbff : (online ? 0xe3edf8 : 0x7285a3);
      cyl.material.color.setHex(c);
      cyl.material.specular.setHex(sel ? 0x88bbff : 0x77889a);
    }
  }

  /* ---------- 全局配置 ---------- */
  function setConfig(c) {
    cfg = c || null;
    if (cfg) {
      WH = { L: cfg.wh_l || 20, W: cfg.wh_w || 10, H: cfg.wh_h || 8 };
    }
    rebuildStatic();
    setPoleList(Object.keys(poleData).map(function (u) { return poleData[u]; }));
  }

  /* ---------- 粮堆列表 ---------- */
  function setPileList(list) {
    pileList = list || [];
    rebuildPiles();
    setPoleList(Object.keys(poleData).map(function (u) { return poleData[u]; }));
  }

  function setSelectedPile(id) {
    selectedPile = id;
    applyPileSelect();
  }

  function setSelectedPole(uid) {
    selectedPole = uid;
    for (var u in poleMeshes) applyPoleDiff(poleMeshes[u], poleData[u]);
  }

  function clearHeatField() {
    if (heatGroup) {
      while (heatGroup.children.length) heatGroup.remove(heatGroup.children[0]);
    }
    cloud = null;
    voxelField = null;
    cloudPhase = [];
    flowTracers = [];
    if (probeGroup) {
      while (probeGroup.children.length) probeGroup.remove(probeGroup.children[0]);
    }
  }

  function heatPoint(heap, point) {
    return new THREE.Vector3(
      sx(point[0]),
      heapHeight(heap, point[0], point[1]) - point[2] + 0.035,
      sz(point[1])
    );
  }

  function makeFlowLine(heap, line, colorFn) {
    if (!line || line.length < 2) return;
    var positions = new Float32Array(line.length * 3);
    var colors = new Float32Array(line.length * 3);
    for (var i = 0; i < line.length; i++) {
      var p = heatPoint(heap, line[i]);
      var c = (colorFn || tempColor)(line[i][3]);
      positions[i * 3] = p.x;
      positions[i * 3 + 1] = p.y;
      positions[i * 3 + 2] = p.z;
      colors[i * 3] = parseInt(c.slice(1, 3), 16) / 255;
      colors[i * 3 + 1] = parseInt(c.slice(3, 5), 16) / 255;
      colors[i * 3 + 2] = parseInt(c.slice(5, 7), 16) / 255;
    }
    var geo = new THREE.BufferGeometry();
    geo.setAttribute("position", new THREE.BufferAttribute(positions, 3));
    geo.setAttribute("color", new THREE.BufferAttribute(colors, 3));
    var mat = new THREE.LineBasicMaterial({
      vertexColors: THREE.VertexColors,
      transparent: true,
      opacity: 0.06,
      depthWrite: false,
      depthTest: false,
    });
    var path = new THREE.Line(geo, mat);
    path.renderOrder = 3;
    heatGroup.add(path);

    var start = line[0];
    for (var k = 0; k < 3; k++) {
      var tracerMat = new THREE.MeshBasicMaterial({
        color: parseInt((colorFn || tempColor)(start[3]).slice(1), 16),
        transparent: true,
        opacity: 0.025,
        depthTest: false,
        blending: THREE.AdditiveBlending,
      });
      var tracer = new THREE.Mesh(new THREE.SphereGeometry(0.07, 8, 6), tracerMat);
      tracer.renderOrder = 4;
      heatGroup.add(tracer);
      flowTracers.push({
        path: line,
        mesh: tracer,
        speed: 0.035 + (line.length % 5) * 0.006,
        phase: ((line[0][0] * 0.13 + line[0][1] * 0.07) + k / 3) % 1,
        heap: heap,
      });
    }
  }

  function buildMeasuredProbes(heap, probes, colorFn) {
    if (!probeGroup || !heap || !probes) return;
    var rods = {};
    probes.forEach(function (s) {
      if (s.x === undefined || s.z === undefined || s.depth_m === undefined) return;
      var pos = heatPoint(heap, [s.x, s.z, s.depth_m]);
      var measured = fieldMetric === "rh" ? s.rh : s.temp;
      var color = parseInt((colorFn || tempColor)(measured === null || measured === undefined ? 20 : measured).slice(1), 16);
      var ringMat = new THREE.MeshBasicMaterial({
        color: color, transparent: true, opacity: s.online ? 0.95 : 0.35,
        depthTest: false, side: THREE.DoubleSide,
      });
      var ring = new THREE.Mesh(new THREE.TorusGeometry(0.18, 0.028, 8, 20), ringMat);
      ring.rotation.x = Math.PI / 2;
      ring.position.copy(pos);
      ring.renderOrder = 8;
      ring.userData = { uid: s.uid, addr: s.addr, name: s.name,
        temp: s.temp, rh: s.rh, probe: true, lost: !s.online };
      probeGroup.add(ring);

      var dotMat = new THREE.MeshBasicMaterial({
        color: color, transparent: true, opacity: s.online ? 0.9 : 0.25,
        depthTest: false, blending: THREE.AdditiveBlending,
      });
      var dot = new THREE.Mesh(new THREE.SphereGeometry(0.075, 10, 8), dotMat);
      dot.position.copy(pos);
      dot.renderOrder = 9;
      dot.userData = ring.userData;
      probeGroup.add(dot);

      /* 实测节点之间的探杆实体：这是同一根物理探杆的可视化，
         不是新增传感器数据；始终压在热场解释层之上。 */
      var key = String(s.uid || s.addr || (s.x + "|" + s.z));
      if (!rods[key]) rods[key] = { x: Number(s.x), z: Number(s.z), depth: 0 };
      rods[key].depth = Math.max(rods[key].depth, Number(s.depth_m) || 0);
    });
    Object.keys(rods).forEach(function (key) {
      var r = rods[key];
      if (!(r.depth > 0)) return;
      var top = heapHeight(heap, r.x, r.z) + 0.08;
      var bottom = heapHeight(heap, r.x, r.z) - r.depth + 0.035;
      var len = Math.max(top - bottom, 0.18);
      var rod = new THREE.Mesh(
        new THREE.CylinderGeometry(0.085, 0.10, len, 12),
        new THREE.MeshBasicMaterial({ color: 0xe6eef8, transparent: true,
          opacity: 0.60, depthTest: false, depthWrite: false })
      );
      rod.position.set(sx(r.x), bottom + len / 2, sz(r.z));
      rod.renderOrder = 50;
      rod.userData = { probeRod: true, uid: key };
      probeGroup.add(rod);
    });
  }

  function addFieldTriangle(heap, a, b, c, positions, colors, uvs, colorFn) {
    var tri = [a, b, c];
    for (var i = 0; i < tri.length; i++) {
      var q = tri[i];
      var v = heatPoint(heap, [q.x, q.z, q.d]);
      var col = new THREE.Color((colorFn || tempColor)(q.t));
      positions.push(v.x, v.y, v.z);
      colors.push(col.r, col.g, col.b);
      uvs.push((q.x - heap.x0) / heap.l * 4, (q.z - heap.z0) / heap.w * 3);
    }
  }

  function makeFieldSurface(heap, positions, colors, uvs, opacity, name) {
    if (!positions.length) return;
    var geo = new THREE.BufferGeometry();
    geo.setAttribute("position", new THREE.Float32BufferAttribute(positions, 3));
    geo.setAttribute("color", new THREE.Float32BufferAttribute(colors, 3));
    geo.setAttribute("uv", new THREE.Float32BufferAttribute(uvs, 2));
    var mat = new THREE.MeshBasicMaterial({
      map: grainReliefTexture(),
      vertexColors: THREE.VertexColors,
      transparent: true,
      opacity: opacity,
      side: THREE.DoubleSide,
      depthWrite: false,
      depthTest: true,
      polygonOffset: true,
      polygonOffsetFactor: -1,
      polygonOffsetUnits: -1,
      blending: THREE.NormalBlending,
    });
    var mesh = new THREE.Mesh(geo, mat);
    mesh.name = name;
    mesh.renderOrder = 2;
    heatGroup.add(mesh);
  }

  function buildThermalSlices(heap, pts, colorFn) {
    /* 将后端 IDW 网格平滑铺到完整粮堆曲面；不切开或替换粮堆实体。 */
    if (!heap || !pts || pts.length < 4) return;
    var topD = Math.min.apply(null, pts.map(function (p) { return Number(p[2]); }));
    var topSamples = pts.filter(function (p) {
      return Math.abs(Number(p[2]) - topD) < 1e-6 && p[3] !== null && p[3] !== undefined;
    }).map(function (p) {
      return { x: Number(p[0]), z: Number(p[1]), t: Number(p[3]) };
    });
    if (!topSamples.length) return;
    var sampleTop = function (x, z) {
      /* 显示细分只平滑现有 IDW 场，不生成或回写任何传感器数据。 */
      var sum = 0, wsum = 0;
      for (var si = 0; si < topSamples.length; si++) {
        var dx = topSamples[si].x - x, dz = topSamples[si].z - z;
        var wgt = 1 / (dx * dx + dz * dz + 0.55);
        sum += topSamples[si].t * wgt;
        wsum += wgt;
      }
      return wsum ? sum / wsum : null;
    };
    /* 细分密度高于后端格点；温度场跟随粮堆的完整坡面边界。 */
    var topNx = Math.max(28, Math.min(48, Math.round(Number(heap.l) * 2.2)));
    var topNz = Math.max(18, Math.min(36, Math.round(Number(heap.w) * 2.2)));
    var topVerts = [];
    for (var txi = 0; txi <= topNx; txi++) {
      topVerts[txi] = [];
      var tx = Number(heap.x0) + Number(heap.l) * txi / topNx;
      for (var tzi = 0; tzi <= topNz; tzi++) {
        var tz = Number(heap.z0) + Number(heap.w) * tzi / topNz;
        var th = heapHeight(heap, tx, tz);
        topVerts[txi][tzi] = {
          x: tx, z: tz, d: topD,
          t: sampleTop(tx, tz),
          h: th
        };
      }
    }
    var posH = [], colH = [], uvH = [];
    for (var tix = 0; tix < topNx; tix++) {
      for (var tiz = 0; tiz < topNz; tiz++) {
        var ta = topVerts[tix][tiz], tb = topVerts[tix + 1][tiz];
        var tc = topVerts[tix + 1][tiz + 1], td = topVerts[tix][tiz + 1];
        if (ta.h > 0.03 && tb.h > 0.03 && td.h > 0.03) {
          addFieldTriangle(heap, ta, tb, td, posH, colH, uvH, colorFn);
        }
        if (tb.h > 0.03 && tc.h > 0.03 && td.h > 0.03) {
          addFieldTriangle(heap, tb, tc, td, posH, colH, uvH, colorFn);
        }
      }
    }
    makeFieldSurface(heap, posH, colH, uvH, 0.60, "thermal-grain-surface");
  }

  /* ---------- 温度场（连续切片/体积场 + 后端示意流线 + 动态示踪） ---------- */
  function setHeatmap(hm) {
    lastHeatmap = hm || null;
    clearHeatField();
    if (!hm || !hm.points || !hm.points.length || !hm.heap) {
      if (scaleEl) {
        var m0 = scaleEl.querySelector("span:first-child"), m1 = scaleEl.querySelector("span:last-child");
        if (m0) m0.textContent = "18";
        if (m1) m1.textContent = "32";
      }
      return;
    }

    var heap = hm.heap;
    var pts = hm.points;
    var pos = new Float32Array(pts.length * 3);
    var col = new Float32Array(pts.length * 3);
    var viewPoints = pts.map(function (pt) {
      var v = fieldMetric === "rh" ? pt[4] : pt[3];
      return [pt[0], pt[1], pt[2], v === null || v === undefined ? null : v, pt[4]];
    });
    var tmin = Infinity, tmax = -Infinity;
    for (var ti = 0; ti < viewPoints.length; ti++) {
      if (viewPoints[ti][3] !== null && viewPoints[ti][3] !== undefined) {
        tmin = Math.min(tmin, viewPoints[ti][3]);
        tmax = Math.max(tmax, viewPoints[ti][3]);
      }
    }
    if (tmin === Infinity) {
      tmin = fieldMetric === "rh" ? 0 : 18;
      tmax = fieldMetric === "rh" ? 100 : 32;
    }
    /* 现场温度场采用稳定的工程色带范围，避免样本范围窄时整面
       被拉成同一片黄橙色；数值仍使用原始实测/插值结果。 */
    var colorLo = fieldMetric === "rh" ? 0 : 18;
    var colorHi = fieldMetric === "rh" ? 100 : 32;
    var fieldColor = function (value) { return fieldTempColor(value, colorLo, colorHi); };
    for (var i = 0; i < viewPoints.length; i++) {
      var px = viewPoints[i][0], pz = viewPoints[i][1], d = viewPoints[i][2], t = viewPoints[i][3];
      pos[i * 3] = sx(px);
      pos[i * 3 + 1] = heapHeight(heap, px, pz) - d;
      pos[i * 3 + 2] = sz(pz);
      var c = fieldColor(t === null || t === undefined ? 20 : t);
      col[i * 3] = parseInt(c.slice(1, 3), 16) / 255;
      col[i * 3 + 1] = parseInt(c.slice(3, 5), 16) / 255;
      col[i * 3 + 2] = parseInt(c.slice(5, 7), 16) / 255;
      cloudPhase.push((((px * 7.13 + pz * 3.7 + d * 5.9) % 12.56) + 12.56) % 12.56);
    }
    buildThermalSlices(heap, viewPoints, fieldColor);

    /* 体素层保留为很弱的体积纹理；连续切片是主视觉，避免小方块主导。 */
    if (THREE.InstancedMesh && THREE.BoxGeometry) {
      var side = Math.max(0.22, Math.min(0.62, (hm.step || 0.5) * 0.92));
      var cube = new THREE.BoxGeometry(side, side, side);
      var cubeMat = new THREE.MeshBasicMaterial({
        vertexColors: true, transparent: true, opacity: 0.0,
        depthWrite: false, blending: THREE.AdditiveBlending,
      });
      voxelField = new THREE.InstancedMesh(cube, cubeMat, viewPoints.length);
      var matrix = new THREE.Matrix4();
      for (var vi = 0; vi < viewPoints.length; vi++) {
        var vp = heatPoint(heap, viewPoints[vi]);
        matrix.makeTranslation(vp.x, vp.y, vp.z);
        voxelField.setMatrixAt(vi, matrix);
        var vc = fieldColor(viewPoints[vi][3] === null ? tmin : viewPoints[vi][3]);
        voxelField.setColorAt(vi, new THREE.Color(parseInt(vc.slice(1), 16)));
      }
      voxelField.instanceMatrix.needsUpdate = true;
      if (voxelField.instanceColor) voxelField.instanceColor.needsUpdate = true;
      voxelField.renderOrder = 0;
      heatGroup.add(voxelField);
    }

    var geo = new THREE.BufferGeometry();
    geo.setAttribute("position", new THREE.BufferAttribute(pos, 3));
    geo.setAttribute("color", new THREE.BufferAttribute(col, 3));
    var mat = new THREE.PointsMaterial({
      size: 0.10, vertexColors: true, transparent: true, opacity: 0.0,
      depthWrite: false, blending: THREE.AdditiveBlending, sizeAttenuation: true });
    cloud = new THREE.Points(geo, mat);
    cloud.renderOrder = 1;
    cloud.userData.base = pos.slice();
    heatGroup.add(cloud);

    buildMeasuredProbes(heap, hm.probes || [], fieldColor);

    /* 后端已经根据节点样本生成 pathlines；这里将其显式绘制，
       不把轻微动画误称为 CFD 求解。 */
    if (fieldMetric !== "rh") {
      (hm.lines || []).forEach(function (line) { makeFlowLine(heap, line, fieldColor); });
    }

    /* 色标条端点跟随实际数据范围 */
    if (scaleEl) {
      var e0 = scaleEl.querySelector("span:first-child"), e1 = scaleEl.querySelector("span:last-child");
      if (e0) e0.textContent = Math.floor(colorLo) + "";
      if (e1) e1.textContent = Math.ceil(colorHi) + "";
    }
    if (probeGroup) probeGroup.visible = probesVisible;
  }

  function setMetric(metric) {
    fieldMetric = metric === "rh" ? "rh" : "temp";
    if (lastHeatmap) setHeatmap(lastHeatmap);
  }

  function toggleProbes() {
    probesVisible = !probesVisible;
    if (probeGroup) probeGroup.visible = probesVisible;
    return probesVisible;
  }

  function resetView() {
    if (!camera || !controls || !homeCamera) return;
    camera.position.copy(homeCamera.position);
    controls.target.copy(homeCamera.target);
    controls.update();
  }

  function fullscreen() {
    if (!container) return;
    if (document.fullscreenElement) document.exitFullscreen();
    else if (container.requestFullscreen) container.requestFullscreen();
  }

  /* ---------- 悬停 ---------- */
  function setMouse(e) {
    var rect = renderer.domElement.getBoundingClientRect();
    mouse.x = ((e.clientX - rect.left) / rect.width) * 2 - 1;
    mouse.y = -((e.clientY - rect.top) / rect.height) * 2 + 1;
  }

  function rayAll() {
    var targets = [];
    for (var uid in poleMeshes) targets.push(poleMeshes[uid]);
    if (probeGroup) targets.push(probeGroup);
    for (var id in pileMeshes) targets.push(pileMeshes[id].group);
    return raycaster.intersectObjects(targets, true);
  }

  /* 命中优先：探杆/节点（uid）> 粮堆面 > 无。
     探杆插在粮堆锥台内部，射线会先穿过半透明锥台面命中粮堆；
     必须优先取杆，否则点杆永远被锥台"吃掉"。 */
  function pickTop(hits) {
    for (var i = 0; i < hits.length; i++) {
      if (hits[i].object.userData.uid) return hits[i];
    }
    for (var j = 0; j < hits.length; j++) {
      if (hits[j].object.userData.pile) return hits[j];
    }
    return null;
  }

  function onPointerMove(e) {
    setMouse(e);
    if (drag) { dragMove(e); return; }
    raycaster.setFromCamera(mouse, camera);
    var hit = pickTop(rayAll());
    if (!hit) {
      lastHover = null;
      tooltip.classList.add("hidden");
      return;
    }
    var u = hit.object.userData;
    if (lastHover === u) return;
    lastHover = u;
    var html;
    if (u.pile) {
      html = "<b>" + esc(u.name) + "</b><br><span style='color:#5b6b8c'>点击选中粮堆 · 左键拖拽空白处旋转</span>";
    } else if (u.pole) {
      html = "<b>" + esc(u.name) + "</b><br>" + esc(u.uid) +
        "<br><span style='color:#5b6b8c'>点击进入详情 · 拖动可移动位置</span>";
    } else {
      html = "<b>" + esc(u.name) + "</b> · 节点 " + u.addr +
        (u.lost ? " <span class='badge off'>失联</span>"
                : "<br>温度 <span style='color:" + tempColor(u.temp) + "'>" +
                  (u.temp === null || u.temp === undefined ? "-" : u.temp.toFixed(2)) +
                  " °C</span>" +
                  " · 湿度 " + (u.rh === null || u.rh === undefined ? "-" : u.rh.toFixed(2)) +
                  " %" +
                  (u.alarm ? " <span class='badge alarm'>超限</span>" : ""));
    }
    tooltip.innerHTML = html;
    tooltip.classList.remove("hidden");
    var rect = renderer.domElement.getBoundingClientRect();
    tooltip.style.left = (e.clientX - rect.left + 14) + "px";
    tooltip.style.top = (e.clientY - rect.top + 14) + "px";
  }

  /* ---------- 按下：可能是点击，也可能是开始拖杆 ---------- */
  function onPointerDown(e) {
    setMouse(e);
    raycaster.setFromCamera(mouse, camera);
    var hit = pickTop(rayAll());
    if (!hit) return;
    var u = hit.object.userData;
    if (u.pile) {                        /* 点击粮堆：临时停旋转，防手抖 */
      if (controls) controls.enabled = false;
      return;
    }
    if (!u.uid) return;
    if (controls) controls.enabled = false;   /* 命中探杆：拖杆期间不旋转 */
    var g = poleMeshes[u.uid];
    if (!g) return;
    var h = heapOf(poleData[u.uid]);
    drag = {
      uid: u.uid, started: false,
      px: e.clientX, py: e.clientY,
      gx: g.position.x, gz: g.position.z,
      planeY: h ? heapHeight(h, poleData[u.uid].x, poleData[u.uid].z) : 0,
    };
  }

  function dragMove(e) {
    if (!drag.started && Math.abs(e.clientX - drag.px) + Math.abs(e.clientY - drag.py) > DRAG_TH) {
      drag.started = true;
      tooltip.classList.add("hidden");
    }
    if (!drag.started) return;
    var g = poleMeshes[drag.uid];
    if (!g) return;
    raycaster.setFromCamera(mouse, camera);
    var plane = new THREE.Plane(new THREE.Vector3(0, 1, 0), -drag.planeY);
    var pt = new THREE.Vector3();
    if (!raycaster.ray.intersectPlane(plane, pt)) return;
    var margin = 0.3;
    g.position.x = Math.max(-WH.L / 2 + margin, Math.min(WH.L / 2 - margin, pt.x));
    g.position.z = Math.max(-WH.W / 2 + margin, Math.min(WH.W / 2 - margin, pt.z));
  }

  function onPointerUp(e) {
    if (controls) controls.enabled = true;    /* 松手恢复旋转 */
    if (!drag) return;
    var d = drag;
    drag = null;
    if (!d.started) {
      /* 点击：探杆优先（可穿透半透明粮堆面），其次粮堆 */
      raycaster.setFromCamera(mouse, camera);
      var hit = pickTop(rayAll());
      if (hit) {
        var u = hit.object.userData;
        if (u.pile) {
          if (onPileClick) onPileClick(u.pileId);
          return;
        }
        if (u.uid) {
          if (onPoleClick) onPoleClick(u.uid);
          return;
        }
      }
      return;
    }
    var g = poleMeshes[d.uid];
    if (!g || !onDragEnd) return;
    var nx = unX(g.position.x), nz = unZ(g.position.z);
    var dx = nx - poleData[d.uid].x, dz = nz - poleData[d.uid].z;
    if (Math.abs(dx) < 0.01 && Math.abs(dz) < 0.01) return;
    onDragEnd(d.uid, Math.round(nx * 100) / 100, Math.round(nz * 100) / 100);
  }

  function onResize() {
    fit();
  }

  /* ---------- 渲染循环（告警脉冲 + 热场流动 + 流线光点） ---------- */
  function animate() {
    requestAnimationFrame(animate);
    fit();
    if (controls) controls.update();    /* 阻尼旋转 */
    var t = performance.now() / 1000;

    for (var uid in poleMeshes) {
      var g = poleMeshes[uid];
      for (var j = 0; j < g.children.length; j++) {
        var m = g.children[j];
        if (m.userData.alarm) {
          var s = 1 + ALARM_PULSE * (0.5 + 0.5 * Math.sin(t * 5));
          m.scale.set(s, s, s);
        }
      }
    }

    if (cloud) {
      var pos = cloud.geometry.attributes.position.array;
      var base = cloud.userData.base;
      var amp = (cfg ? cfg.heatmap_step : 0.5) * 0.08 + 0.02;
      for (var i = 0; i < pos.length / 3; i++) {
        pos[i * 3] = base[i * 3] + amp * 0.35 * Math.sin(t * 0.7 + cloudPhase[i] * 1.7);
        pos[i * 3 + 1] = base[i * 3 + 1] + amp * Math.sin(t * 1.2 + cloudPhase[i]);
        pos[i * 3 + 2] = base[i * 3 + 2] + amp * 0.35 * Math.cos(t * 0.9 + cloudPhase[i]);
      }
      cloud.geometry.attributes.position.needsUpdate = true;
    }

    for (var q = 0; q < flowTracers.length; q++) {
      var tr = flowTracers[q];
      var progress = (t * tr.speed + tr.phase) % 1;
      var scaled = progress * (tr.path.length - 1);
      var a = Math.floor(scaled);
      var b = Math.min(a + 1, tr.path.length - 1);
      var blend = scaled - a;
      var pa = tr.path[a], pb = tr.path[b];
      var p0 = heatPoint(tr.heap, pa), p1 = heatPoint(tr.heap, pb);
      tr.mesh.position.lerpVectors(p0, p1, blend);
      var tc = tempColor(pa[3] + (pb[3] - pa[3]) * blend);
      tr.mesh.material.color.setHex(parseInt(tc.slice(1), 16));
    }

    renderer.render(scene, camera);
  }

  return {
    init: init,
    setConfig: setConfig,
    setPoleList: setPoleList,
    updatePoles: updatePoles,
    setPileList: setPileList,
    setSelectedPile: setSelectedPile,
    setSelectedPole: setSelectedPole,
    setHeatmap: setHeatmap,
    setMetric: setMetric,
    toggleProbes: toggleProbes,
    resetView: resetView,
    fullscreen: fullscreen,
    setDragEnd: function (fn) { onDragEnd = fn; },
    setOnPileClick: function (fn) { onPileClick = fn; },
    setOnPoleClick: function (fn) { onPoleClick = fn; },
  };
})();
