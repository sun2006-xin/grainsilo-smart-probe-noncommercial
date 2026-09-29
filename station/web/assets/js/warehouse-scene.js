/* Real mesh scene, using the project's vendored Three.js r128. Units: metres. */
window.Scene3D = (function () {
  'use strict';
  var T=window.THREE, G=window.WarehouseGeometry;
  var scene, camera, renderer, controls, container, world, grain, rods, notice, legend, detail;
  var config={wh_l:22,wh_w:16,wh_h:9,warehouse:{shape_type:'flat'}}, piles=[], poles=[], heatmap=null, selected=null, selectedPole=null;
  var demoMode=false, sliced=true, metric='temp', probesVisible=true, pending=false, renderedKey='';
  var pickPile=null, pickPole=null, dragEnd=null, drag=null, pointerDown=null, raf=0;
  var ray=new T.Raycaster(), pointer=new T.Vector2(), materials={}, textures={}, frameCount=0, fps=0, clockStart=0;
  var warehouseAsset=null;
  var previousFit=1;
  var scalarRange=[0,1];
  function setScalarRange(samples){
    var values=G.validSamples(samples,metric,Date.now()/1000).map(function(s){return Number(s[metric]);});
    var lo=values.length?Math.floor(Math.min.apply(null,values)):0,hi=values.length?Math.ceil(Math.max.apply(null,values)):1;
    scalarRange=[lo,hi>lo?hi:lo+1];
  }
  function finite(v) { return G.finite(v); }
  function el(tag,cls,text) { var e=document.createElement(tag);e.className=cls;if(text)e.textContent=text;return e; }
  function dispose(group) {
    if(!group)return;
    group.traverse(function(o){if(o.geometry)o.geometry.dispose(); if(o.material && o.userData.ownMaterial)o.material.dispose();});
    group.clear();
  }
  function seeded(seed) {return function(){seed=(seed*1664525+1013904223)>>>0;return seed/4294967296;};}
  function texture(kind) {
    if(textures[kind])return textures[kind];
    var cv=document.createElement('canvas');cv.width=cv.height=512;var ctx=cv.getContext('2d'),rand=seeded(4127);
    ctx.fillStyle=kind==='grain'?'#b58b45':kind==='relief'?'#b6b6b6':'#7b807e';ctx.fillRect(0,0,512,512);
    if(kind==='grain'||kind==='relief') {
      for(var i=0;i<8500;i++){
        var x=rand()*512,y=rand()*512,r=rand()*Math.PI;
        ctx.save();ctx.translate(x,y);ctx.rotate(r);
        ctx.fillStyle='rgba(24,20,14,.48)';ctx.beginPath();ctx.ellipse(1,1,3.6,1.6,0,0,7);ctx.fill();
        ctx.fillStyle=(kind==='relief'?['#cccccc','#ababab','#eeeeee','#aaaaaa','#dddddd']:['#ddba72','#c8a15b','#e7c581','#ae8040','#d7ad63'])[Math.floor(rand()*5)];
        ctx.beginPath();ctx.ellipse(0,0,3.1,1.35,0,0,7);ctx.fill();
        ctx.strokeStyle='rgba(92,63,19,.28)';ctx.lineWidth=.45;ctx.beginPath();ctx.moveTo(-2,0);ctx.lineTo(2,0);ctx.stroke();ctx.restore();
      }
    } else {
      for(var j=0;j<30000;j++){var v=Math.floor(90+rand()*95);ctx.fillStyle='rgba('+v+','+v+','+v+','+(rand()*.19)+')';ctx.fillRect(rand()*512,rand()*512,rand()*6+1,rand()*3+1);}
      ctx.strokeStyle='rgba(30,40,40,.22)';ctx.lineWidth=1;ctx.strokeRect(0,0,512,512);
    }
    var tex=new T.CanvasTexture(cv);tex.wrapS=tex.wrapT=T.RepeatWrapping;tex.anisotropy=renderer?Math.min(renderer.capabilities.getMaxAnisotropy(),8):4;
    tex.encoding=T.sRGBEncoding;textures[kind]=tex;return tex;
  }
  function mat(key,color,roughness,metalness) {
    if(!materials[key]){
      materials[key]=new T.MeshStandardMaterial({color:color,roughness:roughness===undefined?.8:roughness,metalness:metalness||0});
      materials[key].color.convertSRGBToLinear();
    }
    materials[key].name=key;
    return materials[key];
  }
  function box(parent,name,w,h,d,x,y,z,material) {
    var mesh=new T.Mesh(new T.BoxGeometry(w,h,d),material);mesh.name=name;mesh.position.set(x,y,z);
    mesh.castShadow=true;mesh.receiveShadow=true;parent.add(mesh);return mesh;
  }
  function beam(parent,a,b,width,material,name) {
    var va=new T.Vector3().fromArray(a),vb=new T.Vector3().fromArray(b),m=box(parent,name||'SteelBeam',width,va.distanceTo(vb),width,0,0,0,material);
    m.position.copy(va.clone().add(vb).multiplyScalar(.5));m.quaternion.setFromUnitVectors(new T.Vector3(0,1,0),vb.sub(va).normalize());
    if(/Roof|Truss|Lamp/.test(name||''))m.castShadow=false;
    return m;
  }
  function warehouseSilo(diameter,H) {
    var radius=diameter/2,arcStart=Math.PI/4,arcLength=Math.PI*1.5;
    var steel=mat('siloSteel',0x687a80,.42,.72),shell=mat('siloShell',0x9daeb0,.38,.42);
    var roof=mat('siloRoof',0x54676d,.52,.58),concrete=mat('concrete',0x85857e);
    concrete.map=texture('concrete');concrete.bumpMap=texture('concrete');concrete.bumpScale=.045;
    var floor=new T.Mesh(new T.CylinderGeometry(radius+6,radius+6,.16,96),concrete);
    floor.name='WarehouseFloor';floor.position.y=-.13;floor.receiveShadow=true;world.add(floor);
    var base=new T.Mesh(new T.CylinderGeometry(radius+.14,radius+.14,.34,96),mat('siloBase',0x72746f,.84,.12));
    base.name='SiloConcreteBase';base.position.y=.03;base.castShadow=true;base.receiveShadow=true;world.add(base);
    var wall=new T.Mesh(new T.CylinderGeometry(radius,radius,H,96,1,true,arcStart,arcLength),shell);
    wall.name='SiloShell';wall.position.y=H/2;wall.castShadow=true;wall.receiveShadow=true;world.add(wall);
    for(var y=.45;y<H;y+=Math.max(1.3,H/6)){
      var ring=new T.Mesh(new T.TorusGeometry(radius+.025,.045,10,96),steel);
      ring.name='SiloRingBeam';ring.rotation.x=Math.PI/2;ring.position.y=y;ring.castShadow=true;world.add(ring);
    }
    var eave=new T.Mesh(new T.TorusGeometry(radius+.04,.075,12,96),steel);
    eave.name='SiloEaveRing';eave.rotation.x=Math.PI/2;eave.position.y=H;world.add(eave);
    var roofHeight=Math.max(1.2,Math.min(3.2,diameter*.22));
    var roofMesh=new T.Mesh(new T.CylinderGeometry(0,radius,roofHeight,96,1,true,arcStart,arcLength),roof);
    roofMesh.name='SiloConicalRoof';roofMesh.position.y=H+roofHeight/2;roofMesh.castShadow=true;roofMesh.receiveShadow=true;world.add(roofMesh);
    for(var angle=arcStart;angle<arcStart+arcLength;angle+=Math.PI/18){
      var x=radius*Math.sin(angle),z=radius*Math.cos(angle);
      beam(world,[x,.15,z],[x,H-.08,z],.075,steel,'SiloVerticalRib');
      var roofX=x*.06,roofZ=z*.06;
      beam(world,[roofX,H+roofHeight-.05,roofZ],[x,H+.08,z],.045,steel,'SiloRoofRafter');
    }
    [arcStart,arcStart+arcLength].forEach(function(angle){
      var x=radius*Math.sin(angle),z=radius*Math.cos(angle);
      beam(world,[x,.1,z],[x,H,z],.14,mat('siloCutRim',0xb3a576,.48,.62),'SiloCutEdge');
    });
    var finial=new T.Mesh(new T.CylinderGeometry(.04,.12,.45,16),steel);
    finial.name='SiloRoofFinial';finial.position.y=H+roofHeight+.18;world.add(finial);
    // A short service ladder/cage gives the curved shell a readable physical scale.
    var ladderAngle=Math.PI*.78,lx=(radius+.18)*Math.sin(ladderAngle),lz=(radius+.18)*Math.cos(ladderAngle);
    beam(world,[lx-.22,.3,lz],[lx-.22,H-.35,lz],.035,steel,'SiloLadderRail');
    beam(world,[lx+.22,.3,lz],[lx+.22,H-.35,lz],.035,steel,'SiloLadderRail');
    for(var rung=.7;rung<H-.45;rung+=.42)beam(world,[lx-.22,rung,lz],[lx+.22,rung,lz],.025,steel,'SiloLadderRung');
    container.dataset.asset='procedural-silo';
  }
  function warehouse() {
    dispose(world);var L=demoMode?24:Number(config.wh_l)||20,W=demoMode?18:Number(config.wh_w)||10,H=demoMode?9:Number(config.wh_h)||8;
    if(!demoMode&&config.warehouse&&config.warehouse.shape_type==='silo'){
      warehouseSilo(Number(config.warehouse.diameter_m)||L,Number(config.warehouse.height_m)||H);return;
    }
    if(warehouseAsset){
      var model=warehouseAsset.clone(true);model.scale.set(L/24,H/9,W/18);
      model.traverse(function(o){if(!o.isMesh)return;o.geometry=o.geometry.clone();
        if(materials[o.userData.runtime_material])o.material=materials[o.userData.runtime_material];
        o.castShadow=/Column|Plinth|Wall|Walkway/.test(o.name);o.receiveShadow=true;
      });world.add(model);container.dataset.asset='GLB';return;
    }
    // Keep the near wall open for camera access; all other architecture is geometry.
    var steel=mat('steel',0x344752,.48,.72),wall=mat('wall',0x61757b,.8,.3),concrete=mat('concrete',0x85857e);
    concrete.map=texture('concrete');concrete.bumpMap=texture('concrete');concrete.bumpScale=.045;
    wall.bumpMap=texture('concrete');wall.bumpScale=.018;
    var floor=box(world,'WarehouseFloor',L+18,.16,W+22,0,-.13,1,concrete);
    var floorUV=floor.geometry.attributes.uv;for(var u=0;u<floorUV.count;u++)floorUV.setXY(u,floorUV.getX(u)*8,floorUV.getY(u)*8);
    var rear=-W*.70,left=-L*.65,right=L*.65,front=W*.82;
    box(world,'WarehouseRearWall',right-left,H,.16,0,H/2,rear,wall);
    box(world,'WarehouseLeftWall',.16,H,front-rear,left,H/2,(front+rear)/2,wall);
    box(world,'WarehouseRightWall',.16,H,front-rear,right,H/2,(front+rear)/2,wall);
    [left,right].forEach(function(x){box(world,'ConcretePlinth',.2,1.5,front-rear,x,.75,(front+rear)/2,concrete);});
    box(world,'RearPlinth',right-left,1.5,.2,0,.75,rear+.08,concrete);
    // Corrugated panel seams, window frames and structural trusses give parallax at every angle.
    for(var x=left+.3;x<right;x+=.62)box(world,'RearPanelRib',.025,H,.035,x,H/2,rear+.12,steel);
    var glass=mat('window',0xb9d5db,.28,.05);glass.emissive=new T.Color(0xa8c9d5);glass.emissiveIntensity=.35;
    function windowFrame(x,y,z,side){
      var group=new T.Group();world.add(group);group.position.set(x,y,z);if(side)group.rotation.y=Math.PI/2;
      box(group,'WarehouseWindow',1.8,1.6,.035,0,0,0,glass);
      [-.9,0,.9].forEach(function(dx){box(group,'WindowMullion',.055,1.7,.08,dx,0,.03,steel);});
      [-.8,0,.8].forEach(function(dy){box(group,'WindowTransom',1.85,.04,.08,0,dy,.03,steel);});
    }
    for(var z=rear+1.6;z<front;z+=3.5){
      [left+.12,right-.12].forEach(function(x){
        box(world,'SteelColumn',.22,H,.28,x,H/2,z,steel);windowFrame(x,H*.52,z+1.5,true);
      });
      beam(world,[left,H,z],[0,H+2.6,z],.17,steel,'RoofRafter');beam(world,[0,H+2.6,z],[right,H,z],.17,steel,'RoofRafter');
      beam(world,[left,H-.7,z],[right,H-.7,z],.13,steel,'TrussTie');
      for(var s=0;s<10;s++){
        var xx=left+(right-left)*s/10,nx=left+(right-left)*(s+1)/10;
        beam(world,[xx,H-.7,z],[nx,H+2.6*(1-Math.abs(nx/right)),z],.065,steel,'TrussDiagonal');
      }
      beam(world,[0,H+1,z],[0,H-.6,z],.04,steel,'LampCable');
      var lamp=new T.Mesh(new T.ConeGeometry(.28,.16,20,1,true),steel);lamp.position.set(0,H-.65,z);world.add(lamp);
      var bulb=new T.Mesh(new T.CircleGeometry(.20,16),new T.MeshBasicMaterial({color:0xfff1ca,side:T.DoubleSide}));
      bulb.rotation.x=Math.PI/2;bulb.position.set(0,H-.74,z);bulb.userData.ownMaterial=true;world.add(bulb);
    }
    for(var xx=left;xx<=right;xx+=2.3){
      var yy=H+2.6*(1-Math.abs(xx/right));beam(world,[xx,yy,rear],[xx,yy,front],.065,steel,'RoofPurlin');
    }
    var roofmat=mat('roof',0x384750,.8,.35);roofmat.side=T.DoubleSide;
    [[left,0],[0,right]].forEach(function(pair){
      var x0=pair[0],x1=pair[1],y0=H+2.6*(1-Math.abs(x0/right)),y1=H+2.6*(1-Math.abs(x1/right));
      var geo=new T.BufferGeometry();geo.setAttribute('position',new T.Float32BufferAttribute([x0,y0,rear,x0,y0,front,x1,y1,front,x0,y0,rear,x1,y1,front,x1,y1,rear],3));geo.computeVertexNormals();
      var mesh=new T.Mesh(geo,roofmat);mesh.name='WarehouseRoof';world.add(mesh);
    });
    for(var q=left+2;q<right;q+=4)windowFrame(q,H*.56,rear+.18,false);
    for(var i=-3;i<=3;i++)box(world,'FloorJoint',.014,.002,W+16,i*3,.002,0,mat('joint',0x5a625f));
    // A modeled rear loading door, side service walkway and guard rail.
    box(world,'LoadingDoor',4,4.4,.08,1,2.2,rear+.18,mat('door',0x65767f,.6,.5));
    for(var d=0;d<10;d++)box(world,'DoorSlat',4,.018,.04,1,.22+d*.43,rear+.24,steel);
    beam(world,[right-1.0,1.1,rear],[right-1.0,1.1,front],.05,mat('rail',0xb1934b,.5,.5),'WalkwayRail');
    for(var zz=rear;zz<front;zz+=2)beam(world,[right-1,0,zz],[right-1,1.1,zz],.05,materials.rail,'RailPost');
  }
  function color(value,lo,hi) {
    var stops=[0x125adc,0x08afd3,0x18ba9e,0xa9d74c,0xffd041,0xf47f1d,0xd93d25];
    if(value===null)return new T.Color(0xbcb3a1);
    var t=T.MathUtils.clamp((value-lo)/(hi-lo),0,1)*(stops.length-1),i=Math.min(stops.length-2,Math.floor(t));
    return new T.Color(stops[i]).lerp(new T.Color(stops[i+1]),t-i).convertSRGBToLinear();
  }
  function meshFrom(name,positions,material,parent) {
    var geo=new T.BufferGeometry(),uv=[];geo.setAttribute('position',new T.Float32BufferAttribute(positions,3));
    for(var i=0;i<positions.length;i+=3){var x=positions[i],y=positions[i+1],z=positions[i+2];uv.push((x+z)*.28,(y+z)*.28);}
    geo.setAttribute('uv',new T.Float32BufferAttribute(uv,2));geo.computeVertexNormals();
    var mesh=new T.Mesh(geo,material);mesh.name=name;mesh.castShadow=true;mesh.receiveShadow=true;parent.add(mesh);return mesh;
  }
  function grainMaterial() {
    var material=mat('grain',0xffffff,.95);material.map=texture('grain');material.bumpMap=texture('grain');material.bumpScale=.035;material.side=T.DoubleSide;return material;
  }
  function buildPile(p, samples, validated) {
    var geometry=G.build(p,sliced,64),group=new T.Group();group.name='Pile_'+p.id;group.userData.pileId=p.id;grain.add(group);
    meshFrom('PileOuter',geometry.outer,grainMaterial(),group);meshFrom('PileBottom',geometry.bottom,grainMaterial(),group);
    var fn=G.field(samples,metric,{demo:demoMode}),lo=scalarRange[0],hi=scalarRange[1],supported=false;
    [geometry.main,geometry.side].forEach(function(pos,index){
      if(!pos.length)return;
      var m=grainMaterial().clone();m.vertexColors=true;m.map=texture('relief');m.bumpMap=texture('relief');m.bumpScale=.05;
      m.roughness=.86;
      // Keep scalar colors intact: the surface albedo contributes only fine luminance.
      m.onBeforeCompile=function(shader){shader.fragmentShader=shader.fragmentShader.replace('#include <map_fragment>',
        '#ifdef USE_MAP\n vec4 g = mapTexelToLinear(texture2D(map,vUv));\n diffuseColor.rgb *= 0.40 + 0.60 * dot(g.rgb,vec3(0.2126,0.7152,0.0722));\n #endif');};
      var face=meshFrom(index?'PileCutSide':'PileCutMain',pos,m,group);face.userData.ownMaterial=true;
      var vals=[],colors=[];
      for(var i=0;i<pos.length;i+=3){var v=validated?fn(pos[i],pos[i+1],pos[i+2]):null;if(v!==null)supported=true;vals.push(v);var cc=color(v,lo,hi);colors.push(cc.r,cc.g,cc.b);}
      face.geometry.setAttribute('color',new T.Float32BufferAttribute(colors,3));
      var levels=Array.from({length:16},function(_,i){return lo+(hi-lo)*(i+1)/17;});
      var lines=G.contours(pos,vals,levels);
      if(lines.length){
        var lg=new T.BufferGeometry();lg.setAttribute('position',new T.Float32BufferAttribute(lines,3));
        var lm=new T.LineBasicMaterial({color:0xfff4d5,transparent:true,opacity:.34,depthWrite:false});
      var contour=new T.LineSegments(lg,lm);contour.name='DataIsolines';contour.position[index?'x':'z']=.023;contour.userData.ownMaterial=true;group.add(contour);
      }
    });
    // Actual grain kernels, instanced on the exterior and the exposed rim; never screen sprites.
    var rand=seeded(901),count=9000,kg=new T.SphereGeometry(1,6,4),km=mat('kernels',0xd7ae63,.92);
    var kernels=new T.InstancedMesh(kg,km,count),obj=new T.Object3D(),n=0;
    for(var k=0;k<count*2&&n<count;k++){
      var x=p.x+(rand()-.5)*p.l,z=p.z+(rand()-.5)*p.w;if(sliced&&G.removed(p,x,z))continue;
      var y=G.height(p,x,z);if(y<.05)continue;
      obj.position.set(x,y+.012,z);obj.rotation.set(rand()*.4,rand()*Math.PI,rand()*.4);
      var size=.021+rand()*.018;obj.scale.set(size*1.7,size*.56,size*.72);obj.updateMatrix();kernels.setMatrixAt(n,obj.matrix);
      kernels.setColorAt(n,new T.Color().setHSL(.105+rand()*.02,.39+rand()*.16,.43+rand()*.22));n++;
    }
    kernels.count=n;kernels.name='GrainKernels';kernels.receiveShadow=true;group.add(kernels);
    if(supported) {
      var supportLabel={single_point_flat:'单测点常值外推 · 极低置信度',single_probe_profile:'沿单探杆轴向插值；横向均匀外推 · 低置信度',multi_probe_idw:'多探杆 IDW 插值估算',sparse_multi_probe_idw:'稀疏测点 IDW · 低置信度'}[fn.support]||'实测点插值估算';
      legend.hidden=false;legend.querySelector('b').textContent=(metric==='rh'?'相对湿度 · %RH':'温度 · °C');
      legend.querySelector('small').textContent=lo+' — '+hi+(demoMode?' · 演示数据':' · '+supportLabel);
    }
  }
  function label(text,x,y,z) {
    var cv=document.createElement('canvas');cv.width=256;cv.height=64;var ctx=cv.getContext('2d');
    ctx.fillStyle='rgba(17,35,41,.88)';ctx.fillRect(0,0,256,64);ctx.font='28px sans-serif';ctx.textAlign='center';ctx.fillStyle='#ffffff';ctx.fillText(text,128,43);
    var map=new T.CanvasTexture(cv),m=new T.SpriteMaterial({map:map,depthTest:false}),s=new T.Sprite(m);s.scale.set(1.2,.3,1);s.position.set(x,y,z);s.userData.ownMaterial=true;s.userData.labelTexture=map;rods.add(s);
  }
  function buildRod(p, samples) {
    var group=new T.Group();group.name='Probe_'+p.uid;group.userData.uid=p.uid;group.userData.demo=demoMode;rods.add(group);
    var ordered=samples.filter(function(s){return finite(s.x)&&finite(s.y)&&finite(s.z);}).slice().sort(function(a,b){return a.y-b.y;});
    var metal=mat('probe',0xbac6cc,.27,.85),top=ordered.length?ordered[ordered.length-1].y:.5;
    for(var i=0;i<ordered.length-1;i++){
      var a=ordered[i],b=ordered[i+1];beam(group,[a.x,a.y,a.z],[b.x,b.y,b.z],.046,metal,'CalibratedProbeSegment');
    }
    if(ordered.length===1){var marker=new T.Mesh(new T.CylinderGeometry(.035,.035,.18,12),metal);marker.position.set(ordered[0].x,ordered[0].y,ordered[0].z);group.add(marker);}
    var anchor=ordered.length?ordered[ordered.length-1]:{x:p.x||0,z:p.z||0};
    var halo=new T.Mesh(new T.TorusGeometry(.12,.018,8,20),mat('selection',0x27e7c3,.4));
    halo.name='SelectedProbeHalo';halo.rotation.x=Math.PI/2;halo.position.set(anchor.x,top+.12,anchor.z);halo.visible=p.uid===selectedPole;group.add(halo);
    ordered.forEach(function(s){
      var fresh=G.validSamples([s],metric,Date.now()/1000).length>0;
      var cc=fresh?color(Number(s[metric]),scalarRange[0],scalarRange[1]):new T.Color(0x858b89);
      var nm=new T.MeshStandardMaterial({color:cc,emissive:cc,emissiveIntensity:.65,roughness:.3});
      var sensor=new T.Mesh(new T.SphereGeometry(.064,12,8),nm);sensor.name='Sensor_'+(s.node_uid||s.addr);sensor.position.set(s.x,s.y,s.z);sensor.userData={uid:p.uid,sample:s,ownMaterial:true,demo:demoMode};group.add(sensor);
      var band=new T.Mesh(new T.CylinderGeometry(.052,.052,.14,12),metal);band.position.copy(sensor.position);group.add(band);
    });
    if(ordered.length)label(demoMode?p.name:p.name+' · 节点坐标已校准',anchor.x,top+.35,anchor.z);
  }
  function actualSamples(pile) {
    if(!heatmap||!heatmap.field||heatmap.field.coordinates_calibrated!==true)return [];
    var members=poles.filter(function(p){return String(p.pile_id)===String(pile.id);}).map(function(p){return p.uid;});
    return (heatmap.probes||[]).filter(function(s){return members.indexOf(s.uid)>=0&&s.position_validated===true&&s.node_uid&&finite(s.x)&&finite(s.y)&&finite(s.z)&&s.online===true;}).map(function(s){
      return Object.assign({},s,{x:Number(s.x)-Number(config.wh_l)/2,z:Number(s.z)-Number(config.wh_w)/2,y:Number(s.y)});
    }).filter(function(s){return s.y>=0&&s.y<=G.height(pile,s.x,s.z)+.1&&G.contains(pile,s.x,s.z);});
  }
  function rebuild() {
    if(!renderer)return;
    var key=demoMode?JSON.stringify(['demo',sliced,metric]):JSON.stringify([config,piles,poles,heatmap,selected,sliced,metric,Math.floor(Date.now()/1000/30)]);
    if(key===renderedKey)return;renderedKey=key;
    dispose(grain);rods.traverse(function(o){if(o.userData.labelTexture)o.userData.labelTexture.dispose();});dispose(rods);legend.hidden=true;
    var unplaced=0,validCount=0;
    if(demoMode){
      var d=G.demo();setScalarRange(d.samples);buildPile(d.pile,d.samples,true);d.poles.forEach(function(p){buildRod(p,p.nodes);});
      notice.textContent='演示数据 · 4 根虚拟探杆 / 20 个虚拟测点 · 不写入数据库';notice.dataset.mode='demo';
    }else{
      var list=piles.filter(function(p){return selected==null||String(p.id)===String(selected);});
      setScalarRange(list.filter(G.validPile).flatMap(function(p){return actualSamples(Object.assign({},p,{x:p.x-config.wh_l/2,z:p.z-config.wh_w/2}));}));
      list.forEach(function(p){
        if(!G.validPile(p)||p.l>config.wh_l||p.w>config.wh_w)return;
        var model=Object.assign({},p,{x:Number(p.x)-Number(config.wh_l)/2,z:Number(p.z)-Number(config.wh_w)/2});
        var samples=actualSamples(model);validCount++;buildPile(model,samples,samples.length>0);
      });
      poles.filter(function(p){return selected==null||String(p.pile_id)===String(selected);}).forEach(function(p){
        var pile=list.filter(function(v){return String(v.id)===String(p.pile_id);})[0];
        if(!pile||!G.validPile(pile)){unplaced++;return;}
        var model=Object.assign({},pile,{x:pile.x-config.wh_l/2,z:pile.z-config.wh_w/2});
        var samples=actualSamples(model).filter(function(s){return s.uid===p.uid;});
        if(!samples.length){unplaced++;return;}
        buildRod({uid:p.uid,name:p.name,x:p.x-config.wh_l/2,z:p.z-config.wh_w/2},samples);
      });
      var support=heatmap&&heatmap.field?heatmap.field.spatial_support:'none';
      var supportText={single_point_flat:'单测点常值外推，极低置信度',single_probe_profile:'单探杆轴向实测插值、横向均匀外推，低置信度',multi_probe_idw:'多探杆 IDW 插值估算',sparse_multi_probe_idw:'测点稀疏，IDW 插值低置信度'}[support];
      notice.dataset.mode='live';notice.textContent=!validCount?'实时数据 · 粮堆尺寸未配置，暂无可建模粮堆；可切换「模型演示」预览效果':
        '实时数据 · '+(legend.hidden?'无有效温湿度测点；只显示真实粮堆几何':supportText+'；不是 CFD 仿真')+
        (unplaced?' · '+unplaced+' 根探杆尚无有效节点坐标校准，未按通信地址推测位置':'');
    }
    rods.visible=probesVisible;container.dataset.mode=demoMode?'demo':'live';container.dataset.cut=sliced?'quarter':'full';container.dataset.metric=metric;
    updateStats();
  }
  function updateStats(){
    var geometryCount=0,triangles=0;scene.traverse(function(o){if(o.isMesh){geometryCount++;if(o.geometry){triangles+=(o.geometry.index?o.geometry.index.count:o.geometry.attributes.position.count)/3*(o.isInstancedMesh?o.count:1);}}});
    container.dataset.meshes=geometryCount;container.dataset.triangles=Math.round(triangles);container.dataset.fps=fps;
  }
  function schedule(){if(pending)return;pending=true;requestAnimationFrame(function(){pending=false;rebuild();});}
  function home(){
    if(!camera)return;
    var scale=demoMode?1:Math.max(config.wh_l/24,config.wh_w/18,.7);
    var r=container.getBoundingClientRect(),fit=Math.max(1,1.6/(r.width/r.height||1));previousFit=fit;
    camera.position.set(6*scale*fit,(2.5+3.5*fit)*scale,15*scale*fit);controls.target.set(0,2.5*scale,0);controls.update();
    container.dataset.camera=camera.position.toArray().map(function(x){return x.toFixed(2);}).join(',');
  }
  function resize(){if(!renderer)return;var r=container.getBoundingClientRect();if(r.width<1||r.height<1)return;renderer.setSize(r.width,r.height,false);camera.aspect=r.width/r.height;
    var fit=Math.max(1,1.6/camera.aspect);if(controls){camera.position.sub(controls.target).multiplyScalar(fit/previousFit).add(controls.target);previousFit=fit;controls.update();}camera.updateProjectionMatrix();}
  function pick(e){var r=renderer.domElement.getBoundingClientRect();pointer.set((e.clientX-r.left)/r.width*2-1,-(e.clientY-r.top)/r.height*2+1);ray.setFromCamera(pointer,camera);return ray.intersectObjects([rods,grain],true).filter(function(h){return h.object.isMesh;})[0];}
  function identity(o){while(o && !o.userData.uid && o.userData.pileId===undefined)o=o.parent;return o;}
  function init(id){
    container=document.getElementById(id);scene=new T.Scene();scene.background=new T.Color(0x89989d);scene.fog=new T.Fog(0x89989d,42,90);
    camera=new T.PerspectiveCamera(47,1,.08,180);
    try{renderer=new T.WebGLRenderer({antialias:true,alpha:false,preserveDrawingBuffer:true});}catch(e){container.appendChild(el('p','warehouse-error','此浏览器无法启用 WebGL，请使用数据视图查看实测数据。'));return;}
    renderer.setPixelRatio(Math.min(window.devicePixelRatio||1,1.6));renderer.outputEncoding=T.sRGBEncoding;
    renderer.toneMapping=T.ACESFilmicToneMapping;renderer.toneMappingExposure=.94;
    renderer.shadowMap.enabled=true;renderer.shadowMap.type=T.PCFSoftShadowMap;
    renderer.domElement.setAttribute('aria-label','可旋转的三维粮仓模型');container.appendChild(renderer.domElement);
    renderer.domElement.addEventListener('webglcontextlost',function(e){e.preventDefault();notice.textContent='图形上下文中断，请刷新；实测数据仍可在右侧查看';cancelAnimationFrame(raf);});
    renderer.domElement.addEventListener('webglcontextrestored',function(){notice.textContent='图形上下文已恢复';tick(performance.now());});
    controls=new T.OrbitControls(camera,renderer.domElement);controls.enableDamping=true;controls.dampingFactor=.08;controls.minDistance=5;controls.maxDistance=60;controls.maxPolarAngle=Math.PI*.49;
    controls.addEventListener('change',function(){container.dataset.camera=camera.position.toArray().map(function(x){return x.toFixed(2);}).join(',');});
    scene.add(new T.HemisphereLight(0xd9ebff,0x7c6847,.68));
    var sun=new T.DirectionalLight(0xffe5b3,1.7);sun.position.set(-8,10,18);sun.castShadow=true;sun.shadow.mapSize.set(2048,2048);sun.shadow.camera.left=-24;sun.shadow.camera.right=24;sun.shadow.camera.top=24;sun.shadow.camera.bottom=-24;sun.shadow.bias=-.0005;sun.shadow.normalBias=.04;scene.add(sun);
    var fill=new T.DirectionalLight(0xb9d9f5,.65);fill.position.set(8,8,-12);scene.add(fill);
    world=new T.Group();world.name='WarehouseStructure';grain=new T.Group();grain.name='GrainPiles';rods=new T.Group();rods.name='Probes';scene.add(world,grain,rods);
    notice=el('div','warehouse-notice');notice.setAttribute('role','status');container.appendChild(notice);
    legend=el('div','warehouse-legend');legend.innerHTML='<b></b><i></i><small></small>';legend.hidden=true;container.appendChild(legend);
    detail=el('div','warehouse-point-detail');detail.hidden=true;container.appendChild(detail);
    renderer.domElement.addEventListener('pointerdown',function(e){pointerDown={x:e.clientX,y:e.clientY};
      if(e.shiftKey&&!demoMode){var hit=pick(e),o=hit&&identity(hit.object);while(o&&o.parent!==rods)o=o.parent;
        if(o&&o.userData.uid){drag=o;controls.enabled=false;renderer.domElement.setPointerCapture(e.pointerId);}}
    });
    renderer.domElement.addEventListener('pointermove',function(e){
      if(drag){pick(e);var point=new T.Vector3();if(ray.ray.intersectPlane(new T.Plane(new T.Vector3(0,1,0),0),point)){
        drag.userData.dragPoint={x:T.MathUtils.clamp(point.x+config.wh_l/2,0,config.wh_l),z:T.MathUtils.clamp(point.z+config.wh_w/2,0,config.wh_w)};
      }return;}
      var hit=pick(e),s=hit&&hit.object.userData.sample;if(!s){detail.hidden=true;return;}
      detail.hidden=false;detail.textContent=(demoMode?'演示测点 · ':'实测点 · ')+s.uid+' / '+(s.node_uid||s.addr)+' · '+Number(s.temp).toFixed(1)+' °C / '+Number(s.rh).toFixed(1)+' %RH · 高度 '+s.y.toFixed(2)+' m · '+new Date(s.ts*1000).toLocaleString();
    });
    renderer.domElement.addEventListener('pointerup',function(e){
      if(drag){if(drag.userData.dragPoint&&dragEnd)dragEnd(drag.userData.uid,drag.userData.dragPoint.x,drag.userData.dragPoint.z);drag=null;controls.enabled=true;return;}
      if(!pointerDown||Math.hypot(e.clientX-pointerDown.x,e.clientY-pointerDown.y)>5)return;
      var hit=pick(e),o=hit&&identity(hit.object);if(!o)return;
      if(demoMode){detail.hidden=false;detail.textContent='模型演示 · 虚拟探杆和温湿度均未写入数据库';return;}
      if(o.userData.uid&&pickPole)pickPole(o.userData.uid);else if(o.userData.pileId!==undefined&&pickPile)pickPile(o.userData.pileId);
    });
    renderer.domElement.addEventListener('pointercancel',function(){drag=null;pointerDown=null;controls.enabled=true;});
    new ResizeObserver(resize).observe(container);warehouse();home();resize();rebuild();tick(performance.now());
    // Only a grain-surface material is loaded; scene composition always comes from meshes.
    if(T.TextureLoader)new T.TextureLoader().load('/assets/textures/wheat-kernels-albedo.png',function(tex){
      tex.wrapS=tex.wrapT=T.RepeatWrapping;tex.repeat.set(3,3);tex.encoding=T.sRGBEncoding;
      tex.anisotropy=Math.min(renderer.capabilities.getMaxAnisotropy(),8);
      textures.grain=tex;textures.relief=tex;
      if(materials.grain){materials.grain.map=tex;materials.grain.bumpMap=tex;materials.grain.needsUpdate=true;}
      renderedKey='';schedule();
    });
    if(T.GLTFLoader)new T.GLTFLoader().load('/assets/models/warehouse-structure.glb',function(asset){
      warehouseAsset=asset.scene;warehouse();updateStats();
    },undefined,function(){container.dataset.asset='procedural-fallback';});
  }
  function tick(now){
    raf=requestAnimationFrame(tick);if(document.hidden||!container.clientWidth)return;
    controls.update();renderer.render(scene,camera);frameCount++;
    if(now-clockStart>2000){fps=Math.round(frameCount*1000/(now-clockStart));frameCount=0;clockStart=now;container.dataset.fps=fps;}
  }
  function setConfig(c){
    var old=config, next=c||config;
    var changed=['wh_l','wh_w','wh_h'].some(function(k){return next[k]!==old[k];})||
      ['shape_type','length_m','width_m','height_m','diameter_m'].some(function(k){
        return (next.warehouse||{})[k] !== (old.warehouse||{})[k];
      });
    config=next;
    if(changed&&world&&!demoMode){warehouse();home();}
    schedule();
  }
  return {
    init:init,setConfig:setConfig,setPileList:function(v){piles=v||[];schedule();},
    setPoleList:function(v){poles=v||[];schedule();},updatePoles:function(v){poles=v||[];schedule();},
    setSelectedPile:function(v){selected=v;schedule();},setSelectedPole:function(v){selectedPole=v;if(rods)rods.traverse(function(o){if(o.name==='SelectedProbeHalo')o.visible=o.parent.userData.uid===v;});},
    setHeatmap:function(v){heatmap=v;schedule();},setMetric:function(v){metric=v==='rh'?'rh':'temp';schedule();},
    setDemo:function(v){demoMode=!!v;if(world){warehouse();home();}schedule();return demoMode;},
    toggleCut:function(){sliced=!sliced;schedule();return sliced;},
    toggleProbes:function(){probesVisible=!probesVisible;if(rods)rods.visible=probesVisible;return probesVisible;},
    resetView:home,fullscreen:function(){var e=container.parentElement;if(document.fullscreenElement)document.exitFullscreen();else if(e.requestFullscreen)e.requestFullscreen();},
    setDragEnd:function(fn){dragEnd=fn;},setOnPileClick:function(fn){pickPile=fn;},setOnPoleClick:function(fn){pickPole=fn;}
  };
})();
