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

/** 把查错信息显示到页面上（不弹窗，避免打断演示）。 */
function showError(elId, error) {
  const el = document.getElementById(elId);
  if (!el) return;
  el.innerHTML = `<div class="alert bad">加载失败：${escapeHtml(error.message || String(error))}</div>`;
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

/** 折线/柱状图的通用配色。 */
const CHART_COLORS = ['#1168bd', '#2e7d32', '#c62828', '#ef6c00', '#6a1b9a', '#00838f', '#5d4037'];

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
    return `<div class="alert ${kind === 'warn' ? 'warn' : ''}">${escapeHtml(text)}</div>`;
  },

  /** 用户当前选择的景点 ID（用于"智能评价"页与"景点分析"页联动） */
  setActiveSpot(spotId) {
    try { sessionStorage.setItem('activeSpotId', String(spotId)); } catch (e) { /* 隐私模式下忽略 */ }
  },
  getActiveSpot() {
    try { return sessionStorage.getItem('activeSpotId'); } catch (e) { return null; }
  },
};

