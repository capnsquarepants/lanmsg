'use strict';
// Media loads in the browser; webpage metadata uses a preference-gated server API.
globalThis.MediaPreviews = (() => {
  let active = false, running = 0, timer = null, nextStart = 0;
  const queue = [], controllers = new Set(), cache = new Map();
  const observer = typeof IntersectionObserver === 'function' ? new IntersectionObserver(entries => {
    for(const entry of entries) if(entry.isIntersecting) {
      observer.unobserve(entry.target); schedule(entry.target,entry.target.previewURL);
    }
  }, {rootMargin:'200px'}) : null;
  function setEnabled(value) {
    active = value === true;
    if(!active) {
      queue.length = 0; observer?.disconnect();
      for(const controller of controllers) controller.abort();
      if(timer !== null) clearTimeout(timer);
      timer = null;
    }
  }
  function schedule(card,url) {
    if(!active || !card.isConnected || queue.length >= 100) return;
    queue.push({card,url}); pump();
  }
  function pump() {
    if(!active || !queue.length || running >= 2 || timer !== null) return;
    const wait = Math.max(0,nextStart - Date.now());
    timer = setTimeout(() => {
      timer = null;
      const job = queue.shift();
      if(!job || !active || !job.card.isConnected) { pump(); return; }
      nextStart = Date.now() + 1000; running++;
      populateCard(job.card,job.url).finally(() => { running--; pump(); });
      pump();
    },wait);
  }
  async function populateCard(card,url) {
    const controller = new AbortController(); controllers.add(controller);
    const deadline = setTimeout(() => controller.abort(),8000);
    try {
      let metadata = cache.get(url);
      if(!metadata || metadata.expires < Date.now()) {
        let response;
        for(let attempt = 0; attempt < 2; attempt++) {
          if(!active || controller.signal.aborted || !card.isConnected) return;
          response = await fetch('/api/previews',{method:'POST',headers:{'Content-Type':'application/json','X-LANmsg':'1'},body:JSON.stringify({url}),signal:controller.signal});
          if(response.ok || ![429,503].includes(response.status) || attempt === 1) break;
          await new Promise(resolve => setTimeout(resolve,1000));
        }
        if(!response.ok) throw new Error('Preview unavailable');
        metadata = {data:await response.json(),expires:Date.now() + 600000};
        cache.set(url,metadata);
        if(cache.size > 256) cache.delete(cache.keys().next().value);
      }
      if(!active || controller.signal.aborted || !card.isConnected) return;
      const data = metadata.data, text = document.createElement('div'); text.className = 'link-card-text';
      const site = document.createElement('small'); site.textContent = data.site_name;
      const title = link(url,data.title); title.className = 'link-card-title';
      const description = document.createElement('p'); description.textContent = data.description;
      text.append(site,title,description); card.replaceChildren(text);
      const imageURL = data.image && safeURL(data.image);
      if(imageURL && !(location.protocol === 'https:' && imageURL.protocol === 'http:')) {
        const image = document.createElement('img'); image.alt = ''; image.loading = 'lazy'; image.referrerPolicy = 'no-referrer';
        image.onerror = () => image.remove(); image.src = imageURL.href; card.prepend(image);
      }
    } catch {
      if(active && !controller.signal.aborted && card.isConnected) {
        const fallback = document.createElement('figcaption'); fallback.append(link(url,'Open ' + new URL(url).hostname));
        card.replaceChildren(fallback);
      }
    } finally { clearTimeout(deadline); controllers.delete(controller); }
  }
  function enabled(preference, serverDefault) {
    return typeof preference === 'boolean' ? preference : serverDefault === true;
  }
  function safeURL(value) {
    try {
      const url = new URL(value);
      return ['http:', 'https:'].includes(url.protocol) && !url.username && !url.password ? url : null;
    } catch { return null; }
  }
  function classify(value) {
    const url = safeURL(value);
    if(!url) return null;
    const host = url.hostname.toLowerCase();
    let id;
    if(host === 'youtu.be') id = url.pathname.split('/')[1];
    else if(['youtube.com','www.youtube.com','m.youtube.com','music.youtube.com'].includes(host)) {
      if(url.pathname === '/watch') id = url.searchParams.get('v');
      else if(/^\/(shorts|embed|live)\//.test(url.pathname)) id = url.pathname.split('/')[2];
    }
    if(id && /^[a-zA-Z0-9_-]{11}$/.test(id)) return {kind:'youtube', url:url.href, id};
    if(/\.(png|jpe?g|gif|webp|avif)$/i.test(url.pathname)) return {kind:'image', url:url.href};
    if(/\.(mp4|webm|ogg)$/i.test(url.pathname)) return {kind:'video', url:url.href};
    return null;
  }
  function tokens(text) {
    const result = []; let cursor = 0;
    for(const match of text.matchAll(/https?:\/\/[^\s<>"`]+/gi)) {
      let value = match[0].replace(/[.,!?;:]+$/, '');
      while(value.endsWith(')') && (value.match(/\)/g)?.length || 0) > (value.match(/\(/g)?.length || 0)) value = value.slice(0,-1);
      const url = safeURL(value);
      if(!url) continue;
      result.push({text:text.slice(cursor,match.index)}, {text:value,url:url.href});
      cursor = match.index + value.length;
    }
    result.push({text:text.slice(cursor)}); return result;
  }
  function link(url, label) {
    const anchor = document.createElement('a'); anchor.href = url; anchor.textContent = label;
    anchor.target = '_blank'; anchor.rel = 'noopener noreferrer'; return anchor;
  }
  function appendText(body, text) {
    for(const token of tokens(text)) body.append(token.url ? link(token.url, token.text) : document.createTextNode(token.text));
  }
  function appendPreviews(body, text, allow, maximum = 3) {
    if(!allow || maximum <= 0) return; // Do not create src attributes or initiate requests when disabled.
    const previews = document.createElement('div'); previews.className = 'media-previews';
    const seen = new Set();
    for(const token of tokens(text)) {
      if(!token.url) continue;
      const media = classify(token.url) || {kind:'link',url:token.url};
      if(seen.has(media.kind === 'youtube' ? media.id : media.url)) continue;
      if(media.kind !== 'link' && location.protocol === 'https:' && media.url.startsWith('http:')) continue;
      seen.add(media.kind === 'youtube' ? media.id : media.url);
      const figure = document.createElement('figure'); figure.className = 'media-preview';
      if(media.kind === 'link') {
        figure.classList.add('link-card'); figure.previewURL = media.url;
        const caption = document.createElement('figcaption'); caption.append(link(media.url,'Open ' + new URL(media.url).hostname)); figure.append(caption);
        previews.append(figure);
        if(observer) observer.observe(figure);
        else setTimeout(() => schedule(figure,media.url),0);
        if(seen.size >= maximum) break;
        continue;
      }
      let element;
      if(media.kind === 'youtube') {
        element = document.createElement('iframe'); element.title = 'YouTube video preview';
        element.loading = 'lazy'; element.referrerPolicy = 'strict-origin-when-cross-origin';
        element.allow = 'encrypted-media; picture-in-picture; fullscreen'; element.allowFullscreen = true;
        element.src = 'https://www.youtube-nocookie.com/embed/' + media.id;
      } else if(media.kind === 'image') {
        element = document.createElement('img'); element.alt = 'Image preview'; element.loading = 'lazy'; element.decoding = 'async';
        element.referrerPolicy = 'no-referrer'; element.src = media.url;
      } else {
        element = document.createElement('video'); element.controls = true; element.preload = 'none'; element.playsInline = true;
        element.src = media.url;
      }
      if(media.kind !== 'youtube') element.onerror = () => {
        element.remove(); const error = document.createElement('span'); error.className = 'media-error';
        error.textContent = 'Preview unavailable. Open the link instead.'; figure.prepend(error);
      };
      const caption = document.createElement('figcaption'); caption.append(link(media.url, 'Open ' + (media.kind === 'youtube' ? 'on YouTube' : new URL(media.url).hostname)));
      figure.append(element,caption); previews.append(figure);
      if(seen.size >= maximum) break;
    }
    if(previews.childElementCount) body.append(previews);
  }
  return Object.freeze({enabled, setEnabled, safeURL, classify, tokens, appendText, appendPreviews});
})();
