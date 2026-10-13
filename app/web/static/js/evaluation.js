/* 「智能分析 → 景点评价」子功能：读取离线生成的景点评价与事实依据。
   只调用 /api/spots/{id}/report 与 /api/spots/ranking —— 两者都只读数据库。

   【文案纪律】面向普通用户：
     · 不显示模型名、Prompt 版本、token、内部原因码（如 REPORT_NOT_GENERATED）；
     · "尚未生成"要说清**为什么**以及**现在能看什么**；
     · 内部细节（依据来源、评价版本、生成时刻）收进折叠区，答辩时可展开。 */

/** 评价不可用的原因 → 用户可读的说法（内部码只进折叠区）。 */
const REPORT_REASON_TEXT = {
  REPORT_NOT_GENERATED: {
    title: '该景点的智能评价暂未生成',
    hint: '完整智能评价需要较长的离线分析时间，目前尚未生成。你仍然可以在下方查看该景点的评论统计与情感分布。',
  },
  REVIEW_COUNT_BELOW_THRESHOLD: {
    title: '该景点的评论量不足以生成综合评价',
    hint: '为避免用少量评论得出不稳健的结论，评论量不足 100 条的景点不生成综合评价，只提供基础统计。',
  },
  NO_FACT_PACKAGE: {
    title: '该景点暂缺评价所需的基础数据',
    hint: '生成评价前需要先汇总该景点的评论统计，目前这一步尚未完成。',
  },
};

/** 载入景点下拉列表（按评论量降序取前 100）。
 *
 * 【为什么是 100】接口对 `limit` 的上限就是 **100**（传更大值返回参数错误 1002）——
 * 这是实测踩到的：原先写 200，页面直接显示"暂时无法加载景点列表"。
 * 取前 100 足够：库内共 837 个景点，而"评论量 ≥100 条"的 57 个景点必然都在评论量前 100 名之内，
 * 因此合格景点一个都不会漏；不足 100 条的景点也给出若干可选样本。
 */
async function loadSpotOptions() {
  const sel = document.getElementById('eval-spot');
  if (!sel) return;
  try {
    const data = await apiGet('/api/spots/ranking', { by: 'reviews', limit: 100 });
    const items = data.items || [];
    const eligible = items.filter(item => item.has_full_evaluation);
    const others = items.filter(item => !item.has_full_evaluation);

    const opt = it => `<option value="${it.spot_id}">${escapeHtml(it.spot_name)}（${fmtInt(it.review_count)} 条评论）</option>`;
    sel.innerHTML = '<option value="">— 请选择景点 —</option>'
      + (eligible.length ? `<optgroup label="可以生成综合评价（评论量 ≥100）">${eligible.map(opt).join('')}</optgroup>` : '')
      + (others.length ? `<optgroup label="仅提供基础统计（评论量 <100）">${others.map(opt).join('')}</optgroup>` : '');

    // 从「景点分析」页或首页带过来的景点：若不在前 100 名，补一个选项，避免"选了却没反应"。
    // 【修】`/api/spots/{id}` 的返回体是 `{spot: {...}, statistics: {...}}`，景点字段在 `spot` 里；
    // 此前直接取 `one.spot_id / one.spot_name`，会生成 `<option value="undefined">`（且没有名称），
    // 导致「评论量较低的景点」用 URL 带过来时打不开（实测 spot 796 触发）。
    const active = window.UI.getActiveSpot();
    if (active && !items.some(it => String(it.spot_id) === String(active))) {
      try {
        const one = await apiGet(`/api/spots/${active}`);
        const sp = one.spot || one;
        if (sp && sp.spot_id) {
          sel.insertAdjacentHTML('beforeend',
            `<option value="${sp.spot_id}">${escapeHtml(sp.spot_name || '')}（${fmtInt(sp.review_count)} 条评论）</option>`);
        }
      } catch (e) { /* 该景点不存在则忽略 */ }
    }
  } catch (e) {
    const box = document.getElementById('eval-body');
    if (box) box.innerHTML = window.UI.notice(`暂时无法加载景点列表：${window.UI.userMessage(e)}`, 'warn');
  }
}

