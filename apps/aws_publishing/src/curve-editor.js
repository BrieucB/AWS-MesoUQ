import {escapeHtml as h} from './plots.js';

export const MAX_POINTS=1000;
export const PLOT=Object.freeze({left:64,right:616,top:24,bottom:300,width:640,height:360});
const DEFAULT_RANGES=Object.freeze({displacementMin:0,displacementMax:1,forceMin:0,forceMax:1});
const decimal=/^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$/;
const numberText=value=>Object.is(value,-0)?'-0':String(value);
export function finiteNumber(value,label='Value'){
  if(typeof value==='string'){
    if(!decimal.test(value.trim()))throw Error(`${label} must be a finite decimal number.`);
    value=Number(value);
  }
  if(typeof value!=='number'||!Number.isFinite(value))throw Error(`${label} must be a finite decimal number.`);
  return value;
}
export function validateRanges(value){
  const ranges=Object.fromEntries(Object.keys(DEFAULT_RANGES).map(key=>[key,finiteNumber(value[key],'Axis bound')]));
  if(ranges.displacementMin>=ranges.displacementMax||ranges.forceMin>=ranges.forceMax)throw Error('Each axis minimum must be smaller than its maximum.');
  return ranges;
}
// Avoid overflowing max-min for finite axes that cross zero near numeric limits.
function interpolate(min,max,t){return t===0?min:t===1?max:min<0&&max>0?min*(1-t)+max*t:min+(max-min)*t;}
function fraction(value,min,max){const span=max-min;return Number.isFinite(span)?(value-min)/span:(value/2-min/2)/(max/2-min/2);}
function inside(point,ranges){return point.displacement>=ranges.displacementMin&&point.displacement<=ranges.displacementMax&&point.force>=ranges.forceMin&&point.force<=ranges.forceMax;}
export function pointAt(x,y,ranges){
  ranges=validateRanges(ranges);
  if(!Number.isFinite(x)||!Number.isFinite(y)||x<PLOT.left||x>PLOT.right||y<PLOT.top||y>PLOT.bottom)return null;
  return {displacement:interpolate(ranges.displacementMin,ranges.displacementMax,(x-PLOT.left)/(PLOT.right-PLOT.left)),force:interpolate(ranges.forceMin,ranges.forceMax,(PLOT.bottom-y)/(PLOT.bottom-PLOT.top))};
}
export function viewPoint(clientX,clientY,rect){
  const scale=Math.min(rect.width/PLOT.width,rect.height/PLOT.height);
  if(!Number.isFinite(scale)||scale<=0)return null;
  // SVG uses xMidYMid meet; account for the empty margins when aspect ratios differ.
  return {x:(clientX-rect.left-(rect.width-PLOT.width*scale)/2)/scale,y:(clientY-rect.top-(rect.height-PLOT.height*scale)/2)/scale};
}
export class CurveEditor {
  constructor(){this.reset();}
  reset(){this.points=[];this.ranges={...DEFAULT_RANGES};this.mode='freehand';this.sequence=0;}
  stroke(){return ++this.sequence;}
  setMode(mode){if(!['freehand','points'].includes(mode))throw Error('Choose freehand or click-to-add mode.');this.mode=mode;}
  setRanges(value){
    const next=validateRanges(value);
    if(this.points.some(point=>!inside(point,next)))throw Error('The new axes exclude drawn points. Clear the curve or widen the ranges.');
    this.ranges=next;
  }
  add(point,stroke=this.stroke()){
    const next={displacement:finiteNumber(point.displacement,'Displacement'),force:finiteNumber(point.force,'Force'),stroke};
    if(!inside(next,this.ranges))throw Error('The point is outside the current axis ranges.');
    const last=this.points.at(-1);
    if(last&&last.displacement===next.displacement&&last.force===next.force)return false;
    if(this.points.length>=MAX_POINTS)throw Error('The 1,000-point limit is reached. Undo a point or clear the curve to continue.');
    this.points.push(next);return true;
  }
  undo(){return Boolean(this.points.pop());}
  clear(){const changed=this.points.length>0;this.points=[];this.sequence=0;return changed;}
  csv(order){
    if(!['force-displacement','displacement-force'].includes(order))throw Error('Choose an explicit CSV column order.');
    if(this.points.length<2)throw Error('Draw or add at least two distinct points.');
    if(this.points.length>MAX_POINTS)throw Error('Release 1 accepts at most 1,000 curve points.');
    const fields=order==='force-displacement'?['force','displacement']:['displacement','force'];
    return fields.join(',')+'\n'+this.points.map(point=>fields.map(field=>numberText(finiteNumber(point[field]))).join(',')).join('\n')+'\n';
  }
}

