const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

module.exports = async function checkLinkCards() {
  const timers = new Map(), created = [], observed = [], requests = [];
  let nextTimer = 0, observerCallback, responseJSON;
  class Element {
    constructor(tag) { this.tag = tag; this.children = []; this.classList = {add(){}}; this.isConnected = true; created.push(this); }
    append(...children) { this.children.push(...children); }
    prepend(child) { this.children.unshift(child); }
    replaceChildren(...children) { this.children = children; }
    get childElementCount() { return this.children.length; }
    remove() { this.isConnected = false; }
  }
  const context = vm.createContext({URL, AbortController, Date, location:{protocol:'https:'},
    setTimeout:callback => { const id = ++nextTimer; timers.set(id,callback); return id; },
    clearTimeout:id => timers.delete(id),
    IntersectionObserver:class {
      constructor(callback) { observerCallback = callback; }
      observe(element) { observed.push(element); }
      unobserve() {}
      disconnect() {}
    },
    document:{createElement:tag => new Element(tag),createTextNode:text => ({textContent:text})},
    fetch:async (url,options) => {
      requests.push({url,options});
      return {ok:true,json:() => new Promise(resolve=>{responseJSON = resolve;})};
    }
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../lanmsg/static/media.js'),'utf8'),context);
  const previews = context.MediaPreviews, body = new Element('div');
  previews.setEnabled(false);
  previews.appendPreviews(body,'https://example.com/page',false);
  assert.equal(observed.length,0); assert.equal(requests.length,0);
  previews.setEnabled(true);
  previews.appendPreviews(body,'https://example.com/page https://example.com/page https://example.com/a.jpg https://example.com/movie.mp4 https://example.com/fourth',true);
  assert.equal(body.children[0].children.length,3); // Shared cap, including media.
  assert.equal(requests.length,0); // Metadata is lazy until the card enters view.
  const card = observed[0]; observerCallback([{target:card,isIntersecting:true}]);
  const runNext = () => {
    const [id,callback] = timers.entries().next().value; timers.delete(id); callback();
  };
  runNext();
  for(let i=0;i<5;i++) await Promise.resolve();
  assert.equal(requests.length,1);
  assert.equal(requests[0].url,'/api/previews');
  assert.equal(requests[0].options.headers['X-LANmsg'],'1');
  responseJSON({title:'<script>safe text</script>',description:'Summary',site_name:'Example',image:'https://example.com/thumbnail.jpg'});
  for(let i=0;i<5;i++) await Promise.resolve();
  assert.equal(card.children[0].tag,'img');
  assert.equal(card.children[1].children[1].textContent,'<script>safe text</script>');
  assert.equal(card.children[1].children[1].href,'https://example.com/page');
  // A late metadata response must not attach a thumbnail after opt-out.
  const second = new Element('div');
  previews.appendPreviews(second,'https://another.example/page',true);
  const lateCard = observed.at(-1); observerCallback([{target:lateCard,isIntersecting:true}]);
  runNext(); for(let i=0;i<5;i++) await Promise.resolve();
  const lateResponse = responseJSON;
  previews.setEnabled(false);
  assert.equal(requests.at(-1).options.signal.aborted,true);
  lateResponse({title:'Late',description:'No preview',site_name:'Other',image:'https://another.example/image.jpg'});
  for(let i=0;i<5;i++) await Promise.resolve();
  assert.equal(lateCard.children.some(element=>element.tag === 'img'),false);
  const count = requests.length;
  observerCallback([{target:lateCard,isIntersecting:true}]);
  assert.equal(requests.length,count);
  console.log('URL card lazy loading, shared cap, safe text, and opt-out cancellation passed');
};
if(require.main === module) module.exports().catch(error=>{console.error(error);process.exitCode = 1;});
