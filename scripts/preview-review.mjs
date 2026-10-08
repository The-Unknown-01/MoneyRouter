// Local, read-only visual QA; never connects to the agent or a real account.
import http from 'node:http';
import {readFile} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import path from 'node:path';
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const mime={'.css':'text/css','.js':'text/javascript','.html':'text/html'};
const server=http.createServer(async(req,res)=>{
 const pathname=new URL(req.url,'http://localhost').pathname;
 const file=pathname==='/review-preview.html'?path.join(root,'docs/acceptance/review-preview.html'):pathname.startsWith('/static/')&&/^[\w.-]+$/.test(pathname.slice(8))?path.join(root,'web/static',pathname.slice(8)):null;
 if(!file){res.writeHead(404);res.end();return;}
 try{const bytes=await readFile(file);res.writeHead(200,{'Content-Type':mime[path.extname(file)]||'application/octet-stream','Cache-Control':'no-store','Content-Security-Policy':"default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; img-src 'self' data:"});res.end(bytes);}catch{res.writeHead(404);res.end();}
});
server.listen(0,'127.0.0.1',()=>process.stdout.write('Review preview: http://127.0.0.1:'+server.address().port+'/review-preview.html\n'));

