const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const context = {MediaPreviews:{tokens:text => [{text}]},document:{createTextNode:text => ({text}),createElement:() => ({})}};
vm.runInNewContext(fs.readFileSync(__dirname + '/../lanmsg/static/mentions.js','utf8'),context);
const M = context.Mentions;
const user = {id:2,username:'guest',display_name:'Alex'};
const member = {id:2,notifications:'mentions'};
const message = {id:1,user_id:1,channel_id:1,mentions:[user],content:'@guest'};
assert.equal(M.shouldAlert(message,member,1,true),false);
assert.equal(M.shouldAlert(message,member,1,false),true);
assert.equal(M.shouldAlert(message,member,2,true),true);
assert.equal(M.shouldAlert({...message,user_id:2},member,2,false),false);
assert.equal(M.shouldAlert({...message,deleted:123},member,2,false),false);
assert.equal(M.shouldAlert(message,{...member,notifications:'none'},2,false),false);
assert.equal(M.shouldAlert({...message,mentions:[]},member,2,false),false);
assert.equal(M.shouldAlert({...message,mentions:[]},{...member,notifications:'all'},2,false),true);
assert.equal(M.suggestions('hi @gu',6,[user]).users[0].id,2);
assert.equal(M.suggestions('email@gu',8,[user]),null);
assert.equal(M.suggestions('https://host/@gu',16,[user]),null);
assert.equal(M.suggestions('@Alex',5,[user]).users.length,0);
const children = [], body = {append:node => children.push(node)};
M.append(body,'<script> @GUEST me@guest @unknown',[user],() => {});
assert.equal(children.filter(node => node.className === 'mention').length,1);
assert.equal(children.find(node => node.className === 'mention').textContent,'@GUEST');
assert.equal(children[0].text,'<script> ');
// Exercise live badge state and desktop delivery with browser APIs represented by fakes.
const elements = new Map(), delivered = [];
const live = vm.createContext({Mentions:M,console,setTimeout:() => 0,isSecureContext:true,
  document:{visibilityState:'hidden',hasFocus:() => false,addEventListener:() => {},getElementById:id => {
    if(!elements.has(id)) elements.set(id,{textContent:'LANmsg',addEventListener:() => {}});
    return elements.get(id);
  }},window:{addEventListener:() => {}},
  Notification:class { static permission = 'granted'; constructor(title,options) { delivered.push({title,options}); } close() {} }
});
const app = fs.readFileSync(__dirname + '/../lanmsg/static/app.js','utf8');
vm.runInContext(app.slice(0,app.indexOf('function receiveServerSettings(')),live);
vm.runInContext(`member={id:2,notifications:'mentions',desktop_notifications:true};channelList=[{id:1,name:'general'}];loadChannels=async()=>{};showError=()=>{};`,live);
live.event = {...message,display_name:'Sender'};
vm.runInContext('receiveAlert(event);receiveAlert(event);',live);
assert.equal(delivered.length,1);
assert.equal(delivered[0].options.body,'Mentioned you');
assert.equal(vm.runInContext('mentionUnread.get(1).size',live),1);
assert.match(live.document.title,/1 mentions/);
vm.runInContext('removeMentionAlert({...event,mentions:[]});',live);
assert.equal(vm.runInContext('mentionUnread.size',live),0);
live.event = {...message,id:2};
vm.runInContext('member.notifications="none";receiveAlert(event);',live);
assert.equal(delivered.length,1);
vm.runInContext('member.notifications="all";member.desktop_notifications=false;receiveAlert({...event,id:3});',live);
assert.equal(delivered.length,1);
console.log('Mention and notification policy checks passed');
