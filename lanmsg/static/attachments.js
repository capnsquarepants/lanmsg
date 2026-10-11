'use strict';
globalThis.Attachments = (() => {
  function url(file, inline = false) {
    return typeof file.id === 'string' && file.id.length === 32 && /^[a-f0-9]{32}$/.test(file.id) ? '/api/attachments/' + file.id + (inline ? '?inline=true' : '') : null;
  }
  function size(bytes) { return bytes >= 1048576 ? (bytes / 1048576).toFixed(1) + ' MB' : Math.max(1,Math.ceil(bytes / 1024)) + ' KB'; }
  function append(body, files, allow) {
    let previews = 0;
    for(const file of (files || []).slice(0,3)) {
      const href = url(file); if(!href) continue;
      const wrapper = document.createElement('div'); wrapper.className = 'attachment';
      const link = document.createElement('a'); link.href = href; link.textContent = file.filename + ' (' + size(file.size) + ')'; link.download = file.filename;
      wrapper.append(link);
      if(allow && ['image/png','image/jpeg','image/gif','image/webp','video/mp4','video/webm'].includes(file.media_type)) {
        const video = file.media_type.startsWith('video/'), media = document.createElement(video ? 'video' : 'img');
        media.className = 'attachment-preview';
        if(video) { media.controls = true; media.preload = 'none'; media.playsInline = true; }
        else { media.alt = file.filename; media.loading = 'lazy'; }
        media.onerror = () => media.remove(); media.src = url(file,true); wrapper.append(media); previews++;
      }
      body.append(wrapper);
    }
    return previews;
  }
  return Object.freeze({url,size,append});
})();
