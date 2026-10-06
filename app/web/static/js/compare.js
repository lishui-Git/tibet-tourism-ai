/* 景点对比页：指标差异与方面对比由后端计算；解读为可选的在线生成（默认关闭，零费用）。
   只调用 /api/compare 与 /api/spots/ranking —— 默认配置下都不会触发任何模型调用。

   【文案纪律】面向普通用户：
     · 不显示 reason 码（如 LIVE_DISABLED）、model、prompt 版本、token；
     · "解读不可用"要说清"数据部分仍然可用"，并收好技术细节；
     · **相同景点必须在提交前拦截**，不能等接口报错才告诉用户。 */

/** 载入两个下拉框的景点选项（按评论量降序，取前 100）。
    注意：接口对 limit 的上限是 **100**，传更大值会直接返回参数错误（1002）。 */
async function loadCompareOptions() {
  const a = document.getElementById('cmp-a');
  const b = document.getElementById('cmp-b');
  try {
    const data = await apiGet('/api/spots/ranking', { by: 'reviews', limit: 100 });
    const items = data.items || [];
    const options = items.map(item =>
      `<option value="${item.spot_id}">${escapeHtml(item.spot_name)}（${fmtInt(item.review_count)} 条评论）</option>`
    ).join('');
    a.innerHTML = '<option value="">— 景点 A —</option>' + options;
    b.innerHTML = '<option value="">— 景点 B —</option>' + options;

    // 从「景点分析」页选过景点：作为 A，并把 B 预选为另一个不同景点（避免默认就相同）
    const active = String(window.UI.getActiveSpot() || '');
    if (active && items.some(i => String(i.spot_id) === active)) {
      a.value = active;
      const other = items.find(i => String(i.spot_id) !== active);
      if (other) b.value = String(other.spot_id);
    } else if (items.length >= 2) {
      // 默认就给两个**不同**的景点，用户一进来就能直接点「开始对比」看到效果
      a.value = String(items[0].spot_id);
      b.value = String(items[1].spot_id);
    }
  } catch (e) {
    document.getElementById('cmp-body').innerHTML = window.UI.notice(
      `暂时无法加载景点列表：${window.UI.userMessage(e)}`, 'bad',
      e && e.code !== undefined ? `业务码 ${e.code}：${e.message}` : String(e));
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

/** 情感对比固定按深度语义判定口径（两侧口径必须一致，不能一边用基线冒充）。 */
function renderCompare(d) {
  const A = d.facts.spot_a, B = d.facts.spot_b, diff = d.facts.diff;
  const kindOf = {
    review_count: 'int', avg_score: 'score', positive_rate: 'rate',
    negative_rate: 'rate', image_rate: 'rate', total_likes: 'int',
  };

  const indicatorRows = (d.indicator_compare || []).map(row => [
    escapeHtml(row.label),
    cmpValue(row.spot_a, kindOf[row.key]),
    cmpValue(row.spot_b, kindOf[row.key]),
    deltaText(row.delta, kindOf[row.key]),
  ]);

  const sentRow = (label, key) => {
    const av = A.sentiment ? A.sentiment[key] : null;
    const bv = B.sentiment ? B.sentiment[key] : null;
    return [
      label,
      av === null || av === undefined ? '—' : fmtPct(av),
      bv === null || bv === undefined ? '—' : fmtPct(bv),
      (av === null || av === undefined || bv === null || bv === undefined)
        ? '—'
        : deltaText(Math.round((av - bv) * 10000) / 10000, 'rate'),
    ];
  };

  const aspectRows = (d.facts.aspects || []).map(item => {
    const side = s => {
      if (!s) return '<span class="muted">该景点无此方面</span>';
      if (!s.conclusive) return `<span class="muted">样本仅 ${fmtInt(s.sample_size)} 条，暂不出结论</span>`;
      return `样本 ${fmtInt(s.sample_size)}：正面 ${fmtPct(s.positive_rate)}、负面 ${fmtPct(s.negative_rate)}`;
    };
    const anyConclusive = (item.a && item.a.conclusive) || (item.b && item.b.conclusive);
    return [
      escapeHtml(item.aspect),
      side(item.a),
      side(item.b),
      anyConclusive ? '<span class="tag ok">可比较</span>' : '<span class="tag warn">样本不足</span>',
    ];
  });

  const interp = d.interpretation || {};
  const interpHtml = interp.available
    ? `
      <div class="rep-summary">
        <h4>指标差异</h4>
        <ul>${(interp.differences || []).map(x => `<li>${escapeHtml(x)}</li>`).join('')}</ul>
        <h4>可能原因</h4>
        <ul>${(interp.possible_reasons || []).map(x => `<li>${escapeHtml(x)}</li>`).join('') || '<li class="muted">无</li>'}</ul>
        ${interp.reliability_note ? window.UI.notice(interp.reliability_note, 'warn') : ''}
        ${interp.need_review ? window.UI.notice(interp.need_review_note || '该解读待人工复核。', 'warn') : ''}
      </div>`
    : window.UI.notice('本次没有生成文字解读（该项为可选功能，当前未开启）。'
        + '这不影响下面的数据对比——所有指标差异都由系统直接计算，始终可用。', 'warn');

  return `
    ${window.UI.statCards([
      { label: '景点 A', value: escapeHtml(A.spot_name), sub: `${fmtInt(A.review_count)} 条评论` },
      { label: '景点 B', value: escapeHtml(B.spot_name), sub: `${fmtInt(B.review_count)} 条评论` },
      { label: '评论量差', value: deltaText(diff.review_count_delta, 'int'), sub: 'A 减 B' },
      { label: '样本量比', value: diff.sample_ratio === null || diff.sample_ratio === undefined ? '—' : diff.sample_ratio + ' 倍',
        sub: diff.reliability_warning ? '样本量悬殊，解读需谨慎' : '样本量级相当' },
    ])}
    ${diff.reliability_warning ? window.UI.notice(`${diff.reliability_warning}`, 'warn') : ''}

    <h3>指标对比</h3>
    ${renderTable(['指标', escapeHtml(A.spot_name), escapeHtml(B.spot_name), '差值（A−B）'], indicatorRows)}

    <h3>情感分布对比</h3>
    ${renderTable(
      ['情感倾向', escapeHtml(A.spot_name), escapeHtml(B.spot_name), '差值（A−B）'],
      [sentRow('正面', 'positive'), sentRow('中性', 'neutral'), sentRow('负面', 'negative')]
    )}
    <p class="hint">
      两侧均采用同一套判定口径，样本量为
      ${fmtInt((A.sentiment || {}).sample_size)} 条与 ${fmtInt((B.sentiment || {}).sample_size)} 条。
      某一侧尚无判定结果时显示"—"，不会用另一种口径的数字顶替。
    </p>

    <h3>游客关注的方面</h3>
    <p class="hint">某一方该方面的有效样本不足 10 条时，只展示样本量、不给出结论。</p>
    ${aspectRows.length
      ? renderTable(['方面', escapeHtml(A.spot_name), escapeHtml(B.spot_name), '可比性'], aspectRows)
      : window.UI.empty('这两个景点目前都还没有方面级分析结果。', 'empty')}

    <h3>对比解读</h3>
    ${interpHtml}
    ${caliberNote(d.caliber_note, `${fmtInt((d.sample_size || {}).spot_a)} / ${fmtInt((d.sample_size || {}).spot_b)}`)}`;
}

/** 提交前的校验：把"相同景点"这类问题在本地拦下，不让用户看到接口报错。 */
function validateSelection(a, b) {
  const box = document.getElementById('cmp-body');
  if (!a || !b) {
    box.innerHTML = window.UI.empty('请先选择两个景点。', 'warn');
    return false;
  }
  if (String(a) === String(b)) {
    // 同一景点对比没有意义：直接说明，并给出下一步怎么做
    box.innerHTML = window.UI.notice('请选择两个不同的景点进行对比。', 'warn');
    return false;
  }
  return true;
}

async function doCompare() {
  const a = document.getElementById('cmp-a').value;
  const b = document.getElementById('cmp-b').value;
  const box = document.getElementById('cmp-body');
  if (!validateSelection(a, b)) return;

  box.innerHTML = '<p class="hint">计算中…</p>';
  try {
    const data = await apiGet('/api/compare', { spot_a: a, spot_b: b });
    // 返回体留控制台（含解读的 model / prompt_version / token / reason 等内部字段）
    try { console.info('[景点对比] 接口返回', data); window.__lastCompare = data; } catch (e2) { /* 忽略 */ }
    box.innerHTML = renderCompare(data);
  } catch (e) {
    // 兜底：即使后端拒绝（例如传了同一个景点），也只给用户看人话
    box.innerHTML = window.UI.notice(
      `对比未完成：${window.UI.userMessage(e)}`, 'bad',
      e && e.code !== undefined ? `业务码 ${e.code}：${e.message}` : String(e));
  }
}

/** 选择变化时：把"两侧相同"的情况即时提示出来，避免用户点了才发现。 */
function bindSameGuard() {
  const a = document.getElementById('cmp-a');
  const b = document.getElementById('cmp-b');
  const sync = () => {
    if (a.value && b.value && a.value === b.value) {
      // 只提示，不强行改用户的选择（改选择会让用户困惑"为什么自己变了"）
      const box = document.getElementById('cmp-body');
      if (box && !box.querySelector('.qa-answer')) {
        box.innerHTML = window.UI.notice('当前两个景点相同，请把其中一个换成别的景点。', 'warn');
      }
    }
  };
  a.addEventListener('change', sync);
  b.addEventListener('change', sync);
}

document.addEventListener('DOMContentLoaded', () => {
  loadCompareOptions();
  bindSameGuard();
  document.getElementById('btn-compare').addEventListener('click', doCompare);
  document.getElementById('btn-cmp-reset').addEventListener('click', () => {
    document.getElementById('cmp-a').value = '';
    document.getElementById('cmp-b').value = '';
    document.getElementById('cmp-body').innerHTML = '';
  });
});
