/* 「智能分析」整合页：负责两个子标签（景点评价 / 智能问答）的切换与懒加载。
   具体内容由 evaluation.js 与 qa.js 渲染 —— 它们原本是独立页面，
   现在共用同一个页面容器，因此这里只做"切换 + 首次进入时初始化"。

   联动：`/smart?spot=<id>` 会直接把该景点带进「景点评价」；`?tab=qa` 直达问答。 */

/** 当前激活的子标签。 */
let activeTab = 'evaluation';
/** 各子面板是否已初始化（避免每次切换都重复请求）。 */
const tabInited = { evaluation: false, qa: false };

/* 告诉 evaluation.js："本页由 smart.js 统一调度初始化"，避免它自行发起一次多余请求。
   这是一个**显式契约**：两边都读同一个标记，不需要靠脚本加载顺序猜。 */
window.__SMART_PAGE__ = true;

function switchTab(tab, opts) {
  const options = opts || {};
  if (tab !== 'evaluation' && tab !== 'qa') tab = 'evaluation';
  activeTab = tab;

  document.querySelectorAll('.subtab').forEach(btn => {
    const on = btn.getAttribute('data-tab') === tab;
    btn.classList.toggle('active', on);
    btn.setAttribute('aria-selected', on ? 'true' : 'false');
  });
  document.querySelectorAll('.subpanel').forEach(panel => {
    panel.hidden = panel.id !== `panel-${tab}`;
  });

  // 首次进入某标签时才初始化它（问答页尤其不该一进页面就发请求）
  if (!tabInited[tab]) {
    tabInited[tab] = true;
    if (tab === 'evaluation' && typeof loadSpotOptions === 'function') loadSpotOptions();
    if (tab === 'qa') {
      const input = document.getElementById('qa-question');
      if (input && options.focus !== false) input.focus();
    }
  }

  // 把状态写进地址栏，便于分享/刷新后停在同一标签
  try {
    const url = new URL(window.location.href);
    url.searchParams.set('tab', tab);
    window.history.replaceState({}, '', url.toString());
  } catch (e) { /* 老浏览器忽略 */ }
}

document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('.subtab').forEach(btn => {
    btn.addEventListener('click', () => switchTab(btn.getAttribute('data-tab')));
  });

  const initial = (window.__INITIAL_TAB__ === 'qa') ? 'qa' : 'evaluation';
  switchTab(initial, { focus: false });

  // 带 ?spot= 进来时：等景点下拉加载完再选中并直接出评价。
  // evaluation.js 的 loadSpotOptions 是异步的，这里用轮询等待选项就绪
  // （比改造它的内部流程更稳妥，也不影响它在旧地址下的行为）。
  const wanted = window.__INITIAL_SPOT__;
  if (wanted) {
    window.UI.setActiveSpot(wanted);
    if (typeof loadSpotOptions === 'function') {
      let tries = 0;
      const timer = setInterval(() => {
        tries += 1;
        const sel = document.getElementById('eval-spot');
        const has = sel && Array.from(sel.options).some(o => String(o.value) === String(wanted));
        if (has) {
          clearInterval(timer);
          sel.value = String(wanted);
          if (typeof loadReport === 'function') loadReport();
        } else if (tries > 40) {   // ≈4s 后放弃，避免无限轮询
          clearInterval(timer);
        }
      }, 100);
    }
  }
});
