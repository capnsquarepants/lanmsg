'use strict';
const $ = id => document.getElementById(id);
let member, channel = 1, channelList = [], registering = false, socket, heartbeat, reconnectTimer;
let generation = 0, messages = new Map(), pendingSend, loading = false;
const seenEvents = new Set();
const readStates = new Map();
let readFetch = null, readDirty = false, readRefreshTimer, viewportTimer, readSaving = false;
let readBoundary = null, hasNewer = false, selecting = false, mentionsCursor = null, inboxRequest = 0;
const unreadDivider = document.createElement('div'); unreadDivider.className = 'new-messages-divider'; unreadDivider.textContent = 'New messages';
let directory = [], directoryFetched = 0, directoryFetching = false, suggestionIndex = 0, suggestionState = null;
const messageRows = new Map(), dayRows = new Map();
let serverMediaPreviews = false;
let editingMessage = null, editDraft = '', editVersion = 0, savingMessage = false;
let savingAdmin = false, activeAdminSection = 'settings';
const LIMIT_LABELS = {connections:'Total connections', connections_per_ip:'Connections per IP', connections_per_session:'Connections per session', http_concurrency:'Concurrent API requests', message_rate:'Messages per second / account', message_burst:'Message burst / account'};
const desktopAlerts = new Map();
function updateAlertTitle() {
  const count = [...readStates.values()].reduce((sum,state) => sum + state.mentions,0);
  document.title = (count ? '(' + count + ' mentions) ' : '') + ($('server-name').textContent || 'LANmsg') + ' · LANmsg';
}
function notificationStatus() {
  const supported = globalThis.isSecureContext && 'Notification' in globalThis;
  $('notification-permission').disabled = !supported;
  $('notification-status').textContent = !supported ? 'Desktop alerts need a supported browser and HTTPS (or localhost). Channel badges still work.' : 'Device permission: ' + Notification.permission + '. Save your profile to apply your preferences.';
}
$('notification-permission').onclick = async () => {
  try { if(globalThis.isSecureContext && 'Notification' in globalThis) await Notification.requestPermission(); }
  catch(error) { $('display-name-error').textContent = error.message; }
  notificationStatus();
};
function receiveAlert(message) {
  if(!member || seenEvents.has(message.id)) return;
  seenEvents.add(message.id); if(seenEvents.size > 2000) seenEvents.delete(seenEvents.values().next().value);
  const focused = document.visibilityState === 'visible' && document.hasFocus();
  scheduleReadRefresh();
  if(!Mentions.shouldAlert(message,member,channel,focused) || !member.desktop_notifications || !globalThis.isSecureContext || !('Notification' in globalThis) || Notification.permission !== 'granted') return;
  // Visible channels already have their own badges; avoid OS alerts while using the app.
  if(focused) return;
  try {
    const name = channelList.find(c => c.id === message.channel_id)?.name || 'channel';
    const alert = new Notification((message.display_name || message.username) + ' in #' + name,{body:Mentions.isMention(message,member.id) ? 'Mentioned you' : 'New message',tag:'lanmsg-' + message.channel_id});
    desktopAlerts.get(message.channel_id)?.close(); alert.messageId = message.id; desktopAlerts.set(message.channel_id,alert);
    alert.onclick = () => { window.focus(); select(message.channel_id).catch(showError); alert.close(); };
    setTimeout(() => { alert.close(); if(desktopAlerts.get(message.channel_id) === alert) desktopAlerts.delete(message.channel_id); },15000);
  } catch { /* Embedded browsers may expose the API without supporting its constructor. */ }
}
function removeMentionAlert(message) {
  scheduleReadRefresh();
  if(!Mentions.isMention(message,member?.id) && desktopAlerts.get(message.channel_id)?.messageId === message.id) {
    desktopAlerts.get(message.channel_id).close(); desktopAlerts.delete(message.channel_id);
  }
}
function clearCurrentAlerts() {
  if(!member || document.visibilityState !== 'visible' || !document.hasFocus()) return;
  scheduleViewportRead(); scheduleReadRefresh();
  desktopAlerts.get(channel)?.close(); desktopAlerts.delete(channel);
}
function scheduleReadRefresh() {
  if(readRefreshTimer) return;
  readRefreshTimer = setTimeout(() => { readRefreshTimer = null; refreshReadState().catch(showError); },250);
}
async function refreshReadState() {
  if(readFetch) { readDirty = true; return readFetch; }
  readFetch = (async () => {
    do {
      readDirty = false;
      const data = await api('/read-state'); readStates.clear();
      for(const state of data.channels) readStates.set(state.id,state);
      updateAlertTitle(); drawChannels();
    } while(readDirty);
  })();
  try { await readFetch; } finally { readFetch = null; }
}
function scheduleViewportRead() {
  clearTimeout(viewportTimer); viewportTimer = setTimeout(markViewportRead,700);
}
async function markViewportRead() {
  if(!member || selecting || readSaving || document.visibilityState !== 'visible' || !document.hasFocus() || document.querySelector('dialog[open]')) return;
  const box = $('messages').getBoundingClientRect(); let position = 0;
  for(const row of $('message-list').querySelectorAll('article.message')) {
    const rect = row.getBoundingClientRect();
    if(rect.top < box.bottom && rect.bottom <= box.bottom && rect.bottom > box.top) position = Math.max(position,Number(row.id.slice(8)));
  }
  if(!position || position <= (readStates.get(channel)?.read_id || 0)) return;
  const currentChannel = channel; let saved = false; readSaving = true;
  try {
    await api('/channels/' + currentChannel + '/read',{method:'PUT',body:JSON.stringify({message_id:position})}); saved = true; await refreshReadState();
  } catch(error) { showError(error); }
  finally { readSaving = false; if(saved || currentChannel !== channel) scheduleViewportRead(); }
}
$('messages').addEventListener('scroll',scheduleViewportRead);
$('mark-channel-read').onclick = async () => {
  try { await refreshReadState(); const position = readStates.get(channel)?.latest_id || 0;
    await api('/channels/' + channel + '/read',{method:'PUT',body:JSON.stringify({message_id:position})});
    readBoundary = null; render(); await refreshReadState();
  } catch(error) { showError(error); }
};
$('mark-all-read').onclick = async () => {
  try { await api('/read-all',{method:'POST'}); readBoundary = null; render(); await refreshReadState(); if($('mentions-dialog').open) await loadMentions(); }
  catch(error) { showError(error); }
};
async function loadMentions(before = null) {
  const request = ++inboxRequest; $('mentions-error').textContent = ''; $('mentions-more').disabled = true;
  if(before === null) $('mentions-results').replaceChildren();
  try {
    const data = await api('/mentions?unread_only=' + $('mentions-unread-only').checked + (before === null ? '' : '&before=' + before));
    if(request !== inboxRequest || !$('mentions-dialog').open) return;
    for(const message of data.messages) {
      const item = document.createElement('article'), button = document.createElement('button'), text = document.createElement('p');
      button.className = 'quiet'; button.textContent = '#' + message.channel_name + ' / ' + (message.display_name || message.username) + ' / ' + new Date(message.created*1000).toLocaleString() + (message.unread ? ' / unread' : '');
      text.textContent = message.content; button.onclick = async () => { $('mentions-dialog').close(); try { await select(message.channel_id,message.id); } catch(error) { showError(error); } };
      item.append(button,text); $('mentions-results').append(item);
    }
    if(!data.messages.length && before === null) $('mentions-results').textContent = 'No mentions here.';
    mentionsCursor = data.next_before; $('mentions-more').hidden = mentionsCursor === null;
  } catch(error) { if(request === inboxRequest) $('mentions-error').textContent = error.message; }
  finally { if(request === inboxRequest) $('mentions-more').disabled = false; }
}
$('mentions-open').onclick = () => { $('mentions-dialog').showModal(); loadMentions(); };
$('mentions-close').onclick = () => $('mentions-dialog').close();
$('mentions-dialog').onclose = () => { inboxRequest++; scheduleViewportRead(); };
$('mentions-unread-only').onchange = () => loadMentions();
$('mentions-more').onclick = () => loadMentions(mentionsCursor);
window.addEventListener('focus',clearCurrentAlerts);
document.addEventListener('visibilitychange',clearCurrentAlerts);
function hideSuggestions() { suggestionState = null; $('mention-suggestions').hidden = true; $('mention-suggestions').replaceChildren(); }
function chooseSuggestion(index) {
  const state = suggestionState, user = state?.users[index]; if(!user) return;
  const input = $('content'); input.setRangeText('@' + user.username + ' ',state.start,state.end,'end');
  hideSuggestions(); input.focus();
}
function drawSuggestions() {
  const list = $('mention-suggestions');
  list.replaceChildren(...suggestionState.users.map((user,index) => {
    const button = document.createElement('button'); button.type = 'button'; button.className = index === suggestionIndex ? 'selected' : '';
    button.textContent = '@' + user.username + ' — ' + user.display_name;
    button.onmousedown = event => event.preventDefault(); button.onclick = () => chooseSuggestion(index); return button;
  })); list.hidden = false;
}
function refreshSuggestions() {
  const input = $('content'); suggestionState = Mentions.suggestions(input.value,input.selectionStart,directory);
  if(!suggestionState) { hideSuggestions(); return; }
  if(suggestionState.users.length) { suggestionIndex = 0; drawSuggestions(); } else $('mention-suggestions').hidden = true;
  if(!directoryFetching && Date.now() - directoryFetched > 30000) {
    directoryFetching = true; directoryFetched = Date.now();
    api('/users').then(users => { directory = users; if(document.activeElement === input) refreshSuggestions(); }).catch(showError).finally(() => { directoryFetching = false; });
  }
}
function suggestionKey(event) {
  if(!suggestionState?.users.length || event.isComposing) return false;
  if(['ArrowDown','ArrowUp'].includes(event.key)) {
    event.preventDefault(); suggestionIndex = (suggestionIndex + (event.key === 'ArrowDown' ? 1 : -1) + suggestionState.users.length) % suggestionState.users.length; drawSuggestions(); return true;
  }
  if((event.key === 'Enter' && !event.shiftKey) || event.key === 'Tab') { event.preventDefault(); chooseSuggestion(suggestionIndex); return true; }
  if(event.key === 'Escape') { event.preventDefault(); hideSuggestions(); return true; }
  return false;
}
$('content').addEventListener('input',refreshSuggestions);
$('content').addEventListener('click',refreshSuggestions);
$('content').addEventListener('keyup',event => { if(['ArrowLeft','ArrowRight','Home','End'].includes(event.key)) refreshSuggestions(); });
$('content').addEventListener('blur',() => { setTimeout(() => { if(!$('mention-suggestions').contains(document.activeElement)) hideSuggestions(); },150); });
function receiveServerSettings(settings) {
  $('server-name').textContent = settings.server_name;
  $('server-description').textContent = settings.server_description;
  document.title = settings.server_name + ' · LANmsg';
  $('toggle-auth').hidden = !settings.registration_enabled;
  serverMediaPreviews = settings.media_previews === true;
  updateMediaPreference(member?.media_previews ?? null); updateAlertTitle();
  if(!settings.registration_enabled && registering) { registering = false; authMode(); }
}
async function loadServerSettings() { receiveServerSettings(await api('/status')); }
function clearAdminFeedback() { $('admin-error').textContent = ''; $('admin-notice').textContent = ''; }
function adminError(error, section) { if(activeAdminSection === section) $('admin-error').textContent = error.message; }
function adminNotice(message, section) { if(activeAdminSection === section) $('admin-notice').textContent = message; }
function selectAdminSection(section) {
  if(savingAdmin || savingTheme) return;
  editingTheme = false; applyTheme(savedTheme); clearAdminFeedback();
  activeAdminSection = section; $('appearance-error').textContent = '';
  for(const tab of document.querySelectorAll('[data-admin-section]')) {
    const active = section === tab.dataset.adminSection;
    tab.classList.toggle('active',active); tab.setAttribute('aria-pressed',String(active));
    $('admin-section-' + tab.dataset.adminSection).hidden = !active;
  }
  if(section === 'appearance') { editingTheme = true; fillThemeForm(savedTheme); $('theme-preset').value = 'custom'; }
  $('admin-content').scrollTop = 0;
}
function showAdminSettings(data, fill = true) {
  const form = $('admin-settings-form');
  if(fill) for(const [key,value] of Object.entries(data.settings)) {
    const input = form.elements.namedItem(key);
    if(typeof value === 'boolean') input.checked = value; else input.value = value;
  }
  $('admin-restart').textContent = data.restart_required ? 'Saved limits differ from active limits. Restart the server to apply them.' : 'Saved limits are active. Server identity and registration apply immediately.';
  $('admin-effective').replaceChildren(...Object.entries(data.effective_limits).flatMap(([key,value]) => {
    const term = document.createElement('dt'), definition = document.createElement('dd');
    term.textContent = LIMIT_LABELS[key]; definition.textContent = value; return [term,definition];
  }));
}
async function refreshAdmin(fill = true) {
  const data = await api('/admin/console');
  showAdminSettings(data, fill);
  $('admin-stats').replaceChildren(...['users','channels','messages','connections'].map(key => {
    const item = document.createElement('span'); item.textContent = `${data.stats[key]} ${key}`; return item;
  }));
  $('admin-channel-list').replaceChildren(...data.channels.map(channel => {
    const item = document.createElement('li'); item.textContent = '#' + channel.name; return item;
  }));
  $('admin-invite-list').replaceChildren(...data.invites.map(invite => {
    const item = document.createElement('li'), code = document.createElement('code'), expiry = document.createElement('small'), revoke = document.createElement('button');
    code.textContent = invite.token; expiry.textContent = 'Expires ' + new Date(invite.expires*1000).toLocaleString();
    revoke.type = 'button'; revoke.className = 'quiet'; revoke.textContent = 'Revoke';
    revoke.onclick = async () => { revoke.disabled = true; clearAdminFeedback(); try { await api('/admin/invites/' + encodeURIComponent(invite.token),{method:'DELETE'}); await refreshAdmin(false); } catch(error) { adminError(error, 'invites'); revoke.disabled = false; } };
    item.append(code,expiry,revoke); return item;
  }));
  if(!data.invites.length) { const item = document.createElement('li'); item.textContent = 'No active invites.'; $('admin-invite-list').append(item); }
}
$('admin-open').onclick = async () => {
  try { await Promise.all([refreshAdmin(), loadAppearance()]); selectAdminSection('settings'); $('admin-dialog').showModal(); }
  catch(error) { showError(error); }
};
$('admin-close').onclick = () => { if(!savingAdmin && !savingTheme) $('admin-dialog').close(); };
$('admin-dialog').oncancel = event => { if(savingAdmin || savingTheme) event.preventDefault(); };
$('admin-dialog').onclose = () => { editingTheme = false; applyTheme(savedTheme); clearAdminFeedback(); };
for(const button of document.querySelectorAll('[data-admin-section]')) button.onclick = () => selectAdminSection(button.dataset.adminSection);
$('admin-settings-form').onsubmit = async event => {
  event.preventDefault(); if(savingAdmin) return;
  savingAdmin = true; $('admin-save').disabled = true; $('admin-error').textContent = ''; $('admin-notice').textContent = '';
  const body = {};
  for(const input of $('admin-settings-form').elements) {
    if(!input.name) continue;
    body[input.name] = input.type === 'checkbox' ? input.checked : input.type === 'number' ? Number(input.value) : input.value;
  }
  try {
    const result = await api('/admin/settings',{method:'PUT',body:JSON.stringify(body)});
    showAdminSettings(result); receiveServerSettings(result.settings); adminNotice('Settings saved.', 'settings');
  } catch(error) { adminError(error, 'settings'); }
  finally { savingAdmin = false; $('admin-save').disabled = false; }
};
const profileUpdates = new Map();
let savingName = false, viewedUserId = null, userRequest = 0;
let mediaPreviewsEnabled = false;
function updateMediaPreference(preference) {
  const enabled = MediaPreviews.enabled(preference, serverMediaPreviews);
  MediaPreviews.setEnabled(enabled);
  if(member) member.media_previews = preference;
  if(enabled !== mediaPreviewsEnabled) { mediaPreviewsEnabled = enabled; if(member) render(); }
}
function nicknameColor(profile) {
  return profile.nickname_color === 'accent' ? 'var(--accent)' : profile.nickname_color === 'text' ? 'var(--text)' : '';
}
function refreshIdentity() {
  const name = member.display_name || member.username;
  $('identity-name').textContent = name;
  $('identity-handle').textContent = '@' + member.username;
  $('identity').title = name + ' (@' + member.username + ') — manage profile';
  $('composer-nick').textContent = '<' + name + '>';
  $('composer-nick').title = '@' + member.username;
  $('identity-name').style.color = nicknameColor(member); $('composer-nick').style.color = nicknameColor(member);
}
function showUser(profile) {
  $('user-display-name').textContent = profile.display_name;
  $('user-handle').textContent = '@' + profile.username;
  $('user-display-name').style.color = nicknameColor(profile);
  $('user-bio').textContent = profile.bio || ''; $('user-bio').hidden = !profile.bio;
}
function receiveProfile(profile) {
  const entry = directory.find(user => user.id === profile.id);
  if(entry) Object.assign(entry,profile);
  else if(profile.username) directory.push({id:profile.id,username:profile.username,display_name:profile.display_name});
  profileUpdates.set(profile.id, {...profileUpdates.get(profile.id), ...profile});
  if(member?.id === profile.id) { member = {...member, ...profile}; refreshIdentity(); }
  if(viewedUserId === profile.id && $('user-dialog').open) showUser(profileUpdates.get(profile.id));
  render();
}
async function openUser(id) {
  const request = ++userRequest;
  try {
    const profile = await api('/users/' + id + '/profile');
    if(request !== userRequest) return;
    viewedUserId = id; showUser({...profile, ...profileUpdates.get(id)});
    $('user-dialog').showModal();
  } catch(error) { showError(error); }
}
$('user-close').onclick = () => $('user-dialog').close();
$('user-dialog').onclose = () => { viewedUserId = null; };
$('identity').onclick = async () => {
  try {
    const profile = await api('/me/profile'); member = {...member, ...profile}; refreshIdentity();
    updateMediaPreference(profile.media_previews);
    $('profile-notifications').value = profile.notifications; $('profile-desktop-notifications').checked = profile.desktop_notifications; notificationStatus();
    $('profile-bio').value = profile.bio; $('profile-color').value = profile.nickname_color;
    $('profile-media-previews').value = profile.media_previews === null ? 'default' : profile.media_previews ? 'allow' : 'deny';
    $('display-name').value = member.display_name;
    $('display-name-handle').textContent = '@' + member.username;
    $('display-name-error').textContent = ''; $('display-name-dialog').showModal();
  } catch(error) { showError(error); }
};
function closeNameEditor() { if(!savingName) $('display-name-dialog').close(); }
$('display-name-close').onclick = closeNameEditor;
$('display-name-cancel').onclick = closeNameEditor;
$('display-name-dialog').oncancel = event => { if(savingName) event.preventDefault(); };
$('display-name-form').onsubmit = async event => {
  event.preventDefault(); if(savingName) return;
  savingName = true; $('display-name-save').disabled = true; $('display-name-error').textContent = '';
  try {
    const profile = await api('/me/profile', {method:'PUT', body:JSON.stringify({display_name:$('display-name').value, bio:$('profile-bio').value, nickname_color:$('profile-color').value, notifications:$('profile-notifications').value, desktop_notifications:$('profile-desktop-notifications').checked, media_previews:$('profile-media-previews').value === 'default' ? null : $('profile-media-previews').value === 'allow'})});
    updateMediaPreference(profile.media_previews); member.media_previews = profile.media_previews;
    member = {...member, ...profile};
    const {media_previews, notifications, desktop_notifications, ...publicProfile} = profile; receiveProfile(publicProfile); $('display-name-dialog').close();
  } catch(error) { $('display-name-error').textContent = error.message; }
  finally { savingName = false; $('display-name-save').disabled = false; }
};
const DEFAULT_THEME = Object.freeze({background:'#101419', sidebar:'#171d24', panel:'#1c242e', text:'#e2e8f0', muted:'#94a3b8', accent:'#72d6bb', border:'#303b48', nickname:'#91b9f5', font:'mono', font_size:14, line_height:1.6, row_spacing:3, corner_radius:0});
const COLOR_LABELS = {background:'Chat background', sidebar:'Sidebar & status bar', panel:'Inputs & selection', text:'Message text', muted:'Secondary text', accent:'Accent', border:'Borders', nickname:'Nicknames'};
const PRESETS = {
  default: DEFAULT_THEME,
  classic: {...DEFAULT_THEME, background:'#0b120d', sidebar:'#101b13', panel:'#192a1d', text:'#c4e5cb', muted:'#86a58c', accent:'#83e69a', border:'#304b36', nickname:'#b9d99b', corner_radius:2},
  light: {...DEFAULT_THEME, background:'#f7f9fc', sidebar:'#edf1f6', panel:'#e2e9f1', text:'#233247', muted:'#546579', accent:'#176a58', border:'#c5d0dd', nickname:'#315d9c'}
};
let savedTheme = {...DEFAULT_THEME}, savingTheme = false, editingTheme = false;
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
  if (!editingTheme) applyTheme(savedTheme);
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
$('appearance-cancel').onclick = () => {
  if(savingTheme) return;
  fillThemeForm(savedTheme); applyTheme(savedTheme); $('theme-preset').value = 'custom';
  $('appearance-error').textContent = ''; clearAdminFeedback();
};
$('appearance-form').onsubmit = async event => {
  event.preventDefault(); if(savingTheme) return;
  savingTheme = true; clearAdminFeedback(); $('appearance-save').disabled = true; $('appearance-error').textContent = '';
  try {
    receiveTheme(await api('/admin/appearance', {method:'PUT', body:JSON.stringify(readThemeForm())}));
    applyTheme(savedTheme); fillThemeForm(savedTheme); adminNotice('Appearance saved.', 'appearance');
  } catch(error) { $('appearance-error').textContent = error.message; }
  finally { savingTheme = false; $('appearance-save').disabled = false; }
};
async function api(path, options = {}) {
  const response = await fetch('/api' + path, {cache:'no-store', ...options, headers: {'Content-Type':'application/json', 'X-LANmsg':'1'}});
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
  channelList = await api('/channels'); await refreshReadState(); drawChannels();
}
function drawChannels() {
  $('channels').replaceChildren(...channelList.filter(c => c.kind === 'text').map(c => {
    const state = readStates.get(c.id), button = document.createElement('button');
    button.textContent = '#' + c.name + (state?.unread ? ' (' + state.unread + ')' : '') + (state?.mentions ? ' @' + state.mentions : '');
    button.className = (c.id === channel ? 'active ' : '') + (state?.unread ? 'unread' : '');
    if(state?.mentions) button.classList.add('has-mentions');
    if(c.id === channel) button.setAttribute('aria-current','page');
    button.onclick = () => select(c.id).catch(showError); return button;
  }));
}
function showError(error) { $('chat-error').textContent = error.message; }
function mergeMessage(message) {
  const previous = messages.get(message.id);
  if(!previous || (message.version || 1) >= (previous.version || 1)) messages.set(message.id, message);
}
let historyRequest = 0;
async function viewMessageHistory(id, before = null) {
  const request = ++historyRequest;
  const requestId = String(id);
  if(before === null) $('message-history-results').replaceChildren();
  clearAdminFeedback();
  try {
    const audit = await api('/admin/messages/' + encodeURIComponent(requestId) + '/history' + (before === null ? '' : '?before=' + before));
    if(request !== historyRequest || $('message-history-id').value !== requestId || activeAdminSection !== 'history' || !$('admin-dialog').open) return;
    const entries = audit.versions.map(v => ({label:`Version ${v.version} | ${v.action} by @${v.actor_username} | ${new Date(v.changed*1000).toLocaleString()}`, content:v.content}));
    if(before === null) entries.unshift({label:`Current version ${audit.current.version} | @${audit.current.username}${audit.current.deleted ? ' | Deleted' : ''}`,content:audit.current.content});
    for(const entry of entries) {
      const article = document.createElement('article'), heading = document.createElement('h4'), content = document.createElement('pre');
      heading.textContent = entry.label; content.textContent = entry.content; article.append(heading,content); $('message-history-results').append(article);
    }
    if(audit.current.deleted && before === null) {
      const note = document.createElement('p'); note.textContent = `Deleted ${new Date(audit.current.deleted*1000).toLocaleString()} by user #${audit.current.deleted_by}.`;
      $('message-history-results').append(note);
    }
    if(audit.next_before !== null) {
      const more = messageAction('Load earlier versions', async () => {
        more.disabled = true;
        if(await viewMessageHistory(requestId, audit.next_before)) more.remove(); else more.disabled = false;
      });
      $('message-history-results').append(more);
    }
    return true;
  } catch(error) { if(request === historyRequest) adminError(error, 'history'); }
}
$('message-history-form').onsubmit = event => { event.preventDefault(); viewMessageHistory($('message-history-id').value); };
async function openMessageHistory(id) {
  try {
    await refreshAdmin(); selectAdminSection('history'); $('message-history-id').value = id;
    if(!$('admin-dialog').open) $('admin-dialog').showModal();
    await viewMessageHistory(id);
  } catch(error) { showError(error); }
}
function messageAction(label, action) {
  const button = document.createElement('button'); button.type = 'button'; button.className = 'quiet'; button.textContent = label; button.onclick = action; return button;
}
function messageIcon(label, icon, action) {
  const button = messageAction(label, action);
  button.textContent = ''; button.title = label; button.setAttribute('aria-label', label);
  const glyph = document.createElement('span'); glyph.className = 'message-icon';
  glyph.setAttribute('aria-hidden', 'true'); glyph.style.setProperty('--icon', `url('/static/icons/${icon}.svg')`);
  button.append(glyph); return button;
}
async function saveMessageEdit(id) {
  if(savingMessage) return;
  savingMessage = true; $('chat-error').textContent = ''; render();
  try {
    const updated = await api('/messages/' + id,{method:'PUT',body:JSON.stringify({content:editDraft,version:editVersion})});
    if(updated.channel_id === channel) mergeMessage(updated);
    editingMessage = null; editDraft = '';
  } catch(error) { showError(error); }
  finally { savingMessage = false; render(); }
}
async function deleteMessage(id) {
  if(!confirm('Delete this message? Its content will remain available to the server owner.')) return;
  try {
    const updated = await api('/messages/' + id,{method:'DELETE'});
    if(updated.channel_id === channel) mergeMessage(updated);
    if(editingMessage === id) editingMessage = null;
    render();
  } catch(error) { showError(error); }
}
function render() {
  const focused = document.activeElement;
  const editFocus = focused?.id === 'message-edit-' + editingMessage ? {start:focused.selectionStart,end:focused.selectionEnd} : null;
  const scroller = $('messages'), nearBottom = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 100;
  const rows = []; let previousDay, divided = false;
  for (const m of [...messages.values()].sort((a,b) => a.id-b.id)) {
    const timestamp = new Date(m.created*1000), day = timestamp.toDateString();
    if(day !== previousDay) {
      const divider = dayRows.get(day) || document.createElement('div'); divider.className = 'day-divider'; dayRows.set(day, divider);
      divider.textContent = timestamp.toLocaleDateString(undefined,{weekday:'short',year:'numeric',month:'short',day:'numeric'});
      rows.push(divider); previousDay = day;
    }
    if(!divided && readBoundary !== null && m.id > readBoundary && !m.deleted && m.user_id !== member?.id) { rows.push(unreadDivider); divided = true; }
    const profile = {...m, ...profileUpdates.get(m.user_id)}, displayName = profile.display_name || profile.username;
    const signature = JSON.stringify([m.version, m.content, m.deleted, m.edited, profile.display_name, profile.username, profile.nickname_color, mediaPreviewsEnabled, member?.owner, m.mentions]);
    const cached = messageRows.get(m.id);
    if(editingMessage !== m.id && cached?.signature === signature) { rows.push(cached.item); continue; }
    const item = document.createElement('article'); item.className = 'message' + (m.user_id === member?.id ? ' self' : '');
    if(Mentions.isMention(m,member?.id)) item.classList.add('mentioned');
    item.id = 'message-' + m.id;
    const name = document.createElement('button'); name.type = 'button'; name.className = 'nickname'; name.textContent = '<' + displayName + '>'; name.title = displayName + ' (@' + profile.username + ')';
    name.style.color = nicknameColor(profile);
    name.setAttribute('aria-label', 'View ' + displayName + ', @' + profile.username); name.onclick = () => openUser(m.user_id);
    const date = document.createElement('time'); date.dateTime = timestamp.toISOString(); date.textContent = timestamp.toLocaleTimeString(undefined,{hour:'2-digit',minute:'2-digit',hour12:false}); date.title = timestamp.toLocaleString();
    const body = document.createElement('div'); body.className = 'body';
    if(m.deleted) body.textContent = 'Message deleted'; else Mentions.append(body, m.content, m.mentions, openUser);
    if(m.deleted) body.classList.add('deleted-message');
    if(m.edited && !m.deleted) {
      const marker = document.createElement('small'); marker.className = 'edited-marker'; marker.textContent = ' (edited)'; marker.title = new Date(m.edited*1000).toLocaleString(); body.append(marker);
    }
    if(!m.deleted && editingMessage !== m.id) MediaPreviews.appendPreviews(body, m.content, mediaPreviewsEnabled);
    const actions = document.createElement('div'); actions.className = 'message-actions';
    actions.setAttribute('role', 'group'); actions.setAttribute('aria-label', 'Message actions');
    if(!m.deleted && m.user_id === member?.id) actions.append(messageIcon('Edit message', 'edit-pencil', () => {
      if(savingMessage) return;
      editingMessage = m.id; editDraft = m.content; editVersion = m.version; render();
      $('message-edit-' + m.id)?.focus();
    }));
    if(!m.deleted && (m.user_id === member?.id || member?.owner)) actions.append(messageIcon('Delete message', 'trash', () => deleteMessage(m.id)));
    if(member?.owner) actions.append(messageIcon('View message history', 'clock-rotate-right', () => openMessageHistory(m.id)));
    if(editingMessage === m.id && !m.deleted) {
      body.replaceChildren();
      const input = document.createElement('textarea'); input.id = 'message-edit-' + m.id; input.value = editDraft; input.maxLength = 4000; input.disabled = savingMessage; input.setAttribute('aria-label','Edit message');
      input.oninput = () => { editDraft = input.value; };
      const save = messageAction('Save edit', () => saveMessageEdit(m.id)), cancel = messageAction('Cancel', () => { editingMessage = null; render(); });
      save.disabled = cancel.disabled = savingMessage;
      body.append(input,save,cancel);
    }
    item.append(date,name,body);
    if(actions.childElementCount) {
      item.style.setProperty('--message-action-width', (actions.childElementCount * 28 + 12) + 'px');
      item.append(actions);
    }
    rows.push(item);
    if(editingMessage !== m.id) messageRows.set(m.id, {signature,item});
  }
  const list = $('message-list'), desired = new Set(rows);
  for(const child of [...list.children]) if(!desired.has(child)) child.remove();
  rows.forEach((row,index) => { if(list.children[index] !== row) list.insertBefore(row,list.children[index] || null); });
  for(const [id,cached] of messageRows) if(!messages.has(id)) messageRows.delete(id);
  for(const [day,row] of dayRows) if(!desired.has(row)) dayRows.delete(day);
  $('empty').hidden = messages.size > 0;
  if(editFocus) { const input = $('message-edit-' + editingMessage); if(input && !input.disabled) { input.focus({preventScroll:true}); input.setSelectionRange(editFocus.start,editFocus.end); } }
  if (nearBottom && !hasNewer) scroller.scrollTop = scroller.scrollHeight;
  scheduleViewportRead();
}
async function select(id, around = null) {
  channel = id; const current = ++generation; selecting = true; messages.clear(); hasNewer = false; readBoundary = null;
  if(!savingMessage) { editingMessage = null; editDraft = ''; }
  desktopAlerts.get(id)?.close(); desktopAlerts.delete(id); hideSuggestions();
  ChannelState.save(member.id,id);
  const name = channelList.find(c => c.id === id)?.name || 'general';
  $('channel-title').textContent = '#' + name; $('status-channel').textContent = '#' + name; $('content').placeholder = 'Message #' + name;
  $('older').hidden = true; $('newer').hidden = true; render();
  try {
    const view = await api('/channels/' + id + '/view' + (around === null ? '' : '?around=' + around));
    if(current !== generation) return;
    // Live deliveries during this request are merged by version, preserving edits/deletions.
    view.messages.forEach(mergeMessage); readBoundary = view.read_id; hasNewer = view.has_newer;
    if(hasNewer && view.messages.length) { const end = view.messages.at(-1).id; for(const key of messages.keys()) if(key > end) messages.delete(key); }
    render(); $('older').hidden = !view.has_older; $('newer').hidden = !hasNewer;
    const target = around === null ? $('message-list').querySelector('.new-messages-divider') : $('message-' + around);
    if(target) { target.scrollIntoView({block:'start'}); if(around !== null) target.classList.add('jump-target'); }
    else $('messages').scrollTop = $('messages').scrollHeight;
    await refreshReadState(); drawChannels();
  } finally { if(current === generation) { selecting = false; scheduleViewportRead(); } }
}
$('newer').onclick = async () => {
  if(loading || !messages.size) return;
  loading = true; const current = generation;
  try {
    const rows = await api('/channels/' + channel + '/messages?after=' + Math.max(...messages.keys()));
    if(current !== generation) return;
    rows.forEach(mergeMessage); hasNewer = rows.length === 50; render(); $('newer').hidden = !hasNewer;
  } catch(error) { showError(error); } finally { loading = false; }
};
$('older').onclick = async () => {
  if (loading || !messages.size) return;
  loading = true; const current = generation, scroller = $('messages'), height = scroller.scrollHeight, top = scroller.scrollTop;
  try {
    const rows = await api(`/channels/${channel}/messages?before=${Math.min(...messages.keys())}`);
    if (current !== generation) return;
    rows.forEach(mergeMessage); render(); scroller.scrollTop = top + scroller.scrollHeight - height;
    $('older').hidden = rows.length < 50;
  } catch(error) { showError(error); } finally { loading = false; }
};
function connect() {
  clearTimeout(reconnectTimer);
  socket = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws`);
  socket.onopen = async () => {
    $('connection').textContent = 'Connected'; $('connection').dataset.state = 'online';
    heartbeat = setInterval(() => { if(socket.readyState === WebSocket.OPEN) socket.send('ping'); },20000);
    try {
      directory = await api('/users');
      profileUpdates.clear(); member = {...await api('/me'), ...await api('/me/profile')};
      updateMediaPreference(member.media_previews);
      if(profileUpdates.has(member.id)) member = {...member, ...profileUpdates.get(member.id)};
      refreshIdentity(); await loadServerSettings(); await loadAppearance(); await select(channel);
    } catch(error) { showError(error); }
  };
  socket.onmessage = event => {
    const data = JSON.parse(event.data);
    if(data.type === 'profile') {
      receiveProfile(data.profile);
      if(data.profile.id === member?.id) api('/me/profile').then(profile => { member = {...member,...profile}; updateMediaPreference(profile.media_previews); }).catch(showError);
    }
    if(data.type === 'server_settings') receiveServerSettings(data.settings);
    if(data.type === 'appearance') receiveTheme(data.appearance);
    if(data.type === 'reads' && data.user_id === member?.id) { scheduleReadRefresh(); if($('mentions-dialog').open) loadMentions(); }
    if(data.type === 'channels') loadChannels().catch(showError);
    if(data.type === 'message_update') {
      removeMentionAlert(data.message);
      if(data.message.channel_id === channel && (!hasNewer || messages.has(data.message.id))) { mergeMessage(data.message); if(!selecting) render(); }
    }
    if(data.type === 'message') {
      const m = data.message;
      receiveAlert(m);
      if(m.channel_id === channel && selecting) { mergeMessage(m); }
      else if(m.channel_id === channel && !hasNewer) { mergeMessage(m); render(); }
      else { scheduleReadRefresh(); if(m.channel_id === channel) { hasNewer = true; $('newer').hidden = false; } }
    }
  };
  socket.onclose = () => { clearInterval(heartbeat); $('connection').textContent = 'Reconnecting…'; $('connection').dataset.state = 'offline'; reconnectTimer = setTimeout(connect,2000); };
}
async function start() {
  const profile = await api('/me/profile'); member = {...member, ...profile}; updateMediaPreference(profile.media_previews);
  $('auth').hidden = true; $('chat').hidden = false; $('admin').hidden = !member.owner;
  refreshIdentity();
  directory = await api('/users'); seenEvents.clear();
  await loadChannels();
  channel = ChannelState.restore(member.id, channelList);
  await select(channel); connect();
}
$('composer').onsubmit = async event => {
  event.preventDefault(); const content = $('content').value.trim(); if(!content) return;
  $('send').disabled = true; $('chat-error').textContent = '';
  // Keep the same nonce after an ambiguous network failure, so retrying cannot duplicate a message.
  if(!pendingSend || pendingSend.content !== content || pendingSend.channel !== channel) pendingSend = {content,channel,nonce:globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}`};
  const attempt = pendingSend;
  try {
    const m = await api(`/channels/${attempt.channel}/messages`, {method:'POST', body:JSON.stringify(attempt)});
    if(channel === m.channel_id && !hasNewer) { mergeMessage(m); render(); }
    scheduleReadRefresh();
    if($('content').value.trim() === attempt.content) $('content').value = ''; hideSuggestions();
    pendingSend = null;
  } catch(error) { showError(new Error('Message not confirmed. Press Send to retry. ' + error.message)); }
  finally { $('send').disabled = false; $('content').focus(); }
};
$('content').onkeydown = event => { if(suggestionKey(event)) return; if(event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); if(!$('send').disabled) $('composer').requestSubmit(); } };
$('admin-channel-name').oninput = () => {
  const input = $('admin-channel-name'); input.value = input.value.toLowerCase().replace(/\s+/g, '-');
};
$('admin-channel-form').onsubmit = async event => {
  event.preventDefault(); $('add-channel').disabled = true; clearAdminFeedback();
  try { await api('/admin/channels',{method:'POST',body:JSON.stringify({name:$('admin-channel-name').value})}); $('admin-channel-name').value = ''; await loadChannels(); await refreshAdmin(false); }
  catch(error) { adminError(error, 'channels'); } finally { $('add-channel').disabled = false; }
};
$('invite-button').onclick = async () => {
  $('invite-button').disabled = true; clearAdminFeedback();
  try { await api('/admin/invites',{method:'POST'}); await refreshAdmin(false); adminNotice('Invite created. Select the code to copy it.', 'invites'); }
  catch(error) { adminError(error, 'invites'); } finally { $('invite-button').disabled = false; }
};
$('logout').onclick = async () => { try { await api('/session/logout',{method:'POST'}); socket.onclose = null; socket.close(); clearInterval(heartbeat); clearTimeout(reconnectTimer); location.reload(); } catch(error) { showError(error); } };
(async () => {
  try { await loadServerSettings(); } catch { /* Default server identity remains usable. */ }
  try { await loadAppearance(); } catch { /* CSS defaults remain usable if theme loading fails. */ }
  try { member = await api('/me'); await start(); } catch { try { const status = await api('/status'); if(status.setup) { registering = true; authMode(); $('invite-label').hidden = true; $('auth-hint').textContent = 'Create the first account to become the server owner.'; } } catch { $('auth-error').textContent = 'Cannot reach the server.'; } }
})();
