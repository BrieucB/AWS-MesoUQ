// The operator and browser share this release preset; no scientific defaults live here.
export function configurationFor(preset, diameter) {
  if (preset?.schema_version !== 1 || preset.id !== 'indentation-sonovue-v1' || !preset.configuration || !preset.supported_diameters_um?.includes(Number(diameter))) throw Error('Approved indentation preset is unavailable or diameter is unsupported.');
  return {...structuredClone(preset.configuration), emb_diameters:[Number(diameter)]};
}
export function approvedModel(models, preset, modelVersion) {
  const released=models.filter(model=>model.status==='released' && model.provenance?.pipeline_binding?.id===preset.scientific_profile && (!modelVersion || (model.model_version||model.version||model.id)===modelVersion));
  if(released.length!==1) throw Error('The project must have exactly one approved indentation model. Ask the operator to check its release configuration.');
  return released[0].model_version||released[0].version||released[0].id;
}
export function outputArtifacts(result) {
  const found=new Map();
  function visit(value) {
    if(!value || typeof value!=='object')return;
    if(typeof value.id==='string' && typeof value.name==='string' && typeof value.sha256==='string')found.set(value.id,value);
    for(const child of Object.values(value))if(child && typeof child==='object')visit(child);
  }
  visit(result);return [...found.values()];
}
