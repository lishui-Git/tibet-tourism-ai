/* =============================================================================
   公共前端逻辑（原生 JS + axios，不引入构建链，详见详细设计 §10）
   -----------------------------------------------------------------------------
   两条纪律：
   1. 只调用本系统自己的 /api/*（Web 层只读数据库，不触发任何模型调用）；
   2. 接口返回统一为 {code, message, data}，code=0 表示成功
      —— 注意 code=0 也包含「数据不足 available=false」这类正常业务结果（§6.4）。
   ============================================================================= */

/** 统一 GET 请求；失败时抛出带业务码与信息的错误。 */
async function apiGet(path, params) {
  const resp = await axios.get(path, { params: params || {} });
  const body = resp.data || {};
  if (body.code !== 0) {
    const err = new Error(body.message || `接口返回 code=${body.code}`);
    err.code = body.code;
    throw err;
  }
  return body.data;
}

/* -----------------------------------------------------------------------------
   面向用户的错误文案
   -----------------------------------------------------------------------------
   后台会把**内部码与字段名**写进 message（如 `code=2001`、`stat_spot`、
   `sentiment.method`）。这些对普通用户没有意义，直接展示会显得像半成品。
   `showError` / `failBox` 因此统一走"先说人话、技术细节收进折叠区"的路径——
   技术信息**不删除**（答辩与排查仍需要），只是不再占据用户视野。
   -------------------------------------------------------------------------- */

/** 业务码 → 用户能看懂的说法（与设计 §6.4 的码表一一对应）。 */
const USER_FACING_CODE_TEXT = {
  1001: '提交的内容格式不正确，请检查后重试。',
  1002: '请求的参数不完整或超出允许范围，请调整后重试。',
  2001: '你没有权限执行该操作，请先登录管理员账号。',
  2002: '登录状态已过期，请重新登录。',
  2003: '账号或密码不正确，请重新输入。',
  2004: '该账号已被停用，请联系系统管理员。',
  3001: '你要查看的内容不存在，可能已被调整或链接有误。',
  4001: '智能问答暂时不可用，请稍后重试或在下方查看数据依据。',
  4002: '提问内容超出系统可回答的范围。',
  5001: '数据库暂时无法访问，请稍后重试。',
  5002: '数据读取失败，请稍后重试。',
  5003: '服务器内部错误，请稍后重试。',
};

/** 把接口 message 里的内部实现痕迹换成人话（不改变原意，只换措辞）。 */
const INTERNAL_TEXT_REPLACEMENTS = [
  [/stat_spot\s*\/\s*stat_time\s*\/\s*stat_ip/g, '统计数据'],
  [/stat_spot/g, '统计数据'],
  [/stat_time/g, '时间统计'],
  [/stat_ip/g, '客源地统计'],
  [/spot_fact_package/g, '景点事实数据'],
  [/spot_report/g, '智能评价'],
  [/comment_semantic/g, '评论语义结果'],
  [/sentiment\.method\s*=\s*mllib/g, '基线情感判定'],
  [/sentiment\.method\s*=\s*deepseek/g, '大模型语义判定'],
  [/sentiment/g, '情感结果'],
  [/mllib/gi, '基线方法'],
  [/DeepSeek/g, '智能分析'],
  [/analysis_task/g, '批处理任务'],
  [/task_log/g, '任务日志'],
  [/REPORT_NOT_GENERATED/g, '评价尚未生成'],
  [/REVIEW_COUNT_BELOW_THRESHOLD/g, '评论量未达到生成门槛'],
  [/ERR_BAD_REQUEST/g, '请求参数不正确'],
  [/code=\d+/g, ''],
  [/\(code=\d+\)/g, ''],
];

/** 把"内部味"很重的文案转成面向用户的中文。 */
function humanize(text) {
  let s = String(text == null ? '' : text);
  INTERNAL_TEXT_REPLACEMENTS.forEach(([re, to]) => { s = s.replace(re, to); });
  return s.replace(/\s{2,}/g, ' ').replace(/[（(]\s*[)）]/g, '').trim();
}

