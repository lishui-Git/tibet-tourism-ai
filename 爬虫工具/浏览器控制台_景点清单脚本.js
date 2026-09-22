/* ============================================================================
 * 携程西藏景点清单导出 · 浏览器控制台脚本
 * ----------------------------------------------------------------------------
 * 用途：把“西藏景点列表”整页导出成 CSV（景点名 / URL / 携程显示点评数）
 *       用来回答两个问题：① 哪些景点点评数最多（值得优先抓）
 *                       ② 哪些景点你的数据集里还没有（可能需要新增）
 *
 * 用法：
 *   1) 用你自己的 Chrome 打开：https://you.ctrip.com/sight/tibet100003/s0-p1.html
 *   2) 按 F12 → Console → 把本文件全部内容粘进去 → 回车
 *   3) 它自动翻页，跑完自动下载 CSV
 * ========================================================================== */
(async function () {
  'use strict';
  const CFG = {
    maxPages: 60,        // 列表最多翻多少页（一般 20~50 页就到底）
    delayMs: 2500,       // 每页间隔
    waitChangeMs: 15000, // 等新页面加载
    storeKey: '__ctrip_poi_list_v1',
  };

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const log = (...a) => console.log('%c[清单]', 'color:#06c;font-weight:bold', ...a);
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();

  const parseCnt = (txt) => {
    const m = (txt || '').match(/([\d,]+(?:\.\d+)?)\s*(万?)\s*条点评/);
    if (!m) return '';
    return Math.round(parseFloat(m[1].replace(/,/g, '')) * (m[2] ? 10000 : 1));
  };

  function collect() {
    const out = [];
    document.querySelectorAll('a[href*="/sight/"]').forEach((a) => {
      const name = clean(a.innerText);
      const href = a.href || '';
      if (!name || name.length > 40 || !/\.html/.test(href)) return;
      // 向上找带“条点评”的卡片容器
      let card = a, txt = '';
      for (let i = 0; i < 6 && card; i++) {
        card = card.parentElement;
        if (card && /条点评/.test(card.innerText || '')) { txt = card.innerText || ''; break; }
      }
      out.push({ name, url: href.split('?')[0], cnt: parseCnt(txt) });
    });
    return out;
  }

  function signature() {
    const first = document.querySelector('a[href*="/sight/"]');
    return first ? clean(first.innerText).slice(0, 60) : '';
  }

  function findNext() {
    const c = [...document.querySelectorAll('a,button,li,span,div')]
      .filter((e) => clean(e.textContent) === '下一页' && e.querySelectorAll('*').length <= 1);
    return c.length ? c[c.length - 1] : null;
  }

  function disabled(el) {
    if (!el) return true;
    let n = el;
    for (let i = 0; i < 3 && n; i++) {
      const cls = (typeof n.className === 'string' ? n.className : '') || '';
      if (/disabled|forbid/i.test(cls)) return true;
      n = n.parentElement;
    }
    return false;
  }

  // 断点续跑
  let DB = {};
  try { DB = JSON.parse(localStorage.getItem(CFG.storeKey) || '{}'); } catch (e) { DB = {}; }
  if (Object.keys(DB).length) log(`已有 ${Object.keys(DB).length} 条历史记录，将继续累加`);

  function exportCSV() {
    const rows = Object.values(DB).sort((a, b) => (b.cnt || 0) - (a.cnt || 0));
    const esc = (v) => (/[",\n]/.test(String(v)) ? '"' + String(v).replace(/"/g, '""') + '"' : String(v));
    const csv = '\ufeff景点名称,URL,携程显示点评数\r\n' +
      rows.map((r) => [r.name, r.url, r.cnt].map(esc).join(',')).join('\r\n');
    const a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([csv], { type: 'text/csv;charset=utf-8' }));
    a.download = `携程西藏景点清单_${new Date().toISOString().slice(0, 10)}.csv`;
    document.body.appendChild(a); a.click(); a.remove();
    log(`已下载，共 ${rows.length} 个景点`);
  }
  window.ctripPoiExport = exportCSV;

  let pageNo = 0;
  for (let p = 1; p <= CFG.maxPages; p++) {
    const items = collect();
    if (!items.length) { log(`第 ${p} 页没解析到景点，结束`); break; }
    let n = 0;
    items.forEach((it) => { if (!DB[it.url]) { DB[it.url] = it; n++; } });
    localStorage.setItem(CFG.storeKey, JSON.stringify(DB));
    pageNo = p;
    log(`第 ${p} 页：本页 ${items.length} 个，新增 ${n} 个，累计 ${Object.keys(DB).length} 个`);
    if (p >= CFG.maxPages) break;

    const next = findNext();
    if (!next || disabled(next)) { log('已到最后一页'); break; }
    const oldSig = signature();
    next.click();
    const t0 = Date.now();
    let changed = false;
    while (Date.now() - t0 < CFG.waitChangeMs) {
      await sleep(600);
      if (signature() && signature() !== oldSig) { changed = true; break; }
    }
    if (!changed) { log('点击下一页后内容未变化，结束'); break; }
    await sleep(CFG.delayMs);
  }

  log(`完成：共 ${pageNo} 页，${Object.keys(DB).length} 个景点`);
  exportCSV();
  log('清空重来：localStorage.removeItem("' + CFG.storeKey + '")');
})();
