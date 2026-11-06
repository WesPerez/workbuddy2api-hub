/* Fork policy: retained international activity entry points never send requests.
 * 分支策略：保留上游名单计算，但国际奖励仅提示官方客户端，不发自动活跃请求。
 */
'use strict';
const assert = require('assert');
const {dashboardHtml} = require('./_dashboard_source.js');
const {loadDashboard} = require('./_dashboard_sandbox.js');
const html = dashboardHtml();
let posts = [];
const page = loadDashboard({fetch: (url, opts) => {
  if(opts && opts.method === 'POST') posts.push(String(url));
  const body = {rows: [], data: [], results: [], accounts: [], slots: []};
  return Promise.resolve({ok: true, status: 200,
    json: () => Promise.resolve(body), text: () => Promise.resolve(JSON.stringify(body))});
}});
assert(!html.includes('id="btnPageDailyChatPending"'), 'No automatic international activity button');
assert(!html.includes('id="btnPageDailyChat"'), 'No legacy automatic activity button');
assert(html.includes('id="legacyDailyChatSettings" hidden style="display:none"'), 'Legacy activity controls stay hidden');
assert(html.includes('if(btnDailyChat) btnDailyChat.style.display = "none";'), 'Toolbar activity stays hidden');
assert(html.includes('id="cardIntlTodayActive"'), 'Retain the upstream card identity');
assert(html.includes('客户端活跃状态'), 'Describe client activity without claiming API reward eligibility');
const pad = n => String(n).padStart(2, '0');
const d = new Date();
const today = d.getFullYear() + '-' + pad(d.getMonth()+1) + '-' + pad(d.getDate());
page.ACCOUNTS = [
  {uid:'done', realm:'intl', enabled:true, lastDailyChat:today+' 08:00:00'},
  {uid:'pending', realm:'intl', enabled:true},
  {uid:'disabled', realm:'intl', enabled:false},
  {uid:'cn', realm:'cn', enabled:true}
];
assert.deepStrictEqual(Array.from(page.pendingDailyChatAccounts(), a => a.uid), ['pending']);
assert(page.pendingDailyChatTip(page.pendingDailyChatAccounts()).includes('(1)'));
(async () => {
  for(const name of ['doDailyChat', 'doDailyChatPending', 'doDailyChatWeb']) {
    posts = [];
    await page[name]({disabled:false, textContent:'test'});
    assert(!posts.some(url => url.includes('/daily-chat')), name+' must not send activity requests');
  }
  page.updateTaskPageCheckinCards();
  assert.strictEqual(document.getElementById('intlTodayActiveState').textContent,
    '无法从 API 判断，请以官方客户端为准');
  console.log('daily chat fork-policy assertions passed');
})().catch(e => {console.error(e); process.exit(1);});
