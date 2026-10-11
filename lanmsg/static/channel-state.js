'use strict';

// localStorage is scoped to this server's origin; the key also separates accounts.
const ChannelState = {
  restore(userId, channels) {
    let saved;
    try { saved = localStorage.getItem(`lanmsg:last-channel:${userId}`); }
    catch { /* Chat still works when browser storage is unavailable. */ }
    const textChannels = channels.filter(channel => channel.kind === 'text');
    return textChannels.find(channel => String(channel.id) === saved)?.id
      ?? textChannels.find(channel => channel.id === 1)?.id
      ?? textChannels[0]?.id
      ?? 1;
  },
  save(userId, channelId) {
    try { localStorage.setItem(`lanmsg:last-channel:${userId}`, String(channelId)); }
    catch { /* Browsers may disable storage or exhaust their quota. */ }
  }
};
