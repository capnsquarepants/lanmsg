'use strict';
const $ = id => document.getElementById(id);
let member, channel = 1, channelList = [], registering = false, socket, heartbeat, reconnectTimer;
let generation = 0, messages = new Map(), pendingSend, loading = false;
const unread = new Set();
async function api(path, options = {}) {
  const response = await fetch('/api' + path, {...options, headers: {'Content-Type':'application/json', 'X-LANmsg':'1'}});
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Check the supplied fields.');
  return data;
}
function authMode() {
  $('invite-label').hidden = !registering;
  $('submit-auth').textContent = registering ? 'Create account' : 'Sign in';
  $('toggle-auth').textContent = registering ? 'Already have an account? Sign in' : 'Create an account';
  $('password').autocomplete = registering ? 'new-password' : 'current-password';
}
$('toggle-auth').onclick = () => { registering = !registering; authMode(); };
$('login').onsubmit = async event => {
  event.preventDefault(); $('auth-error').textContent = ''; $('submit-auth').disabled = true;
  try {
    member = await api(registering ? '/register' : '/login', {method:'POST', body:JSON.stringify({username:$('username').value, password:$('password').value, invite:$('invite').value})});
    $('password').value = ''; await start();
  } catch(error) { $('auth-error').textContent = error.message; }
  finally { $('submit-auth').disabled = false; }
};
async function loadChannels() {
  channelList = await api('/channels');
  $('channels').replaceChildren(...channelList.filter(c => c.kind === 'text').map(c => {
    const button = document.createElement('button'); button.textContent = '# ' + c.name;
    button.className = (c.id === channel ? 'active ' : '') + (unread.has(c.id) ? 'unread' : '');
    button.onclick = () => select(c.id).catch(showError); return button;
  }));
}
function showError(error) { $('chat-error').textContent = error.message; }
function render() {
  const scroller = $('messages'), nearBottom = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 100;
  $('message-list').replaceChildren(...[...messages.values()].sort((a,b) => a.id-b.id).map(m => {
    const item = document.createElement('article'); item.className = 'message';
    const avatar = document.createElement('div'); avatar.className = 'avatar'; avatar.textContent = m.username[0].toUpperCase();
    const main = document.createElement('div'), meta = document.createElement('div'); meta.className = 'meta';
    const name = document.createElement('strong'); name.textContent = m.username;
    const date = document.createElement('time'); date.dateTime = new Date(m.created*1000).toISOString(); date.textContent = new Date(m.created*1000).toLocaleString();
    const body = document.createElement('div'); body.className = 'body'; body.textContent = m.content;
    meta.append(name,date); main.append(meta,body); item.append(avatar,main); return item;
  }));
  $('empty').hidden = messages.size > 0;
  if (nearBottom) scroller.scrollTop = scroller.scrollHeight;
}
async function select(id) {
  channel = id; const current = ++generation; messages.clear(); unread.delete(id);
  const name = channelList.find(c => c.id === id)?.name || 'general';
  $('channel-title').textContent = '# ' + name; $('content').placeholder = 'Message #' + name;
  $('older').hidden = false; await loadChannels();
  const history = await api(`/channels/${id}/messages`);
  if (current !== generation) return;
  history.forEach(m => messages.set(m.id,m)); render(); $('older').hidden = history.length < 50;
  $('messages').scrollTop = $('messages').scrollHeight;
}
$('older').onclick = async () => {
  if (loading || !messages.size) return;
  loading = true; const current = generation, scroller = $('messages'), height = scroller.scrollHeight, top = scroller.scrollTop;
  try {
    const rows = await api(`/channels/${channel}/messages?before=${Math.min(...messages.keys())}`);
    if (current !== generation) return;
    rows.forEach(m => messages.set(m.id,m)); render(); scroller.scrollTop = top + scroller.scrollHeight - height;
    $('older').hidden = rows.length < 50;
  } catch(error) { showError(error); } finally { loading = false; }
};
function connect() {
  clearTimeout(reconnectTimer);
  socket = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws`);
  socket.onopen = async () => {
    $('connection').textContent = 'Connected';
    heartbeat = setInterval(() => { if(socket.readyState === WebSocket.OPEN) socket.send('ping'); },20000);
    try { await select(channel); } catch(error) { showError(error); }
  };
  socket.onmessage = event => {
    const data = JSON.parse(event.data);
    if(data.type === 'channels') loadChannels().catch(showError);
    if(data.type === 'message') {
      const m = data.message;
      if(m.channel_id === channel) { messages.set(m.id,m); render(); }
      else { unread.add(m.channel_id); loadChannels().catch(showError); }
    }
  };
  socket.onclose = () => { clearInterval(heartbeat); $('connection').textContent = 'Reconnecting…'; reconnectTimer = setTimeout(connect,2000); };
}
async function start() {
  $('auth').hidden = true; $('chat').hidden = false; $('identity').textContent = member.username; $('admin').hidden = !member.owner;
  await loadChannels(); await select(channel); connect();
}
$('composer').onsubmit = async event => {
  event.preventDefault(); const content = $('content').value.trim(); if(!content) return;
  $('send').disabled = true; $('chat-error').textContent = '';
  // Keep the same nonce after an ambiguous network failure, so retrying cannot duplicate a message.
  if(!pendingSend || pendingSend.content !== content || pendingSend.channel !== channel) pendingSend = {content,channel,nonce:globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}`};
  const attempt = pendingSend;
  try {
    const m = await api(`/channels/${attempt.channel}/messages`, {method:'POST', body:JSON.stringify(attempt)});
    if(channel === m.channel_id) { messages.set(m.id,m); render(); }
    if($('content').value.trim() === attempt.content) $('content').value = '';
    pendingSend = null;
  } catch(error) { showError(new Error('Message not confirmed. Press Send to retry. ' + error.message)); }
  finally { $('send').disabled = false; $('content').focus(); }
};
$('content').onkeydown = event => { if(event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); if(!$('send').disabled) $('composer').requestSubmit(); } };
$('add-channel').onclick = async () => { const name = prompt('Channel name (lowercase letters, numbers, hyphens or underscores)'); if(!name) return; try { await api('/admin/channels',{method:'POST',body:JSON.stringify({name})}); await loadChannels(); } catch(error) { showError(error); } };
$('invite-button').onclick = async () => { try { const result = await api('/admin/invites',{method:'POST'}); prompt('Single-use invite code — copy and share with your friend:',result.invite); } catch(error) { showError(error); } };
$('logout').onclick = async () => { try { await api('/session/logout',{method:'POST'}); socket.onclose = null; socket.close(); clearInterval(heartbeat); clearTimeout(reconnectTimer); location.reload(); } catch(error) { showError(error); } };
(async () => { try { member = await api('/me'); await start(); } catch { try { const status = await api('/status'); if(status.setup) { registering = true; authMode(); $('invite-label').hidden = true; $('auth-hint').textContent = 'Create the first account to become the server owner.'; } } catch { $('auth-error').textContent = 'Cannot reach the server.'; } } })();
