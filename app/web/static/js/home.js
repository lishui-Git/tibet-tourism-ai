/* 首页：Hero 关键数据 + 核心数据 + 西藏景点视觉卡 + 评论量排行 + 结果生成情况。
   只调用 /api/overview/summary、/api/spots/ranking、/api/spots —— 全部只读数据库。

   【文案纪律】本页是普通用户第一眼看到的内容，因此：
     · 不出现内部方法名、表名、内部状态码、API 成本、调用次数等实现细节；
     · "尚未生成"必须如实说明，绝不用估算值或示例内容填充；
     · 插画加载失败不影响数据展示（<img> 自然退化为卡片底色）。 */

/** 内部方法名 → 用户能看懂的说法（仅展示层翻译，接口契约不变）。 */
const METHOD_LABELS = {
  deepseek: '深度语义判定',
  mllib: '统计基线判定',
  dict: '词典判定',
};

/** 视觉展示位：挑几个有地域代表性的景点，配自绘矢量插画。
 *
 *  这里的"插画 + 景点名"是**视觉映射**（硬编码）；卡片上的数字**全部来自真实接口**。
 *  `query` 是对接口更友好的检索词（例如库内叫「纳木措景区」，只搜「纳木措」也命中）。
 *  解析规则：先在评论量前 100 里找，找不到再按关键字查一次，并**取评论量最高的那个** ——
 *  避免把「XX滑翔伞基地(8 条)」这类附属点当成主景点展示。
 */
const SCENIC_SPOTS = [
  { name: '布达拉宫',       query: '布达拉宫',       img: 'scene-potala.svg',  note: '世界文化遗产 · 拉萨' },
  { name: '纳木措',         query: '纳木措',         img: 'scene-namtso.svg',  note: '高原圣湖 · 藏北' },
  { name: '雅鲁藏布大峡谷', query: '雅鲁藏布大峡谷', img: 'scene-canyon.svg',  note: '世界最深峡谷之一 · 林芝' },
  { name: '巴松措',         query: '巴松措',         img: 'scene-yamdrok.svg', note: '碧玉色湖泊 · 林芝' },
];

const STATIC_IMG = (f) => `/static/img/${f}`;

/** 从候选里挑"最像主景点"的一个：评论量最高者优先。
 *  同时排除明显的附属设施（停车场／游客中心／售票处…），它们评论量低且不适合做展示。 */
const SUB_SPOT_RE = /(停车场|游客中心|售票|入口|大门|厕所|服务站|驿站|营地|基地|码头|索道|观光车|酒店|民宿|餐厅|购物)/;

function pickMainSpot(items, query) {
  const usable = (items || []).filter(it =>
    it && it.spot_name && it.spot_name.indexOf(query) === 0 && !SUB_SPOT_RE.test(it.spot_name));
  const pool = usable.length ? usable : (items || []).filter(it => it && it.spot_name);
  if (!pool.length) return null;
  return pool.slice().sort((a, b) => Number(b.review_count || 0) - Number(a.review_count || 0))[0];
}

/** 把一份统计结果渲染成 Hero 里的四个关键数字。
 *
 *  口径说明（数字全部来自接口，不硬编码）：
 *    · 游客评论 = 全部已入库评论；
 *    · 有效评价 = 完成情感判定的评论数（已排除短评论与内容重复项），
 *      即库内 `sentiment` 表按"统计基线判定"口径覆盖的条数；
 *    · 覆盖景点 / 可生成完整评价 = 景点规模与门槛。
 */
function renderHeroKpis(t, baselineCount) {
  const box = document.getElementById('hero-kpis');
  if (!box) return;
  box.innerHTML = [
    { label: '游客评论', value: fmtInt(t.review_count), sub: '条真实评论' },
    { label: '覆盖景点', value: fmtInt(t.spot_count), sub: '个西藏及进藏沿线景点' },
    { label: '有效评价', value: fmtInt(baselineCount), sub: '条已完成情感判定' },
    { label: '可生成完整评价', value: fmtInt(t.spot_ge100), sub: '评论量达到 100 条的景点' },
  ].map(k => `
    <div class="hero-kpi">
      <span class="k-label">${escapeHtml(k.label)}</span>
      <span class="k-value">${k.value}</span>
      <span class="k-sub">${escapeHtml(k.sub)}</span>
    </div>`).join('');
}

