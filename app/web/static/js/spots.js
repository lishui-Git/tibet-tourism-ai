/* 景点分析页：排行榜、检索、详情、情感/趋势/方面/主题/代表评论。
   全部为只读查询；页面不触发任何批处理或模型调用。

   【文案纪律】情感判定有两种口径，接口用 `deepseek` / `mllib` 标识。
   这些是**内部标识**，因此：
     · 下拉框的 `value` 用中性键（`semantic` / `baseline`），
       渲染出的 HTML 里不出现内部标识；
     · 发请求前由 `METHOD_OF` 映射回接口值（接口契约保持不变）；
     · 展示一律用中文名称。 */

/** 下拉框中性键 → 接口实际取值（映射只发生在 JS 里，不进 HTML）。 */
const METHOD_OF = { semantic: 'deepseek', baseline: 'mllib' };

/** 接口取值 → 用户可读名称。 */
const METHOD_LABELS = {
  deepseek: '深度语义判定',
  mllib: '统计基线判定',
  dict: '词典判定',
};

const methodLabel = m => METHOD_LABELS[m] || m;

let currentPage = 1;
const PAGE_SIZE = 20;

/* ---------------- 一、排行榜 ---------------- */

async function loadRanking() {
  const by = document.getElementById('rank-by').value;
  const limit = document.getElementById('rank-limit').value;
  const box = document.getElementById('rank-table');
  try {
    const data = await apiGet('/api/spots/ranking', { by, limit });
    const valueOf = item => {
      if (by === 'reviews') return fmtInt(item.review_count);
      if (by === 'rating') return fmtNum(item.avg_score, 2);
      return fmtPct(item.positive_rate);
    };
    const rows = data.items.map(item => [
      `<a href="#" class="link" data-spot="${item.spot_id}">${escapeHtml(item.spot_name)}</a>`,
      valueOf(item),
      fmtInt(item.review_count),
      fmtNum(item.avg_score, 2),
      fmtPct(item.positive_rate),
      fmtPct(item.negative_rate),
      item.has_full_evaluation ? '<span class="tag ok">可生成评价</span>' : '<span class="tag">仅基础统计</span>',
    ]);
    box.innerHTML = renderTable(
      ['景点', '排序依据', '评论量', '平均评分', '好评率', '差评率', '评价条件'], rows
    );
    // 口径说明写在模板专门提供的 #rank-note 里（与总览页 ov-*-note 的写法一致）。
    // 此前是拼在表格容器内，导致 #rank-note 永远为空、且结果为空时口径说明会一并消失。
    document.getElementById('rank-note').innerHTML = caliberNote(data.caliber_note, data.sample_size);
    bindSpotLinks(box);
  } catch (e) {
    showError('rank-table', e);
    document.getElementById('rank-note').innerHTML = '';
  }
}

/* ---------------- 二、检索与分页 ---------------- */

async function loadSpots(page) {
  currentPage = page || 1;
  const keyword = document.getElementById('kw').value.trim();
  const box = document.getElementById('spot-table');
  try {
    const data = await apiGet('/api/spots', { keyword, page: currentPage, page_size: PAGE_SIZE });
    if (!data.items.length) {
      box.innerHTML = window.UI.empty('没有匹配的景点。', 'warn');
      document.getElementById('spot-pager').innerHTML = '';
      return;
    }
    const rows = data.items.map(item => [
      `<a href="#" class="link" data-spot="${item.spot_id}">${escapeHtml(item.spot_name)}</a>`,
      escapeHtml(item.source_label),
      fmtInt(item.review_count),
      fmtNum(item.avg_score, 2),
      fmtPct(item.positive_rate),
      item.evaluation_available ? '<span class="tag ok">可生成评价</span>' : '<span class="tag">仅基础统计</span>',
    ]);
    box.innerHTML = renderTable(['景点', '所属范围', '评论量', '平均评分', '好评率', '评价条件'], rows)
      + caliberNote(data.caliber_note, data.sample_size);
    bindSpotLinks(box);
    renderPager(data.total, data.page, data.page_size);
  } catch (e) {
    showError('spot-table', e);
  }
}

