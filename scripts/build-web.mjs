import {copyFileSync,mkdirSync} from 'node:fs';
import {spawnSync} from 'node:child_process';
mkdirSync('web/static',{recursive:true});
const result=spawnSync(process.execPath,['node_modules/@tailwindcss/cli/dist/index.mjs','-i','web/styles/ui.css','-o','web/static/ui.css','--minify'],{stdio:'inherit'});
if(result.status!==0) process.exit(result.status || 1);
copyFileSync('node_modules/htmx.org/dist/htmx.min.js','web/static/htmx.min.js');
copyFileSync('node_modules/echarts/dist/echarts.min.js','web/static/echarts.min.js');