/** 核心数据区（比 Hero 更详细，用于"看清规模"）。 */
function renderHomeKpis(t, cov) {
  const box = document.getElementById('home-kpis');
  if (!box) return;
  const reportsDone = Number((cov.report || {}).reports_generated || 0);
  const reportsNeed = Number((cov.report || {}).spots_ge100 || 0);
  box.innerHTML = [
    { label: '收录评论', value: fmtInt(t.review_count), sub: '条真实游客评论', accent: false },
    { label: '覆盖景点', value: fmtInt(t.spot_count), sub: `西藏 ${fmtInt(t.spot_tibet)} · 进藏沿线 ${fmtInt(t.spot_route)}`, accent: false },
    { label: '带图评论', value: fmtInt(t.image_review_count), sub: '游客上传了图片的评论', accent: false },
    { label: '可生成完整评价', value: fmtInt(t.spot_ge100), sub: reportsDone > 0 ? `已生成 ${fmtInt(reportsDone)} 个` : '评论量达到 100 条的景点', accent: true },
  ].map(k => `
    <div class="kpi${k.accent ? ' accent' : ''}">
      <span class="kpi-value">${k.value}</span>
      <span class="kpi-label">${escapeHtml(k.label)}</span>
      <span class="kpi-sub">${escapeHtml(k.sub)}</span>
    </div>`).join('');
  return { reportsDone, reportsNeed };
}

/** 西藏景点视觉卡：插画 + 真实统计。
 *  优先用排行数据本地匹配（省请求）；匹配不到再按关键字查一次接口。 */
async function loadScenic(rankingItems) {
  const box = document.getElementById('home-scenic');
  if (!box) return;
  const rank = rankingItems || [];
  const cards = [];

  for (const s of SCENIC_SPOTS) {
    // ① 先在评论量前 100 里找（名字完全相同或以其开头）
    let item = pickMainSpot(rank.filter(it => it.spot_name === s.name || it.spot_name.indexOf(s.name) === 0), s.name);
    // ② 还找不到就按关键字查接口（库内名称可能带后缀，例如「纳木措景区」）
    if (!item) {
      try {
        const res = await apiGet('/api/spots', { keyword: s.query, page: 1, page_size: 10 });
        item = pickMainSpot(res.items || [], s.query);
      } catch (e) { /* 单个景点查不到就跳过，不影响其它卡片 */ }
    }
    if (item) cards.push({ ...s, item });
  }

  if (!cards.length) {
    box.innerHTML = window.UI.empty('暂时无法加载景点数据。', 'empty');
    return;
  }

  box.innerHTML = cards.map(c => `
    <a class="scenic" href="/smart?tab=evaluation&spot=${encodeURIComponent(c.item.spot_id)}">
      <img class="scenic-art" src="${STATIC_IMG(c.img)}" alt="${escapeHtml(c.item.spot_name)} 示意插画"
           loading="lazy" decoding="async" data-scenic-art>
      <span class="scenic-tag tag ${c.item.has_full_evaluation ? 'ok' : ''}">
        ${c.item.has_full_evaluation ? '可生成完整评价' : '仅基础统计'}
      </span>
      <div class="scenic-body">
        <p class="scenic-name">${escapeHtml(c.item.spot_name)}</p>
        <p class="scenic-meta">${escapeHtml(c.note)}</p>
        <div class="scenic-stats">
          <span class="scenic-stat"><span class="v">${fmtInt(c.item.review_count)}</span><span class="l">条评论</span></span>
          <span class="scenic-stat"><span class="v">${fmtNum(c.item.avg_score, 2)}</span><span class="l">平均评分</span></span>
          <span class="scenic-stat"><span class="v">${fmtPct(c.item.positive_rate, 0)}</span><span class="l">好评率</span></span>
        </div>
      </div>
    </a>`).join('');

  // 插画加载失败时：把 <img> 移出布局，卡片退回纯色底，**数据展示完全不受影响**。
  // （插画只是视觉装饰，任何情况下都不该影响页面可用性。）
  box.querySelectorAll('img[data-scenic-art]').forEach(img => {
    img.addEventListener('error', () => {
      img.style.display = 'none';
      const parent = img.closest('.scenic');
      if (parent) parent.classList.add('scenic-noart');
    });
  });
}

