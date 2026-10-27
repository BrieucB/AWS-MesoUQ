// Explicit local visual-test server. Never included in the production build.
import http from 'node:http';
import {readFile} from 'node:fs/promises';
import path from 'node:path';
if(process.env.MESOUQ_VISUAL_FIXTURES!=='1')throw Error('Set MESOUQ_VISUAL_FIXTURES=1 to enable synthetic local visual fixtures.');
const root=path.resolve(new URL('..',import.meta.url).pathname);
const types={'.html':'text/html','.js':'text/javascript','.css':'text/css','.json':'application/json'};
http.createServer(async(req,res)=>{
  try{
    res.setHeader('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'");
    const url=new URL(req.url,'http://127.0.0.1:4173');
    if(url.pathname==='/__fixtures__/workspace'){
      let html=await readFile(path.join(root,'index.html'),'utf8');
      html=html.replace('href="src/style.css"','href="/src/style.css"').replace('src="src/app.js"','src="/test/browser-fixture.js"');
      res.setHeader('Content-Type','text/html');res.setHeader('Cache-Control','no-store');res.end(html);return;
    }
    const rel=url.pathname==='/'?'index.html':decodeURIComponent(url.pathname).slice(1);
    const file=path.resolve(root,rel==='runtime-config.json'?'public/runtime-config.json':rel);
    if(!file.startsWith(root+path.sep))throw Error('Invalid path');
    res.setHeader('Content-Type',types[path.extname(file)]||'application/octet-stream');res.setHeader('Cache-Control','no-store');res.end(await readFile(rel==='presets/indentation-v1.json'?path.resolve(root,'../../src/aws_mesouq/presets/indentation-v1.json'):file));
  }catch{res.writeHead(404);res.end('Not found');}
}).listen(Number(process.env.MESOUQ_VISUAL_PORT||4173),'127.0.0.1',()=>console.log('TEST ONLY synthetic fixtures listening on loopback port '+(process.env.MESOUQ_VISUAL_PORT||4173)));
