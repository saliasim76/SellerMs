/* ══════════════════════════════════════════════════════════════════
   grid-kit.js
   Adds the grid features AG-Grid ships only in its paid Enterprise
   build, implemented on top of the free Community API.

   Toolbar is deliberately small — three controls, not six:

       [ Columns ▾ ]   [ Filters (2) ]   [ Export ▾ ]

   Everything else (pin, hide, auto-size, reset) lives inside the
   Columns dropdown, where it belongs.

   Version note
   ------------
   AG-Grid moved column operations from `columnApi` (v31 and earlier)
   onto `api` (v33+). `cols()` resolves whichever object is present, so
   this file works on both without a version check at every call site.

   Usage, inside onGridReady:

       GridKit.enhance(params, 'empGrid', { storageKey: 'employees' });

   `params` may be the whole onGridReady event, or just the api.
   ══════════════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  var IS_AR = document.documentElement.getAttribute('dir') === 'rtl';

  var T = IS_AR ? {
    columns: 'الأعمدة', filters: 'الفلاتر', exportLbl: 'تصدير',
    showAll: 'إظهار الكل', hideAll: 'إخفاء الكل',
    autosize: 'ملاءمة العرض', reset: 'إعادة تعيين الكل',
    resetWidths: 'إعادة تعيين العرض', resetVisibility: 'إعادة تعيين الإظهار',
    clearFilters: 'مسح كل الفلاتر', noFilters: 'لا توجد فلاتر',
    csv: 'ملف CSV', excel: 'ملف Excel', pdf: 'طباعة / PDF',
    pinLeft: 'تثبيت لليسار', pinRight: 'تثبيت لليمين', unpin: 'إلغاء التثبيت',
    visible: 'مرئي', searchAll: 'بحث في كل الأعمدة...', clearOne: 'مسح',
    pivot: 'محور', pivotTitle: 'جدول محوري', pivotHint: 'اسحب الحقول لإنشاء تجميع/محور', pivotClose: 'إغلاق'
  } : {
    columns: 'Columns', filters: 'Filters', exportLbl: 'Export',
    showAll: 'Show all', hideAll: 'Hide all',
    autosize: 'Fit widths', reset: 'Reset all',
    resetWidths: 'Reset widths', resetVisibility: 'Reset visibility',
    clearFilters: 'Clear all filters', noFilters: 'No active filters',
    csv: 'CSV file', excel: 'Excel file', pdf: 'Print / PDF',
    pinLeft: 'Pin left', pinRight: 'Pin right', unpin: 'Unpin',
    visible: 'Visible', searchAll: 'Search all columns...', clearOne: 'Clear',
    pivot: 'Pivot', pivotTitle: 'Pivot Table', pivotHint: 'Drag fields to build groups / pivot / aggregates', pivotClose: 'Close'
  };

  /* ── Shared date display formatter ────────────────────────────
     ISO 'YYYY-MM-DD' (optionally with a time part) -> '01-Apr-26'.
     Display-only: use it as an ag-grid column's valueFormatter, never
     to rewrite the underlying cell value -- most date fields in this
     app are also used to populate a native <input type="date"> in an
     edit form, which requires the raw ISO string to work. Leaves
     anything that isn't a recognizable ISO date untouched (already-
     formatted strings, blanks, non-date text) so it's safe to apply
     even to columns that mix real dates with placeholder text. */
  var MONTHS_EN = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  function fmtDate(v) {
    if (v == null || v === '') return '';
    var m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(v));
    if (!m) return v;
    var mon = MONTHS_EN[parseInt(m[2], 10) - 1];
    if (!mon) return v;
    return m[3] + '-' + mon + '-' + m[1].slice(-2);
  }

  /* ── Version bridge ───────────────────────────────────────────
     v31: column ops live on columnApi.  v33: they moved to api.   */
  function cols(ctx) {
    return ctx.columnApi || ctx.api;
  }

  function allColumns(ctx) {
    var c = cols(ctx);
    if (c.getAllGridColumns) return c.getAllGridColumns();   // v31
    if (c.getColumns) return c.getColumns();                 // v33
    return [];
  }

  function setVisible(ctx, colId, visible) {
    var c = cols(ctx);
    if (c.setColumnsVisible) c.setColumnsVisible([colId], visible);
    else if (c.setColumnVisible) c.setColumnVisible(colId, visible);
  }

  function setPinned(ctx, colId, side) {
    var c = cols(ctx);
    if (c.setColumnsPinned) c.setColumnsPinned([colId], side);
    else if (c.setColumnPinned) c.setColumnPinned(colId, side);
  }

  function autoSize(ctx) {
    var c = cols(ctx);
    var ids = allColumns(ctx).filter(function (x) { return x.isVisible(); })
                             .map(function (x) { return x.getColId(); });
    if (c.autoSizeColumns) c.autoSizeColumns(ids);
    else if (c.sizeColumnsToFit) c.sizeColumnsToFit();
  }

  function resetCols(ctx) {
    var c = cols(ctx);
    if (c.resetColumnState) c.resetColumnState();
  }

  function colState(ctx) {
    var c = cols(ctx);
    return c.getColumnState ? c.getColumnState() : null;
  }

  function applyState(ctx, state) {
    var c = cols(ctx);
    if (c.applyColumnState) c.applyColumnState({ state: state, applyOrder: true });
  }

  /* Partial state application -- only the named property is touched per
     column (AG-Grid's applyColumnState merges whatever properties are
     present in each entry and leaves the rest alone), so this can reset
     just widths or just visibility without disturbing the other. */
  function applyPartialState(ctx, defaultState, prop) {
    var c = cols(ctx);
    if (!c.applyColumnState || !defaultState) return;
    var partial = defaultState.map(function (s) {
      var entry = { colId: s.colId };
      entry[prop] = s[prop];
      return entry;
    });
    c.applyColumnState({ state: partial, applyOrder: false });
  }

  /* ── Storage ──────────────────────────────────────────────── */
  function load(k) {
    try { return JSON.parse(localStorage.getItem('gk:' + k) || 'null'); }
    catch (e) { return null; }
  }
  function save(k, v) {
    try { localStorage.setItem('gk:' + k, JSON.stringify(v)); } catch (e) {}
  }

  /* ── DOM helpers ──────────────────────────────────────────── */
  function el(tag, cls, html) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (html != null) n.innerHTML = html;
    return n;
  }

  function closeMenus() {
    document.querySelectorAll('.gk-pop').forEach(function (m) { m.remove(); });
    document.querySelectorAll('.gk-btn.gk-open').forEach(function (b) {
      b.classList.remove('gk-open');
    });
  }
  document.addEventListener('click', function (e) {
    if (!e.target.closest('.gk-pop, .gk-btn')) closeMenus();
  });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') closeMenus();
  });

  function popover(anchor) {
    closeMenus();
    anchor.classList.add('gk-open');
    var p = el('div', 'gk-pop');
    var r = anchor.getBoundingClientRect();
    p.style.top = (window.scrollY + r.bottom + 6) + 'px';
    var left = window.scrollX + r.left;
    // Keep the panel on screen.
    left = Math.min(left, window.scrollX + window.innerWidth - 268);
    p.style.left = Math.max(8, left) + 'px';
    document.body.appendChild(p);
    return p;
  }

  function colName(col) {
    var d = col.getColDef();
    return d.headerName || d.field || col.getColId();
  }

  function esc(s) {
    return String(s).replace(/[&<>"]/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
    });
  }

  /* ══ Columns panel: visibility + pin + sizing, all in one ════ */
  function columnsPanel(ctx, anchor, key, onChange, defaultState) {
    var p = popover(anchor);

    var head = el('div', 'gk-pop-head');
    head.appendChild(el('span', 'gk-pop-title', T.columns));
    var bulk = el('div', 'gk-pop-bulk');

    function bulkBtn(label, fn) {
      var b = el('button', 'gk-link', label);
      b.type = 'button';
      b.addEventListener('click', function () {
        fn();
        onChange();
        p.remove();
        columnsPanel(ctx, anchor, key, onChange, defaultState);   // redraw
      });
      bulk.appendChild(b);
    }
    bulkBtn(T.showAll, function () {
      allColumns(ctx).forEach(function (c) { setVisible(ctx, c.getColId(), true); });
    });
    bulkBtn(T.autosize, function () { autoSize(ctx); });
    bulkBtn(T.resetWidths, function () { applyPartialState(ctx, defaultState, 'width'); });
    bulkBtn(T.resetVisibility, function () { applyPartialState(ctx, defaultState, 'hide'); });
    bulkBtn(T.reset, function () {
      resetCols(ctx);
      try { localStorage.removeItem('gk:' + key + ':cols'); } catch (e) {}
    });
    head.appendChild(bulk);
    p.appendChild(head);

    var list = el('div', 'gk-pop-list');
    allColumns(ctx).forEach(function (col) {
      var id = col.getColId();
      var row = el('div', 'gk-col-row');

      var lab = el('label', 'gk-col-name');
      var cb = el('input');
      cb.type = 'checkbox';
      cb.checked = col.isVisible();
      cb.addEventListener('change', function () {
        setVisible(ctx, id, cb.checked);   // instant — no refresh
        onChange();
      });
      lab.appendChild(cb);
      lab.appendChild(document.createTextNode(colName(col)));
      row.appendChild(lab);

      // Pin control: a 3-state segmented toggle, not another button row.
      var pin = el('div', 'gk-pin');
      [['left', 'L', T.pinLeft], [null, '–', T.unpin], ['right', 'R', T.pinRight]]
        .forEach(function (opt) {
          var b = el('button', 'gk-pin-btn', opt[1]);
          b.type = 'button';
          b.title = opt[2];
          if (col.getPinned() === opt[0] || (!col.getPinned() && opt[0] === null)) {
            b.classList.add('gk-pin-on');
          }
          b.addEventListener('click', function () {
            setPinned(ctx, id, opt[0]);
            onChange();
            pin.querySelectorAll('.gk-pin-btn').forEach(function (x) {
              x.classList.remove('gk-pin-on');
            });
            b.classList.add('gk-pin-on');
          });
          pin.appendChild(b);
        });
      row.appendChild(pin);
      list.appendChild(row);
    });
    p.appendChild(list);
  }

  /* ══ Filters panel ═══════════════════════════════════════════
     A read-only list of active filters is useless when nothing is
     filtered — which is most of the time. So this panel is a place to
     *apply* filters: type in any column's box and it filters as you go,
     with the active ones surfaced at the top and clearable in one click. */
  function filtersPanel(ctx, anchor) {
    var api = ctx.api;
    var p = popover(anchor);
    p.classList.add('gk-pop-wide');

    var model = (api.getFilterModel && api.getFilterModel()) || {};
    var active = Object.keys(model);

    var head = el('div', 'gk-pop-head');
    head.appendChild(el('span', 'gk-pop-title', T.filters));
    if (active.length) {
      var clear = el('button', 'gk-link gk-danger', T.clearFilters);
      clear.type = 'button';
      clear.style.marginTop = '7px';
      clear.addEventListener('click', function () {
        api.setFilterModel(null);
        closeMenus();
      });
      head.appendChild(clear);
    }
    p.appendChild(head);

    /* Quick search across every column — the fastest way to find a row. */
    var qsWrap = el('div', 'gk-qs');
    var qs = el('input');
    qs.type = 'search';
    qs.className = 'gk-qs-input';
    qs.placeholder = T.searchAll;
    qs.value = ctx._gkQuick || '';
    qs.addEventListener('input', function () {
      ctx._gkQuick = qs.value;
      api.setGridOption('quickFilterText', qs.value);
    });
    qsWrap.appendChild(el('i', 'fas fa-magnifying-glass gk-qs-icon'));
    qsWrap.appendChild(qs);
    p.appendChild(qsWrap);
    setTimeout(function () { qs.focus(); }, 30);

    /* One text box per filterable column. */
    var list = el('div', 'gk-pop-list');
    var filterable = allColumns(ctx).filter(function (c) {
      var d = c.getColDef();
      return c.isVisible() && d.filter !== false && d.field;
    });

    if (!filterable.length) {
      list.appendChild(el('div', 'gk-empty', T.noFilters));
    }

    filterable.forEach(function (col) {
      var id = col.getColId();
      var row = el('div', 'gk-filter-row');

      var lab = el('label', 'gk-filter-label', esc(colName(col)));
      row.appendChild(lab);

      var box = el('div', 'gk-filter-box');
      var inp = el('input');
      inp.type = 'text';
      inp.className = 'gk-filter-input';
      var cur = model[id];
      inp.value = (cur && (cur.filter != null ? cur.filter : '')) || '';
      if (inp.value) row.classList.add('gk-filter-on');

      var timer = null;
      inp.addEventListener('input', function () {
        clearTimeout(timer);
        timer = setTimeout(function () {
          var m = (api.getFilterModel && api.getFilterModel()) || {};
          var v = inp.value.trim();
          if (v) m[id] = { filterType: 'text', type: 'contains', filter: v };
          else delete m[id];
          api.setFilterModel(m);
          row.classList.toggle('gk-filter-on', !!v);
        }, 260);
      });

      var x = el('button', 'gk-chip-x', '<i class="fas fa-times"></i>');
      x.type = 'button';
      x.title = T.clearOne;
      x.addEventListener('click', function () {
        inp.value = '';
        var m = (api.getFilterModel && api.getFilterModel()) || {};
        delete m[id];
        api.setFilterModel(m);
        row.classList.remove('gk-filter-on');
      });

      box.appendChild(inp);
      box.appendChild(x);
      row.appendChild(box);
      list.appendChild(row);
    });

    p.appendChild(list);
  }

  /* ══ Export ══════════════════════════════════════════════════ */
  function exportPanel(ctx, anchor, name) {
    var p = popover(anchor);
    p.appendChild(el('div', 'gk-pop-head',
      '<span class="gk-pop-title">' + T.exportLbl + '</span>'));

    function item(icon, label, colour, fn) {
      var row = el('button', 'gk-item',
        '<i class="fas ' + icon + '" style="color:' + colour + '"></i>' + label);
      row.type = 'button';
      row.addEventListener('click', function () { closeMenus(); fn(); });
      p.appendChild(row);
    }
    item('fa-file-csv', T.csv, '#16a34a', function () {
      ctx.api.exportDataAsCsv({ fileName: name + '.csv' });
    });
    item('fa-file-excel', T.excel, '#15803d', function () { toExcel(ctx, name); });
    item('fa-file-pdf', T.pdf, '#dc2626', function () { toPdf(ctx, name); });
  }

  function rows(ctx) {
    var visible = allColumns(ctx).filter(function (c) { return c.isVisible(); });
    var out = [];
    ctx.api.forEachNodeAfterFilterAndSort(function (node) {
      if (!node.data) return;
      out.push(visible.map(function (c) {
        var f = c.getColDef().field;
        var v = f ? node.data[f] : '';
        return v == null ? '' : v;
      }));
    });
    return { head: visible.map(colName), body: out };
  }

  function toExcel(ctx, name) {
    var d = rows(ctx);
    var html = '<html xmlns:x="urn:schemas-microsoft-com:office:excel"><head><meta charset="utf-8">' +
      '<style>th{background:#eff6ff;font-weight:bold;}' +
      'table,td,th{border:1px solid #cbd5e1;border-collapse:collapse;padding:4px;}</style></head><body><table><thead><tr>' +
      d.head.map(function (h) { return '<th>' + esc(h) + '</th>'; }).join('') +
      '</tr></thead><tbody>' +
      d.body.map(function (r) {
        return '<tr>' + r.map(function (c) { return '<td>' + esc(c) + '</td>'; }).join('') + '</tr>';
      }).join('') +
      '</tbody></table></body></html>';
    var blob = new Blob(['\ufeff' + html], { type: 'application/vnd.ms-excel' });
    var a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = name + '.xls';
    document.body.appendChild(a); a.click();
    setTimeout(function () { URL.revokeObjectURL(a.href); a.remove(); }, 0);
  }

  function toPdf(ctx, name) {
    var d = rows(ctx);
    var w = window.open('', '_blank');
    if (!w) return;
    w.document.write('<html dir="' + (IS_AR ? 'rtl' : 'ltr') + '"><head><meta charset="utf-8"><title>' +
      esc(name) + '</title><style>' +
      'body{font-family:system-ui,Segoe UI,Arial,sans-serif;padding:18px;}' +
      'h2{font-size:15px;margin:0 0 12px;color:#1e3a5f;}' +
      'table{width:100%;border-collapse:collapse;font-size:11px;}' +
      'th{background:#eff6ff;color:#1d4ed8;text-align:start;}' +
      'th,td{border:1px solid #cbd5e1;padding:5px 7px;}' +
      'tr:nth-child(even) td{background:#f8fafc;}@page{size:landscape;margin:12mm;}' +
      '</style></head><body><h2>' + esc(name) + '</h2><table><thead><tr>' +
      d.head.map(function (h) { return '<th>' + esc(h) + '</th>'; }).join('') +
      '</tr></thead><tbody>' +
      d.body.map(function (r) {
        return '<tr>' + r.map(function (c) { return '<td>' + esc(c) + '</td>'; }).join('') + '</tr>';
      }).join('') + '</tbody></table></body></html>');
    w.document.close(); w.focus();
    setTimeout(function () { w.print(); }, 250);
  }

  /* ══ Pivot panel (free, via PivotTable.js) ═══════════════════
     Additive-only. Reads the grid's current rows + visible columns and
     renders them through PivotTable.js in an overlay, giving drag-and-drop
     grouping, pivoting and aggregation (sum / count / average) without any
     change to the AG-Grid instance. Library is loaded lazily on first use
     from cdnjs, matching how the app already loads AG-Grid/Bootstrap.        */

  function loadPivotLib(cb) {
    if (window.jQuery && window.jQuery.pivotUtilities) { cb(); return; }
    function addCss(href) {
      if (document.querySelector('link[href="' + href + '"]')) return;
      var l = document.createElement('link');
      l.rel = 'stylesheet'; l.href = href; document.head.appendChild(l);
    }
    function addJs(src, done) {
      var existing = document.querySelector('script[data-gk-src="' + src + '"]');
      if (existing) { existing.addEventListener('load', done); if (existing._loaded) done(); return; }
      var s = document.createElement('script');
      s.src = src; s.setAttribute('data-gk-src', src);
      s.addEventListener('load', function () { s._loaded = true; done(); });
      document.head.appendChild(s);
    }
    addCss('https://cdnjs.cloudflare.com/ajax/libs/pivottable/2.23.0/pivot.min.css');
    function afterJq() {
      addJs('https://cdnjs.cloudflare.com/ajax/libs/pivottable/2.23.0/pivot.min.js', cb);
    }
    if (window.jQuery) afterJq();
    else addJs('https://cdnjs.cloudflare.com/ajax/libs/jquery/3.7.1/jquery.min.js', afterJq);
  }

  function gridRowsForPivot(ctx) {
    var rows = [];
    var visible = allColumns(ctx).filter(function (c) {
      var d = c.getColDef();
      // Skip action / selection / renderer-only columns.
      var f = d.field || '';
      if (!f || f.charAt(0) === '_') return false;
      if (d.checkboxSelection) return false;
      if (d.sortable === false && !f) return false;
      return true;
    });
    var api = ctx.api;
    if (!api || !api.forEachNodeAfterFilterAndSort) {
      // fallback: use unsorted nodes
      if (api && api.forEachNode) {
        api.forEachNode(function (n) { if (n.data) rows.push(rowObj(n.data, visible)); });
      }
      return { rows: rows, cols: visible };
    }
    api.forEachNodeAfterFilterAndSort(function (n) {
      if (n.data) rows.push(rowObj(n.data, visible));
    });
    return { rows: rows, cols: visible };
  }

  function rowObj(data, visible) {
    var o = {};
    visible.forEach(function (c) {
      var d = c.getColDef();
      var label = d.headerName || d.field;
      var v = data[d.field];
      o[label] = (v == null) ? '' : v;
    });
    return o;
  }

  function pivotPanel(ctx) {
    closeMenus();
    var overlay = el('div', 'gk-pivot-overlay');
    var box = el('div', 'gk-pivot-box');

    var head = el('div', 'gk-pivot-head');
    head.appendChild(el('span', 'gk-pivot-title',
      '<i class="fas fa-table-cells"></i> ' + T.pivotTitle));
    var closeB = el('button', 'gk-pivot-close', '<i class="fas fa-times"></i> ' + T.pivotClose);
    closeB.type = 'button';
    closeB.addEventListener('click', function () { overlay.remove(); });
    head.appendChild(closeB);
    box.appendChild(head);

    box.appendChild(el('div', 'gk-pivot-hint', T.pivotHint));

    var target = el('div', 'gk-pivot-target');
    box.appendChild(target);
    overlay.appendChild(box);
    document.body.appendChild(overlay);

    // click outside closes
    overlay.addEventListener('click', function (e) {
      if (e.target === overlay) overlay.remove();
    });

    target.innerHTML = '<div class="gk-pivot-loading">…</div>';
    loadPivotLib(function () {
      try {
        var data = gridRowsForPivot(ctx);
        var $ = window.jQuery;
        target.innerHTML = '';
        var $t = $(target);
        var opts = {
          rendererName: 'Table',
          aggregatorName: 'Count',
          renderers: $.extend(
            {}, $.pivotUtilities.renderers
          )
        };
        $t.pivotUI(data.rows, opts, false, IS_AR ? 'ar' : 'en');
      } catch (err) {
        target.innerHTML = '<div class="gk-pivot-loading">Pivot unavailable: ' +
          esc(err && err.message ? err.message : String(err)) + '</div>';
      }
    });
  }

  /* ══ Per-column fill/text color picker ══════════════════════════
     Every data column's header gets two small swatches -- one for its
     background fill, one for its text color -- click (or right-click the
     header) to pick from the Excel/Office theme palette, or clear back to
     none. Applied to every grid that calls enhance() by default; a page
     that already builds its own headerComponent (Payroll) opts out via
     opts.noColumnColors so the two don't collide. Choices persist per
     grid (keyed by the same storageKey used for column widths/visibility)
     in this browser's localStorage. */
  var COLOR_PALETTE = ['#4472C4', '#ED7D31', '#A5A5A5', '#FFC000', '#5B9BD5', '#70AD47',
                        '#DAE3F3', '#FCE4D6', '#EDEDED', '#FFF2CC', '#DDEBF7', '#E2EFDA'];

  function loadColorMap(key, kind) {
    try { return JSON.parse(localStorage.getItem(key + ':col' + kind)) || {}; }
    catch (e) { return {}; }
  }
  function saveColorMap(key, kind, map) {
    try { localStorage.setItem(key + ':col' + kind, JSON.stringify(map)); } catch (e) {}
  }

  var _colorPopover = null;
  var _colorPickState = null;   // { key, api, field, kind }

  function buildColorPopover() {
    if (_colorPopover) return _colorPopover;
    var pop = el('div', 'gk-color-popover');
    COLOR_PALETTE.forEach(function (hex) {
      var b = el('button', 'gk-color-opt');
      b.type = 'button'; b.style.background = hex; b.title = hex;
      b.addEventListener('click', function () { pickColor(hex); });
      pop.appendChild(b);
    });
    var empty = el('button', 'gk-color-opt gk-color-empty');
    empty.type = 'button'; empty.title = IS_AR ? 'بدون لون' : 'No color';
    empty.addEventListener('click', function () { pickColor(null); });
    pop.appendChild(empty);
    document.body.appendChild(pop);
    _colorPopover = pop;
    document.addEventListener('click', function (e) {
      if (!_colorPopover || !_colorPopover.classList.contains('show')) return;
      if (_colorPopover.contains(e.target)) return;
      if (e.target.closest && e.target.closest('.gk-col-swatch')) return;
      closeColorPopover();
    });
    return pop;
  }
  function openColorPopover(key, api, field, kind, anchorEl) {
    var pop = buildColorPopover();
    _colorPickState = { key: key, api: api, field: field, kind: kind };
    var r = anchorEl.getBoundingClientRect();
    pop.style.top = (r.bottom + 4) + 'px';
    pop.style.left = Math.min(r.left, window.innerWidth - 170) + 'px';
    pop.classList.add('show');
  }
  function closeColorPopover() {
    if (_colorPopover) _colorPopover.classList.remove('show');
    _colorPickState = null;
  }
  function pickColor(hex) {
    if (!_colorPickState) return;
    var st = _colorPickState;
    var kindKey = st.kind === 'text' ? 'TextColors' : 'Colors';
    var map = loadColorMap(st.key, kindKey);
    if (hex) map[st.field] = hex; else delete map[st.field];
    saveColorMap(st.key, kindKey, map);
    if (st.api) { st.api.refreshCells({ force: true }); st.api.refreshHeader(); }
    closeColorPopover();
  }

  function fmtMoney(n) {
    return (Number(n) || 0).toLocaleString('en-SA', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }

  /* One header-component class per grid (closed over its own storageKey +
     api) -- a plain shared class can't tell which grid it's rendering
     for, and each grid's column colors are stored under its own key.
     Also drives the optional per-column running total: any colDef the
     page marks with `gkTotal: true` gets its own Excel-SUBTOTAL-style sum
     (only currently filtered-visible rows) shown under its name, exactly
     like Payroll's own column totals. */
  function makeColorHeader(key, api) {
    var totals = {};

    function computeTotals() {
      var defs = api.getColumnDefs ? api.getColumnDefs() : [];
      var fields = (defs || []).filter(function (cd) { return cd.gkTotal; })
        .map(function (cd) { return cd.colId || cd.field; });
      if (!fields.length) return;
      var sums = {};
      fields.forEach(function (f) { sums[f] = 0; });
      if (api.forEachNodeAfterFilter) {
        api.forEachNodeAfterFilter(function (node) {
          if (!node.data) return;
          fields.forEach(function (f) {
            var v = Number(node.data[f]);
            if (!isNaN(v)) sums[f] += v;
          });
        });
      }
      totals = sums;
      // Update already-rendered header total text directly -- NEVER via
      // api.refreshHeader(), which rebuilds the floating-filter row's own
      // <input> elements and steals focus out from under anyone mid-type
      // (see the same bug, once real, in templates/payroll/list.html).
      document.querySelectorAll('.gk-htotal[data-gk-field]').forEach(function (el) {
        var f = el.getAttribute('data-gk-field');
        if (totals[f] !== undefined) el.textContent = fmtMoney(totals[f]);
      });
    }
    api.addEventListener('filterChanged', computeTotals);
    api.addEventListener('rowDataUpdated', computeTotals);
    api.addEventListener('firstDataRendered', computeTotals);

    function ColorHeader() {}
    ColorHeader.prototype.init = function (p) {
      this.params = p;
      this.eGui = document.createElement('div');
      this.eGui.className = 'gk-col-header';
      var self = this;
      this.onSortChanged = function () { self.render(); };
      if (p.column.isSortable()) {
        this.eGui.style.cursor = 'pointer';
        this.eGui.addEventListener('click', function (e) {
          if (e.target.closest && e.target.closest('.gk-col-swatch')) return;
          p.progressSort(e.shiftKey);
        });
        p.column.addEventListener('sortChanged', this.onSortChanged);
      }
      this.eGui.addEventListener('contextmenu', function (e) {
        e.preventDefault(); e.stopPropagation();
        var bg = self.eGui.querySelector('.gk-col-swatch-bg');
        openColorPopover(key, api, p.column.getColId(), 'bg', bg);
      });
      this.render();
    };
    ColorHeader.prototype.render = function () {
      var field = this.params.column.getColId();
      var sort = this.params.column.getSort();
      var sortIcon = sort === 'asc' ? ' <i class="fas fa-arrow-up gk-sort-icon"></i>'
        : sort === 'desc' ? ' <i class="fas fa-arrow-down gk-sort-icon"></i>' : '';
      var bg = loadColorMap(key, 'Colors')[field];
      var fg = loadColorMap(key, 'TextColors')[field];
      var showTotal = !!this.params.column.getColDef().gkTotal;
      var totalHtml = showTotal
        ? '<span class="gk-htotal" data-gk-field="' + field + '">' + fmtMoney(totals[field] || 0) + '</span>' : '';
      this.eGui.innerHTML =
        '<span class="gk-hname-main"><span class="gk-hname">' + esc(this.params.displayName) + sortIcon + '</span>' + totalHtml + '</span>' +
        '<span class="gk-swatch-stack">' +
        '<span class="gk-col-swatch gk-col-swatch-bg" style="' + (bg ? ('background:' + bg + ';') : '') + '"></span>' +
        '<span class="gk-col-swatch gk-col-swatch-text" style="border-bottom-color:' + (fg || '#9ca3af') + ';">A</span></span>';
      var self = this;
      var bgSwatch = this.eGui.querySelector('.gk-col-swatch-bg');
      var fgSwatch = this.eGui.querySelector('.gk-col-swatch-text');
      bgSwatch.addEventListener('click', function (e) { e.stopPropagation(); openColorPopover(key, api, field, 'bg', bgSwatch); });
      fgSwatch.addEventListener('click', function (e) { e.stopPropagation(); openColorPopover(key, api, field, 'text', fgSwatch); });
    };
    ColorHeader.prototype.refresh = function (p) { this.params = p; this.render(); return true; };
    ColorHeader.prototype.destroy = function () {
      if (this.params.column.isSortable()) this.params.column.removeEventListener('sortChanged', this.onSortChanged);
    };
    ColorHeader.prototype.getGui = function () { return this.eGui; };
    return ColorHeader;
  }

  function applyColumnColorStyle(key, field, baseCellStyle) {
    return function (p) {
      var base = typeof baseCellStyle === 'function' ? baseCellStyle(p) : baseCellStyle;
      var bg = loadColorMap(key, 'Colors')[field];
      var fg = loadColorMap(key, 'TextColors')[field];
      if (!bg && !fg) return base || null;
      var style = Object.assign({}, base || {});
      if (bg) style.backgroundColor = bg;
      if (fg) style.color = fg;
      return style;
    };
  }

  /* ══ enhance ═════════════════════════════════════════════════ */
  function enhance(params, containerId, opts) {
    opts = opts || {};

    // Accept either the onGridReady event or a bare api.
    var ctx = params && params.api ? params : { api: params, columnApi: null };
    if (!ctx.api) return;

    /* ── Consistency guarantee (applies to EVERY grid that calls enhance) ──
       - a per-column search field (floating filter) on every data column
       - a filter on every data column
       - always-visible single sort icon (unSortIcon)
       This runs once against the live columnDefs so pages that forgot to set
       these still get the standard behaviour. Action/utility columns opt out. */
    try {
      var defs = ctx.api.getColumnDefs ? ctx.api.getColumnDefs() : null;
      if (defs) {
        var changed = false;
        defs.forEach(function (cd) {
          var fld = cd.field || '';
          var isAction = (!cd.field && !cd.colId) ||
                         cd.headerName === '' ||
                         cd.suppressColumnFilter === true ||
                         cd.gkNoFilter === true ||
                         cd.checkboxSelection === true ||
                         cd.sortable === false ||             // action/utility cols
                         fld.charAt(0) === '_' ||              // e.g. _actions, _a
                         cd.cellRenderer && /Act|action/i.test(String(cd.field || cd.headerName || ''));
          if (isAction) { if (cd.floatingFilter !== false) { cd.floatingFilter = false; changed = true; } return; }
          if (cd.filter === undefined) { cd.filter = true; changed = true; }
          if (cd.floatingFilter === undefined) { cd.floatingFilter = true; changed = true; }
          if (cd.sortable !== false && cd.unSortIcon === undefined) { cd.unSortIcon = true; changed = true; }
        });
        if (changed && ctx.api.setGridOption) { ctx.api.setGridOption('columnDefs', defs); }
      }
    } catch (e) { /* non-fatal */ }

    var container = typeof containerId === 'string'
      ? document.getElementById(containerId) : containerId;
    if (!container || container._gkReady) return;
    container._gkReady = true;

    var key = opts.storageKey || containerId;
    var name = opts.exportName || 'export';

    /* ── Per-column fill/text color picker (see block above) -- on by
       default for every grid; a page with its own headerComponent already
       (Payroll) opts out via noColumnColors to avoid clobbering it. */
    if (!opts.noColumnColors) {
      try {
        var colorDefs = ctx.api.getColumnDefs ? ctx.api.getColumnDefs() : null;
        if (colorDefs) {
          var ColorHeader = makeColorHeader(key, ctx.api);
          var colorChanged = false;
          colorDefs.forEach(function (cd) {
            var fld = cd.field || '';
            var isAction = (!cd.field && !cd.colId) ||
                           cd.headerName === '' ||
                           cd.checkboxSelection === true ||
                           cd.sortable === false ||
                           fld.charAt(0) === '_' ||
                           cd.cellRenderer && /Act|action/i.test(String(cd.field || cd.headerName || ''));
            if (isAction || cd.headerComponent) return;
            var baseCellStyle = cd.cellStyle;
            cd.headerComponent = ColorHeader;
            // AG-Grid's own getColId() (what the header reads at render
            // time) resolves colId before field -- match that order here
            // so a column with both never stores its cell/header state
            // under two different keys.
            cd.cellStyle = applyColumnColorStyle(key, cd.colId || cd.field, baseCellStyle);
            colorChanged = true;
          });
          if (colorChanged && ctx.api.setGridOption) { ctx.api.setGridOption('columnDefs', colorDefs); }
        }
      } catch (e) { /* non-fatal */ }
    }

    // Captured before any saved state is restored, so "reset widths" /
    // "reset visibility" always have the page's own columnDefs-declared
    // defaults to fall back to -- not last session's saved values.
    var defaultState = colState(ctx);

    var saved = load(key + ':cols');
    if (saved && saved.length) applyState(ctx, saved);

    function persist() { var st = colState(ctx); if (st) save(key + ':cols', st); }

    var bar = el('div', 'gk-toolbar');

    function btn(icon, label, caret) {
      var b = el('button', 'gk-btn',
        '<i class="fas ' + icon + '"></i><span>' + label + '</span>' +
        (caret ? '<i class="fas fa-chevron-down gk-caret"></i>' : ''));
      b.type = 'button';
      bar.appendChild(b);
      return b;
    }

    var bCols = btn('fa-table-columns', T.columns, true);
    bCols.addEventListener('click', function (e) {
      e.stopPropagation();
      if (bCols.classList.contains('gk-open')) { closeMenus(); return; }
      columnsPanel(ctx, bCols, key, persist, defaultState);
    });

    var bFilter = btn('fa-filter', T.filters, true);
    bFilter.addEventListener('click', function (e) {
      e.stopPropagation();
      if (bFilter.classList.contains('gk-open')) { closeMenus(); return; }
      filtersPanel(ctx, bFilter);
    });

    var bExp = btn('fa-download', T.exportLbl, true);
    bExp.addEventListener('click', function (e) {
      e.stopPropagation();
      if (bExp.classList.contains('gk-open')) { closeMenus(); return; }
      exportPanel(ctx, bExp, name);
    });

    if (!opts.hidePivot) {
      var bPivot = btn('fa-table-cells', T.pivot, false);
      bPivot.addEventListener('click', function (e) {
        e.stopPropagation();
        closeMenus();
        pivotPanel(ctx);
      });
    }

    /* Every list page already renders a `.toolbar` with Add / Refresh / Clear.
       Adding a *second* bar above the grid duplicates those affordances and
       looks cluttered. Slot our three controls into that existing toolbar
       instead, and only fall back to a standalone row when a page has none. */
    var host = opts.toolbar
      ? (typeof opts.toolbar === 'string' ? document.querySelector(opts.toolbar) : opts.toolbar)
      : null;

    if (!host) {
      var page = container.closest('.proledg-page, .pur-page, .emp-page') || document;
      host = page.querySelector('.toolbar .toolbar-left') ||
             page.querySelector('.toolbar-left');
    }

    if (host) {
      bar.classList.add('gk-inline');
      // A hairline divider keeps our group visually distinct from the
      // page's own Add / Refresh buttons without adding a whole row.
      host.appendChild(el('span', 'gk-divider'));
      host.appendChild(bar);
    } else {
      container.parentNode.insertBefore(bar, container);
    }

    function badge() {
      var n = Object.keys((ctx.api.getFilterModel && ctx.api.getFilterModel()) || {}).length;
      var old = bFilter.querySelector('.gk-badge');
      if (old) old.remove();
      bFilter.classList.toggle('gk-armed', n > 0);
      if (n) {
        var s = el('span', 'gk-badge', String(n));
        bFilter.insertBefore(s, bFilter.querySelector('.gk-caret'));
      }
    }
    badge();

    ctx.api.addEventListener('filterChanged', badge);
    ['columnMoved', 'columnVisible', 'columnPinned'].forEach(function (ev) {
      ctx.api.addEventListener(ev, persist);
    });
    ctx.api.addEventListener('columnResized', function (e) { if (e.finished) persist(); });

    /* Keep the focused cell on-screen: AG-Grid already does this internally
       in most cases, but this makes it explicit and reliable everywhere --
       every arrow-key move (or click) that focuses a cell outside the
       current horizontal scroll window brings it back into view. */
    ctx.api.addEventListener('cellFocused', function (e) {
      if (e.column) ctx.api.ensureColumnVisible(e.column);
    });
  }

  window.GridKit = { enhance: enhance, fmtDate: fmtDate };
})();