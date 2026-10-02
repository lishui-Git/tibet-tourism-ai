/* 登录页：调用 /api/auth/login 建立会话，成功后跳转到任务页。
   失败信息按业务码给出可读提示（不弹窗）。 */

const LOGIN_MESSAGES = {
  1001: '请填写用户名与口令',
  2004: '用户名或口令错误',
  5001: '数据库访问失败，请检查服务状态',
};

async function doLogin() {
  const username = document.getElementById('login-user').value.trim();
  const password = document.getElementById('login-pass').value;
  const msg = document.getElementById('login-msg');
  const btn = document.getElementById('btn-login');
  if (!username || !password) {
    msg.innerHTML = `<div class="alert warn">${LOGIN_MESSAGES[1001]}</div>`;
    return;
  }
  btn.disabled = true;
  msg.innerHTML = '<div class="alert">登录中…</div>';
  try {
    const resp = await axios.post('/api/auth/login', { username, password });
    const body = resp.data || {};
    if (body.code !== 0) {
      msg.innerHTML = `<div class="alert bad">${escapeHtml(body.message || '登录失败')}（code=${body.code}）</div>`;
      return;
    }
    const user = body.data || {};
    msg.innerHTML = `<div class="alert">登录成功，欢迎 ${escapeHtml(user.nickname || user.username)}
      （角色 ${escapeHtml(user.role)}）。正在跳转…</div>`;
    setTimeout(() => { window.location.href = '/tasks'; }, 600);
  } catch (e) {
    const code = (e.response && e.response.data && e.response.data.code) || '';
    const text = LOGIN_MESSAGES[code] || (e.response && e.response.data && e.response.data.message) || e.message;
    msg.innerHTML = `<div class="alert bad">${escapeHtml(text)}${code ? `（code=${code}）` : ''}</div>`;
  } finally {
    btn.disabled = false;
  }
}

document.addEventListener('DOMContentLoaded', () => {
  document.getElementById('btn-login').addEventListener('click', doLogin);
  ['login-user', 'login-pass'].forEach(id => {
    document.getElementById(id).addEventListener('keydown', ev => { if (ev.key === 'Enter') doLogin(); });
  });
  // 已登录则直接跳转
  apiGet('/api/auth/me').then(() => { window.location.href = '/tasks'; }).catch(() => { /* 未登录，留在本页 */ });
});
