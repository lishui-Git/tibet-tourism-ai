/* 管理后台公共部分：登录信息 + 退出 + 系统自检。
   自检原先放在首页（普通用户看到"MySQL 版本／17 张表"这类信息并不合适），
   改版后统一收到管理后台。 */

/** 显示当前登录的管理员，并绑定退出按钮。 */
async function loadWhoami() {
  const box = document.getElementById('whoami');
  if (!box) return;
  try {
    const me = await apiGet('/api/auth/me');
    box.innerHTML = `已登录：<strong>${escapeHtml(me.nickname || me.username)}</strong>
      <span class="muted">（用户名 ${escapeHtml(me.username)}，
      角色 ${me.is_admin ? '管理员' : escapeHtml(me.role || '')}）</span>`;
  } catch (e) {
    // 会话失效：页面本身由服务端把关，这里只需提示并引导重新登录
    box.innerHTML = window.UI.notice('登录状态已失效，请重新登录。', 'warn');
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

/** 系统自检：应用是否正常、数据库是否可连接、数据表是否齐全。 */
async function runSelfCheck() {
  const out = document.getElementById('check-out');
  const btn = document.getElementById('btn-check');
  if (!out || !btn) return;
  btn.disabled = true;
  out.textContent = '检查中…';
  out.className = 'out';
  try {
    const health = await apiGet('/healthz');
    const db = await apiGet('/api/db-ping');
    out.textContent = [
      `应用：${health.app || '—'}　版本：${health.version || '—'}`,
      `运行阶段：${health.stage || '—'}`,
      `数据库连接：${health.db_target || '—'}`,
      `数据库版本：${db.version || '—'}　字符集：${db.charset || '—'}　库名：${db.database_name || '—'}`,
      `已建数据表：${db.table_count} 张　外键约束：${db.foreign_key_count} 个`,
      '',
      '结论：应用运行正常、数据库连接正常、数据表齐全。',
    ].join('\n');
    out.className = 'out ok';
  } catch (e) {
    out.textContent = `自检未通过：${window.UI.userMessage(e)}`;
    out.className = 'out bad';
  } finally {
    btn.disabled = false;
  }
}

document.addEventListener('DOMContentLoaded', () => {
  loadWhoami();
  const btnLogout = document.getElementById('btn-logout');
  if (btnLogout) btnLogout.addEventListener('click', doLogout);
  const btnCheck = document.getElementById('btn-check');
  if (btnCheck) btnCheck.addEventListener('click', runSelfCheck);
});
