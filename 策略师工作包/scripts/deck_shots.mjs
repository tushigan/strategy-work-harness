// 只截指定页，存压缩 JPG；离线、不联网。页码与自检、扫描、对外导出同一定义：由 html_huamian 给出
// “第几页 = 第几个顶层页面在全部 <section> 里的序号”；浏览器里 section 总数对不上即拒绝（不猜）。
// 请求的页不存在即失败；不覆盖目录里已有的同名截图；只输出本次实际写出的文件（JSON）。
// 与对外导出同一口径（N02）：不运行页面脚本；每页按导出的做法展开（解除 hidden/display/visibility/opacity/content-visibility 隐藏）。
// 靠脚本显示或隐藏内容的稿件，截图与对外版都是“不运行脚本”的样子。
import fs from 'node:fs';
import path from 'node:path';
import {createRequire} from 'node:module';
import {pathToFileURL} from 'node:url';
const require = createRequire(import.meta.url);
const [html, out, list, quality, ordinalsArg, totalArg] = process.argv.slice(2);
const ordinals = JSON.parse(ordinalsArg || '[]'); const sections = Number(totalArg);
const base = process.env.HARNESS_NODE_MODULES;
const {chromium} = require(base ? path.join(base, 'playwright') : 'playwright');
const wanted = (list || '').split(',').filter(Boolean).map(Number);
const browser = await chromium.launch({channel: 'chrome', headless: true, args: ['--disable-background-networking']});
try {
  const context = await browser.newContext({viewport: {width: 1280, height: 720}, offline: true, javaScriptEnabled: false});
  const page = await context.newPage();
  await page.goto(pathToFileURL(path.resolve(html)).href, {waitUntil: 'load'});
  const found = await page.evaluate(({ordinals, sections}) => {
    const all = [...document.querySelectorAll('section')];
    if (all.length !== sections) return {error: `浏览器解析出 ${all.length} 个 <section>，源码解析为 ${sections} 个`};
    ordinals.forEach((n, i) => all[n].setAttribute('data-harness-shot', String(i + 1)));
    return {total: ordinals.length};
  }, {ordinals, sections});
  if (found.error) { console.error('页面结构解析不一致：' + found.error); process.exitCode = 2; await browser.close(); process.exit(); }
  const total = found.total;
  const missing = wanted.filter(n => !Number.isInteger(n) || n < 1 || n > total);
  if (missing.length) { console.error(`请求的页不存在：${missing.join(',')}（共 ${total} 页）`); process.exitCode = 2; }
  else {
    const pages = wanted.length ? wanted : Array.from({length: total}, (_, i) => i + 1);
    const files = pages.map(i => path.join(out, `第${String(i).padStart(2, '0')}页.jpg`));
    const exists = files.filter(f => fs.existsSync(f));
    if (exists.length) { console.error(`目录里已有同名截图，不覆盖：${exists.map(f => path.basename(f)).join('、')}；换一个新目录`); process.exitCode = 3; }
    else {
      for (const [k, i] of pages.entries()) {
        const el = page.locator(`section[data-harness-shot="${i}"]`);
        // 解除隐藏：内联 !important 压过类样式与 hidden 属性（只在截图浏览器里，不改文件）
        await el.evaluate(node => {
          node.hidden = false; const cs = getComputedStyle(node);
          if (cs.display === 'none') node.style.setProperty('display', 'block', 'important');
          node.style.setProperty('visibility', 'visible', 'important');
          if (parseFloat(cs.opacity) < 1) node.style.setProperty('opacity', '1', 'important');
          if (cs.contentVisibility && cs.contentVisibility !== 'visible') node.style.setProperty('content-visibility', 'visible', 'important');
        });
        await el.screenshot({path: files[k], type: 'jpeg', quality: Number(quality) || 70});
      }
      console.log(JSON.stringify(files));
    }
  }
} finally { await browser.close(); }
