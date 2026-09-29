/* Bake the exact runtime mesh factory, without WebGL or production data access. */
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const root=path.resolve(__dirname,'..'),T=require('../station/web/assets/vendor/three.min.js');
const G=require('../station/web/assets/js/warehouse-geometry.js');
const context2d=new Proxy({}, {get:(_,key)=>key==='measureText'?()=>({width:120}):()=>{},set:()=>true});
function element(){return {dataset:{},style:{},children:[],hidden:false,clientWidth:1200,clientHeight:720,
  appendChild(e){this.children.push(e);},setAttribute(){},addEventListener(){},getContext(){return context2d;},
  getBoundingClientRect(){return {x:0,y:0,width:1200,height:720};},querySelector(){return element();}};}
function runtime() {
let captured,callbacks=[],container=element();
T.WebGLRenderer=class {constructor(){this.domElement=element();this.shadowMap={};this.capabilities={getMaxAnisotropy:()=>4};}
  setPixelRatio(){} setSize(){} render(scene){captured=scene;}};
T.OrbitControls=class {constructor(){this.target=new T.Vector3();}update(){} addEventListener(){}};
T.TextureLoader=null;
const sandbox={window:{THREE:T,WarehouseGeometry:G},document:{createElement:element,getElementById:()=>container,hidden:false},
  ResizeObserver:class{observe(){}},performance:{now:()=>100},requestAnimationFrame:fn=>{callbacks.push(fn);return 1;},cancelAnimationFrame(){},console};
vm.runInNewContext(fs.readFileSync(path.join(root,'station/web/assets/js/warehouse-scene.js'),'utf8'),sandbox);
sandbox.window.Scene3D.init('scene');
return {api:sandbox.window.Scene3D,container,get scene(){return captured;},flush(){const queued=callbacks;callbacks=[];queued.forEach(fn=>fn(100));captured.updateMatrixWorld(true);}};
}
module.exports={runtime};
if(require.main===module){
const r=runtime();r.api.setDemo(true);r.flush();
const meshes=[];
r.scene.traverse(o=>{
  if(!o.isMesh)return;
  let ancestor=o;while(ancestor.parent&&ancestor.parent!==r.scene)ancestor=ancestor.parent;
  const g=o.geometry,m=o.material,record={name:o.name||'Mesh',group:ancestor.name,
    positions:Array.from(g.attributes.position.array),indices:g.index?Array.from(g.index.array):null,
    colors:g.attributes.color?Array.from(g.attributes.color.array):null,uv:g.attributes.uv?Array.from(g.attributes.uv.array):null,
    matrix:o.matrixWorld.toArray(),material:{name:m===undefined?'':m.name,color:m.color?m.color.toArray():[1,1,1],roughness:m.roughness||.8,metalness:m.metalness||0}};
  if(o.isInstancedMesh)record.instances=Array.from(o.instanceMatrix.array.slice(0,o.count*16));
  meshes.push(record);
});
const out=path.join(root,'.build/warehouse-scene-input.json');fs.mkdirSync(path.dirname(out),{recursive:true});
fs.writeFileSync(out,JSON.stringify({source:'warehouse-scene.js; synthetic demonstration only',meshes}));
console.log(JSON.stringify({output:out,meshCount:meshes.length,bytes:fs.statSync(out).size}));
}
