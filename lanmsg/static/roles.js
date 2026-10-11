'use strict';
globalThis.Roles = (() => {
  const names = {admin:'Admin',moderator:'Moderator',user:'User',banned:'Banned'};
  function role(user) { return user?.role || (user?.banned ? 'banned' : user?.owner || user?.admin ? 'admin' : user?.moderator ? 'moderator' : 'user'); }
  function label(user) { return names[role(user)] + (user?.owner ? ' (root)' : ''); }
  function staff(user) { return ['admin','moderator'].includes(role(user)); }
  function tabs(user) { return role(user) === 'admin' ? ['settings','channels','invites','appearance','history','members','audit'] : role(user) === 'moderator' ? ['members','audit'] : []; }
  function canChange(actor,target,next) {
    if(!actor || target.owner || target.id === actor.id) return false;
    if(actor.owner) return true;
    if(role(actor) === 'admin') return role(target) !== 'admin' && next !== 'admin';
    return role(actor) === 'moderator' && ['user','banned'].includes(role(target)) && ['user','banned'].includes(next);
  }
  function choices(actor,target) {
    return Object.keys(names).filter(next => canChange(actor,target,next) && (role(target) !== 'banned' || ['user','banned'].includes(next)));
  }
  function canDelete(actor,author) { return !!actor && (actor.id === author.id || role(actor) === 'admin' || (role(actor) === 'moderator' && ['user','banned'].includes(role(author)))); }
  return Object.freeze({role,label,staff,tabs,canChange,choices,canDelete});
})();