/** 渲染"评价不可用"时的友好状态。
 *
 * 【文案纪律】页面上**不出现内部原因码**（如 `REPORT_NOT_GENERATED`）：
 * 那是给开发者看的。技术细节仍然完整保留在浏览器的开发者工具里
 * （`console.info` + `data-reason` 属性），既不影响排查，也不污染界面。
 */
function renderUnavailable(data) {
  const known = REPORT_REASON_TEXT[data.reason] || {};
  const title = known.title || '该景点的智能评价暂不可用';
  const hint = known.hint || (data.message_text || '系统暂时无法提供该景点的智能评价。');
  const reviews = data.review_count;
  const below = Number(reviews || 0) < 100;

  // 技术细节：只在控制台留痕（开发/答辩排查用），不进页面
  try {
    console.info('[智能评价] 不可用详情', {
      comment_id_spot: data.spot_id, reason: data.reason,
      review_count: reviews, message: data.message_text,
    });
  } catch (e) { /* 忽略 */ }

  // 评论不足门槛时，顺便告诉用户"还差多少"，比单纯说"不足"更有用
  const gap = below && reviews !== null && reviews !== undefined
    ? `<p class="hint">当前收录该景点评论 <strong>${fmtInt(reviews)}</strong> 条，
         距生成综合评价所需的 100 条还差 <strong>${fmtInt(Math.max(0, 100 - Number(reviews)))}</strong> 条。</p>`
    : (reviews !== null && reviews !== undefined
        ? `<p class="hint">当前收录该景点评论 <strong>${fmtInt(reviews)}</strong> 条。</p>` : '');

  // 没有综合评价时，真实评论反而更有价值：同样把「游客真实声音」挂在这里
  return `
    ${window.UI.pendingNotice(title, hint)}
    ${gap}
    <p class="hint">
      你仍然可以查看该景点的
      <a class="link" href="/spots?spot=${encodeURIComponent(data.spot_id || '')}">基础统计与代表评论</a>，
      或在上方换一个评论量更多的景点。
    </p>
    ${caliberNote(data.caliber_note, data.review_count)}
    <div id="eval-voices"></div>`;
}

async function loadReport() {
  const sel = document.getElementById('eval-spot');
  const box = document.getElementById('eval-body');
  if (!sel || !box) return;
  const spotId = sel.value;
  if (!spotId) {
    box.innerHTML = window.UI.empty('请先在上方选择一个景点。', 'warn');
    return;
  }
  box.innerHTML = '<p class="hint">加载中…</p>';
  try {
    const data = await apiGet(`/api/spots/${spotId}/report`);

    // 完整返回体留在控制台（含 model / prompt_version / token 等内部字段）：
    // 页面只展示用户需要的信息，排查与答辩时打开控制台即可看到全部口径。
    try {
      console.info('[智能评价] 接口返回', data);
      window.__lastReport = data;
    } catch (e) { /* 控制台不可用时忽略 */ }

    // 数据不足 / 尚未生成 → 属正常业务响应（available=false），按提示语展示而非报错
    if (!data.available) {
      box.innerHTML = renderUnavailable(data);
      loadRealVoices(spotId);     // 没有综合评价时，真实评论反而更有价值
      return;
    }
    renderAvailable(data, box);
    loadRealVoices(spotId);       // 「游客真实声音」用的是 review 表的原文，与评价是否生成无关
  } catch (e) {
    box.innerHTML = window.UI.notice(
      `加载该景点的智能评价时出错：${window.UI.userMessage(e)}`, 'bad',
      e && e.code !== undefined ? `业务码 ${e.code}：${e.message}` : String(e));
  }
}