function renderPager(total, page, pageSize) {
  const pages = Math.max(1, Math.ceil(total / pageSize));
  const el = document.getElementById('spot-pager');
  el.innerHTML = `
    <button type="button" class="ghost" id="pg-prev" ${page <= 1 ? 'disabled' : ''}>上一页</button>
    <span>第 ${page} / ${pages} 页（共 ${fmtInt(total)} 个景点）</span>
    <button type="button" class="ghost" id="pg-next" ${page >= pages ? 'disabled' : ''}>下一页</button>`;
  const prev = document.getElementById('pg-prev');
  const next = document.getElementById('pg-next');
  if (prev) prev.addEventListener('click', () => loadSpots(page - 1));
  if (next) next.addEventListener('click', () => loadSpots(page + 1));
}

/** 表格内的景点名点击 → 加载详情（不跳转，便于连续查看）。 */
function bindSpotLinks(root) {
  root.querySelectorAll('a[data-spot]').forEach(a => {
    a.addEventListener('click', ev => {
      ev.preventDefault();
      loadDetail(a.getAttribute('data-spot'));
      document.getElementById('detail-card').scrollIntoView({ behavior: 'smooth', block: 'start' });
    });
  });
}

/* ---------------- 三、景点详情 ---------------- */

async function loadDetail(spotId) {
  const card = document.getElementById('detail-card');
  const body = document.getElementById('detail-body');
  card.style.display = '';
  body.innerHTML = '<p class="hint">加载中…</p>';
  try {
    const data = await apiGet(`/api/spots/${spotId}`);
    const s = data.spot;
    document.getElementById('detail-name').textContent = s.spot_name;
    window.UI.setActiveSpot(spotId);

    const stat = data.statistics || {};
    const missing = data.missing_fields || [];
    const field = (label, value) => {
      if (value === null || value === undefined || value === '') {
        return `<tr><th>${label}</th><td class="muted">暂无该项信息</td></tr>`;
      }
      return `<tr><th>${label}</th><td>${escapeHtml(value)}</td></tr>`;
    };
    const hasData = missing.length < 7;   // 有任意一项资料就展示资料表，否则整块隐藏
    body.innerHTML = `
      ${data.evaluation_available ? '' : window.UI.notice(data.availability_note || '', 'warn')}
      ${window.UI.statCards([
        { label: '评论量', value: fmtInt(data.review_count), sub: '条' },
        { label: '平均评分', value: fmtNum(stat.avg_score, 2), sub: '满分 5 分' },
        { label: '好评率', value: fmtPct(stat.positive_rate), sub: '4 分及以上占比' },
        { label: '差评率', value: fmtPct(stat.negative_rate), sub: '2 分及以下占比' },
        { label: '带图评论占比', value: fmtPct(stat.image_rate) },
        { label: '点赞总数', value: fmtInt(stat.total_likes) },
      ])}
      <p class="hint">
        ${data.evaluation_available
          ? '该景点具备生成综合评价的条件，可以前往 <a class="link" href="/smart?tab=evaluation&amp;spot=' + encodeURIComponent(spotId) + '">智能分析</a> 查看它的综合评价。'
          : '该景点评论量较少，只提供基础统计。你可以继续查看下方的情感分布与代表评论。'}
      </p>
      ${hasData ? `
      <h4>景点资料</h4>
      <table class="data-table">
        <tbody>
          ${field('景区地址', s.address)}
          ${field('开放时间', s.open_time)}
          ${field('咨询电话', s.phone)}
          ${field('景点介绍', s.introduction)}
          ${field('信息来源', s.poi_url === null || s.poi_url === undefined || s.poi_url === ''
            ? '' : '该景点页面的公开链接（见下方原始地址）')}
        </tbody>
      </table>` : ''}
      ${s.poi_url ? `<p class="hint">原始页面：<a class="link" href="${escapeHtml(s.poi_url)}" target="_blank" rel="noopener">${escapeHtml(s.poi_url)}</a></p>` : ''}
      ${caliberNote(data.caliber_note, data.sample_size)}`;

    loadSentiment(spotId);
    loadSpotTrend(spotId);
    loadAspects(spotId);
    loadTopics(spotId);
    loadReviews(spotId);
  } catch (e) {
    body.innerHTML = `<div class="alert bad">加载失败：${escapeHtml(window.UI.userMessage(e))}</div>`;
  }
}

