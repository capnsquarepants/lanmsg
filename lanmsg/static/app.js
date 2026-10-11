'use strict';
const $ = id => document.getElementById(id);
let member, channel = 1, channelList = [], registering = false, socket, heartbeat, reconnectTimer;
let generation = 0, messages = new Map(), pendingSend, loading = false;
const unread = new Set();
const DEFAULT_THEME = Object.freeze({background:'#101419', sidebar:'#171d24', panel:'#1c242e', text:'#e2e8f0', muted:'#94a3b8', accent:'#72d6bb', border:'#303b48', nickname:'#91b9f5', font:'mono', font_size:14, line_height:1.6, row_spacing:3, corner_radius:6});
const COLOR_LABELS = {background:'Chat background', sidebar:'Sidebar & status bar', panel:'Inputs & selection', text:'Message text', muted:'Secondary text', accent:'Accent', border:'Borders', nickname:'Nicknames'};
const PRESETS = {
  default: DEFAULT_THEME,
  classic: {...DEFAULT_THEME, background:'#0b120d', sidebar:'#101b13', panel:'#192a1d', text:'#c4e5cb', muted:'#86a58c', accent:'#83e69a', border:'#304b36', nickname:'#b9d99b', corner_radius:2},
  light: {...DEFAULT_THEME, background:'#f7f9fc', sidebar:'#edf1f6', panel:'#e2e9f1', text:'#233247', muted:'#546579', accent:'#176a58', border:'#c5d0dd', nickname:'#315d9c'}
};
let savedTheme = {...DEFAULT_THEME}, savingTheme = false;
function applyTheme(theme) {
  const root = document.documentElement;
  for (const key of Object.keys(COLOR_LABELS)) root.style.setProperty('--' + key, theme[key]);
  root.style.setProperty('--font-family', theme.font === 'sans' ? '"Segoe UI", system-ui, sans-serif' : 'Consolas, "Cascadia Mono", "SFMono-Regular", monospace');
  for (const key of ['font_size','row_spacing','corner_radius']) root.style.setProperty('--' + key.replaceAll('_','-'), theme[key] + 'px');
  root.style.setProperty('--line-height', theme.line_height);
  const rgb = theme.background.slice(1).match(/../g).map(hex => parseInt(hex,16));
  root.style.colorScheme = rgb[0]*.299 + rgb[1]*.587 + rgb[2]*.114 > 150 ? 'light' : 'dark';
}
function receiveTheme(theme) {
  savedTheme = {...theme};
  if (!$('appearance-dialog').open) applyTheme(savedTheme);
}
async function loadAppearance() { receiveTheme(await api('/appearance')); }
function fillThemeForm(theme) {
  const form = $('appearance-form');
  for (const [key,value] of Object.entries(theme)) form.elements.namedItem(key).value = value;
  updateColorLabels();
}
function updateColorLabels() {
  for (const key of Object.keys(COLOR_LABELS)) $('color-value-' + key).textContent = $('appearance-form').elements.namedItem(key).value;
}
function readThemeForm() {
  const form = $('appearance-form'), theme = {};
  for (const key of Object.keys(DEFAULT_THEME)) {
    const value = form.elements.namedItem(key).value;
    theme[key] = typeof DEFAULT_THEME[key] === 'number' ? Number(value) : value;
  }
  return theme;
}
for (const [key,title] of Object.entries(COLOR_LABELS)) {
  const label = document.createElement('label'); label.textContent = title;
  const control = document.createElement('span'); control.className = 'color-control';
  const input = document.createElement('input'); input.type = 'color'; input.name = key;
  const value = document.createElement('code'); value.id = 'color-value-' + key;
  control.append(input,value); label.append(control); $('theme-colors').append(label);
}
$('appearance-button').onclick = async () => {
  try {
    await loadAppearance(); fillThemeForm(savedTheme); $('theme-preset').value = 'custom';
    $('appearance-error').textContent = ''; $('appearance-dialog').showModal();
  } catch(error) { showError(error); }
};
$('appearance-form').oninput = event => {
  if (event.target.id === 'theme-preset') return;
  $('theme-preset').value = 'custom'; updateColorLabels();
  if ($('appearance-form').checkValidity()) applyTheme(readThemeForm());
};
$('theme-preset').onchange = () => {
  const theme = PRESETS[$('theme-preset').value];
  if(theme) { fillThemeForm(theme); applyTheme(theme); }
};
$('appearance-reset').onclick = () => { fillThemeForm(DEFAULT_THEME); applyTheme(DEFAULT_THEME); $('theme-preset').value = 'default'; };
function closeAppearance() { if (!savingTheme) $('appearance-dialog').close(); }
$('appearance-close').onclick = closeAppearance;
$('appearance-cancel').onclick = closeAppearance;
$('appearance-dialog').onclose = () => applyTheme(savedTheme);
$('appearance-dialog').oncancel = event => { if(savingTheme) event.preventDefault(); };
$('appearance-form').onsubmit = async event => {
  event.preventDefault(); if(savingTheme) return;
  savingTheme = true; $('appearance-save').disabled = true; $('appearance-error').textContent = '';
  try {
    receiveTheme(await api('/admin/appearance', {method:'PUT', body:JSON.stringify(readThemeForm())}));
    $('appearance-dialog').close();
  } catch(error) { $('appearance-error').textContent = error.message; }
  finally { savingTheme = false; $('appearance-save').disabled = false; }
};
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
    const button = document.createElement('button'); button.textContent = '#' + c.name;
    button.className = (c.id === channel ? 'active ' : '') + (unread.has(c.id) ? 'unread' : '');
    if (c.id === channel) button.setAttribute('aria-current','page');
    button.onclick = () => select(c.id).catch(showError); return button;
  }));
}
function showError(error) { $('chat-error').textContent = error.message; }
function render() {
  const scroller = $('messages'), nearBottom = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 100;
  const rows = []; let previousDay;
  for (const m of [...messages.values()].sort((a,b) => a.id-b.id)) {
    const timestamp = new Date(m.created*1000), day = timestamp.toDateString();
    if(day !== previousDay) {
      const divider = document.createElement('div'); divider.className = 'day-divider';
      divider.textContent = timestamp.toLocaleDateString(undefined,{weekday:'short',year:'numeric',month:'short',day:'numeric'});
      rows.push(divider); previousDay = day;
    }
    const item = document.createElement('article'); item.className = 'message' + (m.user_id === member?.id ? ' self' : '');
    const name = document.createElement('strong'); name.className = 'nickname'; name.textContent = '<' + m.username + '>'; name.title = m.username;
    const date = document.createElement('time'); date.dateTime = timestamp.toISOString(); date.textContent = timestamp.toLocaleTimeString(undefined,{hour:'2-digit',minute:'2-digit',hour12:false}); date.title = timestamp.toLocaleString();
    const body = document.createElement('div'); body.className = 'body'; body.textContent = m.content;
    item.append(date,name,body); rows.push(item);
  }
  $('message-list').replaceChildren(...rows);
  $('empty').hidden = messages.size > 0;
  if (nearBottom) scroller.scrollTop = scroller.scrollHeight;
}
async function select(id) {
  channel = id; const current = ++generation; messages.clear(); unread.delete(id);
  const name = channelList.find(c => c.id === id)?.name || 'general';
  $('channel-title').textContent = '#' + name; $('status-channel').textContent = '#' + name; $('content').placeholder = 'Message #' + name;
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
    $('connection').textContent = 'Connected'; $('connection').dataset.state = 'online';
    heartbeat = setInterval(() => { if(socket.readyState === WebSocket.OPEN) socket.send('ping'); },20000);
    try { await loadAppearance(); await select(channel); } catch(error) { showError(error); }
  };
  socket.onmessage = event => {
    const data = JSON.parse(event.data);
    if(data.type === 'appearance') receiveTheme(data.appearance);
    if(data.type === 'channels') loadChannels().catch(showError);
    if(data.type === 'message') {
      const m = data.message;
      if(m.channel_id === channel) { messages.set(m.id,m); render(); }
      else { unread.add(m.channel_id); loadChannels().catch(showError); }
    }
  };
  socket.onclose = () => { clearInterval(heartbeat); $('connection').textContent = 'Reconnecting…'; $('connection').dataset.state = 'offline'; reconnectTimer = setTimeout(connect,2000); };
}
async function start() {
  $('auth').hidden = true; $('chat').hidden = false; $('identity').textContent = member.username; $('admin').hidden = !member.owner;
  $('composer-nick').textContent = '<' + member.username + '>';
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
(async () => {
  try { await loadAppearance(); } catch { /* CSS defaults remain usable if theme loading fails. */ }
  try { member = await api('/me'); await start(); } catch { try { const status = await api('/status'); if(status.setup) { registering = true; authMode(); $('invite-label').hidden = true; $('auth-hint').textContent = 'Create the first account to become the server owner.'; } } catch { $('auth-error').textContent = 'Cannot reach the server.'; } }
})();
