/* Model membership, verification and failures remain distinct in the panel. */
const assert = require('assert');
const {dashboardScript} = require('./_dashboard_source.js');
const dom = require('./_dom_stub.js');
const tbody = dom.makeElement('tbody');
let response = {data: [], excluded: []};
dom.installDom({
  querySelector: {'#modelsTable tbody': tbody},
  fetch: () => Promise.resolve({ok: true, status: 200, json: () => Promise.resolve(response)}),
});
const api = new Function(dashboardScript() + `
  return {modelAvailabilityCell, renderAvailableModels, renderExcludedModels, loadModels};`)();

(async () => {
  const checkedAt = 1791900000;
  window.VIEW_REALM = 'intl';
  response = {
    data: [
      {id: 'good', credits: 'x0.42', availability: {status: 'available', checkedAt}},
      {id: 'new', availability: {status: 'unverified'}},
    ],
    excluded: [{id: 'bad<model>', availability: {status: 'unavailable', checkedAt,
      reason: 'upstream_service_not_found', realm: 'intl'}}],
    catalog: {upstreamCount: 3, verifiedCount: 1, fetchedAt: checkedAt, stale: false},
  };
  await api.loadModels();
  assert.ok(tbody.innerHTML.includes('近期请求成功'));
  assert.ok(tbody.innerHTML.includes('尚未验证'));
  assert.ok(tbody.innerHTML.includes('未提供'));
  assert.ok(!tbody.innerHTML.includes('bad&lt;model&gt;'));
  const excluded = dom.byId('excludedModels').innerHTML;
  assert.ok(excluded.includes('bad&lt;model&gt;'));
  assert.ok(!excluded.includes('bad<model>'));
  assert.ok(excluded.includes('上游返回模型服务不存在'));
  assert.ok(excluded.includes('复测'));
  const status = dom.byId('catalogStatus').textContent;
  assert.ok(status.includes('目录 3 个') && status.includes('成功 1 个') && status.includes('排除 1 个'));
  assert.ok(status.includes('每 30 秒更新'));
  assert.ok(!api.modelAvailabilityCell({availability: {status: 'unverified'}}).includes('近期请求成功'));
  response = {data: [{id: 'bad<model>', availability: {status: 'available', checkedAt}}], excluded: [], catalog: {verifiedCount: 1}};
  await api.loadModels();
  assert.equal(dom.byId('excludedModels').innerHTML, '');
  assert.ok(tbody.innerHTML.includes('bad&lt;model&gt;'));
  console.log('model availability panel assertions passed');
})().catch(error => { console.error(error); process.exit(1); });
