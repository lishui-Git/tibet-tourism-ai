/* 管理员登录页：调用 /api/auth/login 建立会话，成功后进入管理后台。
   失败信息按业务码给出可读提示（不弹窗，也不显示内部码）。 */

/** 业务码 → 用户可读提示。 */
const LOGIN_MESSAGES = {
  1001: '请填写用户名与口令。',
  2003: '用户名或口令不正确，请重新输入。',
  2004: '该账号已被停用，请联系系统管理员。',
  5001: '数据库暂时无法访问，请稍后重试。',
  5002: '登录服务暂时不可用，请稍后重试。',
};

function loginMsg(html, kind) {
  const el = document.getElementById('login-msg');
  if (el) el.innerHTML = window.UI.notice(html, kind || '');
}

async function doLogin() {
  const username = document.getElementById('login-user').value.trim();
  const password = document.getElementById('login-pass').value;
  const btn = document.getElementById('btn-login');

  if (!username || !password) {
    loginMsg(LOGIN_MESSAGES[1001], 'warn');
    return;
  }
  btn.disabled = true;
  loginMsg('正在登录…');

  try {
    const resp = await axios.post('/api/auth/login', { username, password });
    const body = resp.data || {};
    if (body.code !== 0) {
      loginMsg(window.UI.userMessage({ code: body.code, message: body.message }), 'bad');
      return;
    }
    const user = body.data || {};
    loginMsg(`登录成功，欢迎 ${user.nickname || user.username}。正在进入管理后台…`);
    setTimeout(() => { window.location.href = '/admin'; }, 500);
  } catch (e) {
    const data = e.response && e.response.data;
    const err = data ? { code: data.code, message: data.message } : e;
    loginMsg(window.UI.userMessage(err), 'bad');
  } finally {
    btn.disabled = false;
  }
}

document.addEventListener('DOMContentLoaded', () => {
  const btn = document.getElementById('btn-login');
  if (btn) btn.addEventListener('click', doLogin);
  ['login-user', 'login-pass'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.addEventListener('keydown', ev => { if (ev.key === 'Enter') doLogin(); });
  });
  // 已登录则直接进入管理后台，避免"再登一次"的困惑
  apiGet('/api/auth/me').then(() => { window.location.href = '/admin'; }).catch(() => { /* 未登录，留在本页 */ });
});