/** 组装"用户提示 + 可折叠技术详情"的提示块。 */
function noticeBox(text, kind, technical) {
  const cls = kind === 'bad' ? 'alert bad' : (kind === 'warn' ? 'alert warn' : 'alert');
  const head = text ? `<div class="${cls}">${escapeHtml(text)}</div>` : '';
  return head + techDetails(technical);
}

/** 只渲染"可折叠技术详情"（不想要提示语、只想要细节时用它）。 */
function techDetails(technical) {
  if (!technical) return '';
  return `<details class="fold"><summary>技术详情（排查用）</summary>
      <div class="fold-body"><pre class="out">${escapeHtml(String(technical))}</pre></div></details>`;
}

/** 「暂未生成」时的说明块（带粗体标题 + 可选技术详情）。
 *
 * 【临时性 UI，需在数据生产完成后移除】
 * 在「景点综合智能评价」尚未生成期间，首页 / 智能分析 / 景点对比都需要向用户说明
 * "这项还没生成、但其他内容仍可用"。为避免同一段话在几处各写一遍、将来漏改其中一处，
 * 组装逻辑集中在这里。
 *
 * **移除时机**：全量评价生成完毕、`/api/overview/summary` 的
 * `coverage.report.reports_generated` 达到 `spots_ge100` 之后，
 * 删除本函数与 `window.UI.pendingNotice` 的各调用点即可。
 */
function pendingNotice(title, hint, technical) {
  const head = `<div class="alert warn"><strong>${escapeHtml(title)}</strong>`
    + (hint ? `<br/>${escapeHtml(hint)}` : '') + '</div>';
  return head + techDetails(technical);
}

/** 由错误对象生成用户可读文案（优先用业务码映射，其次用 humanize 过的 message）。 */
function userMessageOf(error) {
  const code = error && error.code;
  if (code && USER_FACING_CODE_TEXT[code]) return USER_FACING_CODE_TEXT[code];
  const raw = (error && (error.message || String(error))) || '未知错误';
  return humanize(raw) || '操作未成功，请稍后重试。';
}

/** 把查错信息显示到页面上（不弹窗，避免打断演示）。 */
function showError(elId, error) {
  const el = document.getElementById(elId);
  if (!el) return;
  const tech = error && (error.code !== undefined ? `业务码 ${error.code}：${error.message}` : String(error));
  el.innerHTML = noticeBox(`加载失败：${userMessageOf(error)}`, 'bad', tech);
}

