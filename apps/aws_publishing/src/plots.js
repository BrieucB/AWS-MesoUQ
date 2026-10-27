export const escapeHtml=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function fraction(value,min,max){
  if(min===max)return 0;
  const span=max-min;
  return Number.isFinite(span)?(value-min)/span:(value/2-min/2)/(max/2-min/2);
}
export function curvePlot({force,observed_displacement=[],median=[],lower=[],upper=[],force_unit='',displacement_unit='',dataset_id='Curve'}) {
  if(!Array.isArray(force)||!force.length)return '<p>No curve samples available.</p>';
  const all=[...observed_displacement,...median,...lower,...upper];
  if([...force,...all].some(x=>typeof x!=='number'||!Number.isFinite(x))||[observed_displacement,median,lower,upper].some(a=>a.length&&a.length!==force.length))return '<p>Curve data could not be plotted: invalid or mismatched samples. Download the raw result for inspection.</p>';
  const xmin=force.reduce((a,b)=>Math.min(a,b),Infinity),xmax=force.reduce((a,b)=>Math.max(a,b),-Infinity),ymin=all.reduce((a,b)=>Math.min(a,b),Infinity),ymax=all.reduce((a,b)=>Math.max(a,b),-Infinity);
  const indices=Array.from({length:Math.min(force.length,1000)},(_,i)=>Math.round(i*(force.length-1)/(Math.min(force.length,1000)-1||1)));
  if(!all.length)return '<p>No displacement samples available.</p>';
  const x=v=>56+fraction(v,xmin,xmax)*516,y=v=>210-fraction(v,ymin,ymax)*170;
  const points=values=>indices.map(i=>`${x(force[i])},${y(values[i])}`).join(' ');
  const band=lower.length&&upper.length?`<polygon class="band" points="${points(lower)} ${indices.map(i=>`${x(force[i])},${y(upper[i])}`).reverse().join(' ')}"/>`:'';
  return `<svg class="chart" viewBox="0 0 610 260" role="img" aria-label="${escapeHtml(dataset_id)}: displacement against force, with reported uncertainty"><line class="axis" x1="56" y1="210" x2="572" y2="210"/><line class="axis" x1="56" y1="40" x2="56" y2="210"/>${band}${median.length?`<polyline class="fit" points="${points(median)}"/>`:''}${(observed_displacement.length?indices:[]).map(i=>`<circle class="observed" cx="${x(force[i])}" cy="${y(observed_displacement[i])}" r="2.6"/>`).join('')}<text x="56" y="228">${escapeHtml(xmin.toPrecision(4))}</text><text x="530" y="228">${escapeHtml(xmax.toPrecision(4))}</text><text x="6" y="30">${escapeHtml(ymax.toPrecision(4))}</text><text x="6" y="205">${escapeHtml(ymin.toPrecision(4))}</text><text x="250" y="250">Force (${escapeHtml(force_unit)})</text><text x="115" y="24">Displacement (${escapeHtml(displacement_unit)})</text></svg>${force.length>1000?'<p class="muted">Display sampled to 1,000 points; the original artifact and result retain every sample.</p>':''}`;
}
export function intervalPlot(parameters){
  if(!parameters?.length)return '<p>No parameter intervals reported.</p>';
  return parameters.map(p=>{
    if(![p.lower,p.median,p.upper].every(Number.isFinite)||p.lower>p.median||p.median>p.upper)return `<p>${escapeHtml(p.name)}: interval unavailable</p>`;
    const range=p.upper-p.lower,cx=range?80+(p.median-p.lower)/range*440:300;
    return `<div class="interval"><strong>${escapeHtml(p.name)} <small>${escapeHtml(p.unit)}</small></strong><svg viewBox="0 0 600 60" role="img" aria-label="${escapeHtml(p.name)}: lower ${p.lower}, median ${p.median}, upper ${p.upper} ${escapeHtml(p.unit)}"><line class="uncertainty" x1="${range?80:300}" y1="20" x2="${range?520:300}" y2="20"/><circle class="observed" cx="${cx}" cy="20" r="5"/><text x="4" y="52">${escapeHtml(p.lower.toPrecision(5))}</text><text x="240" y="52">${escapeHtml(p.median.toPrecision(5))}</text><text x="490" y="52">${escapeHtml(p.upper.toPrecision(5))}</text></svg></div>`;
  }).join('');
}
