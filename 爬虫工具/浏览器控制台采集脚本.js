/* ============================================================================
 * 携程景点点评采集 · 浏览器控制台脚本（无需安装任何东西）
 * ----------------------------------------------------------------------------
 * 用法：
 *   1) 用你自己平时上网的 Chrome 打开某个景点的点评页（建议先登录携程账号）
 *      例如：https://you.ctrip.com/sight/lhasa36/3124.html
 *   2) 确认页面上能看到评论列表（能看到“下一页”）
 *   3) 按 F12 → 切到 Console（控制台）标签
 *   4) 把本文件【全部内容】复制粘贴进去 → 回车
 *   5) 它会自动翻页，跑完自动下载 CSV
 *
 * 重要：
 *   - 采集过程中不要关闭这个标签页、不要让电脑睡眠
 *   - 数据会实时存进 localStorage，万一页面崩了，重新打开页面后再粘一次本脚本，
 *     它会问你要不要先导出已存数据（输入 y 回车即可）
 *   - 想停：直接刷新页面，或在控制台执行 window.__ctripStop = true
 * ========================================================================== */
(async function () {
  'use strict';

  // ======================= 配置（一般不用改） =======================
  const CFG = {
    maxPages: 300,          // 最多翻多少页（携程硬顶 300 页）
    delayMs: 4000,          // 每页之间的间隔（毫秒），不要调太小
    waitChangeMs: 20000,    // 点击下一页后，等新内容出现的最大等待
    cardSelector: '',       // 留空自动探测；探测不准时再手工填
    downloadName: '',       // 留空自动命名
    storeKey: '__ctrip_reviews_v1',
  };

  const COLS = ['景点名称', '评论编号', '评分', '评分描述', '评论内容', '发布时间', 'IP归属地',
    '用户昵称', '点赞数', '图片数', '图片URL', '地址', '开放时间', '官方电话', '景点介绍'];

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const log = (...a) => console.log('%c[采集]', 'color:#0a0;font-weight:bold', ...a);
  const warn = (...a) => console.warn('[采集]', ...a);
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();

  // ======================= 断点：先把已存数据读出来 =======================
  const store = {
    read() { try { return JSON.parse(localStorage.getItem(CFG.storeKey) || '{}'); } catch (e) { return {}; } },
    write(o) { try { localStorage.setItem(CFG.storeKey, JSON.stringify(o)); } catch (e) { warn('localStorage 写入失败（可能满了）', e); } },
    clear() { localStorage.removeItem(CFG.storeKey); },
  };
  let DB = store.read();          // { 景点名: { 评论编号: row } }
  const savedSpots = Object.keys(DB);
  const savedCount = savedSpots.reduce((n, k) => n + Object.keys(DB[k]).length, 0);
  if (savedCount > 0) {
    const yes = confirm(`检测到上次已保存 ${savedCount} 条（景点：${savedSpots.join('、')}）。\n\n` +
      `点“确定”= 先把已保存数据导出成 CSV（推荐）\n点“取消”= 继续接着采集`);
    if (yes) { downloadCSV(allRows()); log('已导出历史数据。如需继续采集，请重新运行本脚本并点“取消”。'); return; }
  }

  // ======================= 1. 找到评论卡片 =======================
  function detectCards() {
    const dateRe = /20\d{2}-\d{2}-\d{2}/;
    const scoreRe = /(超棒|满意|不错|一般|不佳|[1-5]\s*分)/;
    const map = new Map();
    document.querySelectorAll('div,li,section,article').forEach((el) => {
      const t = el.innerText || '';
      if (t.length < 60 || t.length > 5000) return;
      if (!dateRe.test(t) || !scoreRe.test(t)) return;
      let sel = el.tagName.toLowerCase();
      const cls = (typeof el.className === 'string' ? el.className : '').trim();
      if (cls) sel += '.' + cls.split(/\s+/).slice(0, 2).join('.');
      const cur = map.get(sel) || { n: 0, len: 0 };
      cur.n += 1; cur.len += t.length;
      map.set(sel, cur);
    });
    return [...map.entries()]
      .map(([sel, v]) => ({ sel, count: v.n, avgLen: Math.round(v.len / v.n) }))
      .sort((a, b) => b.count - a.count);
  }

  let CARD_SEL = CFG.cardSelector;
  if (!CARD_SEL) {
    const cands = detectCards();
    log('候选评论卡片选择器：');
    cands.slice(0, 8).forEach((c) => log(`   ${c.sel}   命中 ${c.count} 个，平均长度 ${c.avgLen}`));
    const good = cands.find((c) => c.count >= 3 && c.avgLen >= 60 && c.avgLen <= 4000);
    if (!good) { alert('没有自动识别到评论卡片。请把控制台里“候选评论卡片选择器”那段发给我，我帮你定位。'); return; }
    CARD_SEL = good.sel;
  }
  log('使用选择器：', CARD_SEL);

  function getCards() {
    return [...document.querySelectorAll(CARD_SEL)];
  }

  // ======================= 2. 字段提取 =======================
  const RE = {
    date: /(20\d{2}-\d{2}-\d{2})/,
    ip: /IP\s*属地\s*[：:]\s*([^\s|，,]+)/,
    desc: /(超棒|满意|不错|一般|不佳)/,
    score: /([1-5])\s*分/,
    img: /https:\/\/dimg\d+\.c-ctrip\.com\/images\/[^\s"'<>]+?\.jpg[^\s"'<>]*/g,
    idAttr: /(?:data-[\w-]*(?:id|Id|ID)|commentId|reviewId|"id")\s*[=:]\s*["']?(\d{6,14})/,
    idNum: /\b(\d{8,10})\b/,
  };

  function pickId(el) {
    const html = el.outerHTML;
    const m = html.match(RE.idAttr);
    if (m) return m[1];
    const m2 = html.match(RE.idNum);
    return m2 ? m2[1] : '';
  }

  function pickNick(el) {
    const a = el.querySelector('a[href*="/members/"], a[href*="member"]');
    if (a) { const t = clean(a.innerText); if (t) return t; }
    const img = el.querySelector('img[alt]');
    if (img) { const t = clean(img.getAttribute('alt')); if (t && t.length < 30) return t; }
    return '';
  }

  function pickLikes(el) {
    const m = (el.innerText || '').match(/(?:点赞|有用|举报)\D{0,12}?(\d{1,4})/);
    return m ? m[1] : '0';
  }

  function pickImgs(el) {
    const set = new Set();
    el.querySelectorAll('img').forEach((im) => {
      let u = im.getAttribute('src') || im.getAttribute('data-src') || '';
      const big = (im.parentElement && im.parentElement.getAttribute('href')) || '';
      if (big && /c-ctrip\.com\/images\//.test(big)) u = big;
      if (!/c-ctrip\.com\/images\//.test(u)) return;
      u = u.replace(/_D_\d+_\d+/, '_W_640_10000').split('?')[0] + '?proc=autoorient';
      set.add(u);
    });
    return [...set];
  }

  function pickContent(el) {
    const lines = (el.innerText || '').split('\n').map(clean).filter(Boolean);
    const body = lines.filter((s) => {
      if (s.length < 8) return false;
      if (RE.date.test(s) || RE.ip.test(s)) return false;
      if (RE.desc.test(s) && s.length < 12) return false;
      if (/^\d+\s*$/.test(s)) return false;
      if (/^(点赞|举报|回复|有用)$/.test(s)) return false;
      return true;
    });
    if (!body.length) return clean(el.innerText);
    body.sort((a, b) => b.length - a.length);
    return body[0];
  }

  function extract(el, spotName, meta) {
    const t = el.innerText || '';
    const imgs = pickImgs(el);
    const s = t.match(RE.score), d = t.match(RE.desc), dt = t.match(RE.date), ip = t.match(RE.ip);
    return {
      '景点名称': spotName,
      '评论编号': pickId(el),
      '评分': s ? s[1] : '',
      '评分描述': d ? d[1] : '',
      '评论内容': pickContent(el),
      '发布时间': dt ? dt[1] : '',
      'IP归属地': ip ? ip[1] : '',
      '用户昵称': pickNick(el),
      '点赞数': pickLikes(el),
      '图片数': imgs.length,
      '图片URL': imgs.join('，'),
      '地址': meta['地址'] || '',
      '开放时间': meta['开放时间'] || '',
      '官方电话': meta['官方电话'] || '',
      '景点介绍': meta['景点介绍'] || '',
    };
  }

  // ======================= 3. 景点级字段（地址/开放时间/电话/介绍） =======================
  function textAfterLabel(label, maxLen) {
    const nodes = [...document.querySelectorAll('div,span,dt,dd,p,li,h3,h4,b,strong')]
      .filter((e) => e.children.length === 0 && clean(e.textContent) === label);
    for (const n of nodes) {
      let p = n.parentElement;
      for (let i = 0; i < 3 && p; i++) {
        let t = clean((p.innerText || '').replace(label, ''));
        if (t.length > 1) {
          t = t.split('\n').map(clean).filter(Boolean)[0] || t;
          if (t.length <= maxLen) return t;
        }
        p = p.parentElement;
      }
    }
    return '';
  }

  function spotMeta() {
    return {
      '地址': textAfterLabel('地址', 80),
      '开放时间': textAfterLabel('开放时间', 100),
      '官方电话': textAfterLabel('官方电话', 80),
      '景点介绍': textAfterLabel('介绍', 2000),
    };
  }

  function spotName() {
    const h = document.querySelector('h1');
    if (h && clean(h.innerText)) return clean(h.innerText).slice(0, 40);
    return clean(document.title.split('_')[0].split('旅游')[0]).slice(0, 40) || '未知景点';
  }

  // ======================= 4. 翻页 =======================
  function findNext() {
    const cands = [...document.querySelectorAll('a,button,li,span,div')]
      .filter((e) => clean(e.textContent) === '下一页' && e.querySelectorAll('*').length <= 1);
    return cands.length ? cands[cands.length - 1] : null;
  }

  function isDisabled(el) {
    if (!el) return true;
    let n = el;
    for (let i = 0; i < 3 && n; i++) {
      const cls = (typeof n.className === 'string' ? n.className : '') || '';
      if (/disabled|forbid|ban/i.test(cls)) return true;
      if (n.getAttribute && (n.getAttribute('aria-disabled') === 'true' || n.getAttribute('disabled') !== null)) return true;
      n = n.parentElement;
    }
    return false;
  }

  function signature() {
    const cards = getCards();
    if (!cards.length) return '';
    return `${cards.length}|${(cards[0].innerText || '').slice(0, 120)}`;
  }

  async function waitChanged(oldSig) {
    const t0 = Date.now();
    while (Date.now() - t0 < CFG.waitChangeMs) {
      await sleep(600);
      if (signature() && signature() !== oldSig) return true;
    }
    return false;
  }

  // ======================= 5. CSV 导出 =======================
  function allRows() {
    const out = [];
    Object.keys(DB).forEach((spot) => Object.values(DB[spot]).forEach((r) => out.push(r)));
    return out;
  }

  function toCSV(rows) {
    const esc = (v) => {
      const s = String(v == null ? '' : v);
      return /[",\n\r]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
    };
    const head = COLS.join(',');
    const body = rows.map((r) => COLS.map((c) => esc(r[c])).join(',')).join('\r\n');
    return '\ufeff' + head + '\r\n' + body;
  }

  function downloadCSV(rows) {
    const name = CFG.downloadName || `携程评论_${spotName()}_${new Date().toISOString().slice(0, 10)}.csv`;
    const blob = new Blob([toCSV(rows)], { type: 'text/csv;charset=utf-8' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = name;
    document.body.appendChild(a); a.click(); a.remove();
    log(`已下载：${name}（${rows.length} 条）`);
  }

  window.ctripExport = () => downloadCSV(allRows());
  window.ctripStat = () => {
    const rows = allRows();
    console.table({
      总数: rows.length,
      缺评论编号: rows.filter((r) => !r['评论编号']).length,
      缺发布时间: rows.filter((r) => !r['发布时间']).length,
      缺评分: rows.filter((r) => !r['评分']).length,
      图片数不一致: rows.filter((r) => String(r['图片数']) !== String(r['图片URL'] ? r['图片URL'].split('，').length : 0)).length,
    });
    return 'OK';
  };

  // ======================= 6. 主流程 =======================
  const spot = spotName();
  const meta = spotMeta();
  log(`景点：${spot}`);
  log('景点级字段：', meta);

  let pages = 0, added = 0;
  for (let p = 1; p <= CFG.maxPages; p++) {
    if (window.__ctripStop) { warn('收到停止指令，提前结束'); break; }
    const cards = getCards();
    if (!cards.length) { warn(`第 ${p} 页没找到评论卡片，结束`); break; }

    if (!DB[spot]) DB[spot] = {};
    let pageNew = 0;
    cards.forEach((el) => {
      const row = extract(el, spot, meta);
      const key = row['评论编号'] || `__NOID__${spot}_${p}_${row['评论内容'].slice(0, 30)}_${row['发布时间']}`;
      if (!DB[spot][key]) { DB[spot][key] = row; pageNew++; }
    });
    added += pageNew; pages = p;
    store.write(DB);
    log(`第 ${p} 页：本页 ${cards.length} 条，新增 ${pageNew} 条，累计 ${Object.keys(DB[spot]).length} 条`);

    if (p >= CFG.maxPages) { warn('已达到最大页数'); break; }
    const next = findNext();
    if (!next) { log('找不到“下一页”，结束'); break; }
    if (isDisabled(next)) { log('“下一页”已置灰，已到最后一页，结束'); break; }

    const oldSig = signature();
    next.click();
    const ok = await waitChanged(oldSig);
    if (!ok) { warn('点击下一页后内容没有变化，可能到最后一页或被风控，结束'); break; }
    await sleep(CFG.delayMs);
  }

  log(`采集结束：共 ${pages} 页，${spot} 累计 ${Object.keys(DB[spot] || {}).length} 条`);
  downloadCSV(allRows());
  log('提示：可以继续运行本脚本采集下一个景点；输入 window.ctripStat() 可看质量自检。');
  log('如需清空重来：localStorage.removeItem("' + CFG.storeKey + '")');
})();
