import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {configurationFor,approvedModel,outputArtifacts} from '../src/defaults.js';
const preset=JSON.parse(await readFile(new URL('../../../src/aws_mesouq/presets/indentation-v1.json',import.meta.url)));
test('browser clones shared preset exactly, changing only the supported diameter',()=>{
  for(const d of [3.2,3.4,5.8])assert.deepEqual(configurationFor(preset,d),{...preset.configuration,emb_diameters:[d]});
  const c=configurationFor(preset,3.2);c.prior_ka[0]=0;assert.equal(preset.configuration.prior_ka[0],617.0581351146693);
  assert.throws(()=>configurationFor(preset,4),/unsupported/);
  assert.equal(preset.configuration.interval_probability,.9);
  assert.equal(preset.configuration.resonance.evaluator.mode,'artifact_polynomial_bank');
});
test('operator model selection fails closed for missing or ambiguous releases and accepts an explicit pin',()=>{
  const a={model_version:'a',status:'released',provenance:{pipeline_binding:{id:preset.scientific_profile}}};
  const b={...a,model_version:'b'};
  assert.equal(approvedModel([a,{...b,status:'candidate'}],preset),'a');
  for(const models of [[],[a,b],[{...a,provenance:{}}]])assert.throws(()=>approvedModel(models,preset),/exactly one/);
  assert.equal(approvedModel([a,b],preset,'b'),'b');assert.throws(()=>approvedModel([a,b],preset,'absent'),/exactly one/);
});
test('output downloads use immutable API artifact IDs, ignoring private path strings',()=>{
  const artifact={id:'a',name:'posterior.csv',sha256:'abc'};
  assert.deepEqual(outputArtifacts({publication_files:[artifact,artifact],private_path:'/private/results/secret.csv',nested:{path:'s3://private'}}),[artifact]);
});
