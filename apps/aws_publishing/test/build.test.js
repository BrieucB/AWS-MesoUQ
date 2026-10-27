import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile,readdir} from 'node:fs/promises';
test('self-contained build carries the identical protected preset and excludes synthetic fixtures',async()=>{
  await import('../build.mjs');
  const root=new URL('../dist/',import.meta.url);
  assert.equal(await readFile(new URL('presets/indentation-v1.json',root),'utf8'),await readFile(new URL('../../../src/aws_mesouq/presets/indentation-v1.json',import.meta.url),'utf8'));
  assert.ok(!(await readdir(root)).includes('test'));
  const html=await readFile(new URL('index.html',root),'utf8');const app=await readFile(new URL('src/app.js',root),'utf8');
  assert.doesNotMatch(html,/<script(?![^>]*\bsrc=)|\sstyle=|\son\w+=/i);
  assert.doesNotMatch(app,/\sstyle=|<script|localStorage|sessionStorage\.setItem/);
  assert.match(app,/credentials:'same-origin',redirect:'error'/);
  const config=JSON.parse(await readFile(new URL('runtime-config.json',root)));assert.equal(config.apiBaseUrl,'');assert.equal(config.clientId,'');
});
