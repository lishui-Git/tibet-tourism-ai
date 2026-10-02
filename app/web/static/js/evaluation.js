/* 智能评价页（M3）：读取离线生成的景点评价与事实依据。
   本页只调用 /api/spots/{id}/report —— 该接口只读数据库，不会触发模型调用。 */

/** 载入"具备评价资格（评论量 ≥100）"的景点下拉列表。 */
async function loadSpotOptions() {
  const sel = document.getElementById('eval-spot');
  try {
    const data = await apiGet('/api/spots/ranking', { by: 'reviews', limit: 100 });
    const eligible = data.items.filter(item => item.has_full_evaluation);
    sel.innerHTML = '<option value="">— 选择具备评价资格的景点（评论量 ≥100）—</option>'
      + eligible.map(item =>
        `<option value="${item.spot_id}">${escapeHtml(item.spot_name)}（${fmtInt(item.review_count)} 条评论）</option>`
      ).join('');
    // 若从"景点分析"页选过景点，自动选中
    const active = window.UI.getActiveSpot();
    if (active && eligible.some(item => String(item.spot_id) === String(active))) {
      sel.value = active;
      loadReport();
    }
  } catch (e) {
    showError('eval-body', e);
  }
}

async function loadReport() {
  const spotId = document.getElementById('eval-spot').value;
  const box = document.getElementById('eval-body');
  if (!spotId) {
    box.innerHTML = window.UI.empty('请先选择一个景点。', 'warn');
    return;
  }
  box.innerHTML = '<p class="hint">加载中…</p>';
  try {
    const data = await apiGet(`/api/spots/${spotId}/report`);

    // 数据不足 / 尚未生成 → 正常业务响应（available=false），按提示语展示而非报错
    if (!data.available) {
      box.innerHTML = `
        <div class="alert warn">
          <strong>智能评价不可用</strong><br/>
          ${escapeHtml(data.message_text || '')}<br/>
          <span class="muted">原因代码：${escapeHtml(data.reason || '')}　评论量：${fmtInt(data.review_count)}</span>
        </div>
        ${caliberNote(data.caliber_note, data.review_count)}`;
      return;
    }

    const r = data.report;
    const basis = data.basis || {};
    const list = (title, items, cls) => `
      <div class="rep-col ${cls}">
        <h4>${title}</h4>
        <ul>${(items || []).map(x => `<li>${escapeHtml(x)}</li>`).join('') || '<li class="muted">无</li>'}</ul>
      </div>`;

    box.innerHTML = `
      ${data.need_review ? `<div class="alert warn">${escapeHtml(data.need_review_note || '该内容待人工复核')}</div>` : ''}
      <div class="rep-summary">
        <h3>综合评价</h3>
        <p>${escapeHtml(r.summary)}</p>
      </div>
      <div class="rep-grid">
        ${list('主要优势', r.advantages, 'pos')}
        ${list('主要问题', r.issues, 'neg')}
        ${list('游客关注点', r.visitor_focus, 'focus')}
      </div>

      <h3>事实依据（评价只允许引用这些数字）</h3>
      ${window.UI.statCards([
        { label: '评论量', value: fmtInt(basis.review_count) },
        { label: '平均评分', value: fmtNum(basis.avg_score, 2) },
        { label: '好评率', value: fmtPct(basis.positive_rate) },
        { label: '差评率', value: fmtPct(basis.negative_rate) },
        {
          label: '情感样本量',
          value: basis.sentiment ? fmtInt(basis.sentiment.sample_size) : '—',
          sub: basis.sentiment ? `正 ${fmtPct(basis.sentiment.positive)} / 中 ${fmtPct(basis.sentiment.neutral)} / 负 ${fmtPct(basis.sentiment.negative)}` : '无'
        },
      ])}
      <p class="hint">依据来源：${basis.source === 'fact_package'
        ? `事实包快照（版本 ${escapeHtml(basis.version || '')}，生成于 ${escapeHtml(basis.generated_at || '')}）—— 保证评价可追溯（FR-IE-07）`
        : `当前统计表 stat_spot（版本 ${escapeHtml(basis.version || '')}）`}</p>

      ${(basis.top_aspects && basis.top_aspects.length) ? `
        <h4>方面依据（样本量前 5）</h4>
        ${renderTable(['方面', '样本量', '正面占比', '负面占比', '说明'],
          basis.top_aspects.map(a => [
            escapeHtml(a.name), fmtInt(a.sample_size),
            a.positive_rate === null ? '<span class="muted">不出结论</span>' : fmtPct(a.positive_rate),
            a.negative_rate === null ? '<span class="muted">不出结论</span>' : fmtPct(a.negative_rate),
            a.note ? escapeHtml(a.note) : '—',
          ]))}` : ''}

      <p class="hint">
        模型：${escapeHtml(data.model || '')}　Prompt 版本：${escapeHtml(data.prompt_version || '')}　
        事实包版本：${escapeHtml(data.fact_package_version || '')}　生成时间：${escapeHtml(data.generated_at || '')}
      </p>
      ${caliberNote(data.caliber_note, data.sample_size)}`;
  } catch (e) {
    box.innerHTML = `<div class="alert bad">加载失败：${escapeHtml(e.message)}</div>`;
  }
}

/** 只有管理员才显示"重新生成评价"按钮（接口 19 需管理员）。
    该按钮只**登记请求**，不产生任何模型调用——真正生成由离线批处理执行。 */
async function setupRegenButton() {
  const btn = document.getElementById('btn-regen');
  try {
    const me = await apiGet('/api/auth/me');
    if (me && me.is_admin) btn.style.display = '';
  } catch (e) {
    btn.style.display = 'none';  // 未登录/非管理员：不显示
  }
}

async function doRegenerate() {
  const spotId = document.getElementById('eval-spot').value;
  const box = document.getElementById('eval-body');
  if (!spotId) {
    box.innerHTML = window.UI.empty('请先选择一个景点。', 'warn');
    return;
  }
  const btn = document.getElementById('btn-regen');
  btn.disabled = true;
  try {
    const resp = await axios.post(`/api/spots/${spotId}/report/regenerate`);
    const body = resp.data || {};
    if (body.code !== 0) {
      box.innerHTML = `<div class="alert bad">提交失败：${escapeHtml(body.message || '')}（code=${body.code}）</div>`;
      return;
    }
    const d = body.data || {};
    box.innerHTML = d.submitted
      ? `<div class="alert">
           <strong>重新生成请求已提交</strong>（任务 #${d.task_id}，状态 pending）。<br/>
           ${escapeHtml(d.message_text || '')}<br/>
           <span class="muted">本接口只登记请求，不产生模型调用；实际生成与费用发生在离线批处理。</span>
         </div>`
      : `<div class="alert warn">${escapeHtml(d.message_text || '该景点不生成智能评价。')}</div>`;
  } catch (e) {
    const data = e.response && e.response.data;
    box.innerHTML = `<div class="alert bad">提交失败：${escapeHtml((data && data.message) || e.message)}
      ${data ? `（code=${data.code}）` : ''}</div>`;
  } finally {
    btn.disabled = false;
  }
}

document.addEventListener('DOMContentLoaded', () => {
  loadSpotOptions();
  setupRegenButton();
  document.getElementById('btn-load-report').addEventListener('click', loadReport);
  document.getElementById('btn-regen').addEventListener('click', doRegenerate);
  document.getElementById('eval-spot').addEventListener('change', () => {
    if (document.getElementById('eval-spot').value) loadReport();
  });
});
