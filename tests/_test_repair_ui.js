const assert = require('assert');
const {dashboardScript} = require('./_dashboard_source.js');
const dom = require('./_dom_stub.js');
dom.installDom({fetch: () => Promise.resolve({ok: true, status: 200,
  json: () => Promise.resolve({accounts: [], data: [], rows: []})})});
const api = new Function(dashboardScript() + `
  return {reasoningTokenCell, slotSelectHtml, updateTaskPageCheckinCards};`)();
assert.ok(api.reasoningTokenCell({reasoning_tokens: null, reasoning_tokens_source: 'not_reported'}).includes('未上报'));
assert.equal(api.reasoningTokenCell({reasoning_tokens: 0, reasoning_tokens_source: 'upstream'}), '0');
assert.ok(api.reasoningTokenCell({reasoning_tokens: 0}).includes('未知'));
assert.ok(api.reasoningTokenCell({reasoning_tokens: 9, reasoning_tokens_source: 'estimated'}).includes('≈9'));
const egress = api.slotSelectHtml({uid: 'fixture', loginSource: 'account-hub:1', egress: {
  policy: 'account_lease_auto_recovery', key: 'wb-fixture', platform: 'AppsGlobal', ip: '203.0.113.8', status: 'available'}});
assert.ok(egress.includes('AppsGlobal') && egress.includes('203.0.113.8'));
assert.ok(!egress.includes('<select') && !egress.includes('直连'));
window.ACCOUNTS = [{realm: 'intl', enabled: true, lastDailyChat: '2099-01-01'}];
api.updateTaskPageCheckinCards();
assert.ok(dom.byId('intlTodayActiveState').textContent.includes('无法从 API 判断'));
console.log('repair UI evidence states passed');
