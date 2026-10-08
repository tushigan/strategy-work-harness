// 对外导出的浏览器一侧（由 duiwai_daochu.py 调用；stdin 传 JSON，stdout 回 JSON）。不联网、不运行页面脚本。
//   prune  <工作区>          源稿按浏览器实际渲染判断可见性（屏幕与打印两种媒体都可见才算），只留可见内容，
//                            去掉注释、元数据、非白名单属性、表单控件、SVG 标题/描述；样式按最终页面重建（去注释、去不用的规则）。
//                            同时返回源稿“可见文字”（含伪元素文字），供与删除无关的核对使用。
//   verify <工作区> <pdf>    载入写好的对外 HTML（只允许它自己，其它请求一律拦下并记为问题），不判断可见性，
//                            取出全部文字（文本节点、属性文字、样式里的 content 字符串、标题），列出禁出现项，再打印 PDF。
// 页码定义来自 html_huamian（第几页 = 第几个顶层页面在全部 <section> 里的序号）；浏览器里 section 总数对不上即拒绝。
// 讲者稿/工具栏的类名与 id 是 html_huamian 给出的精确名单（不按“含 -note”之类的片段猜）。
// 反向核对（与可见性判断无关）：被判为不可见而要删的文字，逐页截图比较“显示 / 隐去这段文字”前后的实际画面，
// 屏幕与打印两种媒体下画面都会变的，就是源稿上看得见却被删的字，逐条返回（lostVisible），由调用方拒绝导出。
import fs from 'node:fs';
import path from 'node:path';
import zlib from 'node:zlib';
import {createRequire} from 'node:module';

const require = createRequire(import.meta.url);
const [mode, rootArg, pdfPath] = process.argv.slice(2);
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const root = fs.realpathSync(rootArg);
const ORIGIN = 'http://deck.invalid';
const TYPES = {'.css': 'text/css', '.svg': 'image/svg+xml', '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
  '.gif': 'image/gif', '.webp': 'image/webp', '.avif': 'image/avif', '.bmp': 'image/bmp', '.woff': 'font/woff', '.woff2': 'font/woff2',
  '.ttf': 'font/ttf', '.otf': 'font/otf'};

function insideRoot(rel) {
  // 只读工作区内、路径任一级都不是符号链接的普通文件
  const target = path.resolve(root, rel);
  if (!target.startsWith(root + path.sep)) return null;
  let cur = root;
  for (const part of path.relative(root, target).split(path.sep)) {
    cur = path.join(cur, part);
    try { if (fs.lstatSync(cur).isSymbolicLink()) return null; } catch { return null; }
  }
  return fs.statSync(target).isFile() ? target : null;
}

let playwright;
try {
  const base = process.env.HARNESS_NODE_MODULES;
  playwright = require(base ? path.join(base, 'playwright') : 'playwright');
} catch (e) {
  console.log(JSON.stringify({fatal: 'no_playwright', message: String(e.message || e).slice(0, 300)}));
  process.exit(0);
}

const blocked = [];
const browser = await playwright.chromium.launch({channel: 'chrome', headless: true, args: ['--disable-background-networking']});
try {
  const context = await browser.newContext({javaScriptEnabled: false, viewport: {width: 1280, height: 720}});
  await context.route('**/*', async route => {
    const url = route.request().url();
    let u;
    try { u = new URL(url); } catch { blocked.push(url.slice(0, 120)); return route.abort(); }
    if (u.origin !== ORIGIN) { blocked.push(url.slice(0, 120)); return route.abort(); }
    const rel = decodeURIComponent(u.pathname).replace(/^\/+/, '');
    if (rel === input.path) return route.fulfill({body: input.html, contentType: 'text/html; charset=utf-8'});
    if (mode !== 'prune') { blocked.push(url.slice(0, 120)); return route.abort(); }
    const target = insideRoot(rel);
    if (!target) return route.fulfill({status: 404, body: ''});
    return route.fulfill({body: fs.readFileSync(target), contentType: TYPES[path.extname(target).toLowerCase()] || 'application/octet-stream'});
  });
  const page = await context.newPage();
  await page.goto(ORIGIN + '/' + input.path.split('/').map(encodeURIComponent).join('/'), {waitUntil: 'load'});
  if (mode === 'prune') console.log(JSON.stringify(await prune(page)));
  else if (mode === 'verify') console.log(JSON.stringify(await verify(page)));
  else throw new Error('未知模式 ' + mode);
} finally { await browser.close(); }

async function prune(page) {
  const prep = await page.evaluate(PREPARE, {ordinals: input.ordinals, total: input.sections, names: input.note_names});
  if (prep.error) return prep;
  for (const media of ['screen', 'print']) {
    await page.emulateMedia({media});
    await page.evaluate(FORCE_PAGES);
  }
  for (const media of ['screen', 'print']) {
    await page.emulateMedia({media});
    await page.evaluate(MEASURE, media);
  }
  const lostVisible = await probeLost(page);
  await page.emulateMedia({media: 'screen'});
  return {...await page.evaluate(PRUNE), lostVisible};
}

// ---- 反向核对：按实际画面判断“要删的文字”在源稿上是否看得见 ----

