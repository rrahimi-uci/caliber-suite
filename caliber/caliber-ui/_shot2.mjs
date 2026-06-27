import { chromium } from '@playwright/test';
const url = 'file:///Users/rezarahimi/Documents/GitHub/kernel-lab/caliber-suite/docs-site/m-16-cookbooks.html';
const b = await chromium.launch();
async function shotAt(theme, y, file) {
  const p = await b.newPage({ viewport: { width: 1440, height: 1000 } });
  await p.addInitScript((t)=>localStorage.setItem('caliber-docs-theme',t), theme);
  await p.goto(url, { waitUntil:'load' });
  await p.waitForTimeout(3000); // mermaid render
  await p.evaluate((yy)=>window.scrollTo(0, yy), y);
  await p.waitForTimeout(700);
  await p.screenshot({ path:file });
  await p.close();
}
await shotAt('light', 1750, '/tmp/ck_steps2.png');   // first cookbook: flow + steps + fill tables
await shotAt('light', 2700, '/tmp/ck_mock.png');      // step cards + UI mockup
console.log('ok');
