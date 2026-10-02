/* 数据总览页（M1）：规模与评分分布、时间趋势、分档、客源地、数据说明。
   数据全部来自已落库的离线分析结果（Spark 统计），本页不发任何模型调用。 */

/** 评分分布：饼图 + 环形（1–5 星）。 */
function renderScoreChart(dist, total) {
  return {
    title: { text: `评分分布（共 ${fmtInt(total)} 条有评分评论）`, left: 'center', textStyle: { fontSize: 14 } },
    tooltip: {
      trigger: 'item',
      formatter: p => `${p.name}：${fmtInt(p.value)} 条（${p.percent}%）`,
    },
    legend: { bottom: 0 },
    series: [{
      type: 'pie',
      radius: ['40%', '68%'],
      label: { formatter: '{b}\n{d}%' },
      data: dist.map(d => ({ name: `${d.score} 星`, value: d.count })),
    }],
  };
}

/** 来源口径构成：西藏 / 进藏沿线。 */
function renderScopeChart(scope) {
  return {
    title: { text: '景点来源口径构成', left: 'center', textStyle: { fontSize: 14 } },
    tooltip: { trigger: 'axis' },
    legend: { bottom: 0 },
    grid: { left: 70, right: 30, top: 50, bottom: 50 },
    xAxis: { type: 'category', data: scope.map(s => s.label) },
    yAxis: { type: 'value' },
    series: [
      { name: '景点数', type: 'bar', data: scope.map(s => s.spot_count), itemStyle: { color: CHART_COLORS[0] } },
      { name: '评论数', type: 'bar', data: scope.map(s => s.review_count), itemStyle: { color: CHART_COLORS[1] } },
    ],
  };
}

function renderTrendChart(points, granularity) {
  return {
    title: { text: `评论量趋势（按${granularity === 'year' ? '年' : '月'}）`, left: 'center', textStyle: { fontSize: 14 } },
    tooltip: { trigger: 'axis' },
    grid: { left: 70, right: 40, top: 50, bottom: 60 },
    xAxis: { type: 'category', data: points.map(p => p.period), axisLabel: { rotate: 45 } },
    yAxis: { type: 'value', name: '评论量' },
    series: [{
      name: '评论量',
      type: 'line',
      smooth: true,
      areaStyle: { opacity: 0.15 },
      data: points.map(p => p.review_count),
      itemStyle: { color: CHART_COLORS[0] },
    }],
  };
}

function renderBucketChart(buckets) {
  return {
    title: { text: '景点评论量分档', left: 'center', textStyle: { fontSize: 14 } },
    tooltip: { trigger: 'axis' },
    grid: { left: 70, right: 40, top: 50, bottom: 50 },
    xAxis: { type: 'category', data: buckets.map(b => b.bucket) },
    yAxis: { type: 'value', name: '景点数' },
    series: [{
      name: '景点数',
      type: 'bar',
      data: buckets.map(b => b.spot_count),
      itemStyle: { color: CHART_COLORS[5] },
      label: { show: true, position: 'top' },
    }],
  };
}

function renderProvinceChart(points) {
  const sorted = points.slice().sort((a, b) => a.review_count - b.review_count);
  return {
    title: { text: '客源地分布（2022-08 之后样本）', left: 'center', textStyle: { fontSize: 14 } },
    tooltip: {
      trigger: 'axis',
      formatter: p => {
        const item = sorted[p[0].dataIndex];
        return `${item.province}${item.is_overseas ? '（境外）' : ''}<br/>评论量：${fmtInt(item.review_count)}<br/>占比：${fmtPct(item.rate)}`;
      },
    },
    grid: { left: 90, right: 60, top: 50, bottom: 40 },
    xAxis: { type: 'value', name: '评论量' },
    yAxis: { type: 'category', data: sorted.map(p => p.province), axisLabel: { fontSize: 11 } },
    series: [{
      type: 'bar',
      data: sorted.map(p => p.review_count),
      itemStyle: { color: CHART_COLORS[2] },
    }],
  };
}

