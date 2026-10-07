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

    // 从「景点分析」页或首页带过来的景点：若不在前 200 名，补一个选项，避免"选了却没反应"
    const active = window.UI.getActiveSpot();
    if (active && !items.some(it => String(it.spot_id) === String(active))) {
      try {
        const one = await apiGet(`/api/spots/${active}`);
        sel.insertAdjacentHTML('beforeend',
          `<option value="${one.spot_id}">${escapeHtml(one.spot_name)}</option>`);
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

  return `
    ${window.UI.pendingNotice(title, hint)}
    ${gap}
    <p class="hint">
      你仍然可以查看该景点的
      <a class="link" href="/spots?spot=${encodeURIComponent(data.spot_id || '')}">基础统计与代表评论</a>，
      或在上方换一个评论量更多的景点。
    </p>
    ${caliberNote(data.caliber_note, data.review_count)}`;
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
      return;
    }
    renderAvailable(data, box);
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
  const list = (title, items, cls) => `
    <div class="rep-col ${cls}">
      <h4>${title}</h4>
      <ul>${(items || []).map(x => `<li>${escapeHtml(x)}</li>`).join('') || '<li class="muted">暂无可提炼内容</li>'}</ul>
    </div>`;

  box.innerHTML = `
    ${data.need_review ? window.UI.notice(data.need_review_note || '该内容中的部分数字与数据依据不完全一致，已标记待人工复核。', 'warn') : ''}

    <div class="rep-summary">
      <h3>综合评价</h3>
      <p>${escapeHtml(r.summary || '')}</p>
    </div>

    <div class="rep-grid">
      ${list('主要优势', r.advantages, 'pos')}
      ${list('主要问题', r.issues, 'neg')}
      ${list('游客关注点', r.visitor_focus, 'focus')}
    </div>

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
      <h4>游客关注的方面</h4>
      <p class="hint">样本量不足 10 条的方面只展示样本量，不给出结论。</p>
      ${renderTable(['方面', '样本量', '正面占比', '负面占比', '说明'],
        basis.top_aspects.map(a => [
          escapeHtml(a.name), fmtInt(a.sample_size),
          a.positive_rate === null ? '<span class="muted">样本不足</span>' : fmtPct(a.positive_rate),
          a.negative_rate === null ? '<span class="muted">样本不足</span>' : fmtPct(a.negative_rate),
          a.note ? escapeHtml(a.note) : '—',
        ]))}` : ''}

    ${caliberNote(data.caliber_note, data.sample_size)}`;
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
