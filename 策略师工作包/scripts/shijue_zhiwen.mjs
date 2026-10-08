// W01 视觉增量：逐页无损截图（PNG），供 shijue_zengliang.py 解码后算像素指纹；离线。
// r2：检核员与客户看到的画面比单一截图更宽，故每页截以下状态（任一变化即该页必看）：
//   运行脚本（客户/检核员在浏览器里打开的样子）：
//     screen   —— 屏幕媒体 1280×720 亮色，按 deck_shots 的做法展开本页（解除 hidden/display/visibility/opacity/content-visibility）
//     print    —— 打印媒体，同样展开本页
//     expanded —— 屏幕媒体，再把本页内被隐藏的元素全部展开（只改隐藏元素也能检出）
//     narrow   —— 屏幕媒体 390×844（手机宽度媒体查询）
//     dark     —— 屏幕媒体，深色配色（prefers-color-scheme: dark）
//   不运行脚本（截图、对外导出与 PDF 的口径）：
//     nojs_screen、nojs_print
// 截图范围 = 本页外框与本页全部内容实际外框的并集（含溢出到页框外的内容；r3 另并入每个元素的滚动范围，覆盖页框外的
// ::before/::after 伪元素），不只截页框；截某页时其它页暂不显示。
// r3：运行脚本时，每次载入后先等 DOM 静止（连续 800 毫秒没有变化，最长 5 秒）再截图；超时或截图期间页面仍在变 → 该页不稳定。
// 页内伪元素（::before/::after/::marker）的 content 计入各状态文字；用到 CSS 计数器（counter()/counters()）的页报出，
// 由 Python 判不稳定（截某页时其它页不显示，跨页编号无法由截图证明没变）。
// 另报：每页屏幕/打印文字、运行中的动画、页内动图/视频等媒体引用、字体与资源加载失败、全部资源请求（供脚本指纹）。
// 每页每种脚本开关各重新载入一次，互不影响。用法：node shijue_zhiwen.mjs <html> <输出目录> <额外等待毫秒>
import fs from 'node:fs';
import path from 'node:path';
import {createRequire} from 'node:module';
import {pathToFileURL} from 'node:url';
const require = createRequire(import.meta.url);
const [html, out, waitArg] = process.argv.slice(2);
const base = process.env.HARNESS_NODE_MODULES;
const pw = require(base ? path.join(base, 'playwright') : 'playwright');
const pwVersion = (() => { try { return require(base ? path.join(base, 'playwright/package.json') : 'playwright/package.json').version; } catch { return 'unknown'; } })();
const VIEW = {width: 1280, height: 720};
const NARROW = {width: 390, height: 844};
const browser = await pw.chromium.launch({channel: 'chrome', headless: true, args: ['--disable-background-networking', '--force-color-profile=srgb', '--font-render-hinting=none']});
// r3：浏览器中途退出（崩溃或被系统结束）时立即报错退出，不无限等待；Python 侧据此按“缺渲染条件”全页处理。
let finished = false;
browser.on('disconnected', () => { if (!finished) { console.error('浏览器进程中途退出，渲染未完成'); process.exit(3); } });
const url = pathToFileURL(path.resolve(html)).href;
const wait = Number(waitArg) || 0;
const result = {browser_version: browser.version(), playwright: pwVersion, viewport: VIEW, narrow_viewport: NARROW, device_scale_factor: 1,
  javascript: 'both', states: ['screen', 'print', 'expanded', 'narrow', 'dark', 'nojs_screen', 'nojs_print'], sections: [],
  load_problems: [], requests: []};
const problems = new Set();
const requests = new Map();
const QUIET_MS = 800, QUIET_MAX_MS = 5000;
// 页面一开始就装上 DOM 变化计数（本程序自己展开页面造成的变化在 EXPAND 末尾丢弃，不计入）。
const WATCH = () => {
  const q = {n: 0, last: performance.now()};
  const obs = new MutationObserver(records => { q.n += records.length; q.last = performance.now(); });
  obs.observe(document, {subtree: true, childList: true, attributes: true, characterData: true});
  q.discard = () => { obs.takeRecords(); };
  Object.defineProperty(window, '__harnessQuiet', {value: q, enumerable: false});
};