export function editorMarkup(editor,{forceUnit,displacementUnit,disabled=false}){
  const lock=disabled?'disabled':'';
  const bound=(key,label)=>`<label>${label}<input type="number" step="any" data-editor-range="${key}" value="${h(numberText(editor.ranges[key]))}" ${lock}></label>`;
  return `<section class="curve-editor" data-curve-editor aria-label="Draw a curve"><h3>Or draw a curve</h3><p>Draw with a mouse, trackpad or touch, or add individual points. These are drawn inputs, not measured data. Points stay in input order, without smoothing or resampling.</p><div class="form-grid"><label>Drawing mode<select data-editor-mode ${lock}><option value="freehand" ${editor.mode==='freehand'?'selected':''}>Freehand</option><option value="points" ${editor.mode==='points'?'selected':''}>Click to add points</option></select></label>${bound('displacementMin',`X minimum · displacement (${h(displacementUnit)})`)}${bound('displacementMax',`X maximum · displacement (${h(displacementUnit)})`)}${bound('forceMin',`Y minimum · force (${h(forceUnit)})`)}${bound('forceMax',`Y maximum · force (${h(forceUnit)})`)}</div><button type="button" data-editor-action="apply-ranges" ${lock}>Apply axis ranges</button><p id="curve-drawing-help">Drag in freehand mode or click in point mode. Separate strokes stay separate on screen. With the plot focused, use arrow keys to move the cursor, Enter or Space to add, Ctrl/⌘ Z to undo, and Escape to end a stroke. Numeric fields below also work with a keyboard.</p><svg class="curve-editor-canvas" data-editor-canvas viewBox="0 0 640 360" tabindex="${disabled?'-1':'0'}" role="img" aria-label="Drawn curve: displacement on X and force on Y" aria-describedby="curve-drawing-help" aria-disabled="${disabled}"><rect class="drawing-surface" data-editor-plot x="64" y="24" width="552" height="276"/><g data-editor-series></g><g class="drawing-cursor" data-editor-cursor-mark><path d="M -7 0 H 7 M 0 -7 V 7"/></g><text x="340" y="350" text-anchor="middle">Displacement (${h(displacementUnit)})</text><text transform="rotate(-90 15 162)" x="15" y="162" text-anchor="middle">Force (${h(forceUnit)})</text><text x="64" y="325" data-editor-tick="displacementMin"></text><text x="616" y="325" text-anchor="end" data-editor-tick="displacementMax"></text><text x="55" y="300" text-anchor="end" data-editor-tick="forceMin"></text><text x="55" y="29" text-anchor="end" data-editor-tick="forceMax"></text></svg><p><output data-editor-count>${editor.points.length}</output> / 1,000 points. <span data-editor-cursor></span></p><div class="form-grid"><label>Displacement (${h(displacementUnit)})<input type="number" step="any" data-editor-coordinate="displacement" value="0" ${lock}></label><label>Force (${h(forceUnit)})<input type="number" step="any" data-editor-coordinate="force" value="0" ${lock}></label></div><div class="drawing-actions"><button type="button" data-editor-action="add-point" ${lock}>Add numeric point</button><button type="button" data-editor-action="undo" ${lock}>Undo point</button><button type="button" data-editor-action="clear" ${lock}>Clear drawing</button><button type="button" data-editor-action="download" ${lock}>Download drawn CSV</button><button type="button" class="secondary" data-editor-action="use" ${lock}>Use drawn curve</button></div><p role="status" aria-live="polite" data-editor-status>Use drawn curve to preview, upload and verify through the same CSV checks.</p></section>`;
}

