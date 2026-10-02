/* 任务与口径页（M6）：数据口径配置 + 批处理任务与日志（只读，需管理员登录）。 */

/** 显示当前登录用户，并绑定注销按钮。 */
async function loadWhoami() {
  const box = document.getElementById('whoami');
  try {
    const me = await apiGet('/api/auth/me');
    box.innerHTML = `已登录：<strong>${escapeHtml(me.nickname || me.username)}</strong>
      （用户名 ${escapeHtml(me.username)}，角色 <span class="tag ${me.is_admin ? 'ok' : ''}">${escapeHtml(me.role)}</span>）`;
  } catch (e) {
    box.innerHTML = `<div class="alert warn">未登录或会话已失效（${escapeHtml(e.message)}）。</div>`;
  }
}

async function doLogout() {
  try {
    await axios.post('/api/auth/logout');
  } catch (e) {
    // 即使注销请求失败也回到登录页，避免停在"看起来还登录着"的状态
  }
  window.location.href = '/login';
}

function renderCaliber(data) {
  const items = data.calibers.map(c => {
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
      { label: '任务总数', value: fmtInt(data.total) },
      { label: '本页条数', value: fmtInt(data.items.length) },
    ]) + renderTable(['类型', '状态', '数量'],
      (data.summary || []).map(s => [escapeHtml(s.task_type), statusTag(s.status), fmtInt(s.n)]));

    if (!data.items.length) {
      box.innerHTML = window.UI.empty('没有符合条件的任务。', 'warn');
      return;
    }
    const rows = data.items.map(t => [
      t.task_id,
      `<code>${escapeHtml(t.task_type)}</code>`,
      escapeHtml(t.task_name || ''),
      statusTag(t.status),
      `${fmtInt(t.success_count)} / ${t.total_count === null ? '—' : fmtInt(t.total_count)}`,
      fmtInt(t.fail_count),
      fmtInt(t.skip_count),
      t.cost_seconds === null ? '—' : `${fmtInt(t.cost_seconds)}s`,
      escapeHtml(t.created_at || ''),
      `<button type="button" class="ghost small" data-task="${t.task_id}">日志</button>`,
    ]);
    const html = renderTable(
      ['ID', '类型', '任务名', '状态', '成功/计划', '失败', '跳过', '耗时', '创建时间', '操作'], rows
    ) + caliberNote(data.caliber_note, data.sample_size);
    box.innerHTML = html;
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
    const t = data.task;
    const rows = data.logs.map(log => [
      log.log_id,
      log.level === 'ERROR' ? '<span class="tag bad">ERROR</span>'
        : log.level === 'WARN' ? '<span class="tag warn">WARN</span>' : '<span class="tag">INFO</span>',
      escapeHtml(log.stage || ''),
      escapeHtml(log.message || ''),
      escapeHtml(log.ref_key || '—'),
      log.processed_count === null ? '—' : fmtInt(log.processed_count),
      escapeHtml(log.created_at || ''),
    ]);
    box.innerHTML = `
      <h4>任务 ${t.task_id} · ${escapeHtml(t.task_name || '')} ${statusTag(t.status)}</h4>
      <p class="hint">共 ${fmtInt(data.log_count)} 条日志，其中 ERROR ${fmtInt(data.error_count)} 条。</p>
      ${renderTable(['ID', '级别', '阶段', '内容', '关联标识', '处理量', '时间'], rows)}
      ${caliberNote(data.caliber_note, data.sample_size)}`;
  } catch (e) {
    box.innerHTML = `<div class="alert bad">加载失败：${escapeHtml(e.message)}</div>`;
  }
}

document.addEventListener('DOMContentLoaded', () => {
  loadWhoami();
  loadCaliber();
  loadTasks();
  document.getElementById('btn-logout').addEventListener('click', doLogout);
  document.getElementById('btn-tasks').addEventListener('click', loadTasks);
  document.getElementById('task-type').addEventListener('change', loadTasks);
});
