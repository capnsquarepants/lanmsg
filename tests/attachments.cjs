const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
class Element {
  constructor(tag) { this.tag = tag; this.children=[]; }
  append(...children) { this.children.push(...children); }
  remove() { this.removed=true; }
}
const context = vm.createContext({document:{createElement:tag => new Element(tag)}});
vm.runInContext(fs.readFileSync(__dirname + '/../lanmsg/static/attachments.js','utf8'),context);
const A = context.Attachments;
const file = {id:'a'.repeat(32),filename:'<script>photo.png',size:500,media_type:'image/png',url:'https://evil.example/'};
let body = new Element('div');
assert.equal(A.append(body,[file],false),0);
assert.equal(body.children[0].children.length,1);
assert.equal(body.children[0].children[0].href,'/api/attachments/' + file.id);
assert.equal(body.children[0].children[0].textContent,'<script>photo.png (1 KB)');
body = new Element('div');
assert.equal(A.append(body,[file],true),1);
const image = body.children[0].children[1];
assert.equal(image.src,'/api/attachments/' + file.id + '?inline=true');
assert.equal(image.loading,'lazy'); image.onerror(); assert.equal(image.removed,true);
body = new Element('div');
A.append(body,[{...file,media_type:'video/mp4'}],true);
const video = body.children[0].children[1];
assert.equal(video.controls,true); assert.equal(video.preload,'none'); assert.equal(video.autoplay,undefined);
body = new Element('div');
assert.equal(A.append(body,[{...file,media_type:'text/html'}],true),0);
assert.equal(A.url({...file,id:'../secret'}),null);
assert.equal(A.url({...file,id:'a'.repeat(32) + '\n'}),null);
console.log('Attachment download safety, preference gates, and media defaults passed');
