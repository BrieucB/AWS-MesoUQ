import test from 'node:test';
import assert from 'node:assert/strict';
import {CurveEditor,MAX_POINTS,PLOT,finiteNumber,validateRanges,pointAt,viewPoint,editorMarkup,mountEditor} from '../src/curve-editor.js';
import {parseCurve} from '../src/client.js';
import {curvePlot} from '../src/plots.js';
function dom(){
  const nodes=new Map();
  const node=selector=>{
    if(!nodes.has(selector))nodes.set(selector,{value:'0',textContent:'',innerHTML:'',dataset:{},disabled:false,handlers:{},attributes:{},addEventListener(name,fn){this.handlers[name]=fn;},removeEventListener(name,fn){if(this.handlers[name]===fn)delete this.handlers[name];},setAttribute(name,value){this.attributes[name]=value;},matches(value){return selector.startsWith(value.replace(']','='))||selector===value;}});
    return nodes.get(selector);
  };
  const root=node('root');root.querySelector=node;root.querySelectorAll=()=>['apply-ranges','add-point','undo','clear','download','use'].map(action=>{const n=node(`[data-editor-action="${action}"]`);n.dataset.editorAction=action;return n;});
  for(const key of ['displacementMax','forceMax'])node(`[data-editor-range="${key}"]`).value='1';
  const svg=node('[data-editor-canvas]');svg.getBoundingClientRect=()=>({left:0,top:0,width:640,height:360});svg.focus=()=>{};svg.setPointerCapture=id=>svg.captured=id;svg.hasPointerCapture=id=>svg.captured===id;svg.releasePointerCapture=()=>svg.captured=null;
  const pointer=(type,x,y,extras={})=>svg.handlers[type]?.({pointerId:1,isPrimary:true,button:0,clientX:x,clientY:y,preventDefault(){},...extras});
  const action=(name,force=false)=>{const button=node(`[data-editor-action="${name}"]`);if(force)button.disabled=false;root.handlers.click({target:{closest:()=>button},preventDefault(){}});};
  const keyboard=(key,target=svg,extras={})=>root.handlers.keydown({key,target,preventDefault(){},...extras});
  return {root,node,svg,pointer,action,keyboard};
}
test('both CSV orders preserve finite extremes, signed zero and original nonmonotonic order',()=>{
  const editor=new CurveEditor();editor.setRanges({displacementMin:-Number.MAX_VALUE,displacementMax:Number.MAX_VALUE,forceMin:-Number.MAX_VALUE,forceMax:Number.MAX_VALUE});
  const points=[{displacement:Number.MAX_VALUE,force:-Number.MIN_VALUE},{displacement:-Number.MAX_VALUE,force:Number.MAX_VALUE},{displacement:-0,force:-0},{displacement:Number.MIN_VALUE,force:-Number.MAX_VALUE}];
  points.forEach(point=>editor.add(point));
  for(const order of ['force-displacement','displacement-force'])assert.deepEqual(parseCurve(editor.csv(order),order),points.map(({force,displacement})=>({force,displacement})));
  assert.throws(()=>editor.csv('guessed'),/column order/);
});
test('subnormal and maximal cross-zero axes map corners/center without invalid SVG coordinates',()=>{
  for(const bound of [Number.MIN_VALUE,Number.MAX_VALUE]){
    const ranges=validateRanges({displacementMin:-bound,displacementMax:bound,forceMin:-bound,forceMax:bound});
    assert.deepEqual(pointAt(PLOT.left,PLOT.bottom,ranges),{displacement:-bound,force:-bound});
    assert.deepEqual(pointAt(PLOT.right,PLOT.top,ranges),{displacement:bound,force:bound});
    assert.deepEqual(pointAt(340,162,ranges),{displacement:0,force:0});
    const editor=new CurveEditor();editor.setRanges(ranges);editor.add({displacement:-bound,force:-bound});editor.add({displacement:0,force:0});editor.add({displacement:bound,force:bound});
    const fixture=dom();mountEditor(fixture.root,editor);assert.doesNotMatch(fixture.node('[data-editor-series]').innerHTML,/NaN|Infinity/);
    assert.equal(pointAt(PLOT.left-1,PLOT.bottom,ranges),null);
  }
  assert.deepEqual(viewPoint(42,122,{left:10,top:20,width:320,height:360}),{x:64,y:24});
  assert.equal(viewPoint(0,0,{left:0,top:0,width:0,height:0}),null);
});
test('shared preview keeps finite extreme and subnormal samples at the correct plot coordinates',()=>{
  for(const bound of [Number.MIN_VALUE,1e308,Number.MAX_VALUE]){
    const curve={force:[-bound,0,bound],observed_displacement:[-bound,0,bound]},before=structuredClone(curve);
    const html=curvePlot(curve);
    assert.doesNotMatch(html,/NaN|Infinity/);
    const coordinates=[...html.matchAll(/<circle[^>]*cx="([^"]+)" cy="([^"]+)"/g)].map(([,x,y])=>[Number(x),Number(y)]);
    assert.deepEqual(coordinates,[[56,210],[314,125],[572,40]]);
    assert.deepEqual(curve,before);
  }
  assert.match(curvePlot({force:[2,2],observed_displacement:[3,3]}),/cx="56" cy="210"/);
});
test('invalid values, narrowing axes and insufficient points fail without changing data',()=>{
  for(const value of ['',NaN,Infinity,'0xff','NaN','1e999'])assert.throws(()=>finiteNumber(value),/finite decimal/);
  const editor=new CurveEditor();editor.add({displacement:.8,force:.8});const original=structuredClone(editor.ranges);
  assert.throws(()=>editor.setRanges({...original,forceMax:.5}),/exclude drawn/);assert.deepEqual(editor.ranges,original);
  assert.throws(()=>editor.setRanges({...original,displacementMin:1}),/minimum/);
  assert.throws(()=>editor.add({displacement:2,force:.5}),/outside/);assert.equal(editor.points.length,1);
  assert.throws(()=>editor.csv('force-displacement'),/at least two/);
});
test('cap is exact and undo/clear recover without sorting or resampling',()=>{
  const editor=new CurveEditor();for(let i=0;i<MAX_POINTS;i++)editor.add({displacement:i/999,force:1-i/999});
  assert.equal(parseCurve(editor.csv('force-displacement'),'force-displacement').length,1000);
  assert.throws(()=>editor.add({displacement:.5,force:.25}),/1,000-point limit/);assert.equal(editor.points.length,1000);
  editor.undo();editor.add({displacement:.5,force:.25});assert.equal(editor.points.at(-1).force,.25);
  editor.clear();assert.equal(editor.undo(),false);editor.reset();assert.equal(editor.points.length,0);
});
test('raw coalesced samples deduplicate adjacent events and strokes/cancel/boundary remain separate',()=>{
  const editor=new CurveEditor(),f=dom();mountEditor(f.root,editor);
  f.pointer('pointerdown',202,93);f.pointer('pointermove',478,231,{getCoalescedEvents:()=>[{clientX:202,clientY:93},{clientX:340,clientY:162},{clientX:478,clientY:231}]});f.pointer('pointerup',478,231);
  assert.equal(editor.points.length,3);assert.equal(f.svg.captured,null);
  f.pointer('pointerdown',202,231);f.pointer('pointermove',340,93);f.pointer('pointercancel',340,93);f.pointer('pointermove',478,93);
  assert.equal(editor.points.length,5);assert.equal((f.node('[data-editor-series]').innerHTML.match(/<polyline/g)||[]).length,2);
  f.pointer('pointerdown',300,200);f.pointer('pointermove',900,200);f.pointer('pointermove',400,200);
  assert.equal(editor.points.length,6);assert.equal(f.svg.captured,null);assert.match(f.node('[data-editor-status]').textContent,/outside the plot/);
  f.pointer('pointerdown',400,200,{isPrimary:false});assert.equal(editor.points.length,6);
});
test('click mode, numeric Enter and keyboard cursor/undo are independent of drag sampling',()=>{
  const editor=new CurveEditor(),f=dom();mountEditor(f.root,editor);editor.setMode('points');
  f.pointer('pointerdown',202,93);f.pointer('pointermove',340,162);f.pointer('pointerup',478,231);assert.equal(editor.points.length,1);
  f.node('[data-editor-coordinate="displacement"]').value='.75';f.node('[data-editor-coordinate="force"]').value='.25';f.keyboard('Enter',f.node('[data-editor-coordinate="force"]'));
  assert.deepEqual(editor.points.map(({displacement,force})=>({displacement,force})),[{displacement:.25,force:.75},{displacement:.75,force:.25}]);
  f.node('[data-editor-coordinate="force"]').value='2';f.action('add-point');assert.equal(editor.points.length,2);assert.match(f.node('[data-editor-status]').textContent,/outside/);
  f.keyboard('ArrowRight');f.keyboard('Enter');assert.equal(editor.points.length,3);f.keyboard('z',f.svg,{ctrlKey:true});assert.equal(editor.points.length,2);f.keyboard('Escape');assert.equal(f.svg.captured,null);
});
test('all drawing actions ignore in-flight operation events including programmatic changes',()=>{
  const editor=new CurveEditor(),f=dom();let blocked=false,uses=0,downloads=0;editor.add({displacement:.1,force:.2});editor.add({displacement:.3,force:.4});
  mountEditor(f.root,editor,{blocked:()=>blocked,onUse:()=>uses++,onDownload:()=>downloads++});const before=JSON.stringify(editor);blocked=true;
  for(const action of ['apply-ranges','add-point','undo','clear','download','use'])f.action(action,true);
  f.pointer('pointerdown',400,200);f.keyboard('Enter');f.keyboard('z',f.svg,{ctrlKey:true});const mode=f.node('[data-editor-mode]');mode.value='points';mode.matches=()=>true;f.root.handlers.change({target:mode});
  assert.equal(JSON.stringify(editor),before);assert.equal(uses,0);assert.equal(downloads,0);
});
test('raw stroke stops with clear cap feedback and markup is self-only CSP compatible',()=>{
  const editor=new CurveEditor(),f=dom();mountEditor(f.root,editor);f.pointer('pointerdown',PLOT.left,PLOT.bottom);
  f.pointer('pointermove',PLOT.right,PLOT.top,{getCoalescedEvents:()=>Array.from({length:1200},(_,i)=>({clientX:PLOT.left+(PLOT.right-PLOT.left)*(i+1)/1200,clientY:PLOT.bottom-(PLOT.bottom-PLOT.top)*(i+1)/1200}))});
  assert.equal(editor.points.length,1000);assert.equal(f.svg.captured,null);assert.match(f.node('[data-editor-status]').textContent,/1,000-point limit/);assert.equal(parseCurve(editor.csv('force-displacement'),'force-displacement').length,1000);
  const html=editorMarkup(editor,{forceUnit:'uN',displacementUnit:'um'});assert.doesNotMatch(html,/<script|\sstyle=|\son\w+=/i);assert.match(html,/data-editor-plot/);
  const hostile=editorMarkup(editor,{forceUnit:'<img onerror="unsafe">',displacementUnit:'um'});assert.doesNotMatch(hostile,/<img/);assert.match(hostile,/&lt;img onerror=&quot;unsafe&quot;&gt;/);
});