async function loadSentiment(spotId) {
  // 下拉框里放的是中性键，这里再映射成接口取值
  const key = document.getElementById('sentiment-method').value;
  const method = METHOD_OF[key] || 'deepseek';
  try {
    const data = await apiGet(`/api/spots/${spotId}/sentiment`, { method });
    const d = data.distribution || {};
    const box = document.getElementById('chart-sentiment');
    if (!data.sample_size) {
      box.innerHTML = window.UI.empty(
        `该景点暂无「${methodLabel(method)}」的情感判定结果。可以切换上方判定口径查看另一种结果。`, 'empty');
      document.getElementById('sentiment-note').innerHTML = caliberNote(data.caliber_note, 0);
      return;
    }
    box.innerHTML = '';
    initChart('chart-sentiment', {
      title: {
        text: `情感分布 · ${methodLabel(method)}（样本 ${fmtInt(data.sample_size)} 条）`,
        left: 'center', textStyle: { fontSize: 14, fontWeight: 600 },
      },
      tooltip: { trigger: 'item', formatter: p => `${p.name}：${fmtInt(p.value)} 条（${p.percent}%）` },
      legend: { bottom: 0 },
      series: [{
        type: 'pie',
        radius: ['42%', '68%'],
        label: { formatter: '{b}\n{d}%' },
        data: [
          { name: '正面', value: d.positive, itemStyle: { color: '#2f7d4f' } },
          { name: '中性', value: d.neutral, itemStyle: { color: '#8a99a8' } },
          { name: '负面', value: d.negative, itemStyle: { color: '#b3352f' } },
        ],
      }],
    });
    const avail = Object.entries(data.available_methods || {})
      .map(([k, v]) => `${methodLabel(k)} ${fmtInt(v)} 条`).join('；') || '无';
    document.getElementById('sentiment-note').innerHTML =
      caliberNote(`${data.caliber_note}　各口径可用条数：${avail}`, data.sample_size);
  } catch (e) {
    showError('sentiment-note', e);
  }
}

async function loadSpotTrend(spotId) {
  try {
    const data = await apiGet(`/api/spots/${spotId}/trend`, { granularity: 'year' });
    if (!data.points.length) {
      document.getElementById('chart-spot-trend').innerHTML = window.UI.empty('该景点暂无可用的年度趋势数据。', 'warn');
      return;
    }
    document.getElementById('chart-spot-trend').innerHTML = '';
    initChart('chart-spot-trend', {
      tooltip: { trigger: 'axis' },
      grid: { left: 70, right: 40, top: 30, bottom: 40 },
      xAxis: { type: 'category', data: data.points.map(p => p.period) },
      yAxis: { type: 'value', name: '评论量' },
      series: [{
        type: 'bar', data: data.points.map(p => p.review_count),
        itemStyle: { color: CHART_COLORS[0] }, label: { show: true, position: 'top' },
      }],
    });
  } catch (e) {
    showError('chart-spot-trend', e);
  }
}

