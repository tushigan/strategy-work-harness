// Actual isolated Mac Chrome checks; the Python controller registers success.
import fs from 'node:fs/promises';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
import {createRequire} from 'node:module';
import {createHash} from 'node:crypto';

const require = createRequire(import.meta.url);
const modulePath = process.env.HARNESS_NODE_MODULES;
const [rootArg, requestArg] = process.argv.slice(2);
const root = path.resolve(rootArg);
function local(relative) {
  const resolved = path.resolve(root, relative);
  if (!relative || path.isAbsolute(relative) || !resolved.startsWith(root + path.sep))
    throw new Error('Project-relative path required');
  return resolved;
}
const request = JSON.parse(await fs.readFile(local(requestArg), 'utf8'));
const folder = path.dirname(local(requestArg));
const output = path.join(folder, 'browser-result.json');
const evidence = [], steps = [], errors = [], external = [];
const checks = {display:false, navigation:false, editing:false, export_reopen:false, offline:false};
let chromium;
let playwrightVersion;
async function ref(file) {
  return {path:path.relative(root, file).split(path.sep).join('/'),
    sha256:createHash('sha256').update(await fs.readFile(file)).digest('hex')};
}
function done(name, observed) {
  checks[name] = true;
  steps.push({name, passed:true, observed});
}
function watch(page) {
  page.on('pageerror', error => errors.push(error.message));
  // Save only origin; external URLs may contain credentials or private query data.
  page.on('request', req => {if (/^https?:/.test(req.url())) external.push(new URL(req.url()).origin);});
}
let browser;
try {
  if (process.platform !== 'darwin' || request.protocol !== 'harness-deck-browser-v1')
    throw new Error('Unsupported browser probe environment or protocol');
  const playwright = require(modulePath ? path.join(modulePath, 'playwright') : 'playwright');
  chromium = playwright.chromium;
  playwrightVersion = require(modulePath ? path.join(modulePath, 'playwright/package.json') :
    'playwright/package.json').version;
  const source = local(request.input_html.path);
  if ((await ref(source)).sha256 !== request.input_html.sha256) throw new Error('HTML fingerprint changed');
  browser = await chromium.launch({channel:'chrome', headless:true, chromiumSandbox:true});
  const environment = {platform:process.platform, browser_version:browser.version(),
    playwright_version:playwrightVersion, node:process.version};
  const context = await browser.newContext({offline:true, viewport:{width:1280,height:900}, acceptDownloads:true});
  const page = await context.newPage();
  watch(page);
  await page.goto(pathToFileURL(source).href);
  const data = JSON.parse(await page.locator('[data-phase5-deck]').textContent());
  if (JSON.stringify(data.deck_ref) !== JSON.stringify(request.target)) throw new Error('Wrong deck version');
  const count = data.pages.length;
  if (await page.locator('.slide').count() !== count) throw new Error('Slide count mismatch');
  for (const viewport of [{width:1280,height:900}, {width:390,height:844}]) {
    await page.setViewportSize(viewport);
    for (let i=0;i<count;i++) {
      await page.locator('#pages').selectOption(String(i));
      const active = page.locator(`#page-${i+1}`);
      if (!await active.isVisible() || await page.locator('.slide:visible').count() !== 1)
        throw new Error('Navigation visibility mismatch');
      const layout = await active.evaluate(slide => {
        const outer = slide.getBoundingClientRect();
        const fields = [...slide.querySelectorAll('[data-edit],.folio')]
          .filter(field => field.textContent.trim());
        return fields.map(field => {
          const box = field.getBoundingClientRect(), style = getComputedStyle(field);
          const range = document.createRange();
          range.selectNodeContents(field);
          const textRects = [...range.getClientRects()];
          const transparent = color => /rgba\([^)]*,\s*0(?:\.0+)?\s*\)$/.test(color);
          const hidden = !field.checkVisibility({checkOpacity:true,checkVisibilityCSS:true}) ||
            box.width <= 0 || box.height <= 0 || parseFloat(style.fontSize) < 1 ||
            transparent(style.color) || transparent(style.webkitTextFillColor);
          const clipped = textRects.some(r => r.left < box.left-1 || r.right > box.right+1 ||
            r.top < box.top-1 || r.bottom > box.bottom+1);
          const overlap = fields.some(other => {
            if (other === field) return false;
            const b = other.getBoundingClientRect();
            return Math.min(box.right,b.right)-Math.max(box.left,b.left)>1 &&
              Math.min(box.bottom,b.bottom)-Math.max(box.top,b.top)>1;
          });
          return {text:field.textContent, hidden, clipped, overlap,
            overflow:field.scrollWidth>field.clientWidth+1 || field.scrollHeight>field.clientHeight+1,
            outside:box.left<outer.left-1 || box.right>outer.right+1 || box.top<outer.top-1 || box.bottom>outer.bottom+1,
            belowViewport:box.bottom>window.innerHeight+1};
        });
      });
      // Narrow long pages can scroll; horizontal overflow and escaped fields fail.
      if (layout.some(x => x.hidden || x.clipped || x.overlap))
        throw new Error(`Hidden, clipped or overlapping content on page ${i+1}`);
      if (layout.some(x => x.overflow || x.outside)) throw new Error(`Overflow on page ${i+1}`);
      const text = await active.locator('[data-edit]').allTextContents();
      if (text[0] !== data.pages[i].title || text.slice(1).join('') !== data.pages[i].text)
        throw new Error(`Changed page-plan text on page ${i+1}`);
      const images = await active.locator('img').evaluateAll(items => items.map(img => ({
        loaded:img.complete && img.naturalWidth>0 && img.naturalHeight>0,
        visible:img.checkVisibility({checkOpacity:true,checkVisibilityCSS:true}),
        width:img.getBoundingClientRect().width,height:img.getBoundingClientRect().height
      })));
      if (images.some(img => !img.loaded || !img.visible || img.width<1 || img.height<1))
        throw new Error(`Missing or hidden image on page ${i+1}`);
      const file = path.join(folder, `page-${String(i+1).padStart(3,'0')}-${viewport.width}.png`);
      await page.screenshot({path:file,fullPage:true});
      evidence.push({page:i+1,rendered:true,viewport,file:await ref(file),layout,images});
    }
  }
  done('display', {pages:count, screenshots:evidence.length, widths:[1280,390]});
  await page.setViewportSize({width:1280,height:900});
  await page.locator('#pages').selectOption('0');
  await page.locator('#next').click();
  if (await page.locator('#pages').inputValue() !== '1') throw new Error('Next button failed');
  await page.locator('#prev').click();
  if (await page.locator('#pages').inputValue() !== '0') throw new Error('Previous button failed');
  await page.locator('main').click({position:{x:20,y:20}});
  await page.keyboard.press('ArrowRight');
  if (await page.locator('#pages').inputValue() !== '1') throw new Error('Keyboard failed');
  done('navigation', {next:true,previous:true,keyboard:true,selector_pages:count});
  await page.locator('#edit').check();
  const field = page.locator('.slide:visible [data-edit]').first();
  const original = await field.textContent();
  const edited = original + ' 浏览器编辑验证';
  await field.fill(edited);
  if (!(await page.locator('#state').textContent()).includes('草稿')) throw new Error('Missing draft warning');
  done('editing', {page:2,field:await field.getAttribute('data-edit'),draft_warning:true});
  const downloadWait = page.waitForEvent('download');
  await page.locator('#download').click();
  const download = await downloadWait;
  const exportFile = path.join(folder, 'exported-draft.html');
  await download.saveAs(exportFile);
  if (await download.failure()) throw new Error('Download failed');
  const fresh = await browser.newContext({offline:true, viewport:{width:1280,height:900}});
  const reopened = await fresh.newPage();
  watch(reopened);
  await reopened.goto(pathToFileURL(exportFile).href);
  await reopened.locator('#pages').selectOption('1');
  if (await reopened.locator('.slide:visible [data-edit]').first().textContent() !== edited)
    throw new Error('Export did not retain edits without cache');
  const restored = JSON.parse(await reopened.locator('[data-phase5-deck]').textContent());
  if (JSON.stringify(restored.deck_ref) !== JSON.stringify(request.target) || restored.draft.revision < 1)
    throw new Error('Export changed source reference');
  const reopenedImage = path.join(folder, 'export-reopened.png');
  await reopened.screenshot({path:reopenedImage,fullPage:true});
  done('export_reopen', {new_context:true,edit_restored:true,draft_revision:restored.draft.revision});
  if (errors.length || external.length) throw new Error('Page errors or external requests');
  done('offline', {original_context:true,reopen_context:true,external_requests:external.length});
  if ((await ref(source)).sha256 !== request.input_html.sha256) throw new Error('Original HTML changed');
  await fresh.close();
  await context.close();
  const result = {...request, mode:'deck', simulation:false, browser:'Chrome headless on Mac',
    environment, offline_context:true, checks, steps, evidence, errors, external,
    exported:await ref(exportFile), reopened_screenshot:await ref(reopenedImage)};
  await fs.writeFile(output, JSON.stringify(result,null,2) + '\n', {flag:'wx'});
} catch (error) {
  const reason = String(error.message).replaceAll(root,'<workspace>');
  await fs.writeFile(path.join(folder,'browser-failure.json'), JSON.stringify({
    status:'failed', checks, steps, errors, external, reason
  },null,2) + '\n', {flag:'wx'});
  console.error(reason);
  process.exitCode = 1;
} finally {
  if (browser) await browser.close();
}
