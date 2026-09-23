// Offline, vector-preserving export of the registered native deck only.
import fs from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath, pathToFileURL} from 'node:url';
import {createRequire} from 'node:module';
import {createHash} from 'node:crypto';
import {isDeepStrictEqual} from 'node:util';

const require = createRequire(import.meta.url);
const digest = raw => createHash('sha256').update(raw).digest('hex');
const fail = code => { throw Object.assign(new Error(code), {exportCode:code}); };
let browser;
try {
  const [rootArg, requestArg] = process.argv.slice(2);
  const root = await fs.realpath(rootArg);
  async function local(relative) {
    if (typeof relative !== 'string' || !relative || path.isAbsolute(relative) || relative.includes('\\'))
      fail('invalid_manifest');
    const file = await fs.realpath(path.resolve(root, relative));
    if (!file.startsWith(root + path.sep)) fail('invalid_manifest');
    return file;
  }
  const requestFile = await local(requestArg), folder = path.dirname(requestFile);
  const request = JSON.parse(await fs.readFile(requestFile, 'utf8'));
  if (request.protocol !== 'harness-deck-pdf-v1' || request.run_id !== path.basename(folder))
    fail('invalid_manifest');
  async function checkExecutors() {
    for (const [name, relative] of Object.entries({runner:'scripts/deck_export.mjs', controller:'scripts/deck_export.py'})) {
      if (request[name]?.path !== relative ||
          digest(await fs.readFile(await local(relative))) !== request[name].sha256) fail('executor_changed');
    }
    if (digest(await fs.readFile(fileURLToPath(import.meta.url))) !== request.runner.sha256)
      fail('executor_changed');
  }
  await checkExecutors();
  const modulePath = process.env.HARNESS_NODE_MODULES;
  let chromium, PDFDocument, playwrightVersion, pdfLibVersion;
  try {
    for (const name of ['playwright', 'pdf-lib', 'playwright/package.json', 'pdf-lib/package.json']) {
      const resolved = require.resolve(modulePath ? path.join(modulePath, name) : name);
      if (/[\\/]\.(agents|codex)[\\/]skills[\\/]/.test(resolved)) fail('dependency_missing');
    }
    ({chromium} = require(modulePath ? path.join(modulePath, 'playwright') : 'playwright'));
    ({PDFDocument} = require(modulePath ? path.join(modulePath, 'pdf-lib') : 'pdf-lib'));
    playwrightVersion = require(modulePath ? path.join(modulePath, 'playwright/package.json') : 'playwright/package.json').version;
    pdfLibVersion = require(modulePath ? path.join(modulePath, 'pdf-lib/package.json') : 'pdf-lib/package.json').version;
  } catch { fail('dependency_missing'); }
  if (process.platform !== 'darwin') fail('environment_unsupported');
  const source = await local(request.source.html.path);
  const sourceURL = pathToFileURL(source).href;
  const html = await fs.readFile(source);
  if (digest(html) !== request.source.html.sha256) fail('source_changed');
  try {
    browser = await chromium.launch({channel:'chrome', headless:true, chromiumSandbox:true,
      args:['--disable-background-networking', '--no-proxy-server']});
  } catch { fail('browser_failed'); }
  const environment = {platform:process.platform, channel:'chrome', browser_version:browser.version(),
    playwright_version:playwrightVersion, pdf_lib_version:pdfLibVersion, node:process.version,
    media:'screen', viewport:{width:1280, height:900}};
  const context = await browser.newContext({offline:true, serviceWorkers:'block',
    viewport:environment.viewport, acceptDownloads:false});
  let external = 0, errors = 0;
  const allowed = url => url === sourceURL || /^(data|blob):/.test(url);
  context.on('request', req => { if (!allowed(req.url())) external++; });
  context.on('requestfailed', () => { errors++; });
  await context.route('**/*', route => allowed(route.request().url()) ? route.continue() : route.abort());
  const page = await context.newPage();
  page.setDefaultTimeout(15000);
  page.on('pageerror', () => { errors++; });
  page.on('console', message => { if (message.type() === 'error') errors++; });
  page.on('websocket', () => { external++; });
  const healthy = () => { if (external) fail('network_request'); if (errors) fail('console_error'); };
  await page.emulateMedia({media:'screen', reducedMotion:'reduce'});
  await page.goto(sourceURL, {waitUntil:'load'});
  await page.evaluate(() => document.fonts.ready);
  healthy();
  const data = JSON.parse(await page.locator('[data-phase5-deck]').textContent());
  const count = request.source.page_count;
  if (!Number.isInteger(count) || count < 1 || data.pages.length !== count ||
      !isDeepStrictEqual(data.deck_ref, request.source.ref) ||
      !isDeepStrictEqual(data.draft, {revision:0, edits:{}}) ||
      await page.locator('.slide').count() !== count || await page.locator('#pages option').count() !== count)
    fail('page_mismatch');
  // Keep screen typography and colors, not the native print rule that reveals every slide.
  await page.addStyleTag({content:`
    html,body { margin:0!important; padding:0!important; min-height:0!important; }
    body>header,body>footer,body>#speaker { display:none!important; }
    body>main { margin:0!important; padding:0!important; }
    html body main .slide[hidden] { display:none!important; }
    .slide { break-before:auto!important; break-after:auto!important; break-inside:avoid!important; }
    * { -webkit-print-color-adjust:exact!important; print-color-adjust:exact!important;
        animation:none!important; transition:none!important; }
  `});
  const merged = await PDFDocument.create(), pages = [];
  const reference = async file => ({path:path.relative(root, file).split(path.sep).join('/'),
    sha256:digest(await fs.readFile(file))});
  for (let i = 0; i < count; i++) {
    await page.locator('#pages').selectOption(String(i), {force:true});
    await page.evaluate(async () => {
      await document.fonts.ready;
      await Promise.all([...document.images].map(image => image.decode().catch(() => {})));
      await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    });
    healthy();
    const slide = page.locator(`#page-${i + 1}`);
    if (!await slide.isVisible() || await page.locator('.slide:visible').count() !== 1 ||
        await page.locator('#pages').inputValue() !== String(i)) fail('page_mismatch');
    const text = await slide.locator('[data-edit]').allTextContents();
    if (text[0] !== data.pages[i].title || text.slice(1).join('') !== data.pages[i].text)
      fail('page_mismatch');
    const layout = await slide.evaluate(element => {
      const box = element.getBoundingClientRect();
      const outside = r => r.left < box.left - 1 || r.right > box.right + 1 ||
        r.top < box.top - 1 || r.bottom > box.bottom + 1;
      let overflow = box.x !== 0 || box.y !== 0;
      for (const field of [element, ...element.querySelectorAll('*')]) {
        const style = getComputedStyle(field), r = field.getBoundingClientRect();
        const visible = style.display !== 'none' && style.visibility === 'visible' && Number(style.opacity) > 0;
        const hasText = field.textContent.trim() && (field.matches('[data-edit]') || field.querySelector('[data-edit]'));
        if (hasText && (!visible || !r.width || !r.height || style.clipPath !== 'none'))
          overflow = true;
        if (!visible || !r.width || !r.height) continue;
        if (outside(r) || (field.clientWidth && field.scrollWidth > field.clientWidth + 1) ||
            (field.clientHeight && field.scrollHeight > field.clientHeight + 1)) overflow = true;
        if (field.tagName === 'IMG' && (!field.complete || !field.naturalWidth)) overflow = true;
        for (const child of field.childNodes) {
          if (child.nodeType !== Node.TEXT_NODE || !child.textContent.trim()) continue;
          const range = document.createRange(); range.selectNodeContents(child);
          if ([...range.getClientRects()].some(outside)) overflow = true;
        }
      }
      return {width:box.width, height:box.height, overflow};
    });
    if (layout.overflow || !Number.isFinite(layout.width) || !Number.isFinite(layout.height) ||
        layout.width <= 0 || layout.height <= 0 || layout.width > 19000 || layout.height > 19000)
      fail('overflow');
    // Match paper to the complete slide, including long pages; never use pageRanges to hide overflow.
    const width = Math.ceil(layout.width), height = Math.ceil(layout.height);
    const paper = await page.addStyleTag({content:`@page { size:${width}px ${height}px; margin:0; }`});
    const raw = await page.pdf({width:`${width}px`, height:`${height}px`, scale:1,
      printBackground:true, preferCSSPageSize:true, displayHeaderFooter:false,
      margin:{top:0, bottom:0, left:0, right:0}});
    await paper.evaluate(node => node.remove());
    healthy();
    const single = await PDFDocument.load(raw);
    if (single.getPageCount() !== 1) fail('page_mismatch');
    const size = single.getPage(0).getSize();
    if (Math.abs(size.width - width * .75) > 1 || Math.abs(size.height - height * .75) > 1)
      fail('page_mismatch');
    const file = path.join(folder, `page-${String(i + 1).padStart(3, '0')}.pdf`);
    await fs.writeFile(file, raw, {flag:'wx'});
    merged.addPage((await merged.copyPages(single, [0]))[0]);
    pages.push({page:i + 1, width_css_px:width, height_css_px:height,
      width_pt:size.width, height_pt:size.height, file:await reference(file)});
  }
  healthy();
  if (digest(await fs.readFile(source)) !== request.source.html.sha256) fail('source_changed');
  await checkExecutors();
  const pdf = path.join(folder, 'presentation.pdf');
  await fs.writeFile(pdf, await merged.save(), {flag:'wx'});
  const reopened = await PDFDocument.load(await fs.readFile(pdf));
  if (reopened.getPageCount() !== count) fail('page_mismatch');
  await context.close();
  await browser.close(); browser = null;
  await fs.writeFile(path.join(folder, 'render-result.json'), JSON.stringify({...request,
    environment, page_count:count, pages, pdf:await reference(pdf),
    checks:{offline:true, console:true, layout:true, page_count:true}}, null, 2) + '\n', {flag:'wx'});
} catch (error) {
  // Do not persist raw console text, external URLs, dependency paths or error stacks.
  console.error(JSON.stringify({code:error.exportCode || 'browser_failed'}));
  process.exitCode = 1;
} finally {
  if (browser) await browser.close().catch(() => {});
}
