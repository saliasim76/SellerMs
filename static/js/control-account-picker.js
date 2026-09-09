/* ══════════════════════════════════════════════════════════════════
   control-account-picker.js
   Two synced dropdowns (Level Five Code + Drawer) for linking a Buyer,
   Supplier, or Employee master record to an AR/AP control account.
   Only Level Five accounts with control_account = 'Yes' are offered
   (see /lookups/control-accounts) -- unlike Item Master's own Chart of
   Account picker, which deliberately allows any account.

   Usage:
     <select id="b_levelfive_code" onchange="ControlAccountPicker.sync('b_levelfive_code','b_levelfive_drawer','code')">
     <select id="b_levelfive_drawer" onchange="ControlAccountPicker.sync('b_levelfive_code','b_levelfive_drawer','drawer')">

     On page load / form reset:   ControlAccountPicker.wire('b_levelfive_code','b_levelfive_drawer');
     When opening an edit form:   ControlAccountPicker.regenerate('b_levelfive_code','b_levelfive_drawer', d.levelfive_code, d.levelfive_drawer);
   ══════════════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  let _list = null;
  let _promise = null;

  function esc(s) {
    return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/"/g, '&quot;')
      .replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  function placeholderText() {
    return document.documentElement.getAttribute('dir') === 'rtl' ? 'اختر...' : 'Select...';
  }

  function ensureLoaded() {
    if (_promise) return _promise;
    _promise = fetch('/lookups/control-accounts')
      .then((r) => r.json())
      .then((list) => { _list = list || []; return _list; })
      .catch(() => { _list = []; return _list; });
    return _promise;
  }

  function optionsHtml(field, selected) {
    const seen = new Set();
    let opts = '';
    (_list || []).forEach((a) => {
      const v = a[field];
      if (!v || seen.has(v)) return;
      seen.add(v);
      opts += '<option value="' + esc(v) + '"' + (v === selected ? ' selected' : '') + '>' + esc(v) + '</option>';
    });
    if (selected && !seen.has(selected)) {
      opts = '<option value="' + esc(selected) + '" selected>' + esc(selected) + '</option>' + opts;
    }
    return '<option value="">' + placeholderText() + '</option>' + opts;
  }

  function regenerate(codeId, drawerId, codeVal, drawerVal) {
    const codeSel = document.getElementById(codeId);
    const drawerSel = document.getElementById(drawerId);
    if (codeSel) codeSel.innerHTML = optionsHtml('code', codeVal || '');
    if (drawerSel) drawerSel.innerHTML = optionsHtml('drawer', drawerVal || '');
  }

  function wire(codeId, drawerId) {
    ensureLoaded().then(() => {
      const codeSel = document.getElementById(codeId);
      const drawerSel = document.getElementById(drawerId);
      regenerate(codeId, drawerId, codeSel ? codeSel.value : '', drawerSel ? drawerSel.value : '');
    });
  }

  /* Picking either Account Code or Drawer auto-selects the matching value
     in the other, for the same Level Five account. */
  function sync(codeId, drawerId, source) {
    const codeSel = document.getElementById(codeId);
    const drawerSel = document.getElementById(drawerId);
    if (!codeSel || !drawerSel) return;
    const val = source === 'code' ? codeSel.value : drawerSel.value;
    const match = val ? (_list || []).find((x) => x[source] === val) : null;
    if (source === 'code') drawerSel.value = match ? (match.drawer || '') : '';
    else codeSel.value = match ? (match.code || '') : '';
  }

  window.ControlAccountPicker = { wire: wire, regenerate: regenerate, sync: sync, ensureLoaded: ensureLoaded };
})();
