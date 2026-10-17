const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, '..', 'dashboard.html'), 'utf8');
const source = html.slice(html.indexOf('function renderCapabilitySources('), html.indexOf('async function loadSettings('));
const root = {innerHTML:'', querySelectorAll:()=>[]};
const elements = {setCapabilitySources:root, setCapabilityState:{textContent:''}};
const cards = [];
let sent;
const context = {document:{getElementById:id=>elements[id], querySelectorAll:()=>cards},
  esc:value=>String(value??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])),
  toast:()=>{}, postJSON:async(url,body)=>{sent={url,body};return {upstream:body.upstream};}};
vm.createContext(context);
vm.runInContext(source,context);
context.renderCapabilitySources({capabilities:{models:{cn:{enabled:true,base_url:'https://example.test/"<script>'}}}});
assert(!root.innerHTML.includes('<script>'));
assert(root.innerHTML.includes('&quot;&lt;script&gt;'));
assert.equal((root.innerHTML.match(/data-capability=/g)||[]).length,6);

for(const kind of ['chat','billing','models']) for(const realm of ['intl','cn']){
  const id='cap-'+kind+'-'+realm;
  cards.push({dataset:{capability:kind,realm}});
  const values={enabled:true,base_url:'https://'+kind+'.example.test/'+realm,auth:'bearer',
    key_file:'/keys/'+kind+'-'+realm,header_name:'X-API-Key',timeout_seconds:'30'};
  for(const [key,value] of Object.entries(values)) elements[id+'-'+key]={value,checked:value===true,disabled:false,reportValidity:()=>true};
  context.toggleCapabilityFields(id);
  assert(elements[id+'-header_name'].disabled);
  assert(!elements[id+'-key_file'].disabled);
}
elements['cap-chat-cn-enabled'].checked=false;
context.toggleCapabilityFields('cap-chat-cn');
assert(elements['cap-chat-cn-base_url'].disabled);
const button={disabled:false};
context.saveCapabilitySources(button).then(()=>{
  assert.equal(sent.url,'/settings/save');
  assert.deepEqual(Object.keys(sent.body),['upstream']);
  assert.deepEqual(Object.keys(sent.body.upstream),['capabilities']);
  assert.equal(sent.body.upstream.capabilities.models.intl.key_file,'/keys/models-intl');
  assert.equal(sent.body.upstream.capabilities.billing.cn.timeout_seconds,30);
  assert.equal(sent.body.upstream.capabilities.chat.cn.enabled,false);
  assert.equal(button.disabled,false);
  assert.equal(elements.setCapabilityState.textContent,'已保存');
  console.log('Capability form source separation, escaping and save contract passed');
}).catch(error=>{console.error(error);process.exitCode=1;});
