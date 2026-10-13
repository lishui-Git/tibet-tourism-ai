/* 「智能分析 → 智能问答」子功能：提问 → 展示回答或"为什么没生成回答 + 数据依据"。
   只调用 /api/qa/ask（只读数据库检索 + 可选模型组织语言）。

   【文案纪律】面向普通用户：
     · 不显示 APP_QA_LIVE / reached_model / token / prompt 版本 / 内部 reason 码；
     · "暂未生成自然语言回答"要讲成用户能理解的话，并说明**数据依据仍然可用**；
     · 技术细节收进折叠区。 */

/** 问题类型 → 用户可读的说法。 */
const TYPE_LABELS = {
  SPOT_EVALUATION: '景点评价咨询',
  SPOT_COMPARISON: '景点差异对比',
  VISITOR_FOCUS: '游客关注点',
  SENTIMENT_EXPLAIN: '情感与方面解释',
  DATA_METRIC: '数据指标查询',
  RANKING: '排行相关',
  OUT_OF_SCOPE: '超出可回答范围',
};

/** 未生成回答的原因 → 用户可读的说法 + 建议。
 *
 * 【措辞纪律】这些文案必须同时满足三点：
 *   ① 不谎称"已支持"——成段回答确实没有生成；
 *   ② 不写成"功能没做完"——**数据检索与证据展示是完整的、并且就在下方**；
 *   ③ 说清它是一个**开关**（可选的在线生成能力，演示环境默认关闭，以保证稳定与零额外调用）。
 */
const REASON_TEXT = {
  LIVE_DISABLED: {
    text: '这次只给出了统计数据，还没有生成成段的文字回答。'
      + '文字回答需要联网生成，当前演示环境默认关闭，不影响下面的数据。',
    advice: '下面是按你的问题检索到的资料，可以直接阅读和引用。',
  },
  API_KEY_MISSING: {
    text: '这次只给出了统计数据，还没有生成成段的文字回答。',
    advice: '下面的资料同样可以回答你的问题。',
  },
  SPOT_NOT_RECOGNIZED: {
    text: '没有识别出你问的是哪个景点。',
    advice: '请把景点名称写得更完整一些，例如「布达拉宫」「纳木措景区」。',
  },
  NEED_TWO_SPOTS: {
    text: '对比类问题需要同时提到两个景点。',
    advice: '例如可以问「布达拉宫和纳木措景区哪个好」。',
  },
  NO_DATA: {
    text: '目前没有收录与这个问题相关的评论分析结果。',
    advice: '系统只依据已采集的游客评论作答，没有相关资料时不会编造内容。',
  },
  EMPTY_QUESTION: { text: '问题内容为空。', advice: '请输入你想了解的问题。' },
  LLM_FAILED: {
    text: '这次没能生成成段的文字回答，请稍后再试。',
    advice: '统计数据不受影响，见下方内容。',
  },
};

/* =============================================================================
   数据来源与数字的"用户化"处理（2026-10 表达规范）
   -----------------------------------------------------------------------------
   接口里的 `source` 是**数据库表名/字段标识**（如 stat_spot、spot_report、
   sentiment(method=…)），那是给开发者追溯用的，不该出现在游客页面上。
   原始值仍完整保留在接口返回体与浏览器控制台（`window.__lastQa` + console.info），
   因此**追溯能力不减**；这里只做展示层翻译。
   ============================================================================= */

/** 内部标识 → 游客能读懂的说明（按出现顺序替换，长名在前避免被短名吃掉）。 */
const SOURCE_WORDS = [
  ['comment_semantic', '评论语义'],
  ['stat_overview', '总体规模'],
  ['stat_spot', '景点统计'],
  ['stat_time', '评论量趋势'],
  ['stat_ip', '游客来源统计'],
  ['spot_report', '离线智能评价'],
  ['sentiment', '情感分析'],
  ['aspect', '关注方面'],
  ['review', '游客评论'],
  ['topic', '评论主题'],
  ['spot', '景点信息'],
];
const SOURCE_FALLBACK = '游客评论与离线分析结果';

/** 把 `source` 翻译成人话；**任何情况下都不透出原始英文标识**。 */
function friendlySource(src) {
  let text = String(src || '');
  SOURCE_WORDS.forEach(pair => { text = text.split(pair[0]).join('·' + pair[1] + '·'); });
  text = text
    .replace(/[A-Za-z_][A-Za-z0-9_.]*/g, ' ')   // 英文表名/字段名/参数名等残余
    .replace(/[（(][^）)]*[）)]/g, ' ')          // 括号里的技术说明（含 method=… 之类）
    .replace(/[·=.,，、/]+/g, '、')
    .replace(/、{2,}/g, '、')
    .replace(/^、|、$/g, '')
    .trim();
  const parts = [...new Set(text.split('、').map(s => s.trim()).filter(Boolean))];
  return parts.length ? parts.join('、') : SOURCE_FALLBACK;
}

