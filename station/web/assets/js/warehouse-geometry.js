/* Shared, deterministic geometry and field math. No network or database writes. */
(function (root, factory) {
  var api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.WarehouseGeometry = api;
})(typeof window !== 'undefined' ? window : null, function () {
  'use strict';
  function finite(v) { return v !== null && v !== undefined && v !== '' && Number.isFinite(Number(v)); }
  function validPile(p) {
    return p && ['x', 'z', 'l', 'w', 'height'].every(function (k) { return finite(p[k]); }) &&
      p.l > 0 && p.w > 0 && p.height > 0;
  }
  function siloRadius(p) { return Number(p.base_radius_m) || Math.min(p.l, p.w) / 2; }
  function contains(p, x, z) {
    if (p && p.shape_type === 'silo') {
      var r = siloRadius(p);
      return (x - p.x) ** 2 + (z - p.z) ** 2 <= r * r + 1e-9;
    }
    return Math.abs(x - p.x) <= p.l / 2 + 1e-9 && Math.abs(z - p.z) <= p.w / 2 + 1e-9;
  }
  function height(p, x, z) {
    if (p && p.shape_type === 'silo') {
      var radius = siloRadius(p), r = Math.hypot(x - p.x, z - p.z);
      var top = Number(p.top_radius_m) || 0;
      if (r >= radius) return 0;
      return r <= top ? p.height : p.height * (radius - r) / Math.max(radius - top, 1e-9);
    }
    var u = Math.abs((x - p.x) * 2 / p.l), zn = (z - p.z) * 2 / p.w;
    var v = Math.abs(zn + 0.25 * (1 - zn * zn));
    if (u >= 1 || Math.abs(zn) >= 1 || v >= 1) return 0;
    var base = Math.pow((1 - Math.pow(u, 2.5)) * (1 - Math.pow(v, 2.5)), 0.68);
    var ripple = 1 - 0.035 * Math.sin((x - p.x) * 1.3) ** 2 * Math.sin((z - p.z) * 1.7) ** 2;
    return p.height * base * ripple;
  }
  function cut(p) { return { x: p.x - p.l * 0.29, z: p.z + p.w * 0.10 }; }
  function removed(p, x, z) { var c = cut(p); return x > c.x && z > c.z; }
  // Piecewise grid split exactly at both cut planes; no triangles span the removed volume.
  function gridRange(lo, hi, n) { return Array.from({length: n + 1}, function (_, i) { return i === n ? hi : lo + (hi - lo) * i / n; }); }
  function build(p, sliced, resolution) {
    var n = resolution || 64, c = cut(p), outer = [], main = [], side = [], bottom = [];
    function tri(out, a, b, d) { out.push.apply(out, a.concat(b, d)); }
    function quad(out, a, b, d, e) { tri(out, a, b, d); tri(out, a, d, e); }
    if (p.shape_type === 'silo') {
      var radius = siloRadius(p), x0 = p.x - radius, x1 = p.x + radius;
      var z0 = p.z - radius, z1 = p.z + radius;
      function surface(xs0, xs1, zs0, zs1) {
        if (xs1 <= xs0 || zs1 <= zs0) return;
        var xs = gridRange(xs0, xs1, n), zs = gridRange(zs0, zs1, n);
        for (var i = 0; i < n; i++) for (var j = 0; j < n; j++) {
          var a = [xs[i], height(p, xs[i], zs[j]), zs[j]];
          var b = [xs[i], height(p, xs[i], zs[j + 1]), zs[j + 1]];
          var d = [xs[i + 1], height(p, xs[i + 1], zs[j + 1]), zs[j + 1]];
          var e = [xs[i + 1], height(p, xs[i + 1], zs[j]), zs[j]];
          var mx = (xs[i] + xs[i + 1]) / 2, mz = (zs[j] + zs[j + 1]) / 2;
          if (!contains(p, mx, mz) || (sliced && removed(p, mx, mz))) continue;
          if ([a, b, d, e].every(function (v) { return contains(p, v[0], v[2]); })) quad(outer, a, b, d, e);
          var ba = [a[0], 0, a[2]], bb = [b[0], 0, b[2]], bd = [d[0], 0, d[2]], be = [e[0], 0, e[2]];
          if ([ba, bb, bd, be].every(function (v) { return contains(p, v[0], v[2]); })) quad(bottom, ba, be, bd, bb);
        }
      }
      if (!sliced) surface(x0, x1, z0, z1);
      else {
        surface(x0, x1, z0, c.z);
        surface(x0, c.x, c.z, z1);
        function capX() {
          var zEnd = p.z + Math.sqrt(Math.max(0, radius * radius - (c.x - p.x) ** 2));
          for (var i = 0; i < n; i++) {
            var za = c.z + (zEnd - c.z) * i / n, zb = c.z + (zEnd - c.z) * (i + 1) / n;
            if (!contains(p, c.x, (za + zb) / 2)) continue;
            quad(main, [c.x, 0, za], [c.x, 0, zb], [c.x, height(p, c.x, zb), zb], [c.x, height(p, c.x, za), za]);
          }
        }
        function capZ() {
          var xEnd = p.x + Math.sqrt(Math.max(0, radius * radius - (c.z - p.z) ** 2));
          for (var j = 0; j < n; j++) {
            var xa = c.x + (xEnd - c.x) * j / n, xb = c.x + (xEnd - c.x) * (j + 1) / n;
            if (!contains(p, (xa + xb) / 2, c.z)) continue;
            quad(side, [xa, 0, c.z], [xa, height(p, xa, c.z), c.z], [xb, height(p, xb, c.z), c.z], [xb, 0, c.z]);
          }
        }
        capX(); capZ();
      }
      return {outer: outer, main: main, side: side, bottom: bottom};
    }
    function roof(x0, x1, z0, z1) {
      var xs = gridRange(x0, x1, n), zs = gridRange(z0, z1, n);
      for (var i = 0; i < n; i++) for (var j = 0; j < n; j++) {
        var a = [xs[i], height(p, xs[i], zs[j]), zs[j]];
        var b = [xs[i], height(p, xs[i], zs[j+1]), zs[j+1]];
        var d = [xs[i+1], height(p, xs[i+1], zs[j+1]), zs[j+1]];
        var e = [xs[i+1], height(p, xs[i+1], zs[j]), zs[j]];
        quad(outer, a, b, d, e);
      }
      quad(bottom, [x0,0,z0], [x1,0,z0], [x1,0,z1], [x0,0,z1]);
    }
    var x0 = p.x-p.l/2, x1 = p.x+p.l/2, z0 = p.z-p.w/2, z1 = p.z+p.w/2;
    if (!sliced) roof(x0,x1,z0,z1);
    else {
      roof(x0,x1,z0,c.z); roof(x0,c.x,c.z,z1);
      function face(out, start, end, alongX) {
        for (var i=0;i<n;i++) for (var j=0;j<32;j++) {
          var u=start+(end-start)*i/n, v=start+(end-start)*(i+1)/n;
          function pt(t,h) { var x=alongX?t:c.x, z=alongX?c.z:t; return [x,height(p,x,z)*h,z]; }
          if (alongX) quad(out,pt(u,j/32),pt(v,j/32),pt(v,(j+1)/32),pt(u,(j+1)/32));
          else quad(out,pt(u,j/32),pt(u,(j+1)/32),pt(v,(j+1)/32),pt(v,j/32));
        }
      }
      face(main,c.x,x1,true); face(side,c.z,z1,false);
    }
    return {outer:outer, main:main, side:side, bottom:bottom};
  }
  function validSamples(samples, metric, now) {
    return (samples || []).filter(function (s) {
      return ['x','y','z',metric].every(function (k) { return finite(s[k]); }) &&
        s.online === true && finite(s.ts) && now - s.ts <= 2700 && now - s.ts >= -60;
    });
  }
  function hull(points) {
    var pts=points.map(function(s){return [s.x,s.z];}).sort(function(a,b){return a[0]-b[0]||a[1]-b[1];});
    pts=pts.filter(function(p,i){return !i||p[0]!==pts[i-1][0]||p[1]!==pts[i-1][1];});
    function cross(a,b,c){return (b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0]);}
    var low=[],up=[];
    pts.forEach(function(p){while(low.length>1&&cross(low[low.length-2],low[low.length-1],p)<=0)low.pop();low.push(p);});
    pts.slice().reverse().forEach(function(p){while(up.length>1&&cross(up[up.length-2],up[up.length-1],p)<=0)up.pop();up.push(p);});
    return low.slice(0,-1).concat(up.slice(0,-1));
  }
  function field(samples, metric, options) {
    options=options||{};
    var valid=validSamples(samples,metric,options.now || Date.now()/1000);
    var pts=options.demo?valid:valid.filter(function(s){return s.position_validated===true;});
    var boundary=hull(pts);
    if (!pts.length) { var empty=function(){return null;};empty.support='none';return empty; }
    if (!options.demo && pts.length===1) {
      var constant=function(){return Number(pts[0][metric]);};constant.support='single_point_flat';return constant;
    }
    var minY=Math.min.apply(null,pts.map(function(p){return p.y;}));
    var maxY=Math.max.apply(null,pts.map(function(p){return p.y;}));
    var probeIds=Array.from(new Set(pts.map(function(p){return p.uid;})));
    if (!options.demo && probeIds.length===1 && pts.length>1 && maxY-minY>1e-6) {
      var profile=pts.slice().sort(function(a,b){return a.y-b.y;});
      var vertical=function(x,y,z){
        if(y<minY||y>maxY)return null;
        for(var j=0;j<profile.length-1;j++){
          var a=profile[j],b=profile[j+1];
          if(y<=b.y){var t=(y-a.y)/(b.y-a.y);return Number(a[metric])+(Number(b[metric])-Number(a[metric]))*t;}
        }
        return Number(profile[profile.length-1][metric]);
      };
      vertical.support='single_probe_profile';return vertical;
    }
    var radius=options.radius || 4;
    var interpolate=function(x,y,z) {
      if (!options.demo) {
        if(y<minY||y>maxY)return null;
        if(boundary.length>=3){
          for(var i=0;i<boundary.length;i++) {
            var a=boundary[i],b=boundary[(i+1)%boundary.length];
            if((b[0]-a[0])*(z-a[1])-(b[1]-a[1])*(x-a[0]) < -1e-7)return null;
          }
        }
      }
      var total=0,weight=0,nearest=Infinity;
      for(var k=0;k<pts.length;k++) {
        var s=pts[k], d=(x-s.x)**2+(y-s.y)**2+(z-s.z)**2;
        if(d<1e-12)return Number(s[metric]);
        nearest=Math.min(nearest,d);var w=1/(d*d);total+=s[metric]*w;weight+=w;
      }
      return !options.demo && nearest>radius*radius ? null : total/weight;
    };
    interpolate.support=options.demo?'demo':'multi_probe_idw';return interpolate;
  }
  // Linear contour extraction from the very same scalar vertices used by the faces.
  function contours(positions, values, levels) {
    var lines=[];
    levels.forEach(function(level){
      for(var i=0;i<values.length;i+=3){
        var v=values.slice(i,i+3); if(v.some(function(x){return x===null;}))continue;
        var hits=[];
        for(var e=0;e<3;e++){
          var f=(e+1)%3;
          if((v[e]<level&&v[f]>=level)||(v[f]<level&&v[e]>=level)){
            var t=(level-v[e])/(v[f]-v[e]);
            hits.push([0,1,2].map(function(k){return positions[(i+e)*3+k]+t*(positions[(i+f)*3+k]-positions[(i+e)*3+k]);}));
          }
        }
        if(hits.length===2)lines.push.apply(lines,hits[0].concat(hits[1]));
      }
    });return lines;
  }
  function demo() {
    var pile={id:'demo',name:'模型演示粮堆',x:0,z:0,l:16,w:11,height:4.5};
    var samples=[], poles=[], xs=[-4.3,-1.0,2.4,5.7], now=Date.now()/1000;
    xs.forEach(function(x,i){
      var z=cut(pile).z+0.025, top=height(pile,x,z), uid='DEMO-'+(i+1), nodes=[];
      for(var j=0;j<5;j++){
        var y=0.35+(top-0.65)*j/4;
        var t=16.4+i*3.5+j*.5+3.2*Math.exp(-((i-2.5)**2+(j-3)**2)/2);
        var s={uid:uid,node_uid:uid+'-'+j,addr:j+1,x:x,y:y,z:z,temp:t,rh:48+i*5+j*1.1,ts:now,online:true};
        samples.push(s);nodes.push(s);
      }
      poles.push({uid:uid,name:'演示探杆 '+(i+1),x:x,z:z,nodes:nodes,top:top+0.6,online:true});
    });return {pile:pile, poles:poles, samples:samples};
  }
  return {finite:finite, validPile:validPile, height:height, contains:contains,
    siloRadius:siloRadius, cut:cut, removed:removed, build:build,
    validSamples:validSamples, field:field, contours:contours, demo:demo};
});