function decodePng(buffer) {
  // Chrome 截图：8 位、非隔行的 RGB/RGBA PNG
  let pos = 8, width = 0, height = 0, channels = 4; const idat = [];
  while (pos < buffer.length) {
    const len = buffer.readUInt32BE(pos); const type = buffer.toString('latin1', pos + 4, pos + 8); const data = buffer.subarray(pos + 8, pos + 8 + len);
    if (type === 'IHDR') {
      width = data.readUInt32BE(0); height = data.readUInt32BE(4);
      if (data[8] !== 8 || data[12] !== 0 || ![2, 6].includes(data[9])) throw new Error('不支持的截图格式');
      channels = data[9] === 6 ? 4 : 3;
    } else if (type === 'IDAT') idat.push(data);
    else if (type === 'IEND') break;
    pos += 12 + len;
  }
  const raw = zlib.inflateSync(Buffer.concat(idat)); const stride = width * channels; const out = Buffer.alloc(stride * height);
  for (let y = 0; y < height; y++) {
    const filter = raw[y * (stride + 1)]; const row = raw.subarray(y * (stride + 1) + 1, (y + 1) * (stride + 1));
    for (let x = 0; x < stride; x++) {
      const a = x >= channels ? out[y * stride + x - channels] : 0, b = y ? out[(y - 1) * stride + x] : 0;
      const c = x >= channels && y ? out[(y - 1) * stride + x - channels] : 0;
      let v = row[x];
      if (filter === 1) v += a; else if (filter === 2) v += b; else if (filter === 3) v += (a + b) >> 1;
      else if (filter === 4) { const p = a + b - c, pa = Math.abs(p - a), pb = Math.abs(p - b), pc = Math.abs(p - c); v += pa <= pb && pa <= pc ? a : pb <= pc ? b : c; }
      out[y * stride + x] = v & 255;
    }
  }
  return {width, height, channels, data: out};
}

function visiblyDifferent(a, b) {
  // 至少 4 个像素某一通道差 ≥24，且变化区域至少 3 像素高：与删除侧“字号 ≥3px、近同色差 ≤10、不透明度 <0.1 不算”同一量级
  const x = decodePng(a), y = decodePng(b);
  if (x.width !== y.width || x.height !== y.height) return true;
  let n = 0, top = Infinity, bottom = -1;
  for (let i = 0, p = 0; i < x.data.length; i += x.channels, p++) {
    if (Math.max(Math.abs(x.data[i] - y.data[i]), Math.abs(x.data[i + 1] - y.data[i + 1]), Math.abs(x.data[i + 2] - y.data[i + 2])) >= 24) {
      n++; const row = Math.floor(p / x.width); top = Math.min(top, row); bottom = Math.max(bottom, row);
    }
  }
  return n >= 4 && bottom - top + 1 >= 3;
}

async function probeLost(page) {
  const marked = await page.evaluate(MARK_PROBES);
  if (!marked.length) return [];
  const setHidden = ids => page.evaluate(ids => {
    const hide = new Set(ids);
    for (const probe of window.__harness.probes) probe.el.setAttribute('style', window.__harness.probeStyle + (hide.has(probe.id) ? window.__harness.hideStyle : ''));
  }, ids);
  const shot = pageNo => page.locator(`[data-harness-page="${pageNo}"]`).screenshot({animations: 'disabled', caret: 'hide', scale: 'css', type: 'png'});
  let candidates = marked;
  for (const media of ['screen', 'print']) {
    await page.emulateMedia({media});
    const visible = [];
    for (const pageNo of [...new Set(candidates.map(c => c.page))]) {
      const ids = candidates.filter(c => c.page === pageNo).map(c => c.id);
      await setHidden([]); const base = await shot(pageNo);
      let budget = 60;
      const find = async group => {  // 二分找出隐去后画面会变的那几段
        if (budget-- <= 0) { visible.push(...group); return; }  // 截图次数用尽：其余按“看得见”处理（偏向拒绝）
        await setHidden(group);
        if (!visiblyDifferent(base, await shot(pageNo))) return;
        if (group.length === 1) { visible.push(group[0]); return; }
        const half = group.length >> 1; await find(group.slice(0, half)); await find(group.slice(half));
      };
      await find(ids);
    }
    const keep = new Set(visible); candidates = candidates.filter(c => keep.has(c.id));
    if (!candidates.length) break;
  }
  await page.evaluate(UNMARK_PROBES);
  return candidates.map(c => ({page: c.page, text: c.text}));
}

// ---- 以下函数在页面里运行（按字符串传入，不依赖页面脚本） ----