/** 依据里的数字按人读的方式呈现（只改**显示格式**，不改数值、不改接口字段）：
 *   · 比率/占比 → 87.8%（1 位小数），不再出现 0.8782 这类原始小数；
 *   · 评分 → 4.49；
 *   · 整数数量 → 3,965（千分位）；
 *   · 认不出含义的浮点数 → **原样输出**（不做四舍五入，避免把 0.8782 显示成 0.88 这种失真）。
 *
 *  `key` 是"这一列的含义"：表头本身是 `值`／`数值` 这类通用列时，调用方应传**指标名**
 *  （见 renderFacts 的 labelHint）——否则比率永远匹配不到 /率|占比/，会被降级成两位小数。 */
function formatBasisValue(key, v) {
  if (v === null || v === undefined || v === '') return '<span class="muted">—</span>';
  if (typeof v === 'number') {
    const k = String(key || '');
    if (/率|占比|比例/.test(k)) return fmtPct(v, 1);
    if (/评分/.test(k)) return fmtNum(v, 2);
    if (Number.isInteger(v)) return fmtInt(v);
    return String(v);
  }
  const text = String(v);
  // 长文本事实（例如离线评价摘要，200+ 字、内部还带原始小数）不铺在表格里，
  // 折成一行"查看原文"：既保留可追溯性，又不让"一大段"淹没数据依据。
  if (text.length > 80) {
    return `<details class="fold"><summary>查看原文（${text.length} 字）</summary>`
      + `<div class="fold-body">${escapeHtml(text)}</div></details>`;
  }
  return escapeHtml(text);
}

/** 渲染"数据依据"。
 *
 *  【折叠规则】有自然语言回答时**折叠**（回答才是主角，依据属于辅助信息，点一下即可核对）；
 *  没有回答时（功能未开启 / 生成失败）**默认展开**——那时数据依据本身就是交付内容。
 */
function renderFacts(blocks, opts) {
  if (!blocks || !blocks.length) return '';
  const collapsed = !!(opts && opts.collapsed);
  const sections = blocks.map(block => {
    if (block.source === 'caliber') {
      return `<div class="caliber">数据说明：${escapeHtml((block.items[0] || {}).口径 || '')}</div>`;
    }
    const items = block.items || [];
    if (!items.length) return '';
    // 取所有条目的键并集作为表头（不同条目字段可能不同）。
    // 列序：**标签列在前、说明列在最后**——接口的 JSON 键是按字母序输出的
    // （Flask jsonify 默认排序），直接拿来当表头会读成"3,965 | 评论量"这种倒装。
    const LABEL_ORDER = ['指标', '方面', '景点', '排名', '名称', '项目', '关键词'];
    const keys = [];
    items.forEach(item => Object.keys(item).forEach(k => { if (!keys.includes(k)) keys.push(k); }));
    keys.sort((a, b) => {
      const ia = LABEL_ORDER.indexOf(a), ib = LABEL_ORDER.indexOf(b);
      const ra = ia === -1 ? (a === '说明' || a === '备注' ? 2 : 1) : 0;
      const rb = ib === -1 ? (b === '说明' || b === '备注' ? 2 : 1) : 0;
      if (ra !== rb) return ra - rb;
      if (ra === 0) return ia - ib;
      return 0;   // 其余列保持接口给出的相对顺序
    });
    // 【数字格式的关键一步】事实里大量条目形如 {指标: "好评率（4-5 星占比）", 值: 0.8782}，
    // 表头是通用的"值"，只看列名永远认不出这是比率。因此当列是通用值列、且该条目另有
    // 指标名时，用**指标名**决定显示格式（否则 0.8782 会被当普通浮点数显示）。
    const LABEL_KEYS = ['指标', '方面', '名称', '项目'];
    const VALUE_KEYS = ['值', '数值'];
    const labelHint = item => {
      for (const k of LABEL_KEYS) if (item[k]) return String(item[k]);
      return '';
    };
    const rows = items.map(item => keys.map(k => {
      const hint = (VALUE_KEYS.includes(k) && typeof item[k] === 'number')
        ? (labelHint(item) || k) : k;
      return formatBasisValue(hint, item[k]);
    }));
    return `
      <h4>${escapeHtml(block.title || '')}
        <span class="muted">· 数据来源：${escapeHtml(friendlySource(block.source))}</span>
      </h4>
      ${renderTable(keys, rows)}`;
  }).join('');
  const body = `<p class="hint">以下数字来自系统已入库的评论分析结果，用于说明上面的内容不是凭空生成的。</p>${sections}`;
  if (!collapsed) return `<h3>数据依据</h3>${body}`;
  return `<details class="fold">
      <summary>数据依据（点击展开核对）</summary>
      <div class="fold-body">${body}</div>
    </details>`;
}

