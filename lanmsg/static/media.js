'use strict';
// Only browser requests are used: the chat server never fetches linked media.
globalThis.MediaPreviews = (() => {
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
  function appendPreviews(body, text, allow) {
    if(!allow) return; // Do not create src attributes or initiate requests when disabled.
    const previews = document.createElement('div'); previews.className = 'media-previews';
    const seen = new Set();
    for(const token of tokens(text)) {
      if(!token.url) continue;
      const media = classify(token.url);
      if(!media || seen.has(media.kind === 'youtube' ? media.id : media.url)) continue;
      if(location.protocol === 'https:' && media.url.startsWith('http:')) continue;
      seen.add(media.kind === 'youtube' ? media.id : media.url);
      const figure = document.createElement('figure'); figure.className = 'media-preview';
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
      if(seen.size === 3) break;
    }
    if(previews.childElementCount) body.append(previews);
  }
  return Object.freeze({enabled, safeURL, classify, tokens, appendText, appendPreviews});
})();
