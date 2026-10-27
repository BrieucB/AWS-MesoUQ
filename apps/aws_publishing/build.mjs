import { cp, mkdir, rm } from 'node:fs/promises';
await rm(new URL('./dist', import.meta.url), {recursive:true, force:true});
await mkdir(new URL('./dist/presets', import.meta.url),{recursive:true});
for (const name of ['index.html','src','public']) await cp(new URL(name,import.meta.url),new URL(`dist/${name === 'public' ? '.' : name}`,import.meta.url),{recursive:true});
await cp(new URL('../../src/aws_mesouq/presets/indentation-v1.json',import.meta.url),new URL('./dist/presets/indentation-v1.json',import.meta.url));
console.log('Built private static portal in dist/; supply runtime-config.json during deployment.');
