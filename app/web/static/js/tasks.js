/* 管理后台 → 数据口径 + 批处理任务与日志（只读，需管理员登录）。

   【定位】这一页**面向管理员/答辩**，因此允许出现内部术语（任务类型码、阶段名、
   日志级别），但会同时给出中文说明，避免"只有代码没有人话"。
   普通用户看不到本页。 */

/** 任务类型码 → 中文说明（管理员页保留码本身，便于与开发文档对照）。 */
const TASK_TYPE_LABELS = {
  clean: '数据导入与清洗',
  stat: '统计聚合',
  mllib: '情感基线',
  lda: '主题分析',
  semantic: '评论语义分析',
  fact_package: '景点事实汇总',
  spot_report: '景点评价生成',
};

function taskTypeCell(code) {
  const label = TASK_TYPE_LABELS[code];
  return label
    ? `<span title="${escapeHtml(code)}">${escapeHtml(label)}</span>`
    : `<code>${escapeHtml(code || '')}</code>`;
}

/** 日志阶段码 → 中文说明。 */
const STAGE_LABELS = {
  clean: '清洗',
  stat: '统计',
  mllib: '情感基线',
  lda: '主题',
  semantic: '语义分析',
  fact_package: '事实汇总',
  spot_report: '评价生成',
};

function renderCaliber(data) {
  const items = (data.calibers || []).map(c => {
    let value = c.value;
    if (Array.isArray(value)) value = value.join(' / ');
    else if (value === null || value === undefined) value = '—';
    return `
      <div class="caliber-item">
        <div class="caliber-key">${escapeHtml(c.key)} = <code>${escapeHtml(value)}</code></div>
        <div class="caliber-desc">${escapeHtml(c.note || '')}</div>
      </div>`;
  }).join('');
  return items + caliberNote(data.caliber_note, data.sample_size);
}

async function loadCaliber() {
  try {
    const data = await apiGet('/api/admin/caliber');
    document.getElementById('caliber-body').innerHTML = renderCaliber(data);
  } catch (e) {
    showError('caliber-body', e);
  }
}

function statusTag(status) {
  const map = {
    success: '<span class="tag ok">成功</span>',
    partial: '<span class="tag warn">部分成功</span>',
    failed: '<span class="tag bad">失败</span>',
    running: '<span class="tag">运行中</span>',
    pending: '<span class="tag">等待</span>',
  };
  return map[status] || `<span class="tag">${escapeHtml(status || '')}</span>`;
}

async function loadTasks() {
  const taskType = document.getElementById('task-type').value;
  const box = document.getElementById('task-table');
  try {
    const data = await apiGet('/api/admin/tasks', { limit: 30, task_type: taskType || undefined });
    document.getElementById('task-summary').innerHTML = window.UI.statCards([
      { label: '任务总数', value: fmtInt(data.total), sub: '条批处理任务记录' },
      { label: '本页条数', value: fmtInt((data.items || []).length) },
    ]) + renderTable(['类型', '状态', '数量'],
      (data.summary || []).map(s => [taskTypeCell(s.task_type), statusTag(s.status), fmtInt(s.n)]));

    if (!data.items || !data.items.length) {
      box.innerHTML = window.UI.empty('没有符合条件的任务记录。', 'empty');
      return;
    }
    const rows = data.items.map(t => [
      t.task_id,
      taskTypeCell(t.task_type),
      escapeHtml(t.task_name || ''),
      statusTag(t.status),
      `${fmtInt(t.success_count)} / ${t.total_count === null ? '—' : fmtInt(t.total_count)}`,
      fmtInt(t.fail_count),
      fmtInt(t.skip_count),
      t.cost_seconds === null || t.cost_seconds === undefined ? '—' : `${fmtInt(t.cost_seconds)}s`,
      escapeHtml(t.created_at || ''),
      `<button type="button" class="ghost small" data-task="${t.task_id}">日志</button>`,
    ]);
    box.innerHTML = renderTable(
      ['ID', '类型', '任务名', '状态', '成功/计划', '失败', '跳过', '耗时', '创建时间', '操作'], rows
    ) + caliberNote(data.caliber_note, data.sample_size);
    box.querySelectorAll('button[data-task]').forEach(btn => {
      btn.addEventListener('click', () => loadTaskLogs(btn.getAttribute('data-task')));
    });
  } catch (e) {
    showError('task-table', e);
  }
}

async function loadTaskLogs(taskId) {
  const box = document.getElementById('task-logs');
  box.innerHTML = '<p class="hint">加载中…</p>';
  try {
    const data = await apiGet(`/api/admin/tasks/${taskId}/logs`);
    const t = data.task || {};
    const rows = (data.logs || []).map(log => [
      log.log_id,
      log.level === 'ERROR' ? '<span class="tag bad">错误</span>'
        : log.level === 'WARN' ? '<span class="tag warn">警告</span>' : '<span class="tag">信息</span>',
      escapeHtml(STAGE_LABELS[log.stage] || log.stage || ''),
      escapeHtml(log.message || ''),
      escapeHtml(log.ref_key || '—'),
      log.processed_count === null || log.processed_count === undefined ? '—' : fmtInt(log.processed_count),
      escapeHtml(log.created_at || ''),
    ]);
    box.innerHTML = `
      <h4>任务 ${t.task_id} · ${escapeHtml(t.task_name || '')} ${statusTag(t.status)}</h4>
      <p class="hint">共 ${fmtInt(data.log_count)} 条日志，其中错误 ${fmtInt(data.error_count)} 条。</p>
      ${renderTable(['ID', '级别', '阶段', '内容', '关联标识', '处理量', '时间'], rows)}
      ${caliberNote(data.caliber_note, data.sample_size)}`;
    // 日志面板在页面底部：点"日志"后自动滚过去，否则演示时看不到反应
    box.scrollIntoView({ behavior: 'smooth', block: 'start' });
  } catch (e) {
    box.innerHTML = window.UI.notice(
      `加载任务日志失败：${window.UI.userMessage(e)}`, 'bad',
      e && e.code !== undefined ? `业务码 ${e.code}：${e.message}` : String(e));
    box.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }
}

document.addEventListener('DOMContentLoaded', () => {
  loadCaliber();
  loadTasks();
  const btnTasks = document.getElementById('btn-tasks');
  if (btnTasks) btnTasks.addEventListener('click', loadTasks);
  const selType = document.getElementById('task-type');
  if (selType) selType.addEventListener('change', loadTasks);
});