function escapeHtml(text) {
  return String(text == null ? '' : text)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function fmtInt(n) {
  if (n === null || n === undefined || n === '') return '—';
  return Number(n).toLocaleString('zh-CN');
}

function fmtPct(v, digits) {
  if (v === null || v === undefined || v === '') return '—';
  return (Number(v) * 100).toFixed(digits === undefined ? 2 : digits) + '%';
}

function fmtNum(v, digits) {
  if (v === null || v === undefined || v === '') return '—';
  return Number(v).toFixed(digits === undefined ? 2 : digits);
}

/** 口径说明块（BR-10：受口径影响的接口都要显示说明与样本量）。 */
function caliberNote(text, sampleSize) {
  if (!text) return '';
  const sample = (sampleSize === null || sampleSize === undefined)
    ? '' : `　有效样本量：<strong>${fmtInt(sampleSize)}</strong>`;
  return `<div class="caliber">口径说明：${escapeHtml(text)}${sample}</div>`;
}

/** 数据表：rows 为二维数组，head 为表头。 */
function renderTable(head, rows) {
  const thead = '<tr>' + head.map(h => `<th class="th-plain">${escapeHtml(h)}</th>`).join('') + '</tr>';
  const tbody = rows.map(r => '<tr>' + r.map(c => `<td>${c === null || c === undefined ? '—' : c}</td>`).join('') + '</tr>').join('');
  return `<table class="data-table"><thead>${thead}</thead><tbody>${tbody}</tbody></table>`;
}

/** ECharts 实例登记，窗口变化时统一 resize。 */
const _charts = [];
function initChart(domId, option) {
  const dom = document.getElementById(domId);
  if (!dom || typeof echarts === 'undefined') return null;
  const chart = echarts.init(dom);
  chart.setOption(option);
  _charts.push(chart);
  return chart;
}
window.addEventListener('resize', () => _charts.forEach(c => c.resize()));

/** 折线/柱状图的通用配色（与 CSS 主色系一致：深青为主，暖棕/绿/红为辅）。 */
const CHART_COLORS = ['#0f5f6b', '#a2603a', '#2f7d4f', '#b06a12', '#b3352f', '#4a6fa5', '#6b7c93'];

/* -----------------------------------------------------------------------------
   返回顶部（全局）
   -----------------------------------------------------------------------------
   行为：顶部时隐藏 → 下滑超过阈值后淡入 → 平滑回到顶部。
   阈值取 320px：小于一屏的一半，避免"稍微滚一下就冒出来"打扰阅读。
   -------------------------------------------------------------------------- */
const BACK_TO_TOP_AT = 320;

function _initBackToTop() {
  const btn = document.getElementById('back-to-top');
  if (!btn) return;

  const sync = () => {
    btn.classList.toggle('show', window.scrollY > BACK_TO_TOP_AT);
  };
  // passive 监听：滚动回调不阻塞渲染
  window.addEventListener('scroll', sync, { passive: true });
  sync();

  btn.addEventListener('click', () => {
    // 尊重"减少动态效果"偏好：该用户群体可能对滚动动画不适
    const reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    window.scrollTo({ top: 0, behavior: reduce ? 'auto' : 'smooth' });
  });
}

/* -----------------------------------------------------------------------------
   页面级工具（挂到 window.UI，供各页面脚本复用，避免每页重复实现）
   -------------------------------------------------------------------------- */
window.UI = {
  /** 统计卡片组：items = [{label, value, sub}] */
  statCards(items) {
    return items.map(it => `
      <div class="stat">
        <span class="stat-label">${escapeHtml(it.label)}</span>
        <span class="stat-value">${it.value}</span>
        ${it.sub ? `<span class="stat-sub">${escapeHtml(it.sub)}</span>` : ''}
      </div>`).join('');
  },

  /** 加载失败或"无数据"的统一提示块 */
  empty(text, kind) {
    if (kind === 'empty') return `<div class="empty-state">${escapeHtml(text)}</div>`;
    return `<div class="alert ${kind === 'warn' ? 'warn' : ''}">${escapeHtml(text)}</div>`;
  },

  /** 用户提示 + 可折叠技术详情（各页面统一用它，避免内部码直接暴露给用户） */
  notice(text, kind, technical) { return noticeBox(text, kind, technical); },

  /** 只渲染可折叠技术详情（技术信息保留但不占用户视野） */
  tech(technical) { return techDetails(technical); },

  /** 「暂未生成」说明块（数据生产完成后可整体移除，见函数注释） */
  pendingNotice(title, hint, technical) { return pendingNotice(title, hint, technical); },

  /** 错误对象 → 用户可读文案 */
  userMessage(error) { return userMessageOf(error); },

  /** 用户当前选择的景点 ID（用于「景点分析」页与「智能分析」页联动） */
  setActiveSpot(spotId) {
    try { sessionStorage.setItem('activeSpotId', String(spotId)); } catch (e) { /* 隐私模式下忽略 */ }
  },
  getActiveSpot() {
    try { return sessionStorage.getItem('activeSpotId'); } catch (e) { return null; }
  },

  /** 跳转到"智能分析 → 景点评价"并带上景点（由「景点分析」页调用） */
  openEvaluation(spotId) {
    window.location.href = `/smart?tab=evaluation&spot=${encodeURIComponent(spotId)}`;
  },
};

document.addEventListener('DOMContentLoaded', _initBackToTop);


