/* 我的问答：只读取 /api/qa/history，服务端按当前会话 user_id 过滤。
   本页不调用 /api/qa/ask，因此打开/翻页不会产生任何模型调用。 */

const MYQA_TYPE_LABELS = {
  SPOT_EVALUATION: '景点评价咨询',
  SPOT_COMPARISON: '景点差异对比',
  VISITOR_FOCUS: '游客关注点',
  SENTIMENT_EXPLAIN: '情感与方面解释',
  DATA_METRIC: '数据指标查询',
  RANKING: '排行相关',
  OUT_OF_SCOPE: '超出可回答范围',
};

const MYQA_PAGE_SIZE = 10;
let myqaPage = 1;

function myqaTypeLabel(type) {
  return MYQA_TYPE_LABELS[type] || (type || '问答');
}

function myqaSpotCount(spotIds) {
  return String(spotIds || '').split(',').map(s => s.trim()).filter(Boolean).length;
}

function renderMyQaItem(item) {
  const answer = String(item.answer || '').trim();
  const shortAnswer = answer.length > 180 ? answer.slice(0, 180) + '…' : answer;
  const answerHtml = answer
    ? `<p class="qa-history-answer">${escapeHtml(shortAnswer)}</p>
       ${answer.length > 180 ? `<details class="fold"><summary>查看完整回答</summary>
         <div class="fold-body">${escapeHtml(answer)}</div></details>` : ''}`
    : '<p class="qa-history-answer muted">该次提问未生成自然语言回答，数据依据已保存。</p>';

  const spotCount = myqaSpotCount(item.spot_ids);
  const meta = [
    myqaTypeLabel(item.question_type),
    item.created_at ? `提问时间 ${escapeHtml(String(item.created_at))}` : '',
    spotCount ? `涉及 ${spotCount} 个景点` : '',
    item.need_review ? '<span class="tag warn">系统已核对</span>' : '',
  ].filter(Boolean).join('　·　');

  return `
    <article class="qa-history-item">
      <p class="qa-history-q">${escapeHtml(item.question || '（问题内容为空）')}</p>
      <div class="qa-history-meta">${meta}</div>
      ${answerHtml}
    </article>`;
}

function renderMyQaPager(total, page, pageSize) {
  const box = document.getElementById('myqa-pager');
  if (!box) return;
  const pages = Math.max(1, Math.ceil(Number(total || 0) / pageSize));
  if (pages <= 1) {
    box.innerHTML = '';
    return;
  }
  box.innerHTML = `
    <button type="button" class="ghost" data-page="${page - 1}" ${page <= 1 ? 'disabled' : ''}>上一页</button>
    <span>第 ${page} / ${pages} 页　共 ${fmtInt(total)} 条</span>
    <button type="button" class="ghost" data-page="${page + 1}" ${page >= pages ? 'disabled' : ''}>下一页</button>`;
  box.querySelectorAll('button[data-page]').forEach(btn => {
    btn.addEventListener('click', () => {
      const target = Number(btn.getAttribute('data-page'));
      if (target >= 1 && target <= pages) loadMyQa(target);
    });
  });
}

async function loadMyQa(page) {
  const box = document.getElementById('myqa-list');
  if (!box) return;
  myqaPage = page || 1;
  box.innerHTML = window.UI.loading('正在加载你的提问记录…');

  try {
    const data = await apiGet('/api/qa/history', {
      page: myqaPage,
      page_size: MYQA_PAGE_SIZE,
    });
    const items = data.items || [];
    if (!items.length) {
      box.innerHTML = `<div class="empty-state">
        你还没有提问记录。去 <a class="link" href="/smart?tab=qa">智能问答</a> 提一个问题吧。</div>`;
      renderMyQaPager(0, 1, MYQA_PAGE_SIZE);
      return;
    }
    box.innerHTML = `<div class="myqa-list">${items.map(renderMyQaItem).join('')}</div>`;
    renderMyQaPager(data.total, data.page || 1, data.page_size || MYQA_PAGE_SIZE);
  } catch (e) {
    const code = e && e.code !== undefined
      ? e.code
      : (e && e.response && e.response.data && e.response.data.code);
    if (code === 2001) {
      window.location.href = '/login?next=/my-qa';
      return;
    }
    box.innerHTML = window.UI.notice(
      `加载提问记录失败：${window.UI.userMessage(e)}`, 'bad',
      e && e.code !== undefined ? `业务码 ${e.code}：${e.message}` : String(e));
  }
}

document.addEventListener('DOMContentLoaded', () => {
  loadMyQa(1);
});