async function session(js) {
  const context = await browser.newContext({viewport: VIEW, deviceScaleFactor: 1, offline: true, javaScriptEnabled: js, colorScheme: 'light', reducedMotion: 'no-preference'});
  const page = await context.newPage();
  if (js) await page.addInitScript(WATCH);
  page.on('request', r => { if (!r.url().startsWith('data:')) requests.set(r.url(), r.resourceType()); });
  page.on('requestfailed', r => problems.add(`资源加载失败：${r.url().slice(0, 160)}（${(r.failure() || {}).errorText || '未知'}）`));
  page.on('response', r => { if (r.status() >= 400) problems.add(`资源返回 ${r.status()}：${r.url().slice(0, 160)}`); });
  page.on('console', m => { const t = m.text(); if (m.type() === 'error' && /Not allowed to load|Unsafe attempt|Failed to load|ERR_|blocked/i.test(t)) problems.add('浏览器报告资源被拦截或加载失败：' + t.slice(0, 160)); });
  page.on('pageerror', e => problems.add('页面脚本出错：' + String(e.message || e).slice(0, 160)));
  const load = async () => {
    await page.setViewportSize(VIEW);
    await page.emulateMedia({media: 'screen', colorScheme: 'light'});
    await page.goto(url, {waitUntil: 'load'});
    const info = await page.evaluate(async () => {
      try { await document.fonts.ready; } catch (e) {}
      const bad = [];
      if (document.fonts) for (const f of document.fonts) if (f.status === 'error') bad.push(`字体加载失败：${f.family}`);  // 不运行脚本的页面不能用回调（forEach）
      for (const img of document.images) if (img.complete && img.naturalWidth === 0 && (img.currentSrc || img.src)) bad.push(`图片加载失败：${(img.currentSrc || img.src).slice(0, 120)}`);
      for (const u of document.querySelectorAll('use')) {
        const h = u.getAttribute('href') || u.getAttribute('xlink:href') || '';
        if (h && !h.startsWith('#')) bad.push(`外部 SVG 引用无法确认加载：${h.slice(0, 120)}`);
      }
      return {count: document.querySelectorAll('section').length, bad};
    });
    info.bad.forEach(b => problems.add(b));
    info.settle = js ? await quiet(page) : {settled: true, n: 0};
    return info;
  };
  return {context, page, load};
}

// 等 DOM 静止：连续 QUIET_MS 没有变化即静止；页面没有可执行脚本时 DOM 不会自己变，直接视为静止；超过 QUIET_MAX_MS 仍在变 → 不静止。
const QUIET = async ([quietMs, maxMs]) => {
  const q = window.__harnessQuiet;
  const JS = new Set(['', 'text/javascript', 'application/javascript', 'module', 'text/ecmascript', 'application/ecmascript', 'text/jscript', 'application/x-javascript', 'text/x-javascript', 'text/livescript']);
  const scripted = [...document.scripts].some(s => JS.has((s.getAttribute('type') || '').split(';')[0].trim().toLowerCase())) ||
    [...document.querySelectorAll('*')].some(el => [...el.attributes].some(a => a.name.startsWith('on')));
  if (!q || !scripted) return {settled: true, n: q ? q.n : 0};
  const start = performance.now();
  while (performance.now() - q.last < quietMs) {
    if (performance.now() - start > maxMs) return {settled: false, n: q.n};
    await new Promise(r => setTimeout(r, 50));
  }
  return {settled: true, n: q.n};
};
async function quiet(page) { return page.evaluate(QUIET, [QUIET_MS, QUIET_MAX_MS]); }
// 截图期间页面自己又变了几次（本程序的展开改动已丢弃）。
const CHANGES = () => { const q = window.__harnessQuiet; if (!q) return 0; q.discard(); return q.n; };

