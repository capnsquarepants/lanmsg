const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

let created = [];
class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.style = {setProperty(){}}; this.classList = {add(){}}; this.attributes = {}; created.push(this); }
  append(...children) { for(const child of children) this.insertBefore(child,null); }
  prepend(child) { this.insertBefore(child,this.children[0] || null); }
  insertBefore(child,reference) {
    if(child.parent) child.parent.children.splice(child.parent.children.indexOf(child),1);
    const index = reference === null ? this.children.length : this.children.indexOf(reference);
    this.children.splice(index,0,child); child.parent = this;
  }
  replaceChildren(...children) { for(const child of [...this.children]) child.remove?.(); this.children = []; this.append(...children); }
  setAttribute(key,value) { this.attributes[key] = value; }
  get childElementCount() { return this.children.length; }
  remove() { this.removed = true; if(this.parent) this.parent.children.splice(this.parent.children.indexOf(this),1); this.parent = null; }
}
const context = vm.createContext({URL, location:{protocol:'http:'}, document:{
  createElement:tag => new Element(tag), createTextNode:text => ({textContent:text})
}});
vm.runInContext(fs.readFileSync(path.join(__dirname,'../lanmsg/static/media.js'),'utf8'),context);
vm.runInContext(fs.readFileSync(path.join(__dirname,'../lanmsg/static/mentions.js'),'utf8'),context);
context.openUser = () => {};
context.scheduleViewportRead = () => {};
context.readBoundary = null; context.hasNewer = false;
const media = context.MediaPreviews;
for(const defaultValue of [false,true]) {
  assert.equal(media.enabled(null,defaultValue),defaultValue);
  assert.equal(media.enabled(true,defaultValue),true);
  assert.equal(media.enabled(false,defaultValue),false);
}
const id = 'dQw4w9WgXcQ';
for(const url of [`https://youtu.be/${id}?t=10`, `https://www.youtube.com/watch?v=${id}`, `https://m.youtube.com/shorts/${id}`, `https://youtube.com/embed/${id}`, `https://youtube.com/live/${id}`]) {
  assert.equal(media.classify(url).id,id);
}
for(const invalid of ['javascript:alert(1)', 'data:image/png;base64,test', 'file:///a.png', 'https://user:password@example.com/a.png', `https://youtube.com.evil/watch?v=${id}`, 'https://youtube.com/watch?v=invalid', 'https://example.com/page']) {
  assert.equal(media.classify(invalid),null);
}
assert.equal(media.classify('https://example.com/photo.JPG?size=1').kind,'image');
assert.equal(media.classify('https://example.com/video.webm').kind,'video');
assert.equal(media.tokens('(https://example.com/a.png).')[1].url,'https://example.com/a.png');
const body = new Element('div'); created = [];
media.appendPreviews(body,'https://example.com/a.png',false);
assert.equal(created.length,0); assert.equal(body.children.length,0);
media.appendText(body,'<script>plain text</script> https://example.com/a.png');
assert.equal(body.children[0].textContent,'<script>plain text</script> ');
assert.equal(body.children[1].rel,'noopener noreferrer');
created = [];
media.appendPreviews(body,`https://example.com/a.png https://example.com/a.png https://example.com/b.mp4 https://youtu.be/${id} https://example.com/extra.png`,true);
assert.equal(created.filter(element=>element.tag === 'figure').length,3);
const video = created.find(element=>element.tag === 'video');
assert.equal(video.controls,true); assert.equal(video.preload,'none'); assert.equal(video.autoplay,undefined);
const iframe = created.find(element=>element.tag === 'iframe');
assert.equal(iframe.src,`https://www.youtube-nocookie.com/embed/${id}`);
assert.equal(iframe.referrerPolicy,'strict-origin-when-cross-origin');
created.find(element=>element.tag === 'img').onerror();
assert.equal(created.find(element=>element.tag === 'img').removed,true);
assert.equal(created.at(-1).textContent,'Preview unavailable. Open the link instead.');
context.location.protocol = 'https:'; created = [];
media.appendPreviews(new Element('div'),'http://example.com/a.png',true);
assert.equal(created.some(element=>element.tag === 'img'),false);
// Incoming messages and edits to other rows must leave a playing iframe attached.
const roots = new Map();
context.$ = id => {
  if(!roots.has(id)) roots.set(id,new Element('div'));
  return roots.get(id);
};
Object.assign(context.$('messages'),{scrollHeight:100,scrollTop:0,clientHeight:100});
context.nicknameColor = () => '';
const app = fs.readFileSync(path.join(__dirname,'../lanmsg/static/app.js'),'utf8');
vm.runInContext(`let member={id:1,owner:true},messages=new Map(),messageRows=new Map(),dayRows=new Map(),profileUpdates=new Map(),editingMessage=null,savingMessage=false,editDraft='',editVersion=0,mediaPreviewsEnabled=true;`,context);
vm.runInContext(app.slice(app.indexOf('function messageAction('),app.indexOf('async function saveMessageEdit(')),context);
vm.runInContext(app.slice(app.indexOf('function render()'),app.indexOf('async function select(id)')),context);
created = [];
vm.runInContext(`messages.set(1,{id:1,user_id:1,username:'owner',display_name:'Owner',created:123,content:'https://youtu.be/${id}',version:1});render();`,context);
const originalFrame = created.find(element=>element.tag === 'iframe');
const originalParent = originalFrame.parent;
const contains = (node,target) => node === target || Boolean(node.children?.some(child=>contains(child,target)));
assert.equal(contains(context.$('message-list'),originalFrame),true);
created = [];
vm.runInContext(`messages.set(2,{id:2,user_id:1,username:'owner',display_name:'Owner',created:124,content:'New message',version:1});render();messages.set(2,{...messages.get(2),version:2,content:'Edited message'});render();`,context);
assert.equal(created.some(element=>element.tag === 'iframe'),false);
assert.equal(originalFrame.parent,originalParent);
assert.equal(originalFrame.removed,undefined);
assert.equal(contains(context.$('message-list'),originalFrame),true);
vm.runInContext('mediaPreviewsEnabled=false;render();',context);
assert.equal(contains(context.$('message-list'),originalFrame),false);
console.log('Media preferences, URL parsing, rendering, request gating, and fallback checks passed');
