/* M5 智能问答页：提问 → 展示系统回答或"不可用原因 + 数据依据"。
   本页只调用 /api/qa/ask —— 该接口默认不会调用模型（APP_QA_LIVE=0）。 */

const TYPE_LABELS = {
  SPOT_EVALUATION: '景点评价咨询',
  SPOT_COMPARISON: '景点差异对比',
  VISITOR_FOCUS: '游客关注点',
  SENTIMENT_EXPLAIN: '情感／方面解释',
  DATA_METRIC: '数据指标查询',
  RANKING: '排行相关',
  OUT_OF_SCOPE: '超范围（拒答）',
};

const REASON_LABELS = {
  LIVE_DISABLED: '问答生成为在线能力，当前已关闭',
  API_KEY_MISSING: '未配置 API Key',
  SPOT_NOT_RECOGNIZED: '未能识别景点名称',
  NEED_TWO_SPOTS: '对比需要两个景点',
  NO_DATA: '数据库中没有相关信息',
  EMPTY_QUESTION: '问题为空',
  LLM_FAILED: '模型调用失败',
};

/** 渲染"检索到的结构化事实"（CC-3：只展示结构化事实，不含原始评论全集）。 */
function renderFacts(blocks) {
  if (!blocks || !blocks.length) return '';
  const sections = blocks.map(block => {
    if (block.source === 'caliber') {
      return `<div class="caliber">口径说明：${escapeHtml((block.items[0] || {}).口径 || '')}</div>`;
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
      <h4>${escapeHtml(block.title || '')}
        <span class="muted">（来源：${escapeHtml(block.source || '')}）</span></h4>
      ${renderTable(keys, rows)}`;
  }).join('');
  return `<h3>数据依据（回答只能引用这些事实）</h3>${sections}`;
}

function renderQa(d) {
  const typeLabel = TYPE_LABELS[d.question_type] || d.question_type;
  const spotTags = (d.spots || []).map(s => `<span class="kw">${escapeHtml(s.spot_name)}</span>`).join('');
  const head = `
    ${window.UI.statCards([
      { label: '问题类型', value: escapeHtml(typeLabel) },
      { label: '识别到的景点', value: (d.spots || []).length ? (d.spots || []).length + ' 个' : '—', sub: (d.spots || []).map(s => s.spot_name).join('、') },
      { label: '本次是否调用模型', value: d.reached_model ? '<span class="tag warn">是</span>' : '<span class="tag ok">否（零成本）</span>' },
      { label: '事实样本量', value: fmtInt(d.sample_size) },
    ])}`;

  if (d.available && d.answer) {
    const modelLine = d.reached_model
      ? `<p class="hint">模型：${escapeHtml(d.model || '')}　Prompt 版本：${escapeHtml(d.prompt_version || '')}
         　token：${fmtInt(d.token_usage)}　生成时间：${escapeHtml(d.generated_at || '')}</p>`
      : '';
    return `
      ${head}
      ${d.question_type === 'OUT_OF_SCOPE' ? '<div class="alert warn">该问题超出系统可回答范围（已直接拒答，未调用模型）。</div>' : ''}
      <div class="qa-answer">
        <h4>回答</h4>
        <p>${escapeHtml(d.answer)}</p>
        ${d.need_review ? '<div class="alert warn">回答中的数字与提供的事实不完全一致，已标记待人工复核。</div>' : ''}
      </div>
      ${modelLine}
      ${renderFacts(d.facts)}
      ${caliberNote(d.caliber_note, d.sample_size)}`;
  }

  // 未生成回答：如实说明原因 + 展示已检索到的依据
  const reasonLabel = REASON_LABELS[d.reason] || d.reason || '';
  return `
    ${head}
    <div class="alert warn">
      <strong>未生成自然语言回答</strong>（原因：${escapeHtml(reasonLabel)}）<br/>
      ${escapeHtml(d.message_text || '')}
    </div>
    ${renderFacts(d.facts)}
    ${caliberNote(d.caliber_note, d.sample_size)}`;
}

async function doAsk(question) {
  const q = question !== undefined ? question : document.getElementById('qa-question').value.trim();
  const box = document.getElementById('qa-body');
  if (!q) {
    box.innerHTML = window.UI.empty('请输入问题。', 'warn');
    return;
  }
  if (question !== undefined) document.getElementById('qa-question').value = q;
  box.innerHTML = '<p class="hint">处理中（分类 → 识别景点 → 检索事实）…</p>';
  try {
    const resp = await axios.post('/api/qa/ask', { question: q });
    const body = resp.data || {};
    if (body.code !== 0) {
      box.innerHTML = `<div class="alert bad">提问失败：${escapeHtml(body.message || '')}（code=${body.code}）</div>`;
      return;
    }
    box.innerHTML = renderQa(body.data);
  } catch (e) {
    const data = e.response && e.response.data;
    box.innerHTML = `<div class="alert bad">提问失败：${escapeHtml((data && data.message) || e.message)}
      ${data ? `（code=${data.code}）` : ''}</div>`;
  }
}

document.addEventListener('DOMContentLoaded', () => {
  document.getElementById('btn-ask').addEventListener('click', () => doAsk());
  document.getElementById('qa-question').addEventListener('keydown', ev => {
    if (ev.key === 'Enter') doAsk();
  });
  document.querySelectorAll('.chip[data-q]').forEach(chip => {
    chip.addEventListener('click', () => doAsk(chip.getAttribute('data-q')));
  });
});