async function loadSummary() {
  const box = document.getElementById('ov-summary');
  try {
    const data = await apiGet('/api/overview/summary');
    const t = data.totals;
    box.innerHTML = window.UI.statCards([
      { label: '评论总数', value: fmtInt(t.review_count), sub: `去重后 ${fmtInt(t.dedup_review_count)}` },
      { label: '景点总数', value: fmtInt(t.spot_count), sub: `西藏 ${t.spot_tibet} / 进藏沿线 ${t.spot_route}` },
      { label: '具备完整评价资格', value: fmtInt(t.spot_ge100), sub: '评论量 ≥100 条（BR-02）' },
      { label: '低信息量评论', value: fmtInt(t.low_info_count), sub: '正文 ≤10 字（BR-05）' },
      { label: '重复正文', value: fmtInt(t.dup_count), sub: `${fmtInt(t.dup_group_count)} 组（BR-06）` },
      { label: '有图评论', value: fmtInt(t.image_review_count), sub: 'image_count > 0' },
      { label: '归属地未知', value: fmtInt(t.ip_unknown_count), sub: '2022-08 前占多数' },
      { label: '无评分评论', value: fmtInt(t.score_null_count), sub: '评分统计单独口径' },
    ]);
    initChart('chart-score', renderScoreChart(data.score_distribution, data.scored_total));
    // 来源口径构成来自 /api/overview/distribution（summary 不含该字段）
    document.getElementById('ov-summary-note').innerHTML = caliberNote(data.caliber_note, data.sample_size);
  } catch (e) {
    showError('ov-summary', e);
  }
}

async function loadDistribution() {
  try {
    const data = await apiGet('/api/overview/distribution');
    initChart('chart-bucket', renderBucketChart(data.buckets));
    initChart('chart-scope', renderScopeChart(data.source_scope || []));
    const rows = data.buckets.map(b => [b.bucket, fmtInt(b.spot_count), fmtInt(b.review_count)]);
    document.getElementById('ov-bucket-table').innerHTML =
      renderTable(['评论量区间', '景点数', '评论数'], rows) + caliberNote(data.caliber_note, data.sample_size);
  } catch (e) {
    showError('ov-bucket-table', e);
  }
}

async function loadTrend() {
  const granularity = document.getElementById('trend-granularity').value;
  try {
    const data = await apiGet('/api/overview/trend', { granularity });
    if (!data.points.length) {
      document.getElementById('chart-trend').innerHTML = window.UI.empty('该粒度下暂无趋势数据。');
      return;
    }
    initChart('chart-trend', renderTrendChart(data.points, granularity));
    document.getElementById('ov-trend-note').innerHTML = caliberNote(data.caliber_note, data.sample_size);
  } catch (e) {
    showError('ov-trend-note', e);
  }
}

async function loadProvinces() {
  const limit = document.getElementById('prov-limit').value;
  try {
    const data = await apiGet('/api/overview/provinces', { limit });
    if (!data.points.length) {
      document.getElementById('chart-province').innerHTML = window.UI.empty('暂无客源地数据。');
      return;
    }
    initChart('chart-province', renderProvinceChart(data.points));
    document.getElementById('ov-prov-note').innerHTML =
      caliberNote(`${data.caliber_note}（共 ${data.province_count} 个地区）`, data.sample_size);
  } catch (e) {
    showError('ov-prov-note', e);
  }
}

async function loadDataNote() {
  try {
    const data = await apiGet('/api/overview/data-note');
    const s = data.size, q = data.quality;
    const html = `
      <p><strong>数据集：</strong>${escapeHtml(data.dataset)}</p>
      <p><strong>来源：</strong>${escapeHtml(data.source)}</p>
      <p><strong>规模：</strong>评论 ${fmtInt(s.reviews)} 条 · 景点 ${fmtInt(s.spots)} 个
        （西藏 ${fmtInt(s.spots_tibet)} / 进藏沿线 ${fmtInt(s.spots_route)}）</p>
      <p><strong>质量标记：</strong>正文为空 ${fmtInt(q.empty_content)} · 低信息量 ${fmtInt(q.low_info)} ·
        重复正文 ${fmtInt(q.dup_content)}（${fmtInt(q.dup_groups)} 组）· 无评分 ${fmtInt(q.score_null)} ·
        归属地未知 ${fmtInt(q.ip_unknown)}</p>
      <p><strong>已知局限：</strong></p>
      <ul class="limits">${data.limitations.map(x => `<li>${escapeHtml(x)}</li>`).join('')}</ul>
      ${caliberNote(data.caliber_note, data.sample_size)}`;
    document.getElementById('ov-data-note').innerHTML = html;
  } catch (e) {
    showError('ov-data-note', e);
  }
}

document.addEventListener('DOMContentLoaded', () => {
  loadSummary();
  loadDistribution();
  loadTrend();
  loadProvinces();
  loadDataNote();
  document.getElementById('trend-granularity').addEventListener('change', loadTrend);
  document.getElementById('prov-limit').addEventListener('change', loadProvinces);
});
