/* 首页：真实数据概况 + 热门景点 + 结果生成情况。
   只调用 /api/overview/summary 与 /api/spots/ranking —— 两者都只读数据库，不触发任何模型调用。

   【文案纪律】本页是普通用户第一眼看到的内容，因此：
     · 不出现 mllib / deepseek / stat_* / spot_report 等内部名称；
     · 不出现 API 成本、调用次数、token 等实现细节（那些属于管理员页与答辩材料）；
     · "尚未生成"必须如实说明，绝不用估算值或示例内容填充。 */

/** 内部方法名 → 用户能看懂的说法（仅在**展示层**翻译，接口契约保持不变）。 */
const SENTIMENT_METHOD_LABELS = {
  deepseek: '深度语义判定',
  mllib: '统计基线判定',
  dict: '词典判定',
};

async function loadHome() {
  try {
    const data = await apiGet('/api/overview/summary');
    const t = data.totals || {};
    const cov = data.coverage || {};

    // ① 概况：只放普通用户关心的四个数字
    document.getElementById('home-stats').innerHTML = window.UI.statCards([
      { label: '收录评论', value: fmtInt(t.review_count), sub: '条真实游客评论' },
      { label: '覆盖景点', value: fmtInt(t.spot_count), sub: '个景点' },
      { label: '可生成完整评价', value: fmtInt(t.spot_ge100), sub: '评论量达到 100 条的景点' },
      { label: '带图评论', value: fmtInt(t.image_review_count), sub: '含图片的游客评论' },
    ]);

    const note = document.getElementById('home-summary-note');
    if (note) note.textContent = data.caliber_note || '';

    // ② 口径说明（折叠区里那段"加载中…"）
    const cal = document.getElementById('home-caliber');
    if (cal) cal.textContent = data.caliber_note || '暂无口径说明。';

    // ③ 分析结果生成情况：如实展示，语气面向用户
    const rep = cov.report || {};
    const sentList = cov.sentiment || [];
    const sentText = sentList.length
      ? sentList.map(s => `${SENTIMENT_METHOD_LABELS[s.method] || '情感判定'} ${fmtInt(s.n)} 条`).join('　·　')
      : '暂无';
    const reportsDone = fmtInt(rep.reports_generated);
    const reportsNeed = fmtInt(rep.spots_ge100);
    const allReportsReady = Number(rep.reports_generated || 0) >= Number(rep.spots_ge100 || 0)
      && Number(rep.spots_ge100 || 0) > 0;

    document.getElementById('home-coverage').innerHTML = renderTable(
      ['分析内容', '状态', '说明'],
      [
        ['评论统计（评分、趋势、来源地区）',
         '<span class="tag ok">已生成</span>',
         `覆盖全部 ${fmtInt(t.review_count)} 条评论`],
        ['评论情感判定',
         '<span class="tag ok">已生成</span>',
         sentText],
        ['游客关注主题',
         '<span class="tag ok">已生成</span>',
         `${fmtInt((cov.topic || {}).topic_count)} 个主题 / ${fmtInt((cov.topic || {}).topic_word_count)} 个主题词`],
        ['景点综合智能评价',
         allReportsReady ? '<span class="tag ok">已生成</span>' : '<span class="tag warn">暂未生成</span>',
         allReportsReady
           ? `已生成 ${reportsDone} 个景点的评价`
           : `已具备生成条件的景点 ${reportsNeed} 个，当前已生成 ${reportsDone} 个；`
             + '在生成完成前，「智能分析」会如实显示"暂未生成"并继续提供基础统计'],
      ]
    );

    if (!allReportsReady) {
      document.getElementById('home-coverage').insertAdjacentHTML('beforeend',
        '<div class="alert">景点综合智能评价需要较长的离线分析时间，目前尚未生成。'
        + '在此之前，你仍然可以查看每个景点的评论统计、情感分布与代表性评论。</div>');
    }
  } catch (e) {
    showError('home-stats', e);
  }
}

/** 热门景点：评论量前 8，点击进入该景点的智能分析。 */
async function loadHotSpots() {
  const box = document.getElementById('home-hot');
  try {
    const data = await apiGet('/api/spots/ranking', { by: 'reviews', limit: 8 });
    const items = data.items || [];
    if (!items.length) {
      box.innerHTML = window.UI.empty('当前还没有景点数据。', 'empty');
      return;
    }
    box.innerHTML = items.map((it, i) => `
      <a class="rank-mini-item" href="/smart?tab=evaluation&spot=${encodeURIComponent(it.spot_id)}"
         title="查看「${escapeHtml(it.spot_name)}」的智能分析">
        <span class="no">${i + 1}</span>
        <span class="nm">${escapeHtml(it.spot_name)}</span>
        <span class="v">${fmtInt(it.review_count)} 条评论</span>
        <span class="v sub2">评分 ${fmtNum(it.avg_score, 2)}</span>
      </a>`).join('');
  } catch (e) {
    box.innerHTML = window.UI.notice(`暂时无法加载热门景点：${window.UI.userMessage(e)}`, 'warn',
      e && e.code !== undefined ? `业务码 ${e.code}：${e.message}` : String(e));
  }
}

document.addEventListener('DOMContentLoaded', () => {
  loadHome();
  loadHotSpots();
});
