const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
module.exports = async function() {
  const elements = new Map(), writes = [];
  let focused = true, modal = false;
  const row = (id,top,bottom) => ({id:'message-' + id,getBoundingClientRect:() => ({top,bottom})});
  const visibleRows = [row(10,10,40),row(11,40,80),row(20,110,140)];
  const context = vm.createContext({console,setTimeout:() => 0,clearTimeout:() => {},
    document:{createElement:() => ({}),visibilityState:'visible',hasFocus:() => focused,querySelector:() => modal ? {} : null,addEventListener:() => {},getElementById:id => {
      if(!elements.has(id)) elements.set(id,{textContent:'LANmsg',addEventListener:() => {},getBoundingClientRect:() => ({top:0,bottom:100}),querySelectorAll:() => visibleRows});
      return elements.get(id);
    }},window:{addEventListener:() => {}},
    api:async(path,options) => { writes.push({path,body:JSON.parse(options.body)}); return {}; }
  });
  const app = fs.readFileSync(__dirname + '/../lanmsg/static/app.js','utf8');
  vm.runInContext(app.slice(0,app.indexOf('function receiveServerSettings(')),context);
  vm.runInContext(`member={id:1};readStates.set(1,{read_id:0});refreshReadState=async()=>{};showError=error=>{throw error;};`,context);
  await vm.runInContext('markViewportRead()',context);
  assert.equal(writes.length,1);
  assert.equal(writes[0].path,'/channels/1/read');
  assert.equal(writes[0].body.message_id,11); // A message below the viewport is still unread.
  vm.runInContext('readStates.set(1,{read_id:11})',context);
  await vm.runInContext('markViewportRead()',context);
  assert.equal(writes.length,1);
  vm.runInContext('readStates.set(1,{read_id:0})',context);
  focused = false;
  await vm.runInContext('markViewportRead()',context);
  focused = true; modal = true;
  await vm.runInContext('markViewportRead()',context);
  modal = false;
  vm.runInContext('selecting=true',context);
  await vm.runInContext('markViewportRead()',context);
  assert.equal(writes.length,1); // Background, dialog, and unfinished channel loads never advance reads.
  console.log('Visible-message read tracking and focus gates passed');
};
if(require.main === module) module.exports().catch(error => { console.error(error); process.exitCode=1; });
