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

/** 未生成回答的原因 → 用户可读的说法 + 建议。 */
const REASON_TEXT = {
  LIVE_DISABLED: {
    text: '自然语言回答功能当前未开启。',
    advice: '系统的检索与分析结果仍然可用——下面就是根据你的问题检索到的数据依据。',
  },
  API_KEY_MISSING: {
    text: '自然语言回答功能当前未配置，暂时无法生成成段回答。',
    advice: '检索到的数据依据同样可以回答你的问题，见下方表格。',
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
    text: '数据库里没有与这个问题相关的信息。',
    advice: '系统只依据已采集的评论数据作答，没有相关数据时不会编造内容。',
  },
  EMPTY_QUESTION: { text: '问题内容为空。', advice: '请输入你想了解的问题。' },
  LLM_FAILED: {
    text: '生成回答时出现异常，本次没能给出成段回答。',
    advice: '检索到的数据依据不受影响，见下方表格；稍后可以再试一次。',
  },
};

/** 渲染"检索到的结构化事实"（只展示结构化事实，不含原始评论全集）。 */
function renderFacts(blocks) {
  if (!blocks || !blocks.length) return '';
  const sections = blocks.map(block => {
    if (block.source === 'caliber') {
      return `<div class="caliber">数据口径：${escapeHtml((block.items[0] || {}).口径 || '')}</div>`;
    }
    const items = block.items || [];
    if (!items.length) return '';
    // 取所有条目的键并集作为表头（不同条目字段可能不同）
    const keys = [];
    items.forEach(item => Object.keys(item).forEach(k => { if (!keys.includes(k)) keys.push(k); }));
    const rows = items.map(item => keys.map(k => {
      const v = item[k];
      if (v === null || v === undefined) return '<span class="muted">—</span>';
      if (typeof v === 'number') return Number.isInteger(v) ? fmtInt(v) : String(v);
      return escapeHtml(v);
    }));
    return `
      <h4>${escapeHtml(block.title || '')}</h4>
      ${renderTable(keys, rows)}`;
  }).join('');
  return `<h3>数据依据</h3>
    <p class="hint">回答只依据以下来自评论数据的事实，不引入外部信息。</p>${sections}`;
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
    { label: '数据依据条数', value: fmtInt(d.sample_size), sub: '来自评论分析结果' },
  ]);

  if (d.available && d.answer) {
    const outOfScope = d.question_type === 'OUT_OF_SCOPE';
    return `
      ${head}
      ${outOfScope
        ? window.UI.notice('这个问题超出了系统的可回答范围，系统已直接说明、未作任何推测。', 'warn')
        : ''}
      <div class="qa-answer">
        <h4>回答</h4>
        <p>${escapeHtml(d.answer)}</p>
        ${d.need_review
          ? window.UI.notice('回答中的部分数字与提供的数据依据不完全一致，已标记待人工复核。', 'warn')
          : ''}
      </div>
      ${renderFacts(d.facts)}
      ${caliberNote(d.caliber_note, d.sample_size)}`;
  }

  // 未生成回答：如实说明原因 + 展示已检索到的依据（依据本身就有价值）
  const known = REASON_TEXT[d.reason] || {};
  const text = known.text || '本次没有生成成段回答。';
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