function PREPARE({ordinals, total, names}) {
  const NOTE_CLASSES = new Set(names.classes), NOTE_IDS = new Set(names.ids);
  const DROP = 'script,template,noscript,aside,iframe,object,embed,[data-speaker-notes],[data-deck-toolbar]';
  const all = [...document.querySelectorAll('section')];
  if (all.length !== total) return {error: 'structure', message: `浏览器解析出 ${all.length} 个 <section>，源码解析为 ${total} 个`};
  const pages = ordinals.map(i => all[i]);
  const dropped = {};
  const count = (reason, n = 1) => { dropped[reason] = (dropped[reason] || 0) + n; };
  const conventional = el => el.matches(DROP) || [...el.classList].some(c => NOTE_CLASSES.has(c.toLowerCase())) || (el.id && NOTE_IDS.has(el.id.toLowerCase()));
  for (const p of pages) {
    for (let e = p; e; e = e.parentElement) if (conventional(e)) return {error: 'structure', message: '页面位于讲者稿/工具栏/脚本类元素之内，页码定义不一致'};
  }
  for (const el of [...document.querySelectorAll('*')]) {
    if (el.isConnected && conventional(el)) { count('讲者稿/工具栏/脚本/模板/aside'); el.remove(); }
  }
  // 注释
  const walker = document.createTreeWalker(document, NodeFilter.SHOW_COMMENT);
  const comments = []; while (walker.nextNode()) comments.push(walker.currentNode);
  comments.forEach(c => c.remove()); if (comments.length) count('注释', comments.length);
  // 页面之外的内容（包括页面外的页眉页脚导航、进度条）：只保留页面与页面的祖先
  const isPage = new Set(pages);
  const outside = node => {
    for (const child of [...node.childNodes]) {
      if (child.nodeType === 1 && isPage.has(child)) continue;
      if (child.nodeType === 1 && pages.some(p => child.contains(p))) { outside(child); continue; }
      if (child.nodeType === 3 && !child.data.trim()) continue;
      count('页面之外'); child.remove();
    }
  };
  outside(document.body);
  // head 只留样式、字符集与视口
  for (const el of [...document.head.children]) {
    const tag = el.localName;
    const keep = tag === 'style' || (tag === 'link' && /\bstylesheet\b/i.test(el.rel)) ||
      (tag === 'meta' && (el.hasAttribute('charset') || (el.name || '').toLowerCase() === 'viewport'));
    if (!keep) { if (tag !== 'title') count('元数据'); el.remove(); }
  }
  pages.forEach((p, i) => { p.setAttribute('data-harness-page', String(i + 1)); p.hidden = false; });
  window.__harness = {pages, dropped};
  const pe = document.createElement('style'); pe.id = '__harness_pe'; pe.textContent = '*{pointer-events:auto!important}';
  document.head.append(pe);
  return {ok: true};
}

function FORCE_PAGES() {
  // 页面在演示时逐页切换（常见做法是类样式隐藏非当前页）：导出时每页都展开
  for (const p of window.__harness.pages) {
    const cs = getComputedStyle(p);
    if (cs.display === 'none') p.style.setProperty('display', 'block', 'important');
    if (cs.visibility !== 'visible') p.style.setProperty('visibility', 'visible', 'important');
    if (parseFloat(cs.opacity) < 1) p.style.setProperty('opacity', '1', 'important');
    if (cs.contentVisibility && cs.contentVisibility !== 'visible') p.style.setProperty('content-visibility', 'visible', 'important');
  }
}