/** 评论量排行：比表格更易读的榜单列表。 */
function renderHot(items) {
  const box = document.getElementById('home-hot');
  if (!box) return;
  if (!items || !items.length) {
    box.innerHTML = window.UI.empty('当前还没有景点数据。', 'empty');
    return;
  }
  box.innerHTML = items.map((it, i) => `
    <a class="rank-row" href="/smart?tab=evaluation&spot=${encodeURIComponent(it.spot_id)}"
       title="查看「${escapeHtml(it.spot_name)}」的智能分析">
      <span class="rk">${i + 1}</span>
      <span class="rn">${escapeHtml(it.spot_name)}</span>
      <span class="rv">${fmtInt(it.review_count)} 条</span>
      <span class="rs">评分 ${fmtNum(it.avg_score, 2)} · 好评率 ${fmtPct(it.positive_rate, 0)}</span>
    </a>`).join('');
}

async function loadHome() {
  try {
    const data = await apiGet('/api/overview/summary');
    const t = data.totals || {};
    const cov = data.coverage || {};

    // 有效评价 = 统计基线判定口径覆盖的条数（对应"已排除短评论与重复项"的业务口径）
    const baseline = (cov.sentiment || []).find(s => s.method === 'mllib');
    const baselineCount = baseline ? Number(baseline.n || 0) : 0;

    renderHeroKpis(t, baselineCount);
    renderHomeKpis(t, cov);

    const note = document.getElementById('home-summary-note');
    if (note) note.textContent = data.caliber_note || '';
    const cal = document.getElementById('home-caliber');
    if (cal) cal.textContent = data.caliber_note || '暂无口径说明。';

    // 分析结果生成情况：如实展示
    const rep = cov.report || {};
    const reportsDone = Number(rep.reports_generated || 0);
    const reportsNeed = Number(rep.spots_ge100 || 0);
    const allReady = reportsNeed > 0 && reportsDone >= reportsNeed;
    const sentText = (cov.sentiment || []).length
      ? (cov.sentiment || []).map(s => `${METHOD_LABELS[s.method] || '情感判定'} ${fmtInt(s.n)} 条`).join('　·　')
      : '暂无';

    document.getElementById('home-coverage').innerHTML = renderTable(
      ['分析内容', '状态', '说明'],
      [
        ['评论统计（评分、趋势、来源地区）', '<span class="tag ok">已生成</span>',
         `覆盖全部 ${fmtInt(t.review_count)} 条评论`],
        ['评论情感判定', '<span class="tag ok">已生成</span>', sentText],
        ['游客关注主题', '<span class="tag ok">已生成</span>',
         `${fmtInt((cov.topic || {}).topic_count)} 个主题 / ${fmtInt((cov.topic || {}).topic_word_count)} 个主题词`],
        ['景点综合智能评价',
         allReady ? '<span class="tag ok">已生成</span>' : '<span class="tag warn">暂未生成</span>',
         allReady
           ? `已生成 ${fmtInt(reportsDone)} 个景点的评价`
           : `已具备生成条件的景点 ${fmtInt(reportsNeed)} 个，当前已生成 ${fmtInt(reportsDone)} 个；`
             + '在生成完成前，「智能分析」会如实显示"暂未生成"并继续提供基础统计'],
      ]
    );

    if (!allReady) {
      document.getElementById('home-coverage').insertAdjacentHTML('beforeend',
        '<div class="alert">景点综合智能评价需要较长的离线分析时间，目前尚未生成。'
        + '在此之前，你仍然可以查看每个景点的评论统计、情感分布与代表性评论。</div>');
    }

    return t;
  } catch (e) {
    showError('home-kpis', e);
    return null;
  }
}

/** 拉取排行数据（首页多处共用：风景卡与榜单都基于它）。 */
async function loadRanking() {
  try {
    const data = await apiGet('/api/spots/ranking', { by: 'reviews', limit: 100 });
    const items = data.items || [];
    renderHot(items.slice(0, 8));
    await loadScenic(items);
  } catch (e) {
    const hot = document.getElementById('home-hot');
    if (hot) {
      hot.innerHTML = window.UI.notice(`暂时无法加载景点排行：${window.UI.userMessage(e)}`, 'warn');
    }
    const sc = document.getElementById('home-scenic');
    if (sc) sc.innerHTML = window.UI.empty('暂时无法加载景点数据。', 'empty');
  }
}

document.addEventListener('DOMContentLoaded', () => {
  loadHome();
  loadRanking();
});
