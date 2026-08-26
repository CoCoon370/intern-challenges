/* 仿微信网页版 · 前端逻辑（E3 mock） */
(function () {
  'use strict';

  const $ = (s) => document.querySelector(s);
  const listEl = $('.chat-list');
  const emptyHint = $('.empty-hint');
  const chatMain = $('.chat-main');
  const chatPanel = $('.chat-panel');
  const chatTitle = $('.chat-title');
  const msgList = $('.msg-list');
  const editor = $('.editor');
  const sendBtn = $('.send-btn');
  const modalRoot = $('.modal-root');
  const connDot = $('.conn-dot');

  const state = {
    ws: null,
    convs: new Map(), // id -> {id,name,order,unread,last_ts,messages:[]}
    active: null,
    popups: new Map(), // id -> popup
    me: { name: '我' },
    clockOffsetMs: 0, // Date.now() - ts*1000  （把服务端相对秒换算成本地时间显示）
  };

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  function fmtTime(ts) {
    if (ts == null) return '';
    const d = new Date(state.clockOffsetMs + ts * 1000);
    const hh = String(d.getHours()).padStart(2, '0');
    const mm = String(d.getMinutes()).padStart(2, '0');
    return `${hh}:${mm}`;
  }

  function wsSend(obj) {
    if (state.ws && state.ws.readyState === WebSocket.OPEN) {
      state.ws.send(JSON.stringify(obj));
      return true;
    }
    return false;
  }

  // ------------------------------------------------------------------ list
  function sortedConvs() {
    return Array.from(state.convs.values()).sort((a, b) => {
      const ta = a.last_ts == null ? -1 : a.last_ts;
      const tb = b.last_ts == null ? -1 : b.last_ts;
      if (tb !== ta) return tb - ta;
      return a.order - b.order;
    });
  }

  function renderList() {
    listEl.innerHTML = '';
    for (const c of sortedConvs()) {
      const last = c.messages.length ? c.messages[c.messages.length - 1] : null;
      const preview = last ? (last.dir === 'out' ? '我: ' : '') + last.text : '';
      const item = document.createElement('div');
      item.className = 'chat-item' + (c.id === state.active ? ' active' : '');
      item.dataset.conv = c.id;
      item.innerHTML =
        `<div class="avatar">${esc(c.name.slice(0, 1))}</div>` +
        `<div class="meta">` +
        `<div class="row1"><span class="name">${esc(c.name)}</span><span class="time">${esc(fmtTime(c.last_ts))}</span></div>` +
        `<div class="row2"><span class="preview">${esc(preview)}</span>${c.unread > 0 ? `<span class="badge">${c.unread}</span>` : ''}</div>` +
        `</div>`;
      item.addEventListener('click', () => openConv(c.id));
      listEl.appendChild(item);
    }
  }

  // ------------------------------------------------------------------ panel
  function rowEl(m, convName) {
    const row = document.createElement('div');
    row.className = 'msg-row ' + (m.dir === 'in' ? 'msg-in' : 'msg-out');
    row.dataset.mid = m.mid;
    const avatar = `<div class="avatar">${esc((m.dir === 'in' ? convName : state.me.name).slice(0, 1))}</div>`;
    const bubble = `<div class="bubble">${esc(m.text)}</div>`;
    row.innerHTML = m.dir === 'in' ? avatar + bubble : bubble + avatar;
    return row;
  }

  function renderPanel() {
    const c = state.active ? state.convs.get(state.active) : null;
    if (!c) {
      emptyHint.hidden = false;
      chatMain.hidden = true;
      chatPanel.removeAttribute('data-conv');
      return;
    }
    emptyHint.hidden = true;
    chatMain.hidden = false;
    chatPanel.dataset.conv = c.id;
    chatTitle.textContent = c.name;
    msgList.innerHTML = '';
    for (const m of c.messages) msgList.appendChild(rowEl(m, c.name));
    scrollBottom();
  }

  function scrollBottom() {
    msgList.scrollTop = msgList.scrollHeight;
  }

  function openConv(id) {
    const c = state.convs.get(id);
    if (!c) return;
    state.active = id;
    c.unread = 0;
    wsSend({ type: 'read', conv_id: id });
    renderList();
    renderPanel();
    if (!editor.disabled) editor.focus();
  }

  // ------------------------------------------------------------------ popups
  function updateComposerLock() {
    const locked = state.popups.size > 0;
    editor.disabled = locked;
    sendBtn.disabled = locked;
  }

  function showPopup(p) {
    if (state.popups.has(p.id)) return;
    state.popups.set(p.id, p);
    const mask = document.createElement('div');
    mask.className = 'modal-mask';
    mask.dataset.popup = p.id;
    mask.innerHTML =
      `<div class="modal" role="dialog" aria-modal="true">` +
      `<div class="modal-title">${esc(p.title)}</div>` +
      `<div class="modal-body">${esc(p.body)}</div>` +
      `<div class="modal-footer"><button class="modal-close" type="button">知道了</button></div>` +
      `</div>`;
    mask.querySelector('.modal-close').addEventListener('click', (ev) => {
      ev.stopPropagation();
      wsSend({ type: 'popup_close', popup_id: p.id });
      removePopup(p.id);
    });
    modalRoot.appendChild(mask);
    updateComposerLock();
  }

  function removePopup(id) {
    state.popups.delete(id);
    const el = modalRoot.querySelector(`.modal-mask[data-popup="${CSS.escape(id)}"]`);
    if (el) el.remove();
    updateComposerLock();
    if (!editor.disabled && state.active) editor.focus();
  }

  // ------------------------------------------------------------------ send
  function send() {
    const text = editor.value.trim();
    if (!text || !state.active) return;
    if (!wsSend({ type: 'send', conv_id: state.active, text })) return;
    editor.value = '';
  }

  sendBtn.addEventListener('click', send);
  editor.addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter' && !ev.shiftKey && !ev.isComposing) {
      ev.preventDefault();
      send();
    }
  });

  // ------------------------------------------------------------------ ws
  function handle(msg) {
    switch (msg.type) {
      case 'snapshot': {
        state.me = msg.me || state.me;
        state.clockOffsetMs = Date.now() - msg.ts * 1000;
        state.convs = new Map(msg.conversations.map((c) => [c.id, c]));
        state.popups = new Map();
        modalRoot.innerHTML = '';
        for (const p of msg.popups || []) showPopup(p);
        if (state.active && !state.convs.has(state.active)) state.active = null;
        if (state.active) {
          state.convs.get(state.active).unread = 0;
          wsSend({ type: 'read', conv_id: state.active });
        }
        updateComposerLock();
        renderList();
        renderPanel();
        break;
      }
      case 'msg_in':
      case 'msg_out': {
        const c = state.convs.get(msg.conv_id);
        if (!c) return;
        const m = { mid: msg.msg_id, dir: msg.type === 'msg_in' ? 'in' : 'out', text: msg.text, ts: msg.ts };
        c.messages.push(m);
        c.last_ts = msg.ts;
        if (msg.type === 'msg_in') {
          if (c.id === state.active) {
            c.unread = 0;
            wsSend({ type: 'read', conv_id: c.id });
          } else {
            c.unread = (c.unread || 0) + 1;
          }
        }
        if (c.id === state.active) {
          msgList.appendChild(rowEl(m, c.name));
          scrollBottom();
        }
        renderList();
        break;
      }
      case 'popup':
        showPopup(msg);
        break;
      case 'popup_closed':
        removePopup(msg.id);
        break;
      case 'reset':
        location.reload();
        break;
      default:
        break;
    }
  }

  function connect() {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const ws = new WebSocket(`${proto}://${location.host}/ws`);
    state.ws = ws;
    ws.onopen = () => connDot.classList.add('on');
    ws.onmessage = (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch (_) { return; }
      handle(msg);
    };
    ws.onclose = () => {
      connDot.classList.remove('on');
      setTimeout(connect, 1000);
    };
    ws.onerror = () => { try { ws.close(); } catch (_) { /* noop */ } };
  }

  renderList();
  renderPanel();
  connect();
})();