/** 渲染"评价可用"的完整版式。 */
function renderAvailable(data, box) {
  const r = data.report || {};
  const basis = data.basis || {};
  const list = (title, items, cls) => {
    // 每一项都过一遍"用户化"改写；返回 null 的表示与下方数字卡重复，不再罗列。
    const rows = (items || []).map(x => humanizeReportItem(x)).filter(Boolean);
    return `
    <div class="rep-col ${cls}">
      <h4>${title}</h4>
      <ul>${rows.map(x => `<li>${x}</li>`).join('')
        || '<li class="muted">这一项没有可展示的内容</li>'}</ul>
    </div>`;
  };
  // 游客关注点：原来是一整列"方面，样本量N"，与左侧两列重复；这里并成一行话题清单。
  const focusLine = (r.visitor_focus || []).map(x => humanizeReportItem(x)).filter(Boolean).join('、');

  box.innerHTML = `
    ${data.need_review ? window.UI.notice(data.need_review_note
      || '部分统计数据存在一致性提示，相关内容已由系统核对；'
        + '阅读时建议结合各项的样本量综合判断。', 'warn') : ''}

    <div class="rep-summary">
      <h3>综合评价 <span class="tag">由分析结果生成</span></h3>
      <p>${humanizeSummary(r.summary || '')}</p>
    </div>

    <p class="hint">下面三项都来自评论统计：括号里的<b>百分比</b>＝提到该方面的评论中给出好评／差评的比例，
      <b>条数</b>＝提到该方面的评论数。整体好评率与平均评分见下方「数据依据」。</p>
    <div class="rep-grid">
      ${list('游客满意的地方', r.advantages, 'pos')}
      ${list('游客提到的问题', r.issues, 'neg')}
    </div>
    ${focusLine ? `<p class="hint"><b>游客讨论最多的话题</b>：${focusLine}</p>` : ''}

    <div id="eval-voices"></div>

    <h3>数据依据</h3>
    <p class="hint">上述评价只允许引用以下数字，不允许引入评论之外的信息。${(basis.source === 'fact_package' && basis.version)
      ? `依据来源：离线事实数据快照 ${escapeHtml(String(basis.version))}${basis.generated_at
        ? `（生成于 ${escapeHtml(String(basis.generated_at).slice(0, 16))}）` : ''}，可逐项回溯。`
      : ''}</p>
    ${window.UI.statCards([
      { label: '评论量', value: fmtInt(basis.review_count), sub: '条' },
      { label: '平均评分', value: fmtNum(basis.avg_score, 2), sub: '满分 5 分' },
      { label: '好评率', value: fmtPct(basis.positive_rate), sub: '4 分及以上占比' },
      { label: '差评率', value: fmtPct(basis.negative_rate), sub: '2 分及以下占比' },
      {
        label: '情感判定样本',
        value: basis.sentiment ? fmtInt(basis.sentiment.sample_size) : '—',
        sub: basis.sentiment
          ? `正面 ${fmtPct(basis.sentiment.positive)} / 中性 ${fmtPct(basis.sentiment.neutral)} / 负面 ${fmtPct(basis.sentiment.negative)}`
          : '暂无'
      },
    ])}

    ${(basis.top_aspects && basis.top_aspects.length) ? `
      <h4>游客提到的方面</h4>
      <p class="hint">提到该方面的评论不足 10 条时，只展示条数、不给出比例。</p>
      ${renderTable(['游客提到的方面', '提到该方面的评论数', '其中好评比例', '其中差评比例', '说明'],
        basis.top_aspects.map(a => [
          escapeHtml(a.name), fmtInt(a.sample_size),
          a.positive_rate === null ? '<span class="muted">样本不足</span>' : fmtPct(a.positive_rate),
          a.negative_rate === null ? '<span class="muted">样本不足</span>' : fmtPct(a.negative_rate),
          a.note ? escapeHtml(a.note) : '—',
        ]))}` : ''}

    ${caliberNote(data.caliber_note, data.sample_size)}`;
}

