/* 本地渲染桩：不启动服务、不连数据库，直接把 evaluation.js 的函数加载起来跑边界用例。
   用途：验证"字段缺失 / 空评论 / 重复评论 / 短评论 / 认不出的文案"这些情况不会渲染出
   null / undefined / NaN，也不会把条目改坏。（一次性核查脚本，不进入正式测试套件。） */
import fs from 'fs';

const src = fs.readFileSync('app/web/static/js/evaluation.js', 'utf8');
const esc = s => String(s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

const stubs = {
  window: {
    UI: {
      statCards: () => '', notice: t => `<div class="alert">${esc(t)}</div>`,
      pendingNotice: () => '', empty: () => '', userMessage: () => '', getActiveSpot: () => null,
    },
  },
  document: { addEventListener() {}, getElementById: () => null, querySelectorAll: () => [] },
  axios: { get: async () => ({ data: {} }), post: async () => ({ data: {} }) },
  apiGet: async () => ({}),
  escapeHtml: esc,
  fmtInt: n => Number(n || 0).toLocaleString('en-US'),
  fmtNum: (n, d = 2) => (n === null || n === undefined) ? '—' : Number(n).toFixed(d),
  fmtPct: (n, d = 1) => (n === null || n === undefined) ? '—' : `${(Number(n) * 100).toFixed(d)}%`,
  caliberNote: () => '', renderTable: () => '', loadSpotOptions: async () => {},
};

const names = Object.keys(stubs);
const factory = new Function(...names, `${src}
  return { renderVoiceCard, voiceUsable, humanizeReportItem, humanizeSummary, humanizeTerms, fmtRateText, renderAvailable };`);
const api = factory(...names.map(k => stubs[k]));

let pass = 0, fail = 0;
const check = (name, ok, detail = '') => {
  if (ok) { pass += 1; console.log(`  [PASS] ${name}`); }
  else { fail += 1; console.log(`  [FAIL] ${name} — ${detail}`); }
};
const strip = h => String(h).replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim();

console.log('本地渲染桩边界核对（不连服务、不写数据）');

// —— 字段缺失：score / publish_date / like_count / topics 全空
const bare = strip(api.renderVoiceCard({ content: '景色很好，值得一看，天气也不错。', score: null, publish_date: null, like_count: null, topics: [] }, 'neu'));
check('字段全缺时不出现 null/undefined/NaN', !/null|undefined|NaN/.test(bare), bare);
check('评分缺失显示「未评分」', bare.includes('未评分'), bare);
check('无话题时不出现空的「提到：」', !/提到：\s*$/.test(bare), bare);

// —— 空 / 重复 / 短评论过滤
check('空评论被剔除', api.voiceUsable({ content: '', content_length: 0 }) === false);
check('无信息量短评（≤10 字）被剔除', api.voiceUsable({ content: '不错', content_length: 2 }) === false);
check('内容重复的评论被剔除', api.voiceUsable({ content: 'x'.repeat(20), content_length: 20, is_dup_content: 1 }) === false);
check('正常评论保留', api.voiceUsable({ content: 'x'.repeat(20), content_length: 20, is_dup_content: 0 }) === true);

// —— 长评论折叠
const long = api.renderVoiceCard({ content: '甲'.repeat(400), score: 5, publish_date: '2024-01-02', like_count: 3, topics: ['风景'] }, 'pos');
check('长评论折叠并给出「查看完整评论」', long.includes('查看完整评论') && long.includes('400 字'), '');
check('折叠区里是完整原文', long.includes('甲'.repeat(400)), '');

// —— 空输入不炸
check('humanizeReportItem(null) 返回 null（不渲染空条目）', api.humanizeReportItem(null) === null);
check('humanizeReportItem("") 返回 null', api.humanizeReportItem('') === null);
check('humanizeSummary(null/undefined) 不抛错并返回空串',
  api.humanizeSummary(null) === '' && api.humanizeSummary(undefined) === '');
check('整体指标条目被去重（返回 null）', api.humanizeReportItem('整体正面率0.8782，平均评分4.49') === null);

// —— 认不出的句子原样保留
const factual = '古城为唐代吐蕃王朝所建，藏语意为月光城';
check('事实性描述原样保留', strip(api.humanizeReportItem(factual)) === factual, strip(api.humanizeReportItem(factual)));

// —— 百分比换算与两套口径
check('0.8782 → 87.8%', api.fmtRateText('0.8782') === '87.8%', api.fmtRateText('0.8782'));
check('14% 不被截断（回归：曾输出 100%4%）', api.fmtRateText('14%') === '14%', api.fmtRateText('14%'));
check('星级口径 → 好评/差评比例',
  strip(api.humanizeReportItem('门票负面率0.58，样本量452')) === '门票：差评比例 58%，452 条评论',
  strip(api.humanizeReportItem('门票负面率0.58，样本量452')));
check('情感口径 → 正面/负面评价占比（不与好评率混用）',
  strip(api.humanizeReportItem('正面情感占比0.61')).includes('正面评价占比 61%'),
  strip(api.humanizeReportItem('正面情感占比0.61')));
check('不带数字的术语也能转换（…等负面率均超过0.5）',
  strip(api.humanizeReportItem('交通、设施等负面率均超过0.5')) === '交通、设施等：差评比例均超过50%',
  strip(api.humanizeReportItem('交通、设施等负面率均超过0.5')));
check('裸写法 14% 在句内不被改坏',
  strip(api.humanizeReportItem('服务负面率14%，样本量177')) === '服务：差评比例 14%，177 条评论',
  strip(api.humanizeReportItem('服务负面率14%，样本量177')));

console.log(`\n本地渲染桩：${pass}/${pass + fail} 项通过`);
process.exit(fail ? 1 : 0);
