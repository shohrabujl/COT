(() => {
  'use strict';
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
  const token = () => (document.querySelector('meta[name="csrf-token"]') || {}).content || '';
  let timer;

  function toast(msg) {
    const t = $('#toast');
    if (!t) return;
    t.textContent = msg;
    t.classList.add('on');
    clearTimeout(timer);
    timer = setTimeout(() => t.classList.remove('on'), 4200);
  }

  async function post(url, body) {
    try {
      const r = await fetch(url, {
        method: 'POST', credentials: 'same-origin',
        headers: {'Content-Type': 'application/json', 'X-CSRF-Token': token()},
        body: JSON.stringify(body || {}),
      });
      let j = null;
      try { j = await r.json(); } catch (e) { /* not JSON */ }
      if (r.status === 401) { location.href = '/login'; return null; }
      if (!r.ok || !j || j.ok === false) { toast((j && j.error) || 'That did not work. Reload the page and try again.'); return null; }
      return j;
    } catch (e) {
      toast('Could not reach the app. Is the black window still open?');
      return null;
    }
  }

  // ---------- theme, auto-submitting filters, clickable rows, confirmations ----------
  document.addEventListener('click', e => {
    const t = e.target;
    if (!t.closest) return;
    if (t.closest('[data-act="theme"]')) {
      const root = document.documentElement, cur = root.getAttribute('data-theme');
      const dark = cur ? cur === 'dark' : matchMedia('(prefers-color-scheme: dark)').matches;
      root.setAttribute('data-theme', dark ? 'light' : 'dark');
      try { localStorage.setItem('ujl-theme', dark ? 'light' : 'dark'); } catch (err) { /* private mode */ }
      return;
    }
    const row = t.closest('tr[data-href]');
    if (row && !t.closest('a,button,input,select')) location.href = row.dataset.href;
  });
  document.addEventListener('change', e => {
    const f = e.target.closest && e.target.closest('form[data-auto]');
    if (f && e.target.matches('select')) (f.requestSubmit ? f.requestSubmit() : f.submit());
  });
  document.addEventListener('submit', e => {
    const m = e.target.dataset && e.target.dataset.confirm;
    if (m && !confirm(m)) e.preventDefault();
  });

  // ---------- Kanban ----------
  const board = $('#board');
  if (board) {
    const cards = JSON.parse($('#cards-data').textContent || '{}');
    const cols = $$('.col', board), stages = cols.map(c => c.dataset.stage);
    const canMove = board.dataset.canMove === '1';
    const el = {m: $('#m')};
    let cur = null;

    const cardEl = key => $$('.kc', board).find(k => k.dataset.key === key);

    function recount() {
      cols.forEach(col => {
        const list = $('.list', col), items = $$('.kc', list), none = $('.none', list);
        const pcs = items.reduce((a, k) => a + Number(k.dataset.pcs || 0), 0);
        $('.cnt', col).textContent = items.length + ' · ' + pcs.toLocaleString('en-US') + ' pcs';
        if (!items.length && !none) {
          const p = document.createElement('p');
          p.className = 'mute none';
          p.textContent = 'No cards';
          list.appendChild(p);
        }
        if (items.length && none) none.remove();
      });
    }

    function place(card, stage) {
      const col = cols.find(c => c.dataset.stage === stage);
      if (!col) return;
      const list = $('.list', col);
      const next = $$('.kc', list).find(k => k.dataset.sort > card.dataset.sort);
      list.insertBefore(card, next || null);
      card.dataset.stage = stage;
      recount();
    }

    async function move(key, stage) {
      const card = cardEl(key);
      if (!card || card.dataset.stage === stage) return;
      const r = await post('/api/card/move', {key, stage});
      if (!r) return;
      place(card, stage);
      const lock = $('.lk', card);
      if (lock) lock.hidden = false;
      if (cards[key]) Object.assign(cards[key], r.card);
    }

    function openModal(key) {
      const c = cards[key];
      if (!c || !el.m) return;
      cur = key;
      $('#m-title').textContent = c.line + ' · PF ' + c.pf;
      const st = $('#m-status');
      st.textContent = c.status;
      st.className = 'chip ' + (c.status === 'Matched' ? 'ok' : c.status === 'Incomplete' ? 'warn' : 'bad');
      $('#m-sub').textContent = [c.buyer, c.style, c.qty.toLocaleString('en-US') + ' pcs', c.start + ' → ' + c.end].join(' · ');
      $('#m-type').textContent = c.type + (c.changes ? ' · ' + c.changes + ' changes' : '');
      $('#m-what').textContent = c.changes ? c.what : '';
      $('#m-stage').value = c.stage;
      $('#m-pri').value = c.priority;
      $('#m-own').value = c.owner;
      $('#m-notes').value = c.notes;
      $('#m-lock').textContent = c.locked ? '🔒 Locked: this card stays in its stage until you unlock it.' : 'Automatic: the stage follows the plan dates and master data.';
      const un = $('[data-act="card-unlock"]');
      if (un) un.hidden = !c.locked;
      el.m.hidden = false;
      $('#m-stage').focus();
    }
    const closeModal = () => { if (el.m) el.m.hidden = true; cur = null; };

    board.addEventListener('dragstart', e => {
      const k = e.target.closest && e.target.closest('.kc');
      if (!k) return;
      e.dataTransfer.setData('text/plain', k.dataset.key);
      e.dataTransfer.effectAllowed = 'move';
    });
    board.addEventListener('dragover', e => {
      const c = e.target.closest('.col');
      if (!c || !canMove) return;
      e.preventDefault();
      cols.forEach(x => x.classList.toggle('over', x === c));
    });
    board.addEventListener('drop', e => {
      const c = e.target.closest('.col');
      cols.forEach(x => x.classList.remove('over'));
      if (!c || !canMove) return;
      e.preventDefault();
      const key = e.dataTransfer.getData('text/plain');
      if (key) move(key, c.dataset.stage);
    });
    board.addEventListener('dragend', () => cols.forEach(x => x.classList.remove('over')));
    board.addEventListener('click', e => {
      const card = e.target.closest('.kc');
      if (!card) return;
      const mv = e.target.closest('[data-mv]');
      if (mv) {
        const i = stages.indexOf(card.dataset.stage) + Number(mv.dataset.mv);
        if (i >= 0 && i < stages.length) move(card.dataset.key, stages[i]);
        return;
      }
      openModal(card.dataset.key);
    });
    board.addEventListener('keydown', e => {
      if ((e.key === 'Enter' || e.key === ' ') && e.target.classList && e.target.classList.contains('kc')) {
        e.preventDefault();
        openModal(e.target.dataset.key);
      }
    });

    if (el.m) {
      el.m.addEventListener('click', async e => {
        if (e.target === el.m || e.target.closest('[data-act="close"]')) return closeModal();
        if (e.target.closest('[data-act="card-save"]')) {
          const r = await post('/api/card/save', {key: cur, stage: $('#m-stage').value, priority: $('#m-pri').value, owner: $('#m-own').value, notes: $('#m-notes').value});
          if (r) location.reload();
        }
        if (e.target.closest('[data-act="card-unlock"]')) {
          const r = await post('/api/card/unlock', {key: cur});
          if (r) location.reload();
        }
      });
      document.addEventListener('keydown', e => { if (e.key === 'Escape') closeModal(); });
    }
  }

  // ---------- inline editing (line-wise and PF master) ----------
  document.addEventListener('focusin', e => {
    if (e.target.dataset && (e.target.dataset.k || e.target.dataset.mp)) e.target.dataset.was = e.target.value;
  });

  function setCell(td, text, cls) {
    const chip = td.firstElementChild;
    if (chip && chip.tagName === 'SPAN') { chip.textContent = text; chip.className = cls; }
    else { td.textContent = text; td.className = cls; }
  }

  const lw = $('#lw');
  if (lw) {
    lw.addEventListener('change', async e => {
      const input = e.target;
      if (!input.dataset.k || input.dataset.mp) return;
      const tr = input.closest('tr');
      const r = await post('/api/linewise/' + tr.dataset.id, {field: input.dataset.k, value: input.value});
      if (!r) { input.value = input.dataset.was || ''; return; }
      input.value = r.value;
      Object.entries(r.cells).forEach(([k, v]) => { const td = $('[data-d="' + k + '"]', tr); if (td) setCell(td, v[0], v[1]); });
      Object.entries(r.totals).forEach(([k, v]) => { const td = $('[data-t="' + k + '"]', lw); if (td) td.textContent = v; });
    });
  }

  const mt = $('#master');
  if (mt) {
    mt.addEventListener('change', async e => {
      const sel = e.target;
      if (!sel.dataset.mp) return;
      const r = await post('/api/master/cell', {i: Number(sel.dataset.mp), k: Number(sel.dataset.k), pf: sel.closest('tr').dataset.pf, v: sel.value});
      if (!r) { sel.value = sel.dataset.was || ''; return; }
      sel.closest('td').className = r.blank ? 'h1' : '';
    });
  }
})();