export function mountEditor(root,editor,{blocked=()=>false,order=()=> 'force-displacement',onChange=()=>{},onUse=()=>{},onDownload=()=>{}}={}){
  const svg=root.querySelector('[data-editor-canvas]');
  const find=selector=>root.querySelector(selector);
  const removers=[];
  let active=null,cursor={x:(PLOT.left+PLOT.right)/2,y:(PLOT.top+PLOT.bottom)/2};
  const listen=(target,name,fn)=>{target.addEventListener(name,fn);removers.push(()=>target.removeEventListener(name,fn));};
  const message=text=>{find('[data-editor-status]').textContent=text;};
  function finish(){
    const pointer=active;active=null;
    if(pointer&&svg.hasPointerCapture?.(pointer.id))svg.releasePointerCapture(pointer.id);
  }
  function paint(){
    find('[data-editor-count]').textContent=String(editor.points.length);
    const groups=new Map();
    for(const point of editor.points){if(!groups.has(point.stroke))groups.set(point.stroke,[]);groups.get(point.stroke).push(point);}
    const xy=point=>({x:PLOT.left+fraction(point.displacement,editor.ranges.displacementMin,editor.ranges.displacementMax)*(PLOT.right-PLOT.left),y:PLOT.bottom-fraction(point.force,editor.ranges.forceMin,editor.ranges.forceMax)*(PLOT.bottom-PLOT.top)});
    find('[data-editor-series]').innerHTML=[...groups.values()].map(points=>`<polyline class="drawing-line" points="${points.map(point=>{const p=xy(point);return p.x+','+p.y;}).join(' ')}"/>`).join('')+editor.points.map(point=>{const p=xy(point);return `<circle class="drawing-point" cx="${p.x}" cy="${p.y}" r="2.5"/>`;}).join('');
    find('[data-editor-cursor-mark]').setAttribute('transform',`translate(${cursor.x} ${cursor.y})`);
    const point=pointAt(cursor.x,cursor.y,editor.ranges);
    find('[data-editor-cursor]').textContent=`Cursor: displacement ${numberText(point.displacement)}, force ${numberText(point.force)}.`;
    for(const key of Object.keys(DEFAULT_RANGES))find(`[data-editor-tick="${key}"]`).textContent=editor.ranges[key].toPrecision(4);
    for(const button of root.querySelectorAll('[data-editor-action]'))button.disabled=blocked()||(['use','download'].includes(button.dataset.editorAction)&&editor.points.length<2)||(['undo','clear'].includes(button.dataset.editorAction)&&!editor.points.length);
  }
  function changed(){onChange();paint();}
  function append(point,stroke){
    if(blocked())return false;
    try{
      if(editor.add(point,stroke))changed();
      if(editor.points.length===MAX_POINTS){message('The 1,000-point limit is reached. Undo a point or clear the curve to continue.');finish();return false;}
      return true;
    }catch(error){message(error.message);finish();return false;}
  }
  function position(event){
    const matrix=svg.getScreenCTM?.();
    if(matrix&&svg.createSVGPoint){const point=svg.createSVGPoint();point.x=event.clientX;point.y=event.clientY;return point.matrixTransform(matrix.inverse());}
    return viewPoint(event.clientX,event.clientY,svg.getBoundingClientRect());
  }
  function sample(event,stroke){
    const p=position(event),point=p&&pointAt(p.x,p.y,editor.ranges);
    if(!point){finish();message('Stroke ended outside the plot. Start another stroke inside.');return false;}
    cursor={x:p.x,y:p.y};return append(point,stroke);
  }
  listen(svg,'pointerdown',event=>{
    if(blocked()||active||event.isPrimary===false||event.button!==0)return;
    const p=position(event);
    if(!p||!pointAt(p.x,p.y,editor.ranges))return;
    event.preventDefault();svg.focus();active={id:event.pointerId,stroke:editor.stroke(),mode:editor.mode};
    svg.setPointerCapture?.(event.pointerId);
    if(sample(event,active.stroke)&&active)message(editor.mode==='freehand'?'Drawing a stroke.':'Point added.');
  });
  listen(svg,'pointermove',event=>{
    if(blocked()){finish();return;}
    if(!active||event.pointerId!==active.id||active.mode!=='freehand')return;
    event.preventDefault();
    const samples=event.getCoalescedEvents?.()||[];
    for(const sampleEvent of [...samples,event]){
      if(!active||!sample(sampleEvent,active.stroke))break;
    }
  });
  listen(svg,'pointerup',event=>{
    if(!active||event.pointerId!==active.id)return;
    if(!blocked()&&active.mode==='freehand')sample(event,active.stroke);
    finish();
  });
  for(const name of ['pointercancel','lostpointercapture'])listen(svg,name,event=>{if(active&&event.pointerId===active.id){finish();message('Stroke ended; collected points are kept.');}});
  function action(name){
    if(blocked())return;
    finish();
    try{
      if(name==='apply-ranges'){
        editor.setRanges(Object.fromEntries(Object.keys(DEFAULT_RANGES).map(key=>[key,find(`[data-editor-range="${key}"]`).value])));
        paint();message('Axis ranges updated. Existing point values are unchanged.');
      }else if(name==='add-point'){
        if(append(Object.fromEntries(['displacement','force'].map(key=>[key,find(`[data-editor-coordinate="${key}"]`).value])),editor.stroke()))message('Numeric point added.');
      }else if(name==='undo'){
        if(editor.undo())changed();message('Last point undone.');
      }else if(name==='clear'){
        if(editor.clear())changed();message('Drawing cleared.');
      }else if(name==='download')onDownload(editor.csv(order()));
      else if(name==='use')onUse(editor.csv(order()));
    }catch(error){message(error.message);}
  }
  listen(root,'click',event=>{const button=event.target.closest('[data-editor-action]');if(button&&!button.disabled){event.preventDefault();action(button.dataset.editorAction);}});
  listen(root,'change',event=>{if(event.target.matches('[data-editor-mode]')){if(blocked())return;finish();try{editor.setMode(event.target.value);message(editor.mode==='freehand'?'Freehand mode.':'Click-to-add mode.');}catch(error){event.target.value=editor.mode;message(error.message);}}});
  listen(root,'keydown',event=>{
    if(blocked())return;
    if(event.key==='Enter'&&event.target.matches('[data-editor-coordinate]')){event.preventDefault();action('add-point');return;}
    if(event.target!==svg)return;
    if((event.ctrlKey||event.metaKey)&&event.key.toLowerCase()==='z'){event.preventDefault();action('undo');return;}
    if(event.key==='Escape'){event.preventDefault();finish();message('Stroke ended; collected points are kept.');return;}
    if(event.key==='Enter'||event.key===' '){event.preventDefault();append(pointAt(cursor.x,cursor.y,editor.ranges),editor.stroke());return;}
    const step=event.shiftKey?0.1:0.01;
    if(['ArrowLeft','ArrowRight','ArrowUp','ArrowDown'].includes(event.key)){
      event.preventDefault();
      if(event.key==='ArrowLeft')cursor.x=Math.max(PLOT.left,cursor.x-(PLOT.right-PLOT.left)*step);
      if(event.key==='ArrowRight')cursor.x=Math.min(PLOT.right,cursor.x+(PLOT.right-PLOT.left)*step);
      if(event.key==='ArrowUp')cursor.y=Math.max(PLOT.top,cursor.y-(PLOT.bottom-PLOT.top)*step);
      if(event.key==='ArrowDown')cursor.y=Math.min(PLOT.bottom,cursor.y+(PLOT.bottom-PLOT.top)*step);
      paint();
    }
  });
  paint();
  return {destroy(){finish();for(const remove of removers)remove();}};
}
