/* 首页：Hero 关键数据 + 核心数据 + 西藏景点视觉卡 + 评论量排行 + 分析覆盖与数据依据。
   只调用 /api/overview/summary、/api/spots/ranking、/api/spots —— 全部只读数据库。

   【文案纪律】本页是普通用户第一眼看到的内容，因此：
     · 不出现内部方法名、表名、内部状态码、API 成本、调用次数等实现细节；
     · "尚未生成"必须如实说明，绝不用估算值或示例内容填充；
     · 分析覆盖区回答的是"结论建立在多少条真实评论上"，不是"后台跑了多少任务"；
     · 插画加载失败不影响数据展示（<img> 自然退化为卡片底色）。 */

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
 *    · 语义结构化 = 库内 `comment_semantic` 覆盖的评论条数（与 `sentiment` 的
 *      "语义判定"口径同一档，当前 59,030 条）：每条评论都有结构化语义结果，
 *      低信息量短评按既定规则产出，其余由模型逐条分析；
 *    · 覆盖景点 / 可生成完整评价 = 景点规模与门槛。
 *
 *  【2026-10 收尾优化】第三个数字此前取的是"统计基线"口径（47,110 条）并写作
 *  "有效评价"，容易被误读成"系统只分析了 47,110 条"。改为直接展示**逐条语义
 *  结构化**的真实条数，与首页"分析覆盖与数据依据"里的数字保持同一口径。
 */
function renderHeroKpis(t, semanticCount) {
  const box = document.getElementById('hero-kpis');
  if (!box) return;
  box.innerHTML = [
    { label: '游客评论', value: fmtInt(t.review_count), sub: '条真实评论' },
    { label: '覆盖景点', value: fmtInt(t.spot_count), sub: '个西藏及进藏沿线景点' },
    { label: '评论语义分析', value: fmtInt(semanticCount), sub: '条评论已完成逐条语义结构化' },
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

    // 语义结构化条数 = 逐条语义判定口径覆盖的评论数（comment_semantic 的行数）。
    // 首页 Hero 与下方"分析覆盖与数据依据"共用这一个数，避免两处口径不一致。
    const semantic = (cov.sentiment || []).find(s => s.method === 'deepseek');
    const semanticCount = semantic ? Number(semantic.n || 0) : 0;

    renderHeroKpis(t, semanticCount);
    renderHomeKpis(t, cov);

    const note = document.getElementById('home-summary-note');
    if (note) note.textContent = data.caliber_note || '';
    const cal = document.getElementById('home-caliber');
    if (cal) cal.textContent = data.caliber_note || '暂无数据说明。';

    // 分析覆盖与数据依据。
    //
    // 【2026-10 收尾优化】这一块原来叫"分析结果生成情况"，列的是"已生成／暂未生成"，
    // 对游客来说那是**后台生产进度**，不是他关心的问题。改为用同样真实的数字回答
    // "这些结论建立在什么之上"：
    //   ① 收录了多少条真实评论、覆盖多少个景点；
    //   ② 其中多少条**逐条**完成了语义结构化（结论的唯一依据）；
    //   ③ **只有评论量 ≥100 的 57 个景点**才有完整智能评价——绝不写成
    //      "837 个景点全部完成智能分析"（那是与库内事实不符的表述）；
    //   ④ 归纳出多少主题，用于观察游客在谈什么。
    const rep = cov.report || {};
    const reportsDone = Number(rep.reports_generated || 0);
    const reportsNeed = Number(rep.spots_ge100 || 0);
    const allReady = reportsNeed > 0 && reportsDone >= reportsNeed;

    document.getElementById('home-coverage').innerHTML = renderTable(
      ['分析内容', '覆盖规模', '说明'],
      [
        ['收录的游客评论', `${fmtInt(t.review_count)} 条`,
         `来自 ${fmtInt(t.spot_count)} 个西藏及进藏沿线景点（西藏 ${fmtInt(t.spot_tibet)} · 进藏沿线 ${fmtInt(t.spot_route)}）`],
        ['逐条完成语义结构化的评论', `${fmtInt(semanticCount)} 条`,
         '逐条提取情感倾向、游客关注的方面与关键词，是所有景点结论的唯一依据'],
        ['已生成完整智能评价的景点', `${fmtInt(reportsDone)} 个`,
         `只对评论量达到 100 条的 ${fmtInt(reportsNeed)} 个景点生成评价；其余景点只提供基础统计，不强行下结论`],
        ['归纳出的评论主题', `${fmtInt((cov.topic || {}).topic_count)} 个`,
         `由 ${fmtInt((cov.topic || {}).topic_word_count)} 个主题词构成，用于观察游客在谈什么`],
      ]
    );

    if (!allReady) {
      document.getElementById('home-coverage').insertAdjacentHTML('beforeend',
        '<div class="alert">部分景点的完整智能评价仍在生成中。'
        + '在此期间，所有景点都可以查看评论统计、情感分布、关注方面与代表性评论。</div>');
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
