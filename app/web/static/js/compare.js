/* M4 景点对比页：指标与方面由后端计算；解读默认关闭（零 API 消费）。
   注意：本页只调用 /api/compare —— 该接口在默认配置下不会调用任何模型。 */

/** 载入两个下拉框的景点选项（按评论量降序，取前 200 个便于选择）。 */
async function loadCompareOptions() {
  const a = document.getElementById('cmp-a');
  const b = document.getElementById('cmp-b');
  try {
    const data = await apiGet('/api/spots/ranking', { by: 'reviews', limit: 100 });
    const options = data.items.map(item =>
      `<option value="${item.spot_id}">${escapeHtml(item.spot_name)}（${fmtInt(item.review_count)} 条）</option>`
    ).join('');
    a.innerHTML = '<option value="">— 景点 A —</option>' + options;
    b.innerHTML = '<option value="">— 景点 B —</option>' + options;

    // 若从景点分析页选过景点，自动作为 A
    const active = window.UI.getActiveSpot();
    if (active && data.items.some(i => String(i.spot_id) === String(active))) a.value = active;
  } catch (e) {
    showError('cmp-body', e);
  }
}

function cmpValue(v, kind) {
  if (v === null || v === undefined) return '—';
  if (kind === 'rate') return fmtPct(v);
  if (kind === 'score') return fmtNum(v, 2);
  return fmtInt(v);
}

function deltaText(delta, kind) {
  if (delta === null || delta === undefined) return '—';
  if (delta === 0) return '<span class="muted">持平</span>';
  const sign = delta > 0 ? '+' : '';
  let text;
  if (kind === 'rate') text = sign + (delta * 100).toFixed(2) + ' 个百分点';
  else if (kind === 'score') text = sign + delta.toFixed(2);
  else text = sign + fmtInt(delta);
  const cls = delta > 0 ? 'up' : 'down';
  return `<span class="${cls}">${text}</span>`;
}