/* =============================================================================
   展示层改写：把离线报告里机器生成的条目换成游客能读的话
   -----------------------------------------------------------------------------
   【为什么是"词级"改写】57 份报告共 881 个条目，写法有 469 种（"样本量886"／"样本122条"／
   "风景维度正面率0.89"／"整体好评率0.9143，差评率0.0357"／纯事实描述……）。
   整串模板匹配只能覆盖三分之一，因此这里统一做**词级替换**：
     · 术语 → 人话："正面率0.97" → "好评比例 97%"、"样本量886" → "886 条评论"、"维度" → "方面"；
     · **只换写法，不改数值**：0.97 × 100 = 97%，同一个数；"正面率"是方面级口径，
       绝不写成整体"好评率"（两者含义不同）；
     · 认不出的句子（例如"古城为唐代吐蕃王朝所建"）**原样保留**，不改写、不补写；
     · 与下方「数据依据」数字卡完全重复的整体指标（整体好评率、平均评分、情感分布）
       不再重复罗列。
   ============================================================================= */

/* 【踩坑记录】比率正则必须**百分数形式在前**：
   写成 `([01](?:\.\d+)?|\d+(?:\.\d+)?%)` 时，`1` 会先匹配到 "14%" 的十位，
   把"负面率14%"改写成"差评比例 100%4%"（数值被改坏，实测在全部 57 份报告里抓到 3 处）。
   因此：百分数优先，整数形式用 `(?![\d%])` 保证不会截断后面的数字或百分号。

   【两套口径分开改写，绝不混用】
     · 星级口径：正面率 / 负面率 / 好评率 / 差评率 → "好评比例 / 差评比例"；
     · 情感口径：正面情感占比 / 正面评价占比 / 中性占比 … → "正面评价占比 / 负面评价占比"。
   两者在项目里是**不同的统计方法**（BR-04/BR-10），不能都写成"好评率"。 */
const RATE_SRC = '(\\d+(?:\\.\\d+)?%|[01](?:\\.\\d+)?(?![\\d%]))';
// 修饰词要覆盖"均 / 都 / 分别 / 各 / 仅 / 约 / 为"——线上真实报告里"负面率均为0.53"很常见
const MOD = '(仅|约|为|均|都|分别|各)?';
const RE_STAR_POS = new RegExp(`(?:正面率|好评率)\\s*${MOD}\\s*${RATE_SRC}`, 'g');
const RE_STAR_NEG = new RegExp(`(?:负面率|差评率)\\s*${MOD}\\s*${RATE_SRC}`, 'g');
const RE_SENT_POS = new RegExp(`(?:(?:情感)?正面(?:情感)?(?:占比|占)?|情感分析正面占比)\\s*${MOD}\\s*${RATE_SRC}`, 'g');
const RE_SENT_NEG = new RegExp(`(?:(?:情感)?负面(?:情感)?(?:占比|占)?|情感分析负面占比)\\s*${MOD}\\s*${RATE_SRC}`, 'g');
// 裸写法："正面0.6、中性0.27、负面0.13"（情感分析段落里常见）
const RE_SENT_BARE = new RegExp(`(正面|中性|负面)\\s*${RATE_SRC}`, 'g');
const RE_BOTH = new RegExp(`正面(?:率|评价)?与负面(?:率|评价)?(?:均)?为\\s*${RATE_SRC}`, 'g');
const RE_IMAGE = new RegExp(`图片率\\s*${MOD}\\s*${RATE_SRC}`, 'g');
// 一句里已经出现"比例/占比"时，句内剩下的**小数**（如"分别为0.6、0.58"）只可能是比率
// → 一并写成百分比。只认小数形式（不认裸整数 0/1，避免把"共1条评论"改成"100%条评论"），
// 且后面跟 %/倍/年/岁/万/千 等量词的一律跳过（那是量纲，不是比率）。
const RE_BARE_RATE = /(?<![\d.])([01]\.\d+)(?![\d.%倍年岁万千米个人次天])/g;
const HAS_RATE_TERM = /(?:好评|差评|正面评价|负面评价|带图评论)比例|(?:正面|中性|负面)评价占比/;

/** 把 0.87 / 87% 两种写法统一显示成 "87%"（只换写法，数值不变）。 */
function fmtRateText(raw) {
  const s = String(raw);
  const n = parseFloat(s);
  if (!isFinite(n)) return s;
  return s.indexOf('%') >= 0 ? `${Math.round(n * 10) / 10}%` : `${Math.round(n * 1000) / 10}%`;
}

