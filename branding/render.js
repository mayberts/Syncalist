// node render.js <svg> <out.png> <size>
const { chromium } = require('playwright'); const fs = require('fs');
(async () => {
  const [svg, out, size] = process.argv.slice(2); const n = parseInt(size);
  const b = await chromium.launch(); const p = await b.newPage({viewport:{width:n,height:n}, deviceScaleFactor:1});
  await p.setContent(`<html><body style="margin:0;background:transparent">${fs.readFileSync(svg,'utf8').replace('<svg ', `<svg width="${n}" height="${n}" `)}</body></html>`);
  await p.screenshot({path: out, omitBackground: true, clip:{x:0,y:0,width:n,height:n}}); await b.close();
})();