// 本页外框与全部内容外框的并集（文档坐标），夹在文档范围内。
const BOX = () => {
  const node = document.querySelector('section[data-harness-fp="1"]');
  const sx = window.scrollX, sy = window.scrollY;
  let r = node.getBoundingClientRect();
  let x1 = r.left, y1 = r.top, x2 = r.right, y2 = r.bottom;
  for (const el of node.querySelectorAll('*')) {
    for (const b of el.getClientRects()) {
      if (b.width <= 0 || b.height <= 0) continue;
      x1 = Math.min(x1, b.left); y1 = Math.min(y1, b.top); x2 = Math.max(x2, b.right); y2 = Math.max(y2, b.bottom);
    }
  }
  // 滚动范围含绝对定位的伪元素等不是元素的内容（页框外的 ::after 改字也要截到）。
  for (const el of [node, ...node.querySelectorAll('*')]) {
    if (el.scrollWidth <= el.clientWidth && el.scrollHeight <= el.clientHeight) continue;
    const b = el.getBoundingClientRect();
    x2 = Math.max(x2, b.left + el.clientLeft + el.scrollWidth); y2 = Math.max(y2, b.top + el.clientTop + el.scrollHeight);
  }
  const W = Math.max(document.documentElement.scrollWidth, document.body ? document.body.scrollWidth : 0);
  const H = Math.max(document.documentElement.scrollHeight, document.body ? document.body.scrollHeight : 0);
  const ax = Math.max(0, Math.floor(x1 + sx)), ay = Math.max(0, Math.floor(y1 + sy));
  const bx = Math.min(W, Math.ceil(x2 + sx)), by = Math.min(H, Math.ceil(y2 + sy));
  return {x: ax, y: ay, width: Math.max(1, bx - ax), height: Math.max(1, by - ay),
          overflow: x1 < r.left - 0.5 || y1 < r.top - 0.5 || x2 > r.right + 0.5 || y2 > r.bottom + 0.5};
};

const EXPAND = i => {
  for (const old of document.querySelectorAll('[data-harness-fp]')) old.removeAttribute('data-harness-fp');
  const node = document.querySelectorAll('section')[i];
  node.setAttribute('data-harness-fp', '1');
  // 其它页（不是本页的祖先或后代）暂时不显示：本页的位置不随别页内容长短移动（亚像素位移会改变文字抗锯齿），
  // 与打印分页、逐页翻看的效果一致；别页溢出到本页区域的内容由别页自己的截图范围覆盖。
  for (const other of document.querySelectorAll('section')) {
    if (other !== node && !other.contains(node) && !node.contains(other)) other.style.setProperty('display', 'none', 'important');
  }
  node.hidden = false; const cs = getComputedStyle(node);
  if (cs.display === 'none') node.style.setProperty('display', 'block', 'important');
  node.style.setProperty('visibility', 'visible', 'important');
  if (parseFloat(cs.opacity) < 1) node.style.setProperty('opacity', '1', 'important');
  if (cs.contentVisibility && cs.contentVisibility !== 'visible') node.style.setProperty('content-visibility', 'visible', 'important');
  const top = !node.parentElement || !node.parentElement.closest('section');
  if (window.__harnessQuiet) window.__harnessQuiet.discard();
  return {top, id: node.getAttribute('id')};
};

const EXPAND_HIDDEN = () => {
  const node = document.querySelector('section[data-harness-fp="1"]');
  const SKIP = new Set(['SCRIPT', 'STYLE', 'TEMPLATE', 'NOSCRIPT', 'TITLE', 'META', 'LINK', 'HEAD']);
  for (const child of node.querySelectorAll('*')) {
    if (SKIP.has(child.tagName)) continue;
    if (child.hasAttribute('hidden')) child.removeAttribute('hidden');
    if (child.tagName === 'DETAILS') child.setAttribute('open', '');
    const cs = getComputedStyle(child);
    if (cs.display === 'none') child.style.setProperty('display', 'revert', 'important');
    if (getComputedStyle(child).display === 'none') child.style.setProperty('display', 'block', 'important');
    if (cs.visibility !== 'visible') child.style.setProperty('visibility', 'visible', 'important');
    if (parseFloat(cs.opacity) < 1) child.style.setProperty('opacity', '1', 'important');
    if (cs.contentVisibility && cs.contentVisibility !== 'visible') child.style.setProperty('content-visibility', 'visible', 'important');
  }
  if (window.__harnessQuiet) window.__harnessQuiet.discard();
};