function renderCompare(d) {
  const A = d.facts.spot_a, B = d.facts.spot_b, diff = d.facts.diff;
  const kindOf = {
    review_count: 'int', avg_score: 'score', positive_rate: 'rate',
    negative_rate: 'rate', image_rate: 'rate', total_likes: 'int',
  };

  // 指标对比表
  const indicatorRows = d.indicator_compare.map(row => [
    escapeHtml(row.label),
    cmpValue(row.spot_a, kindOf[row.key]),
    cmpValue(row.spot_b, kindOf[row.key]),
    deltaText(row.delta, kindOf[row.key]),
  ]);

  // 情感对比
  const sentRow = (label, key) => [
    label,
    A.sentiment[key] === null ? '—' : fmtPct(A.sentiment[key]),
    B.sentiment[key] === null ? '—' : fmtPct(B.sentiment[key]),
    (A.sentiment[key] === null || B.sentiment[key] === null) ? '—'
      : deltaText(Math.round((A.sentiment[key] - B.sentiment[key]) * 10000) / 10000, 'rate'),
  ];

  // 方面对比
  const aspectRows = d.facts.aspects.map(item => {
    const side = s => {
      if (!s) return '<span class="muted">该景点无此方面</span>';
      if (!s.conclusive) return `<span class="muted">样本 ${fmtInt(s.sample_size)}，不出结论</span>`;
      return `样本 ${fmtInt(s.sample_size)}，正面 ${fmtPct(s.positive_rate)}，负面 ${fmtPct(s.negative_rate)}`;
    };
    const anyConclusive = (item.a && item.a.conclusive) || (item.b && item.b.conclusive);
    return [
      escapeHtml(item.aspect),
      side(item.a),
      side(item.b),
      anyConclusive ? '<span class="tag ok">可结论</span>' : '<span class="tag warn">样本不足</span>',
    ];
  });

  const interp = d.interpretation;
  const interpHtml = interp.available
    ? `
      <div class="rep-summary">
        <h4>指标差异</h4>
        <ul>${interp.differences.map(x => `<li>${escapeHtml(x)}</li>`).join('')}</ul>
        <h4>可能原因</h4>
        <ul>${interp.possible_reasons.map(x => `<li>${escapeHtml(x)}</li>`).join('') || '<li class="muted">无</li>'}</ul>
        ${interp.reliability_note ? `<div class="alert warn">${escapeHtml(interp.reliability_note)}</div>` : ''}
        ${interp.need_review ? `<div class="alert warn">${escapeHtml(interp.need_review_note || '')}</div>` : ''}
        <p class="hint">模型：${escapeHtml(interp.model || '')}　Prompt 版本：${escapeHtml(interp.prompt_version || '')}
          　生成时间：${escapeHtml(interp.generated_at || '')}　token：${fmtInt(interp.token_usage)}</p>
      </div>`
    : `<div class="alert warn">
         <strong>解读不可用</strong>（原因代码：${escapeHtml(interp.reason || '')}）<br/>
         ${escapeHtml(interp.message_text || '')}
         ${interp.reliability_note ? `<br/><br/>${escapeHtml(interp.reliability_note)}` : ''}
       </div>`;

  return `
    ${window.UI.statCards([
      { label: '景点 A', value: escapeHtml(A.spot_name), sub: `${fmtInt(A.review_count)} 条评论` },
      { label: '景点 B', value: escapeHtml(B.spot_name), sub: `${fmtInt(B.review_count)} 条评论` },
      { label: '评论量差', value: deltaText(diff.review_count_delta, 'int') },
      { label: '样本量比', value: diff.sample_ratio === null ? '—' : diff.sample_ratio + ' 倍',
        sub: diff.reliability_warning ? '样本悬殊，需谨慎解读' : '样本量级相当' },
    ])}
    ${diff.reliability_warning ? `<div class="alert warn">可靠性提示：${escapeHtml(diff.reliability_warning)}</div>` : ''}

    <h3>指标对比</h3>
    ${renderTable(['指标', escapeHtml(A.spot_name), escapeHtml(B.spot_name), '差值（A−B）'], indicatorRows)}

    <h3>情感对比（method=deepseek）</h3>
    ${renderTable(
      ['极性', escapeHtml(A.spot_name), escapeHtml(B.spot_name), '差值（A−B）'],
      [sentRow('正面', 'positive'), sentRow('中性', 'neutral'), sentRow('负面', 'negative')]
    )}
    <p class="hint">
      A 的 deepseek 样本量 ${fmtInt(A.sentiment.sample_size)} 条，B 为 ${fmtInt(B.sentiment.sample_size)} 条。
      无 deepseek 结果时该侧显示"—"（不退回 mllib 口径冒充同一口径）。
    </p>

    <h3>方面对比</h3>
    <p class="hint">任一方该方面样本 &lt;10 条时只展示样本量、不出结论（BR-04）。</p>
    ${aspectRows.length
      ? renderTable(['方面', escapeHtml(A.spot_name), escapeHtml(B.spot_name), '门槛'], aspectRows)
      : window.UI.empty('两个景点都还没有方面级结果（方面来自 DeepSeek 离线语义分析）。', 'warn')}

    <h3>对比解读</h3>
    ${interpHtml}
    ${caliberNote(d.caliber_note, `${fmtInt(d.sample_size.spot_a)} / ${fmtInt(d.sample_size.spot_b)}`)}`;
}

async function doCompare() {
  const a = document.getElementById('cmp-a').value;
  const b = document.getElementById('cmp-b').value;
  const box = document.getElementById('cmp-body');
  if (!a || !b) {
    box.innerHTML = window.UI.empty('请先选择两个景点。', 'warn');
    return;
  }
  box.innerHTML = '<p class="hint">计算中…</p>';
  try {
    const data = await apiGet('/api/compare', { spot_a: a, spot_b: b });
    box.innerHTML = renderCompare(data);
  } catch (e) {
    box.innerHTML = `<div class="alert bad">对比失败（code=${escapeHtml(e.code || '')}）：${escapeHtml(e.message)}</div>`;
  }
}

document.addEventListener('DOMContentLoaded', () => {
  loadCompareOptions();
  document.getElementById('btn-compare').addEventListener('click', doCompare);
  document.getElementById('btn-cmp-reset').addEventListener('click', () => {
    document.getElementById('cmp-a').value = '';
    document.getElementById('cmp-b').value = '';
    document.getElementById('cmp-body').innerHTML = '';
  });
});
