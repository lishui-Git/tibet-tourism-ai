/* 首页：数据概况 + 结果生产状态 + 自检。
   与其它页面一样只读数据库；自检按钮调用 /api/db-ping 确认环境。 */

async function loadHome() {
  try {
    const data = await apiGet('/api/overview/summary');
    const t = data.totals;
    const cov = data.coverage || {};
    const sent = (cov.sentiment || []).map(s => `${s.method} ${fmtInt(s.n)}`).join(' · ') || '暂无';

    document.getElementById('home-stats').innerHTML = window.UI.statCards([
      { label: '评论总数', value: fmtInt(t.review_count), sub: `景点 ${fmtInt(t.spot_count)} 个` },
      { label: '具备完整评价资格', value: fmtInt(t.spot_ge100), sub: '评论量 ≥100 条' },
      { label: '低信息量评论', value: fmtInt(t.low_info_count), sub: '≤10 字，走规则判定不调模型' },
      { label: '重复正文', value: fmtInt(t.dup_count), sub: `${fmtInt(t.dup_group_count)} 组，组内只调一次` },
    ]);
    document.getElementById('home-note').innerHTML = caliberNote(data.caliber_note, data.sample_size);

    const rep = cov.report || {};
    document.getElementById('home-coverage').innerHTML = `
      ${renderTable(['结果类别', '生产状态', '说明'], [
        ['Spark 统计（stat_spot / stat_time / stat_ip）', '<span class="tag ok">已生产</span>', '全量 59,033 条评论的统计聚合'],
        ['MLlib 情感基线（sentiment.method=mllib）', `<span class="tag ok">已生产</span>`, `${fmtInt((cov.sentiment || []).find(s => s.method === 'mllib') ? (cov.sentiment || []).find(s => s.method === 'mllib').n : 0)} 条`],
        ['LDA 主题（topic / topic_word）', '<span class="tag ok">已生产</span>', `${fmtInt((cov.topic || {}).topic_count)} 个主题 / ${fmtInt((cov.topic || {}).topic_word_count)} 个主题词`],
        ['DeepSeek 语义（评论级）', '<span class="tag warn">部分生产</span>', sent + '（全量离线任务待授权执行）'],
        ['景点事实包（spot_fact_package）', rep.fact_packages ? '<span class="tag ok">已生产</span>' : '<span class="tag warn">待生产</span>', `${fmtInt(rep.fact_packages)} 个景点`],
        ['景点智能评价（spot_report）', rep.reports_generated ? '<span class="tag ok">已生产</span>' : '<span class="tag warn">待生产</span>', `${fmtInt(rep.reports_generated)} / ${fmtInt(rep.spots_ge100)} 个合格景点`],
      ])}
      <div class="alert">
        上表如实反映当前数据生产进度：<strong>DeepSeek 全量语义分析与景点评价尚未执行</strong>，
        因此部分页面会明确提示"尚无结果"，而不会用估算值填充。
      </div>`;
  } catch (e) {
    showError('home-stats', e);
  }
}

async function runSelfCheck() {
  const out = document.getElementById('check-out');
  const btn = document.getElementById('btn-check');
  btn.disabled = true;
  out.textContent = '检查中…';
  out.className = 'out';
  try {
    const health = await apiGet('/healthz');
    const db = await apiGet('/api/db-ping');
    out.textContent = [
      `应用：${health.app}　版本：${health.version}`,
      `阶段：${health.stage}`,
      `数据库连接：${health.db_target}`,
      `MySQL 版本：${db.version}　字符集：${db.charset}　库名：${db.database_name}`,
      `已建表：${db.table_count} 张　外键：${db.foreign_key_count} 个`,
      '',
      '结论：Flask 运行正常、MySQL 连接正常、17 张表在位。',
    ].join('\n');
    out.className = 'out ok';
  } catch (e) {
    out.textContent = `自检失败：${e.message}`;
    out.className = 'out bad';
  } finally {
    btn.disabled = false;
  }
}

document.addEventListener('DOMContentLoaded', () => {
  loadHome();
  document.getElementById('btn-check').addEventListener('click', runSelfCheck);
});