async function loadAspects(spotId) {
  try {
    const data = await apiGet(`/api/spots/${spotId}/aspects`, { method: 'deepseek' });
    const box = document.getElementById('aspect-table');
    if (!data.items.length) {
      box.innerHTML = window.UI.empty(
        '该景点暂无方面级分析结果。方面分析需要逐条评论的语义结果，'
        + '在离线分析覆盖到该景点之前，请先参考上方的情感分布与代表评论。', 'empty');
      document.getElementById('aspect-note').innerHTML = caliberNote(data.caliber_note, data.sample_size);
      return;
    }
    const rows = data.items.map(a => [
      escapeHtml(a.aspect),
      fmtInt(a.sample_size),
      a.conclusive ? fmtPct(a.positive_rate) : '<span class="muted">样本不足</span>',
      a.conclusive ? fmtPct(a.negative_rate) : '<span class="muted">样本不足</span>',
      a.conclusive ? '<span class="tag ok">可结论</span>' : '<span class="tag warn">样本不足，不出结论</span>',
    ]);
    box.innerHTML = renderTable(['方面', '样本量', '正面占比', '负面占比', '结论'], rows);
    document.getElementById('aspect-note').innerHTML =
      caliberNote(`${data.caliber_note}　本次可下结论的方面：${data.conclusive_count} / ${data.items.length}`, data.sample_size);
  } catch (e) {
    showError('aspect-table', e);
  }
}

async function loadTopics(spotId) {
  try {
    const data = await apiGet(`/api/spots/${spotId}/topics`);
    const box = document.getElementById('topic-body');
    if (!data.topics || !data.topics.length) {
      box.innerHTML = window.UI.empty('暂无主题结果。', 'warn');
      return;
    }
    const cards = data.topics.map(t => `
      <div class="topic">
        <div class="topic-head">主题 ${t.topic_index + 1}
          <span class="muted">占全部主题的 ${fmtPct(t.topic_rate)} · 归纳自 ${fmtInt(t.sample_size)} 条评论</span>
        </div>
        <div class="topic-words">${(t.words || []).map(w => `<span class="kw">${escapeHtml(w.word)}</span>`).join('')}</div>
      </div>`).join('');
    box.innerHTML = `<p class="hint">主题范围：${data.scope === 'spot'
      ? '仅根据本景点的评论归纳'
      : '根据全部景点评论归纳（本景点评论量较少，未单独建模）'}</p>`
      + cards + caliberNote(data.caliber_note, data.sample_size);
  } catch (e) {
    showError('topic-body', e);
  }
}

async function loadReviews(spotId) {
  try {
    const data = await apiGet(`/api/spots/${spotId}/reviews`, { limit: 5 });
    const render = (list, cls, emptyText) => list.length
      ? list.map(r => `
          <div class="review ${cls}">
            <div class="review-meta">
              ${r.score ? `${r.score} 星` : '未评分'} · 点赞 ${fmtInt(r.like_count)} · ${escapeHtml(r.publish_date || '')}
              ${r.is_dup_content ? '<span class="tag warn">内容重复</span>' : ''}
            </div>
            <div class="review-text">${escapeHtml(r.content)}</div>
          </div>`).join('')
      : `<div class="empty-state">${escapeHtml(emptyText)}</div>`;
    document.getElementById('review-body').innerHTML = `
      <h4>好评代表（评分 ≥4 星）</h4>
      ${render(data.positive, 'pos', '该景点暂无符合条件的正面评论。')}
      <h4>差评代表（评分 ≤2 星）</h4>
      ${render(data.negative, 'neg', '该景点暂无符合条件的负面评论——没有就不列出，不会补充示例。')}
      ${caliberNote(`${data.note || ''}　${data.caliber_note || ''}`, data.sample_size)}`;
  } catch (e) {
    showError('review-body', e);
  }
}

/* ---------------- 事件绑定 ---------------- */

document.addEventListener('DOMContentLoaded', () => {
  loadRanking();
  loadSpots(1);

  document.getElementById('rank-by').addEventListener('change', loadRanking);
  document.getElementById('rank-limit').addEventListener('change', loadRanking);
  document.getElementById('btn-search').addEventListener('click', () => loadSpots(1));
  document.getElementById('btn-reset').addEventListener('click', () => {
    document.getElementById('kw').value = '';
    loadSpots(1);
  });
  document.getElementById('kw').addEventListener('keydown', ev => {
    if (ev.key === 'Enter') loadSpots(1);
  });
  document.getElementById('sentiment-method').addEventListener('change', () => {
    const id = window.UI.getActiveSpot();
    if (id) loadSentiment(id);
  });
});