/** 共用的词级替换：术语 → 人话。**只换写法，不改任何数值**。 */
function humanizeTerms(text) {
  let out = String(text == null ? '' : text)
    .replace(RE_BOTH, (m, r) => `好评与差评各占 ${fmtRateText(r)}`)
    .replace(RE_STAR_POS, (m, mod, r) => `好评比例${mod === '仅' ? '仅 ' : ' '}${fmtRateText(r)}`)
    .replace(RE_STAR_NEG, (m, mod, r) => `差评比例${mod === '仅' ? '仅 ' : ' '}${fmtRateText(r)}`)
    .replace(RE_SENT_POS, (m, mod, r) => `正面评价占比${mod === '仅' ? '仅 ' : ' '}${fmtRateText(r)}`)
    .replace(RE_SENT_NEG, (m, mod, r) => `负面评价占比${mod === '仅' ? '仅 ' : ' '}${fmtRateText(r)}`)
    .replace(RE_IMAGE, (m, mod, r) => `带图评论比例${mod === '仅' ? '仅 ' : ' '}${fmtRateText(r)}`)
    .replace(RE_SENT_BARE, (m, which, r) => `${which}评价占比 ${fmtRateText(r)}`)
    // 样本量 → N 条评论（"样本量886" / "样本122条" / "样本量分别为16条和13条"）
    .replace(/样本量?\s*(?:分别)?为\s*(\d+)\s*条?\s*和\s*(\d+)\s*条?/g,
      (m, a, b) => `分别为 ${fmtInt(a)} 条和 ${fmtInt(b)} 条评论`)
    .replace(/样本量?\s*(?:均|都)?为\s*(\d+)\s*条?/g, (m, n) => `为 ${fmtInt(n)} 条评论`)
    .replace(/样本量?\s*(\d+)\s*条/g, (m, n) => `${fmtInt(n)} 条评论`)
    .replace(/样本量?\s*(\d+)(?!\s*条)/g, (m, n) => `${fmtInt(n)} 条评论`)
    .replace(/样本量/g, '讨论量')     // 剩下的都是"样本量最大"这类没有数字的写法
    // 论文式说法 → 日常说法
    .replace(/维度/g, '话题')
    // 没有紧跟数字的术语（"…等负面率均超过0.5" / "负面率相对较高"）也要换掉
    .replace(/正面率/g, '好评比例')
    .replace(/好评率/g, '好评比例')
    .replace(/负面率/g, '差评比例')
    .replace(/差评率/g, '差评比例')
    .replace(/正面情感(?:占比|占)?/g, '正面评价占比')
    .replace(/负面情感(?:占比|占)?/g, '负面评价占比')
    .replace(/[，,]?\s*为各(?:话题|方面|分项|评价方面)(?:中)?(?:最高|最大|最多)/g, '（在各话题里最高）')
    .replace(/[，,]?\s*为各(?:话题|方面|分项|评价方面)(?:中)?(?:最低|最少)/g, '（在各话题里最低）')
    .replace(/样本不足\s*(\d+)\s*条评论/g, (m, n) => `只有 ${fmtInt(n)} 条评论、样本太少`)
    .replace(/不纳入评价/g, '不单独下结论');

  // 兜底：句子里已出现"比例/占比"时，剩下的裸小数（含"分别/均"后面的列表）就是比率
  if (HAS_RATE_TERM.test(out)) {
    out = out.replace(RE_BARE_RATE, (m, num) => fmtRateText(num));
  }
  return out;
}

/** 综合评价（模型写的一段话）：只做同样的术语替换，不改写句子、不改数值。 */
function humanizeSummary(text) {
  return escapeHtml(humanizeTerms(text));
}