function MEASURE(media) {
  const H = window.__harness;
  H.problems = H.problems || [];
  const alpha = c => { const m = /rgba?\(([^)]+)\)/.exec(c || ''); if (!m) return c === 'transparent' ? 0 : 1; const v = m[1].split(/[\s,/]+/).filter(Boolean); return v.length > 3 ? parseFloat(v[3]) * (v[3].endsWith('%') ? 0.01 : 1) : 1; };
  const opacity = el => {
    let o = 1;
    for (let e = el; e && e.nodeType === 1; e = e.parentElement) {
      const cs = getComputedStyle(e); o *= parseFloat(cs.opacity);
      for (const m of (cs.filter || '').matchAll(/opacity\(\s*([\d.]+)(%?)\s*\)/g)) o *= m[2] ? parseFloat(m[1]) / 100 : parseFloat(m[1]);
    }
    return o;
  };
  const inter = (a, b) => ({left: Math.max(a.left, b.left), top: Math.max(a.top, b.top), right: Math.min(a.right, b.right), bottom: Math.min(a.bottom, b.bottom)});
  const area = r => r.right - r.left > 0.5 && r.bottom - r.top > 0.5;
  const pageOf = el => H.pages.find(p => p.contains(el));
  // 裁切区域：页面本身 ∩ 每个 overflow 非 visible / contain:paint 的祖先 ∩ clip:rect()
  const clipRegion = (el, page) => {
    let region = page.getBoundingClientRect(); region = {left: region.left, top: region.top, right: region.right, bottom: region.bottom};
    let hit = false;
    for (let e = el; e && e !== page.parentElement; e = e.parentElement) {
      const cs = getComputedStyle(e); const r = e.getBoundingClientRect();
      if (cs.overflowX !== 'visible' || cs.overflowY !== 'visible' || /paint|strict|content/.test(cs.contain)) {
        region = inter(region, {left: cs.overflowX !== 'visible' ? r.left : -1e9, right: cs.overflowX !== 'visible' ? r.right : 1e9,
          top: cs.overflowY !== 'visible' ? r.top : -1e9, bottom: cs.overflowY !== 'visible' ? r.bottom : 1e9});
      }
      const m = /rect\(([^)]+)\)/.exec(cs.clip || '');
      if (m && /absolute|fixed/.test(cs.position)) {
        const v = m[1].split(/[\s,]+/).map(x => x === 'auto' ? null : parseFloat(x));
        region = inter(region, {top: r.top + (v[0] ?? 0), right: v[1] == null ? r.right : r.left + v[1], bottom: v[2] == null ? r.bottom : r.top + v[2], left: r.left + (v[3] ?? 0)});
      }
      if (cs.clipPath && cs.clipPath !== 'none') hit = true;
      if (cs.maskImage && cs.maskImage !== 'none' || cs.webkitMaskImage && cs.webkitMaskImage !== 'none') region.mask = true;
    }
    region.hit = hit;
    return region;
  };
  const hitVisible = (el, rects) => {
    el.scrollIntoView({block: 'center', inline: 'center'});
    for (const r0 of rects()) {
      for (const [fx, fy] of [[0.5, 0.5], [0.2, 0.5], [0.8, 0.5], [0.5, 0.2], [0.5, 0.8]]) {
        const x = r0.left + (r0.right - r0.left) * fx, y = r0.top + (r0.bottom - r0.top) * fy;
        if (x < 0 || y < 0 || x >= innerWidth || y >= innerHeight) continue;
        if (document.elementsFromPoint(x, y).includes(el)) return true;
      }
    }
    return false;
  };
  const visibleBox = (el, rectsFn, minHeight = 0) => {
    if (!el.checkVisibility({checkOpacity: true, checkVisibilityCSS: true, opacityProperty: true, visibilityProperty: true})) return false;
    if (opacity(el) < 0.1) return false;
    const page = pageOf(el); if (!page) return false;
    const region = clipRegion(el, page);
    // 文字的排版框至少 minHeight 像素高：变换缩到几乎看不见（scale(0.01)）的文字不算可见
    const rects = rectsFn().map(r => inter(r, region)).filter(r => area(r) && r.bottom - r.top >= minHeight);
    if (!rects.length) return false;
    if (region.hit && !hitVisible(el, () => rectsFn().map(r => inter(r, clipRegion(el, page))).filter(area))) return false;
    if (region.mask) return 'mask';
    return true;
  };
  // 被盖住或与背景同色（M01）：只在文字所在位置“全部为纯色”时才判断。取样点上（上层或下层）只要有
  // img/svg/video/picture/canvas 等图形、背景图或渐变（含伪元素的背景图）、边框图、混合模式/滤镜，背景一律按“未知”处理：
  // 不做同色判断，也不算被盖住（文字写出；是否真看得见由人回读）。半透明的底同样按未知处理。
  const rgb = c => { const m = /rgba?\(([^)]+)\)/.exec(c || ''); return m ? m[1].split(/[\s,/]+/).filter(Boolean).slice(0, 3).map(Number) : null; };
  const solid = el => { const cs = getComputedStyle(el); return cs.backgroundImage === 'none' && alpha(cs.backgroundColor) >= 0.95 && opacity(el) >= 0.95; };
  const sameColor = (a, b) => { const x = rgb(a), y = rgb(b); return !!(x && y) && x.every((v, i) => Math.abs(v - y[i]) <= 10); };
  const GRAPHIC = new Set(['img', 'svg', 'video', 'picture', 'canvas', 'object', 'embed', 'iframe', 'image']);
  const unknownCache = new Map();
  const unknownLayer = e => {
    if (unknownCache.has(e)) return unknownCache.get(e);
    let unknown = GRAPHIC.has(e.localName) || e.namespaceURI === 'http://www.w3.org/2000/svg';
    if (!unknown) {
      const cs = getComputedStyle(e);
      unknown = cs.backgroundImage !== 'none' || (cs.borderImageSource && cs.borderImageSource !== 'none') || cs.mixBlendMode !== 'normal' ||
        cs.filter !== 'none' || (cs.backdropFilter && cs.backdropFilter !== 'none') ||
        ['::before', '::after'].some(which => {
          const ps = getComputedStyle(e, which);
          return ps.content !== 'none' && ps.content !== 'normal' && (ps.backgroundImage !== 'none' || /url\(|image-set\(|gradient\(/.test(ps.content));
        });
    }
    unknownCache.set(e, unknown);
    return unknown;
  };
  const hiddenByPaint = (n, parent, cs) => {
    parent.scrollIntoView({block: 'center', inline: 'center'});
    range.selectNodeContents(n);
    const points = [];
    for (const r of range.getClientRects()) {
      if (r.width < 1 || r.height < 1) continue;
      for (const fx of [0.2, 0.5, 0.8]) points.push([r.left + r.width * fx, r.top + r.height / 2]);
    }
    const inView = points.filter(([x, y]) => x >= 0 && y >= 0 && x < innerWidth && y < innerHeight);
    if (!inView.length) return false;  // 判断不了就不据此删
    const plain = cs.textShadow === 'none' && !(parseFloat(cs.webkitTextStrokeWidth || 0) > 0);
    let covered = true, same = plain;
    for (const [x, y] of inView) {
      const stack = document.elementsFromPoint(x, y); const i = stack.indexOf(parent);
      if (i < 0) return false;
      if (stack.some(unknownLayer)) return false;  // 该位置不是全部纯色：背景未知
      if (!stack.slice(0, i).some(e => !e.contains(parent) && solid(e))) covered = false;
      if (same) {
        let bg = 'rgb(255, 255, 255)';
        for (const e of stack.slice(i)) {
          const a = alpha(getComputedStyle(e).backgroundColor);
          if (a <= 0.05) continue;
          if (a < 0.95 || opacity(e) < 0.95) return false;  // 半透明的底：未知
          bg = getComputedStyle(e).backgroundColor; break;
        }
        if (!sameColor(cs.webkitTextFillColor || cs.color, bg)) same = false;
      }
    }
    return covered || same;
  };
  const key = 'v_' + media;
  const range = document.createRange();
  for (const page of H.pages) {
    const tw = document.createTreeWalker(page, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT);
    for (let n = tw.currentNode; n; n = tw.nextNode()) {
      if (n.nodeType === 1) {
        n[key] = visibleBox(n, () => [...n.getClientRects()]) === true;
        // 伪元素文字：只认普通流里的、显示且不透明不过小的
        n[key + '_pseudo'] = [];
        if (n[key]) for (const which of ['::before', '::after']) {
          const ps = getComputedStyle(n, which); const content = ps.content;
          if (!content || content === 'none' || content === 'normal' || ps.display === 'none') continue;
          if (/attr\(|var\(/.test(content)) { H.problems.push('样式 content 用了 attr()/var()，对外导出无法核对其文字'); continue; }
          const ok = ps.visibility === 'visible' && parseFloat(ps.opacity) >= 0.1 && parseFloat(ps.fontSize) >= 3 &&
            (alpha(ps.webkitTextFillColor || ps.color) > 0.05) && /static|relative/.test(ps.position) && ps.transform === 'none' && parseFloat(ps.textIndent || 0) > -100;
          const strings = [...content.matchAll(/"((?:[^"\\]|\\.)*)"|'((?:[^'\\]|\\.)*)'/g)].map(m => (m[1] ?? m[2]).replace(/\\(.)/g, '$1'));
          if (ok) n[key + '_pseudo'].push(...strings);
        }
      } else if (n.data.trim()) {
        const parent = n.parentElement;
        const cs = getComputedStyle(parent);
        let ok = parseFloat(cs.fontSize) >= 3;
        // 关闭的 <details>：除第一个 <summary> 外的内容不显示（Chrome 仍会给出排版框，须单独判断）
        for (let d = parent.closest('details'); d && ok; d = d.parentElement && d.parentElement.closest('details')) {
          const summary = [...d.children].find(c => c.localName === 'summary');
          if (!d.open && !(summary && summary.contains(n))) ok = false;
        }
        let fill = alpha(cs.webkitTextFillColor || cs.color);
        let stroke = parseFloat(cs.webkitTextStrokeWidth || 0) > 0 && alpha(cs.webkitTextStrokeColor) > 0.05;
        if (parent.namespaceURI === 'http://www.w3.org/2000/svg') {
          // SVG 文字由 fill / stroke 上色（color 不起作用）
          fill = cs.fill === 'none' ? 0 : alpha(cs.fill) * parseFloat(cs.fillOpacity || 1);
          stroke = cs.stroke !== 'none' && parseFloat(cs.strokeWidth || 0) > 0 && alpha(cs.stroke) * parseFloat(cs.strokeOpacity || 1) > 0.05;
        }
        // 文字颜色透明但仍看得见的写法（M01 同类）：背景裁成文字形状（background-clip:text 渐变/图片字）、只靠不透明的文字阴影显示
        const clipText = () => { for (let e = parent; e && e.nodeType === 1; e = e.parentElement) { const s = getComputedStyle(e);
          if ((s.backgroundClip === 'text' || s.webkitBackgroundClip === 'text') && (s.backgroundImage !== 'none' || alpha(s.backgroundColor) > 0.05)) return true; } return false; };
        const shadow = () => [...(cs.textShadow || '').matchAll(/rgba?\([^)]*\)/g)].some(m => alpha(m[0]) > 0.05);
        if (ok && fill <= 0.05 && !stroke && !shadow() && !clipText()) ok = false;
        if (ok) {
          range.selectNodeContents(n);
          let v = visibleBox(parent, () => { range.selectNodeContents(n); return [...range.getClientRects()]; }, 3);  // 没有排版框（如关闭的 details 内容）即不可见
          if (v === 'mask') { H.problems.push('文字所在元素用了 mask 遮罩，无法判定是否可见：去掉遮罩或改用图片后再导出'); v = false; }
          if (v === true && parent.namespaceURI !== 'http://www.w3.org/2000/svg' && hiddenByPaint(n, parent, cs)) v = false;
          ok = v;
        }
        n[key] = !!ok;
      }
    }
  }
}

function MARK_PROBES() {
  // 要删的文字（不是两种媒体都可见）逐段套上探针元素：样式 all:unset 让它与原文字同样排版，隐去时只改颜色、不动排版。
  // 不参与反向核对：按约定删去的类别（表单控件、SVG 标题/描述、样式/脚本文字）与不渲染的替代内容
  const PROBE_SKIP = 'input,textarea,select,option,optgroup,datalist,button,output,progress,meter,label,form,fieldset,legend,' +
    'style,script,template,noscript,title,desc,metadata,video,audio,canvas,iframe,object,embed';
  const H = window.__harness; const SVG = 'http://www.w3.org/2000/svg';
  H.probeStyle = 'all:unset!important;';
  H.hideStyle = 'color:transparent!important;-webkit-text-fill-color:transparent!important;-webkit-text-stroke-color:transparent!important;' +
    'text-shadow:none!important;text-decoration-color:transparent!important;text-emphasis-color:transparent!important;' +
    'fill:transparent!important;stroke:transparent!important;visibility:hidden!important;';
  H.probes = []; const out = [];
  H.pages.forEach((page, index) => {
    const tw = document.createTreeWalker(page, NodeFilter.SHOW_TEXT); const nodes = [];
    for (let n = tw.nextNode(); n; n = tw.nextNode()) nodes.push(n);
    for (const n of nodes) {
      if (!n.data.trim() || (n.v_screen && n.v_print)) continue;
      const parent = n.parentElement;
      if (!parent || parent.closest(PROBE_SKIP) || !parent.checkVisibility({visibilityProperty: true})) continue;
      let el;
      if (parent.namespaceURI === SVG) {
        if (!['text', 'tspan', 'textPath', 'a'].includes(parent.localName) || !parent.closest('text')) continue;  // 其它 SVG 位置的文字不渲染
        el = document.createElementNS(SVG, 'tspan');
      } else el = document.createElement('harness-probe');
      el.setAttribute('style', H.probeStyle);
      n.replaceWith(el); el.append(n);
      const id = H.probes.length; H.probes.push({id, el, node: n});
      out.push({id, page: index + 1, text: n.data.trim().replace(/\s+/g, ' ').slice(0, 40)});
    }
  });
  return out;
}

function UNMARK_PROBES() {
  for (const probe of window.__harness.probes || []) probe.el.replaceWith(probe.node);
  window.__harness.probes = [];
}

function PRUNE() {
  const H = window.__harness; const dropped = H.dropped; const problems = H.problems || [];
  const count = (reason, n = 1) => { dropped[reason] = (dropped[reason] || 0) + n; };
  const both = n => n.v_screen && n.v_print;
  const FORM = new Set(['input', 'textarea', 'select', 'option', 'optgroup', 'datalist', 'button', 'output', 'progress', 'meter', 'label', 'form', 'fieldset', 'legend']);
  const SVG_META = new Set(['title', 'desc', 'metadata']);
  const UNSUPPORTED = new Set(['video', 'audio', 'canvas', 'iframe', 'embed', 'object', 'portal']);
  const SVG = 'http://www.w3.org/2000/svg';
  // 不承载文字的结构元素：自身没有可见框也保留（换行、表格列、SVG 定义）；其中不可见的文字照样删
  const STRUCTURAL = new Set(['br', 'wbr', 'col', 'colgroup']);
  const SVG_NONRENDER = new Set(['defs', 'lineargradient', 'radialgradient', 'stop', 'clippath', 'mask', 'pattern', 'filter', 'marker', 'symbol']);
  // 源稿可见文字（逐页；含伪元素文字）—— 在删除之前、按文档顺序取
  const sourceText = H.pages.map(page => {
    const parts = []; const tw = document.createTreeWalker(page, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT);
    for (let n = tw.currentNode; n; n = tw.nextNode()) {
      if (n.nodeType === 3) { if (both(n)) parts.push(n.data); }
      else {
        const s = (n.v_screen_pseudo || []).filter(x => (n.v_print_pseudo || []).includes(x));
        if (s.length) parts.push(' ' + s.join(' ') + ' ');
      }
    }
    return parts.join('');
  });
  // 反向核对用：逐页两种媒体都可见的文字节点（不含伪元素；不含按约定删去的表单控件与 SVG 标题/描述里的字）
  const sourcePages = H.pages.map(page => {
    const parts = []; const tw = document.createTreeWalker(page, NodeFilter.SHOW_TEXT);
    for (let n = tw.nextNode(); n; n = tw.nextNode()) {
      const p = n.parentElement;
      if (both(n) && n.data.trim() && !(p && p.closest('input,textarea,select,option,optgroup,datalist,button,output,progress,meter,label,form,fieldset,legend,title,desc,metadata'))) parts.push(n.data);
    }
    return parts.join('');
  });
  // 自下而上删除：文字两种媒体都可见才留；元素自身可见或有留下的后代才留
  let droppedChars = 0;
  const visit = el => {
    let kept = false;
    for (const child of [...el.childNodes]) {
      if (child.nodeType === 3) {
        if (!child.data.trim()) continue;
        if (both(child)) kept = true; else { droppedChars += child.data.trim().length; child.remove(); }
      } else if (child.nodeType === 1) {
        const tag = child.localName;
        if (tag === 'style' || tag === 'link') continue;  // 样式在重建后统一去掉
        if (child.namespaceURI === SVG && SVG_META.has(tag)) { count('SVG 标题/描述'); child.remove(); continue; }
        if (FORM.has(tag) && child.namespaceURI !== SVG) { count('表单控件'); droppedChars += (child.textContent || '').trim().length; child.remove(); continue; }
        if (tag === 'source' || tag === 'track') { child.remove(); continue; }
        const childKept = visit(child);
        if (childKept || both(child)) {
          kept = true;
          if (UNSUPPORTED.has(tag)) problems.push(`画面里有 <${tag}>，对外导出暂不支持：改为图片或去掉后再导出`);
        } else if (STRUCTURAL.has(tag) || (child.namespaceURI === SVG && (SVG_NONRENDER.has(tag.toLowerCase()) || /^fe[A-Z]/.test(tag)))) {
          continue;
        } else {
          count('不可见（屏幕或打印）'); child.remove();
        }
      }
    }
    return kept;
  };
  H.pages.forEach(visit);
  // 属性白名单；资源地址改成绝对地址（由调用方内嵌）
  const ALLOW = new Set(['class', 'id', 'style', 'width', 'height', 'colspan', 'rowspan', 'lang', 'dir', 'start', 'reversed', 'span', 'scope', 'headers', 'charset', 'name', 'content']);
  const resources = new Set();
  const absolute = (value, base) => value.replace(/url\(\s*(['"]?)([^'")]+)\1\s*\)/g, (m, q, u) => {
    if (u.startsWith('data:')) return m;
    const abs = new URL(u, base).href; resources.add(abs); return `url("${abs}")`;
  });
  for (const el of document.querySelectorAll('*')) {
    const svg = el.namespaceURI === SVG; const tag = el.localName;
    if (tag === 'style' || tag === 'link') continue;  // 样式在重建后整体替换；先动它会让样式表失效
    for (const attr of [...el.attributes]) {
      const name = attr.name.toLowerCase(); let value = attr.value; let keep;
      if (name.startsWith('on') || name.startsWith('data-') || name.startsWith('aria-') || name === 'xmlns:xlink' && false) keep = false;
      else if (name === 'data-harness-page') keep = false;
      else if (name === 'src' && (tag === 'img')) { value = new URL(value, document.baseURI).href; if (!value.startsWith('data:')) resources.add(value); keep = true; }
      else if ((name === 'href' || name === 'xlink:href') && svg) {
        if (value.startsWith('#') || value.startsWith('data:')) keep = true;
        else { value = new URL(value, document.baseURI).href; resources.add(value); keep = true; }
      }
      else if (name === 'href' && tag === 'a') keep = /^(https?:|mailto:)/i.test(value);
      else if (name === 'style') { value = absolute(value, document.baseURI); keep = true; }
      else if (name === 'type') keep = tag === 'ol' || tag === 'ul' || tag === 'li';
      else if (name === 'value') keep = tag === 'li' && /^-?\d+$/.test(value);
      else if (name === 'name' || name === 'content') keep = tag === 'meta' && (el.getAttribute('name') || '').toLowerCase() === 'viewport';
      else if (svg) keep = !['title', 'alt', 'aria-label', 'role', 'tabindex', 'lang', 'xml:lang'].includes(name);
      else keep = ALLOW.has(name);
      if (!keep) { el.removeAttribute(attr.name); count('属性文字/元数据属性'); }
      else if (value !== attr.value) el.setAttribute(attr.name, value);
    }
    if (tag === 'img') el.removeAttribute('srcset');
  }
  for (const s of document.querySelectorAll('picture > source')) s.remove();
  // 样式：按最终页面重建，只留会用到的规则；不保留注释与原有 <style>/<link>
  const STRIP = /::?(?:before|after|first-line|first-letter|marker|placeholder|selection|backdrop|file-selector-button|-webkit-[\w-]+|-moz-[\w-]+)(?:\([^)]*\))?|:(?:hover|focus|focus-visible|focus-within|active|visited|target|link|any-link)\b/g;
  const used = selector => selector.split(',').some(part => {
    const s = part.replace(STRIP, '').trim() || '*';
    try { return !!document.querySelector(s); } catch { return true; }
  });
  const compact = text => {
    let out = '', q = null;
    for (let i = 0; i < text.length; i++) {
      const c = text[i];
      if (q) { out += c; if (c === '\\') { out += text[++i] || ''; } else if (c === q) q = null; continue; }
      if (c === '"' || c === "'") { q = c; out += c; continue; }
      if (/\s/.test(c) && /[:;{,]$/.test(out)) continue;
      out += c;
    }
    return out.trim();
  };
  const rules = (list, base) => {
    const out = [];
    for (const r of list) {
      if (r instanceof CSSStyleRule) {
        if (!used(r.selectorText)) continue;
        const nested = r.cssRules && r.cssRules.length ? rules(r.cssRules, base).join('') : '';
        out.push(`${r.selectorText}{${compact(absolute(r.style.cssText, base))}${nested}}`);
      } else if (r instanceof CSSImportRule) {
        problems.push('样式含 @import，对外导出不支持；把被导入的样式合并进来');
      } else if (r instanceof CSSPageRule) {
        out.push(r.cssText);
      } else if (r instanceof CSSFontFaceRule) {
        out.push(`@font-face{${compact(absolute(r.style.cssText, base))}}`);
      } else if (r.cssRules && !(r instanceof CSSKeyframesRule)) {
        const inner = rules(r.cssRules, base);
        if (inner.length) out.push(r.cssText.slice(0, r.cssText.indexOf('{')).trim() + '{' + inner.join('') + '}');
      } else if (r instanceof CSSNamespaceRule) {
        continue;
      } else {
        out.push(absolute(r.cssText, base));
      }
    }
    return out;
  };
  const css = [];
  for (const sheet of document.styleSheets) {
    if (sheet.ownerNode && sheet.ownerNode.id === '__harness_pe') continue;
    let list; try { list = sheet.cssRules; } catch { problems.push('有样式表无法读取：' + (sheet.href || '内联')); continue; }
    css.push(...rules(list, sheet.href || document.baseURI));
  }
  for (const el of [...document.querySelectorAll('style, link')]) el.remove();
  // 每页一张：页面展开并分页
  H.pages.forEach((p, i) => {
    p.style.setProperty('break-inside', 'avoid');
    if (i < H.pages.length - 1) p.style.setProperty('break-after', 'page');
  });
  const style = document.createElement('style');
  style.textContent = css.join('\n') + '\n@media print{html,body{margin:0!important;padding:0!important}}';
  document.head.append(style);
  let title = document.querySelector('title');
  if (!title) { title = document.createElement('title'); document.head.prepend(title); }
  title.textContent = '对外演示稿';
  if (!document.querySelector('meta[charset]')) { const m = document.createElement('meta'); m.setAttribute('charset', 'utf-8'); document.head.prepend(m); }
  for (const el of document.querySelectorAll('[data-harness-page]')) el.removeAttribute('data-harness-page');
  return {html: '<!doctype html>\n' + document.documentElement.outerHTML, resources: [...resources], sourceText, sourcePages,
          pages: H.pages.length, dropped, droppedChars, problems: [...new Set(problems)]};
}

async function verify(page) {
  const found = await page.evaluate(() => {
    const issues = []; const texts = []; const pageTexts = [];
    const SVG = 'http://www.w3.org/2000/svg';
    const BAD = new Set(['script', 'template', 'noscript', 'aside', 'iframe', 'object', 'embed', 'link', 'base', 'input', 'textarea',
      'select', 'option', 'button', 'form', 'video', 'audio', 'canvas', 'source', 'track']);
    const SAFE = new Set(['class', 'id', 'style', 'src', 'href', 'width', 'height', 'colspan', 'rowspan', 'lang', 'dir', 'start',
      'reversed', 'span', 'scope', 'headers', 'charset', 'type', 'value', 'name', 'content']);
    const tops = [...document.querySelectorAll('section')].filter(s => !s.parentElement.closest('section'));
    const pageIndex = node => { for (let i = 0; i < tops.length; i++) if (tops[i].contains(node)) return i + 1; return 0; };
    const addCss = text => { (pageTexts[0] = pageTexts[0] || []).push(text); texts.push(text); };  // 样式文字算作页面之外（第 0 页）一并扫描
    const add = (node, text) => { const i = pageIndex(node); (pageTexts[i] = pageTexts[i] || []).push(text); texts.push(text); };
    const walker = document.createTreeWalker(document, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT | NodeFilter.SHOW_COMMENT);
    for (let n = walker.currentNode; n; n = walker.nextNode()) {
      if (n.nodeType === 8) { issues.push('HTML 注释'); continue; }
      if (n.nodeType === 3) {
        const parent = n.parentElement && n.parentElement.localName;
        if (parent === 'style') continue;
        if (parent === 'title') { if (n.data.trim() !== '对外演示稿') issues.push('标题不是中性标题'); continue; }
        if (n.data.trim()) add(n, n.data);
        continue;
      }
      if (n.nodeType !== 1) continue;
      const tag = n.localName; const svg = n.namespaceURI === SVG;
      if (BAD.has(tag) && !svg) issues.push(`<${tag}> 节点`);
      if (svg && ['title', 'desc', 'metadata'].includes(tag)) issues.push(`SVG <${tag}>`);
      if (tag === 'meta' && !n.hasAttribute('charset') && (n.getAttribute('name') || '').toLowerCase() !== 'viewport') issues.push('<meta> 元数据');
      for (const a of n.attributes) {
        const name = a.name.toLowerCase(), v = a.value;
        if (name.startsWith('on') || name.startsWith('data-') || name.startsWith('aria-') || ['hidden', 'contenteditable', 'alt', 'title', 'placeholder', 'label', 'summary'].includes(name))
          issues.push(`<${tag}> 属性 ${name}`);
        if (['src', 'href', 'xlink:href', 'poster', 'srcset', 'background'].includes(name) && !/^(data:|#)/.test(v) && !(tag === 'a' && /^(https?:|mailto:)/i.test(v)))
          issues.push(`<${tag}> 未内嵌的引用 ${name}=${v.slice(0, 60)}`);
        if (name === 'style' && /url\(\s*['"]?(?!data:)/.test(v)) issues.push(`<${tag}> 样式里未内嵌的引用`);
        // 属性文字：非安全属性全部计入；安全属性里含中文的也计入
        if (v && (!(SAFE.has(name) || svg) || /[㐀-鿿]/.test(v)) && !['src', 'href', 'xlink:href', 'style'].includes(name)) add(n, v);
      }
      if (tag === 'img' && n.hasAttribute('srcset')) issues.push('<img> srcset');
    }
    // 样式里的文字：所有 content 字符串（不论是否渲染）与字体名以外的中文
    for (const s of document.querySelectorAll('style')) {
      const text = s.textContent;
      if (/\/\*/.test(text)) issues.push('样式注释');
      if (/@import/i.test(text)) issues.push('样式 @import');
      if (/url\(\s*['"]?(?!data:)/.test(text.replace(/url\(\s*(['"]?)data:[^)]*\)/g, ''))) issues.push('样式里未内嵌的引用');
      const noFonts = text.replace(/url\(\s*(['"]?)data:[^)]*\)/g, '').replace(/font(?:-family)?\s*:[^;}]*/gi, '').replace(/local\([^)]*\)/gi, '');
      for (const m of noFonts.matchAll(/content\s*:\s*([^;}]*)/gi)) {
        if (/attr\(|var\(/.test(m[1])) issues.push('样式 content 用了 attr()/var()');
        for (const q of m[1].matchAll(/"((?:[^"\\]|\\.)*)"|'((?:[^'\\]|\\.)*)'/g))
          addCss((q[1] ?? q[2]).replace(/\\([0-9a-fA-F]{1,6})\s?/g, (x, h) => String.fromCodePoint(parseInt(h, 16))).replace(/\\(.)/g, '$1'));
      }
      for (const m of noFonts.replace(/content\s*:\s*[^;}]*/gi, '').matchAll(/[㐀-鿿][^"'{};:]*/g)) addCss(m[0]);
    }
    return {issues, texts, pageTexts: Array.from({length: tops.length + 1}, (_, i) => (pageTexts[i] || []).join(' ')), pages: tops.length};
  });
  await page.emulateMedia({media: 'print'});
  await page.pdf({path: pdfPath, width: '1280px', height: '720px', printBackground: true, margin: {top: '0', right: '0', bottom: '0', left: '0'}});
  if (blocked.length) found.issues.push('请求了未内嵌的外部资源：' + blocked.slice(0, 3).join('、'));
  return found;
}
