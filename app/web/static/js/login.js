/* 统一登录页：调用 /api/auth/login 建立会话。
   普通用户 → 我的问答；管理员 → 管理后台。
   失败信息按业务码给出可读提示（不弹窗，也不显示内部码）。 */

let loggingIn = false;

function loginMsg(html, kind) {
  const el = document.getElementById('login-msg');
  if (el) el.innerHTML = window.UI.notice(html, kind || '');
}

/** 客户端基础校验；服务端仍会按 3–32 / 6–64 再做一次。 */
function validateLogin(username, password) {
  if (!username) return '请输入用户名。';
  if (username.length < 3) return '用户名至少 3 个字符。';
  if (!password) return '请输入登录口令。';
  if (password.length < 6) return '口令至少 6 位。';
  return '';
}

function currentNext() {
  try {
    return new URLSearchParams(window.location.search).get('next');
  } catch (e) {
    return null;
  }
}

async function doLogin() {
  if (loggingIn) return;

  const userEl = document.getElementById('login-user');
  const passEl = document.getElementById('login-pass');
  const btn = document.getElementById('btn-login');
  if (!userEl || !passEl || !btn) return;

  const username = userEl.value.trim();
  const password = passEl.value;
  const problem = validateLogin(username, password);
  if (problem) {
    loginMsg(problem, 'warn');
    (username ? passEl : userEl).focus();
    return;
  }

  loggingIn = true;
  let succeeded = false;
  btn.disabled = true;
  btn.textContent = '登录中…';
  loginMsg('正在登录…');

  try {
    const resp = await axios.post('/api/auth/login', { username, password });
    const body = resp.data || {};
    if (body.code !== 0) {
      loginMsg(window.UI.userMessage({ code: body.code, message: body.message }), 'bad');
      return;
    }

    const user = body.data || {};
    const dest = window.AUTH.nextFor(user, currentNext());
    succeeded = true;
    loginMsg(`登录成功，欢迎 ${user.nickname || user.username}。正在进入${user.is_admin ? '管理后台' : '我的问答'}…`, 'ok');
    setTimeout(() => { window.location.href = dest; }, 450);
  } catch (e) {
    const data = e.response && e.response.data;
    loginMsg(window.UI.userMessage(data ? { code: data.code, message: data.message } : e), 'bad');
  } finally {
    loggingIn = false;
    // 成功后保持禁用，避免跳转前的 450ms 内再次提交；失败才恢复按钮
    if (!succeeded) {
      btn.disabled = false;
      btn.textContent = '登录';
    }
  }
}

document.addEventListener('DOMContentLoaded', () => {
  const form = document.getElementById('login-form');
  if (form) {
    form.addEventListener('submit', ev => {
      ev.preventDefault();
      doLogin();
    });
  }

  // 密码显示 / 隐藏
  const passEl = document.getElementById('login-pass');
  const toggle = document.getElementById('toggle-pass');
  if (passEl && toggle) {
    toggle.addEventListener('click', () => {
      const show = passEl.type === 'password';
      passEl.type = show ? 'text' : 'password';
      toggle.textContent = show ? '隐藏' : '显示';
      toggle.setAttribute('aria-label', show ? '隐藏口令' : '显示口令');
      toggle.setAttribute('aria-pressed', show ? 'true' : 'false');
      passEl.focus();
    });
  }

  // 已登录则按角色跳转，避免"再登一次"或把普通用户送到 /admin
  apiGet('/api/auth/me')
    .then(me => { window.location.href = window.AUTH.nextFor(me, currentNext()); })
    .catch(() => { /* 未登录，留在本页 */ });
});
