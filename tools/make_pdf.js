// Guide PDF from docs/guide/guide.html (Playwright / Chromium print).
const { chromium } = require('playwright');
const path = require('path');
const DOCS = path.join(__dirname, '..', 'docs');
(async () => {
  const b = await chromium.launch();
  const p = await b.newPage();
  await p.goto('file://' + path.join(DOCS, 'guide', 'guide.html'));
  await p.waitForTimeout(800);
  await p.pdf({
    path: path.join(DOCS, 'Light-Bridge-Guide.pdf'),
    format: 'A4', printBackground: true, preferCSSPageSize: true,
    displayHeaderFooter: true,
    headerTemplate: '<div></div>',
    footerTemplate: `<div style="width:100%;font-family:Inter,sans-serif;font-size:7.5pt;color:#7a868d;padding:0 17mm;display:flex;justify-content:space-between">
      <span>Light Bridge · Guide</span><span><span class="pageNumber"></span> / <span class="totalPages"></span></span></div>`,
  });
  await b.close();
  console.log('pdf ok');
})();