function humanizeReportItem(text) {
  const t = String(text == null ? '' : text).trim();
  if (!t) return null;

  // ① 与下方「数据依据」数字卡重复的整体指标 → 不再重复罗列
  //    （"图片率/点赞"这类卡片里没有的信息要保留，所以加了例外）
  if (/^(整体|综合|总体|平均评分|平均分|综合评分)/.test(t) && !/图片率|点赞/.test(t)) return null;

  let out = humanizeTerms(t);

  // ② 条目以"方面"开头时补一个冒号，读起来是"门票：差评比例 58%，452 条评论"
  out = out.replace(/^([^，,：:]{2,10}?)(?=(?:好评|差评)比例)/, '$1：');
  // ③ 关注点写法："性价比，样本量2382，好评比例 87%" → "性价比（2,382 条评论，好评比例 87%）"
  out = out.replace(/^([^，,：:]{2,10})，\s*([\d,]+ 条评论)(.*)$/, '$1（$2$3）');

  return escapeHtml(out);
}

/* =============================================================================
   游客真实声音（原文照录）
   -----------------------------------------------------------------------------
   【与 AI 内容的边界】这一块**只做搬运**：原文、评分、时间、话题全部取自数据库
   （`review` 表 + 该评论在 `aspect` 表里的真实标注），不经过任何模型，
   不做改写、不做补全、不做润色。页面上用"原文照录"标签与上方"由分析结果生成"区分。
   数据不足时**少展示**：重复正文、无信息量短评、空评论一律剔除，绝不凑数。
   ============================================================================= */
const VOICE_GROUPS = [
  { key: 'positive', label: '好评', range: '4 星及以上' },
  { key: 'neutral', label: '中评', range: '3 星' },
  { key: 'negative', label: '差评', range: '2 星及以下' },
];
const VOICE_FETCH_LIMIT = 6;      // 接口每次取回的条数（上限 20）
const VOICE_MAX_PER_GROUP = 3;    // 页面每组最多展示 3 条，避免一次性堆大量文本
const VOICE_CLAMP = 150;          // 单条超过这个长度就折叠，避免刷屏

/** 一条评论是否值得展示：排除内容重复与无信息量短评（宁少不凑）。 */
function voiceUsable(row) {
  if (!row || !row.content) return false;
  if (row.is_dup_content) return false;
  const len = Number(row.content_length || String(row.content).length);
  if (len <= 10) return false;
  return true;
}

function renderVoiceCard(row, cls) {
  const text = String(row.content || '');
  const body = text.length > VOICE_CLAMP
    ? `<p class="review-text">${escapeHtml(text.slice(0, VOICE_CLAMP))}…</p>
       <details class="fold"><summary>查看完整评论（${text.length} 字）</summary>
         <div class="fold-body"><p class="review-text">${escapeHtml(text)}</p></div>
       </details>`
    : `<p class="review-text">${escapeHtml(text)}</p>`;
  const topics = (row.topics || []).filter(Boolean);
  const meta = [
    row.score ? `${row.score} 星` : '未评分',
    row.publish_date ? escapeHtml(String(row.publish_date).slice(0, 10)) : '',
    row.like_count ? `${fmtInt(row.like_count)} 人点赞` : '',
    topics.length ? `提到：${topics.map(t => escapeHtml(t)).join('、')}` : '',
  ].filter(Boolean).join(' · ');
  // 复用站点既有的 .review 版式（与「景点分析」页的代表评论视觉一致）
  return `
    <div class="review ${cls}">
      <div class="review-meta">${meta}</div>
      ${body}
    </div>`;
}