// 本页引用的媒体：动图/视频/内嵌页面判不稳定，图片地址交给 Python 判断是否为动图。
const MEDIA = () => {
  const node = document.querySelector('section[data-harness-fp="1"]');
  const urls = new Set(), live = [];
  const grab = v => { for (const m of String(v || '').matchAll(/url\(\s*(['"]?)(.*?)\1\s*\)/g)) if (m[2]) urls.add(new URL(m[2], document.baseURI).href); };
  for (const el of [node, ...node.querySelectorAll('*')]) {
    const tag = el.tagName.toLowerCase();
    if (['video', 'iframe', 'object', 'embed'].includes(tag)) live.push(tag);
    if (tag === 'img' && (el.currentSrc || el.src)) urls.add(el.currentSrc || el.src);
    if (tag === 'image') { const h = el.getAttribute('href') || el.getAttribute('xlink:href'); if (h) urls.add(new URL(h, document.baseURI).href); }
    if (tag === 'input' && el.type === 'image' && el.src) urls.add(el.src);
    for (const pseudo of [null, '::before', '::after']) {
      const cs = getComputedStyle(el, pseudo);
      grab(cs.backgroundImage); grab(cs.borderImageSource); grab(cs.listStyleImage); grab(cs.maskImage || cs.webkitMaskImage); grab(cs.content);
    }
  }
  const animations = (node.getAnimations ? node.getAnimations({subtree: true}) : []).filter(a => a.playState === 'running' || a.playState === 'pending').length;
  return {urls: [...urls], live, animations};
};

// 改视口/媒体/展开后先强制排版并稍等绘制落定，再量外框并截图（不运行脚本的页面没有动画帧回调，不能用 rAF）。
const settle = async page => { await page.evaluate(() => document.documentElement.getBoundingClientRect().height); await page.waitForTimeout(60); };
const shot = async (page, file) => {
  await settle(page);
  const box = await page.evaluate(BOX);
  await page.screenshot({path: file, type: 'png', fullPage: true, clip: {x: box.x, y: box.y, width: box.width, height: box.height}, animations: 'allow', caret: 'initial'});
  return box.overflow;
};
// 本页文字 = innerText + 伪元素 content（::before/::after/::marker）；另报是否用到 CSS 计数器。
const TEXT = () => {
  const node = document.querySelector('section[data-harness-fp="1"]');
  const pseudo = [];
  let counters = false;
  for (const el of [node, ...node.querySelectorAll('*')]) {
    for (const p of ['::before', '::after', '::marker']) {
      const c = getComputedStyle(el, p).content;
      if (!c || c === 'none' || c === 'normal') continue;
      pseudo.push(c);
      if (/\bcounters?\(/.test(c)) counters = true;
    }
  }
  return {inner: node.innerText || '', pseudo: pseudo.join('\n'), counters};
};
const text = async (page, sink) => {
  const t = await page.evaluate(TEXT);
  sink.counters = sink.counters || t.counters;
  return t.pseudo ? t.inner + '\n［伪元素］\n' + t.pseudo : t.inner;
};
const inner = page => page.evaluate(() => document.querySelector('section[data-harness-fp="1"]').innerText || '');

const name = (i, s) => path.join(out, `p${String(i + 1).padStart(3, '0')}-${s}.png`);

// 运行脚本：screen / print / dark / narrow / expanded
async function jsPass(js, first) {
  const rows = [];
  for (let i = 0; i < first.count; i++) {
    const files = {}, texts = {}, flags = {counters: false};
    let overflow = false;
    const info = i === 0 ? first : await js.load();
    if (info.count !== first.count) throw new Error('两次载入的页面数不同');
    const meta = await js.page.evaluate(EXPAND, i);
    if (wait) await js.page.waitForTimeout(wait);
    const media = await js.page.evaluate(MEDIA);
    files.screen = name(i, 'screen'); overflow = (await shot(js.page, files.screen)) || overflow; texts.screen = await text(js.page, flags);
    await js.page.emulateMedia({media: 'print', colorScheme: 'light'}); await js.page.evaluate(EXPAND, i);
    files.print = name(i, 'print'); overflow = (await shot(js.page, files.print)) || overflow; texts.print = await text(js.page, flags); const printInner = await inner(js.page);
    await js.page.emulateMedia({media: 'screen', colorScheme: 'dark'}); await js.page.evaluate(EXPAND, i);
    files.dark = name(i, 'dark'); overflow = (await shot(js.page, files.dark)) || overflow;
    await js.page.emulateMedia({media: 'screen', colorScheme: 'light'});
    await js.page.setViewportSize(NARROW); await js.page.evaluate(EXPAND, i);
    files.narrow = name(i, 'narrow'); overflow = (await shot(js.page, files.narrow)) || overflow; texts.narrow = await text(js.page, flags);
    await js.page.setViewportSize(VIEW); await js.page.evaluate(EXPAND, i);
    await js.page.evaluate(EXPAND_HIDDEN);
    files.expanded = name(i, 'expanded'); overflow = (await shot(js.page, files.expanded)) || overflow;
    const unsettled = [];
    if (!info.settle.settled) unsettled.push(`载入后 ${QUIET_MAX_MS / 1000} 秒内页面仍在变化（DOM 未静止）`);
    else if ((await js.page.evaluate(CHANGES)) > info.settle.n) unsettled.push('截图期间页面仍在变化（脚本改了页面）');
    rows.push({meta, media, files, texts, overflow, printInner, counters: flags.counters, unsettled});
  }
  return rows;
}

// 不运行脚本：nojs_screen / nojs_print（截图、对外导出与 PDF 的口径）
async function nojsPass(nojs, first) {
  const rows = [];
  for (let i = 0; i < first.count; i++) {
    const files = {}, texts = {}, flags = {counters: false};
    let overflow = false;
    const info = i === 0 ? first : await nojs.load();
    if (info.count !== first.count) throw new Error('两次载入的页面数不同');
    await nojs.page.evaluate(EXPAND, i);
    if (wait) await nojs.page.waitForTimeout(wait);
    const media = await nojs.page.evaluate(MEDIA);
    files.nojs_screen = name(i, 'nojs_screen'); overflow = (await shot(nojs.page, files.nojs_screen)) || overflow; texts.nojs_screen = await text(nojs.page, flags);
    await nojs.page.emulateMedia({media: 'print', colorScheme: 'light'}); await nojs.page.evaluate(EXPAND, i);
    files.nojs_print = name(i, 'nojs_print'); overflow = (await shot(nojs.page, files.nojs_print)) || overflow; texts.nojs_print = await text(nojs.page, flags);
    rows.push({media, files, texts, overflow, printInner: await inner(nojs.page), counters: flags.counters});
  }
  return rows;
}

// v1.7.1：先写完 result.json 再关浏览器；关浏览器最多等 CLOSE_MS，超时则结束本进程（结果已写出，按成功退出）。
// 渲染期间的错误单独保存，关浏览器出错或超时都不会把它盖掉。
const CLOSE_MS = 15000;
let renderError = null;
try {
  const js = await session(true), nojs = await session(false);
  const [first, firstNo] = await Promise.all([js.load(), nojs.load()]);
  if (firstNo.count !== first.count) problems.add(`运行脚本与不运行脚本时页数不同（${first.count} / ${firstNo.count}）`);
  const [rows, rowsNo] = await Promise.all([jsPass(js, first), firstNo.count === first.count ? nojsPass(nojs, firstNo) : Promise.resolve([])]);
  rows.forEach((row, i) => {
    const other = rowsNo[i] || {media: {urls: [], live: [], animations: 0}, files: {}, texts: {}, overflow: false, printInner: undefined, counters: false};
    const urls = [...new Set([...row.media.urls, ...other.media.urls])];
    const texts = {...row.texts, ...other.texts};
    result.sections.push({index: i + 1, id: row.meta.id, top: row.meta.top, animations: row.media.animations + other.media.animations,
      media_urls: urls, live_media: [...new Set([...row.media.live, ...other.media.live])], overflow: row.overflow || other.overflow,
      texts, print_text: other.printInner ?? row.printInner, counters: row.counters || other.counters, unsettled: row.unsettled,
      files: {...row.files, ...other.files}});
  });
  await js.context.close(); await nojs.context.close();
  result.load_problems = [...problems].sort();
  result.requests = [...requests.entries()].map(([u, t]) => ({url: u, type: t})).sort((a, b) => a.url < b.url ? -1 : 1);
  fs.writeFileSync(path.join(out, 'result.json'), JSON.stringify(result));
} catch (e) { renderError = e; }
finished = true;
const closed = await Promise.race([
  browser.close().then(() => 'ok', e => '关浏览器出错：' + String((e && e.message) || e).slice(0, 200)),
  new Promise(r => setTimeout(() => r(`关浏览器超过 ${CLOSE_MS / 1000} 秒未返回，强制结束本进程`), CLOSE_MS)),
]);
if (closed !== 'ok') console.error(closed);
if (renderError) {
  console.error('渲染失败：' + String((renderError && renderError.stack) || renderError).slice(0, 1000));
  process.exit(1);
}
console.log(JSON.stringify({ok: true, sections: result.sections.length}));
process.exit(0);
