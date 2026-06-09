import * as esbuild from 'esbuild-wasm';
import { fileURLToPath } from 'url';
import { dirname, join } from 'path';

const __dirname = dirname(fileURLToPath(import.meta.url));
await esbuild.initialize({
  wasmURL: new URL('./node_modules/esbuild-wasm/esbuild.wasm', import.meta.url).href,
});

await esbuild.build({
  entryPoints: [join(__dirname, 'src/main.jsx')],
  outfile: join(__dirname, '../../static/dashboard-bg/dashboard-bg.js'),
  bundle: true,
  format: 'esm',
  jsx: 'automatic',
  minify: true,
});

console.log('Built dashboard-bg.js');