async function loadRealVoices(spotId) {
  const box = document.getElementById('eval-voices');
  if (!box) return;
  box.innerHTML = '<p class="hint">正在取这个景点的真实评论…</p>';
  try {
    const data = await apiGet(`/api/spots/${spotId}/reviews`, { limit: VOICE_FETCH_LIMIT });
    const groups = VOICE_GROUPS
      .map(g => ({ ...g, rows: (data[g.key] || []).filter(voiceUsable).slice(0, VOICE_MAX_PER_GROUP) }))
      .filter(g => g.rows.length);
    if (!groups.length) {
      box.innerHTML = `
        <h3>游客真实声音 <span class="tag ok">原文照录</span></h3>
        <p class="hint">这个景点目前没有可展示的完整评论原文
          （过短、内容重复或缺失的评论不会被拿来充数）。</p>`;
      return;
    }
    box.innerHTML = `
      <h3>游客真实声音 <span class="tag ok">原文照录，未经改写</span></h3>
      <p class="hint">下面是从该景点评论里挑出的原文，按好评／中评／差评分组，每组最多
        ${VOICE_MAX_PER_GROUP} 条。它们只用于让你看到"游客原话长什么样"，
        <b>不代表整体评价分布</b>（分布请看下方数据依据）。</p>
      ${groups.map(g => `
        <div class="voice-group">
          <h4>${g.label}<span class="muted">（${g.range}）</span></h4>
          ${g.rows.map(r => renderVoiceCard(r, g.key === 'positive' ? 'pos' : (g.key === 'negative' ? 'neg' : 'neu'))).join('')}
        </div>`).join('')}
      ${caliberNote(data.caliber_note, null)}`;
  } catch (e) {
    box.innerHTML = window.UI.notice(
      `暂时取不到评论原文：${window.UI.userMessage(e)}`, 'warn');
  }
}

/** 只有管理员才显示"申请重新生成"按钮（接口需管理员）。
    该按钮只**登记请求**，不产生任何模型调用——真正生成由离线批处理执行。 */
async function setupRegenButton() {
  const btn = document.getElementById('btn-regen');
  if (!btn) return;
  try {
    const me = await apiGet('/api/auth/me');
    btn.style.display = (me && me.is_admin) ? '' : 'none';
  } catch (e) {
    btn.style.display = 'none';  // 未登录/非管理员：不显示
  }
}

async function doRegenerate() {
  const sel = document.getElementById('eval-spot');
  const box = document.getElementById('eval-body');
  if (!sel || !sel.value) {
    if (box) box.innerHTML = window.UI.empty('请先在上方选择一个景点。', 'warn');
    return;
  }
  const btn = document.getElementById('btn-regen');
  btn.disabled = true;
  try {
    const resp = await axios.post(`/api/spots/${sel.value}/report/regenerate`);
    const body = resp.data || {};
    if (body.code !== 0) {
      box.innerHTML = window.UI.notice(`提交失败：${window.UI.userMessage({ code: body.code, message: body.message })}`,
        'bad', `业务码 ${body.code}：${body.message}`);
      return;
    }
    const d = body.data || {};
    box.innerHTML = d.submitted
      ? window.UI.notice('已登记重新生成请求，稍后由离线任务处理。'
          + '该操作只登记请求，不会立即产生分析结果，也不会在浏览页面时触发任何模型调用。', '')
      : window.UI.notice(d.message_text || '该景点不生成智能评价。', 'warn');
  } catch (e) {
    const data = e.response && e.response.data;
    box.innerHTML = window.UI.notice(
      `提交失败：${window.UI.userMessage(data ? { code: data.code, message: data.message } : e)}`,
      'bad', data ? `业务码 ${data.code}：${data.message}` : String(e));
  } finally {
    btn.disabled = false;
  }
}

/* 事件绑定与"首次加载"都延迟到 smart.js 调用对应函数时进行：
   本文件在「智能分析」整合页与旧地址下共用，初始化时机由页面决定。 */
document.addEventListener('DOMContentLoaded', () => {
  const btnLoad = document.getElementById('btn-load-report');
  if (btnLoad) btnLoad.addEventListener('click', loadReport);
  const btnRegen = document.getElementById('btn-regen');
  if (btnRegen) btnRegen.addEventListener('click', doRegenerate);
  const sel = document.getElementById('eval-spot');
  if (sel) {
    sel.addEventListener('change', () => { if (sel.value) loadReport(); });
  }
  setupRegenButton();

  // 若本页**没有**由 smart.js 接管（例如未来单独加载），则自行初始化。
  if (typeof window.__SMART_PAGE__ === 'undefined') {
    loadSpotOptions().then(() => {
      const active = window.UI.getActiveSpot();
      const selEl = document.getElementById('eval-spot');
      if (active && selEl && Array.from(selEl.options).some(o => String(o.value) === String(active))) {
        selEl.value = String(active);
        loadReport();
      }
    });
  }
});
