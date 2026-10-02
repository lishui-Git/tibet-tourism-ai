/* 景点分析页（M2）：排行榜、检索、详情、情感/趋势/方面/主题/代表评论。
   全部为只读查询；页面不触发任何批处理或模型调用。 */

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
      item.has_full_evaluation ? '<span class="tag ok">可评价</span>' : '<span class="tag">仅统计</span>',
    ]);
    box.innerHTML = renderTable(
      ['景点', '排序指标', '评论量', '平均评分', '好评率', '差评率', '评价资格'], rows
    ) + caliberNote(data.caliber_note, data.sample_size);
    bindSpotLinks(box);
  } catch (e) {
    showError('rank-table', e);
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
      item.evaluation_available ? '<span class="tag ok">可评价</span>' : '<span class="tag">仅统计</span>',
    ]);
    box.innerHTML = renderTable(['景点', '来源口径', '评论量', '平均评分', '好评率', '评价资格'], rows)
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
        return `<tr><th>${label}</th><td class="muted">该景点无此字段（不展示，不填充占位）</td></tr>`;
      }
      return `<tr><th>${label}</th><td>${escapeHtml(value)}</td></tr>`;
    };
    body.innerHTML = `
      ${data.evaluation_available ? '' : `<div class="alert warn">${escapeHtml(data.availability_note || '')}</div>`}
      ${window.UI.statCards([
        { label: '评论量', value: fmtInt(data.review_count) },
        { label: '平均评分', value: fmtNum(stat.avg_score, 2) },
        { label: '好评率', value: fmtPct(stat.positive_rate) },
        { label: '差评率', value: fmtPct(stat.negative_rate) },
        { label: '图文率', value: fmtPct(stat.image_rate) },
        { label: '点赞总数', value: fmtInt(stat.total_likes) },
      ])}
      <table class="data-table">
        <tbody>
          ${field('景点编号', s.spot_id)}
          ${field('来源口径', s.source_scope === 'tibet' ? '西藏' : '进藏沿线')}
          ${field('地址', s.address)}
          ${field('开放时间', s.open_time)}
          ${field('官方电话', s.phone)}
          ${field('景点介绍', s.introduction)}
          ${field('携程页面', s.poi_url)}
          <tr><th>缺失字段</th><td>${missing.length ? escapeHtml(missing.join('、')) : '无'}</td></tr>
        </tbody>
      </table>
      ${caliberNote(data.caliber_note, data.sample_size)}`;

    loadSentiment(spotId);
    loadSpotTrend(spotId);
    loadAspects(spotId);
    loadTopics(spotId);
    loadReviews(spotId);
  } catch (e) {
    body.innerHTML = `<div class="alert bad">加载失败：${escapeHtml(e.message)}</div>`;
  }
}

async function loadSentiment(spotId) {
  const method = document.getElementById('sentiment-method').value;
  try {
    const data = await apiGet(`/api/spots/${spotId}/sentiment`, { method });
    const d = data.distribution;
    const box = document.getElementById('chart-sentiment');
    if (!data.sample_size) {
      box.innerHTML = window.UI.empty(`该方法（${method}）下暂无情感结果。`, 'warn');
      document.getElementById('sentiment-note').innerHTML = caliberNote(data.caliber_note, 0);
      return;
    }
    box.innerHTML = '';
    initChart('chart-sentiment', {
      title: { text: `情感分布（${method}，样本 ${fmtInt(data.sample_size)} 条）`, left: 'center', textStyle: { fontSize: 14 } },
      tooltip: { trigger: 'item', formatter: p => `${p.name}：${fmtInt(p.value)} 条（${p.percent}%）` },
      legend: { bottom: 0 },
      series: [{
        type: 'pie',
        radius: ['40%', '68%'],
        label: { formatter: '{b}\n{d}%' },
        data: [
          { name: '正面', value: d.positive, itemStyle: { color: CHART_COLORS[1] } },
          { name: '中性', value: d.neutral, itemStyle: { color: CHART_COLORS[4] } },
          { name: '负面', value: d.negative, itemStyle: { color: CHART_COLORS[2] } },
        ],
      }],
    });
    const avail = Object.entries(data.available_methods || {})
      .map(([k, v]) => `${k}=${fmtInt(v)}`).join('，') || '无';
    document.getElementById('sentiment-note').innerHTML =
      caliberNote(`${data.caliber_note}　各方法可用条数：${avail}`, data.sample_size);
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
        '该景点暂无方面级结果（方面级结果只有 deepseek 来源；离线语义分析尚未覆盖到此景点）。', 'warn');
      document.getElementById('aspect-note').innerHTML = caliberNote(data.caliber_note, data.sample_size);
      return;
    }
    const rows = data.items.map(a => [
      escapeHtml(a.aspect),
      fmtInt(a.sample_size),
      a.conclusive ? fmtPct(a.positive_rate) : '<span class="muted">不出结论</span>',
      a.conclusive ? fmtPct(a.negative_rate) : '<span class="muted">不出结论</span>',
      a.conclusive ? '<span class="tag ok">可结论</span>' : `<span class="tag warn">${escapeHtml(a.note || '样本不足')}</span>`,
    ]);
    box.innerHTML = renderTable(['方面', '样本量', '正面占比', '负面占比', '门槛判断'], rows);
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
          <span class="muted">占比 ${fmtPct(t.topic_rate)} · 训练样本 ${fmtInt(t.sample_size)}</span>
        </div>
        <div class="topic-words">${(t.words || []).map(w => `<span class="kw">${escapeHtml(w.word)}</span>`).join('')}</div>
      </div>`).join('');
    box.innerHTML = `<p class="hint">主题范围：${data.scope === 'spot' ? '本景点专属模型' : '全局模型（该景点无专属主题）'}</p>`
      + cards + caliberNote(data.caliber_note, data.sample_size);
  } catch (e) {
    showError('topic-body', e);
  }
}

async function loadReviews(spotId) {
  try {
    const data = await apiGet(`/api/spots/${spotId}/reviews`, { limit: 5 });
    const render = (list, cls) => list.length
      ? list.map(r => `
          <div class="review ${cls}">
            <div class="review-meta">
              ${r.score ? `${r.score} 星` : '无评分'} · 点赞 ${fmtInt(r.like_count)} · ${escapeHtml(r.publish_date || '')}
              ${r.is_dup_content ? '<span class="tag warn">重复正文</span>' : ''}
            </div>
            <div class="review-text">${escapeHtml(r.content)}</div>
          </div>`).join('')
      : '<div class="alert warn">该景点没有符合条件的评论（负面评论不足时如实留少，不补造）。</div>';
    document.getElementById('review-body').innerHTML = `
      <h4>正面代表评论（评分 ≥4）</h4>${render(data.positive, 'pos')}
      <h4>负面代表评论（评分 ≤2）</h4>${render(data.negative, 'neg')}
      ${caliberNote(`${data.note}　${data.caliber_note}`, data.sample_size)}`;
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
