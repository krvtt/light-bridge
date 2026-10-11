// Screenshots for the guide (Playwright). Start tools/run_dev.py --demo --port 8099 first.
const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');
const OUT = path.join(__dirname, '..', 'docs', 'guide', 'img');
const FLAG = process.env.LB_FLAGDIR;   // same folder as given to tools/run_dev.py
const U = process.env.LB_URL || 'http://localhost:8099';
fs.mkdirSync(OUT, { recursive: true });
(async () => {
  const b = await chromium.launch(); const errors = [];
  const H = { 'Content-Type': 'application/json' };
  const phone = await b.newContext({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, colorScheme: 'light', isMobile: true, hasTouch: true });
  const p = await phone.newPage(); p.on('pageerror', e => errors.push(e.message)); p.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });
  const shot = async (name, full) => { await p.screenshot({ path: `${OUT}/${name}.png`, fullPage: !!full }); };
  await p.goto(U + '/#/'); await p.waitForTimeout(10000);   // past the 'steady link' wait of the demo lights
  // 1. new light found
  await p.evaluate(() => window.scrollTo(0, 0));
  await shot('phone-new');
  await p.click('[data-act=adopt]'); await p.waitForTimeout(500);
  await p.fill('#adName', ''); await p.fill('#adRoom', '');
  await shot('phone-adopt');
  await p.click('#sheetBody [data-a=cancel]'); await p.waitForTimeout(300);
  await p.click('[data-act=ignore]'); await p.waitForTimeout(400); await p.click('#sheetBody [data-a=yes]'); await p.waitForTimeout(3000);
  // 2. overview with two lights
  await p.evaluate(() => window.scrollTo(0, 0));
  await shot('phone-home');
  // 3. now tab
  await p.goto(U + '/#/light/chihiros-demo-1/now'); await p.waitForTimeout(1500);
  await shot('phone-now', true);
  // 4. program tab
  await p.goto(U + '/#/light/chihiros-demo-1/program'); await p.waitForTimeout(1800);
  await shot('phone-program', true);
  // single cards of the program tab, without the sticky header
  await p.addStyleTag({ content: 'header.top{display:none !important}' });
  const cards = await p.$$('#app .card');
  await cards[1].screenshot({ path: `${OUT}/card-stored.png` });
  await cards[2].screenshot({ path: `${OUT}/card-chart.png` });
  const top = await cards[3].evaluate(e => e.getBoundingClientRect().top + window.scrollY);
  await p.evaluate(y => window.scrollTo(0, y), top);
  const bb = await cards[3].boundingBox();
  await p.screenshot({ path: `${OUT}/card-phases-top.png`, clip: { x: bb.x, y: bb.y, width: bb.width, height: Math.min(505, 844 - bb.y) } });
  await p.reload(); await p.waitForTimeout(1800);
  // phase edit
  const ph = await p.$$('.phase'); if (ph.length > 3) { await ph[3].click(); await p.waitForTimeout(600); await shot('phone-phase-edit'); await p.mouse.click(195, 30); await p.waitForTimeout(400); }
  // 5. curve test
  await p.click('[data-act=curvetest]'); await p.waitForTimeout(600); await shot('phone-test-intro');
  await p.click('#sheetBody [data-a=go]'); await p.waitForTimeout(1500); await shot('phone-test-watch');
  await p.mouse.click(195, 30); await p.waitForTimeout(1500);
  // 6. light settings
  await p.goto(U + '/#/light/chihiros-demo-1/settings'); await p.waitForTimeout(1000);
  await shot('phone-light-settings', true);
  // 8. away: living room out of range normally, bedroom left in maintenance light
  await p.request.post(U + '/api/lights/chihiros-demo-2/maintenance', { data: { minutes: 60 }, headers: H });
  await p.goto(U + '/#/'); await p.waitForTimeout(2500);
  await shot('phone-home-maint');
  fs.writeFileSync(`${FLAG}/away-SIM-DYU700-2`, '1');
  fs.writeFileSync(`${FLAG}/away-SIM-DYU700`, '1');
  await p.waitForTimeout(5000);
  await shot('phone-home-away');
  fs.unlinkSync(`${FLAG}/away-SIM-DYU700`);
  await p.request.post(U + '/api/lights/chihiros-demo-2/program', { data: {}, headers: H }).catch(() => {});
  // 8b. a phone that has not scanned the code yet
  const ip = (await (await p.request.get(U + '/api/state')).json()).bridge.urls[0];
  const remote = await phone.newPage();
  await remote.goto(ip.replace(/\?k=.*/, '')); await remote.waitForTimeout(2500);
  await remote.screenshot({ path: `${OUT}/phone-unpaired.png` });
  await remote.goto(ip); await remote.waitForTimeout(2500);
  console.log('paired page title:', await remote.$eval('#subtitle', e => e.textContent));
  await remote.close();
  // 9. PC shots
  const pc = await b.newContext({ viewport: { width: 1280, height: 800 }, deviceScaleFactor: 2, colorScheme: 'light' });
  const q = await pc.newPage(); q.on('pageerror', e => errors.push(e.message));
  try { fs.unlinkSync(`${FLAG}/away-SIM-DYU700-2`); } catch (e) {}
  await q.goto(U + '/#/'); await q.waitForTimeout(20000);
  await q.screenshot({ path: `${OUT}/pc-home.png` });
  const qr = await q.$('.card:has(.qr)');
  if (qr) await qr.screenshot({ path: `${OUT}/pc-qr.png` }); else console.log('no qr card found');
  const st = await (await q.request.get(U + '/api/state')).json();
  console.log(st.lights.map(l => `${l.name}: ${l.connection} ${l.mode} adopted=${l.adopted}`).join(' | '));
  console.log('errors', errors.length ? errors : 'none'); await b.close();
})();