function renderQa(d) {
  const typeLabel = TYPE_LABELS[d.question_type] || '问题';
  const spotNames = (d.spots || []).map(s => s.spot_name).join('、');

  // 完整返回体留控制台：内部字段（model / prompt_version / token / reason）不进页面正文，
  // 但排查与答辩时打开控制台即可看到全部口径。
  try { console.info('[智能问答] 接口返回', d); window.__lastQa = d; } catch (e) { /* 忽略 */ }

  const head = window.UI.statCards([
    { label: '问题类型', value: escapeHtml(typeLabel) },
    { label: '识别到的景点', value: (d.spots || []).length ? `${(d.spots || []).length} 个` : '—',
      sub: spotNames || '未涉及具体景点' },
    { label: '分析样本量', value: fmtInt(d.sample_size), sub: '条 · 来自评论分析结果' },
  ]);

  if (d.available && d.answer) {
    const outOfScope = d.question_type === 'OUT_OF_SCOPE';
    // 【2026-10 呈现收尾】**结论先行**：回答放在最前（它才是用户要的东西），
    // "问题类型／识别到的景点／分析样本量"三张卡移到回答之后做补充说明，
    // 数据依据继续折叠在最后。原来三张卡排在回答之前，会把回答压到折叠线以下。
    return `
      ${outOfScope
        ? window.UI.notice('这个问题不在本系统的分析范围内，系统已直接说明、没有作任何推测。', 'warn')
        : ''}
      <div class="qa-answer">
        <h4>回答</h4>
        <p>${escapeHtml(d.answer)}</p>
        ${d.need_review
          ? window.UI.notice(d.need_review_note
              || '部分统计数据存在一致性提示，相关内容已由系统核对；'
                + '阅读时建议结合样本量综合判断。', 'warn')
          : ''}
      </div>
      ${head}
      ${renderFacts(d.facts, { collapsed: true })}
      ${caliberNote(d.caliber_note, d.sample_size)}`;
  }

  // 未生成回答：如实说明原因 + 展示已检索到的依据（依据本身就有价值）
  const known = REASON_TEXT[d.reason] || {};
  const text = known.text || '这次只给出了统计数据，还没有生成成段的文字回答。';
  const advice = known.advice || '';
  return `
    ${head}
    ${window.UI.notice(text, 'warn')}
    ${advice ? `<p class="hint">${escapeHtml(advice)}</p>` : ''}
    ${renderFacts(d.facts)}
    ${caliberNote(d.caliber_note, d.sample_size)}`;
}

async function doAsk(question) {
  const input = document.getElementById('qa-question');
  const box = document.getElementById('qa-body');
  if (!box) return;
  const q = question !== undefined ? question : (input ? input.value.trim() : '');
  if (!q) {
    box.innerHTML = window.UI.empty('请先输入你的问题。', 'warn');
    return;
  }
  if (input) input.value = q;
  box.innerHTML = '<p class="hint">正在检索相关数据…</p>';
  try {
    const resp = await axios.post('/api/qa/ask', { question: q });
    const body = resp.data || {};
    if (body.code !== 0) {
      box.innerHTML = window.UI.notice(
        `提问失败：${window.UI.userMessage({ code: body.code, message: body.message })}`,
        'bad', `业务码 ${body.code}：${body.message}`);
      return;
    }
    box.innerHTML = renderQa(body.data || {});
  } catch (e) {
    const data = e.response && e.response.data;
    box.innerHTML = window.UI.notice(
      `提问失败：${window.UI.userMessage(data ? { code: data.code, message: data.message } : e)}`,
      'bad', data ? `业务码 ${data.code}：${data.message}` : String(e));
  }
}

document.addEventListener('DOMContentLoaded', () => {
  const btn = document.getElementById('btn-ask');
  if (btn) btn.addEventListener('click', () => doAsk());
  const input = document.getElementById('qa-question');
  if (input) {
    input.addEventListener('keydown', ev => { if (ev.key === 'Enter') doAsk(); });
  }
  document.querySelectorAll('.chip[data-q]').forEach(chip => {
    chip.addEventListener('click', () => doAsk(chip.getAttribute('data-q')));
  });
});
