'use strict';
globalThis.Mentions = (() => {
  function matches(text) {
    return text.matchAll(/(?<![\w@/])@([a-zA-Z0-9_-]{2,32})(?![\w-])/g);
  }
  function append(body, text, mentions, openUser) {
    const users = new Map((mentions || []).map(user => [user.username.toLowerCase(),user]));
    for(const token of MediaPreviews.tokens(text)) {
      if(token.url) { MediaPreviews.appendText(body,token.text); continue; }
      let cursor = 0;
      for(const match of matches(token.text)) {
        const user = users.get(match[1].toLowerCase()); if(!user) continue;
        body.append(document.createTextNode(token.text.slice(cursor,match.index)));
        const button = document.createElement('button'); button.type = 'button'; button.className = 'mention';
        button.textContent = match[0]; button.title = 'View @' + user.username; button.onclick = () => openUser(user.id);
        body.append(button); cursor = match.index + match[0].length;
      }
      body.append(document.createTextNode(token.text.slice(cursor)));
    }
  }
  function isMention(message, id) { return !message.deleted && (message.mentions || []).some(user => user.id === id); }
  function shouldAlert(message, member, channel, focused) {
    return !message.deleted && message.user_id !== member.id && !(message.channel_id === channel && focused)
      && (member.notifications === 'all' || (member.notifications !== 'none' && isMention(message,member.id)));
  }
  function suggestions(text, caret, users) {
    const match = text.slice(0,caret).match(/(?:^|\s)@([a-zA-Z0-9_-]{0,32})$/);
    if(!match) return null;
    return {start:caret-match[1].length-1, end:caret, users:users.filter(user => user.username.toLowerCase().startsWith(match[1].toLowerCase())).slice(0,8)};
  }
  return Object.freeze({append,isMention,shouldAlert,suggestions});
})();
