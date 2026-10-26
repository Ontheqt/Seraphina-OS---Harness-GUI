/* Seraphina OS Harness web GUI: Home, the Software Factory (board, plans, chat) and the shared shell.
 * Vanilla ES2020: no build step, no framework, no external requests. Each further section of the harness lives in
 * its own file, swfactory/web/views/<area>.js, loaded after this one; it registers itself through window.SF (the
 * "view modules" section below) and reaches the shared helpers only through that one namespace.
 * Security model: untrusted strings (job text, agent output, files, diffs, config values)
 * reach the DOM only through textContent / createTextNode / setAttribute on safe attributes.
 * This file never uses innerHTML, outerHTML, insertAdjacentHTML or document.write.
 * Links built from untrusted content pass through safeHref(), which allows http(s) only. */
'use strict';
(function () {

  // ================================================================ storage + theme
  function lsGet(key) { try { return window.localStorage.getItem(key); } catch (e) { return null; } }
  function lsSet(key, value) {
    try {
      if (value === null || value === undefined) window.localStorage.removeItem(key);
      else window.localStorage.setItem(key, String(value));
    } catch (e) { /* storage unavailable (private mode, blocked): per-viewer convenience only */ }
  }

  const THEMES = ['auto', 'light', 'dark'];
  let themeMode = THEMES.indexOf(lsGet('sf.theme')) >= 0 ? lsGet('sf.theme') : 'auto';
  function applyTheme() {
    const root = document.documentElement;
    if (themeMode === 'auto') root.removeAttribute('data-theme');
    else root.setAttribute('data-theme', themeMode);
    const btn = document.getElementById('theme-toggle');
    if (btn) {
      const labels = { auto: 'auto (follows system)', light: 'light', dark: 'dark' };
      btn.title = 'Theme: ' + labels[themeMode];
      btn.setAttribute('aria-label', 'Theme: ' + labels[themeMode] + '. Click to change.');
      btn.dataset.mode = themeMode;
      const use = btn.querySelector('use');
      if (use) use.setAttribute('href', themeMode === 'light' ? '#i-sun' : themeMode === 'dark' ? '#i-moon' : '#i-contrast');
    }
  }
  applyTheme();

  // ================================================================ DOM helpers
  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));
  const SPRITE = document.getElementById('sprite');
  const SVG_NS = SPRITE ? SPRITE.namespaceURI : null;
  const PROP_KEYS = new Set(['value', 'checked', 'selected', 'disabled', 'hidden', 'open', 'readOnly', 'required', 'multiple']);

  /** h(tag, props, ...children): create an element. Strings become text nodes (never HTML). */
  function h(tag, props) {
    const el = document.createElement(tag);
    if (props) {
      for (const key of Object.keys(props)) {
        const v = props[key];
        if (v === null || v === undefined || v === false) continue;
        if (key === 'class') el.className = v;
        else if (key === 'text') el.textContent = String(v);
        else if (key === 'dataset') { for (const d of Object.keys(v)) el.dataset[d] = v[d]; }
        else if (key === 'style') { for (const s of Object.keys(v)) el.style.setProperty(s, v[s]); }
        else if (key.slice(0, 2) === 'on' && typeof v === 'function') el.addEventListener(key.slice(2), v);
        else if (PROP_KEYS.has(key)) el[key] = v;
        else el.setAttribute(key, v === true ? '' : String(v));
      }
    }
    for (let i = 2; i < arguments.length; i++) add(el, arguments[i]);
    return el;
  }
  function add(el, kid) {
    if (kid === null || kid === undefined || kid === false) return;
    if (Array.isArray(kid)) { for (const k of kid) add(el, k); return; }
    if (kid instanceof Node) el.appendChild(kid);
    else el.appendChild(document.createTextNode(String(kid)));
  }
  function clear(el) { while (el && el.firstChild) el.removeChild(el.firstChild); return el; }
  function icon(name, cls) {
    const svg = document.createElementNS(SVG_NS, 'svg');
    svg.setAttribute('class', 'icon' + (cls ? ' ' + cls : ''));
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('aria-hidden', 'true');
    svg.setAttribute('focusable', 'false');
    const use = document.createElementNS(SVG_NS, 'use');
    use.setAttribute('href', '#i-' + name);
    svg.appendChild(use);
    return svg;
  }
  function svgEl(tag, attrs) {
    const el = document.createElementNS(SVG_NS, tag);
    if (attrs) for (const k of Object.keys(attrs)) el.setAttribute(k, String(attrs[k]));
    return el;
  }
  const enc = (s) => encodeURIComponent(String(s));
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const clone = (v) => (v === undefined ? undefined : JSON.parse(JSON.stringify(v)));
  const isObj = (v) => v !== null && typeof v === 'object' && !Array.isArray(v);
  function stable(v) {
    if (Array.isArray(v)) return '[' + v.map(stable).join(',') + ']';
    if (isObj(v)) return '{' + Object.keys(v).sort().map((k) => JSON.stringify(k) + ':' + stable(v[k])).join(',') + '}';
    return JSON.stringify(v === undefined ? null : v);
  }
  /** The merge patch that turns a into b for POST /api/config (settings.merge_patch): changed keys only, nested
   *  objects as their own diff, null for a removed key (the server drops it), lists and scalars whole. */
  function mergeDiff(a, b) {
    if (!isObj(a) || !isObj(b)) return clone(b);
    const out = {};
    for (const k of Object.keys(a)) if (b[k] === undefined) out[k] = null;
    for (const k of Object.keys(b)) {
      if (b[k] === undefined || stable(a[k]) === stable(b[k])) continue;
      out[k] = a[k] !== undefined && isObj(a[k]) && isObj(b[k]) ? mergeDiff(a[k], b[k]) : clone(b[k]);
    }
    return out;
  }
  /** A <select>: options are [value, label] pairs (or plain values); {group, options} adds an <optgroup>. */
  function selectEl(options, value, attrs) {
    const sel = h('select', Object.assign({ class: 'select' }, attrs || {}));
    const opt = (o) => { const pair = Array.isArray(o) ? o : [o, o]; return h('option', { value: String(pair[0]) }, String(pair[1])); };
    for (const o of options) {
      if (isObj(o) && Array.isArray(o.options)) { if (o.options.length) sel.appendChild(h('optgroup', { label: o.group }, o.options.map(opt))); }
      else sel.appendChild(opt(o));
    }
    const want = value === null || value === undefined ? '' : String(value);
    sel.value = want;
    if (sel.value !== want) {
      sel.appendChild(h('option', { value: want }, want + ' (not configured)'));
      sel.value = want;
    }
    return sel;
  }
  function field(label, control, hint, opts) {
    const id = control && control.id ? control.id : 'f-' + Math.random().toString(36).slice(2, 9);
    if (control && !control.id && /^(INPUT|SELECT|TEXTAREA)$/.test(control.tagName)) control.id = id;
    return h('div', { class: 'field' + (opts && opts.cls ? ' ' + opts.cls : '') },
      h('label', { class: 'field-label', for: id }, label),
      control,
      hint ? h('div', { class: 'field-hint' }, hint) : null);
  }
  function callout(kind, iconName, title, text) {
    return h('div', { class: 'callout callout-' + kind }, icon(iconName, 'icon-sm'),
      h('div', null, title ? h('strong', null, title) : null, title && text ? ' ' : null, text || null));
  }
  function emptyState(iconName, title, text, actions, small) {
    return h('div', { class: 'empty' + (small ? ' empty-sm' : '') },
      h('div', { class: 'empty-icon' }, icon(iconName, 'icon-lg')),
      h('div', { class: 'empty-title' }, title),
      text ? h('div', { class: 'empty-text' }, text) : null,
      actions && actions.length ? h('div', { class: 'empty-actions' }, actions) : null);
  }
  function btn(label, opts) {
    opts = opts || {};
    const b = h('button', {
      type: opts.type || 'button',
      class: 'btn ' + (opts.cls || ''),
      title: opts.title || null,
      'aria-label': opts.aria || null,
      disabled: !!opts.disabled,
      dataset: opts.dataset || null,
    }, opts.icon ? icon(opts.icon, opts.iconCls || 'icon-sm') : null, label ? h('span', null, label) : null);
    if (opts.onClick) b.addEventListener('click', opts.onClick);
    return b;
  }
  function setBusy(button, busy) {
    if (!button) return;
    button.disabled = !!busy;
    button.classList.toggle('is-busy', !!busy);
    button.setAttribute('aria-busy', busy ? 'true' : 'false');
  }
  function skeletonLines(n) {
    const box = h('div', { class: 'skel-box', 'aria-hidden': 'true' });
    for (let i = 0; i < n; i++) box.appendChild(h('div', { class: 'skel skel-line', style: { width: (55 + ((i * 37) % 40)) + '%' } }));
    return h('div', null, h('span', { class: 'sr-only' }, 'Loading…'), box);
  }

  // ---------------------------------------------------------------- horizontal scroll cues
  // Scrollers that may hide content sideways get data-fade="l|r|lr"; CSS turns that into edge fades.
  const FADES = new Set();
  function trackFades(el, adjust) {
    if (!el || el._fades) return el && el._fades;
    const update = () => {
      const max = el.scrollWidth - el.clientWidth;
      let f = '';
      if (max > 2 && el.scrollLeft > 2) f += 'l';
      if (max > 2 && el.scrollLeft < max - 2) f += 'r';
      if (adjust) f = adjust(f);
      if (el.dataset.fade !== f) el.dataset.fade = f;
    };
    el._fades = update;
    el.addEventListener('scroll', update, { passive: true });
    FADES.add(el);
    update();
    return update;
  }
  function refreshFades() {
    for (const el of FADES) { if (el.isConnected) el._fades(); else FADES.delete(el); }
  }

  // ================================================================ formatting
  function parseTs(ts) {
    if (typeof ts === 'number') return ts;
    const t = Date.parse(ts || '');
    return isFinite(t) ? t : NaN;
  }
  function relTime(ts) {
    const t = parseTs(ts);
    if (!isFinite(t)) return '';
    let s = Math.round((Date.now() - t) / 1000);
    if (s < 0) s = 0;
    if (s < 10) return 'just now';
    if (s < 60) return s + 's ago';
    const m = Math.floor(s / 60);
    if (m < 60) return m + 'm ago';
    const hr = Math.floor(m / 60);
    if (hr < 24) return hr + 'h ago';
    const d = Math.floor(hr / 24);
    if (d < 30) return d + 'd ago';
    return new Date(t).toLocaleDateString();
  }
  function timeEl(ts, cls) {
    const t = parseTs(ts);
    const el = h('time', { class: cls || 'rel', datetime: ts || null, title: isFinite(t) ? new Date(t).toLocaleString() : null }, relTime(ts));
    if (ts) el.dataset.rel = String(ts);
    return el;
  }
  function fmtDur(ms) {
    if (!(ms >= 0)) return '';
    if (ms < 1000) return '<1s';
    const s = Math.floor(ms / 1000);
    if (s < 60) return s + 's';
    const m = Math.floor(s / 60);
    if (m < 60) return m + 'm ' + String(s % 60).padStart(2, '0') + 's';
    const hr = Math.floor(m / 60);
    return hr + 'h ' + String(m % 60).padStart(2, '0') + 'm';
  }
  function fmtClock(ms) {
    const s = Math.max(0, Math.floor(ms / 1000));
    const m = Math.floor(s / 60);
    if (m >= 60) return Math.floor(m / 60) + ':' + String(m % 60).padStart(2, '0') + ':' + String(s % 60).padStart(2, '0');
    return m + ':' + String(s % 60).padStart(2, '0');
  }
  function fmtNum(n) {
    n = Number(n) || 0;
    if (n < 1000) return String(n);
    if (n < 1e6) return (n / 1000).toFixed(n < 1e4 ? 1 : 0).replace(/\.0$/, '') + 'k';
    return (n / 1e6).toFixed(1).replace(/\.0$/, '') + 'M';
  }
  function fmtUsage(u, long) {
    if (!u || typeof u !== 'object') return '';
    const parts = [];
    if (u.input_tokens) parts.push(fmtNum(u.input_tokens) + (long ? ' in' : ' in'));
    if (u.output_tokens) parts.push(fmtNum(u.output_tokens) + ' out');
    if (long && u.cached_tokens) parts.push(fmtNum(u.cached_tokens) + ' cached');
    if (long && u.calls) parts.push(u.calls + (u.calls === 1 ? ' call' : ' calls'));
    return parts.join(' · ');
  }
  function plural(n, one, many) { return n + ' ' + (n === 1 ? one : (many || one + 's')); }
  function firstLine(s) { return String(s || '').replace(/^\s+/, '').split('\n')[0].trim(); }
  /** A provider is LOCAL when its base_url points at this machine (loopback), else it is an API. */
  function isLoopback(url) {
    const m = /^[a-z][a-z0-9+.-]*:\/\/(\[[^\]]*\]|[^/:?#]*)/i.exec(String(url || '').trim());
    if (!m) return false;
    const host = m[1].toLowerCase().replace(/^\[|\]$/g, '');
    return host === 'localhost' || /^127(\.\d{1,3}){3}$/.test(host) || host === '::1';
  }
  /** Plan jobs carry "Plan context: <plan-id> — see …" (plans._job_feature): the plan they belong to. */
  const PLAN_CTX_RE = /(?:^|\n)Plan context: (P\d{3}-[a-z0-9]+(?:-[a-z0-9]+)*) — see /g;
  function planOfJob(job) {
    let m;
    let id = null;
    const text = String((job && job.feature) || '');
    PLAN_CTX_RE.lastIndex = 0;
    while ((m = PLAN_CTX_RE.exec(text))) id = m[1];
    return id;
  }
  /** The feature as a person wrote it: without the plan context and review lines the plan appends. */
  function featureTitle(job) {
    const text = String((job && job.feature) || '');
    const cut = text.search(/\n\n(?:Plan context: P\d{3}-|From final review \d+ of the plan: )/);
    return (cut >= 0 ? text.slice(0, cut) : text).trim();
  }

  // ---------------------------------------------------------------- measured model speed (Test results)
  // Per viewer: the last tokens/s and tool-call result Test measured for each model key, for the Routing badges.
  const TPS = (function () {
    let m = {};
    try { m = JSON.parse(lsGet('sf.tps') || '{}') || {}; } catch (e) { m = {}; }
    return isObj(m) ? m : {};
  })();
  function recordTest(key, r) {
    if (!r || !r.ok) return;
    const tps = Number(r.tokens_per_second);
    TPS[key] = { tps: isFinite(tps) && tps > 0 ? tps : (TPS[key] && TPS[key].tps) || null, tool: r.tool_call === true, at: Date.now() };
    lsSet('sf.tps', JSON.stringify(TPS));
  }
  function fmtTps(n) { return (n >= 100 ? Math.round(n) : Math.round(n * 10) / 10) + ' tok/s'; }

  // ---------------------------------------------------------------- reviewer chip labels
  /** DESIGN §22: first letter uppercased, deduplicated with a second letter (security, style → S, St). */
  function reviewerLabels(list) {
    const out = {};
    const used = new Set();
    for (const raw of list) {
      const name = String(raw);
      if (out[name]) continue;
      const first = (name.charAt(0) || '?').toUpperCase();
      let label = used.has(first) ? null : first;
      for (let i = 1; !label && i < name.length; i++) {
        const c = name.charAt(i);
        if (/[a-z0-9]/i.test(c) && !used.has(first + c.toLowerCase())) label = first + c.toLowerCase();
      }
      for (let n = 2; !label; n++) if (!used.has(first + n)) label = first + n;
      used.add(label);
      out[name] = label;
    }
    return out;
  }

  /** The single place a URL from untrusted content may become a link: http(s) only. */
  function safeHref(url) {
    const u = String(url || '').trim();
    if (!/^https?:\/\/[^\s<>"'`\\]+$/i.test(u)) return null;
    return u;
  }

  // ================================================================ markdown (safe DOM builder)
  // Escape-first by construction: every character of the source becomes text nodes; only the
  // structures below create elements. No raw HTML, no images, links only through safeHref().
  const MD = (function () {
    const LIST_RE = /^(\s*)([-*+]|\d{1,9}[.)])(\s+|$)(.*)$/;
    const FENCE_RE = /^(\s{0,3})(`{3,}|~{3,})\s*([\w+#.-]*)/;
    const HR_RE = /^\s{0,3}([-*_])(?:\s*\1){2,}\s*$/;
    const HEAD_RE = /^\s{0,3}(#{1,6})(?:[ \t]+(.*?))?(?:[ \t]+#+)?[ \t]*$/;
    const QUOTE_RE = /^\s{0,3}>/;
    const TABLE_SEP_RE = /^\s*\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)*\|?\s*$/;
    const MAX_DEPTH = 12;
    const RE = {
      code: /(`+)([\s\S]*?[^`])\1(?!`)/y,
      tick: /`+/y,
      bold1: /\*\*(?=\S)([\s\S]*?\S)\*\*/y,
      bold2: /__(?=\S)([\s\S]*?\S)__(?!\w)/y,
      em1: /\*(?=[^\s*])([\s\S]*?[^\s*])\*(?!\*)/y,
      em2: /_(?=[^\s_])([\s\S]*?[^\s_])_(?!\w)/y,
      strike: /~~(?=\S)([\s\S]*?\S)~~/y,
      link: /\[((?:\\.|[^\\[\]]|\[(?:\\.|[^\\[\]])*\])*)\]\(\s*<?([^\s<>()]*(?:\([^\s<>()]*\)[^\s<>()]*)*)>?(?:\s+(?:"[^"]*"|'[^']*'))?\s*\)/y,
      auto: /<(https?:\/\/[^\s<>]+)>/iy,
      bare: /https?:\/\/[^\s<>()[\]"'`]*[^\s<>()[\]"'`.,;:!?*_~]/iy,
    };

    function render(src, cls) {
      const root = h('div', { class: 'md' + (cls ? ' ' + cls : '') });
      const text = String(src === null || src === undefined ? '' : src).replace(/\r\n?/g, '\n').replace(/^﻿/, '');
      blocks(text.split('\n').map((l) => l.replace(/\t/g, '    ')), root, 0);
      return root;
    }

    function splitRow(line) {
      let s = line.trim();
      if (s.startsWith('|')) s = s.slice(1);
      if (s.endsWith('|') && !s.endsWith('\\|')) s = s.slice(0, -1);
      const cells = [];
      let cur = '';
      let inCode = false;
      for (let k = 0; k < s.length; k++) {
        const c = s[k];
        if (c === '\\' && s[k + 1] === '|') { cur += '|'; k++; continue; }
        if (c === '`') inCode = !inCode;
        if (c === '|' && !inCode) { cells.push(cur.trim()); cur = ''; continue; }
        cur += c;
      }
      cells.push(cur.trim());
      return cells;
    }
    function isTableStart(lines, i) {
      if (i + 1 >= lines.length || lines[i].indexOf('|') < 0) return false;
      const sep = lines[i + 1];
      if (!TABLE_SEP_RE.test(sep) || sep.indexOf('-') < 0) return false;
      return splitRow(sep).length === splitRow(lines[i]).length;
    }
    function startsBlock(lines, i) {
      const l = lines[i];
      return FENCE_RE.test(l) || HEAD_RE.test(l) || HR_RE.test(l) || QUOTE_RE.test(l) || LIST_RE.test(l) || isTableStart(lines, i);
    }

    function codeBlock(text, lang) {
      const code = h('code', { text });
      if (lang) code.className = 'lang-' + lang.toLowerCase().replace(/[^a-z0-9+#-]/g, '');
      const pre = h('pre', null, code);
      const copy = h('button', { type: 'button', class: 'btn btn-xs btn-icon copy-code', title: 'Copy code', 'aria-label': 'Copy code' }, icon('copy', 'icon-xs'));
      copy.addEventListener('click', (e) => { e.preventDefault(); e.stopPropagation(); copyText(text, 'Code copied'); });
      pre.appendChild(copy);
      return pre;
    }

    function blocks(lines, parent, depth) {
      let i = 0;
      while (i < lines.length) {
        const line = lines[i];
        if (!line.trim()) { i++; continue; }
        let m = FENCE_RE.exec(line);
        if (m) {
          const indent = m[1].length;
          const fence = m[2];
          const close = new RegExp('^\\s{0,3}' + (fence[0] === '`' ? '`' : '~') + '{' + fence.length + ',}\\s*$');
          const strip = indent ? new RegExp('^ {0,' + indent + '}') : null;
          const body = [];
          i++;
          while (i < lines.length && !close.test(lines[i])) { body.push(strip ? lines[i].replace(strip, '') : lines[i]); i++; }
          i++;
          parent.appendChild(codeBlock(body.join('\n'), m[3] || ''));
          continue;
        }
        m = HEAD_RE.exec(line);
        if (m) {
          const el = h('h' + m[1].length);
          inline(m[2] || '', el, depth, false);
          parent.appendChild(el);
          i++;
          continue;
        }
        if (HR_RE.test(line)) { parent.appendChild(h('hr')); i++; continue; }
        if (QUOTE_RE.test(line)) {
          const q = [];
          while (i < lines.length && lines[i].trim() && (QUOTE_RE.test(lines[i]) || (q.length && !startsBlock(lines, i)))) {
            q.push(lines[i].replace(/^\s{0,3}>\s?/, ''));
            i++;
          }
          const bq = h('blockquote');
          if (depth < MAX_DEPTH) blocks(q, bq, depth + 1); else bq.textContent = q.join('\n');
          parent.appendChild(bq);
          continue;
        }
        if (isTableStart(lines, i)) { i = table(lines, i, parent, depth); continue; }
        if (LIST_RE.test(line)) { i = list(lines, i, parent, depth); continue; }
        const para = [line.replace(/^\s+/, '')];
        i++;
        while (i < lines.length && lines[i].trim() && !startsBlock(lines, i)) { para.push(lines[i].replace(/^\s+/, '')); i++; }
        const p = h('p');
        inline(para.join('\n'), p, depth, false);
        parent.appendChild(p);
      }
    }

    function table(lines, i, parent, depth) {
      const head = splitRow(lines[i]);
      const aligns = splitRow(lines[i + 1]).map((c) => (/^:-+:$/.test(c) ? 'center' : /-+:$/.test(c) ? 'right' : /^:-+$/.test(c) ? 'left' : ''));
      const tbl = h('table');
      const tr = h('tr');
      head.forEach((c, k) => {
        const th = h('th');
        if (aligns[k]) th.style.textAlign = aligns[k];
        inline(c, th, depth, false);
        tr.appendChild(th);
      });
      tbl.appendChild(h('thead', null, tr));
      const tbody = h('tbody');
      i += 2;
      while (i < lines.length && lines[i].trim() && lines[i].indexOf('|') >= 0) {
        const cells = splitRow(lines[i]);
        const row = h('tr');
        for (let k = 0; k < head.length; k++) {
          const td = h('td');
          if (aligns[k]) td.style.textAlign = aligns[k];
          inline(cells[k] || '', td, depth, false);
          row.appendChild(td);
        }
        tbody.appendChild(row);
        i++;
      }
      if (tbody.firstChild) tbl.appendChild(tbody);
      parent.appendChild(h('div', { class: 'md-table-wrap' }, tbl));
      return i;
    }

    function list(lines, i, parent, depth) {
      const first = LIST_RE.exec(lines[i]);
      const baseIndent = first[1].length;
      const ordered = /\d/.test(first[2]);
      const el = h(ordered ? 'ol' : 'ul');
      if (ordered) {
        const n = parseInt(first[2], 10);
        if (isFinite(n) && n !== 1) el.setAttribute('start', String(n));
      }
      const items = [];
      let loose = false;
      while (i < lines.length) {
        const m = LIST_RE.exec(lines[i]);
        if (!m || m[1].length < baseIndent || /\d/.test(m[2]) !== ordered) break;
        if (HR_RE.test(lines[i])) break;
        const contentIndent = m[1].length + m[2].length + Math.min(Math.max(m[3].length, 1), 4);
        const itemLines = [m[4]];
        i++;
        let blank = false;
        while (i < lines.length) {
          const l = lines[i];
          if (!l.trim()) { itemLines.push(''); blank = true; i++; continue; }
          const ind = l.length - l.replace(/^\s+/, '').length;
          if (ind >= baseIndent + 2) { itemLines.push(l.slice(Math.min(ind, contentIndent))); blank = false; i++; continue; }
          if (blank || LIST_RE.test(l) || startsBlock(lines, i)) break;
          itemLines.push(l.replace(/^\s+/, ''));
          i++;
        }
        let trailing = 0;
        while (itemLines.length > 1 && itemLines[itemLines.length - 1] === '') { itemLines.pop(); trailing++; }
        if (trailing && i < lines.length) {
          const nm = LIST_RE.exec(lines[i]);
          if (nm && nm[1].length >= baseIndent && /\d/.test(nm[2]) === ordered) loose = true;
        }
        if (itemLines.indexOf('') > 0) loose = true;
        items.push(itemLines);
      }
      for (const itemLines of items) {
        const li = h('li');
        const tm = /^\[([ xX])\](?:\s+|$)/.exec(itemLines[0]);
        if (tm) {
          const cb = h('input', { type: 'checkbox', disabled: true, 'aria-label': tm[1] === ' ' ? 'Not done' : 'Done' });
          cb.checked = tm[1] !== ' ';
          li.appendChild(cb);
          li.className = 'task';
          itemLines[0] = itemLines[0].slice(tm[0].length);
        }
        const box = h('div');
        if (depth < MAX_DEPTH) blocks(itemLines, box, depth + 1); else box.textContent = itemLines.join('\n');
        if (!loose) {
          for (const p of Array.from(box.children)) {
            if (p.tagName !== 'P') continue;
            while (p.firstChild) box.insertBefore(p.firstChild, p);
            box.removeChild(p);
          }
        }
        while (box.firstChild) li.appendChild(box.firstChild);
        el.appendChild(li);
      }
      parent.appendChild(el);
      return i;
    }

    function inline(src, parent, depth, noLinks) {
      if (depth > MAX_DEPTH) { parent.appendChild(document.createTextNode(src)); return; }
      let buf = '';
      const flush = () => { if (buf) { parent.appendChild(document.createTextNode(buf)); buf = ''; } };
      const at = (re, i) => { re.lastIndex = i; return re.exec(src); };
      const wrap = (tag, content, cls) => {
        flush();
        const el = h(tag, cls ? { class: cls } : null);
        inline(content, el, depth + 1, noLinks);
        parent.appendChild(el);
      };
      const link = (url, label) => {
        const href = noLinks ? null : safeHref(url);
        flush();
        if (href) {
          const a = h('a', { href, target: '_blank', rel: 'noopener noreferrer' });
          if (label === null) a.textContent = url; else inline(label, a, depth + 1, true);
          parent.appendChild(a);
        } else if (label === null) {
          parent.appendChild(document.createTextNode(url));
        } else {
          const s = h('span', { class: 'md-blocked', title: 'Link removed: only http(s) links are allowed' });
          inline(label, s, depth + 1, true);
          parent.appendChild(s);
        }
      };
      let i = 0;
      while (i < src.length) {
        const c = src[i];
        let m;
        if (c === '\\') {
          const n = src[i + 1];
          if (n === '\n') { flush(); parent.appendChild(h('br')); i += 2; continue; }
          if (n && /[!-/:-@[-`{-~]/.test(n)) { buf += n; i += 2; continue; }
        } else if (c === '\n') {
          if (/ {2,}$/.test(buf)) { buf = buf.replace(/ +$/, ''); flush(); parent.appendChild(h('br')); i++; continue; }
        } else if (c === '`') {
          m = at(RE.code, i);
          if (m) {
            flush();
            let t = m[2];
            if (t.length > 2 && t[0] === ' ' && t[t.length - 1] === ' ' && t.trim()) t = t.slice(1, -1);
            parent.appendChild(h('code', { text: t }));
            i = m.index + m[0].length;
            continue;
          }
          m = at(RE.tick, i);
          buf += m[0];
          i += m[0].length;
          continue;
        } else if (c === '!' && src[i + 1] === '[') {
          m = at(RE.link, i + 1);
          if (m) {
            flush();
            const alt = m[1].replace(/\\(.)/g, '$1');
            parent.appendChild(h('span', { class: 'md-img', title: 'Images are not loaded', text: '[image' + (alt ? ': ' + alt : '') + ']' }));
            i = m.index + m[0].length;
            continue;
          }
        } else if (c === '[') {
          m = at(RE.link, i);
          // Nested calls reuse these sticky regexes and move lastIndex: always advance from the match itself.
          if (m) { const end = m.index + m[0].length; link(m[2], m[1]); i = end; continue; }
        } else if (c === '<') {
          m = at(RE.auto, i);
          if (m) { const end = m.index + m[0].length; link(m[1], null); i = end; continue; }
        } else if ((c === 'h' || c === 'H') && !/[\w/]/.test(src[i - 1] || '')) {
          m = at(RE.bare, i);
          if (m) { const end = m.index + m[0].length; link(m[0], null); i = end; continue; }
        } else if (c === '*' || c === '_') {
          if (!(c === '_' && /\w/.test(src[i - 1] || ''))) {
            const strongRe = c === '*' ? RE.bold1 : RE.bold2;
            const emRe = c === '*' ? RE.em1 : RE.em2;
            if (src[i + 1] === c && (m = at(strongRe, i))) { const end = m.index + m[0].length; wrap('strong', m[1]); i = end; continue; }
            if ((m = at(emRe, i))) { const end = m.index + m[0].length; wrap('em', m[1]); i = end; continue; }
          }
          let j = i;
          while (src[j] === c) j++;
          buf += src.slice(i, j);
          i = j;
          continue;
        } else if (c === '~' && src[i + 1] === '~') {
          m = at(RE.strike, i);
          if (m) { const end = m.index + m[0].length; wrap('del', m[1]); i = end; continue; }
        }
        buf += c;
        i++;
      }
      flush();
    }

    return { render };
  })();

  // ================================================================ verdicts
  function firstLineVerdict(text) {
    const lines = String(text || '').replace(/^﻿/, '').split('\n');
    const line = lines.find((l) => l.trim());
    if (!line) return null;
    const t = line.trim().replace(/^#+\s*/, '').replace(/^[*_`\s]+|[*_`\s]+$/g, '');
    const m = /^(?:VERDICT: (PASS|CHANGES)|STATUS: (READY|BLOCKED)|(APPROVE|ESCALATE))$/.exec(t);
    return m ? (m[1] || m[2] || m[3]) : null;
  }
  const VERDICT_TONE = { PASS: 'ok', READY: 'ok', APPROVE: 'ok', CHANGES: 'warn', ESCALATE: 'human', BLOCKED: 'err', ERROR: 'err' };
  // Server and agent strings never become class names directly: map them through allow-lists.
  const pick = (v, allowed, fallback) => (allowed.indexOf(v) >= 0 ? v : fallback);
  const CHIP_STATES = ['PASS', 'CHANGES', 'ERROR', 'pending', 'running'];
  const CHAT_STATES = ['idle', 'running', 'error'];
  const LOG_LEVELS = ['info', 'warn', 'error'];
  function verdictTag(v) {
    if (!v) return null;
    const tone = VERDICT_TONE[v] || 'neutral';
    const cls = tone === 'human' ? 'tag-warn' : 'tag-' + tone;
    return h('span', { class: 'tag verdict ' + cls, title: 'First line: ' + v }, v);
  }

  // ================================================================ diff
  function stripDiffPrefix(p) { return p.replace(/^[ab]\//, '').replace(/\t.*$/, ''); }
  function parseDiff(text) {
    const files = [];
    let cur = null;
    const lines = String(text || '').split('\n');
    if (lines.length && lines[lines.length - 1] === '') lines.pop();
    const fresh = (header, path) => ({ header, path, meta: [], lines: [], add: 0, del: 0, binary: false, status: '' });
    for (const line of lines) {
      if (line.startsWith('diff --git ') || line.startsWith('diff --cc ')) {
        const m = / b\/(.*)$/.exec(line);
        cur = fresh(line, m ? m[1] : line.replace(/^diff --\w+ /, ''));
        files.push(cur);
        continue;
      }
      if (!cur) { cur = fresh('', ''); files.push(cur); }
      if (!cur.lines.length && !line.startsWith('@@') &&
          /^(index |old mode|new mode|similarity|dissimilarity|rename |copy |deleted file|new file|Binary files|--- |\+\+\+ )/.test(line)) {
        cur.meta.push(line);
        if (line.startsWith('+++ ') && line.slice(4).trim() !== '/dev/null') cur.path = stripDiffPrefix(line.slice(4));
        else if (line.startsWith('--- ') && !cur.path && line.slice(4).trim() !== '/dev/null') cur.path = stripDiffPrefix(line.slice(4));
        if (line.startsWith('new file')) cur.status = 'added';
        else if (line.startsWith('deleted file')) cur.status = 'deleted';
        else if (line.startsWith('rename ')) cur.status = 'renamed';
        if (line.startsWith('Binary files')) cur.binary = true;
        continue;
      }
      cur.lines.push(line);
      if (line.startsWith('+')) cur.add++;
      else if (line.startsWith('-')) cur.del++;
    }
    return files.filter((f) => f.header || f.lines.length || f.meta.length);
  }
  function renderHunks(f) {
    const wrap = h('div', { class: 'diff-table' });
    let oldN = 0;
    let newN = 0;
    const MAX = 6000;
    const n = Math.min(f.lines.length, MAX);
    for (let k = 0; k < n; k++) {
      const line = f.lines[k];
      let cls = 'ctx';
      let a = '';
      let b = '';
      if (line.startsWith('@@')) {
        const m = /^@@+ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/.exec(line);
        if (m) { oldN = +m[1]; newN = +m[2]; }
        cls = 'hunk';
      } else if (line.startsWith('+')) { cls = 'add'; b = newN++; }
      else if (line.startsWith('-')) { cls = 'del'; a = oldN++; }
      else if (line.startsWith('\\')) { cls = 'meta'; }
      else { a = oldN++; b = newN++; }
      wrap.appendChild(h('div', { class: 'dl ' + cls },
        h('span', { class: 'ln', text: String(a) }), h('span', { class: 'ln', text: String(b) }),
        h('span', { class: 'code', text: line || ' ' })));
    }
    const out = h('div', null, wrap);
    if (f.lines.length > MAX) out.appendChild(h('div', { class: 'diff-more' }, (f.lines.length - MAX) + ' more lines not shown.'));
    if (f.binary) out.appendChild(h('div', { class: 'diff-more' }, 'Binary file: no textual diff.'));
    if (!f.lines.length && !f.binary) out.appendChild(h('div', { class: 'diff-more' }, f.meta.join('\n') || 'No content changes.'));
    return out;
  }
  function renderStat(stat) {
    const box = h('div', { class: 'diff-stat', 'aria-label': 'Diff stat' });
    for (const line of String(stat).split('\n')) {
      if (!line.trim()) continue;
      const m = /^\s*(.+?)\s+\|\s+(\d+|Bin[^+-]*?)\s*([+-]*)\s*$/.exec(line);
      if (m) {
        const plus = (m[3].match(/\+/g) || []).length;
        box.appendChild(h('div', { class: 'stat-row' },
          h('span', { class: 'stat-path', title: m[1] }, m[1]),
          h('span', { class: 'stat-n' }, m[2]),
          h('span', { class: 'stat-bar' }, h('span', { class: 'stat-add' }, '+'.repeat(plus)), h('span', { class: 'stat-del' }, '-'.repeat(m[3].length - plus)))));
      } else {
        box.appendChild(h('div', { class: 'stat-summary' }, line.trim()));
      }
    }
    return box;
  }
  function renderDiff(data, onRetry) {
    const root = h('div', { class: 'diff' });
    if (data && data.error) {
      // The server answers 200 with an error field when git fails (missing base, deleted branch).
      root.appendChild(h('div', { class: 'error-state', role: 'alert' },
        h('div', { class: 'error-state-icon' }, icon('error', 'icon-lg')),
        h('div', { class: 'error-state-main' },
          h('div', { class: 'error-state-title' }, 'git could not produce the diff'),
          h('div', { class: 'error-state-text mono' }, String(data.error))),
        onRetry ? btn('Retry', { cls: 'btn-sm', icon: 'refresh', onClick: onRetry }) : null));
    }
    if (data && data.truncated) {
      root.appendChild(callout('warn', 'alert', 'Diff truncated.', 'The server cut this diff at its size limit; inspect the branch with git for the rest.'));
      root.lastChild.style.marginBottom = '12px';
    }
    if (data && data.stat && String(data.stat).trim()) root.appendChild(renderStat(data.stat));
    const files = parseDiff(data && data.diff);
    if (!files.length) {
      if (!(data && data.error)) root.appendChild(emptyState('git', 'No changes on the branch yet', 'The diff against the base branch appears here once the builder commits.', null, true));
      return root;
    }
    const total = files.reduce((n, f) => n + f.lines.length, 0);
    files.forEach((f, idx) => {
      const det = h('details', { class: 'diff-file' });
      const statusCls = f.status === 'added' ? 'tag-ok' : f.status === 'deleted' ? 'tag-err' : 'tag-outline';
      det.appendChild(h('summary', null,
        icon('chevron', 'icon-sm chev'),
        h('span', { class: 'diff-path', title: f.path }, f.path || '(diff)'),
        f.status ? h('span', { class: 'tag ' + statusCls }, f.status) : null,
        f.binary ? h('span', { class: 'tag tag-outline' }, 'binary') : null,
        h('span', { class: 'diff-counts' }, h('span', { class: 'stat-add' }, '+' + f.add), h('span', { class: 'stat-del' }, '−' + f.del))));
      const body = h('div', { class: 'diff-body' });
      det.appendChild(body);
      let rendered = false;
      const renderBody = () => { if (!rendered) { rendered = true; body.appendChild(renderHunks(f)); } };
      det.addEventListener('toggle', () => { if (det.open) renderBody(); });
      if (total < 4000 || idx < 3) { det.open = true; renderBody(); }
      root.appendChild(det);
    });
    return root;
  }

  // ================================================================ API
  class ApiError extends Error {
    /** data: the server's reply body ({error, ...}: e.g. a 409's open_blocking), or null. */
    constructor(status, message, data) { super(message); this.status = status; this.data = data === undefined ? null : data; }
  }
  let unauthorized = false;
  function onUnauthorized() {
    if (unauthorized) return;
    unauthorized = true;
    window.location.replace('/');
  }
  async function api(path, opts) {
    opts = opts || {};
    const method = opts.method || (opts.body !== undefined ? 'POST' : 'GET');
    const init = { method, credentials: 'same-origin', headers: { Accept: opts.text ? 'text/plain, */*' : 'application/json' }, cache: 'no-store' };
    if (method !== 'GET') {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(opts.body === undefined ? {} : opts.body);
    }
    let res;
    try { res = await fetch(path, init); }
    catch (e) { throw new ApiError(0, 'Cannot reach the server.'); }
    if (res.status === 401) { onUnauthorized(); throw new ApiError(401, 'Not signed in.'); }
    const ct = res.headers.get('content-type') || '';
    let data = null;
    if (!opts.text && ct.indexOf('json') >= 0) {
      try { data = await res.json(); } catch (e) { data = null; }
    } else {
      data = await res.text();
      if (!res.ok && ct.indexOf('json') >= 0) { try { data = JSON.parse(data); } catch (e) { /* keep text */ } }
    }
    // A 409 can mean "mid-ingest, try again in about a second" (DESIGN §15): retry409 asks once more.
    if (res.status === 409 && opts.retry409 && !opts._again) { await sleep(1000); return api(path, Object.assign({}, opts, { _again: true })); }
    if (!res.ok) {
      let msg = data && typeof data === 'object' && data.error ? String(data.error) : '';
      if (!msg && typeof data === 'string' && data.trim()) msg = data.trim().slice(0, 300);
      if (!msg) msg = res.status + ' ' + (res.statusText || 'error');
      const err = new ApiError(res.status, msg, data);
      const wait = parseFloat(res.headers.get('Retry-After') || '');
      if (wait > 0) err.retryAfter = wait;   // seconds, from a 429's Retry-After
      throw err;
    }
    if (method !== 'GET' && SETTINGS_PATH_RE.test(path)) doctorStale();
    return data;
  }
  /** A saved setting (the config, a preset, a repository, a key) changes what Doctor finds; nothing on the event
   *  stream says so. Without a fresh run the badge kept a fresh install's to-do list after setup and, the install no
   *  longer fresh, showed it as that many red issues. Every save, from any section, runs Doctor again once the saves
   *  settle (the setup wizard's Test adds and removes a temporary entry back to back). */
  const SETTINGS_PATH_RE = /^\/api\/(config|config\/preset|repos|secrets)$/;
  let doctorTimer = 0;
  function doctorStale() {
    clearTimeout(doctorTimer);
    doctorTimer = setTimeout(() => loadDoctor(false), 800);
  }
  const jobPath = (repo, id) => '/api/jobs/' + enc(repo) + '/' + enc(id);

  // ================================================================ toasts + clipboard
  // At most three toasts show; older ones fold into "+N more". Info and ok toasts leave after 5 s (the clock stops
  // while the pointer or focus is on one); errors stay until dismissed. The stack rises above a chat composer, the
  // settings save bar and the wizard's Back/Next, so it never covers them (dialogs sit in the top layer, above it).
  const TOASTS = { list: [], expanded: false };
  const TOAST_MAX = 3;
  const TOAST_AVOID = ['.composer-wrap', '.save-bar', '.wz-foot'];
  function placeToasts() {
    const box = document.getElementById('toasts');
    if (!box) return;
    if (!TOASTS.list.length) { box.style.bottom = ''; return; }
    let lift = 0;
    for (const sel of TOAST_AVOID) {
      for (const el of document.querySelectorAll(sel)) {
        if (el.hidden || !el.getClientRects().length) continue;
        const r = el.getBoundingClientRect();
        if (r.height === 0 || r.top >= window.innerHeight || r.bottom <= 0) continue;
        lift = Math.max(lift, window.innerHeight - r.top + 10);
      }
    }
    box.style.bottom = lift ? lift + 'px' : '';
  }
  function layoutToasts() {
    const box = document.getElementById('toasts');
    if (!box) return;
    TOASTS.list = TOASTS.list.filter((t) => t.el.isConnected);
    const live = TOASTS.list;
    if (!live.length) TOASTS.expanded = false;
    const folded = TOASTS.expanded ? 0 : Math.max(0, live.length - TOAST_MAX);
    live.forEach((t, i) => { t.el.hidden = i < folded; });
    let more = box.querySelector('.toast-more');
    if (folded) {
      if (!more) {
        more = h('button', { type: 'button', class: 'toast-more' });
        more.addEventListener('click', () => { TOASTS.expanded = true; layoutToasts(); });
        box.insertBefore(more, box.firstChild);
      }
      more.textContent = '+' + folded + ' more';
      more.setAttribute('aria-label', 'Show ' + plural(folded, 'older notification'));
    } else if (more) more.remove();
    placeToasts();
  }
  function toast(message, opts) {
    opts = opts || {};
    const kind = opts.kind || 'info';
    const sticky = kind === 'error' || !!opts.sticky;
    const box = document.getElementById('toasts');
    const el = h('div', { class: 'toast toast-' + kind, role: kind === 'error' ? 'alert' : 'status' });
    el.appendChild(icon(kind === 'error' ? 'alert' : kind === 'ok' ? 'check-circle' : 'info'));
    el.appendChild(h('div', { class: 'toast-msg' }, message));
    const t = { el, timer: 0, left: opts.timeout || 5000, started: 0 };
    const dismiss = () => { clearTimeout(t.timer); el.remove(); layoutToasts(); };
    const arm = () => { if (sticky || !el.isConnected) return; clearTimeout(t.timer); t.started = Date.now(); t.timer = setTimeout(dismiss, t.left); };
    const hold = () => { if (sticky || !t.timer) return; clearTimeout(t.timer); t.timer = 0; t.left = Math.max(1500, t.left - (Date.now() - t.started)); };
    if (opts.action) {
      const a = opts.action.href
        ? h('a', { class: 'toast-action', href: opts.action.href }, opts.action.label)
        : h('button', { type: 'button', class: 'btn btn-ghost btn-xs toast-action' }, opts.action.label);
      a.addEventListener('click', () => { if (opts.action.onClick) opts.action.onClick(); dismiss(); });
      el.appendChild(a);
    }
    const x = h('button', { type: 'button', class: 'btn btn-ghost btn-icon btn-xs toast-x', 'aria-label': 'Dismiss notification' }, icon('x', 'icon-xs'));
    x.addEventListener('click', dismiss);
    el.appendChild(x);
    el.addEventListener('mouseenter', hold);
    el.addEventListener('mouseleave', () => { if (!el.contains(document.activeElement)) arm(); });
    el.addEventListener('focusin', hold);
    el.addEventListener('focusout', (e) => { if (!el.contains(e.relatedTarget) && !el.matches(':hover')) arm(); });
    if (sticky) el.dataset.sticky = '1';
    box.appendChild(el);
    TOASTS.list.push(t);
    arm();
    layoutToasts();
    return el;
  }
  function toastError(prefix, e) { toast((prefix ? prefix + ': ' : '') + (e && e.message ? e.message : String(e)), { kind: 'error' }); }
  async function copyText(text, msg) {
    try {
      await navigator.clipboard.writeText(text);
      toast(msg || 'Copied to clipboard', { kind: 'ok', timeout: 2000 });
    } catch (e) {
      const ta = h('textarea', { class: 'sr-only', readonly: true });
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      let ok = false;
      try { ok = document.execCommand('copy'); } catch (e2) { ok = false; }
      ta.remove();
      toast(ok ? (msg || 'Copied to clipboard') : 'Copy failed: select the text and copy it manually.', { kind: ok ? 'ok' : 'error', timeout: 2500 });
    }
  }

  // ================================================================ modals (<dialog>)
  let modalSeq = 0;
  function openModal(opts) {
    return new Promise((resolve) => {
      const id = 'modal-' + (++modalSeq);
      const opener = document.activeElement;
      const openerAction = opener && opener.dataset ? opener.dataset.action : null;
      const dlg = h('dialog', { class: 'modal' + (opts.wide ? ' modal-wide' : ''), 'aria-labelledby': id + '-t' });
      const errorBox = h('div', { class: 'form-error', role: 'alert', hidden: true });
      const form = h('form', { class: 'modal-form', method: 'dialog', novalidate: true });
      const closeBtn = h('button', { type: 'button', class: 'btn btn-ghost btn-icon btn-sm', 'aria-label': 'Close', title: 'Close (Esc)' }, icon('x'));
      form.appendChild(h('div', { class: 'modal-head' }, h('h2', { class: 'modal-title', id: id + '-t' }, opts.title), closeBtn));
      if (opts.description) form.appendChild(h('p', { class: 'modal-desc' }, opts.description));
      form.appendChild(h('div', { class: 'modal-body' }, opts.body || null, errorBox));
      const foot = h('div', { class: 'modal-foot' }, opts.hint ? h('span', { class: 'foot-hint' }, opts.hint) : null);
      const buttons = [];
      const actions = opts.actions || [{ label: 'Cancel', value: 'cancel' }, { label: 'OK', value: 'ok', primary: true }];
      let done = false;
      const finish = (value) => {
        if (done) return;
        done = true;
        try { dlg.close(); } catch (e) { /* already closed */ }
        dlg.remove();
        restoreFocus(opener, openerAction);
        resolve(value);
      };
      for (const a of actions) {
        const b = h('button', { type: a.value === 'cancel' ? 'button' : 'submit', value: a.value, class: 'btn ' + (a.cls || (a.primary ? 'btn-primary' : '')), dataset: { action: a.value } },
          a.icon ? icon(a.icon, 'icon-sm') : null, a.label);
        if (a.value === 'cancel') b.addEventListener('click', () => finish(null));
        buttons.push(b);
        foot.appendChild(b);
      }
      form.appendChild(foot);
      dlg.appendChild(form);
      const ctl = {
        dlg, form,
        showError(msg) { clear(errorBox); errorBox.appendChild(icon('alert', 'icon-sm')); errorBox.appendChild(h('span', null, msg)); errorBox.hidden = !msg; },
        clearError() { errorBox.hidden = true; },
        submit(value) {
          const b = buttons.find((x) => x.value === value && x.type === 'submit') || buttons.find((x) => x.type === 'submit');
          if (b && !b.disabled) form.requestSubmit(b);
        },
        close: finish,
      };
      closeBtn.addEventListener('click', () => finish(null));
      form.addEventListener('submit', async (e) => {
        e.preventDefault();
        const submitter = e.submitter || buttons.find((x) => x.type === 'submit');
        const value = (submitter && submitter.value) || 'ok';
        if (value === 'cancel') { finish(null); return; }
        if (!opts.onSubmit) { finish(value); return; }
        ctl.clearError();
        // Buttons the dialog disabled itself (e.g. Add to backlog under a custom routing) stay disabled afterwards.
        const wasDisabled = buttons.map((b) => b.disabled);
        buttons.forEach((b) => { b.disabled = true; });
        setBusy(submitter, true);
        try {
          const r = await opts.onSubmit(value, ctl);
          if (r === false) return;
          finish(r === undefined ? value : r);
        } catch (err) {
          ctl.showError(err && err.message ? err.message : String(err));
        } finally {
          if (!done) {
            buttons.forEach((b, i) => { b.disabled = wasDisabled[i]; }); setBusy(submitter, false);
            // The busy button lost focus when it was disabled: give it back unless onSubmit focused a field.
            const at = document.activeElement;
            if ((!at || at === document.body || !dlg.contains(at)) && submitter && !submitter.disabled) submitter.focus();
          }
        }
      });
      // Esc: a dialog that holds typed text (non-dismissable ones) asks for a second Esc before discarding it.
      let escArmed = false;
      const textFields = () => Array.from(form.querySelectorAll('.modal-body textarea, .modal-body input[type=text]'));
      let initial = null;
      const typed = () => opts.dismissable === false && !!initial && textFields().some((el) => el.value.trim() && el.value !== initial.get(el));
      dlg.addEventListener('cancel', (e) => {
        e.preventDefault();
        if (!escArmed && typed()) {
          escArmed = true;
          ctl.showError('Press Esc again to discard what you typed.');
          setTimeout(() => { escArmed = false; }, 4000);
          return;
        }
        finish(null);
      });
      dlg.addEventListener('close', () => finish(null));
      if (opts.dismissable !== false) {
        let downOnBackdrop = false;
        dlg.addEventListener('mousedown', (e) => { downOnBackdrop = e.target === dlg; });
        dlg.addEventListener('click', (e) => { if (e.target === dlg && downOnBackdrop) finish(null); downOnBackdrop = false; });
      }
      document.body.appendChild(dlg);
      dlg.showModal();
      initial = new Map(textFields().map((el) => [el, el.value]));
      const focusEl = opts.initialFocus || form.querySelector('.modal-body textarea, .modal-body input:not([type=hidden]):not([disabled]):not([type=radio]), .modal-body select') || buttons.find((b) => b.type === 'submit');
      if (focusEl) focusEl.focus();
      if (opts.onOpen) opts.onOpen(ctl);
    });
  }
  /** Return focus after a dialog: the opener, or its re-rendered twin (drawer actions are rebuilt on job.updated). */
  function restoreFocus(opener, action) {
    if (opener && opener.isConnected && typeof opener.focus === 'function' && !opener.closest('[inert]')) { opener.focus({ preventScroll: true }); return; }
    const drawer = document.getElementById('drawer');
    const scope = drawer && !drawer.hidden ? drawer : document.getElementById('main');
    const twin = action ? scope.querySelector('[data-action="' + CSS.escape(action) + '"]') : null;
    const target = twin || (drawer && !drawer.hidden ? document.getElementById('drawer-title') : null) || scope;
    if (target && typeof target.focus === 'function') target.focus({ preventScroll: true });
  }
  function confirmModal(o) {
    return openModal({
      title: o.title, description: o.description,
      body: o.body || null,
      actions: [{ label: 'Cancel', value: 'cancel' }, { label: o.confirm || 'Confirm', value: 'ok', cls: o.cls || 'btn-primary', icon: o.icon }],
      onSubmit: o.onSubmit,
      dismissable: true,
    });
  }

  // ================================================================ state
  const ACTIVE = new Set(['queued', 'spec', 'build', 'review', 'approve', 'merging']);
  const STAGE_LABEL = { queued: 'Queued', spec: 'Spec', build: 'Build', review: 'Review', approve: 'Approve', merging: 'Merging', merged: 'Merged', delivered: 'Delivered', 'needs-human': 'Needs human', rejected: 'Rejected', failed: 'Failed', cancelled: 'Cancelled' };
  const STAGE_TONE = { queued: 'neutral', spec: 'accent', build: 'accent', review: 'accent', approve: 'accent', merging: 'accent', merged: 'ok', delivered: 'ok', 'needs-human': 'human', failed: 'err', rejected: 'muted', cancelled: 'muted' };
  /** Delivery (§25): what APPROVE did or does with a job, and where a delivered folder job's files are. */
  const DELIVERY_UI = { merge: { label: 'merge', icon: 'merge', text: 'merged into the base branch' }, branch: { label: 'branch', icon: 'branch', text: 'kept on its branch' }, folder: { label: 'folder', icon: 'folder', text: 'exported to the output folder' } };
  function jobDelivery(job) {
    const d = job && isObj(job.delivered) && job.delivered.mode ? job.delivered.mode : job && job.delivery;
    return DELIVERY_UI[d] ? d : null;
  }
  function jobOutputPath(job) {
    if (!job) return null;
    const p = job.output_dir || job.output_path || job.delivery_path || (isObj(job.delivered) ? job.delivered.path || job.delivered.output_dir : null);
    return p ? String(p) : null;
  }
  const COLORS = new Set(['red', 'orange', 'yellow', 'green', 'cyan', 'blue', 'purple', 'pink', 'gray']);
  // Plans (DESIGN §24).
  const PLAN_STAGE_LABEL = { planning: 'Planning', ready: 'Ready', running: 'Running', blocked: 'Blocked', 'final-review': 'Final review', done: 'Done', 'needs-human': 'Needs human', failed: 'Failed', cancelled: 'Cancelled' };
  const PLAN_STAGE_TONE = { planning: 'accent', ready: 'accent', running: 'accent', blocked: 'human', 'final-review': 'accent', done: 'ok', 'needs-human': 'human', failed: 'err', cancelled: 'muted' };
  const PLAN_LIVE = new Set(['planning', 'running', 'final-review']);
  const PLAN_EDITABLE = new Set(['ready', 'needs-human', 'blocked']);

  const S = {
    loaded: false, stateError: null, version: '', repos: [], jobs: new Map(), pipeline: {}, models: {}, drivers: [], agents: [],
    chats: new Map(), plans: new Map(), lastSeq: 0, conn: 'connecting', retryAt: 0, connectedOnce: false,
    doctor: null, doctorError: null, doctorLoading: false,
    filterRepo: lsGet('sf.repo') || '', search: '',
    runs: new Map(), runsByOwner: new Map(), jobLogs: new Map(),
    // Runs queued on a busy provider slot (agent.waiting) until their next event: run key → {owner, provider, agent, repo}.
    waiting: new Map(),
    // Removals by event seq: a list response requested before the removal must not bring the item back.
    removedJobs: new Map(), removedChats: new Map(),
    route: { view: '' },
  };
  const jobKey = (j) => j.repo + '/' + j.id;
  const stageLabel = (s) => STAGE_LABEL[s] || s || 'unknown';
  function stagePill(stage, extraCls) {
    const tone = STAGE_TONE[stage] || 'neutral';
    return h('span', { class: 'pill tone-' + tone + (ACTIVE.has(stage) && stage !== 'queued' ? ' live' : '') + (extraCls ? ' ' + extraCls : '') }, stageLabel(stage));
  }
  function planStagePill(stage) {
    const tone = PLAN_STAGE_TONE[stage] || 'neutral';
    return h('span', { class: 'pill tone-' + tone + (PLAN_LIVE.has(stage) ? ' live' : '') }, PLAN_STAGE_LABEL[stage] || stage || 'unknown');
  }
  /** The reviewers of the line: pipeline.reviewers from /api/state, any count and names. */
  function pipelineReviewers() {
    const r = S.pipeline && Array.isArray(S.pipeline.reviewers) ? S.pipeline.reviewers.filter((x) => typeof x === 'string' && x) : [];
    if (r.length || !S.loaded) return r;
    return S.agents.filter((a) => /^reviewer-/.test(a.name)).map((a) => a.name.slice(9));
  }
  function agentDef(name) { return S.agents.find((a) => a && a.name === name) || null; }
  function agentColorCls(name) {
    const a = agentDef(name);
    const c = a && a.color ? String(a.color).toLowerCase() : 'gray';
    return 'c-' + (COLORS.has(c) ? c : c === 'grey' ? 'gray' : 'gray');
  }
  function modelOf(agentName) {
    const a = agentDef(agentName);
    if (!a) return { key: '', model: '—', provider: '', driver: '' };
    const key = a.model ? String(a.model) : '';
    const m = S.models && key ? S.models[key] : null;
    return { key, model: m && m.model ? String(m.model) : (key || '—'), provider: m && m.provider ? String(m.provider) : '', driver: a.driver ? String(a.driver) : '' };
  }
  function visibleJobs() {
    const out = [];
    for (const j of S.jobs.values()) if (!S.filterRepo || j.repo === S.filterRepo) out.push(j);
    return out;
  }

  // ---------------------------------------------------------------- render scheduling
  const RENDER = {};
  const dirtyParts = new Set();
  let rafPending = false;
  function invalidate() {
    for (let i = 0; i < arguments.length; i++) dirtyParts.add(arguments[i]);
    if (!rafPending) { rafPending = true; requestAnimationFrame(flushRender); }
  }
  function flushRender() {
    rafPending = false;
    const parts = Array.from(dirtyParts);
    dirtyParts.clear();
    for (const p of parts) {
      if (!RENDER[p]) continue;
      try { RENDER[p](); } catch (e) { console.error('render ' + p + ' failed', e); }
    }
    // A view may have just shown (or hidden) a composer, a save bar or Back/Next: the toasts move clear of it.
    if (TOASTS.list.length) placeToasts();
  }

  // ---------------------------------------------------------------- state application
  function applyState(st, startSeq) {
    if (!st || typeof st !== 'object') return;
    S.version = st.version ? String(st.version) : '';
    S.repos = Array.isArray(st.repos) ? st.repos.filter((r) => r && r.name) : [];
    S.pipeline = isObj(st.pipeline) ? st.pipeline : {};
    S.models = isObj(st.models) ? st.models : {};
    S.drivers = Array.isArray(st.drivers) ? st.drivers : [];
    S.agents = Array.isArray(st.agents) ? st.agents.filter((a) => a && a.name) : [];
    S.chatAgents = Array.isArray(st.chat_agents) ? st.chat_agents.filter((a) => typeof a === 'string' && a) : null;
    const next = new Map();
    for (const j of Array.isArray(st.jobs) ? st.jobs : []) {
      if (!j || !j.id || !j.repo) continue;
      const k = jobKey(j);
      if ((S.removedJobs.get(k) || 0) > startSeq) continue;
      const cur = S.jobs.get(k);
      next.set(k, cur && ((cur.updated_at || '') > (j.updated_at || '') || (cur._seq || 0) > startSeq) ? cur : j);
    }
    // Keep jobs that events created while this state request was in flight.
    for (const [k, j] of S.jobs) if (!next.has(k) && (j._seq || 0) > startSeq) next.set(k, j);
    S.jobs = next;
    S.loaded = true;
    S.stateError = null;
    if (S.filterRepo && !S.repos.some((r) => r.name === S.filterRepo)) { S.filterRepo = ''; lsSet('sf.repo', null); }
    invalidate('topbar', 'strip', 'board', 'drawerHead', 'agents', 'chatList', 'chatConv', 'backlog', 'grid');
    if (S.route.view === 'flow' && S.route.flow !== 'welcome') invalidate('flow');
  }
  let stateInflight = null;
  let stateAgain = false;
  function refreshState() {
    if (stateInflight) { stateAgain = true; return stateInflight; }
    const startSeq = S.lastSeq;
    stateInflight = api('/api/state')
      .then((st) => applyState(st, startSeq))
      .catch((e) => { if (e.status !== 401) { S.stateError = e.message; invalidate('board', 'strip'); } })
      .finally(() => {
        stateInflight = null;
        if (stateAgain) { stateAgain = false; refreshState(); }
      });
    return stateInflight;
  }
  /** seq: the event's seq (events), or 0 for API responses; since: S.lastSeq when that request started. */
  function upsertJob(job, seq, since) {
    if (!job || !job.id || !job.repo) return;
    const k = jobKey(job);
    const cur = S.jobs.get(k);
    if (cur && (cur.updated_at || '') > (job.updated_at || '')) return;
    if (!seq && since !== undefined && cur && (cur._seq || 0) > since) return;  // an event already superseded this response
    job._seq = seq || (cur && cur._seq) || 0;
    S.jobs.set(k, job);
    if (!ACTIVE.has(job.stage)) clearWaiting(k);
    invalidate('strip', 'board', 'topbar');
    if (S.route.view === 'grid') invalidate('grid');
    // Only a live update re-reads the drawer's files: a response of that re-read must not schedule another.
    if (D.key === k) { invalidate('drawerHead', 'raw'); if (seq) onDrawerJobUpdate(cur, job); }
    if (S.route.view === 'plans' && planOfJob(job)) invalidate('plans');
  }

  // ---------------------------------------------------------------- plans
  const planKey = (p) => p.repo + '/' + p.id;
  function upsertPlan(plan, seq, since) {
    if (!plan || !plan.id || !plan.repo) return;
    const k = planKey(plan);
    const cur = S.plans.get(k);
    if (cur && (cur.updated_at || '') > (plan.updated_at || '')) return;
    if (!seq && since !== undefined && cur && (cur._seq || 0) > since) return;
    plan._seq = seq || (cur && cur._seq) || 0;
    S.plans.set(k, plan);
    if (!PLAN_LIVE.has(plan.stage)) clearWaiting('plan:' + k);
    invalidate('plans', 'topbar');
    if (S.route.view === 'grid') invalidate('grid');
    if (PL.sel === k && seq) onSelectedPlanUpdate(cur, plan);  // live updates only (see upsertJob)
  }

  // ---------------------------------------------------------------- provider waits (agent.waiting)
  function waitOwner(ev) {
    const d = ev.data || {};
    if (d.chat) return 'chat:' + d.chat;
    if (d.plan && ev.repo) return 'plan:' + ev.repo + '/' + d.plan;
    if (ev.repo && ev.job) return ev.repo + '/' + ev.job;
    return null;
  }
  /** agent.waiting marks its run as queued on a provider; the run's next event (any type) ends the wait. */
  function trackWaiting(ev) {
    const owner = waitOwner(ev);
    if (!owner) return;
    const key = owner + '#' + runName(ev);
    if (ev.type === 'agent.waiting') {
      const d = ev.data || {};
      S.waiting.set(key, { owner, provider: String(d.provider || 'the provider'), custody: d.custody ? String(d.custody) : null, agent: String(ev.agent || ''), repo: ev.repo || '', since: ev.ts || null });
      onWaitingChanged(owner);
    } else if (S.waiting.delete(key)) {
      onWaitingChanged(owner);
    }
  }
  function clearWaiting(owner) {
    let hit = false;
    for (const [k, w] of S.waiting) if (w.owner === owner) { S.waiting.delete(k); hit = true; }
    if (hit) onWaitingChanged(owner);
  }
  function waitingOf(owner) {
    for (const w of S.waiting.values()) if (w.owner === owner) return w;
    return null;
  }
  /** What a run waits for, in words (agent.waiting): a free slot of its provider, or, when the GPU custody register
   *  answered HELD (custody: "HELD", DESIGN §37), the register: someone else holds the card. Never "a slot" then. */
  const HELD_WAIT = 'waiting for the GPU custody register: the card is held by someone else';
  function waitWords(w) {
    if (w && w.custody === 'HELD') return { short: HELD_WAIT, long: 'Waiting for the GPU custody register: the card is held by someone else. The run waits once, up to the provider’s overflow_after_seconds, then goes to the next build or its overflow.' };
    const p = (w && w.provider) || 'the provider';
    return { short: 'waiting for a free ' + p + ' slot', long: 'Queued: the ' + p + ' provider has no free slot (max_concurrency); the run starts when one frees up' };
  }
  function onWaitingChanged(owner) {
    invalidate('strip', 'board');
    if (D.key === owner) invalidate('live', 'drawerHead');
    if (CH.id && owner === 'chat:' + CH.id) invalidate('chatConv');
    if (owner.indexOf('plan:') === 0) invalidate('plans');
  }
  function removeJob(repo, id, seq) {
    if (!repo || !id) return;
    const k = repo + '/' + id;
    if (seq) S.removedJobs.set(k, seq);
    if (!S.jobs.delete(k)) return;
    invalidate('strip', 'board', 'topbar');
    if (D.key === k) invalidate('drawerHead');
  }

  // ---------------------------------------------------------------- agent runs (transcripts)
  function runOwner(ev) {
    const d = ev.data || {};
    if (d.chat) return 'chat:' + d.chat;
    if (ev.repo && ev.job) return ev.repo + '/' + ev.job;
    return null;
  }
  function runName(ev) {
    const d = ev.data || {};
    return String(d.run || ('run-' + (ev.agent || 'agent')));
  }
  /** Transcripts are kept only for what is on screen: the open job drawer and chats (small, user-driven).
   *  Other jobs' agent events are dropped; opening a job loads its runs from the log files. */
  function keepOwner(owner) { return !!owner && (owner.indexOf('chat:') === 0 || owner === D.key); }
  function dropOwner(owner) {
    const set = S.runsByOwner.get(owner);
    if (set) for (const k of set.keys()) S.runs.delete(k);
    S.runsByOwner.delete(owner);
    S.jobLogs.delete(owner);
  }
  function getRun(owner, name, ev) {
    const key = owner + '#' + name;
    let run = S.runs.get(key);
    if (!run) {
      const no = /^(\d+)-/.exec(name);
      run = {
        key, owner, name, no: no ? parseInt(no[1], 10) : null,
        agent: (ev && ev.agent) || name.replace(/^\d+-/, ''), model: '', modelId: '', provider: '', driver: '', round: null, waiting: null,
        startTs: null, endTs: null, status: 'pending', ok: null, error: null, usage: null, usageLive: null, turns: null, began: false,
        startData: null, endData: null, placeV: 0, events: [], seqs: new Set(), lastSeq: -Infinity, firstSeq: Infinity, items: [], tools: new Map(), gen: 0, v: 0,
      };
      S.runs.set(key, run);
      let set = S.runsByOwner.get(owner);
      if (!set) { set = new Map(); S.runsByOwner.set(owner, set); }
      set.set(key, run);
      if (S.runs.size > 400) pruneRuns();
    }
    return run;
  }
  function pruneRuns() {
    const keep = new Set();
    if (D.key) keep.add(D.key);
    if (CH.id) keep.add('chat:' + CH.id);
    const runs = Array.from(S.runs.values()).filter((r) => !keep.has(r.owner)).sort((a, b) => a.lastSeq - b.lastSeq);
    for (const r of runs.slice(0, Math.max(0, S.runs.size - 300))) {
      S.runs.delete(r.key);
      const set = S.runsByOwner.get(r.owner);
      if (set) { set.delete(r.key); if (!set.size) S.runsByOwner.delete(r.owner); }
    }
  }
  function resetRun(run) {
    run.items = []; run.tools = new Map(); run.status = 'pending'; run.ok = null; run.error = null;
    run.usage = null; run.usageLive = null; run.turns = null; run.startTs = null; run.endTs = null; run.waiting = null; run.waitCustody = null; run.began = false;
    run.startData = null; run.endData = null; run.placeV++;
  }
  function applyRunEvent(run, ev) {
    const d = ev.data || {};
    const items = run.items;
    const last = items[items.length - 1];
    if (!run.startTs && ev.ts) run.startTs = ev.ts;
    if (ev.type !== 'agent.end' && run.status === 'pending') run.status = 'running';
    // Queued on a busy provider slot (or held by the GPU custody register) until the run's next event.
    run.waiting = ev.type === 'agent.waiting' ? String(d.provider || 'the provider') : null;
    run.waitCustody = ev.type === 'agent.waiting' && d.custody ? String(d.custody) : null;
    switch (ev.type) {
      case 'agent.start':
        run.agent = d.agent || ev.agent || run.agent;
        run.model = d.model ? String(d.model) : run.model;
        run.modelId = d.model_id ? String(d.model_id) : run.modelId;
        run.provider = d.provider ? String(d.provider) : run.provider;
        run.driver = d.driver ? String(d.driver) : run.driver;
        run.round = d.round !== undefined ? d.round : run.round;
        run.startTs = ev.ts || run.startTs;
        run.status = 'running';
        run.startData = d;
        run.placeV++;
        run.began = true;   // the agent started: a run without agent.start was refused before any model call
        break;
      case 'agent.end':
        run.endTs = ev.ts || null;
        run.ok = d.ok !== false;
        run.error = d.error ? String(d.error) : null;
        run.usage = isObj(d.usage) ? d.usage : run.usage;
        run.turns = d.turns !== undefined ? d.turns : run.turns;
        run.status = run.ok ? 'ok' : 'error';
        run.endData = d;
        run.placeV++;
        if (run.error) items.push({ t: 'error', text: run.error, v: 1 });
        break;
      case 'agent.text':
        if (last && last.t === 'text') { last.text += String(d.text || ''); last.v++; }
        else items.push({ t: 'text', text: String(d.text || ''), v: 1 });
        break;
      case 'agent.reasoning':
        if (last && last.t === 'reasoning') { last.text += String(d.text || ''); last.v++; }
        else items.push({ t: 'reasoning', text: String(d.text || ''), v: 1 });
        break;
      case 'agent.tool_call': {
        const it = { t: 'tool', id: d.id, name: String(d.name || 'tool'), input: d.input, result: null, v: 1 };
        items.push(it);
        if (d.id !== undefined) run.tools.set(String(d.id), it);
        break;
      }
      case 'agent.tool_result': {
        const it = d.id !== undefined ? run.tools.get(String(d.id)) : null;
        const result = { ok: d.ok !== false, output: d.output === undefined || d.output === null ? '' : (typeof d.output === 'string' ? d.output : JSON.stringify(d.output, null, 2)) };
        if (it && !it.result) { it.result = result; it.v++; }
        else items.push({ t: 'tool', id: d.id, name: String(d.name || 'tool'), input: null, result, v: 1 });
        break;
      }
      case 'agent.output':
        if (last && last.t === 'output') { last.lines.push(String(d.line === undefined ? '' : d.line)); last.v++; }
        else items.push({ t: 'output', lines: [String(d.line === undefined ? '' : d.line)], v: 1 });
        break;
      case 'agent.usage': {
        const u = run.usageLive || { input_tokens: 0, output_tokens: 0, cached_tokens: 0 };
        u.input_tokens += Number(d.input_tokens) || 0;
        u.output_tokens += Number(d.output_tokens) || 0;
        u.cached_tokens += Number(d.cached_tokens) || 0;
        run.usageLive = u;
        break;
      }
      case 'log':
        items.push({ t: 'log', level: pick(String(d.level || 'info'), LOG_LEVELS, 'info'), message: String(d.message || ''), ts: ev.ts, v: 1 });
        break;
      default:
        break;
    }
  }
  function rebuildRun(run) {
    resetRun(run);
    for (const ev of run.events) applyRunEvent(run, ev);
    run.gen++;
  }
  function ingestAgentEvent(ev) {
    const owner = runOwner(ev);
    if (!keepOwner(owner)) return false;
    const run = getRun(owner, runName(ev), ev);
    const seq = Number(ev.seq) || 0;
    if (seq) {
      if (run.seqs.has(seq)) return false;
      run.seqs.add(seq);
    }
    if (seq && seq < run.lastSeq) {
      let k = run.events.length;
      while (k > 0 && (Number(run.events[k - 1].seq) || 0) > seq) k--;
      run.events.splice(k, 0, ev);
      rebuildRun(run);
    } else {
      run.events.push(ev);
      if (seq) run.lastSeq = seq;
      applyRunEvent(run, ev);
    }
    if (seq && seq < run.firstSeq) run.firstSeq = seq;
    run.v++;
    return true;
  }
  function ingestBulk(events) {
    const touched = new Set();
    for (const ev of events) {
      if (!ev || typeof ev !== 'object' || typeof ev.type !== 'string') continue;
      if (ev.type === 'log' && !(ev.data && ev.data.run)) { addJobLog(ev); continue; }
      if (ev.type.indexOf('agent.') !== 0) continue;
      const owner = runOwner(ev);
      if (!keepOwner(owner)) continue;
      const run = getRun(owner, runName(ev), ev);
      const seq = Number(ev.seq) || 0;
      if (seq) { if (run.seqs.has(seq)) continue; run.seqs.add(seq); }
      run.events.push(ev);
      touched.add(run);
    }
    for (const run of touched) {
      run.events.sort((a, b) => (Number(a.seq) || 0) - (Number(b.seq) || 0));
      for (const ev of run.events) {
        const s = Number(ev.seq) || 0;
        if (s && s < run.firstSeq) run.firstSeq = s;
        if (s && s > run.lastSeq) run.lastSeq = s;
      }
      rebuildRun(run);
      run.v++;
    }
    return touched.size;
  }
  function addJobLog(ev) {
    if (!ev.repo || !ev.job) return false;
    const owner = ev.repo + '/' + ev.job;
    if (!keepOwner(owner)) return false;
    let m = S.jobLogs.get(owner);
    if (!m) { m = new Map(); S.jobLogs.set(owner, m); }
    // seq restarts at 1 with every server start: key on seq, time and text together.
    const k = (ev.seq || 0) + '|' + (ev.ts || '') + '|' + (ev.data && ev.data.message);
    if (m.has(k)) return false;
    m.set(k, ev);
    return true;
  }
  function onRunChanged(owner) {
    if (D.key && owner === D.key) invalidate('live');
    if (CH.id && owner === 'chat:' + CH.id) invalidate('chatConv');
  }

  // ================================================================ live events (SSE)
  const EVENT_TYPES = ['job.created', 'job.updated', 'job.removed', 'agent.start', 'agent.end', 'agent.text', 'agent.reasoning',
    'agent.tool_call', 'agent.tool_result', 'agent.output', 'agent.usage', 'agent.waiting', 'log', 'backlog.updated', 'repos.updated',
    'chat.updated', 'chat.created', 'chat.removed', 'plan.updated', 'agent.updated', 'setup.updated'];
  let es = null;
  let retryCount = 0;
  let retryTimer = 0;
  let stableTimer = 0;
  let lastReconnectLoad = 0;
  let firstEventOnConn = true;
  let offlineTimer = 0;
  function setConn(state) {
    S.conn = state;
    renderConn();
    // A short blip is normal (server restart, proxy timeout); say so only when it lasts. The timer runs
    // from the first failure: reconnect attempts must not restart it.
    if (state === 'open') { clearTimeout(offlineTimer); offlineTimer = 0; setOffline(false); }
    else if (!offlineTimer) offlineTimer = setTimeout(() => { offlineTimer = 0; setOffline(S.conn !== 'open'); }, S.connectedOnce ? 4000 : 6000);
  }
  function setOffline(off) {
    S.offline = off;
    const app = document.getElementById('app');
    if (app) app.classList.toggle('is-offline', off);
    const banner = document.getElementById('offline-banner');
    if (banner) banner.hidden = !off;
    // The drawer is modal and covers the banner: it carries its own notice.
    const note = document.getElementById('drawer-offline');
    if (note) note.hidden = !off;
  }
  function retryNow() { retryCount = 0; connect(); refreshState(); }
  function connect() {
    clearTimeout(retryTimer);
    if (es) { try { es.close(); } catch (e) { /* ignore */ } es = null; }
    setConn('connecting');
    let source;
    try { source = new EventSource('/api/events?since=' + encodeURIComponent(String(S.lastSeq))); }
    catch (e) { scheduleReconnect(); return; }
    es = source;
    firstEventOnConn = true;
    source.addEventListener('open', () => {
      if (es !== source) return;
      // Reset the backoff only once the stream has stayed up: a proxy that accepts and drops the
      // connection must not make us reconnect (and reload everything) every second.
      clearTimeout(stableTimer);
      stableTimer = setTimeout(() => { if (es === source) retryCount = 0; }, 10000);
      setConn('open');
      refreshState();
      if (S.connectedOnce) onReconnected();
      S.connectedOnce = true;
    });
    source.addEventListener('error', () => {
      if (es !== source) return;
      clearTimeout(stableTimer);
      source.close();
      es = null;
      scheduleReconnect();
    });
    const onMsg = (m) => { if (es === source) onSseMessage(m); };
    // The core's own event types, then every type a view module subscribed to (SF.on): a named SSE event reaches
    // only the listeners registered for its name.
    source._onMsg = onMsg;
    for (const t of eventTypes()) source.addEventListener(t, onMsg);
    source.onmessage = onMsg;
  }
  function scheduleReconnect() {
    const delay = Math.min(30000, 1000 * Math.pow(2, Math.min(retryCount, 5))) * (0.75 + Math.random() * 0.5);
    retryCount++;
    S.retryAt = Date.now() + delay;
    setConn('retrying');
    retryTimer = setTimeout(connect, delay);
    if (retryCount >= 2) refreshState();
  }
  function onReconnected() {
    if (Date.now() - lastReconnectLoad < 15000) return;  // flapping: the state refresh above is enough
    lastReconnectLoad = Date.now();
    loadChats();
    loadPlans();
    if (D.key) loadJobDetail(false);
    if (BL.open) loadBacklog();
    loadDoctor(false);
    notifyReconnect('reconnected');
  }
  function resetLiveData() {
    S.runs.clear();
    S.runsByOwner.clear();
    S.jobLogs.clear();
    S.waiting.clear();
    S.removedJobs.clear();  // seqs from the previous server run mean nothing now
    S.removedChats.clear();
    if (D.transcript) D.transcript.reset();
    if (CH.transcript) CH.transcript.reset();
    for (const st of CH.store.values()) { st.loaded = false; }
    if (D.key) loadJobDetail(true);
    if (CH.id) loadChatDetail(CH.id);
    notifyReconnect('restarted');
  }
  function onSseMessage(m) {
    let ev;
    try { ev = JSON.parse(m.data); } catch (e) { return; }
    if (!ev || typeof ev !== 'object') return;
    if (typeof ev.type !== 'string' || !('data' in ev)) {
      ev = { seq: Number(m.lastEventId) || 0, ts: new Date().toISOString(), type: m.type || 'message', repo: ev.repo || null, job: ev.job || null, agent: ev.agent || null, data: ev };
    }
    const seq = Number(ev.seq !== undefined && ev.seq !== null ? ev.seq : m.lastEventId) || 0;
    ev.seq = seq;
    if (firstEventOnConn) {
      firstEventOnConn = false;
      if (seq && S.lastSeq && seq <= S.lastSeq) { S.lastSeq = 0; resetLiveData(); }
    } else if (seq && seq <= S.lastSeq) {
      return;
    }
    handleEvent(ev);
  }
  function handleEvent(ev) {
    if (ev.seq > S.lastSeq) S.lastSeq = ev.seq;
    try { coreEvent(ev); } catch (e) { console.error('event ' + ev.type + ' failed', e); }
    dispatchToViews(ev);
  }
  function coreEvent(ev) {
    const d = ev.data || {};
    const t = ev.type;
    if (t === 'job.created' || t === 'job.updated') upsertJob(d.job, ev.seq);
    else if (t === 'job.removed') removeJob(ev.repo || (d.job && d.job.repo), ev.job || (d.job && d.job.id), ev.seq);
    else if (t === 'chat.updated' || t === 'chat.created') upsertChat(d.chat, ev.seq);
    else if (t === 'chat.removed') removeChat(typeof d.chat === 'string' ? d.chat : (d.chat && d.chat.id) || d.id, ev.seq);
    else if (t === 'plan.updated') upsertPlan(d.plan, ev.seq);
    else if (t === 'agent.updated') onAgentUpdated(d);
    else if (t === 'setup.updated') loadSetup();
    else if (t === 'backlog.updated') onBacklogUpdated(d.repo || ev.repo);
    else if (t === 'repos.updated') { refreshState(); if (CFG.orig && !configDirty()) loadConfig(); loadSetup(); }
    else if (t === 'log') {
      if (d.run) { trackWaiting(ev); if (ingestAgentEvent(ev)) onRunChanged(runOwner(ev)); }
      else if (ev.repo && ev.job) { if (addJobLog(ev) && D.key === ev.repo + '/' + ev.job) invalidate('live'); }
      else if (!d.chat && (d.level === 'error' || d.level === 'warn') && d.message) toast(String(d.message), { kind: d.level === 'error' ? 'error' : 'info' });
    } else if (t.indexOf('agent.') === 0) {
      if (ev.type === 'agent.updated') return;
      trackWaiting(ev);
      if (ingestAgentEvent(ev)) onRunChanged(runOwner(ev));
    }
  }

  // ================================================================ transcript view (job Live + chat)
  function toolArg(input) {
    if (!input || typeof input !== 'object') return typeof input === 'string' ? input : '';
    const keys = ['file_path', 'path', 'command', 'pattern', 'url', 'query', 'name', 'skill', 'description'];
    for (const k of keys) {
      const v = input[k];
      if (typeof v === 'string' && v) {
        let s = k === 'command' ? v.split('\n')[0] : v;
        if (k === 'pattern' && typeof input.path === 'string') s += '  in ' + input.path;
        return s;
      }
    }
    for (const k of Object.keys(input)) if (typeof input[k] === 'string' && input[k]) return input[k];
    return '';
  }
  function prettyJson(v) {
    if (v === undefined || v === null) return '';
    if (typeof v === 'string') { try { return JSON.stringify(JSON.parse(v), null, 2); } catch (e) { return v; } }
    try { return JSON.stringify(v, null, 2); } catch (e) { return String(v); }
  }
  function runIsLive(run) {
    if (run.status !== 'running') return false;
    if (run.owner.indexOf('chat:') === 0) {
      const c = S.chats.get(run.owner.slice(5));
      return !c || c.status === 'running';
    }
    const j = S.jobs.get(run.owner);
    return !j || ACTIVE.has(j.stage);
  }
  function runUsage(run) { return run.usage || run.usageLive; }
  /** A run as SF.extend('runHead', fn) sees it (read-only): its agent.start and agent.end data, or null. */
  function runView(run, mode) {
    return Object.freeze({ mode, owner: run.owner, name: run.name, agent: run.agent, round: run.round, status: run.status, start: run.startData, end: run.endData });
  }
  /** The model a run used, as "key → model id" when the backend reports both. */
  function runModel(run) {
    if (run.modelId && run.model && run.modelId !== run.model) return run.model + ' → ' + run.modelId;
    return run.modelId || run.model || '';
  }
  const STEP_AGENT = { spec: (a) => a === 'spec-writer', build: (a) => a === 'builder', review: (a) => /^reviewer-/.test(a), approve: (a) => a === 'approver' };
  /** A run that never ended, judged by its job: the step that failed, a cancel, or an interruption. */
  function unfinishedRunState(run) {
    if (run.owner.indexOf('chat:') === 0) return 'stopped';
    const job = S.jobs.get(run.owner);
    if (!job) return 'stopped';
    if (job.stage === 'cancelled') return 'cancelled';
    if (job.stage === 'failed') {
      const match = STEP_AGENT[job.failed_step];
      if (!match || match(run.agent)) return 'failed';
    }
    return 'stopped';
  }

  class Transcript {
    constructor(root, scroller, pill, mode) {
      this.root = root; this.scroller = scroller; this.pill = pill; this.mode = mode;
      this.recs = new Map(); this.stick = true; this.total = -1;
      this.onScroll = () => {
        this.stick = this.atBottom();
        if (this.stick && this.pill) this.pill.hidden = true;
      };
      scroller.addEventListener('scroll', this.onScroll, { passive: true });
      if (pill) pill.addEventListener('click', () => this.toBottom());
    }
    atBottom() { const s = this.scroller; return s.scrollHeight - s.scrollTop - s.clientHeight < 48; }
    toBottom() {
      this.scroller.scrollTop = this.scroller.scrollHeight;
      this.stick = true;
      if (this.pill) this.pill.hidden = true;
    }
    reset() { clear(this.root); this.recs.clear(); this.total = -1; }
    destroy() { this.scroller.removeEventListener('scroll', this.onScroll); }
    sync(blocks) {
      const stick = this.stick;
      let total = 0;
      const seen = new Set();
      blocks.forEach((b, i) => {
        let rec = this.recs.get(b.key);
        if (!rec) { rec = this.create(b); this.recs.set(b.key, rec); }
        seen.add(b.key);
        total += this.update(rec, b, i === blocks.length - 1) + 1;
        if (this.root.children[i] !== rec.el) this.root.insertBefore(rec.el, this.root.children[i] || null);
      });
      for (const [k, rec] of this.recs) if (!seen.has(k)) { rec.el.remove(); this.recs.delete(k); }
      while (this.root.children.length > blocks.length) this.root.lastChild.remove();
      if (total !== this.total) {
        const first = this.total < 0;
        this.total = total;
        if (stick || first) this.toBottom();
        else if (this.pill) this.pill.hidden = false;
      }
    }
    create(b) {
      if (b.kind === 'run') return this.mode === 'job' ? this.createJobRun(b.run) : this.createChatTurn(b.run);
      if (b.kind === 'log') {
        const d = b.ev.data || {};
        const level = pick(String(d.level || 'info'), LOG_LEVELS, 'info');
        const el = h('div', { class: 't-log ' + level, role: level === 'error' ? 'alert' : null },
          icon(level === 'error' ? 'error' : level === 'warn' ? 'alert' : 'info', 'icon-sm'),
          h('span', { class: 't-log-msg' }, String(d.message || '')),
          timeEl(b.ev.ts, 'muted'));
        return { el };
      }
      // chat message from history or sent from this tab
      if (b.role === 'user') return { el: h('div', { class: 'msg msg-user' }, h('div', { class: 'bubble' }, b.text)) };
      const chat = b.chat ? S.chats.get(b.chat) : null;
      return {
        el: h('div', { class: 'msg msg-assistant' },
          h('div', { class: 'msg-who' }, h('span', { class: 'avatar' }, icon('logo')), (chat && chat.agent) || 'coder'),
          h('div', { class: 'turn-body' }, MD.render(b.text)),
          chat ? extNodes('chatReply', { chat, turn: b.msg && Number.isInteger(b.msg.turn) ? b.msg.turn : null, message: b.msg || null, run: null }) : null),
      };
    }
    update(rec, b, isLast) {
      if (b.kind !== 'run') return 0;
      const run = b.run;
      if (this.mode === 'job') this.updateJobRunHead(rec, run, isLast); else this.updateChatFoot(rec, run);
      if (rec.gen !== run.gen) { clear(rec.body); rec.items = []; rec.gen = run.gen; }
      const done = run.status !== 'running' && run.status !== 'pending';
      for (let k = 0; k < run.items.length; k++) {
        const it = run.items[k];
        let ir = rec.items[k];
        if (!ir) {
          ir = this.createItem(it, run, done);
          rec.items[k] = ir;
          rec.body.appendChild(ir.el);
        } else if (ir.v !== it.v || ir.done !== done) {
          this.updateItem(ir, it, run, done);
        }
      }
      if (rec.placeholder) {
        const show = !run.items.length;
        rec.placeholder.hidden = !show;
        if (show) rec.placeholder.textContent = runIsLive(run) ? (run.waiting ? (run.waitCustody === 'HELD' ? 'Queued: ' + HELD_WAIT + '…' : 'Queued: waiting for a free ' + run.waiting + ' slot…') : 'Waiting for the agent…') : 'No output recorded.';
        if (show && rec.placeholder.parentNode !== rec.body) rec.body.appendChild(rec.placeholder);
      }
      return run.v;
    }
    // ---- job runs: collapsible sections with a header
    createJobRun(run) {
      const head = h('summary', { class: 'run-head' });
      const el = h('details', { class: 'run', dataset: { run: run.name } }, head);
      const rec = { el, head, body: h('div', { class: 'run-body' }), items: [], gen: -1, auto: true, programmatic: false, placeholder: h('div', { class: 'run-empty' }) };
      rec.parts = {
        dot: h('span', { class: 'dot' }),
        agent: h('span', { class: 'run-agent' }),
        model: h('span', { class: 'run-model' }),
        round: h('span', { class: 'tag tag-outline' }),
        ext: h('span', { class: 'run-ext' }),
        usage: h('span', { class: 'run-usage' }),
        dur: h('span', { class: 'run-dur' }),
        status: h('span', { class: 'run-status' }),
      };
      add(head, [icon('chevron', 'icon-sm chev'), rec.parts.dot, rec.parts.agent, rec.parts.model, rec.parts.round, rec.parts.ext,
        h('span', { class: 'run-meta' }, rec.parts.usage, rec.parts.dur, rec.parts.status)]);
      el.appendChild(rec.body);
      el.addEventListener('toggle', () => { if (rec.programmatic) rec.programmatic = false; else rec.auto = false; });
      return rec;
    }
    updateJobRunHead(rec, run, isLast) {
      const p = rec.parts;
      p.dot.className = 'dot ' + agentColorCls(run.agent);
      p.agent.textContent = run.agent || run.name;
      p.model.textContent = runModel(run);
      p.model.title = [run.driver ? run.driver + ' driver' : '', runModel(run), run.provider ? 'provider ' + run.provider : ''].filter(Boolean).join(' · ');
      const hasRound = run.round !== null && run.round !== undefined && run.round !== '';
      p.round.hidden = !hasRound;
      if (hasRound) p.round.textContent = 'r' + run.round;
      p.usage.textContent = fmtUsage(runUsage(run));
      const extSig = run.placeV + '|' + run.status;
      if (rec.extSig !== extSig) { rec.extSig = extSig; clear(p.ext); add(p.ext, extNodes('runHead', runView(run, 'job'))); }
      const live = runIsLive(run);
      const start = parseTs(run.startTs);
      if (live && isFinite(start)) { p.dur.dataset.since = String(start); p.dur.textContent = fmtDur(Date.now() - start); }
      else {
        delete p.dur.dataset.since;
        const end = parseTs(run.endTs);
        p.dur.textContent = isFinite(start) && isFinite(end) ? fmtDur(end - start) : '';
      }
      clear(p.status);
      p.status.title = '';
      if (live && run.waiting) {
        const ww = waitWords({ provider: run.waiting, custody: run.waitCustody });
        p.status.className = 'run-status wait';
        p.status.title = ww.long;
        add(p.status, [icon('hourglass', 'icon-sm'), run.waitCustody === 'HELD' ? HELD_WAIT + '…' : 'waiting for ' + run.waiting + '…']);
      } else if (live) { p.status.className = 'run-status live'; add(p.status, [h('span', { class: 'spinner', 'aria-hidden': 'true' }), 'running']); }
      else if (run.status === 'ok') { p.status.className = 'run-status ok'; add(p.status, [icon('check', 'icon-sm'), 'done']); }
      else if (run.status === 'error') { p.status.className = 'run-status err'; add(p.status, [icon('error', 'icon-sm'), 'failed']); }
      else if (run.status === 'running') {
        const st = unfinishedRunState(run);
        p.status.className = 'run-status' + (st === 'failed' ? ' err' : '');
        add(p.status, st === 'failed' ? [icon('error', 'icon-sm'), 'failed'] : st);
      } else { p.status.className = 'run-status'; add(p.status, 'pending'); }
      const failed = p.status.classList.contains('err');
      rec.el.classList.toggle('is-failed', failed);
      if (rec.auto) {
        const want = live || isLast || failed;
        if (rec.el.open !== want) { rec.programmatic = true; rec.el.open = want; }
      }
    }
    // ---- chat turns: assistant message with inline tools
    createChatTurn(run) {
      const rec = { body: h('div', { class: 'turn-body' }), foot: h('div', { class: 'turn-foot' }), items: [], gen: -1 };
      rec.el = h('div', { class: 'turn msg-assistant', dataset: { run: run.name } },
        h('div', { class: 'msg-who' }, h('span', { class: 'avatar' }, icon('logo')), run.agent || 'coder'),
        rec.body, rec.foot);
      rec.placeholder = h('div', { class: 'run-empty' });
      return rec;
    }
    updateChatFoot(rec, run) {
      clear(rec.foot);
      const live = runIsLive(run);
      // While the turn runs, the bar above the composer shows elapsed time and Cancel; here only a caret.
      rec.el.classList.toggle('is-live', live);
      rec.foot.hidden = live;
      if (live) return;
      const start = parseTs(run.startTs);
      const end = parseTs(run.endTs);
      const bits = [];
      if (runModel(run)) bits.push(runModel(run));
      if (isFinite(start) && isFinite(end)) bits.push(fmtDur(end - start));
      const u = fmtUsage(runUsage(run));
      if (u) bits.push(u);
      const chat = run.owner.indexOf('chat:') === 0 ? S.chats.get(run.owner.slice(5)) : null;
      if (run.status === 'error') rec.foot.appendChild(h('span', { class: 'tag tag-err' }, 'failed'));
      else if (run.status === 'running' && !run.began && chat && chat.status === 'error') rec.foot.appendChild(h('span', { class: 'tag tag-outline', title: 'The turn was refused before the agent started: nothing was sent to a model.' }, 'did not run'));
      else if (run.status === 'running') rec.foot.appendChild(h('span', { class: 'tag tag-outline' }, 'stopped'));
      for (const b of bits) rec.foot.appendChild(h('span', null, b));
      add(rec.foot, extNodes('runHead', runView(run, 'chat')));
      const text = run.items.some((it) => it.t === 'text' && it.text);
      if (chat) add(rec.foot, extNodes('chatReply', { chat, turn: run.no, message: null, run: Object.freeze({ name: run.name, no: run.no, status: run.status, started: run.began, text }) }));
    }
    // ---- items
    createItem(it, run, done) {
      const ir = { t: it.t, v: it.v, done };
      if (it.t === 'text') {
        ir.el = h('div', { class: 't-text' });
      } else if (it.t === 'reasoning') {
        ir.count = h('span', { class: 'muted' });
        ir.body = h('div', { class: 't-reason-body' });
        ir.el = h('details', { class: 't-reason' }, h('summary', null, icon('bulb', 'icon-xs'), 'Reasoning', ir.count), ir.body);
      } else if (it.t === 'tool') {
        ir.name = h('span', { class: 't-tool-name' });
        ir.arg = h('span', { class: 't-tool-arg' });
        ir.status = h('span', { class: 't-tool-status' });
        ir.body = h('div', { class: 't-tool-body' });
        ir.el = h('details', { class: 't-tool' }, h('summary', null, icon('chevron', 'icon-xs chev'), ir.name, ir.arg, ir.status), ir.body);
        ir.built = false;
        ir.el.addEventListener('toggle', () => { if (ir.el.open && !ir.built) this.buildToolBody(ir, ir.item); });
      } else if (it.t === 'output') {
        ir.pre = h('pre', { class: 't-term' });
        ir.lines = 0;
        ir.el = h('div', { class: 't-output' }, h('div', { class: 't-term-head' }, icon('terminal', 'icon-xs'), (run.driver ? run.driver + ' ' : '') + 'output'), ir.pre);
      } else if (it.t === 'error') {
        ir.el = h('div', { class: 't-error', role: 'alert' }, icon('error', 'icon-sm'), h('span', null, it.text));
      } else if (it.t === 'log') {
        ir.el = h('div', { class: 't-log ' + it.level }, icon(it.level === 'error' ? 'error' : it.level === 'warn' ? 'alert' : 'info', 'icon-sm'), h('span', { class: 't-log-msg' }, it.message));
      } else {
        ir.el = h('div');
      }
      this.updateItem(ir, it, run, done);
      return ir;
    }
    updateItem(ir, it, run, done) {
      ir.v = it.v;
      ir.done = done;
      ir.item = it;
      if (it.t === 'text') {
        if (done) {
          if (!ir.md || ir.mdV !== it.v) { clear(ir.el); ir.el.classList.remove('streaming'); ir.el.appendChild(MD.render(it.text, this.mode === 'job' ? 'md-sm' : '')); ir.md = true; ir.mdV = it.v; }
        } else {
          ir.md = false;
          ir.el.classList.add('streaming');
          ir.el.textContent = it.text;
        }
      } else if (it.t === 'reasoning') {
        ir.body.textContent = it.text;
        ir.count.textContent = '· ' + fmtNum(it.text.length) + ' chars';
      } else if (it.t === 'tool') {
        ir.name.textContent = it.name;
        ir.arg.textContent = toolArg(it.input);
        ir.arg.title = ir.arg.textContent;
        clear(ir.status);
        if (!it.result) {
          ir.status.className = 't-tool-status';
          if (runIsLive(run)) add(ir.status, [h('span', { class: 'spinner', 'aria-hidden': 'true' }), 'running']);
          else add(ir.status, 'no result');
        } else if (it.result.ok) {
          ir.status.className = 't-tool-status ok';
          add(ir.status, [icon('check', 'icon-xs'), fmtNum(it.result.output.length) + ' chars']);
        } else {
          ir.status.className = 't-tool-status err';
          add(ir.status, [icon('error', 'icon-xs'), 'error']);
        }
        ir.el.classList.toggle('is-err', !!(it.result && !it.result.ok));
        if (ir.built) this.buildToolBody(ir, it);
      } else if (it.t === 'output') {
        const pre = ir.pre;
        const atEnd = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 24;
        if (it.lines.length > ir.lines) {
          const chunk = it.lines.slice(ir.lines).join('\n');
          pre.appendChild(document.createTextNode((ir.lines ? '\n' : '') + chunk));
          ir.lines = it.lines.length;
        }
        if (atEnd) pre.scrollTop = pre.scrollHeight;
      }
    }
    buildToolBody(ir, it) {
      ir.built = true;
      clear(ir.body);
      if (it.input !== null && it.input !== undefined) {
        ir.body.appendChild(h('div', { class: 't-label' }, 'Input'));
        ir.body.appendChild(h('pre', { class: 't-pre' }, prettyJson(it.input)));
      }
      ir.body.appendChild(h('div', { class: 't-label' }, it.result ? (it.result.ok ? 'Output' : 'Error') : 'Output'));
      if (it.result) ir.body.appendChild(h('pre', { class: 't-pre' + (it.result.ok ? '' : ' err') }, it.result.output || '(empty)'));
      else ir.body.appendChild(h('div', { class: 'run-empty' }, 'Waiting for the result…'));
    }
  }

  // ================================================================ top bar
  function renderConn() {
    const el = document.getElementById('conn');
    if (!el) return;
    el.dataset.state = S.conn;
    const label = S.conn === 'open' ? 'Live' : S.conn === 'connecting' ? 'Connecting' : 'Offline';
    document.getElementById('conn-label').textContent = label;
    const secs = Math.max(0, Math.ceil((S.retryAt - Date.now()) / 1000));
    el.title = S.conn === 'open' ? 'Live: connected to the event stream'
      : S.conn === 'connecting' ? 'Connecting to the event stream…'
        : 'Disconnected from the event stream. Retrying in ' + secs + 's.';
    el.setAttribute('aria-label', el.title);
  }
  RENDER.topbar = function () {
    const sel = document.getElementById('repo-filter');
    const want = [''].concat(S.repos.map((r) => r.name));
    const have = Array.from(sel.options).map((o) => o.value);
    if (want.join('\n') !== have.join('\n')) {
      clear(sel);
      sel.appendChild(h('option', { value: '' }, 'All repos'));
      for (const r of S.repos) sel.appendChild(h('option', { value: r.name }, r.name));
    }
    sel.value = S.filterRepo;
    const ver = document.getElementById('brand-ver');
    ver.textContent = S.version ? 'v' + S.version : '';
    ver.hidden = !S.version;
    const human = visibleJobs().filter((j) => j.stage === 'needs-human').length;
    const badge = document.getElementById('rail-human');
    badge.textContent = String(human);
    badge.hidden = !human;
    badge.title = plural(human, 'job') + ' waiting for you';
    const stuck = visiblePlans().filter((p) => p.stage === 'needs-human' || p.stage === 'blocked' || p.stage === 'failed').length;
    const pb = document.getElementById('rail-plans');
    pb.textContent = String(stuck);
    pb.hidden = !stuck;
    pb.title = plural(stuck, 'plan') + ' waiting for you';
    updateTitle();
    if (S.route.view === 'home') invalidate('home');
  };
  function updateTitle() {
    const human = visibleJobs().filter((j) => j.stage === 'needs-human').length;
    const names = { home: 'Home', factory: 'Factory', plans: 'Plans', chat: 'Chat', agents: 'Agents', settings: 'Settings', doctor: 'Doctor', grid: 'Grid Card', procedures: 'Procedures' };
    const ext = REG.views.get(S.route.view);
    const view = S.route.view === 'flow' ? ({ welcome: 'Welcome', setup: 'Setup', start: 'Start' })[S.route.flow] || 'Welcome' : ext ? ext.title : names[S.route.view] || 'Home';
    document.title = (human ? '(' + human + ') ' : '') + view + ' · ' + PRODUCT;
  }
  function doctorLevel() {
    if (!S.doctor) return 'unknown';
    const bad = S.doctor.checks.filter((c) => !c.ok);
    if (bad.some((c) => (c.level || 'error') === 'error')) return 'error';
    if (bad.some((c) => c.level === 'warn')) return 'warn';
    return 'ok';
  }
  RENDER.doctorBadge = function () {
    const el = document.getElementById('doctor-badge');
    let level = S.doctorLoading && !S.doctor ? 'unknown' : doctorLevel();
    // A fresh install has not been set up yet: what Doctor finds is the to-do list, not an alarm.
    const fresh = level === 'error' && typeof freshInstall === 'function' && freshInstall();
    if (fresh) level = 'warn';
    el.dataset.level = level;
    const bad = S.doctor ? S.doctor.checks.filter((c) => !c.ok && (c.level || 'error') !== 'info') : [];
    const label = document.getElementById('doctor-label');
    label.textContent = level === 'unknown' ? 'Doctor' : level === 'ok' ? 'Healthy' : fresh ? plural(bad.length, 'thing') + ' to set up' : plural(bad.length, 'issue');
    // Phones show only the icon plus the issue count (CSS hides the label there).
    const count = document.getElementById('doctor-count');
    count.textContent = String(bad.length);
    count.hidden = !bad.length || level === 'ok' || level === 'unknown';
    el.title = level === 'unknown' ? (S.doctorError ? 'Doctor failed: ' + S.doctorError : 'Doctor: checking…')
      : level === 'ok' ? 'Doctor: all checks pass'
        : 'Doctor: ' + bad.map((c) => c.name).join(', ');
    el.setAttribute('aria-label', el.title);
    if (S.route.view === 'home') invalidate('home');
  };
  let doctorSeq = 0;
  async function loadDoctor(manual) {
    const seq = ++doctorSeq;   // only the newest run's answer counts (a save can start one while another runs)
    S.doctorLoading = true;
    invalidate('doctor', 'doctorBadge');
    try {
      const r = await api('/api/doctor');
      if (seq !== doctorSeq) return;
      S.doctor = { checks: Array.isArray(r && r.checks) ? r.checks.filter((c) => c && c.name) : [], at: Date.now() };
      S.doctorError = null;
    } catch (e) {
      if (seq !== doctorSeq) return;
      S.doctorError = e.message;
      if (manual) toastError('Doctor failed', e);
    } finally {
      if (seq === doctorSeq) {
        S.doctorLoading = false;
        invalidate('doctor', 'doctorBadge');
      }
    }
  }

  // ================================================================ factory line strip
  const STRIP = { sig: '', nodes: {}, wires: {}, labels: {} };
  const stripReviewers = pipelineReviewers;
  /** Runs queued on a busy provider, per provider (visible repos only). */
  function providerQueues() {
    const q = new Map();
    for (const w of S.waiting.values()) {
      if (S.filterRepo && w.repo && w.repo !== S.filterRepo) continue;
      q.set(w.provider, (q.get(w.provider) || 0) + 1);
    }
    return q;
  }
  // Two layouts: regular (one-line reviewer boxes with the model inline) when the whole loop fits,
  // compact (smaller boxes, two-line reviewers) down to about 920px so laptops see the full line.
  const STRIP_TIERS = {
    regular: { PAD: 12, RH: 34, RG: 6, NH: 62, OH: 52, W: { feature: 116, spec: 142, builder: 140, rev: 216, wait: 112, approver: 140, out: 170 }, G: [24, 24, 38, 34, 54, 62], grow: 36 },
    compact: { PAD: 8, RH: 38, RG: 5, NH: 56, OH: 48, W: { feature: 86, spec: 128, builder: 110, rev: 108, wait: 94, approver: 110, out: 138 }, G: [12, 12, 16, 10, 28, 50], grow: 30 },
  };
  const tierBase = (t) => t.PAD * 2 + Object.keys(t.W).reduce((s, k) => s + t.W[k], 0) + t.G.reduce((s, x) => s + x, 0);
  function stripTier(avail) { return avail >= tierBase(STRIP_TIERS.regular) ? 'regular' : 'compact'; }
  function stripGeometry(reviewers, avail, tierName) {
    const T = STRIP_TIERS[tierName];
    const PAD = T.PAD, RH = T.RH, RG = T.RG, NH = T.NH, OH = T.OH;
    const n = reviewers.length;
    const stackH = Math.max(0, n * RH + (n - 1) * RG);
    const bandH = Math.max(stackH, OH * 2 + 14, NH);
    const top = PAD + 6;
    const cy = top + bandH / 2;
    const W = T.W;
    const G = T.G;
    const addEach = Math.min(Math.max(0, avail - tierBase(T)) / G.length, T.grow);
    const g = G.map((x) => x + addEach);
    const pos = {};
    let x = PAD;
    const main = (k) => { pos[k] = { x, y: cy - NH / 2, w: W[k], h: NH }; };
    main('feature'); x += W.feature + g[0];
    main('spec'); x += W.spec + g[1];
    main('builder'); x += W.builder + g[2];
    reviewers.forEach((r, k) => { pos['rev:' + r] = { x, y: cy - stackH / 2 + k * (RH + RG), w: W.rev, h: RH }; });
    x += W.rev + g[3];
    pos.wait = { x, y: cy - 18, w: W.wait, h: 36 }; x += W.wait + g[4];
    main('approver'); x += W.approver + g[5];
    pos.merge = { x, y: cy - 7 - OH, w: W.out, h: OH };
    pos.human = { x, y: cy + 7, w: W.out, h: OH };
    x += W.out + PAD;
    const loopY = top + bandH + 20;
    return { pos, width: Math.floor(x), height: Math.round(loopY + 18), loopY, cy };
  }
  function buildStrip(canvas, reviewers, avail) {
    const tier = stripTier(avail);
    const compact = tier === 'compact';
    const geo = stripGeometry(reviewers, avail, tier);
    clear(canvas);
    canvas.classList.toggle('compact', compact);
    canvas.style.width = geo.width + 'px';
    canvas.style.height = geo.height + 'px';
    STRIP.nodes = {}; STRIP.wires = {}; STRIP.labels = {};
    const svg = svgEl('svg', { class: 'strip-svg', width: geo.width, height: geo.height, viewBox: '0 0 ' + geo.width + ' ' + geo.height, 'aria-hidden': 'true' });
    const defs = svgEl('defs');
    for (const kind of ['base', 'hot', 'loop', 'human', 'ok']) {
      const mk = svgEl('marker', { id: 'sf-arrow-' + kind, viewBox: '0 0 10 10', refX: 8.5, refY: 5, markerWidth: 7, markerHeight: 7, orient: 'auto-start-reverse' });
      mk.appendChild(svgEl('path', { d: 'M1 1.5 L8.5 5 L1 8.5 z', class: 'arrow-head' + (kind === 'base' ? '' : ' ' + kind) }));
      defs.appendChild(mk);
    }
    svg.appendChild(defs);
    canvas.appendChild(svg);
    const P = geo.pos;
    const R = (p) => ({ x: p.x + p.w, y: p.y + p.h / 2 });
    const L = (p) => ({ x: p.x - 2, y: p.y + p.h / 2 });
    const curve = (a, b) => {
      const dx = Math.max(14, (b.x - a.x) * 0.55);
      return 'M' + a.x + ',' + a.y + ' C' + (a.x + dx) + ',' + a.y + ' ' + (b.x - dx) + ',' + b.y + ' ' + b.x + ',' + b.y;
    };
    const wire = (id, d, cls) => {
      const path = svgEl('path', { d, class: 'wire' + (cls ? ' ' + cls : ''), 'marker-end': 'url(#sf-arrow-' + (cls || 'base') + ')' });
      path.dataset.cls = cls || '';
      svg.appendChild(path);
      STRIP.wires[id] = path;
    };
    const label = (id, text, x, y, cls) => {
      const el = h('span', { class: 'wire-label' + (cls ? ' ' + cls : ''), style: { left: x + 'px', top: y + 'px' } }, text);
      canvas.appendChild(el);
      STRIP.labels[id] = el;
    };
    wire('feature-spec', curve(R(P.feature), L(P.spec)));
    wire('spec-builder', curve(R(P.spec), L(P.builder)));
    if (!reviewers.length) wire('builder-wait', curve(R(P.builder), L(P.wait)));
    for (const r of reviewers) {
      wire('builder-' + r, curve(R(P.builder), L(P['rev:' + r])));
      wire(r + '-wait', curve(R(P['rev:' + r]), L(P.wait)));
    }
    wire('wait-approver', curve(R(P.wait), L(P.approver)));
    wire('approver-merge', curve(R(P.approver), L(P.merge)), 'ok');
    wire('approver-human', curve(R(P.approver), L(P.human)), 'human');
    const wx = P.wait.x + P.wait.w / 2;
    const wb = P.wait.y + P.wait.h;
    const bx = P.builder.x + P.builder.w / 2;
    const bb = P.builder.y + P.builder.h + 2;
    const ly = geo.loopY;
    const rr = 10;
    wire('loop', 'M' + wx + ',' + wb + ' V' + (ly - rr) + ' Q' + wx + ',' + ly + ' ' + (wx - rr) + ',' + ly +
      ' H' + (bx + rr) + ' Q' + bx + ',' + ly + ' ' + bx + ',' + (ly - rr) + ' V' + bb, 'loop');
    const midA = (a, b) => (a.x + a.w + b.x) / 2;
    label('pass', compact ? 'PASS' : 'all PASS', midA(P.wait, P.approver), P.wait.y + P.wait.h / 2 - 11);
    label('approve', 'APPROVE', midA(P.approver, P.merge), (P.merge.y + P.merge.h / 2 + geo.cy) / 2 - 4, 'ok');
    label('escalate', 'ESCALATE', midA(P.approver, P.human), (P.human.y + P.human.h / 2 + geo.cy) / 2 + 4, 'human');
    label('loop', 'any CHANGES → resume builder', (wx + bx) / 2, ly, 'loop');

    const node = (key, stage, cls, kids, title) => {
      const p = P[key];
      const b = h('button', { type: 'button', class: 'node ' + cls, dataset: { stage, node: key }, style: { left: p.x + 'px', top: p.y + 'px', width: p.w + 'px', height: p.h + 'px' } });
      add(b, kids);
      b.addEventListener('click', () => focusColumn(stage));
      canvas.appendChild(b);
      const rec = { el: b, count: b.querySelector('.node-count'), model: b.querySelector('.node-model'), sub: b.querySelector('.node-sub'), title };
      STRIP.nodes[key] = rec;
      return rec;
    };
    const count = () => h('span', { class: 'node-count' }, '0');
    const agentNode = (key, stage, agent) => node(key, stage, 'n-agent n-' + key, [
      h('span', { class: 'node-head' }, h('span', { class: 'dot ' + agentColorCls(agent) }), h('span', { class: 'node-name' }, agent), count()),
      h('span', { class: 'node-model' }, ''),
    ]);
    node('feature', 'queued', 'n-feature', [
      h('span', { class: 'node-head' }, icon('inbox', 'icon-sm'), h('span', { class: 'node-name' }, 'Feature'), count()),
      h('span', { class: 'node-sub' }, 'queued'),
    ]);
    agentNode('spec', 'spec', 'spec-writer').agent = 'spec-writer';
    agentNode('builder', 'build', 'builder').agent = 'builder';
    for (const r of reviewers) {
      const rec = node('rev:' + r, 'review', 'reviewer', [
        h('span', { class: 'node-head' }, h('span', { class: 'dot ' + agentColorCls('reviewer-' + r) }), h('span', { class: 'node-name' }, r), count()),
        h('span', { class: 'node-model' }, ''),
      ]);
      rec.agent = 'reviewer-' + r;
      rec.reviewer = r;
    }
    node('wait', 'review', 'small n-wait', [
      h('span', { class: 'node-head' }, icon('hourglass', 'icon-xs'), h('span', { class: 'node-name' }, 'wait for all'), count()),
    ]);
    agentNode('approver', 'approve', 'approver').agent = 'approver';
    node('merge', 'merged', 'n-merge', [
      h('span', { class: 'node-head' }, icon('merge', 'icon-sm'), h('span', { class: 'node-name' }, 'Merge to base'), count()),
      h('span', { class: 'node-sub' }, ''),
    ]);
    node('human', 'needs-human', 'n-human', [
      h('span', { class: 'node-head' }, icon('user', 'icon-sm'), h('span', { class: 'node-name' }, 'Needs human'), count()),
      h('span', { class: 'node-sub' }, 'approve · rework · reject'),
    ]);
  }
  RENDER.strip = function () {
    const card = document.getElementById('strip-card');
    const pref = lsGet('sf.stripCollapsed');
    const collapsed = pref === null ? window.matchMedia('(max-width: 720px)').matches : pref === '1';
    card.classList.toggle('collapsed', collapsed);
    const scroller = document.getElementById('strip-scroll');
    scroller.hidden = collapsed;
    const toggle = document.getElementById('strip-toggle');
    toggle.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
    toggle.title = collapsed ? 'Expand the factory line' : 'Collapse the factory line';
    const chev = toggle.querySelector('use');
    if (chev) chev.setAttribute('href', collapsed ? '#i-chevron' : '#i-chevron-down');
    const reviewers = stripReviewers();
    const jobs = visibleJobs();
    const c = {};
    for (const s of Object.keys(STAGE_LABEL)) c[s] = 0;
    const rev = {};
    for (const r of reviewers) rev[r] = { running: 0, pending: 0, done: 0 };
    let resuming = 0;
    for (const j of jobs) {
      c[j.stage] = (c[j.stage] || 0) + 1;
      if (j.stage === 'review' && j.reviews) {
        for (const r of reviewers) {
          const v = j.reviews[r];
          if (!rev[r]) continue;
          if (v === 'running') rev[r].running++;
          else if (v === 'pending' || v === undefined) rev[r].pending++;
          else rev[r].done++;
        }
      }
      if (j.stage === 'build' && (Number(j.resumes) > 0 || Number(j.round) > 1)) resuming++;
    }
    renderStripSummary(c);
    if (collapsed) return;
    const avail = scroller.clientWidth || 1200;
    const sig = reviewers.join(',') + '|' + Math.round(avail / 12) + '|' + S.agents.map((a) => a.name + ':' + a.color).join(',');
    if (sig !== STRIP.sig) { buildStrip(document.getElementById('strip-canvas'), reviewers, avail); STRIP.sig = sig; requestAnimationFrame(refreshFades); }
    const N = STRIP.nodes;
    // Runs of each agent queued on a busy provider slot (agent.waiting), for the node markers.
    const waits = {};
    for (const w of S.waiting.values()) {
      if ((S.filterRepo && w.repo && w.repo !== S.filterRepo) || w.owner.indexOf(':') >= 0) continue;
      (waits[w.agent] = waits[w.agent] || []).push(w.provider);
    }
    const setNode = (key, n, title, extraActive) => {
      const rec = N[key];
      if (!rec) return;
      if (rec.count) rec.count.textContent = String(n);
      const active = n > 0 || !!extraActive;
      rec.el.classList.toggle('active', active);
      const queued = rec.agent ? waits[rec.agent] || [] : [];
      rec.el.classList.toggle('is-waiting', queued.length > 0);
      if (queued.length) title += ' ' + plural(queued.length, 'run') + ' waiting for ' + Array.from(new Set(queued)).join(', ') + '.';
      rec.el.title = title;
      rec.el.setAttribute('aria-label', title);
    };
    const modelLine = (rec) => {
      if (!rec || !rec.agent || !rec.model) return '';
      const m = modelOf(rec.agent);
      rec.model.textContent = m.model;
      return rec.agent + ': ' + (m.key && m.key !== m.model ? m.key + ' → ' : '') + (m.provider ? m.provider + '/' : '') + m.model + (m.driver ? ' (' + m.driver + ' driver)' : '');
    };
    setNode('feature', c.queued, plural(c.queued, 'job') + ' queued');
    setNode('spec', c.spec, modelLine(N.spec) + '. ' + plural(c.spec, 'job') + ' in spec.');
    setNode('builder', c.build, modelLine(N.builder) + '. ' + plural(c.build, 'job') + ' building.');
    for (const r of reviewers) {
      const rec = N['rev:' + r];
      const ml = modelLine(rec);
      setNode('rev:' + r, rev[r].running, ml + '. ' + rev[r].running + ' reviewing, ' + rev[r].pending + ' waiting, ' + rev[r].done + ' done this round.');
    }
    setNode('wait', c.review, plural(c.review, 'job') + ' in a review round (the builder resumes only after every reviewer finishes)');
    setNode('approver', c.approve, modelLine(N.approver) + '. ' + plural(c.approve, 'job') + ' at the final gate.');
    const pipe = S.pipeline || {};
    const autoMerge = pipe.auto_merge !== false;
    if (N.merge) {
      const compact = document.getElementById('strip-canvas').classList.contains('compact');
      N.merge.sub.textContent = (c.merged ? c.merged + ' merged · ' : '') + (c.delivered ? c.delivered + ' delivered · ' : '') + (autoMerge ? 'auto ≤ ' + fmtNum(pipe.max_auto_merge_lines || 1500) + (compact ? '' : ' lines') : 'auto-merge off');
      N.merge.sub.title = autoMerge ? 'Auto-merge when every review passes, the approver approves and the diff has at most ' + (pipe.max_auto_merge_lines || 1500) + ' changed lines' : 'Auto-merge is off';
    }
    setNode('merge', c.merging, c.merging + ' merging, ' + c.merged + ' merged. ' + (autoMerge ? 'Auto-merge is on.' : 'Auto-merge is off: approved jobs wait for you.'));
    setNode('human', c['needs-human'], plural(c['needs-human'], 'job') + ' waiting for you to approve, rework or reject');
    const maxRes = pipe.max_resumes !== undefined ? pipe.max_resumes : 2;
    if (STRIP.labels.loop) STRIP.labels.loop.textContent = 'any CHANGES → resume builder (max ' + maxRes + ')';
    const hot = (id, on) => {
      const w = STRIP.wires[id];
      if (!w) return;
      const base = w.dataset.cls;
      w.setAttribute('class', 'wire' + (base ? ' ' + base : '') + (on ? ' hot' : ''));
      w.setAttribute('marker-end', 'url(#sf-arrow-' + (on && base !== 'loop' ? 'hot' : (base || 'base')) + ')');
    };
    hot('feature-spec', c.spec > 0);
    hot('spec-builder', c.build > 0);
    for (const r of reviewers) { hot('builder-' + r, rev[r].running > 0); hot(r + '-wait', rev[r].done > 0 && c.review > 0); }
    hot('wait-approver', c.approve > 0);
    hot('approver-merge', c.merging > 0);
    hot('approver-human', false);
    hot('loop', resuming > 0);
  };
  function renderStripSummary(c) {
    const box = document.getElementById('strip-summary');
    clear(box);
    const active = ['queued', 'spec', 'build', 'review', 'approve', 'merging'].reduce((s, k) => s + (c[k] || 0), 0);
    box.appendChild(h('span', { class: 'pill ' + (active ? 'tone-accent live' : 'tone-muted') }, active + ' active'));
    // A small queue count per provider while agents wait for a free slot (max_concurrency).
    for (const [prov, n] of providerQueues()) {
      box.appendChild(h('span', { class: 'pill no-dot tone-warn queue-pill', dataset: { provider: prov }, title: plural(n, 'agent run') + ' waiting for a free ' + prov + ' slot' },
        icon('hourglass', 'icon-xs'), h('span', { class: 'queue-name' }, prov), h('span', { class: 'queue-n' }, String(n))));
    }
    if (c['needs-human']) box.appendChild(h('span', { class: 'pill tone-human' }, c['needs-human'] + ' need you'));
    if (c.failed) box.appendChild(h('span', { class: 'pill tone-err' }, c.failed + ' failed'));
    if (c.merged) box.appendChild(h('span', { class: 'pill tone-ok' }, c.merged + ' merged'));
    if (c.delivered) box.appendChild(h('span', { class: 'pill tone-ok' }, c.delivered + ' delivered'));
  }

  // ================================================================ board
  const COLUMNS = [
    { id: 'queued', title: 'Queued', stages: ['queued'], dot: 'c-gray' },
    { id: 'spec', title: 'Spec', stages: ['spec'], dot: 'c-blue' },
    { id: 'build', title: 'Build', stages: ['build'], dot: 'c-green' },
    { id: 'review', title: 'Review', stages: ['review'], dot: 'c-purple' },
    { id: 'approve', title: 'Approve', stages: ['approve', 'merging'], dot: 'c-orange' },
    { id: 'needs-human', title: 'Needs human', stages: ['needs-human'], dot: 'c-orange' },
    { id: 'merged', title: 'Done', stages: ['merged', 'delivered'], dot: 'c-green' },
    { id: 'closed', title: 'Closed', stages: ['failed', 'rejected', 'cancelled'], dot: 'c-red' },
  ];
  const STAGE_COLUMN = {};
  for (const col of COLUMNS) for (const s of col.stages) STAGE_COLUMN[s] = col.id;
  const BOARD = { cols: null, cards: new Map() };
  function focusColumn(stage) {
    const colId = STAGE_COLUMN[stage] || stage;
    if (S.route.view !== 'factory') { location.hash = '#/factory'; }
    const col = BOARD.cols && BOARD.cols[colId];
    if (!col) return;
    if (colId === 'closed' && col.el.classList.contains('collapsed')) toggleClosed(false);
    const reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    col.el.scrollIntoView({ behavior: reduce ? 'auto' : 'smooth', block: 'nearest', inline: 'center' });
    col.el.classList.remove('flash');
    void col.el.offsetWidth;
    col.el.classList.add('flash');
  }
  function toggleClosed(collapse) {
    lsSet('sf.closedCollapsed', collapse ? '1' : '0');
    invalidate('board');
  }
  function reviewRounds(job) {
    const rows = [];
    const hist = Array.isArray(job.history) ? job.history : [];
    for (const r of hist) if (r && isObj(r.reviews)) rows.push({ round: r.round, reviews: r.reviews });
    const cur = isObj(job.reviews) ? job.reviews : {};
    const round = Number(job.round) || 0;
    const inHist = hist.some((r) => r && Number(r.round) === round);
    const vals = Object.keys(cur).map((k) => cur[k]);
    if (round > 0 && !inHist && vals.length && (job.stage === 'review' || vals.some((v) => v !== 'pending'))) rows.push({ round, reviews: cur, current: true });
    return rows;
  }
  function chipRow(job, row) {
    const order = Array.isArray(job.reviewers) && job.reviewers.length ? job.reviewers.slice() : Object.keys(row.reviews);
    for (const k of Object.keys(row.reviews)) if (order.indexOf(k) < 0) order.push(k);
    const labels = reviewerLabels(order);
    const desc = [];
    const el = h('span', { class: 'round' }, h('span', { class: 'round-label', 'aria-hidden': 'true' }, 'r' + row.round));
    for (const r of order) {
      const v = String(row.reviews[r] || 'pending');
      const full = r + ': ' + v;
      desc.push(full);
      const label = labels[r];
      el.appendChild(h('span', { class: 'chip v-' + pick(v, CHIP_STATES, 'other') + (label.length > 1 ? ' chip-wide' : ''), title: 'Round ' + row.round + ' · ' + full, 'aria-hidden': 'true' }, label));
    }
    el.appendChild(h('span', { class: 'sr-only' }, 'Round ' + row.round + ': ' + desc.join(', ') + '.'));
    return el;
  }
  function fillCard(el, job, showRepo) {
    clear(el);
    const stage = job.stage;
    el.className = 'card' + (stage === 'needs-human' ? ' is-human' : '') + (stage === 'failed' ? ' is-failed' : '') +
      (stage === 'merged' || stage === 'rejected' || stage === 'cancelled' ? ' is-done' : '');
    const showPill = stage === 'merging' || stage === 'failed' || stage === 'rejected' || stage === 'cancelled' || !STAGE_COLUMN[stage];
    // The id gets the whole top row (the time is short); the repo tag moves to the meta line.
    const wait = ACTIVE.has(stage) ? waitingOf(jobKey(job)) : null;
    add(el, [
      h('span', { class: 'card-top' },
        h('span', { class: 'card-id', title: job.repo + '/' + job.id }, job.id),
        timeEl(job.updated_at || job.created_at, 'card-time')),
      h('span', { class: 'card-feature' }, featureTitle(job) || '(no feature text)'),
      wait ? h('span', { class: 'card-detail card-wait', title: (wait.agent ? wait.agent + ' is queued: ' : 'Queued: ') + (wait.custody === 'HELD' ? HELD_WAIT : 'no free ' + wait.provider + ' slot yet') }, icon('hourglass', 'icon-xs'), wait.custody === 'HELD' ? 'waiting: the card is held by someone else' : 'waiting for ' + wait.provider + '…')
        : job.detail ? h('span', { class: 'card-detail', title: job.detail }, job.detail) : null,
    ]);
    const meta = [];
    if (showRepo) meta.push(h('span', { class: 'tag' }, job.repo));
    const planId = planOfJob(job);
    if (planId) meta.push(h('span', { class: 'tag tag-accent tag-mono', title: 'Part of plan ' + planId }, icon('plan', 'icon-xs'), planId.slice(0, 4)));
    if (showPill) meta.push(stagePill(stage));
    if (Number(job.round) > 0 || Number(job.resumes) > 0) {
      meta.push(h('span', { class: 'card-round' }, 'r' + (Number(job.round) || 0) + ' · resumes ' + (Number(job.resumes) || 0) + '/' + (job.max_resumes !== undefined && job.max_resumes !== null ? job.max_resumes : '?')));
    }
    if (job.usage && (job.usage.output_tokens || job.usage.input_tokens)) meta.push(h('span', { title: fmtUsage(job.usage, true) }, fmtNum((job.usage.input_tokens || 0) + (job.usage.output_tokens || 0)) + ' tok'));
    if (job.decision === 'APPROVE' && stage === 'needs-human') meta.push(h('span', { class: 'tag tag-ok' }, 'approved'));
    if (stage === 'delivered') {
      const mode = jobDelivery(job) || 'branch';
      meta.push(h('span', { class: 'tag tag-deliver', title: 'Delivered: ' + DELIVERY_UI[mode].text + '. Open it to merge it now.' }, icon(DELIVERY_UI[mode].icon, 'icon-xs'), mode === 'folder' ? 'in folder' : 'on branch'));
    } else if (stage === 'merged') meta.push(h('span', { class: 'tag tag-ok tag-merged' }, icon('merge', 'icon-xs'), 'merged'));
    const pushed = pushChip(job);
    if (pushed) meta.push(pushed);
    for (const n of extNodes('jobCard', job)) meta.push(n);
    if (meta.length) el.appendChild(h('span', { class: 'card-meta' }, meta));
    const reason = job.reason || (stage === 'failed' ? job.error : null);
    if (reason && (stage === 'needs-human' || stage === 'failed' || stage === 'rejected')) {
      el.appendChild(h('span', { class: 'card-reason' + (stage === 'failed' ? ' err' : ''), title: String(reason) }, String(reason)));
    }
    const rows = reviewRounds(job);
    if (rows.length) el.appendChild(h('span', { class: 'rounds' }, rows.map((r) => chipRow(job, r))));
  }
  /** A card is a button inside a role=listitem wrapper (the button keeps its native role). */
  function getCard(job, showRepo) {
    const k = jobKey(job);
    let item = BOARD.cards.get(k);
    if (!item) {
      const el = h('button', { type: 'button', class: 'card', dataset: { job: k } });
      el.addEventListener('click', () => { D.opener = el; location.hash = '#/job/' + enc(job.repo) + '/' + enc(job.id); });
      item = h('div', { class: 'card-item', role: 'listitem' }, el);
      item._card = el;
      BOARD.cards.set(k, item);
    }
    const el = item._card;
    const w = waitingOf(k);
    const sig = (job.updated_at || '') + '|' + showRepo + '|' + job.stage + '|' + (w ? w.provider + ':' + (w.custody || '') : '') + '|' + JSON.stringify(job.push || null);
    if (el._job !== job || el._sig !== sig) { fillCard(el, job, showRepo); el._job = job; el._sig = sig; }
    el.setAttribute('aria-current', D.key === k ? 'true' : 'false');
    return item;
  }
  function buildBoardColumns(board) {
    clear(board);
    BOARD.cols = {};
    for (const col of COLUMNS) {
      const count = h('span', { class: 'count-pill' }, '0');
      const head = h('div', { class: 'col-head' }, h('span', { class: 'dot ' + col.dot, 'aria-hidden': 'true' }), h('h3', { class: 'col-title', id: 'col-' + col.id + '-t' }, col.title), count);
      const body = h('div', { class: 'col-body', role: 'list', 'aria-labelledby': 'col-' + col.id + '-t' });
      const el = h('section', { class: 'col col-' + col.id, dataset: { col: col.id } }, head, body);
      let toggle = null;
      if (col.id === 'closed') {
        toggle = h('button', { type: 'button', class: 'btn btn-ghost btn-xs btn-icon col-toggle', 'aria-controls': 'col-closed-body' }, icon('chevron', 'icon-xs'));
        body.id = 'col-closed-body';
        toggle.addEventListener('click', () => toggleClosed(!el.classList.contains('collapsed')));
        head.appendChild(toggle);
      }
      BOARD.cols[col.id] = { el, body, count, toggle, empty: h('div', { class: 'col-empty', role: 'listitem' }, 'No jobs') };
      board.appendChild(el);
    }
  }
  RENDER.board = function () {
    const board = document.getElementById('board');
    document.getElementById('board-count').textContent = String(visibleJobs().length);
    if (!S.loaded) {
      BOARD.cols = null;
      clear(board);
      if (S.stateError) {
        board.appendChild(emptyState('alert', 'Cannot load the factory', S.stateError, [btn('Retry', { cls: 'btn-primary', icon: 'refresh', onClick: () => refreshState() })]));
        return;
      }
      for (let i = 0; i < 5; i++) {
        board.appendChild(h('section', { class: 'col', 'aria-hidden': 'true' },
          h('div', { class: 'col-head' }, h('div', { class: 'skel skel-line', style: { width: '40%' } })),
          h('div', { class: 'col-body' }, h('div', { class: 'skel skel-card' }), i % 2 ? h('div', { class: 'skel skel-card' }) : null)));
      }
      return;
    }
    const all = visibleJobs();
    if (!S.repos.length && !S.jobs.size) {
      BOARD.cols = null;
      clear(board);
      board.appendChild(emptyState('folder', 'Add a repository to start the factory', 'The factory works on git repositories on this server. Setup registers one by its path, together with your models and keys.',
        [h('a', { class: 'btn btn-primary', href: '#/setup/repo' }, icon('plus', 'icon-sm'), 'Add a repository'), h('a', { class: 'btn', href: '#/welcome' }, 'Welcome')]));
      return;
    }
    if (!all.length) {
      BOARD.cols = null;
      clear(board);
      board.appendChild(emptyState('zap', 'Describe a feature to start the line',
        'The spec-writer turns it into a spec, the builder implements it, ' + (pipelineReviewers().length ? plural(pipelineReviewers().length, 'reviewer') : 'the reviewers') + ' check it and the approver decides. ' + (S.filterRepo ? 'No jobs in ' + S.filterRepo + ' yet.' : ''),
        [btn('New feature', { cls: 'btn-primary', icon: 'plus', onClick: () => openNewFeature() }), h('a', { class: 'btn', href: '#/start' }, icon('route', 'icon-sm'), 'Step by step'), btn('Open backlog', { icon: 'list', onClick: () => setBacklogOpen(true) })]));
      return;
    }
    if (!BOARD.cols) buildBoardColumns(board);
    const focused = document.activeElement;
    const q = S.search.trim().toLowerCase();
    const jobs = q ? all.filter((j) => [j.id, j.feature, j.detail, j.repo, j.reason, j.branch].some((x) => x && String(x).toLowerCase().indexOf(q) >= 0)) : all;
    const showRepo = !S.filterRepo && S.repos.length !== 1;
    const byCol = {};
    for (const col of COLUMNS) byCol[col.id] = [];
    for (const j of jobs) byCol[STAGE_COLUMN[j.stage] || 'closed'].push(j);
    const seen = new Set();
    for (const col of COLUMNS) {
      const rec = BOARD.cols[col.id];
      const list = byCol[col.id];
      if (col.id === 'queued') list.sort((a, b) => String(a.created_at || a.id).localeCompare(String(b.created_at || b.id)));
      else list.sort((a, b) => String(b.updated_at || '').localeCompare(String(a.updated_at || '')));
      rec.count.textContent = String(list.length);
      rec.count.classList.toggle('has', list.length > 0);
      rec.el.classList.toggle('is-empty', !list.length);
      if (col.id === 'closed') {
        const collapsed = lsGet('sf.closedCollapsed') !== '0';
        rec.el.classList.toggle('collapsed', collapsed);
        rec.toggle.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
        rec.toggle.setAttribute('aria-label', collapsed ? 'Expand closed jobs' : 'Collapse closed jobs');
        rec.toggle.title = rec.toggle.getAttribute('aria-label');
        rec.toggle.querySelector('use').setAttribute('href', collapsed ? '#i-chevron' : '#i-chevron-down');
        const failed = list.filter((j) => j.stage === 'failed').length;
        rec.count.classList.toggle('tone-err', failed > 0);
        rec.count.title = failed ? failed + ' failed' : '';
      }
      const els = list.map((j) => { seen.add(jobKey(j)); return getCard(j, showRepo); });
      if (!els.length) els.push(rec.empty);
      rec.empty.textContent = q ? 'No matches' : col.id === 'needs-human' ? 'Nothing waiting for you' : 'No jobs';
      els.forEach((el, i) => { if (rec.body.children[i] !== el) rec.body.insertBefore(el, rec.body.children[i] || null); });
      while (rec.body.children.length > els.length) rec.body.lastChild.remove();
    }
    for (const k of Array.from(BOARD.cards.keys())) if (!seen.has(k)) { BOARD.cards.get(k).remove(); BOARD.cards.delete(k); }
    if (focused && focused.classList && focused.classList.contains('card') && focused.isConnected && document.activeElement !== focused) focused.focus({ preventScroll: true });
    const wrap = document.getElementById('board-wrap');
    if (wrap._fades) wrap._fades();
  };
  /** Needs human sticks to the right edge of the board while its place is scrolled out of view. */
  function boardFades(f) {
    const col = BOARD.cols && BOARD.cols['needs-human'];
    if (!col || !col.el.isConnected) return f;
    const prev = col.el.previousElementSibling;
    const pinned = !!prev && getComputedStyle(col.el).position === 'sticky' &&
      col.el.getBoundingClientRect().left < prev.getBoundingClientRect().right + 4;
    col.el.classList.toggle('is-pinned', pinned);
    return pinned ? f.replace('r', '') : f;
  }

  // ================================================================ routing helpers (DESIGN §23)
  // What each preset in examples/ routes, as the README describes them (the API lists names only).
  const PRESET_INFO = {
    deepseek: 'DeepSeek V4 Pro and Flash over the API for every tier and agent.',
    anthropic: 'Claude Opus 5.5, Sonnet 5.5 and Haiku 5.5 over the Anthropic API.',
    nanogpt: 'One NanoGPT key for Claude, GPT, Gemini, Qwen and DeepSeek models.',
    ollama: 'Every agent local on Ollama.',
    strata: 'Every agent local on Strata (Qwen3.8-Flash-Next).',
    hybrid: 'Local for volume (spec-writer, builder, security, chat, scout), Claude Sonnet for the other reviewers, Opus at the gate, Fable as the lead.',
    mixed: 'Two local llama.cpp servers (Qwen 27B and Gemma 4) plus Claude Opus for security, the approver and the lead.',
    'gpt-oss': 'gpt-oss-120b on llama.cpp for the judgment roles, DeepSeek for the builder and the other reviewers.',
    'local-first': 'Local Flash-Next builds, codes, scouts and reviews security; Claude Opus specs, approves and leads; GPT-6.1 Sol, Claude Sonnet and Gemini review; five routing profiles.',
    'nanogpt-value': 'All API through NanoGPT: Muse Spark builds, four model families review, Claude Opus specs, approves and leads.',
  };
  // A few words per preset for the menus (the hint and the confirm show PRESET_INFO in full).
  const PRESET_SHORT = {
    deepseek: 'DeepSeek API everywhere', anthropic: 'Claude API everywhere', nanogpt: 'one NanoGPT key, many vendors',
    ollama: 'all local on Ollama', strata: 'all local on Strata', hybrid: 'local volume, Claude reviews and gate',
    mixed: 'two llama.cpp servers + Claude Opus', 'gpt-oss': 'gpt-oss-120b judges + DeepSeek',
    'local-first': 'local Flash-Next builds, API judges', 'nanogpt-value': 'all API through one NanoGPT key',
  };
  const presetLabel = (p) => p + (PRESET_SHORT[p] ? ' — ' + PRESET_SHORT[p] : '');
  /** The routing presets of a config view: names (older servers) or {name, description} objects (§25);
   *  an example that routes nothing (an MCP example such as rea-*) is left out of the routing menus. */
  function presetList(cfg) {
    const raw = cfg && Array.isArray(cfg.presets) ? cfg.presets : [];
    const FL = window.SFFlows;
    if (FL && typeof FL.normalizePresets === 'function') return FL.normalizePresets(raw).filter((p) => p.routing);
    return raw.filter((p) => typeof p === 'string' && p).map((name) => ({ name, description: PRESET_INFO[name] || '' }));
  }
  /** What a preset routes: the server's description when it sends one, else the README summary. */
  function presetInfo(name) {
    const raw = CFG.orig && Array.isArray(CFG.orig.presets) ? CFG.orig.presets : [];
    const o = raw.find((p) => p && typeof p === 'object' && p.name === name);
    return (o && typeof o.description === 'string' && o.description) || PRESET_INFO[name] || '';
  }
  const TIER_ORDER = ['opus', 'sonnet', 'haiku'];
  function tierSort(a, b) {
    const ra = TIER_ORDER.indexOf(a) >= 0 ? TIER_ORDER.indexOf(a) : 9;
    const rb = TIER_ORDER.indexOf(b) >= 0 ? TIER_ORDER.indexOf(b) : 9;
    return ra - rb || a.localeCompare(b);
  }
  let cfgPromise = null;
  /** The config view (GET /api/config): loaded once and shared with Settings. */
  function ensureConfig() {
    if (CFG.orig) return Promise.resolve(CFG.orig);
    if (!cfgPromise) cfgPromise = loadConfig().then(() => { cfgPromise = null; });
    return cfgPromise.then(() => CFG.orig);
  }
  const cfgModels = (cfg) => (cfg && isObj(cfg.models) ? cfg.models : {});
  const cfgProviders = (cfg) => (cfg && isObj(cfg.providers) ? cfg.providers : {});
  function modelKeys(cfg) { return Object.keys(cfgModels(cfg)).filter((k) => isObj(cfgModels(cfg)[k])).sort(tierSort); }
  function driverNames(cfg) {
    return Object.keys(cfg && isObj(cfg.drivers) ? cfg.drivers : {}).concat(S.drivers || []).filter((v, i, a) => v && a.indexOf(v) === i);
  }
  function modelProvider(cfg, key) { const m = cfgModels(cfg)[key]; return m && m.provider ? String(m.provider) : ''; }
  function providerIsLocal(cfg, prov) { const p = cfgProviders(cfg)[prov]; return !!(p && isLoopback(p.base_url)); }
  /** "key · model id (provider)": the provider only where it adds something (the id or key does not name it). */
  function modelOptionLabel(cfg, key, withProvider) {
    const m = cfgModels(cfg)[key] || {};
    const id = m.model && m.model !== key ? String(m.model) : '';
    const prov = m.provider ? String(m.provider) : '';
    const named = (key + ' ' + id).toLowerCase().indexOf(prov.toLowerCase()) >= 0;
    const fam = cfg && isObj(cfg.model_families) && cfg.model_families[key] ? String(cfg.model_families[key]) : '';
    return key + (id ? ' · ' + id : '') + (withProvider !== false && prov && !named ? ' (' + prov + ')' : '') + (fam ? ' · ' + fam + ' family' : '');
  }
  /** LOCAL when the provider's base_url is loopback, else API (null when the provider is unknown). */
  function locBadge(cfg, key) {
    const prov = modelProvider(cfg, key);
    if (!prov) return null;
    const local = providerIsLocal(cfg, prov);
    return h('span', { class: 'tag ' + (local ? 'tag-local' : 'tag-api'), title: prov + (local ? ' runs on this machine (loopback base_url)' : ' is a hosted API') },
      icon(local ? 'server' : 'cloud', 'icon-xs'), local ? 'LOCAL' : 'API');
  }
  /** A failed Test in a few words (the chip); the whole error goes in its title. */
  function testReason(err) {
    const e = String(err || '');
    if (/is not set|does not define it|api key|missing key/i.test(e)) return 'key missing';
    if (/\b(401|403)\b|unauthori[sz]ed|forbidden|invalid.{0,12}key|authentication/i.test(e)) return 'auth failed';
    if (/cannot reach|connection refused|unreachable|timed? ?out|name or service|getaddrinfo|ECONNREFUSED/i.test(e)) return 'unreachable';
    if (/unknown model|model .{0,40}not found|\b404\b/i.test(e)) return 'unknown model';
    if (/^tool probe/i.test(e)) return 'tool call failed';
    return 'failed';
  }
  /** Each agent's route as saved (config_view.agent_list): {model, driver, extra_body}. */
  function savedRoutes(cfg) {
    const out = {};
    for (const a of cfg && Array.isArray(cfg.agent_list) ? cfg.agent_list : []) {
      if (a && a.name) out[a.name] = { model: String(a.model || ''), driver: String(a.driver || 'builtin'), extra_body: isObj(a.extra_body) ? a.extra_body : null };
    }
    for (const a of S.agents) if (a && a.name && !out[a.name]) out[a.name] = { model: String(a.model || ''), driver: String(a.driver || 'builtin'), extra_body: null };
    return out;
  }
  function factorySteps() { return ['spec-writer', 'builder'].concat(pipelineReviewers().map((r) => 'reviewer-' + r), ['approver']); }
  /** A job's routing snapshot per step: what it runs on now, from the snapshot itself (never the live config). */
  function snapshotRoutes(job) {
    const out = {};
    for (const [name, r] of Object.entries(isObj(job.routing) ? job.routing : {})) {
      if (!isObj(r)) continue;
      const pe = isObj(r.provider_entry) ? r.provider_entry : null;
      out[name] = {
        model: String(r.model || ''), driver: String(r.driver || 'builtin'), model_id: String(r.model_id || r.model || ''),
        provider: String(r.provider || ''), base_url: pe && pe.base_url ? String(pe.base_url) : null,
        local: pe ? isLoopback(pe.base_url) : providerIsLocal(CFG.orig, r.provider),
        extra_body: isObj(r.extra_body) ? r.extra_body : {},
      };
    }
    return out;
  }
  /** The saved model key that resolves to exactly a snapshot route's model (same provider, model id and
   *  base_url), its own key first; null when the live config no longer has it (e.g. a preset's tier). */
  function liveKeyFor(cfg, snap) {
    const provs = cfgProviders(cfg);
    const same = (k) => {
      const m = cfgModels(cfg)[k];
      if (!isObj(m) || String(m.model || '') !== snap.model_id || String(m.provider || '') !== snap.provider) return false;
      const p = provs[snap.provider];
      return !snap.base_url || (isObj(p) && String(p.base_url || '') === snap.base_url);
    };
    if (snap.model && same(snap.model)) return snap.model;
    return modelKeys(cfg).find(same) || null;
  }
  const KEEP = ':keep';
  /** Per-step model + driver selects; changes() gives {agent: {model?, driver?, extra_body?}} for the steps that
   *  differ from base. base holds saved routes ({model, driver}), or with opts.snapshot a job's snapshot routes
   *  (snapshotRoutes): the model select then starts on "Keep <model id> · <provider>". */
  function routeEditor(cfg, steps, base, opts) {
    const snap = !!(opts && opts.snapshot);
    const keys = modelKeys(cfg);
    const drivers = driverNames(cfg);
    const rows = [];
    const list = h('div', { class: 'rt-rows', role: 'group', 'aria-label': 'Per-step routing' });
    const tagFor = (local, title) => h('span', { class: 'tag ' + (local ? 'tag-local' : 'tag-api'), title }, icon(local ? 'server' : 'cloud', 'icon-xs'), local ? 'LOCAL' : 'API');
    for (const name of steps) {
      const b = base[name] || { model: '', driver: 'builtin' };
      const pins = keys.map((k) => [k, modelOptionLabel(cfg, k)]);
      const msel = snap
        ? selectEl([[KEEP, 'Keep ' + (b.model_id || b.model || '?') + ' · ' + (b.provider || '?')], { group: 'Re-route to a saved model', options: pins }], KEEP, { class: 'select select-sm', 'aria-label': name + ' model' })
        : selectEl(pins, b.model, { class: 'select select-sm', 'aria-label': name + ' model' });
      const dsel = selectEl(drivers.map((d) => [d, d]), b.driver || 'builtin', { class: 'select select-sm', 'aria-label': name + ' driver' });
      const badge = h('span', { class: 'rt-row-badge' });
      const warn = h('div', { class: 'rt-row-warn', role: 'status', hidden: true });
      const row = h('div', { class: 'rt-row', dataset: { agent: name } },
        h('span', { class: 'rt-row-name' }, h('span', { class: 'dot ' + agentColorCls(name) }), h('span', { class: 'rt-row-agent', title: name }, name), h('span', { class: 'rt-row-dot', title: 'Changed' })),
        msel, dsel, badge, warn);
      const r = { name, msel, dsel, b, warn };
      const paint = () => {
        const keep = snap && msel.value === KEEP;
        row.classList.toggle('changed', (snap ? !keep : msel.value !== b.model) || dsel.value !== (b.driver || 'builtin'));
        clear(badge);
        add(badge, keep ? tagFor(b.local, b.provider + (b.local ? ' runs on the server’s machine (loopback base_url)' : ' is a hosted API')) : locBadge(cfg, msel.value));
        const stuck = keep && dsel.value !== b.driver && !liveKeyFor(cfg, b);
        row.classList.toggle('invalid', stuck);
        clear(warn);
        warn.hidden = !stuck;
        if (stuck) add(warn, [icon('alert', 'icon-xs'), h('span', null, b.model_id + ' on ' + b.provider + ' is no longer one of your saved models, so the driver cannot change on its own: pick a model for this step too.')]);
      };
      msel.addEventListener('change', paint);
      dsel.addEventListener('change', paint);
      paint();
      rows.push(r);
      list.appendChild(row);
    }
    return {
      el: list,
      changes() {
        const out = {};
        const stuck = [];
        for (const r of rows) {
          const dChanged = r.dsel.value !== (r.b.driver || 'builtin');
          if (snap) {
            const keep = r.msel.value === KEEP;
            if (keep && !dChanged) continue;
            if (!keep) { out[r.name] = { model: r.msel.value, driver: r.dsel.value }; continue; }
            // A new driver on the same model: the server resolves a model key in the live config, so send the key
            // that still means the snapshot's model, plus the snapshot's extra_body (an explicit model drops the
            // step's saved extra_body otherwise). No such key: the user must pick a model.
            const key = liveKeyFor(cfg, r.b);
            if (!key) { stuck.push(r.name); continue; }
            out[r.name] = { model: key, driver: r.dsel.value };
            if (Object.keys(r.b.extra_body || {}).length) out[r.name].extra_body = clone(r.b.extra_body);
            continue;
          }
          const mChanged = r.msel.value !== r.b.model;
          if (!mChanged && !dChanged) continue;
          const e = {};
          if (mChanged && keys.indexOf(r.msel.value) >= 0) e.model = r.msel.value;
          if (dChanged) e.driver = r.dsel.value;
          out[r.name] = e;
        }
        if (stuck.length) {
          const first = rows.find((x) => x.name === stuck[0]);
          if (first) first.msel.focus();
          throw new Error(stuck.join(', ') + ': the model in the job’s snapshot is no longer one of your saved models. Pick a model for ' + (stuck.length === 1 ? 'that step' : 'those steps') + ' to change the driver.');
        }
        return out;
      },
    };
  }
  /** "Routing: Default ▾" — Default (saved settings), Saved (explicit), each preset, or Custom… with per-step selects.
   *  value(): undefined (default), null (Saved settings), a preset name, or {agent: {model?, driver?}}; throws when
   *  Custom changes nothing. */
  function routingChooser(o) {
    const id = 'rt-choice-' + (++modalSeq);
    const sel = h('select', { class: 'select', id, name: 'routing' });
    const hint = h('div', { class: 'field-hint rt-hint' });
    const box = h('div', { class: 'rt-editor', hidden: true });
    const wrap = h('div', { class: 'field rt-chooser' }, h('label', { class: 'field-label', for: id }, o.label || 'Routing'), sel, hint, box);
    let cfg = CFG.orig;
    let editor = null;
    let loadErr = null;
    let choice = o.initial || (o.includeDefault ? '' : ':custom');
    const load = () => {
      loadErr = null;
      const failed = (msg) => { loadErr = msg || 'unknown error'; fill(); show(); };
      ensureConfig().then((c) => { if (!c) { failed(CFG.loadError); return; } cfg = c; fill(); show(); }).catch((e) => failed(e && e.message));
    };
    const describe = () => {
      clear(hint);
      if (loadErr && !cfg) {
        const retry = h('button', { type: 'button', class: 'btn btn-xs', dataset: { action: 'retry-presets' } }, icon('refresh', 'icon-xs'), 'Retry');
        retry.addEventListener('click', () => { CFG.loadError = null; loadErr = null; fill(); show(); load(); });
        add(hint, [h('span', { class: 'rt-hint-err' }, icon('alert', 'icon-xs'), 'Presets and Custom are unavailable: the settings did not load (' + loadErr + ').'), ' ', o.includeDefault ? 'Default still works. ' : '', retry]);
        if (o.extraHint) add(hint, o.extraHint(choice));
        return;
      }
      if (choice === '') add(hint, 'Every step runs on its saved route (Settings › Routing).');
      else if (choice === ':saved') add(hint, 'Every step is resolved again from the saved routes (Settings › Routing).');
      else if (choice === ':custom') add(hint, o.customHint || 'Pick a model and driver per step. Steps you leave alone keep their route.');
      else if (choice.indexOf('preset:') === 0) {
        const name = choice.slice(7);
        add(hint, [h('strong', null, name), ': ', presetInfo(name) || 'examples/' + name + '.json.', ' ', o.presetHint || 'For this run only: your saved settings stay as they are.']);
      }
      if (o.extraHint) add(hint, o.extraHint(choice));
    };
    const fill = () => {
      clear(sel);
      if (o.includeDefault) sel.appendChild(h('option', { value: '' }, 'Default (saved settings)'));
      if (o.includeSaved) sel.appendChild(h('option', { value: ':saved' }, 'Saved settings'));
      if (!cfg) sel.appendChild(h('option', { value: ':loading', disabled: true }, loadErr ? 'Presets unavailable' : 'Loading presets…'));
      else {
        sel.appendChild(h('option', { value: ':custom' }, 'Custom…'));
        const presets = presetList(cfg);
        if (presets.length) {
          const og = h('optgroup', { label: 'Presets' });
          for (const p of presets) og.appendChild(h('option', { value: 'preset:' + p.name, title: p.description || '' }, presetLabel(p.name)));
          sel.appendChild(og);
        }
      }
      sel.value = choice;
      if (sel.value !== choice) { choice = o.includeDefault ? '' : sel.value; sel.value = choice; }
    };
    const show = () => {
      describe();
      const custom = choice === ':custom' && !!cfg;
      if (custom && !editor) {
        editor = routeEditor(cfg, o.steps(), o.base(cfg), { snapshot: !!o.snapshot });
        box.appendChild(h('div', { class: 'rt-editor-head', 'aria-hidden': 'true' }, h('span', null, 'Step'), h('span', null, 'Model'), h('span', null, 'Driver'), h('span', null, 'Runs')));
        box.appendChild(editor.el);
      }
      box.hidden = !custom;
      if (o.onChange) o.onChange(choice);
    };
    sel.addEventListener('change', () => { choice = sel.value; show(); });
    // A dialog's "changes no step" error goes away as soon as the routing changes.
    wrap.addEventListener('change', () => {
      const err = wrap.closest('.modal-body') && wrap.closest('.modal-body').querySelector(':scope > .form-error');
      if (err) err.hidden = true;
    });
    fill();
    show();
    if (!cfg) load();
    return {
      el: wrap, select: sel,
      choice: () => choice,
      value() {
        if (choice === '' || choice === ':loading') return undefined;
        if (choice.indexOf('preset:') === 0) return choice.slice(7);
        // Saved settings: null, which the server resolves from the saved config (Factory.set_routing), so each
        // step keeps the effort, escalation, reason and alternate Settings › Routing gives it. Each step's saved
        // model sent explicitly would drop them (Factory._resolve_route).
        if (choice === ':saved') return null;
        const changes = editor ? editor.changes() : {};
        if (!Object.keys(changes).length) { sel.focus(); throw new Error(o.emptyCustom || 'Custom routing changes no step: pick another model or driver for at least one step, or choose Default.'); }
        return changes;
      },
    };
  }
  /** What a routing chooser routes, for a form's extensions, without validating (no focus moves): undefined (the saved
   *  routing), null (Saved settings, explicitly), a preset name, or {} (per-step routes: Custom). */
  function chooserRouting(ch) {
    const c = ch.choice();
    return c.indexOf('preset:') === 0 ? c.slice(7) : c === ':custom' ? {} : c === ':saved' ? null : undefined;
  }
  function describeRouting(routing) {
    if (routing === undefined || routing === null) return '';
    if (typeof routing === 'string') return 'preset ' + routing;
    return 'custom routing for ' + plural(Object.keys(routing).length, 'step');
  }

  // ================================================================ new feature dialog
  function noRepoModal(what) {
    return openModal({
      title: 'Add a repo first',
      body: h('p', { class: 'muted' }, what + ' needs a git repository registered with the factory. Setup › Repository adds one by its absolute path on the server.'),
      actions: [{ label: 'Cancel', value: 'cancel' }, { label: 'Add a repository', value: 'ok', primary: true }],
    }).then((v) => { if (v) location.hash = '#/setup/repo'; });
  }
  function openNewFeature() {
    if (document.querySelector('dialog[open]')) return;
    if (!S.repos.length) { noRepoModal('A feature'); return; }
    const last = lsGet('sf.lastRepo');
    const def = S.filterRepo || (S.repos.some((r) => r.name === last) ? last : S.repos[0].name);
    const repoSel = selectEl(S.repos.map((r) => [r.name, r.name]), def, { id: 'nf-repo', name: 'repo' });
    const ta = h('textarea', { class: 'textarea', id: 'nf-feature', name: 'feature', rows: 7, placeholder: 'What should it do, for whom, and how will you know it works? e.g. Rate limit the signup form to 5 attempts per IP per minute and show a friendly error.', spellcheck: 'true' });
    const addRepo = h('a', { href: '#/setup/repo', class: 'field-hint' }, 'Add repo');
    const mod = /Mac|iPhone|iPad/.test(navigator.platform || '') ? '⌘' : 'Ctrl';
    let backlogBtn = null;
    let routeChoice = '';
    let delivery = null; // routingChooser reports its first choice while it is built, before this exists
    const outNote = h('div', { class: 'field-hint rt-hint-note dl-note', hidden: true }, icon('info', 'icon-xs'), h('span', null, 'Add to backlog needs the default output: backlog items always use it.'));
    const paintBacklog = () => {
      const custom = routeChoice !== '' && routeChoice !== ':loading';
      const out = !!delivery && delivery.differs();
      if (backlogBtn) {
        backlogBtn.disabled = custom || out;
        backlogBtn.title = custom ? 'Backlog items run with the saved routing: choose Default to park it' : out ? 'Backlog items use the default output: choose it to park this' : '';
      }
      outNote.hidden = !out;
    };
    const routing = routingChooser({
      includeDefault: true, steps: factorySteps, base: savedRoutes,
      customHint: 'For this job only: pick a model and driver per step. Steps you leave alone keep their saved route.',
      extraHint: (choice) => (choice !== '' && choice !== ':loading' ? h('span', { class: 'rt-hint-note' }, icon('info', 'icon-xs'), 'Add to backlog needs Default: backlog items always run on the saved routing.') : null),
      onChange: (choice) => { routeChoice = choice; paintBacklog(); },
    });
    delivery = deliveryChooser({ id: 'nf-delivery', onChange: () => paintBacklog() });
    const fx = formExt('feature', { repo: () => repoSel.value, routing: () => chooserRouting(routing), watch: [repoSel, routing.el] });
    openModal({
      title: 'New feature',
      description: 'The request goes to the spec-writer verbatim. Run it now, or park it in the repo’s backlog.',
      body: [
        h('div', { class: 'field' }, h('div', { style: { display: 'flex', 'align-items': 'baseline', 'justify-content': 'space-between' } }, h('label', { class: 'field-label', for: 'nf-repo' }, 'Repository'), addRepo), repoSel),
        field('Feature request', ta),
        routing.el,
        fx.nodes,
        delivery.el,
        outNote,
      ],
      hint: [h('kbd', null, mod), '+', h('kbd', null, 'Enter'), ' runs now'],
      actions: [{ label: 'Cancel', value: 'cancel' }, { label: 'Add to backlog', value: 'backlog', icon: 'list' }, { label: 'Run now', value: 'run', primary: true, icon: 'play' }],
      dismissable: false,
      initialFocus: ta,
      onOpen: (ctl) => {
        ta.addEventListener('keydown', (e) => { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); ctl.submit('run'); } });
        addRepo.addEventListener('click', () => ctl.close(null));
        backlogBtn = ctl.form.querySelector('button[data-action="backlog"]');
        paintBacklog();
      },
      onSubmit: async (value) => {
        const repo = repoSel.value;
        const feature = ta.value.trim();
        if (!feature) { ta.focus(); throw new Error('Describe the feature first.'); }
        const route = routing.value();
        const out = delivery.value();
        lsSet('sf.lastRepo', repo);
        if (value === 'backlog') {
          if (route !== undefined) throw new Error('Backlog items run with the saved routing. Choose Default, or use Run now.');
          if (delivery.differs()) throw new Error('Backlog items use the default output. Choose it again, or use Run now.');
          if (/\n/.test(feature)) throw new Error('Backlog items are a single line. Shorten it, or use Run now for a longer request.');
          fx.apply({}, 'backlog');
          await api('/api/repos/' + enc(repo) + '/backlog', { method: 'POST', body: { text: feature } });
          toast(['Added to the ', h('strong', null, repo), ' backlog.'], { kind: 'ok', action: { label: 'Show', onClick: () => { BL.repo = repo; setBacklogOpen(true); } } });
          if (BL.open && backlogRepo() === repo) loadBacklog();
          return 'backlog';
        }
        const since = S.lastSeq;
        const body = { repo, feature };
        if (route !== undefined) body.routing = route;
        if (out !== undefined) body.delivery = out;
        fx.apply(body, 'run');
        const r = await api('/api/jobs', { method: 'POST', body });
        const job = r && r.job;
        if (job) {
          upsertJob(job, 0, since);
          toast(['Job ', h('strong', { class: 'mono' }, job.id), ' queued' + (route !== undefined ? ' with ' + describeRouting(route) : '') + (out !== undefined ? ', output ' + out : '') + '.'], { kind: 'ok', action: { label: 'Open', href: '#/job/' + enc(job.repo) + '/' + enc(job.id) } });
        }
        return 'run';
      },
    });
  }

  // ================================================================ backlog panel
  const BL = { open: lsGet('sf.backlog') === '1', repo: '', items: null, loading: false, error: null, loadedFor: '' };
  function backlogRepo() {
    if (S.filterRepo) return S.filterRepo;
    if (BL.repo && S.repos.some((r) => r.name === BL.repo)) return BL.repo;
    return S.repos.length ? S.repos[0].name : '';
  }
  function setBacklogOpen(open) {
    BL.open = !!open;
    lsSet('sf.backlog', BL.open ? '1' : '0');
    if (S.route.view !== 'factory') location.hash = '#/factory';
    if (BL.open) loadBacklog();
    invalidate('backlog');
  }
  async function loadBacklog() {
    const repo = backlogRepo();
    if (!repo) { BL.items = []; invalidate('backlog'); return; }
    BL.loading = true;
    if (BL.loadedFor !== repo) BL.items = null;
    invalidate('backlog');
    try {
      const r = await api('/api/repos/' + enc(repo) + '/backlog');
      if (backlogRepo() !== repo) return;
      BL.items = Array.isArray(r && r.items) ? r.items : [];
      BL.loadedFor = repo;
      BL.error = null;
    } catch (e) {
      BL.error = e.message;
    } finally {
      BL.loading = false;
      invalidate('backlog');
    }
  }
  function onBacklogUpdated(repo) { if (BL.open && (!repo || repo === backlogRepo())) loadBacklog(); }
  RENDER.backlog = function () {
    const panel = document.getElementById('backlog');
    const toggle = document.getElementById('backlog-toggle');
    toggle.setAttribute('aria-expanded', BL.open ? 'true' : 'false');
    toggle.classList.toggle('btn-primary', false);
    toggle.classList.toggle('is-on', BL.open);
    panel.hidden = !BL.open;
    if (!BL.open) return;
    const repo = backlogRepo();
    if (repo && BL.loadedFor !== repo && !BL.loading) { loadBacklog(); }
    const prevInput = panel.querySelector('.backlog-add input');
    const draft = prevInput ? prevInput.value : '';
    const hadFocus = prevInput && document.activeElement === prevInput;
    clear(panel);
    const head = h('div', { class: 'backlog-head' }, icon('list', 'icon-sm'), h('h2', { class: 'section-title' }, 'Backlog'));
    if (!S.filterRepo && S.repos.length > 1) {
      const sel = selectEl(S.repos.map((r) => [r.name, r.name]), repo, { class: 'select select-sm', 'aria-label': 'Backlog repository' });
      sel.addEventListener('change', () => { BL.repo = sel.value; loadBacklog(); });
      head.appendChild(sel);
    } else if (repo) {
      head.appendChild(h('span', { class: 'tag', style: { 'margin-left': 'auto' } }, repo));
    }
    const close = h('button', { type: 'button', class: 'btn btn-ghost btn-icon btn-sm', 'aria-label': 'Close backlog', title: 'Close backlog' }, icon('x', 'icon-sm'));
    close.addEventListener('click', () => setBacklogOpen(false));
    head.appendChild(close);
    panel.appendChild(head);
    if (!repo) { panel.appendChild(emptyState('folder', 'No repos yet', 'Add a repository to keep a backlog.', [h('a', { class: 'btn btn-sm', href: '#/setup/repo' }, 'Add a repository')], true)); return; }
    const list = h('ul', { class: 'backlog-list', 'aria-label': 'Backlog items for ' + repo });
    if (BL.items === null) list.appendChild(h('li', null, skeletonLines(4)));
    else if (BL.error) list.appendChild(h('li', null, callout('err', 'alert', 'Backlog unavailable.', BL.error)));
    else if (!BL.items.length) list.appendChild(h('li', null, emptyState('inbox', 'Backlog is empty', 'Park ideas here; Run next starts the first open one.', null, true)));
    else {
      for (const it of BL.items) {
        const jobLink = it.job && /^\d{3}-/.test(String(it.job))
          ? h('a', { class: 'bl-job', href: '#/job/' + enc(repo) + '/' + enc(it.job), title: 'Open job ' + it.job }, it.job)
          : it.job ? h('span', { class: 'bl-job muted' }, String(it.job)) : null;
        list.appendChild(h('li', { class: 'backlog-item' + (it.done ? ' done' : '') },
          icon(it.done ? 'check-circle' : 'circle', 'icon-sm'),
          h('span', { class: 'bl-main' }, h('span', { class: 'bl-text' }, String(it.text || '')), jobLink),
          h('span', { class: 'sr-only' }, it.done ? '(done)' : '(open)')));
      }
    }
    panel.appendChild(list);
    const inp = h('input', { class: 'input input-sm', type: 'text', placeholder: 'Add an item (one line)', 'aria-label': 'New backlog item', maxlength: 500, value: draft });
    if (hadFocus) setTimeout(() => inp.focus(), 0);
    const addBtn = btn('Add', { cls: 'btn-sm', icon: 'plus' });
    const doAdd = async () => {
      const text = inp.value.trim();
      if (!text) { inp.focus(); return; }
      setBusy(addBtn, true);
      try {
        const r = await api('/api/repos/' + enc(repo) + '/backlog', { method: 'POST', body: { text } });
        BL.items = Array.isArray(r && r.items) ? r.items : BL.items;
        inp.value = '';
        invalidate('backlog');
        setTimeout(() => { const i = panel.querySelector('.backlog-add input'); if (i) i.focus(); }, 30);
      } catch (e) { toastError('Not added', e); } finally { setBusy(addBtn, false); }
    };
    addBtn.addEventListener('click', doAdd);
    inp.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); doAdd(); } });
    const open = (BL.items || []).filter((x) => !x.done).length;
    const next = btn('Run next', { cls: 'btn-primary btn-sm', icon: 'play', disabled: !open, title: open ? 'Start the first open item as a job' : 'No open items' });
    next.addEventListener('click', async () => {
      setBusy(next, true);
      try {
        const since = S.lastSeq;
        const r = await api('/api/repos/' + enc(repo) + '/next', { method: 'POST' });
        if (r && r.job) {
          upsertJob(r.job, 0, since);
          toast(['Started ', h('strong', { class: 'mono' }, r.job.id), '.'], { kind: 'ok', action: { label: 'Open', href: '#/job/' + enc(r.job.repo) + '/' + enc(r.job.id) } });
        }
        loadBacklog();
      } catch (e) {
        toast(e.status === 409 ? 'Nothing to run: ' + e.message : 'Run next failed: ' + e.message, { kind: 'error' });
      } finally { setBusy(next, false); }
    });
    panel.appendChild(h('div', { class: 'backlog-foot' }, h('div', { class: 'backlog-add' }, inp, addBtn),
      h('div', { style: { display: 'flex', 'align-items': 'center', gap: '8px' } }, h('span', { class: 'muted', style: { 'font-size': '12px' } }, open + ' open'), h('span', { class: 'spacer' }), next)));
  };

  // ================================================================ job drawer
  const TABS = [['live', 'Live', 'terminal'], ['files', 'Files', 'file'], ['diff', 'Diff', 'git'], ['routing', 'Routing', 'route'], ['raw', 'Raw', 'layers']];
  const D = { key: null };
  function freshDrawer(repo, id) {
    return {
      key: repo + '/' + id, repo, id, tab: 'live', files: [], filesLoaded: false, filesError: null, notFound: false,
      fileSel: null, fileText: new Map(), fileErr: new Map(), verdicts: new Map(), diff: null, diffLoading: false, diffError: null,
      diffSig: '', historyState: 'idle', transcript: null, els: {}, updTimer: 0, opener: D.opener || null, back: D.back || null,
    };
  }
  function currentJob() { return D.key ? S.jobs.get(D.key) || null : null; }
  function openDrawer(repo, id, tab) {
    const key = repo + '/' + id;
    const drawer = document.getElementById('drawer');
    if (GD.open) closePanel(false);
    if (D.key !== key) {
      if (D.transcript) D.transcript.destroy();
      clearTimeout(D.updTimer);
      const fresh = freshDrawer(repo, id);
      for (const k of Object.keys(D)) delete D[k];
      Object.assign(D, fresh);
      buildDrawerShell(drawer);
      loadJobDetail(true);
      drawer.hidden = false;
      document.getElementById('scrim').hidden = false;
      setDrawerModal(true);
      setDrawerTab(TABS.some((t) => t[0] === tab) ? tab : 'live', false);
      const title = document.getElementById('drawer-title');
      invalidate('drawerHead', 'live', 'board');
      setTimeout(() => { const t = document.getElementById('drawer-title'); if (t) t.focus({ preventScroll: true }); else if (title) title.focus(); }, 0);
    } else if (tab && tab !== D.tab && TABS.some((t) => t[0] === tab)) {
      setDrawerTab(tab, false);
    }
  }
  function hideDrawer() {
    if (!D.key) return;
    const key = D.key;
    if (D.transcript) D.transcript.destroy();
    clearTimeout(D.updTimer);
    const opener = D.opener;
    for (const k of Object.keys(D)) delete D[k];
    D.key = null;
    dropOwner(key);
    const drawer = document.getElementById('drawer');
    drawer.hidden = true;
    clear(drawer);
    document.getElementById('scrim').hidden = true;
    setDrawerModal(false);
    invalidate('board');
    const item = BOARD.cards.get(key);
    const card = item ? item._card : null;
    const target = refocusTarget(opener) || card;
    if (target && target.isConnected) target.focus({ preventScroll: true });
  }
  function closeDrawer() { location.hash = D.back || '#/factory'; }
  /** Where focus goes back to: the opener, or, when its page was re-rendered meanwhile, its twin (same data-focus). */
  function refocusTarget(opener) {
    if (!opener) return null;
    if (opener.isConnected) return opener;
    const key = opener.dataset ? opener.dataset.focus : null;
    return key ? document.querySelector('[data-focus="' + CSS.escape(key) + '"]') : null;
  }
  /** The drawer is modal (the scrim covers the app): the app behind it is inert, so Tab stays inside. */
  function setDrawerModal(on) {
    for (const el of [document.getElementById('app'), document.querySelector('.skip-link')]) {
      if (!el) continue;
      if (on) el.setAttribute('inert', ''); else el.removeAttribute('inert');
    }
  }
  function buildDrawerShell(drawer) {
    clear(drawer);
    const head = h('header', { class: 'drawer-head', id: 'drawer-head' });
    const tabs = h('div', { class: 'tabs', role: 'tablist', 'aria-label': 'Job views' });
    const body = h('div', { class: 'drawer-body' });
    const offline = h('div', { class: 'drawer-offline', id: 'drawer-offline', role: 'status', hidden: !S.offline },
      icon('alert', 'icon-sm'), h('span', null, h('strong', null, 'Reconnecting…'), ' This job may have moved on since.'),
      btn('Retry now', { cls: 'btn-xs', onClick: retryNow }));
    D.els = { head, tabs, body, offline, tabBtns: {}, panels: {} };
    for (const [id, label, ic] of TABS) {
      const b = h('button', { type: 'button', class: 'tab', role: 'tab', id: 'tab-' + id, 'aria-controls': 'panel-' + id, 'aria-selected': 'false', tabindex: '-1' }, icon(ic, 'icon-sm'), label);
      b.addEventListener('click', () => setDrawerTab(id, true));
      b.addEventListener('keydown', (e) => {
        const idx = TABS.findIndex((t) => t[0] === id);
        let nxt = null;
        if (e.key === 'ArrowRight') nxt = TABS[(idx + 1) % TABS.length][0];
        else if (e.key === 'ArrowLeft') nxt = TABS[(idx + TABS.length - 1) % TABS.length][0];
        else if (e.key === 'Home') nxt = TABS[0][0];
        else if (e.key === 'End') nxt = TABS[TABS.length - 1][0];
        if (nxt) { e.preventDefault(); setDrawerTab(nxt, true); D.els.tabBtns[nxt].focus(); }
      });
      tabs.appendChild(b);
      D.els.tabBtns[id] = b;
      const panel = h('div', { class: 'panel' + (id === 'files' ? ' panel-files' : ''), id: 'panel-' + id, role: 'tabpanel', 'aria-labelledby': 'tab-' + id, tabindex: '0', hidden: true });
      body.appendChild(panel);
      D.els.panels[id] = panel;
    }
    // Live panel: transcript + placeholder + jump pill
    const live = D.els.panels.live;
    D.els.livePlaceholder = h('div');
    D.els.liveRoot = h('div', { class: 'transcript', 'aria-live': 'off' });
    D.els.jump = h('button', { type: 'button', class: 'jump-pill', hidden: true }, icon('arrow-down', 'icon-sm'), 'Jump to latest');
    add(live, [D.els.livePlaceholder, D.els.liveRoot, D.els.jump]);
    D.transcript = new Transcript(D.els.liveRoot, live, D.els.jump, 'job');
    // Files panel
    D.els.filesList = h('nav', { class: 'files-list', 'aria-label': 'Job files' });
    D.els.fileView = h('div', { class: 'file-view' });
    add(D.els.panels.files, [D.els.filesList, D.els.fileView]);
    add(drawer, [head, offline, tabs, body]);
  }
  function setDrawerTab(tab, updateHash) {
    if (!D.key) return;
    D.tab = tab;
    for (const [id] of TABS) {
      const sel = id === tab;
      D.els.tabBtns[id].setAttribute('aria-selected', sel ? 'true' : 'false');
      D.els.tabBtns[id].tabIndex = sel ? 0 : -1;
      D.els.panels[id].hidden = !sel;
    }
    if (updateHash) {
      const h2 = '#/job/' + enc(D.repo) + '/' + enc(D.id) + (tab === 'live' ? '' : '/' + tab);
      if (location.hash !== h2) history.replaceState(null, '', h2);
    }
    if (tab === 'live') { invalidate('live'); if (D.transcript && D.transcript.stick) requestAnimationFrame(() => D.transcript && D.transcript.toBottom()); }
    else if (tab === 'files') { renderFiles(); if (D.filesLoaded) ensureFileSelected(); }
    else if (tab === 'diff') { if (!D.diff && !D.diffLoading) loadDiff(); else renderDiffTab(); }
    else if (tab === 'routing') renderRoutingTab();
    else if (tab === 'raw') renderRaw();
    else renderJobTab(tab);
  }
  function onDrawerJobUpdate(prev, job) {
    if (JOB_TABS.has(D.tab)) renderJobTab(D.tab);
    clearTimeout(D.updTimer);
    D.updTimer = setTimeout(() => {
      if (!D.key || D.key !== jobKey(job)) return;
      loadJobDetail(false);
      if (D.tab === 'diff') {
        const sig = job.stage + '|' + job.round + '|' + job.resumes;
        if (sig !== D.diffSig) loadDiff();
      }
    }, 600);
  }
  async function loadJobDetail(first) {
    const key = D.key;
    const repo = D.repo;
    const id = D.id;
    if (!key) return;
    try {
      const since = S.lastSeq;
      const r = await api(jobPath(repo, id));
      if (D.key !== key) return;
      if (r && r.job) upsertJob(r.job, 0, since);
      const prevFiles = D.files;
      D.files = Array.isArray(r && r.files) ? r.files.map(String) : [];
      D.filesLoaded = true;
      D.filesError = null;
      D.notFound = false;
      if (first) loadHistory(key, repo, id, D.files);
      else {
        const newLogs = D.files.filter((f) => /^logs\/.+\.jsonl$/.test(f) && prevFiles.indexOf(f) < 0);
        if (newLogs.length && D.historyState === 'done') loadHistory(key, repo, id, newLogs);
      }
      refreshFileTexts(first);
    } catch (e) {
      if (D.key !== key) return;
      D.filesError = e.message;
      D.notFound = e.status === 404;
      if (first) D.historyState = 'done';
    } finally {
      if (D.key === key) { invalidate('drawerHead', 'live'); renderFiles(); }
    }
  }
  async function fetchLog(repo, id, file) {
    const out = [];
    let offset = 0;
    for (let page = 0; page < 200; page++) {
      const r = await api(jobPath(repo, id) + '/log?file=' + enc(file) + '&offset=' + offset);
      const lines = Array.isArray(r && r.lines) ? r.lines : [];
      for (const ln of lines) {
        let ev = ln;
        if (typeof ln === 'string') { try { ev = JSON.parse(ln); } catch (e) { continue; } }
        if (ev && typeof ev === 'object') out.push(ev);
      }
      const next = Number(r && r.offset);
      // An empty page with more=true means the server skipped an oversize line: keep going.
      if ((!lines.length && !(r && r.more)) || !isFinite(next) || next <= offset) break;
      offset = next;
    }
    return out;
  }
  async function loadHistory(key, repo, id, files) {
    const logs = files.filter((f) => /^logs\/.+\.jsonl$/.test(f));
    if (!logs.length) { if (D.key === key) { D.historyState = 'done'; invalidate('live'); } return; }
    if (D.key === key && D.historyState !== 'done') D.historyState = 'loading';
    invalidate('live');
    const queue = logs.slice();
    const worker = async () => {
      while (queue.length) {
        const f = queue.shift();
        let events;
        try { events = await fetchLog(repo, id, f); } catch (e) { continue; }
        if (D.key !== key) return;
        const stem = f.replace(/^logs\//, '').replace(/\.jsonl$/, '');
        for (const ev of events) {
          if (!ev.repo) ev.repo = repo;
          if (!ev.job) ev.job = id;
          if (!isObj(ev.data)) ev.data = {};
          if (stem !== 'events' && ev.type && ev.type.indexOf('agent.') === 0 && !ev.data.run) ev.data.run = stem;
        }
        if (stem === 'events') {
          for (const ev of events) if (ev.type === 'log' && !ev.data.run) addJobLog(ev);
          const agentEvents = events.filter((ev) => ev.type && ev.type.indexOf('agent.') === 0 && ev.data.run);
          if (agentEvents.length) ingestBulk(agentEvents);
        } else {
          ingestBulk(events);
        }
        if (D.key === key) invalidate('live');
      }
    };
    await Promise.all([worker(), worker(), worker()]);
    if (D.key === key) { D.historyState = 'done'; invalidate('live'); }
  }
  RENDER.live = function () {
    if (!D.key || !D.transcript) return;
    const owner = D.key;
    const blocks = [];
    const runs = S.runsByOwner.get(owner);
    if (runs) for (const run of runs.values()) blocks.push({ key: run.key, kind: 'run', run, t: parseTs(run.startTs), no: run.no, seq: isFinite(run.firstSeq) ? run.firstSeq : 1e15 });
    const logs = S.jobLogs.get(owner);
    if (logs) for (const [k, ev] of logs) blocks.push({ key: 'log:' + k, kind: 'log', ev, t: parseTs(ev.ts), no: null, seq: Number(ev.seq) || 0 });
    // Event seqs restart with every server start, so order by time first, then by the run number in the
    // log name (NN-agent), and only then by seq.
    blocks.sort((a, b) => {
      if (isFinite(a.t) && isFinite(b.t) && a.t !== b.t) return a.t - b.t;
      if (a.no !== null && b.no !== null && a.no !== b.no) return a.no - b.no;
      return a.seq - b.seq || String(a.key).localeCompare(String(b.key));
    });
    const ph = D.els.livePlaceholder;
    clear(ph);
    if (!blocks.length) {
      if (D.historyState === 'loading' || D.historyState === 'idle') ph.appendChild(h('div', { class: 'transcript' }, [0, 1, 2].map(() => h('div', { class: 'skel skel-card', style: { height: '48px' } }))));
      else if (D.notFound) ph.appendChild(emptyState('alert', 'Job not found', 'It may have been removed.', null, true));
      else {
        const job = currentJob();
        ph.appendChild(emptyState('terminal', job && job.stage === 'queued' ? 'Waiting in the queue' : 'No agent runs yet',
          'Agent transcripts stream here live: text, reasoning, tool calls and their output, grouped by run.', null, true));
      }
    }
    D.transcript.sync(blocks);
    const count = D.els.tabBtns.live.querySelector('.count-pill');
    const n = runs ? runs.size : 0;
    if (n) {
      if (count) count.textContent = String(n);
      else D.els.tabBtns.live.appendChild(h('span', { class: 'count-pill' }, String(n)));
    }
  };

  // ---------------------------------------------------------------- drawer header + actions
  /** The decisions first (Approve or Retry, then Rework), Cancel/Reject, and Change routing last: it only prepares
   *  the next Retry or Rework (POST …/routing does not resume the job), so it is a secondary action. */
  function actionsFor(job) {
    const s = job.stage;
    const a = [];
    if (s === 'needs-human' || s === 'delivered' || (s === 'failed' && job.failed_step === 'merge')) a.push('approve');
    if (s === 'failed') a.push('retry');
    if (s === 'needs-human') a.push('rework');
    if (ACTIVE.has(s)) a.push('cancel');
    if (s === 'needs-human' || s === 'failed' || s === 'cancelled') a.push('reject');
    if (canChangeRouting(job)) a.push('routing');
    return a;
  }
  /** Change routing: stopped jobs (needs-human, failed) that carry a routing snapshot (DESIGN §23). */
  function canChangeRouting(job) {
    return !!job && (job.stage === 'needs-human' || job.stage === 'failed') && isObj(job.routing) && Object.keys(job.routing).length > 0;
  }
  const ACTION_UI = {
    approve: { label: 'Approve', icon: 'check', cls: 'btn-ok-solid' },
    rework: { label: 'Rework', icon: 'rework', cls: '' },
    routing: { label: 'Change routing', icon: 'route', cls: 'btn-ghost act-secondary' },
    retry: { label: 'Retry', icon: 'refresh', cls: 'btn-primary' },
    cancel: { label: 'Cancel job', icon: 'stop', cls: 'btn-danger' },
    reject: { label: 'Reject', icon: 'ban', cls: 'btn-danger' },
  };
  async function postAction(job, action, body) {
    const since = S.lastSeq;
    const r = await api(jobPath(job.repo, job.id) + '/' + action, { method: 'POST', body: body || {} });
    if (r && r.job) upsertJob(r.job, 0, since);
    return r;
  }
  function actionError(e) { toast((e.status === 409 ? 'Not allowed now: ' : e.status === 400 ? 'Invalid request: ' : 'Action failed: ') + e.message, { kind: 'error' }); }
  async function doAction(job, action, button) {
    const base = job.base || 'the base branch';
    const done = (msg) => toast(msg, { kind: 'ok' });
    const wrap = (fn) => async (v, ctl) => { try { return await fn(v, ctl); } catch (e) { actionError(e); throw e; } };
    if (action === 'approve') {
      const human = job.stage === 'needs-human' || job.stage === 'delivered';
      // A view module's fields (form 'approve': the Housekeeping Tracker's must-do items, sent as carry_items). Their
      // checks run first: what they refuse is shown in the dialog, and nothing is posted.
      const fx = formExt('approve', { job });
      const fxBody = fx.nodes.length ? fx.nodes : null;
      // A refusal a field explains in plain words (a 409 naming must-do items: its list is read again) is shown in
      // the dialog in those words; any other is the server's, as a toast too.
      const approve = (fn) => async (v, ctl) => {
        const body = fx.apply({}, v);
        try { return await fn(body, ctl); }
        catch (e) { const plain = fx.error(e, v); if (plain) throw new Error(plain); actionError(e); throw e; }
      };
      if (job.stage === 'delivered') {
        await confirmModal({
          title: 'Merge ' + job.id + ' now?',
          description: [h('span', { class: 'mono' }, job.branch || 'the job branch'), ' merges into ', h('span', { class: 'mono' }, base), '. It was approved and ' + DELIVERY_UI[jobDelivery(job) || 'branch'].text +
            '; merging it now is your decision, so the gate’s path and size rules are skipped. The merge still requires no conflicts.'],
          confirm: 'Merge it now', cls: 'btn-ok-solid', icon: 'merge', body: fxBody,
          onSubmit: approve(async (body) => { await postAction(job, 'approve', body); done('Merging ' + job.id + '…'); }),
        });
        return;
      }
      // A branch or folder job with nothing delivered yet is delivered, not merged (orchestrator.approve): base stays.
      const mode = jobDelivery(job) || 'merge';
      if (mode !== 'merge' && !job.delivered) {
        await confirmModal({
          title: 'Approve and deliver ' + job.id + '?',
          description: ['Its output is ', h('strong', null, mode === 'folder' ? 'a folder' : 'its branch'), ': approving delivers it as the job asked, so it is ' + DELIVERY_UI[mode].text +
            (mode === 'folder' ? ' (changes.patch, the changed files and REPORT.md)' : '') + '. Nothing merges and ', h('span', { class: 'mono' }, base),
            ' does not move. This is your decision, so the review rule is skipped; Merge it now on the delivered job merges it later.'],
          confirm: 'Approve & deliver', cls: 'btn-ok-solid', icon: DELIVERY_UI[mode].icon, body: fxBody,
          onSubmit: approve(async (body) => { await postAction(job, 'approve', body); done('Approved ' + job.id + ': delivering as a ' + mode + '…'); }),
        });
        return;
      }
      await confirmModal({
        title: 'Approve and merge ' + job.id + '?',
        description: [h('span', { class: 'mono' }, job.branch || 'the job branch'), ' merges into ', h('span', { class: 'mono' }, base), '. ' +
          (human ? 'This is a human override: the gate’s review, path and size rules are skipped, but the merge still requires no conflicts.' : 'The merge still requires no conflicts.')],
        confirm: 'Approve & merge', cls: 'btn-ok-solid', icon: 'merge', body: fxBody,
        onSubmit: approve(async (body) => { await postAction(job, 'approve', body); done('Approved ' + job.id + '. Merging…'); }),
      });
    } else if (action === 'rework') {
      const ta = h('textarea', { class: 'textarea', rows: 6, placeholder: 'What should the builder change? Be specific: file, behaviour, acceptance check.' });
      await openModal({
        title: 'Send ' + job.id + ' back for rework',
        description: 'The builder picks up your note and changes the branch, then the reviewers check it in a fresh round.',
        body: field('Rework note', ta), dismissable: false, initialFocus: ta,
        actions: [{ label: 'Cancel', value: 'cancel' }, { label: 'Send to builder', value: 'ok', primary: true, icon: 'rework' }],
        onOpen: (ctl) => ta.addEventListener('keydown', (e) => { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); ctl.submit('ok'); } }),
        onSubmit: wrap(async () => {
          const note = ta.value.trim();
          if (!note) { ta.focus(); const err = new Error('Write a note for the builder.'); err.status = -1; throw err; }
          await postAction(job, 'rework', { note });
          done('Sent ' + job.id + ' back to the builder.');
        }),
      });
    } else if (action === 'reject') {
      const inp = h('input', { class: 'input', type: 'text', placeholder: 'Optional: why (kept in job.json)', maxlength: 500 });
      await openModal({
        title: 'Reject ' + job.id + '?',
        description: 'This removes the worktree and force-deletes branch ' + (job.branch || '') + '. The job folder (spec, reviews, logs) is kept. This cannot be undone.',
        body: field('Reason', inp), dismissable: false,
        actions: [{ label: 'Cancel', value: 'cancel' }, { label: 'Reject job', value: 'ok', cls: 'btn-danger-solid', icon: 'ban' }],
        onSubmit: wrap(async () => { await postAction(job, 'reject', { reason: inp.value.trim() }); done('Rejected ' + job.id + '.'); }),
      });
    } else if (action === 'cancel') {
      await confirmModal({
        title: 'Cancel ' + job.id + '?',
        description: 'Running agents stop at their next check and the job moves to Cancelled. The worktree is kept until you reject it.',
        confirm: 'Cancel job', cls: 'btn-danger-solid', icon: 'stop',
        onSubmit: wrap(async () => { await postAction(job, 'cancel'); done('Cancelling ' + job.id + '…'); }),
      });
    } else if (action === 'routing') {
      setBusy(button, true);
      let cfg = null;
      try { cfg = await ensureConfig(); } catch (e) { /* shown below */ } finally { setBusy(button, false); }
      if (!cfg) { toast('Cannot load the settings: ' + (CFG.loadError || 'unknown error'), { kind: 'error' }); return; }
      const steps = () => { const own = Object.keys(job.routing || {}); const order = factoryStepsOf(job); return order.filter((n) => own.indexOf(n) >= 0).concat(own.filter((n) => order.indexOf(n) < 0)); };
      const chooser = routingChooser({
        includeDefault: false, includeSaved: true, initial: ':custom', steps, base: () => snapshotRoutes(job), snapshot: true, label: 'New routing',
        customHint: 'Each step starts on what it runs on now (the job’s snapshot). A step you re-route is resolved again from your saved models; the others keep their snapshot, unless you pick a routing profile below.',
        presetHint: 'Every step is re-routed from the preset.',
        emptyCustom: 'Custom routing changes no step: pick another model or driver for at least one step, pick a routing profile, or choose Saved settings.',
      });
      const failed = job.stage === 'failed';
      const next = failed ? 'retry' : 'rework';
      const fx = formExt('routing', { job, repo: () => job.repo, routing: () => chooserRouting(chooser), watch: [chooser.el] });
      const choice = await openModal({
        title: 'Change routing of ' + job.id,
        description: [h('strong', null, 'Two steps: '), 'this rewrites the job’s routing snapshot only; the job stays ' + (failed ? 'failed' : 'in Needs human') +
          ' until you ' + (failed ? 'Retry' : 'Rework') + ' it, and that run uses the new routing. Your saved settings stay as they are.'],
        body: [chooser.el, fx.nodes], wide: true,
        actions: [{ label: 'Cancel', value: 'cancel' }, { label: 'Change only', value: 'ok', icon: 'route' },
          { label: failed ? 'Change & retry' : 'Change & rework…', value: next, primary: true, icon: failed ? 'refresh' : 'rework' }],
        onSubmit: (v, ctl) => {
          let routing;
          try { routing = chooser.value(); }  // a local validation error shows in the dialog only
          catch (e) { if (chooser.choice() !== ':custom') throw e; routing = {}; }   // Custom may change only what a form extension adds
          const rbody = fx.apply({ routing }, v);
          if (isObj(rbody.routing) && !Object.keys(rbody.routing).length) chooser.value();   // nothing changes: the chooser's own error
          routing = rbody.routing;
          const what = (routing === null ? 'your saved settings' : describeRouting(routing)) +
            (rbody.profile === undefined ? '' : rbody.profile ? ', profile ' + rbody.profile : ', no profile');
          return wrap(async () => {
            await postAction(job, 'routing', rbody);
            if (v === 'retry') {
              try { await postAction(job, 'retry'); } catch (e) { toast('Routing changed, but Retry failed: ' + e.message, { kind: 'error' }); return 'ok'; }
              done('Routing changed (' + what + '); retrying ' + job.id + (job.failed_step ? ' from ' + job.failed_step : '') + '.');
              return v;
            }
            if (v === 'ok') {
              toast('Routing of ' + job.id + ' changed: ' + what + '. ' + (failed ? 'Retry' : 'Rework') + ' runs on it.',
                { kind: 'ok', action: { label: failed ? 'Retry now' : 'Rework…', onClick: () => { const j = S.jobs.get(jobKey(job)); if (j && j.stage === job.stage) doAction(j, next, null); } } });
            }
            return v;
          })(v, ctl);
        },
      });
      // Change & rework: the rework note is the second dialog.
      if (choice === 'rework') { const j = S.jobs.get(jobKey(job)) || job; if (j.stage === 'needs-human') await doAction(j, 'rework', null); }
    } else if (action === 'retry') {
      setBusy(button, true);
      try { await postAction(job, 'retry'); done('Retrying ' + job.id + (job.failed_step ? ' from ' + job.failed_step : '') + '.'); }
      catch (e) { actionError(e); }
      finally { setBusy(button, false); }
    }
  }
  /** The order a job runs its steps in: spec-writer, builder, its reviewers, approver. */
  function factoryStepsOf(job) {
    const revs = Array.isArray(job.reviewers) && job.reviewers.length ? job.reviewers : pipelineReviewers();
    return ['spec-writer', 'builder'].concat(revs.map((r) => 'reviewer-' + r), ['approver']);
  }
  /** The steps of a job's routing snapshot in line order, with how many run locally. */
  function routingSummary(job) {
    const routes = isObj(job && job.routing) ? job.routing : {};
    const order = factoryStepsOf(job);
    const names = order.filter((n) => routes[n]).concat(Object.keys(routes).filter((n) => order.indexOf(n) < 0));
    const snaps = snapshotRoutes(job);
    const local = names.filter((n) => snaps[n] && snaps[n].local).length;
    return { names, snaps, local, text: plural(names.length, 'step') + ' · ' + local + ' local · ' + (names.length - local) + ' API' };
  }
  /** Drawer tab: which model, provider and driver each step runs on (the job.routing snapshot). */
  function renderRoutingTab() {
    if (!D.key || !D.els || !D.els.panels) return;
    const panel = D.els.panels.routing;
    const job = currentJob();
    const focusedAction = document.activeElement && panel.contains(document.activeElement) ? document.activeElement.dataset.action : null;
    clear(panel);
    if (!job) { panel.appendChild(D.notFound ? emptyState('alert', 'Job not found', 'It may have been removed.', null, true) : skeletonLines(5)); return; }
    if (!isObj(job.routing) || !Object.keys(job.routing).length) {
      panel.appendChild(emptyState('route', 'No routing snapshot', 'This job was submitted before routing snapshots: its steps run on the saved routes (Settings › Routing).', null, true));
      return;
    }
    const sum = routingSummary(job);
    const rows = sum.names.map((name) => {
      const r = isObj(job.routing[name]) ? job.routing[name] : {};
      const s = sum.snaps[name];
      const extra = isObj(r.extra_body) && Object.keys(r.extra_body).length ? JSON.stringify(r.extra_body) : '';
      return h('tr', { dataset: { agent: name } },
        h('td', { class: 'rs-step' }, h('span', { class: 'dot ' + agentColorCls(name) }), name),
        h('td', { class: 'rs-model' }, h('span', { class: 'mono rs-id', title: r.model && r.model !== r.model_id ? 'model entry ' + r.model : '' }, s.model_id || '?'),
          r.model && r.model_id && r.model !== r.model_id ? h('span', { class: 'rs-key' }, r.model) : null),
        h('td', { class: 'rs-prov' }, h('span', { class: 'rs-prov-name' }, s.provider || '?'),
          h('span', { class: 'tag ' + (s.local ? 'tag-local' : 'tag-api'), title: s.base_url || '' }, s.local ? 'LOCAL' : 'API')),
        h('td', { class: 'rs-driver' }, h('span', { class: 'rs-driver-name' }, s.driver),
          extra ? h('span', { class: 'tag tag-outline rs-extra', title: 'extra_body: ' + extra, tabindex: '0', 'aria-label': 'extra_body ' + extra }, '+extra_body') : null));
    });
    const head = h('div', { class: 'rs-head' },
      h('div', null, h('h3', { class: 'rs-title' }, 'Routing snapshot'), h('p', { class: 'rs-sub' }, sum.text)),
      h('span', { class: 'spacer' }));
    if (canChangeRouting(job)) {
      const b = btn('Change routing', { cls: 'btn-sm', icon: 'route', dataset: { action: 'routing' }, title: 'Pick other models or drivers for the next ' + (job.stage === 'failed' ? 'Retry' : 'Rework') });
      b.addEventListener('click', () => doAction(job, 'routing', b));
      head.appendChild(b);
    }
    add(panel, [head,
      h('table', { class: 'table route-table' }, h('thead', null, h('tr', null, ['Step', 'Model', 'Provider', 'Driver'].map((t) => h('th', null, t)))), h('tbody', null, rows)),
      h('p', { class: 'route-sum-note' }, icon('info', 'icon-xs'), 'Taken when the job was submitted: the job keeps it whatever the settings say now. ' +
        (canChangeRouting(job) ? 'Change routing rewrites it for the next ' + (job.stage === 'failed' ? 'Retry.' : 'Rework.') : 'It can change while the job is failed or needs you.'))]);
    if (focusedAction) { const el = panel.querySelector('[data-action="' + CSS.escape(focusedAction) + '"]'); if (el) el.focus({ preventScroll: true }); }
  }
  RENDER.drawerHead = function () {
    if (!D.key || !D.els || !D.els.head) return;
    const head = D.els.head;
    const focusedAction = document.activeElement && head.contains(document.activeElement) ? document.activeElement.dataset.action : null;
    clear(head);
    const job = currentJob();
    const closeBtn = h('button', { type: 'button', class: 'btn btn-ghost btn-icon drawer-close', 'aria-label': 'Close job panel', title: 'Close (Esc)', dataset: { action: 'close' } }, icon('x'));
    closeBtn.addEventListener('click', closeDrawer);
    if (!job) {
      head.appendChild(h('div', { class: 'drawer-row' }, h('span', { class: 'drawer-id' }, D.id), h('span', { class: 'tag' }, D.repo), closeBtn));
      head.appendChild(h('h2', { class: 'drawer-title', id: 'drawer-title', tabindex: '-1' }, D.notFound ? 'Job not found' : 'Loading…'));
      if (D.filesError && !D.notFound) head.appendChild(callout('err', 'alert', 'Could not load the job.', D.filesError));
      return;
    }
    const copyId = h('button', { type: 'button', class: 'btn btn-ghost btn-icon btn-xs', title: 'Copy job id', 'aria-label': 'Copy job id', dataset: { action: 'copy-id' } }, icon('copy', 'icon-xs'));
    copyId.addEventListener('click', () => copyText(job.repo + '/' + job.id, 'Job id copied'));
    head.appendChild(h('div', { class: 'drawer-row' }, stagePill(job.stage), h('span', { class: 'drawer-id' }, job.id), copyId, h('span', { class: 'tag' }, job.repo), timeEl(job.updated_at, 'muted'), closeBtn));
    head.appendChild(h('h2', { class: 'drawer-title', id: 'drawer-title', tabindex: '-1', title: job.feature || '' }, featureTitle(job) || '(no feature text)'));
    const meta = h('div', { class: 'meta-row' });
    if (job.branch) meta.appendChild(h('span', { class: 'meta-item', title: 'Branch' }, icon('branch', 'icon-xs'), h('span', { class: 'mono' }, job.branch), job.base ? h('span', null, '→ ' + job.base) : null));
    if (job.worktree) {
      const cp = h('button', { type: 'button', class: 'btn btn-ghost btn-icon btn-xs', title: 'Copy worktree path', 'aria-label': 'Copy worktree path', dataset: { action: 'copy-wt' } }, icon('copy', 'icon-xs'));
      cp.addEventListener('click', () => copyText(job.worktree, 'Worktree path copied'));
      meta.appendChild(h('span', { class: 'meta-item meta-extra', title: job.worktree }, icon('folder', 'icon-xs'), h('span', { class: 'mono' }, job.worktree), cp));
    }
    if (Number(job.round) > 0 || Number(job.resumes) > 0) meta.appendChild(h('span', { class: 'meta-item' }, icon('rework', 'icon-xs'), 'round ' + (job.round || 0) + ' · resumes ' + (job.resumes || 0) + '/' + (job.max_resumes !== undefined ? job.max_resumes : '?')));
    const u = fmtUsage(job.usage, true);
    if (u) meta.appendChild(h('span', { class: 'meta-item meta-extra', title: 'Token usage across all agent runs' }, icon('zap', 'icon-xs'), u));
    if (job.port) meta.appendChild(h('span', { class: 'meta-item meta-extra', title: 'Builder dev-server port' }, 'port ' + job.port));
    if (job.merged_commit) meta.appendChild(h('span', { class: 'meta-item meta-extra', title: 'Merge commit' }, icon('merge', 'icon-xs'), h('span', { class: 'mono' }, String(job.merged_commit).slice(0, 10))));
    const planId = planOfJob(job);
    if (planId) meta.appendChild(h('a', { class: 'meta-item meta-link', href: '#/plans/' + enc(job.repo) + '/' + enc(planId), title: 'Open the plan this job belongs to' }, icon('plan', 'icon-xs'), h('span', { class: 'mono' }, planId)));
    const mode = jobDelivery(job);
    if (mode && job.stage !== 'delivered') meta.appendChild(h('span', { class: 'meta-item', dataset: { delivery: mode }, title: 'Output: after APPROVE the work is ' + DELIVERY_UI[mode].text }, icon(DELIVERY_UI[mode].icon, 'icon-xs'), 'output: ' + mode));
    for (const n of extNodes('jobMeta', job)) meta.appendChild(n);
    head.appendChild(meta);
    for (const n of extNodes('jobHead', job)) head.appendChild(n);
    if (job.stage === 'delivered') head.appendChild(deliveredBox(job));
    if (isObj(job.push)) head.appendChild(pushBox(job));
    if (isObj(job.routing) && Object.keys(job.routing).length) {
      const rb = h('button', { type: 'button', class: 'meta-item meta-link meta-btn', dataset: { action: 'show-routing' }, title: 'Which model, provider and driver each step runs on' },
        icon('route', 'icon-xs'), h('span', null, 'Routing: ' + routingSummary(job).text));
      rb.addEventListener('click', () => { setDrawerTab('routing', true); D.els.tabBtns.routing.focus(); });
      meta.appendChild(rb);
    }
    const wait = ACTIVE.has(job.stage) ? waitingOf(D.key) : null;
    if (wait) head.appendChild(h('div', { class: 'meta-row wait-row', role: 'status', title: waitWords(wait).long }, icon('hourglass', 'icon-sm'), (wait.agent || 'An agent') + ' is ' + waitWords(wait).short + '…'));
    if (job.detail && ACTIVE.has(job.stage)) head.appendChild(h('div', { class: 'meta-row' }, h('span', { class: 'spinner', 'aria-hidden': 'true' }), job.detail));
    const reason = job.reason || job.error;
    if (reason && (job.stage === 'needs-human' || job.stage === 'failed')) {
      head.appendChild(callout(job.stage === 'failed' ? 'err' : 'human', job.stage === 'failed' ? 'error' : 'user',
        job.stage === 'failed' ? 'Failed' + (job.failed_step ? ' at ' + job.failed_step : '') + ':' : 'Needs you:', String(reason)));
    } else if (reason && job.stage === 'rejected') {
      head.appendChild(callout('info', 'info', 'Rejected:', String(reason)));
    }
    const acts = actionsFor(job);
    if (acts.length) {
      const row = h('div', { class: 'actions' });
      for (const a of acts) {
        const deliverMode = a === 'approve' && job.stage !== 'delivered' && !job.delivered && jobDelivery(job) && jobDelivery(job) !== 'merge' ? jobDelivery(job) : null;
        const ui = a === 'approve' && job.stage === 'delivered' ? { label: 'Merge it now', icon: 'merge', cls: 'btn-ok-solid' }
          : deliverMode ? { label: 'Approve & deliver', icon: DELIVERY_UI[deliverMode].icon, cls: 'btn-ok-solid' } : ACTION_UI[a];
        const b = btn(ui.label, { cls: ui.cls + ' btn-sm', icon: ui.icon, dataset: { action: a } });
        b.addEventListener('click', () => doAction(job, a, b));
        row.appendChild(b);
      }
      head.appendChild(row);
    }
    if (focusedAction) { const el = head.querySelector('[data-action="' + focusedAction + '"]'); if (el) el.focus({ preventScroll: true }); }
    if (D.tab === 'routing') renderRoutingTab();
  };

  // ---------------------------------------------------------------- files tab
  function fileGroups(files) {
    const groups = new Map();
    const put = (g, f) => { if (!groups.has(g)) groups.set(g, []); groups.get(g).push(f); };
    for (const f of files) {
      if (f === 'job.json') continue;
      if (/^logs\//.test(f)) { put('Logs', f); continue; }
      const rm = /^round-(\d+)\//.exec(f);
      if (rm) { put('Round ' + rm[1], f); continue; }
      if (f === 'decision.md') { put('Decision', f); continue; }
      if (f === 'feature.md' || f === 'spec.md' || f === 'build.md' || /^rework-\d+\.md$/.test(f)) { put('Job', f); continue; }
      put('Other', f);
    }
    const prio = (f) => (f === 'feature.md' ? 0 : f === 'spec.md' ? 1 : f === 'build.md' ? 2 : 3);
    const jobFiles = groups.get('Job');
    if (jobFiles) jobFiles.sort((a, b) => prio(a) - prio(b) || a.localeCompare(b, undefined, { numeric: true }));
    const names = Array.from(groups.keys()).sort((a, b) => {
      const rank = (g) => (g === 'Job' ? 0 : /^Round /.test(g) ? 1 : g === 'Decision' ? 2 : g === 'Other' ? 3 : 4);
      return rank(a) - rank(b) || a.localeCompare(b, undefined, { numeric: true });
    });
    return names.map((n) => [n, groups.get(n)]);
  }
  function defaultFile() {
    const job = currentJob();
    const f = D.files;
    if (f.indexOf('decision.md') >= 0 && job && (job.stage === 'needs-human' || job.stage === 'merged' || job.stage === 'approve')) return 'decision.md';
    for (const c of ['spec.md', 'feature.md', 'build.md']) if (f.indexOf(c) >= 0) return c;
    return f.find((x) => x !== 'job.json' && !/^logs\//.test(x)) || null;
  }
  function ensureFileSelected() {
    if (!D.fileSel || D.files.indexOf(D.fileSel) < 0) D.fileSel = defaultFile();
    if (D.fileSel && !D.fileText.has(D.fileSel)) loadFileText(D.fileSel);
    renderFiles();
  }
  async function loadFileText(path) {
    const key = D.key;
    if (!key) return;   // the drawer closed while earlier files loaded
    try {
      const text = await api(jobPath(D.repo, D.id) + '/file?path=' + enc(path), { text: true });
      if (D.key !== key) return;
      D.fileText.set(path, String(text));
      D.fileErr.delete(path);
      if (/\.md$/.test(path)) D.verdicts.set(path, firstLineVerdict(text));
    } catch (e) {
      if (D.key !== key) return;
      D.fileErr.set(path, e.message);
    } finally {
      if (D.key === key) renderFiles();
    }
  }
  function refreshFileTexts(first) {
    // Verdict badges: every markdown file; on updates re-read the files agents rewrite plus the open file.
    const mds = D.files.filter((f) => /\.md$/.test(f));
    const want = mds.filter((f) => first || !D.fileText.has(f) || f === 'build.md' || f === 'decision.md' || f === 'spec.md' || f === D.fileSel);
    if (!first && D.fileSel && !/\.md$/.test(D.fileSel) && D.files.indexOf(D.fileSel) >= 0) want.push(D.fileSel);
    let i = 0;
    const key = D.key;
    // Three at a time, and only for the job still open: a closed or switched drawer stops the chain.
    const next = () => { if (i < want.length && D.key === key) { const f = want[i++]; return loadFileText(f).then(next); } return null; };
    next(); next(); next();
    if (D.tab === 'files') ensureFileSelected();
  }
  function renderFiles() {
    if (!D.key || !D.els.filesList) return;
    const list = D.els.filesList;
    const view = D.els.fileView;
    clear(list);
    if (!D.filesLoaded) {
      list.appendChild(skeletonLines(6));
      clear(view).appendChild(skeletonLines(10));
      return;
    }
    if (D.filesError && !D.files.length) {
      clear(view).appendChild(callout('err', 'alert', 'Could not list files.', D.filesError));
      return;
    }
    const groups = fileGroups(D.files);
    if (!groups.length) {
      clear(view).appendChild(emptyState('file', 'No files yet', 'spec.md, build.md, reviews and decision.md appear here as agents write them.', null, true));
      return;
    }
    if (!D.fileSel || D.files.indexOf(D.fileSel) < 0) D.fileSel = defaultFile();
    // Phones get a native picker above the file instead of the side list (one scroll area, not two).
    const picker = h('select', { class: 'select files-picker', 'aria-label': 'Job file' });
    const choose = (f) => {
      D.fileSel = f;
      if (!D.fileText.has(f)) loadFileText(f);
      renderFiles();
    };
    for (const [name, files] of groups) {
      const og = h('optgroup', { label: name });
      for (const f of files) {
        const v = D.verdicts.get(f);
        og.appendChild(h('option', { value: f }, f + (v ? '  ·  ' + v : '')));
      }
      picker.appendChild(og);
    }
    picker.value = D.fileSel || '';
    picker.addEventListener('change', () => choose(picker.value));
    for (const [name, files] of groups) {
      const g = h('div', { class: 'files-group' }, h('div', { class: 'files-group-title' }, name));
      for (const f of files) {
        const label = name.indexOf('Round') === 0 ? f.replace(/^round-\d+\//, '') : name === 'Logs' ? f.replace(/^logs\//, '') : f;
        const b = h('button', { type: 'button', class: 'file-btn', 'aria-current': D.fileSel === f ? 'true' : 'false', title: f, dataset: { file: f } },
          icon(/\.jsonl$/.test(f) ? 'terminal' : 'file', 'icon-sm'), h('span', { class: 'file-name' }, label), verdictTag(D.verdicts.get(f)));
        b.addEventListener('click', () => choose(f));
        g.appendChild(b);
      }
      list.appendChild(g);
    }
    const pickerFocused = document.activeElement && document.activeElement.classList.contains('files-picker');
    clear(view);
    view.appendChild(picker);
    if (pickerFocused) picker.focus({ preventScroll: true });
    const sel = D.fileSel;
    if (!sel) return;
    const text = D.fileText.get(sel);
    const reload = h('button', { type: 'button', class: 'btn btn-ghost btn-icon btn-xs', title: 'Reload file', 'aria-label': 'Reload file' }, icon('refresh', 'icon-xs'));
    reload.addEventListener('click', () => loadFileText(sel));
    const copy = h('button', { type: 'button', class: 'btn btn-ghost btn-icon btn-xs', title: 'Copy file contents', 'aria-label': 'Copy file contents', disabled: text === undefined }, icon('copy', 'icon-xs'));
    copy.addEventListener('click', () => copyText(text || '', 'File copied'));
    view.appendChild(h('div', { class: 'file-view-head' }, icon('file', 'icon-sm'), h('span', { class: 'file-view-path' }, sel), verdictTag(D.verdicts.get(sel)), h('span', { class: 'spacer' }), reload, copy));
    if (D.fileErr.has(sel) && text === undefined) { view.appendChild(callout('err', 'alert', 'Could not load ' + sel + '.', D.fileErr.get(sel))); return; }
    if (text === undefined) { view.appendChild(skeletonLines(12)); return; }
    if (/\.md$/.test(sel)) view.appendChild(MD.render(text));
    else view.appendChild(h('pre', { class: 'code-block', style: { 'max-height': 'none' } }, text || '(empty file)'));
  }

  // ---------------------------------------------------------------- diff + raw tabs
  async function loadDiff() {
    const key = D.key;
    const job = currentJob();
    if (job && job.stage === 'queued') {
      D.diff = null;
      D.diffError = null;
      D.diffInfo = 'The job is still queued: its worktree is created when the spec stage starts.';
      D.diffSig = '';
      renderDiffTab();
      return;
    }
    D.diffInfo = null;
    D.diffLoading = true;
    D.diffSig = job ? job.stage + '|' + job.round + '|' + job.resumes : '';
    renderDiffTab();
    try {
      const r = await api(jobPath(D.repo, D.id) + '/diff');
      if (D.key !== key) return;
      D.diff = r || { stat: '', diff: '', truncated: false };
      D.diffError = null;
    } catch (e) {
      if (D.key !== key) return;
      D.diffError = e.message;
    } finally {
      if (D.key === key) { D.diffLoading = false; renderDiffTab(); }
    }
  }
  function renderDiffTab() {
    if (!D.key) return;
    const panel = D.els.panels.diff;
    const y = panel.scrollTop;
    clear(panel);
    const job = currentJob();
    const refresh = btn('Refresh', { cls: 'btn-sm', icon: 'refresh', onClick: () => loadDiff() });
    if (D.diffLoading) setBusy(refresh, true);
    panel.appendChild(h('div', { class: 'file-view-head' }, icon('git', 'icon-sm'),
      h('span', { class: 'file-view-path' }, job && job.branch ? (job.base || 'base') + '...' + job.branch : 'Diff'), h('span', { class: 'spacer' }), refresh));
    if (D.diffLoading && !D.diff) { panel.appendChild(skeletonLines(14)); return; }
    if (D.diffInfo && !D.diff) { panel.appendChild(emptyState('git', 'No diff yet', D.diffInfo, null, true)); return; }
    if (D.diffError && !D.diff) {
      const early = job && (job.stage === 'queued' || job.stage === 'spec');
      // Before the builder runs, a missing worktree is expected, not an error.
      if (early) { panel.appendChild(emptyState('git', 'No diff yet', D.diffError + ' The worktree may not exist yet.', null, true)); return; }
      panel.appendChild(h('div', { class: 'error-state', role: 'alert' },
        h('div', { class: 'error-state-icon' }, icon('error', 'icon-lg')),
        h('div', { class: 'error-state-main' },
          h('div', { class: 'error-state-title' }, 'Could not load the diff'),
          h('div', { class: 'error-state-text' }, D.diffError)),
        btn('Retry', { cls: 'btn-sm', icon: 'refresh', onClick: () => loadDiff() })));
      return;
    }
    if (D.diff) panel.appendChild(renderDiff(D.diff, () => loadDiff()));
    panel.scrollTop = y;
  }
  function renderRaw() {
    if (!D.key) return;
    const panel = D.els.panels.raw;
    clear(panel);
    const job = currentJob();
    if (!job) { panel.appendChild(skeletonLines(10)); return; }
    const data = {};
    for (const k of Object.keys(job)) if (k.charAt(0) !== '_') data[k] = job[k];
    const json = JSON.stringify(data, null, 2);
    const copy = btn('Copy', { cls: 'btn-sm', icon: 'copy', onClick: () => copyText(json, 'job.json copied') });
    panel.appendChild(h('div', { class: 'file-view-head' }, icon('layers', 'icon-sm'), h('span', { class: 'file-view-path' }, 'job.json'), h('span', { class: 'spacer' }), copy));
    panel.appendChild(h('pre', { class: 'json-view' }, json));
  }
  RENDER.raw = function () { if (D.key && D.tab === 'raw') renderRaw(); };

  // ================================================================ chat
  const CH = { id: null, listLoaded: false, listError: null, search: '', store: new Map(), transcript: null, els: null, sending: false, builtFor: null };
  let localSeq = 0;
  function chatStore(id) {
    let st = CH.store.get(id);
    if (!st) { st = { loaded: false, loading: false, error: null, history: [], baseSeq: 0, completed: 0, firstUser: '', locals: [] }; CH.store.set(id, st); }
    return st;
  }
  async function loadChats() {
    const since = S.lastSeq;
    try {
      const r = await api('/api/chats');
      const next = new Map();
      for (const c of Array.isArray(r && r.chats) ? r.chats : []) {
        if (!c || !c.id || (S.removedChats.get(c.id) || 0) > since) continue;
        const cur = S.chats.get(c.id);
        next.set(c.id, cur && ((cur.updated_at || '') > (c.updated_at || '') || (cur._seq || 0) > since) ? cur : c);
      }
      for (const [id, c] of S.chats) if (!next.has(id) && (c._seq || 0) > since) next.set(id, c);
      S.chats = next;
      CH.listLoaded = true;
      CH.listError = null;
    } catch (e) {
      CH.listError = e.message;
    }
    invalidate('chatList', 'chatConv');
  }
  function upsertChat(chat, seq, since) {
    if (!chat || !chat.id) return;
    const cur = S.chats.get(chat.id);
    if (cur && (cur.updated_at || '') > (chat.updated_at || '')) return;
    if (!seq && since !== undefined && cur && (cur._seq || 0) > since) return;  // an event already superseded this response
    chat._seq = seq || (cur && cur._seq) || 0;
    S.chats.set(chat.id, chat);
    if (chat.status !== 'running') clearWaiting('chat:' + chat.id);
    invalidate('chatList');
    if (CH.id === chat.id) invalidate('chatConv');
  }
  function removeChat(id, seq) {
    if (id && seq) S.removedChats.set(id, seq);
    if (!id || !S.chats.delete(id)) return;
    invalidate('chatList');
    if (CH.id === id) location.hash = '#/chat';
  }
  async function loadChatDetail(id) {
    const st = chatStore(id);
    if (st.loading) return;
    st.loading = true;
    invalidate('chatConv');
    try {
      const since = S.lastSeq;
      const r = await api('/api/chats/' + enc(id));
      if (r && r.chat) upsertChat(r.chat, 0, since);
      let history = Array.isArray(r && r.messages) ? r.messages.filter((m) => m && (m.role === 'user' || m.role === 'assistant') && m.text) : [];
      st.baseSeq = S.lastSeq;
      st.locals = [];
      const chat = (r && r.chat) || S.chats.get(id);
      // Turns already in the history. Live runs are named NN-turn, so runs up to this number are
      // the same turns again (the event replay on page load would show them twice).
      // chat.turns counts every finished turn, a refused one too (it wrote no message), so it is the live runs'
      // filter; the person's messages say how far the transcript got.
      const users = history.filter((m) => m.role === 'user').length;
      const turns = chat && Number.isInteger(chat.turns) ? chat.turns : null;
      const answered = turns !== null ? Math.min(turns, users) : (chat && chat.status === 'running' ? Math.max(0, users - 1) : users);
      st.completed = turns !== null ? turns : answered;
      if (chat && chat.status === 'running' && users > answered) {
        // The running turn's user message is already saved: drop what follows it (the live run shows that).
        let n = 0;
        const cut = history.findIndex((m) => m.role === 'user' && ++n > answered);
        if (cut >= 0) history = history.slice(0, cut + 1);
      }
      const firstUser = history.find((m) => m.role === 'user');
      st.firstUser = firstUser ? String(firstUser.text) : '';
      st.history = history;
      st.loaded = true;
      st.error = null;
    } catch (e) {
      st.error = e.message;
    } finally {
      st.loading = false;
      invalidate('chatConv');
    }
  }
  function chatBlocks(id) {
    const st = chatStore(id);
    const blocks = st.history.map((m, i) => ({ key: 'h' + i, kind: 'msg', role: m.role, text: String(m.text || ''), msg: m, chat: id }));
    const live = [];
    const runs = S.runsByOwner.get('chat:' + id);
    if (runs) {
      for (const run of runs.values()) {
        const shown = run.no !== null ? run.no > st.completed : run.firstSeq > st.baseSeq;
        if (shown) live.push({ key: run.key, kind: 'run', run, order: run.firstSeq });
      }
    }
    for (const l of st.locals) live.push({ key: l.key, kind: 'msg', role: 'user', text: l.text, order: l.anchor });
    live.sort((a, b) => a.order - b.order);
    return blocks.concat(live);
  }
  function chatRunning(chat) { return !!chat && chat.status === 'running'; }
  function chatJobLink(chat) {
    if (!chat || !chat.job) return null;
    const j = chat.job;
    const id = typeof j === 'string' ? j : j.id;
    const repo = typeof j === 'object' && j.repo ? j.repo : chat.repo;
    if (!id) return null;
    return h('a', { class: 'tag tag-accent', href: '#/job/' + enc(repo) + '/' + enc(id), title: 'Handed off to the factory' }, icon('handoff', 'icon-xs'), id);
  }
  RENDER.chatList = function () {
    const ul = document.getElementById('chat-list');
    if (!ul) return;
    const focusedId = document.activeElement && ul.contains(document.activeElement) ? document.activeElement.dataset.chat : null;
    clear(ul);
    if (!CH.listLoaded) {
      if (CH.listError) ul.appendChild(h('li', null, callout('err', 'alert', 'Could not load chats.', CH.listError)));
      else for (let i = 0; i < 4; i++) ul.appendChild(h('li', { style: { padding: '8px 10px' } }, skeletonLines(2)));
      return;
    }
    const q = CH.search.trim().toLowerCase();
    const chats = Array.from(S.chats.values())
      .filter((c) => (!S.filterRepo || c.repo === S.filterRepo) && (!q || [c.title, c.repo, c.id, c.model, c.agent].some((x) => x && String(x).toLowerCase().indexOf(q) >= 0)))
      .sort((a, b) => String(b.updated_at || '').localeCompare(String(a.updated_at || '')));
    if (!chats.length) {
      ul.appendChild(h('li', null, emptyState('chat', q ? 'No chats match' : 'No chats yet', q ? 'Try another filter.' : 'Start a chat: with an agent on one of your repositories, with an agent in a scratch folder, or with one of your personas.', null, true)));
      return;
    }
    for (const c of chats) {
      const status = String(c.status || 'idle');
      const b = h('button', { type: 'button', class: 'chat-item', 'aria-current': CH.id === c.id ? 'true' : 'false', dataset: { chat: c.id } },
        h('span', { class: 'chat-item-top' }, h('span', { class: 'status-dot ' + pick(status, CHAT_STATES, 'other'), title: status, role: 'img', 'aria-label': 'Status: ' + status }), h('span', { class: 'chat-item-title' }, c.title || 'New chat'), timeEl(c.updated_at, 'muted')),
        h('span', { class: 'chat-item-meta' }, h('span', { class: 'tag' }, c.repo || (c.persona ? 'persona' : 'no repository')), c.repo ? h('span', { class: 'tag ' + (c.mode === 'repo' ? 'tag-warn' : 'tag-outline') }, c.mode || 'worktree') : null, c.model ? h('span', { class: 'ellipsis mono' }, c.model) : null));
      b.addEventListener('click', () => { location.hash = '#/chat/' + enc(c.id); });
      ul.appendChild(h('li', null, b));
    }
    if (focusedId) { const el = ul.querySelector('[data-chat="' + CSS.escape(focusedId) + '"]'); if (el) el.focus({ preventScroll: true }); }
  };
  function selectChat(id) {
    const view = document.getElementById('view-chat');
    view.classList.toggle('has-conv', !!id);
    if (CH.id !== id) {
      CH.id = id;
      if (CH.transcript) { CH.transcript.destroy(); CH.transcript = null; }
      CH.builtFor = null;
      if (id) { const st = chatStore(id); if (!st.loaded) loadChatDetail(id); }
    }
    invalidate('chatList', 'chatConv');
  }
  function autosize(ta) {
    ta.style.height = 'auto';
    ta.style.height = Math.min(ta.scrollHeight, 220) + 'px';
  }
  function buildChatShell(main, id) {
    clear(main);
    const head = h('div', { class: 'chat-head' });
    const scroll = h('div', { class: 'chat-scroll' });
    const thread = h('div', { class: 'chat-thread', 'aria-live': 'off' });
    const intro = h('div');
    const outro = h('div');   // after the last message: the last turn's error, where the person is reading
    const jump = h('button', { type: 'button', class: 'jump-pill', hidden: true }, icon('arrow-down', 'icon-sm'), 'Jump to latest');
    add(scroll, [h('div', { class: 'chat-thread', style: { 'padding-bottom': '0' } }, intro), thread,
      h('div', { class: 'chat-thread chat-outro', style: { 'padding-top': '0' } }, outro), jump]);
    const running = h('div', { class: 'chat-running', hidden: true, role: 'status' });
    const input = h('textarea', { rows: 1, placeholder: 'Message the coder agent…', 'aria-label': 'Message', spellcheck: 'true' });
    const send = h('button', { type: 'submit', class: 'btn btn-primary btn-icon', 'aria-label': 'Send message', title: 'Send (Enter)' }, icon('send'));
    const form = h('form', { class: 'composer' }, input, send);
    const hint = h('div', { class: 'composer-hint' }, h('span', null, h('kbd', null, 'Enter'), ' send'), h('span', null, h('kbd', null, 'Shift'), '+', h('kbd', null, 'Enter'), ' new line'));
    add(main, [head, scroll, running, h('div', { class: 'composer-wrap' }, form, hint)]);
    input.addEventListener('input', () => autosize(input));
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey && !e.isComposing && e.keyCode !== 229) { e.preventDefault(); sendChat(); }
    });
    form.addEventListener('submit', (e) => { e.preventDefault(); sendChat(); });
    CH.els = { head, scroll, thread, intro, outro, jump, running, input, send, form };
    CH.transcript = new Transcript(thread, scroll, jump, 'chat');
    CH.builtFor = id;
  }
  RENDER.chatConv = function () {
    const main = document.getElementById('chat-main');
    if (!main || S.route.view !== 'chat') return;
    if (!CH.id) {
      CH.builtFor = null;
      if (CH.transcript) { CH.transcript.destroy(); CH.transcript = null; }
      clear(main);
      // No repository is needed to talk: a persona chat and a chat without a repository start without one (§15).
      main.appendChild(h('div', { class: 'chat-scroll', style: { display: 'grid', 'place-items': 'center' } },
        S.loaded && !S.repos.length
          ? emptyState('chat', 'Talk to an agent or a persona', 'Pick a chat on the left, or start one. Talk to a persona such as Kate, or to an agent in a scratch folder of its own: neither needs a repository. For code work, add a repository: its chats get their own branch and can be handed to the factory.',
            [btn('New chat', { cls: 'btn-primary', icon: 'plus', onClick: () => openNewChat() }), h('a', { class: 'btn', href: '#/setup/repo' }, icon('folder', 'icon-sm'), 'Add a repository')])
          : emptyState('chat', 'Talk to an agent or a persona', 'Pick a chat on the left, or start one. On a repository, each chat gets its own branch and worktree, so your main checkout stays untouched, and you can hand the branch to the factory for review. You can also talk to an agent without a repository, or to one of your personas.',
            [btn('New chat', { cls: 'btn-primary', icon: 'plus', onClick: () => openNewChat() }), btn('Analyse a binary', { icon: 'scout', onClick: () => startFlowWith({ what: 'analyse' }) })])));
      return;
    }
    const id = CH.id;
    if (CH.builtFor !== id) buildChatShell(main, id);
    const chat = S.chats.get(id);
    const st = chatStore(id);
    renderChatHead(chat, st);
    const els = CH.els;
    clear(els.intro);
    if (st.error && !st.loaded) els.intro.appendChild(callout('err', 'alert', 'Could not load this chat.', st.error));
    else if (!st.loaded) els.intro.appendChild(skeletonLines(5));
    const blocks = st.loaded ? chatBlocks(id) : [];
    // A turn the server refused for good (the agent became a persona: chats.py _refuse_new_persona) ends the chat:
    // no "Start the conversation", no "try again", and the composer says why it is closed.
    const ended = !!chat && chat.status === 'error' && /this chat cannot go on|its chats run only as persona chats/.test(String(chat.error || ''));
    if (st.loaded && !blocks.length && !(chat && chat.status === 'error')) {
      els.intro.appendChild(emptyState('chat', 'Start the conversation',
        chat && !chat.repo ? (chat.persona ? 'Say what you need: ' + (chat.persona.label || chat.agent) + ' answers here. This chat has no repository and nothing in it goes to the factory.'
          : 'The ' + (chat.agent || 'coder') + ' agent works in a scratch folder of its own: no repository, no branch and no hand-off. An agent without tools only talks.')
          : chat ? 'The coder agent works in ' + (chat.mode === 'repo' ? 'your repo checkout directly' : 'branch ' + (chat.branch || 'chat/' + id)) + ' on ' + chat.repo + '. Ask it to explain, change or test something.' : '', null, true));
    }
    CH.transcript.sync(blocks);
    // running indicator + composer state
    const running = chatRunning(chat);
    clear(els.running);
    els.running.hidden = !running;
    if (running) {
      const runs = S.runsByOwner.get('chat:' + id);
      let since = parseTs(chat.updated_at);
      if (runs) for (const r of runs.values()) if (r.status === 'running' && isFinite(parseTs(r.startTs))) since = parseTs(r.startTs);
      const cancel = btn('Cancel', { cls: 'btn-sm btn-danger', icon: 'stop' });
      cancel.addEventListener('click', async () => {
        setBusy(cancel, true);
        try { const since = S.lastSeq; const r = await api('/api/chats/' + enc(id) + '/cancel', { method: 'POST' }); if (r && r.chat) upsertChat(r.chat, 0, since); }
        catch (e) { toastError('Cancel failed', e); setBusy(cancel, false); }
      });
      const elapsed = h('span', { class: 'elapsed' }, isFinite(since) ? fmtClock(Date.now() - since) : '');
      if (isFinite(since)) elapsed.dataset.clock = String(since);
      const wait = waitingOf('chat:' + id);
      els.running.classList.toggle('is-waiting', !!wait);
      add(els.running, [wait ? icon('hourglass', 'icon-sm') : h('span', { class: 'spinner', 'aria-hidden': 'true' }),
        h('span', { title: wait ? waitWords(wait).long : null }, wait ? (wait.custody === 'HELD' ? 'Waiting for the GPU custody register: the card is held by someone else…' : 'Waiting for ' + wait.provider + '…') : 'Working'), elapsed, h('span', { class: 'spacer' }), cancel]);
    }
    const disabled = running || CH.sending || !chat || ended;
    els.input.disabled = disabled;
    els.send.disabled = disabled;
    els.input.placeholder = ended ? 'This chat cannot go on: see the note above.' : running ? 'The agent is working… wait for it to finish or cancel.'
      : chat && chat.persona ? 'Message ' + chat.agent + '…' : 'Message the ' + ((chat && chat.agent) || 'coder') + ' agent…';
    const shownErr = chat && chat.status === 'error' && !running ? String(chat.error || '').trim() || '-' : '';
    if (els.outro.dataset.err !== shownErr) {
      clear(els.outro);
      els.outro.dataset.err = shownErr;
      if (shownErr) {
        const why = shownErr === '-' ? '' : shownErr;
        els.outro.appendChild(ended ? callout('err', 'alert', 'This chat cannot go on.', 'The server said: ' + why + (/[.!?]$/.test(why) ? '' : '.'))
          : callout('err', 'alert', 'The last turn ended with an error.', (why ? why + (/[.!?]$/.test(why) ? ' ' : '. ') : '') + 'Send another message to try again.'));
        if (CH.transcript && CH.transcript.stick) CH.transcript.toBottom();
      }
    }
  };
  /** The server keeps the first 60 characters of the first message as the title: mark the cut. */
  function chatTitle(chat, st) {
    const t = String(chat.title || '');
    if (!t) return 'New chat';
    const raw = st ? st.firstUser || (st.locals[0] && st.locals[0].text) || '' : '';
    const first = raw.replace(/\s+/g, ' ').trim();
    return first.length > t.length && first.indexOf(t) === 0 ? t.replace(/\s+$/, '') + '…' : t;
  }
  function chatModelLabel(chat) {
    const key = String(chat.model || '');
    const m = key && S.models ? S.models[key] : null;
    return m && m.model && m.model !== key ? key + ' → ' + m.model : key;
  }
  function renderChatHead(chat, st) {
    const head = CH.els.head;
    // The head is rebuilt on each render: a focused control in it (data-focus, or the title) keeps the focus.
    const active = document.activeElement;
    const keep = active && active !== head && head.contains(active)
      ? (active.dataset && active.dataset.focus) || (active.classList.contains('chat-title') ? '@title' : null) : null;
    clear(head);
    const back = h('a', { class: 'btn btn-ghost btn-icon btn-sm chat-back', href: '#/chat', 'aria-label': 'Back to chats' }, icon('chevron-left'));
    if (!chat) {
      head.appendChild(back);
      head.appendChild(h('div', { class: 'chat-head-main' }, h('div', { class: 'chat-title' }, st.error ? 'Chat unavailable' : 'Loading…')));
      return;
    }
    const meta = h('div', { class: 'meta-row' },
      h('span', { class: 'tag', title: chat.repo ? null : 'A chat without a repository: it works in a scratch folder and has no hand-off' }, chat.repo || 'no repository'),
      chat.repo ? h('span', { class: 'tag ' + (chat.mode === 'repo' ? 'tag-warn' : 'tag-outline'), title: chat.mode === 'repo' ? 'Works directly in the repo checkout' : 'Isolated worktree' }, chat.mode || 'worktree') : null,
      chat.branch ? h('span', { class: 'meta-item', title: 'Branch' }, icon('branch', 'icon-xs'), h('span', { class: 'mono' }, chat.branch)) : null,
      chat.model ? h('span', { class: 'meta-item', title: 'Model' }, icon('agents', 'icon-xs'), h('span', { class: 'mono' }, chatModelLabel(chat))) : null,
      fmtUsage(chat.usage) ? h('span', { class: 'meta-item', title: fmtUsage(chat.usage, true) }, icon('zap', 'icon-xs'), fmtUsage(chat.usage)) : null,
      chatJobLink(chat), extNodes('chatHead', chat));
    const actions = h('div', { class: 'chat-head-actions' });
    if (chat.repo && chat.mode !== 'repo' && chat.mode !== 'none') {
      const ho = btn('Hand off to factory', { cls: 'btn-sm', icon: 'handoff', disabled: chatRunning(chat), title: 'Send this chat’s branch through the review line' });
      ho.addEventListener('click', () => handoffChat(chat));
      actions.appendChild(ho);
    }
    const del = h('button', { type: 'button', class: 'btn btn-ghost btn-icon btn-sm', title: 'Delete chat', 'aria-label': 'Delete chat' }, icon('trash', 'icon-sm'));
    del.addEventListener('click', () => deleteChat(chat));
    actions.appendChild(del);
    const title = chatTitle(chat, st);
    add(head, [back, h('div', { class: 'chat-head-main' }, h('h2', { class: 'chat-title', tabindex: '-1', title: st && st.firstUser ? st.firstUser.slice(0, 300) : title }, title), meta), actions]);
    if (chat.mode === 'repo') head.appendChild(h('div', { style: { 'flex-basis': '100%' } }, callout('warn', 'alert', 'Repo mode:', 'the agent edits your real checkout at ' + (chat.cwd || 'the repo path') + '. Commit or stash your own work first.')));
    for (const n of extNodes('chatBanner', chat)) head.appendChild(h('div', { class: 'chat-banner', style: { 'flex-basis': '100%' } }, n));
    if (keep) {
      const t = keep === '@title' ? head.querySelector('.chat-title') : head.querySelector('[data-focus="' + CSS.escape(keep) + '"]');
      if (t) t.focus({ preventScroll: true });
    }
  }
  async function sendChat() {
    const id = CH.id;
    if (!id || !CH.els) return;
    const ta = CH.els.input;
    const text = ta.value.trim();
    if (!text || CH.sending) return;
    const chat = S.chats.get(id);
    if (chatRunning(chat)) { toast('The agent is still working. Wait for it, or cancel the turn.', { kind: 'info' }); return; }
    const st = chatStore(id);
    const local = { key: 'l' + (++localSeq), text, anchor: S.lastSeq + 0.5 };
    st.locals.push(local);
    CH.sending = true;
    ta.value = '';
    autosize(ta);
    if (CH.transcript) CH.transcript.stick = true;
    invalidate('chatConv');
    try {
      const since = S.lastSeq;
      const r = await api('/api/chats/' + enc(id) + '/messages', { method: 'POST', body: { text } });
      if (r && r.chat) upsertChat(r.chat, 0, since);
    } catch (e) {
      st.locals = st.locals.filter((x) => x !== local);
      if (!ta.value) { ta.value = text; autosize(ta); }
      toast(e.status === 409 ? 'The agent is still working on the previous message.' : 'Message not sent: ' + e.message, { kind: 'error' });
    } finally {
      CH.sending = false;
      invalidate('chatConv');
      setTimeout(() => { if (CH.els && !CH.els.input.disabled) CH.els.input.focus(); }, 0);
    }
  }
  function openNewChat() {
    if (document.querySelector('dialog[open]')) return;
    const hasRepos = S.repos.length > 0;
    const def = hasRepos ? (S.filterRepo || lsGet('sf.lastRepo') || S.repos[0].name) : '';
    const repoSel = selectEl(S.repos.map((r) => [r.name, r.name]), hasRepos ? (S.repos.some((r) => r.name === def) ? def : S.repos[0].name) : '', { id: 'nc-repo' });
    const agents = chatAgents();
    const agentSel = selectEl(agents.map((n) => [n, n + (CHAT_AGENT_TEXT[n] ? ' — ' + CHAT_AGENT_TEXT[n] : '')]), agents[0], { id: 'nc-agent' });
    const modelOpts = (sel) => { const m = modelOf(sel.value); return [['', 'Agent default (' + (m.key || 'opus') + ' → ' + m.model + ')']].concat(Object.keys(S.models).map((k) => [k, k + ' — ' + (S.models[k].model || '?') + (S.models[k].provider ? ' (' + S.models[k].provider + ')' : '')])); };
    const modelSel = selectEl(modelOpts(agentSel), '', { id: 'nc-model' });
    agentSel.addEventListener('change', () => { const keep = modelSel.value; clear(modelSel); for (const [v, l] of modelOpts(agentSel)) modelSel.appendChild(h('option', { value: v }, l)); modelSel.value = keep; });
    const warn = h('div', { hidden: true }, callout('warn', 'alert', 'Repo mode edits your real checkout.', 'No branch or worktree is created; the agent works directly in the repo on whatever branch is checked out. Hand-off to the factory needs worktree mode.'));
    const radio = (value, title, desc, checked) => {
      const inp = h('input', { type: 'radio', name: 'nc-mode', value, checked });
      inp.addEventListener('change', () => { warn.hidden = value !== 'repo' || !inp.checked; });
      return h('label', { class: 'radio-card' }, inp, h('span', null, h('span', { class: 'radio-card-title' }, title), h('span', { class: 'radio-card-desc', style: { display: 'block' } }, desc)));
    };
    const modes = h('div', { class: 'radio-cards', role: 'radiogroup', 'aria-label': 'Mode' },
      radio('worktree', 'Worktree', 'New branch chat/<id> in its own worktree. Safe; can hand off to the factory.', true),
      radio('repo', 'Repo checkout', 'Works directly in the repo. For quick fixes on the current branch.', false));
    // Three starts (DESIGN §15): on a repository, without one (a scratch folder, any agent), and the view modules'
    // (a persona, from views/personas.js). A name a view's start claims (a persona file) is offered only there: the
    // server would turn {mode: 'none', agent} for it into that start's chat, or refuse it.
    const extStarts = extSpecs('chatStart', { form: 'chat' });
    const allAgents = Array.from(new Set(agents.concat(S.agents.map((a) => a.name)))).filter(Boolean);
    const claimed = new Set();
    const claimedBy = [];
    for (const x of extStarts) {
      const mine = new Set([].concat((x.claims && safely('chat start ' + x.id + ' claims', () => x.claims())) || []).map(String));
      const names = allAgents.filter((n) => mine.has(n) && !claimed.has(n));
      names.forEach((n) => claimed.add(n));
      if (names.length) claimedBy.push(names.join(', ') + ' (see “' + x.title + '”)');
    }
    const anyAgents = allAgents.filter((n) => !claimed.has(n));
    const claimedNote = claimedBy.length ? h('div', { class: 'field-hint nc-claimed' }, 'Not listed here: ' + claimedBy.join('; ') + '.') : null;
    const freeSel = selectEl(anyAgents.map((n) => [n, n + (CHAT_AGENT_TEXT[n] ? ' — ' + CHAT_AGENT_TEXT[n] : '')]), anyAgents[0] || 'coder', { id: 'nc-free-agent' });
    const freeModel = selectEl(modelOpts(freeSel), '', { id: 'nc-free-model' });
    freeSel.addEventListener('change', () => { const keep = freeModel.value; clear(freeModel); for (const [v, l] of modelOpts(freeSel)) freeModel.appendChild(h('option', { value: v }, l)); freeModel.value = keep; });
    const starts = [
      { id: 'on-repo', title: 'On a repository', desc: hasRepos ? 'An agent on one of your repositories; worktree chats can hand off to the factory.' : 'Add a repository first (Setup › Repository).',
        disabled: !hasRepos, fields: () => [field('Repository', repoSel), field('Agent', agentSel), field('Model', modelSel, 'Any model key from Settings › Models.'), h('div', { class: 'field' }, h('span', { class: 'field-label' }, 'Mode'), modes), warn],
        body: () => { const body = { repo: repoSel.value, mode: (modes.querySelector('input:checked') || {}).value || 'worktree', agent: agentSel.value }; if (modelSel.value) body.model = modelSel.value; lsSet('sf.lastRepo', repoSel.value); return body; } },
      { id: 'no-repo', title: 'No repository', desc: 'Any agent in a scratch folder of its own: no branch, no hand-off. An agent without tools only talks.',
        fields: () => [field('Agent', freeSel), claimedNote, field('Model', freeModel, 'Any model key from Settings › Models.')],
        body: () => { const body = { mode: 'none', agent: freeSel.value }; if (freeModel.value) body.model = freeModel.value; return body; } },
    ].concat(extStarts);
    const panes = new Map();
    const startName = 'nc-start-' + (++modalSeq);
    const showPane = (id) => { for (const [k, pane] of panes) pane.hidden = k !== id; };
    const first = starts.find((x) => !x.disabled) || starts[0];
    const cards = h('div', { class: 'radio-cards nc-starts', role: 'radiogroup', 'aria-label': 'Start' }, starts.map((x) => {
      const inp = h('input', { type: 'radio', name: startName, value: x.id, checked: x === first, disabled: !!x.disabled, dataset: { start: x.id } });
      inp.addEventListener('change', () => { if (inp.checked) showPane(x.id); });
      return h('label', { class: 'radio-card' + (x.disabled ? ' is-disabled' : '') }, inp, h('span', null, h('span', { class: 'radio-card-title' }, String(x.title)), x.desc ? h('span', { class: 'radio-card-desc', style: { display: 'block' } }, String(x.desc)) : null));
    }));
    const body = [h('div', { class: 'field' }, h('span', { class: 'field-label' }, 'Start'), cards)];
    for (const x of starts) {
      const pane = h('div', { class: 'nc-start-pane', dataset: { pane: x.id }, hidden: x !== first });
      add(pane, safely('chat start ' + x.id, () => x.fields ? x.fields() : null) || null);
      panes.set(x.id, pane);
      body.push(pane);
    }
    // The view modules' fields of the New chat form (a routing profile) serve every start: POST /api/chats takes a
    // profile for a chat on a repository, without one and a persona's.
    const fx = formExt('chat', { repo: () => repoSel.value, watch: [repoSel, modelSel, freeModel] });
    body.push(fx.nodes);
    openModal({
      title: 'New chat',
      description: 'An interactive session with one agent: on a repo (the coder with all tools, or the analyst with REA), without a repository, or with one of your personas.',
      body,
      actions: [{ label: 'Cancel', value: 'cancel' }, { label: 'Start chat', value: 'ok', primary: true, icon: 'chat' }],
      onSubmit: async (v, ctl) => {
        const chosen = (cards.querySelector('input:checked') || {}).value || first.id;
        const start = starts.find((x) => x.id === chosen) || first;
        const req = start.body();
        fx.apply(req, 'ok');
        let r;
        try { r = await api('/api/chats', { method: 'POST', body: req }); }
        catch (e) {
          const help = start.onError ? safely('chat start ' + start.id + ' error', () => start.onError(e)) : null;
          if (!help) throw e;
          ctl.showError([e.message, ' ', help]);
          return false;
        }
        if (!r || !r.chat) throw new Error('The server returned no chat.');
        upsertChat(r.chat);
        const st = chatStore(r.chat.id);
        st.loaded = true; st.history = []; st.baseSeq = S.lastSeq; st.completed = 0; st.firstUser = ''; st.locals = [];
        location.hash = '#/chat/' + enc(r.chat.id);
        setTimeout(() => { if (CH.els) CH.els.input.focus(); }, 60);
        return r.chat;
      },
    });
  }
  function handoffChat(chat) {
    const inp = h('input', { class: 'input', type: 'text', maxlength: 300, value: chat.title || '', placeholder: 'One line: what the work on this branch does' });
    const delivery = deliveryChooser({ id: 'ho-delivery' });
    const fx = formExt('handoff', { chat, repo: () => chat.repo || null });
    openModal({
      title: 'Hand off to the factory',
      description: ['The factory takes branch ', h('span', { class: 'mono' }, chat.branch || 'chat/' + chat.id), ' as round 1: the spec-writer describes it, the reviewers check it, the approver decides. The branch needs commits ahead of base and a clean tree.'],
      body: [field('Feature (one line)', inp), fx.nodes, delivery.el],
      dismissable: false,
      actions: [{ label: 'Cancel', value: 'cancel' }, { label: 'Hand off', value: 'ok', primary: true, icon: 'handoff' }],
      onSubmit: async () => {
        const feature = inp.value.trim();
        if (!feature) throw new Error('Describe the work in one line.');
        let r;
        const since = S.lastSeq;
        const body = { feature };
        const out = delivery.value();
        if (out !== undefined) body.delivery = out;
        fx.apply(body, 'ok');
        try { r = await api('/api/chats/' + enc(chat.id) + '/handoff', { method: 'POST', body }); }
        catch (e) { toastError('Hand-off failed', e); throw e; }
        const job = r && r.job;
        if (job) {
          upsertJob(job, 0, since);
          toast(['Handed off as ', h('strong', { class: 'mono' }, job.id), '.'], { kind: 'ok', action: { label: 'Open job', href: '#/job/' + enc(job.repo) + '/' + enc(job.id) }, timeout: 8000 });
          const c = S.chats.get(chat.id);
          if (c && !c.job) { S.chats.set(chat.id, Object.assign({}, c, { job: job.id })); invalidate('chatConv'); }
        }
        return job;
      },
    });
  }
  async function deleteChat(chat) {
    await confirmModal({
      title: 'Delete this chat?',
      description: chat.mode === 'repo' ? 'The conversation is deleted. Your repo checkout is left as it is.' : 'The conversation, its worktree and branch ' + (chat.branch || 'chat/' + chat.id) + ' are deleted. Unmerged work on the branch is lost.',
      confirm: 'Delete chat', cls: 'btn-danger-solid', icon: 'trash',
      onSubmit: async () => {
        try { await api('/api/chats/' + enc(chat.id) + '/delete', { method: 'POST' }); }
        catch (e) { toastError('Delete failed', e); throw e; }
        S.chats.delete(chat.id);
        S.removedChats.set(chat.id, S.lastSeq + 0.5);
        CH.store.delete(chat.id);
        toast('Chat deleted.', { kind: 'ok' });
        location.hash = '#/chat';
      },
    });
  }

  // ================================================================ plans view (DESIGN §24)
  // edit: the open plan.md editor {key, text, orig, …}; drafts: unsaved editors of other plans, kept until the
  // user comes back to them (switching plans never throws typed text away).
  const PL = { loaded: false, loading: false, error: null, sel: null, detail: new Map(), edit: null, drafts: new Map(), answers: new Map(), updTimer: 0 };
  const planEditDirty = (ed) => !!ed && ed.text !== ed.orig;
  const planPath = (repo, id) => '/api/plans/' + enc(repo) + '/' + enc(id);
  const splitKey = (k) => { const i = k.indexOf('/'); return [k.slice(0, i), k.slice(i + 1)]; };
  function visiblePlans() {
    const out = [];
    for (const p of S.plans.values()) if (!S.filterRepo || p.repo === S.filterRepo) out.push(p);
    return out.sort((a, b) => String(b.updated_at || '').localeCompare(String(a.updated_at || '')));
  }
  async function loadPlans() {
    PL.loading = true;
    const since = S.lastSeq;
    try {
      const r = await api('/api/plans');
      const next = new Map();
      for (const p of Array.isArray(r && r.plans) ? r.plans : []) {
        if (!p || !p.id || !p.repo) continue;
        const k = planKey(p);
        const cur = S.plans.get(k);
        next.set(k, cur && ((cur.updated_at || '') > (p.updated_at || '') || (cur._seq || 0) > since) ? cur : p);
      }
      for (const [k, p] of S.plans) if (!next.has(k) && (p._seq || 0) > since) next.set(k, p);
      S.plans = next;
      PL.loaded = true;
      PL.error = null;
    } catch (e) {
      PL.error = e.message;
    } finally {
      PL.loading = false;
      invalidate('plans', 'topbar');
    }
  }
  function planDetail(k) {
    let d = PL.detail.get(k);
    if (!d) { d = { files: [], loaded: false, loading: false, error: null, notFound: false, texts: new Map(), errs: new Map() }; PL.detail.set(k, d); }
    return d;
  }
  async function loadPlanFile(k, rel, gen) {
    const [repo, id] = splitKey(k);
    const d = planDetail(k);
    try {
      const t = await api(planPath(repo, id) + '/file?path=' + enc(rel), { text: true });
      if (gen !== d.gen) return;
      d.texts.set(rel, String(t));
      d.errs.delete(rel);
    } catch (e) {
      if (gen === d.gen) d.errs.set(rel, e.message);
    }
  }
  /** Reloads overlap (live updates, a save, the route): each has a generation, and only the newest one's answers
   *  land, so an older file list can never drop a review file a newer one has. */
  async function loadPlanDetail(k) {
    const [repo, id] = splitKey(k);
    const d = planDetail(k);
    const gen = d.gen = (d.gen || 0) + 1;
    d.loading = true;
    try {
      const since = S.lastSeq;
      const r = await api(planPath(repo, id));
      if (gen !== d.gen) return;
      if (r && r.plan) upsertPlan(r.plan, 0, since);
      d.files = Array.isArray(r && r.files) ? r.files.map(String) : [];
      const want = d.files.filter((f) => f === 'plan.md' || f === 'request.md' || /^review-\d+\.md$/.test(f));
      for (const f of Array.from(d.texts.keys())) if (d.files.indexOf(f) < 0) d.texts.delete(f);
      await Promise.all(want.map((f) => loadPlanFile(k, f, gen)));
      if (gen !== d.gen) return;
      d.loaded = true;
      d.error = null;
      d.notFound = false;
    } catch (e) {
      if (gen !== d.gen) return;
      d.error = e.message;
      d.notFound = e.status === 404;
    } finally {
      if (gen === d.gen) {
        d.loading = false;
        if (PL.sel === k) invalidate('plans');
      }
    }
  }
  function onSelectedPlanUpdate(prev, plan) {
    clearTimeout(PL.updTimer);
    PL.updTimer = setTimeout(() => { if (PL.sel === planKey(plan)) loadPlanDetail(PL.sel); }, 350);
  }
  /** plans.plan_status + the ## Questions list of a PLAN: QUESTIONS plan.md. */
  function planMdStatus(text) {
    for (const line of String(text || '').replace(/^﻿/, '').split('\n')) {
      let t = line.trim();
      if (!t) continue;
      for (;;) { const bare = t.replace(/^#+/, '').trim().replace(/^[*_`]+|[*_`]+$/g, '').trim(); if (bare === t) break; t = bare; }
      return t === 'PLAN: READY' ? 'READY' : t === 'PLAN: QUESTIONS' ? 'QUESTIONS' : null;
    }
    return null;
  }
  const HEADING_RE = /^ {0,3}(#{1,6})\s+(.*?)[\s#]*$/;
  const BULLET_RE = /^ ?(?:\d+[.)]|[-*+])\s+(.*\S)\s*$/;
  const ANSWER_RE = /^\s*(?:\*\*|__)?answer:(?:\*\*|__)?\s*/i;
  /** The ## Questions items of plan.md (plans._bullets): [{q, a, line}], where a is the human's answer written
   *  under the item as an indented "**Answer:** …" (continuation lines), line the item's line index. */
  function planQuestionItems(text) {
    const lines = String(text || '').split('\n');
    const out = [];
    let level = 0;
    let inside = false;
    let inAnswer = false;
    lines.forEach((line, i) => {
      if (inside === null) return;
      const hm = HEADING_RE.exec(line);
      if (hm) {
        if (inside && hm[1].length <= level) { inside = null; return; }
        if (!inside && hm[2].trim().toLowerCase() === 'questions') { inside = true; level = hm[1].length; }
        return;
      }
      if (!inside) return;
      const bm = BULLET_RE.exec(line);
      if (bm) { out.push({ q: bm[1], a: '', line: i }); inAnswer = false; return; }
      const cur = out[out.length - 1];
      if (!cur || !/^\s{2,}\S/.test(line)) return;
      if (ANSWER_RE.test(line)) { inAnswer = true; cur.a = line.replace(ANSWER_RE, '').trim(); }
      else if (inAnswer) cur.a += '\n' + line.trim();
      else cur.q += '\n' + line.trim();
    });
    return out;
  }
  function planQuestions(text) { return planQuestionItems(text).map((x) => x.q); }
  /** plan.md with answers[i] written under question i as "   **Answer:** …" (replacing an earlier answer). */
  function withAnswers(text, answers) {
    const lines = String(text || '').split('\n');
    const items = planQuestionItems(text);
    for (let i = items.length - 1; i >= 0; i--) {
      const start = items[i].line;
      // The item runs up to the next item, heading or non-indented line; trailing blank lines stay outside it.
      let end = start + 1;
      while (end < lines.length && !BULLET_RE.test(lines[end]) && !HEADING_RE.test(lines[end]) && (/^\s{2,}\S/.test(lines[end]) || !lines[end].trim())) end++;
      while (end > start + 1 && !lines[end - 1].trim()) end--;
      const body = lines.slice(start + 1, end);
      const at = body.findIndex((l) => ANSWER_RE.test(l));
      const keep = at >= 0 ? body.slice(0, at) : body;
      const ans = String(answers[i] || '').trim();
      const ansLines = ans ? ans.split('\n').map((l, j) => '   ' + (j ? '' : '**Answer:** ') + l.trim()) : [];
      lines.splice(start + 1, end - start - 1, ...keep.concat(ansLines));
    }
    return lines.join('\n');
  }
  /** plan.md without its first line when that is the PLAN: status line (the page shows it as a badge). */
  function stripStatusLine(text) {
    const t = String(text || '');
    if (!planMdStatus(t)) return t;
    return t.replace(/^﻿?\s*[^\n]*\n?/, '');
  }
  /** A review file without its VERDICT line and a leading "Final review k" heading (the card shows both). */
  function stripReviewHead(text) {
    let t = String(text || '').replace(/^﻿?\s*/, '');
    if (/^[#*_` ]*VERDICT\s*:/i.test(t.split('\n')[0])) t = t.replace(/^[^\n]*\n?/, '').replace(/^\s*/, '');
    if (/^#{1,6}\s+final review\b[^\n]*$/im.test(t.split('\n')[0])) t = t.replace(/^[^\n]*\n?/, '');
    return t;
  }
  function planProgress(p) {
    const jobs = Array.isArray(p.jobs) ? p.jobs : [];
    const merged = jobs.filter((j) => liveJobStage(p, j) === 'merged').length;
    return { total: jobs.length, merged };
  }
  function liveJobStage(p, entry) {
    const job = entry && entry.job_id ? S.jobs.get(p.repo + '/' + entry.job_id) : null;
    return (job && job.stage) || (entry && entry.stage) || null;
  }
  function planRoutingLabel(p) {
    if (p.routing === null || p.routing === undefined) return 'saved routing';
    if (typeof p.routing === 'string') return 'preset ' + p.routing;
    return isObj(p.routing) ? 'custom routing (' + plural(Object.keys(p.routing).length, 'step') + ')' : 'routing';
  }
  function planItem(p) {
    const k = planKey(p);
    const pr = planProgress(p);
    const pct = pr.total ? Math.round((pr.merged / pr.total) * 100) : 0;
    const last = Array.isArray(p.reviews) && p.reviews.length ? p.reviews[p.reviews.length - 1] : null;
    const draft = PL.drafts.has(k) || (PL.edit && PL.edit.key === k && planEditDirty(PL.edit));
    return h('a', { class: 'plan-item', href: '#/plans/' + enc(p.repo) + '/' + enc(p.id), 'aria-current': PL.sel === k ? 'true' : 'false', dataset: { plan: k } },
      h('span', { class: 'plan-item-top' }, planStagePill(p.stage), h('span', { class: 'plan-item-id', title: k }, p.id),
        draft ? h('span', { class: 'tag tag-warn plan-draft', title: 'An unsaved plan.md edit is kept for this plan' }, icon('edit', 'icon-xs'), 'draft') : null,
        timeEl(p.updated_at || p.created_at, 'plan-item-time')),
      h('span', { class: 'plan-item-req' }, firstLine(p.request) || '(no request)'),
      h('span', { class: 'plan-item-meta' },
        h('span', { class: 'tag' }, p.repo),
        pr.total
          ? h('span', { class: 'plan-progress', title: pr.merged + ' of ' + pr.total + ' jobs merged' },
            h('span', { class: 'plan-bar', 'aria-hidden': 'true' }, h('span', { class: 'plan-bar-fill', style: { width: pct + '%' } })), pr.merged + '/' + pr.total + ' merged')
          : h('span', { class: 'muted' }, p.stage === 'planning' ? 'planning…' : 'no jobs yet'),
        last ? verdictTag(last.verdict) : null));
  }

  // ---------------------------------------------------------------- job graph (SVG)
  let measureCtx = null;
  function textWidth(text, font) {
    if (!measureCtx) { try { measureCtx = document.createElement('canvas').getContext('2d'); } catch (e) { measureCtx = null; } }
    if (!measureCtx) return text.length * 6.6;
    measureCtx.font = font;
    return measureCtx.measureText(text).width;
  }
  /** Greedy word wrap into at most `max` lines of `width` px, the last one ellipsized. */
  function wrapLines(text, width, font, max) {
    const words = String(text).replace(/\s+/g, ' ').trim().split(' ');
    const lines = [];
    let cur = '';
    for (let i = 0; i < words.length; i++) {
      const next = cur ? cur + ' ' + words[i] : words[i];
      if (textWidth(next, font) <= width) { cur = next; continue; }
      if (cur) lines.push(cur);
      cur = words[i];
      if (lines.length === max) { cur = ''; break; }
    }
    if (cur && lines.length < max) lines.push(cur);
    const shown = lines.join(' ').length;
    if (shown < String(text).replace(/\s+/g, ' ').trim().length && lines.length) {
      let last = lines[lines.length - 1];
      while (last && textWidth(last + '…', font) > width) last = last.slice(0, -1);
      lines[lines.length - 1] = last.replace(/\s+$/, '') + '…';
    }
    return lines;
  }
  /** Layers left→right by dependency depth (longest chain of `after`); follow-ups from final reviews come last. */
  function planLayers(jobs) {
    const byN = new Map(jobs.map((j) => [j.n, j]));
    const depth = new Map();
    const visit = (j, trail) => {
      if (depth.has(j.n)) return depth.get(j.n);
      if (trail.has(j.n)) return 0;
      trail.add(j.n);
      let d = 0;
      for (const a of Array.isArray(j.after) ? j.after : []) { const dep = byN.get(a); if (dep && !dep.review) d = Math.max(d, visit(dep, trail) + 1); }
      depth.set(j.n, d);
      return d;
    };
    const layers = [];
    for (const j of jobs) if (!j.review) { const d = visit(j, new Set()); (layers[d] = layers[d] || []).push(j); }
    const follow = jobs.filter((j) => j.review);
    const out = layers.filter(Boolean);
    if (follow.length) out.push(follow);
    const pos = new Map();
    out.forEach((layer, li) => {
      if (li > 0) {
        const bary = (j) => { const ps = (j.after || []).map((a) => pos.get(a)).filter((x) => x !== undefined); return ps.length ? ps.reduce((s, x) => s + x, 0) / ps.length : 1e9; };
        layer.sort((a, b) => bary(a) - bary(b) || a.n - b.n);
      } else layer.sort((a, b) => a.n - b.n);
      layer.forEach((j, i) => pos.set(j.n, i));
    });
    return out;
  }
  const NODE_TONE = { queued: 'neutral', spec: 'accent', build: 'accent', review: 'accent', approve: 'accent', merging: 'accent', merged: 'ok', 'needs-human': 'human', failed: 'err', rejected: 'muted', cancelled: 'muted' };
  function planGraph(p, avail) {
    const jobs = Array.isArray(p.jobs) ? p.jobs.filter((j) => j && Number.isFinite(Number(j.n))) : [];
    const layers = planLayers(jobs);
    const L = Math.max(1, layers.length);
    const PAD = 10;
    const GX = avail < 620 ? 40 : 56;
    const W = Math.max(156, Math.min(216, Math.floor((avail - PAD * 2 - (L - 1) * GX) / L)));
    const H = 66;
    const GY = 14;
    const maxRows = layers.reduce((m, l) => Math.max(m, l.length), 1);
    let height = PAD * 2 + maxRows * H + (maxRows - 1) * GY;
    const width = PAD * 2 + L * W + (L - 1) * GX;
    const xy = new Map();
    const layerOf = new Map();
    const layerX = (li) => PAD + li * (W + GX);
    layers.forEach((layer, li) => {
      const lh = layer.length * H + (layer.length - 1) * GY;
      const top = PAD + (height - PAD * 2 - lh) / 2;
      layer.forEach((j, i) => { xy.set(j.n, { x: layerX(li), y: top + i * (H + GY) }); layerOf.set(j.n, li); });
    });
    // Edges: neighbouring layers get an S-curve; an edge that skips layers runs in a lane above or below the
    // nodes it passes, so it never looks like it goes through one of them.
    const byN = new Map(jobs.map((j) => [j.n, j]));
    const lanes = { above: 0, below: 0 };
    const edgeSpecs = [];
    let minY = 0;
    let maxY = height;
    for (const j of jobs) {
      for (const a of Array.isArray(j.after) ? j.after : []) {
        const from = xy.get(a);
        const to = xy.get(j.n);
        if (!from || !to) continue;
        const done = liveJobStage(p, byN.get(a)) === 'merged';
        const x1 = from.x + W;
        const y1 = from.y + H / 2;
        const x2 = to.x - 2;
        const y2 = to.y + H / 2;
        const la = layerOf.get(a);
        const lj = layerOf.get(j.n);
        let d;
        if (lj - la <= 1) {
          const dx = Math.max(16, (x2 - x1) * 0.5);
          d = 'M' + x1 + ',' + y1 + ' C' + (x1 + dx) + ',' + y1 + ' ' + (x2 - dx) + ',' + y2 + ' ' + x2 + ',' + y2;
        } else {
          let top = Infinity;
          let bottom = -Infinity;
          for (let li = la + 1; li < lj; li++) for (const n of layers[li].map((x) => x.n)) { const q = xy.get(n); top = Math.min(top, q.y); bottom = Math.max(bottom, q.y + H); }
          const below = (y1 + y2) / 2 >= height / 2;
          const laneY = below ? bottom + 10 + 7 * lanes.below++ : top - 10 - 7 * lanes.above++;
          minY = Math.min(minY, laneY - 6);
          maxY = Math.max(maxY, laneY + 6);
          const ax = layerX(la + 1) - GX * 0.35;
          const bx = layerX(lj - 1) + W + GX * 0.35;
          const d1 = Math.max(10, (ax - x1) * 0.6);
          const d2 = Math.max(10, (x2 - bx) * 0.6);
          d = 'M' + x1 + ',' + y1 + ' C' + (x1 + d1) + ',' + y1 + ' ' + (ax - d1) + ',' + laneY + ' ' + ax + ',' + laneY +
            ' L' + bx + ',' + laneY + ' C' + (bx + d2) + ',' + laneY + ' ' + (x2 - d2) + ',' + y2 + ' ' + x2 + ',' + y2;
        }
        edgeSpecs.push({ d, done, from: a, to: j.n });
      }
    }
    const shift = -minY;
    height = maxY - minY;
    const svg = svgEl('svg', { class: 'pg-svg', width, height, viewBox: '0 0 ' + width + ' ' + height, role: 'group', 'aria-label': 'Job graph: ' + plural(jobs.length, 'job') + ' in ' + plural(layers.length, 'layer') });
    const defs = svgEl('defs');
    for (const kind of ['base', 'done']) {
      const mk = svgEl('marker', { id: 'pg-arrow-' + kind, viewBox: '0 0 10 10', refX: 8.5, refY: 5, markerWidth: 7, markerHeight: 7, orient: 'auto-start-reverse' });
      mk.appendChild(svgEl('path', { d: 'M1 1.5 L8.5 5 L1 8.5 z', class: 'pg-arrow' + (kind === 'done' ? ' done' : '') }));
      defs.appendChild(mk);
    }
    svg.appendChild(defs);
    const root = svgEl('g', { transform: 'translate(0,' + shift + ')' });
    svg.appendChild(root);
    const edges = svgEl('g', { class: 'pg-edges' });
    for (const e of edgeSpecs) {
      const path = svgEl('path', { class: 'pg-edge' + (e.done ? ' done' : ''), d: e.d, 'marker-end': 'url(#pg-arrow-' + (e.done ? 'done' : 'base') + ')' });
      path.dataset.from = String(e.from);
      path.dataset.to = String(e.to);
      edges.appendChild(path);
    }
    root.appendChild(edges);
    const font = '600 12px ' + getComputedStyle(document.body).fontFamily;
    for (const j of jobs) {
      const at = xy.get(j.n);
      if (!at) continue;
      const stage = liveJobStage(p, j);
      const tone = stage ? NODE_TONE[stage] || 'neutral' : 'pending';
      const label = stage ? stageLabel(stage) : 'not started';
      const g = svgEl('g', { class: 'pg-node tone-' + tone + (ACTIVE.has(stage) && stage !== 'queued' ? ' live' : ''), transform: 'translate(' + at.x + ',' + at.y + ')', tabindex: '0', role: 'button',
        'aria-label': 'Job ' + j.n + ': ' + j.feature + '. ' + label + (j.job_id ? ', ' + j.job_id + '. Opens the job.' : '.') });
      g.dataset.n = String(j.n);
      if (j.job_id) g.dataset.job = p.repo + '/' + j.job_id;
      const title = svgEl('title');
      title.textContent = '#' + j.n + ' ' + j.feature + (j.after && j.after.length ? '\nafter: ' + j.after.map((n) => '#' + n).join(', ') : '') + (j.job_id ? '\n' + j.job_id + ' · ' + label : '\nnot submitted yet') + (j.why ? '\nfrom final review ' + j.review + ': ' + j.why : '');
      g.appendChild(title);
      g.appendChild(svgEl('rect', { class: 'pg-box', width: W, height: H, rx: 8 }));
      g.appendChild(svgEl('rect', { class: 'pg-bar', x: 0, y: 0, width: 4, height: H, rx: 2 }));
      const num = svgEl('text', { class: 'pg-num', x: 14, y: 19 });
      num.textContent = '#' + j.n + (j.review ? ' · review ' + j.review : '');
      g.appendChild(num);
      const st = svgEl('text', { class: 'pg-stage', x: W - 10, y: 19, 'text-anchor': 'end' });
      st.textContent = label;
      g.appendChild(st);
      wrapLines(j.feature || '', W - 26, font, 2).forEach((line, i) => {
        const t = svgEl('text', { class: 'pg-feat', x: 14, y: 38 + i * 16 });
        t.textContent = line;
        g.appendChild(t);
      });
      const open = () => {
        if (!j.job_id) { toast('Job ' + j.n + ' has not been submitted yet' + (j.after && j.after.length ? ': it starts once ' + j.after.map((n) => '#' + n).join(', ') + ' merged.' : '.'), { kind: 'info' }); return; }
        D.back = location.hash;
        D.opener = g;
        location.hash = '#/job/' + enc(p.repo) + '/' + enc(j.job_id);
      };
      g.addEventListener('click', open);
      g.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); } });
      root.appendChild(g);
    }
    return svg;
  }
  function graphLegend() {
    const item = (cls, label) => h('span', { class: 'pg-legend-item' }, h('span', { class: 'pg-swatch ' + cls }), label);
    return h('div', { class: 'pg-legend', 'aria-hidden': 'true' }, item('tone-pending', 'not started'), item('tone-accent', 'in the line'), item('tone-ok', 'merged'), item('tone-human', 'needs you'), item('tone-err', 'failed'), item('tone-muted', 'rejected / cancelled'));
  }

  // ---------------------------------------------------------------- plan detail
  async function planPost(p, action, body) {
    const since = S.lastSeq;
    const r = await api(planPath(p.repo, p.id) + '/' + action, { method: 'POST', body: body || {} });
    if (r && r.plan) upsertPlan(r.plan, 0, since);
    return r;
  }
  async function planAction(p, action, button) {
    if (action === 'cancel') {
      await confirmModal({
        title: 'Cancel plan ' + p.id + '?',
        description: 'Stops the lead and cancels the plan’s jobs that are still in the line. Merged jobs stay merged; cancelled ones keep their worktrees until you reject them.',
        confirm: 'Cancel plan', cls: 'btn-danger-solid', icon: 'stop',
        onSubmit: async () => {
          try { await planPost(p, 'cancel'); } catch (e) { actionError(e); throw e; }
          toast('Plan ' + p.id + ' cancelled.', { kind: 'ok' });
        },
      });
      return;
    }
    if (action === 'edit') { startPlanEdit(p); return; }
    setBusy(button, true);
    try {
      await planPost(p, action);
      if (action === 'run') {
        const ready = (p.jobs || []).filter((j) => !(j.after && j.after.length)).length;
        toast(['Plan ', h('strong', { class: 'mono' }, p.id), ' is running: ' + plural(ready, 'job') + ' start now, the rest as their dependencies merge.'], { kind: 'ok' });
      } else toast('Retrying plan ' + p.id + (p.failed_step ? ' from ' + p.failed_step : '') + '.', { kind: 'ok' });
    } catch (e) {
      actionError(e);
    } finally { setBusy(button, false); }
  }
  function startPlanEdit(p) {
    const k = planKey(p);
    const text = planDetail(k).texts.get('plan.md') || '';
    PL.edit = { key: k, text, orig: text, error: null, saving: false, ta: null, focus: true, restored: false };
    invalidate('plans');
  }
  async function cancelPlanEdit() {
    const ed = PL.edit;
    if (!ed) return;
    if (planEditDirty(ed)) {
      const ok = await confirmModal({ title: 'Discard your plan.md edits?', description: 'The changes you typed are not saved and will be lost.', confirm: 'Discard edits', cls: 'btn-danger-solid', icon: 'x' });
      if (!ok || PL.edit !== ed) return;
    }
    PL.edit = null;
    invalidate('plans');
  }
  /** The lead's questions answered inline: the answers go under each question in plan.md (POST …/plan, a
   *  PLAN: QUESTIONS text is accepted as answers while the plan waits on them), then Retry re-plans. */
  async function sendPlanAnswers(p, items, fields, button) {
    const k = planKey(p);
    const answers = fields.map((ta) => ta.value);
    if (!answers.some((a) => a.trim())) { toast('Write at least one answer first.', { kind: 'info' }); if (fields[0]) fields[0].focus(); return; }
    const text = planDetail(k).texts.get('plan.md');
    if (text === undefined) return;
    setBusy(button, true);
    try {
      const next = withAnswers(text, answers);
      await planPost(p, 'plan', { text: next });
      planDetail(k).texts.set('plan.md', next);
      PL.answers.delete(k);
      await planPost(p, 'retry');
      toast(['Answers sent: the lead of ', h('strong', { class: 'mono' }, p.id), ' is planning again with them.'], { kind: 'ok' });
      loadPlanDetail(k);
    } catch (e) {
      actionError(e);
      loadPlanDetail(k);
    } finally { setBusy(button, false); }
  }
  async function savePlanEdit(p) {
    const ed = PL.edit;
    if (!ed || ed.saving) return;
    if (!ed.text.trim()) { ed.error = 'plan.md cannot be empty.'; invalidate('plans'); return; }
    ed.saving = true;
    ed.error = null;
    invalidate('plans');
    try {
      await planPost(p, 'plan', { text: ed.text });
      planDetail(ed.key).texts.set('plan.md', ed.text);
      if (PL.edit === ed) PL.edit = null;
      toast('plan.md saved.', { kind: 'ok' });
      loadPlanDetail(planKey(p));
    } catch (e) {
      ed.error = (e.status === 409 ? 'Not allowed now: ' : e.status === 400 ? 'The plan does not parse: ' : 'Not saved: ') + e.message;
    } finally {
      if (PL.edit === ed) ed.saving = false;
      invalidate('plans');
    }
  }
  function planSection(title, iconName, extra, body, cls) {
    return h('section', { class: 'plan-sec' + (cls ? ' ' + cls : '') }, h('div', { class: 'plan-sec-head' }, icon(iconName, 'icon-sm'), h('h3', { class: 'plan-sec-title' }, title), h('span', { class: 'spacer' }), extra || null), h('div', { class: 'plan-sec-body' }, body));
  }
  function planDetailView(k) {
    const p = S.plans.get(k);
    const d = planDetail(k);
    const [repo, id] = splitKey(k);
    if (!d.loaded && !d.loading && !d.error) loadPlanDetail(k);
    const back = h('a', { class: 'btn btn-ghost btn-icon btn-sm plan-back', href: '#/plans', 'aria-label': 'Back to plans' }, icon('chevron-left'));
    if (!p) {
      return h('article', { class: 'plan-detail', 'aria-label': 'Plan ' + id },
        h('div', { class: 'plan-head' }, back, h('h2', { class: 'plan-title' }, d.notFound ? 'Plan not found' : 'Loading…')),
        d.error && !d.notFound ? callout('err', 'alert', 'Could not load the plan.', d.error) : skeletonLines(6));
    }
    const art = h('article', { class: 'plan-detail', 'aria-label': 'Plan ' + p.id, dataset: { plan: k } });
    const lines = String(p.request || '').trim().split('\n');
    const head = h('header', { class: 'plan-head' },
      h('div', { class: 'plan-head-row' }, back, planStagePill(p.stage), h('span', { class: 'plan-id' }, p.id), h('span', { class: 'tag' }, p.repo), timeEl(p.updated_at, 'muted')),
      h('h2', { class: 'plan-title', title: p.request || '' }, firstLine(p.request) || '(no request)'));
    if (lines.length > 1) head.appendChild(h('details', { class: 'plan-request' }, h('summary', null, icon('chevron', 'icon-xs chev'), 'Full request'), MD.render(p.request, 'md-sm')));
    const meta = h('div', { class: 'meta-row' },
      h('span', { class: 'meta-item', title: 'Base branch and the commit the plan started from' }, icon('branch', 'icon-xs'), h('span', { class: 'mono' }, (p.base || 'base') + (p.base_commit ? ' @ ' + String(p.base_commit).slice(0, 8) : ''))),
      h('span', { class: 'meta-item', title: 'Routing for the lead and every job of the plan' }, icon('route', 'icon-xs'), planRoutingLabel(p)),
      fmtUsage(p.usage) ? h('span', { class: 'meta-item', title: 'Lead usage: ' + fmtUsage(p.usage, true) }, icon('zap', 'icon-xs'), fmtUsage(p.usage)) : null,
      extNodes('planHead', p));
    head.appendChild(meta);
    if (p.stage === 'planning' || p.stage === 'final-review') {
      const wait = waitingOf('plan:' + k);
      head.appendChild(h('div', { class: 'meta-row lead-row' + (wait ? ' wait-row' : ''), role: 'status' },
        wait ? icon('hourglass', 'icon-sm') : h('span', { class: 'spinner', 'aria-hidden': 'true' }),
        wait ? 'The lead is ' + waitWords(wait).short + '…' : p.stage === 'planning' ? 'The lead is reading the repo and writing plan.md…' : 'The lead is reviewing the combined change…'));
    }
    const planText = d.texts.get('plan.md');
    const asking = planText !== undefined && planMdStatus(planText) === 'QUESTIONS';
    // The lead's questions wait on a human: answered inline below (the questions card says why it waits).
    const answerable = asking && p.stage === 'needs-human' && p.failed_step === 'plan';
    if (p.reason && (p.stage === 'needs-human' || p.stage === 'failed' || p.stage === 'blocked') && !answerable) {
      head.appendChild(callout(p.stage === 'failed' ? 'err' : 'human', p.stage === 'failed' ? 'error' : 'user',
        p.stage === 'failed' ? 'Failed' + (p.failed_step ? ' at ' + p.failed_step : '') + ':' : p.stage === 'blocked' ? 'Blocked:' : 'Needs you:', String(p.reason)));
    }
    const editing = PL.edit && PL.edit.key === k;
    const acts = [];
    const act = (label, action, cls, ic, title) => {
      const b = btn(label, { cls: cls + ' btn-sm', icon: ic, dataset: { action }, title });
      b.addEventListener('click', () => planAction(p, action, b));
      acts.push(b);
    };
    if (p.stage === 'ready') act('Run plan', 'run', 'btn-primary', 'play', 'Submit every job whose dependencies have merged');
    if ((p.stage === 'failed' || p.stage === 'needs-human') && p.failed_step) act('Retry', 'retry', p.stage === 'failed' ? 'btn-primary' : '', 'refresh', 'Run the ' + p.failed_step + ' step again');
    if (PLAN_EDITABLE.has(p.stage) && planText !== undefined && !editing) act(asking ? 'Edit plan.md' : 'Edit plan', 'edit', '', 'edit', asking ? 'Edit the raw plan.md (the questions as the lead wrote them)' : 'Edit plan.md (jobs, order, context)');
    if (p.stage !== 'done' && p.stage !== 'cancelled') act('Cancel plan', 'cancel', 'btn-danger', 'stop', 'Stop the lead and cancel the plan’s active jobs');
    if (acts.length) head.appendChild(h('div', { class: 'actions' }, acts));
    art.appendChild(head);
    // The lead's questions, prominently: they block the plan until a human answers them, right here.
    if (asking) {
      const items = planQuestionItems(planText);
      const typed = PL.answers.get(k) || null;
      const fields = [];
      const send = btn('Send answers & re-plan', { cls: 'btn-primary btn-sm', icon: 'send', dataset: { action: 'send-answers' }, title: 'Write your answers under the questions in plan.md, then Retry: the lead plans again with them' });
      const list = items.length ? h('ol', { class: 'plan-q-list' }, items.map((it, i) => {
        const li = h('li', { class: 'plan-q-item' });
        li.appendChild(MD.render(it.q, 'md-sm'));
        if (answerable) {
          const ta = h('textarea', { class: 'textarea plan-q-answer', rows: 2, placeholder: 'Your answer (leave empty to let the lead decide)', 'aria-label': 'Answer to question ' + (i + 1), dataset: { q: String(i) } });
          // Typed answers survive re-renders, keyed by the question (a re-plan may ask different ones).
          ta.value = typed && typed.has(it.q) ? typed.get(it.q) : it.a;
          ta.addEventListener('input', () => {
            const cur = PL.answers.get(k) || new Map();
            cur.set(it.q, ta.value);
            PL.answers.set(k, cur);
            autosize(ta);
          });
          ta.addEventListener('keydown', (e) => { if ((e.key === 'Enter' || e.key.toLowerCase() === 's') && (e.metaKey || e.ctrlKey)) { e.preventDefault(); send.click(); } });
          fields.push(ta);
          li.appendChild(ta);
        } else if (it.a) li.appendChild(h('div', { class: 'plan-q-given' }, h('strong', null, 'Your answer: '), it.a));
        return li;
      })) : null;
      send.addEventListener('click', () => sendPlanAnswers(p, items, fields, send));
      const answered = items.some((x) => x.a);
      art.appendChild(h('section', { class: 'plan-questions', role: 'region', 'aria-label': 'The lead’s questions' },
        h('div', { class: 'plan-q-head' }, h('span', { class: 'plan-q-icon' }, icon('help', 'icon-lg')),
          h('div', null, h('h3', { class: 'plan-q-title' }, 'The lead has ' + (items.length ? plural(items.length, 'question') : 'questions') + ' before it can plan'),
            h('p', { class: 'plan-q-sub' }, answerable
              ? (answered ? 'Your answers are saved in plan.md. Change them if you like, then send: the lead plans again with them.' : 'Answer what you can and send: your answers go under each question in plan.md and the lead plans again with them.')
              : 'The plan waits for these to be answered.'))),
        list,
        answerable && items.length ? h('div', { class: 'plan-q-foot' },
          h('span', { class: 'muted plan-edit-keys' }, h('kbd', null, 'Ctrl'), '+', h('kbd', null, 'Enter'), ' sends'), h('span', { class: 'spacer' }), send) : null));
    }
    // Job graph. A phone gets the list first: the graph is wider than the screen, so it is one tap away.
    const jobs = Array.isArray(p.jobs) ? p.jobs : [];
    const pr = planProgress(p);
    if (jobs.length) {
      const narrow = !!(window.matchMedia && window.matchMedia('(max-width: 640px)').matches);
      const scroller = h('div', { class: 'pg-scroll' });
      const list = h('ol', { class: 'pg-list' }, jobs.map((j) => {
        const stage = liveJobStage(p, j);
        return h('li', null, h('span', { class: 'pg-list-n' }, '#' + j.n), h('span', { class: 'pg-list-feat' }, j.feature, j.after && j.after.length ? h('span', { class: 'muted' }, '  after ' + j.after.map((n) => '#' + n).join(', ')) : null),
          j.job_id ? h('a', { class: 'mono pg-list-job', href: '#/job/' + enc(p.repo) + '/' + enc(j.job_id), onclick: () => { D.back = location.hash; } }, j.job_id) : h('span', { class: 'muted' }, 'not started'),
          stage ? stagePill(stage) : null);
      }));
      const meta = h('span', { class: 'plan-sec-meta' }, pr.merged + '/' + pr.total + ' merged');
      if (narrow) {
        const det = h('details', { class: 'pg-list-wrap pg-graph-wrap', open: !!PL.graphOpen }, h('summary', null, icon('chevron', 'icon-xs chev'), 'As a graph'), scroller, graphLegend());
        det.addEventListener('toggle', () => { if (PL.graphOpen !== det.open) { PL.graphOpen = det.open; invalidate('plans'); } });
        art.appendChild(planSection('Jobs', 'plan', meta, [list, det], 'plan-sec-graph is-narrow'));
        if (PL.graphOpen) scroller._plan = p;
      } else {
        art.appendChild(planSection('Jobs', 'plan', meta,
          [scroller, graphLegend(), h('details', { class: 'pg-list-wrap' }, h('summary', null, icon('chevron', 'icon-xs chev'), 'As a list'), list)], 'plan-sec-graph'));
        scroller._plan = p;
      }
    } else if (p.stage === 'planning') {
      art.appendChild(planSection('Jobs', 'plan', null, h('div', { class: 'plan-placeholder' }, skeletonLines(3), h('p', { class: 'muted' }, 'The job graph appears once the lead has written plan.md.'))));
    }
    // plan.md: rendered (its status line is the badge, shown while the plan can still change), or the editor.
    let planBody;
    const status = planText !== undefined ? planMdStatus(planText) : null;
    const statusTag = planText === undefined ? null
      : !status ? h('span', { class: 'tag tag-mono tag-err', title: 'The first line must be PLAN: READY or PLAN: QUESTIONS' }, 'no status line')
        : PLAN_EDITABLE.has(p.stage) && !asking ? h('span', { class: 'tag tag-mono tag-ok', title: 'plan.md’s first line' }, 'PLAN: ' + status) : null;
    if (editing) {
      const ed = PL.edit;
      if (!ed.ta) {
        ed.ta = h('textarea', { class: 'textarea mono-input plan-editor', rows: 18, spellcheck: 'false', 'aria-label': 'plan.md' });
        ed.ta.value = ed.text;
        ed.ta.addEventListener('input', () => { const was = planEditDirty(ed); ed.text = ed.ta.value; if (was !== planEditDirty(ed)) invalidate('plans'); });
        // Ctrl+S saves, like the agent editor (Ctrl+Enter too).
        ed.ta.addEventListener('keydown', (e) => { if ((e.key === 'Enter' || e.key.toLowerCase() === 's') && (e.metaKey || e.ctrlKey)) { e.preventDefault(); savePlanEdit(p); } });
      }
      const save = btn('Save plan', { cls: 'btn-primary btn-sm', icon: 'check', dataset: { action: 'save-plan' } });
      if (ed.saving) setBusy(save, true);
      save.addEventListener('click', () => savePlanEdit(p));
      const cancel = btn('Cancel', { cls: 'btn-sm btn-ghost', dataset: { action: 'cancel-edit' }, onClick: () => cancelPlanEdit() });
      const changed = planText !== undefined && ed.orig !== planText;
      planBody = [
        ed.restored ? callout(changed ? 'warn' : 'info', changed ? 'alert' : 'info', 'Draft restored.', changed ? 'plan.md changed on the server since you started editing: saving replaces it with your text.' : 'Your unsaved edit from before; nothing was lost.') : null,
        h('p', { class: 'field-hint plan-edit-hint' }, asking ? ['Answer under each question as an indented ', h('code', null, '**Answer:** …'), ' line and save, then Retry. Or replace it with a PLAN: READY plan.']
          : ['The first line is PLAN: READY. Under ## Jobs, one job per line: ', h('code', null, '3. Add the export button (after: 1, 2)'), '. Each job must fit one builder run; unchanged lines keep their jobs.']),
        ed.ta,
        ed.error ? h('div', { class: 'form-error', role: 'alert' }, icon('alert', 'icon-sm'), h('span', null, ed.error)) : null,
        h('div', { class: 'plan-edit-foot' }, h('span', { class: 'muted plan-edit-keys' }, h('kbd', null, /Mac|iPhone|iPad/.test(navigator.platform || '') ? '⌘' : 'Ctrl'), '+', h('kbd', null, 'S'), ' saves'), h('span', { class: 'spacer' }), cancel, save),
      ];
    } else if (planText !== undefined) {
      const body = stripStatusLine(planText);
      // With questions, the card above already shows them: plan.md as written stays one click away.
      planBody = asking ? h('details', { class: 'pg-list-wrap plan-raw' }, h('summary', null, icon('chevron', 'icon-xs chev'), 'plan.md as the lead wrote it'), MD.render(body)) : MD.render(body);
    } else if (d.errs.has('plan.md')) {
      planBody = callout('err', 'alert', 'Could not load plan.md.', d.errs.get('plan.md'));
    } else {
      planBody = d.loaded ? h('p', { class: 'muted' }, p.stage === 'planning' ? 'The lead has not written plan.md yet.' : 'No plan.md.') : skeletonLines(6);
    }
    art.appendChild(planSection('plan.md', 'file', statusTag, planBody, editing ? 'is-editing' : ''));
    // Final reviews
    const reviews = Array.isArray(p.reviews) ? p.reviews : [];
    if (reviews.length || p.stage === 'final-review') {
      const cards = reviews.map((rv) => {
        const rel = 'review-' + rv.k + '.md';
        const text = d.texts.get(rel);
        return h('div', { class: 'plan-review', dataset: { review: String(rv.k) } },
          h('div', { class: 'plan-review-head' }, h('span', { class: 'plan-review-title' }, 'Final review ' + rv.k), verdictTag(rv.verdict), h('span', { class: 'spacer' }), h('span', { class: 'mono muted' }, rel)),
          text !== undefined ? MD.render(stripReviewHead(text), 'md-sm') : d.errs.has(rel) ? callout('err', 'alert', 'Could not load ' + rel + '.', d.errs.get(rel)) : skeletonLines(4));
      });
      if (p.stage === 'final-review') cards.push(h('div', { class: 'plan-review pending' }, h('span', { class: 'spinner', 'aria-hidden': 'true' }), 'Final review ' + (reviews.length + 1) + ': the lead is reading the combined diff…'));
      art.appendChild(planSection('Final reviews', 'check-circle', null, cards));
    }
    return art;
  }
  RENDER.plans = function () {
    if (S.route.view !== 'plans') return;
    const root = document.getElementById('plans-root');
    const main = document.getElementById('main');
    const y = main.scrollTop;
    const ed = PL.edit;
    const edState = ed && ed.ta && document.activeElement === ed.ta ? [ed.ta.selectionStart, ed.ta.selectionEnd, ed.ta.scrollTop] : null;
    const answerFocus = document.activeElement && document.activeElement.classList && document.activeElement.classList.contains('plan-q-answer')
      ? [document.activeElement.dataset.q, document.activeElement.selectionStart, document.activeElement.selectionEnd] : null;
    const oldScroll = root.querySelector('.pg-scroll');
    const graphX = oldScroll ? oldScroll.scrollLeft : 0;
    const focused = document.activeElement && root.contains(document.activeElement) ? (document.activeElement.dataset.action || document.activeElement.dataset.plan || (document.activeElement.dataset.n ? 'n:' + document.activeElement.dataset.n : null)) : null;
    clear(root);
    const newBtn = btn('New plan', { cls: 'btn-primary btn-sm', icon: 'plus', onClick: () => openNewPlan(), dataset: { action: 'new-plan' } });
    root.appendChild(viewHead('Plans', 'For work bigger than one job: a lead agent splits the request into factory jobs, runs each once the jobs it depends on have merged, then reviews the combined change.', newBtn));
    if (!PL.loaded) {
      if (PL.error) root.appendChild(emptyState('alert', 'Cannot load plans', PL.error, [btn('Retry', { cls: 'btn-primary', icon: 'refresh', onClick: () => loadPlans() })]));
      else root.appendChild(skeletonLines(8));
      return;
    }
    const plans = visiblePlans();
    if (!plans.length && !PL.sel) {
      root.appendChild(emptyState('plan', S.filterRepo ? 'No plans in ' + S.filterRepo : 'No plans yet',
        'Describe something too big for one job (a feature with a migration, an API and a UI). The lead writes a plan you can read and edit before anything runs.',
        [btn('New plan', { cls: 'btn-primary', icon: 'plus', onClick: () => openNewPlan() }), btn('Step by step', { icon: 'route', onClick: () => startFlowWith({ what: 'plan' }) })]));
      return;
    }
    const list = h('nav', { class: 'plan-list', 'aria-label': 'Plans' }, plans.length ? plans.map(planItem) : h('p', { class: 'muted' }, 'No plans in this repo.'));
    const detail = PL.sel ? planDetailView(PL.sel) : h('div', { class: 'plan-pick' }, emptyState('plan', 'Pick a plan', 'Its job graph, plan.md and final reviews show here.', null, true));
    root.appendChild(h('div', { class: 'plans-layout' + (PL.sel ? ' has-detail' : '') }, list, detail));
    const scroller = root.querySelector('.pg-scroll');
    if (scroller && scroller._plan) {
      scroller.appendChild(planGraph(scroller._plan, scroller.clientWidth || 800));
      scroller.scrollLeft = graphX;
      trackFades(scroller);
    }
    main.scrollTop = y;
    if (ed && ed.ta && ed.ta.isConnected && (edState || ed.focus)) {
      ed.ta.focus({ preventScroll: !ed.focus });
      if (edState) { ed.ta.setSelectionRange(edState[0], edState[1]); ed.ta.scrollTop = edState[2]; }
      ed.focus = false;
    } else if (answerFocus) {
      const el = root.querySelector('.plan-q-answer[data-q="' + CSS.escape(String(answerFocus[0])) + '"]');
      if (el) { el.focus({ preventScroll: true }); el.setSelectionRange(answerFocus[1], answerFocus[2]); }
    } else if (focused) {
      const el = focused.indexOf('n:') === 0 ? root.querySelector('.pg-node[data-n="' + CSS.escape(focused.slice(2)) + '"]')
        : root.querySelector('[data-action="' + CSS.escape(focused) + '"], .plan-item[data-plan="' + CSS.escape(focused) + '"]');
      if (el) el.focus({ preventScroll: true });
    }
  };
  async function openNewPlan() {
    if (document.querySelector('dialog[open]')) return;
    if (!S.repos.length) { noRepoModal('A plan'); return; }
    const last = lsGet('sf.lastRepo');
    const def = S.filterRepo || (S.repos.some((r) => r.name === last) ? last : S.repos[0].name);
    const repoSel = selectEl(S.repos.map((r) => [r.name, r.name]), def, { id: 'np-repo' });
    const ta = h('textarea', { class: 'textarea', id: 'np-request', rows: 8, spellcheck: 'true',
      placeholder: 'The whole change, in your words: what it should do, for whom, and what must keep working. e.g. Audit log v2: searchable, exportable, with 180-day retention.' });
    const routing = routingChooser({
      includeDefault: true, steps: () => ['lead'].concat(factorySteps()), base: savedRoutes,
      customHint: 'For this plan only: the lead and every job it submits run on these routes.',
      presetHint: 'For this plan and its jobs only: your saved settings stay as they are.',
    });
    const delivery = deliveryChooser({ id: 'np-delivery', hint: 'For every job of this plan.' });
    const fx = formExt('plan', { repo: () => repoSel.value, routing: () => chooserRouting(routing), watch: [repoSel, routing.el] });
    const lead = agentDef('lead') ? modelOf('lead') : null;
    await openModal({
      title: 'New plan',
      description: 'For work bigger than one job. The lead' + (lead ? ' (' + lead.model + ')' : '') + ' reads the repo and writes plan.md: you read and edit it before anything runs.',
      body: [field('Repository', repoSel), field('Request', ta, 'The lead may answer with questions first; you answer them in plan.md.'), routing.el, fx.nodes, delivery.el],
      hint: [h('kbd', null, /Mac|iPhone|iPad/.test(navigator.platform || '') ? '⌘' : 'Ctrl'), '+', h('kbd', null, 'Enter'), ' starts planning'],
      actions: [{ label: 'Cancel', value: 'cancel' }, { label: 'Start planning', value: 'ok', primary: true, icon: 'plan' }],
      dismissable: false, initialFocus: ta,
      onOpen: (ctl) => ta.addEventListener('keydown', (e) => { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); ctl.submit('ok'); } }),
      onSubmit: async () => {
        const request = ta.value.trim();
        if (!request) { ta.focus(); throw new Error('Describe the change first.'); }
        const route = routing.value();
        const body = { repo: repoSel.value, request };
        if (route !== undefined) body.routing = route;
        const out = delivery.value();
        if (out !== undefined) body.delivery = out;
        fx.apply(body, 'ok');
        lsSet('sf.lastRepo', repoSel.value);
        const since = S.lastSeq;
        const r = await api('/api/plans', { method: 'POST', body });
        const plan = r && r.plan;
        if (!plan) throw new Error('The server returned no plan.');
        upsertPlan(plan, 0, since);
        toast(['Plan ', h('strong', { class: 'mono' }, plan.id), ' created: the lead is planning.'], { kind: 'ok' });
        location.hash = '#/plans/' + enc(plan.repo) + '/' + enc(plan.id);
        return plan;
      },
    });
  }

  // ================================================================ agents view
  const AG = { sel: null, cache: new Map(), errors: new Map() };
  const AGENT_ORDER = ['spec-writer', 'builder', 'approver', 'lead', 'coder', 'repo-scout'];
  const WRITABLE_AGENT_RE = /^[a-z0-9][a-z0-9-]{0,40}$/;
  const SOURCE_UI = {
    builtin: { label: 'built-in', cls: 'tag-outline', title: 'Shipped in agents/ (edit to create a user override)' },
    user: { label: 'user', cls: 'tag-accent', title: 'Your override in ~/.config/software-factory/agents/ (Reset brings the shipped file back)' },
    repo: { label: 'repo', cls: 'tag-warn', title: 'From a repo’s .factory/agents/ (it wins over user overrides)' },
  };
  function sortedAgents() {
    const rank = (n) => {
      if (n === 'spec-writer') return 0;
      if (n === 'builder') return 1;
      if (/^reviewer-/.test(n)) return 2;
      const i = AGENT_ORDER.indexOf(n);
      return i >= 0 ? 3 + i : 10;
    };
    return S.agents.slice().sort((a, b) => rank(a.name) - rank(b.name) || a.name.localeCompare(b.name));
  }
  function listify(v) {
    if (Array.isArray(v)) return v.map(String).filter(Boolean);
    if (typeof v === 'string') return v.replace(/^\[|\]$/g, '').split(',').map((s) => s.trim()).filter(Boolean);
    return [];
  }
  function viewHead(title, sub, actions) {
    return h('div', { class: 'view-head' }, h('div', null, h('h1', { class: 'view-title' }, title), sub ? h('p', { class: 'view-sub' }, sub) : null), h('span', { class: 'spacer' }), actions || null);
  }
  async function loadAgent(name, force) {
    if (AG.cache.has(name) && !force) return;
    try {
      const r = await api('/api/agents/' + enc(name));
      AG.cache.set(name, r || {});
      AG.errors.delete(name);
    } catch (e) {
      AG.errors.set(name, e.message);
    }
    invalidate('agents');
  }
  /** builtin / user / repo: from the agent's detail, else config_view.agent_list. */
  function agentSource(name) {
    const d = AG.cache.get(name);
    if (d && d.source) return String(d.source);
    const a = CFG.orig && Array.isArray(CFG.orig.agent_list) ? CFG.orig.agent_list.find((x) => x && x.name === name) : null;
    return a && a.source ? String(a.source) : null;
  }
  function sourceTag(name) {
    const src = agentSource(name);
    if (!src) return null;
    const ui = SOURCE_UI[src] || { label: src, cls: 'tag-outline', title: '' };
    return h('span', { class: 'tag ' + ui.cls + ' src-tag', title: 'Source: ' + ui.title, dataset: { source: src } }, icon('file', 'icon-xs'), ui.label);
  }
  const lensOf = (name) => (/^reviewer-[a-z0-9][a-z0-9-]*$/.test(name) ? name.slice(9) : null);
  /** Agents, settings and the strip all follow what the server now says. */
  function afterAgentChange(name) {
    if (name) AG.cache.delete(name);
    refreshState();
    if (CFG.orig && !configDirty()) loadConfig(); else invalidate('agents');
    if (name) loadAgent(name, true);
  }
  /** POST /api/config {pipeline: {reviewers}} and adopt the answer (keeps unsaved edits in other sections). */
  async function saveReviewers(list) {
    const r = await api('/api/config', { method: 'POST', body: { pipeline: { reviewers: list } } });
    adoptConfig(r, ['pipeline']);
    await refreshState();
    return r;
  }
  /** Append a lens to pipeline.reviewers. Lists replace whole in a config patch, so the list is read from the
   *  server just before (another tab or Settings may have changed it), and unsaved Pipeline edits are confirmed. */
  async function addToPipeline(lens, button) {
    if (sectionDirty('pipeline')) {
      const ok = await confirmModal({
        title: 'Add ' + lens + ' and discard your Pipeline edits?',
        description: 'Settings › Pipeline has unsaved changes. Adding a reviewer saves the pipeline now, from the saved settings, so those changes are discarded.',
        confirm: 'Add and discard', cls: 'btn-danger-solid', icon: 'plus',
      });
      if (!ok) return;
    }
    setBusy(button, true);
    try {
      const fresh = normalizeConfig(await api('/api/config'));
      const cur = fresh.pipeline && Array.isArray(fresh.pipeline.reviewers) ? fresh.pipeline.reviewers.map(String) : pipelineReviewers();
      if (cur.indexOf(lens) >= 0) { adoptConfig(fresh, ['pipeline']); await refreshState(); toast(lens + ' is already in the pipeline.', { kind: 'info' }); return; }
      await saveReviewers(cur.concat([lens]));
      toast(['Added ', h('strong', { class: 'mono' }, lens), ' to the pipeline: new jobs run ' + plural(cur.length + 1, 'reviewer') + '.'], { kind: 'ok', action: { label: 'See the line', href: '#/factory' } });
    } catch (e) {
      toast('Not added: ' + e.message, { kind: 'error' });
    } finally { setBusy(button, false); invalidate('agents'); }
  }
  function openAgentEditor(name) {
    const d = AG.cache.get(name);
    if (!d || typeof d.text !== 'string') { toast('Load the agent first.', { kind: 'info' }); return; }
    const src = d.source || agentSource(name);
    const ta = h('textarea', { class: 'textarea mono-input agent-editor', rows: 22, spellcheck: 'false', 'aria-label': 'Agent file ' + name });
    ta.value = d.text;
    const where = src === 'user' ? 'Saves your override ' + (d.path || name + '.md') + '.'
      : 'Saving creates your override ~/.config/software-factory/agents/' + name + '.md; the shipped file stays as it is (Reset brings it back).';
    openModal({
      title: 'Edit ' + name, wide: true,
      description: [where, ' Frontmatter (name, description, tools, model, skills, mcp, color) plus the prompt body; ', h('code', null, 'name:'), ' must stay ', h('code', null, name), '.'],
      body: ta, dismissable: false, initialFocus: ta,
      hint: [h('kbd', null, /Mac|iPhone|iPad/.test(navigator.platform || '') ? '⌘' : 'Ctrl'), '+', h('kbd', null, 'S'), ' saves'],
      actions: [{ label: 'Cancel', value: 'cancel' }, { label: 'Save agent', value: 'ok', primary: true, icon: 'check' }],
      onOpen: (ctl) => {
        // Editors save with Ctrl+S (Ctrl+Enter too), like plan.md.
        ta.addEventListener('keydown', (e) => { if ((e.key.toLowerCase() === 's' || e.key === 'Enter') && (e.metaKey || e.ctrlKey)) { e.preventDefault(); ctl.submit('ok'); } });
        // Open at the top: the frontmatter the description talks about comes first.
        ta.setSelectionRange(0, 0);
        ta.scrollTop = 0;
        requestAnimationFrame(() => { ta.scrollTop = 0; });
      },
      onSubmit: async () => {
        if (!ta.value.trim()) throw new Error('The agent file cannot be empty.');
        const r = await api('/api/agents/' + enc(name), { method: 'POST', body: { text: ta.value } });
        if (r && r.agent) AG.cache.set(name, r.agent);
        toast(['Saved ', h('strong', { class: 'mono' }, name), src === 'user' ? '.' : ' as your override.'], { kind: 'ok' });
        afterAgentChange(name);
        return true;
      },
    });
  }
  function openNewAgent(fromName) {
    if (document.querySelector('dialog[open]')) return;
    const names = sortedAgents().map((a) => a.name);
    const nameIn = h('input', { class: 'input mono-input', type: 'text', id: 'na-name', maxlength: 41, placeholder: 'reviewer-perf', spellcheck: 'false', autocomplete: 'off' });
    const def = fromName && names.indexOf(fromName) >= 0 ? fromName : names.indexOf('reviewer-code') >= 0 ? 'reviewer-code' : names[0];
    const fromSel = selectEl(names.map((n) => [n, n]), def || '', { id: 'na-from' });
    const note = h('div', { class: 'field-hint' });
    const paint = () => {
      const n = nameIn.value.trim();
      const lens = lensOf(n);
      clear(note);
      if (n && !WRITABLE_AGENT_RE.test(n)) { note.className = 'field-error'; add(note, 'Use a-z, 0-9 and "-", start with a letter or digit, at most 41 characters.'); return; }
      note.className = 'field-hint';
      if (names.indexOf(n) >= 0) { note.className = 'field-error'; add(note, n + ' already exists: edit it instead.'); return; }
      if (lens) add(note, ['A review lens: copying a reviewer renames its review file to ', h('code', null, 'review-' + lens + '.md'), '. Add it to the pipeline afterwards to run it on every job.']);
      else add(note, 'Name a review lens reviewer-<lens> (e.g. reviewer-perf, reviewer-a11y) to use it in the pipeline.');
    };
    nameIn.addEventListener('input', paint);
    paint();
    openModal({
      title: 'New agent',
      description: 'A copy of an existing agent, saved as your override in ~/.config/software-factory/agents/. Edit its prompt next.',
      body: [field('Name', nameIn), note, field('Copy from', fromSel)],
      actions: [{ label: 'Cancel', value: 'cancel' }, { label: 'Create agent', value: 'ok', primary: true, icon: 'plus' }],
      initialFocus: nameIn,
      onSubmit: async () => {
        const name = nameIn.value.trim();
        if (!WRITABLE_AGENT_RE.test(name)) { nameIn.focus(); throw new Error('Use a-z, 0-9 and "-" for the name (it starts with a letter or digit).'); }
        const r = await api('/api/agents', { method: 'POST', body: { name, from: fromSel.value } });
        if (r && r.agent) AG.cache.set(name, r.agent);
        const lens = lensOf(name);
        toast(['Created ', h('strong', { class: 'mono' }, name), '.'], { kind: 'ok', action: lens && pipelineReviewers().indexOf(lens) < 0 ? { label: 'Add to pipeline', onClick: () => addToPipeline(lens, null) } : null });
        afterAgentChange(name);
        location.hash = '#/agents/' + enc(name);
        return true;
      },
    });
  }
  async function resetAgent(name) {
    const d = AG.cache.get(name) || {};
    const lens = lensOf(name);
    const inPipeline = lens && pipelineReviewers().indexOf(lens) >= 0;
    const drop = h('input', { type: 'checkbox', checked: true });
    await confirmModal({
      title: 'Reset ' + name + '?',
      description: 'Deletes your override' + (d.path ? ' ' + d.path : '') + '. A shipped or repo definition of ' + name + ' comes back; an agent that exists only as your file is removed.',
      body: inPipeline ? h('label', { class: 'check' }, drop, 'Also remove ' + lens + ' from the pipeline if no other definition of ' + name + ' remains (jobs cannot run a reviewer without its agent)') : null,
      confirm: 'Reset agent', cls: 'btn-danger-solid', icon: 'rework',
      onSubmit: async () => {
        const r = await api('/api/agents/' + enc(name) + '/reset', { method: 'POST' });
        const agent = r ? r.agent : null;
        if (agent) {
          AG.cache.set(name, agent);
          toast(['Reset ', h('strong', { class: 'mono' }, name), ' to its ' + (agent.source === 'repo' ? 'repo' : 'built-in') + ' definition.'], { kind: 'ok' });
        } else {
          // Gone: drop it here at once, so nothing asks the server for it before /api/state catches up.
          AG.cache.delete(name);
          AG.errors.delete(name);
          S.agents = S.agents.filter((x) => x.name !== name);
          if (AG.sel === name) AG.sel = null;
          if (inPipeline && drop.checked) {
            try { await saveReviewers(pipelineReviewers().filter((x) => x !== lens)); } catch (e) { toast('Removed the agent, but not from the pipeline: ' + e.message, { kind: 'error' }); }
          }
          toast(['Removed ', h('strong', { class: 'mono' }, name), ': it existed only as your file.'], { kind: 'ok' });
          location.hash = '#/agents';
        }
        afterAgentChange(agent ? name : null);
      },
    });
  }
  RENDER.agents = function () {
    if (S.route.view !== 'agents') return;
    const root = document.getElementById('agents-root');
    const y = document.getElementById('main').scrollTop;
    const focusedAction = document.activeElement && root.contains(document.activeElement) ? document.activeElement.dataset.action : null;
    clear(root);
    if (!CFG.orig && !CFG.loading && !CFG.loadError) ensureConfig().catch(() => {});
    root.appendChild(viewHead('Agents', 'The factory line’s agent definitions (Claude Code subagent format). Edit a prompt, add a review lens, or reset to the shipped file.',
      [h('a', { class: 'btn btn-sm', href: '#/settings/routing' }, icon('route', 'icon-sm'), 'Routing'),
        btn('New agent', { cls: 'btn-primary btn-sm', icon: 'plus', dataset: { action: 'new-agent' }, onClick: () => openNewAgent(AG.sel) })]));
    if (!S.loaded) { root.appendChild(skeletonLines(8)); return; }
    const agents = sortedAgents();
    if (!agents.length) { root.appendChild(emptyState('agents', 'No agents found', 'Agent files live in agents/*.md, ~/.config/software-factory/agents/ and <repo>/.factory/agents/.')); return; }
    // The route's agent may not be in /api/state yet (just created): show the first one until it is.
    const sel = AG.sel && agents.some((a) => a.name === AG.sel) ? AG.sel : agents[0].name;
    const lenses = pipelineReviewers();
    const list = h('div', { class: 'agent-list', role: 'list' });
    for (const a of agents) {
      const m = modelOf(a.name);
      const tools = listify(a.tools);
      const lens = lensOf(a.name);
      const card = h('a', { class: 'agent-card', href: '#/agents/' + enc(a.name), 'aria-current': a.name === sel ? 'true' : 'false', dataset: { agent: a.name } },
        h('span', { class: 'agent-card-top' }, h('span', { class: 'dot ' + agentColorCls(a.name) }), h('span', { class: 'agent-name' }, a.name), h('span', { class: 'spacer' }), sourceTag(a.name)),
        a.description ? h('span', { class: 'agent-desc' }, a.description) : null,
        h('span', { class: 'agent-meta' },
          h('span', { class: 'tag tag-accent tag-mono', title: 'Model reference' }, m.key || '?'),
          h('span', { class: 'tag tag-mono', title: m.provider ? 'Provider ' + m.provider : '' }, m.model),
          // The driver sits with the model it runs; the default (builtin) is not worth a chip.
          a.driver && a.driver !== 'builtin' ? h('span', { class: 'tag tag-outline', title: 'Runs through the ' + a.driver + ' driver' }, icon('terminal', 'icon-xs'), 'driver ' + a.driver) : null,
          tools.length ? h('span', { class: 'tag tag-outline', title: tools.join(', ') }, plural(tools.length, 'tool')) : null,
          lens ? h('span', { class: 'tag ' + (lenses.indexOf(lens) >= 0 ? 'tag-ok' : 'tag-warn'), title: lenses.indexOf(lens) >= 0 ? 'Runs on every job (pipeline.reviewers)' : 'Not in pipeline.reviewers: no job runs it' }, lenses.indexOf(lens) >= 0 ? 'in pipeline' : 'not in pipeline') : null));
      card.style.textDecoration = 'none';
      list.appendChild(h('div', { role: 'listitem' }, card));
    }
    const a = agents.find((x) => x.name === sel);
    const detail = AG.cache.get(sel);
    if (!detail && !AG.errors.has(sel)) loadAgent(sel);
    const m = modelOf(a.name);
    const tools = listify((detail && detail.tools) || a.tools);
    const skills = listify(detail && detail.skills);
    const mcp = listify(detail && detail.mcp);
    const src = agentSource(a.name);
    const lens = lensOf(a.name);
    const kv = h('dl', { class: 'kv' },
      h('dt', null, 'Model'), h('dd', { class: 'mono' }, (m.key || '?') + (m.model && m.model !== m.key ? ' → ' + (m.provider ? m.provider + '/' : '') + m.model : '') + (a.tier && a.tier !== m.key ? '  (file tier ' + a.tier + ')' : '')),
      h('dt', null, 'Driver'), h('dd', null, a.driver || 'builtin'),
      h('dt', null, 'Tools'), h('dd', null, tools.length ? h('span', { class: 'agent-meta' }, tools.map((t) => h('span', { class: 'tag tag-mono' }, t))) : 'none'),
      skills.length ? [h('dt', null, 'Skills'), h('dd', null, h('span', { class: 'agent-meta' }, skills.map((t) => h('span', { class: 'tag tag-mono' }, t))))] : null,
      mcp.length ? [h('dt', null, 'MCP'), h('dd', null, h('span', { class: 'agent-meta' }, mcp.map((t) => h('span', { class: 'tag tag-mono' }, t))))] : null,
      src ? [h('dt', null, 'Source'), h('dd', null, sourceTag(a.name), ' ', detail && detail.path ? h('span', { class: 'mono agent-path' }, String(detail.path)) : null)] : null,
      lens ? [h('dt', null, 'Pipeline'), h('dd', null, lenses.indexOf(lens) >= 0 ? 'Review lens ' + lens + ': runs on every job.' : 'Review lens ' + lens + ': not in the pipeline yet.')] : null);
    const acts = h('div', { class: 'actions agent-actions' });
    const writable = WRITABLE_AGENT_RE.test(a.name);
    if (writable) {
      const edit = btn('Edit', { cls: 'btn-sm', icon: 'edit', dataset: { action: 'edit-agent' }, disabled: !detail || src === 'repo',
        title: src === 'repo' ? 'Defined in the repo’s .factory/agents/: edit it there (it wins over a user override)' : 'Edit the whole file, frontmatter included' });
      edit.addEventListener('click', () => openAgentEditor(a.name));
      acts.appendChild(edit);
    }
    acts.appendChild(btn('Duplicate', { cls: 'btn-sm btn-ghost', icon: 'copy', dataset: { action: 'dup-agent' }, title: 'New agent copied from ' + a.name, onClick: () => openNewAgent(a.name) }));
    // DESIGN §36: what this agent did, on the Operator area's activity page (the last 7 days)
    acts.appendChild(h('a', { class: 'btn btn-sm btn-ghost', href: '#/activity?agent=' + enc(a.name) + '&since=7d', dataset: { action: 'agent-activity' }, title: 'What ' + a.name + ' did: its turns, tool calls and receipts' }, icon('clock', 'icon-sm'), 'Activity'));
    if (lens && lenses.indexOf(lens) < 0) {
      const addB = btn('Add to pipeline', { cls: 'btn-sm btn-primary', icon: 'plus', dataset: { action: 'add-pipeline' }, title: 'Append ' + lens + ' to pipeline.reviewers' });
      addB.addEventListener('click', () => addToPipeline(lens, addB));
      acts.appendChild(addB);
    }
    if (src === 'user' && writable) acts.appendChild(btn('Reset to default', { cls: 'btn-sm btn-danger', icon: 'rework', dataset: { action: 'reset-agent' }, onClick: () => resetAgent(a.name) }));
    const viewer = h('article', { class: 'agent-viewer', 'aria-label': 'Agent ' + a.name },
      h('div', { class: 'agent-viewer-head' },
        h('div', { class: 'agent-card-top' }, h('span', { class: 'dot ' + agentColorCls(a.name) }), h('h2', { class: 'agent-name', style: { 'font-size': '15px' } }, a.name), h('span', { class: 'spacer' }), sourceTag(a.name)),
        (detail && detail.description) || a.description ? h('p', { class: 'muted' }, (detail && detail.description) || a.description) : null,
        kv, acts),
      h('div', { class: 'agent-viewer-body' },
        AG.errors.has(a.name) ? callout('err', 'alert', 'Could not load the prompt.', AG.errors.get(a.name))
          : detail ? (detail.body ? MD.render(detail.body) : h('p', { class: 'muted' }, 'This agent has an empty prompt body.'))
            : skeletonLines(12)));
    root.appendChild(h('div', { class: 'agents-layout' }, list, viewer));
    document.getElementById('main').scrollTop = y;
    // Stacked (list above the agent, below 1100 px): an agent picked by the route is shown, not left below the list.
    const routed = S.route.agent && S.route.agent === sel ? sel : null;
    if (routed && AG.shown !== routed && window.matchMedia && window.matchMedia('(max-width: 1100px)').matches) {
      requestAnimationFrame(() => viewer.scrollIntoView({ block: 'start' }));
    }
    AG.shown = routed;
    if (focusedAction) { const el = root.querySelector('[data-action="' + CSS.escape(focusedAction) + '"]'); if (el) el.focus({ preventScroll: true }); }
  };

  // ================================================================ doctor view
  RENDER.doctor = function () {
    if (S.route.view !== 'doctor') return;
    const root = document.getElementById('doctor-root');
    clear(root);
    const rerun = btn('Re-run checks', { cls: 'btn-sm', icon: 'refresh', onClick: () => loadDoctor(true) });
    if (S.doctorLoading) setBusy(rerun, true);
    root.appendChild(viewHead('Doctor', S.doctor ? 'Last run ' + relTime(S.doctor.at) + '. Checks providers, keys, git, sandbox and local model servers.' : 'Checks providers, keys, git, sandbox and local model servers.', rerun));
    if (!S.doctor) {
      if (S.doctorError) root.appendChild(callout('err', 'alert', 'Doctor failed.', S.doctorError));
      else root.appendChild(skeletonLines(8));
      return;
    }
    const checks = S.doctor.checks;
    const errors = checks.filter((c) => !c.ok && (c.level || 'error') === 'error');
    const warns = checks.filter((c) => !c.ok && c.level === 'warn');
    const infos = checks.filter((c) => !c.ok && c.level === 'info');
    const passed = checks.filter((c) => c.ok);
    const stat = (cls, ic, n, label) => h('div', { class: 'stat-card' }, h('div', { class: 'stat-icon ' + cls }, icon(ic, 'icon-lg')), h('div', null, h('div', { class: 'stat-num' }, String(n)), h('div', { class: 'stat-label' }, label)));
    root.appendChild(h('div', { class: 'doctor-summary' }, stat('err', 'error', errors.length, errors.length === 1 ? 'error' : 'errors'), stat('warn', 'alert', warns.length, warns.length === 1 ? 'warning' : 'warnings'), stat('ok', 'check-circle', passed.length, 'passed')));
    if (!checks.length) { root.appendChild(emptyState('doctor', 'No checks reported', null, null, true)); return; }
    const group = (title, list, kind) => {
      if (!list.length) return null;
      return h('section', { class: 'check-group' },
        h('h2', { class: 'check-group-title' }, title, h('span', { class: 'count-pill' }, String(list.length))),
        h('ul', { class: 'check-list' }, list.map((c) => {
          const k = c.ok ? 'ok' : kind;
          const ic = k === 'ok' ? 'check' : k === 'err' ? 'x' : k === 'warn' ? 'alert' : 'info';
          return h('li', { class: 'check-row' },
            h('span', { class: 'check-icon ' + k, title: c.ok ? 'ok' : (c.level || 'error') }, icon(ic), h('span', { class: 'sr-only' }, c.ok ? 'ok' : (c.level || 'error'))),
            h('div', { class: 'check-main' }, h('div', { class: 'check-name' }, String(c.name)), c.detail ? h('div', { class: 'check-detail' }, String(c.detail)) : null,
              // the "local servers" advice names a button two steps into the setup wizard: a link goes straight there
              c.name === 'local servers' && /Look for model servers/.test(String(c.detail || '')) ? h('a', { class: 'check-link', href: '#/setup/local-details', dataset: { action: 'look-around-step' } }, 'Open Server details') : null,
              extNodes('doctorCheck', c)),
            c.level && !c.ok ? h('span', { class: 'tag tag-outline' }, String(c.level)) : null);
        })));
    };
    add(root, [group('Errors', errors, 'err'), group('Warnings', warns, 'warn'), group('Notes', infos, 'info'), group('Passed', passed, 'ok')]);
  };

  // ================================================================ settings view
  // [config key, label, icon]; the "agents" routes are edited in the Routing view (anchor #/settings/routing).
  const SETTINGS_SECTIONS = [
    ['agents', 'Routing', 'route'], ['profiles', 'Profiles', 'list'], ['providers', 'Providers', 'settings'], ['models', 'Models', 'agents'], ['pipeline', 'Pipeline', 'factory'],
    ['limits', 'Limits', 'clock'], ['harness', 'Harness', 'terminal'], ['mcp_servers', 'MCP servers', 'layers'],
    ['escalate_paths', 'Escalate paths', 'alert'], ['repos', 'Repos', 'folder'],
  ];
  const SEC_ANCHOR = { agents: 'routing' };
  const secAnchor = (key) => SEC_ANCHOR[key] || key;
  const anchorSec = (anchor) => (anchor === 'agents' ? 'routing' : anchor);
  const CONFIG_KEYS = SETTINGS_SECTIONS.map((s) => s[0]).concat(['default_profile']);   // default_profile: in the Profiles card
  const SEC_OF = { default_profile: 'profiles' };   // a config key edited in another key's section
  const secDirty = (id) => sectionDirty(id) || CONFIG_KEYS.some((k) => SEC_OF[k] === id && sectionDirty(k));
  const DICT_SECTIONS = new Set(['providers', 'models', 'agents', 'mcp_servers']);
  const NAME_RE = /^[A-Za-z0-9][A-Za-z0-9._-]*$/;
  const MASK = '***';
  const CFG = { orig: null, draft: null, loading: false, loadError: null, saving: false, saveError: null, saveSection: null, invalid: new Map(), rawText: new Map(), tests: new Map(), scout: new Map(), saveBar: null };
  /** GET/POST /api/config: "agents" is the override dict {name or "*": {driver, model}}. Older servers sent
   *  the resolved agent list there and the overrides under agent_routes: accept both. */
  function normalizeConfig(r) {
    const cfg = isObj(r) ? r : {};
    if (Array.isArray(cfg.agents)) {
      if (!Array.isArray(cfg.agent_list)) cfg.agent_list = cfg.agents;
      cfg.agents = isObj(cfg.agent_routes) ? cfg.agent_routes : {};
    }
    delete cfg.agent_routes;
    return cfg;
  }
  async function loadConfig() {
    CFG.loading = true;
    try {
      const r = await api('/api/config');
      CFG.orig = normalizeConfig(r);
      CFG.draft = clone(CFG.orig);
      CFG.invalid.clear();
      CFG.loadError = null;
      CFG.saveError = null;
      for (const repo of cfgRepos()) if (!CFG.scout.has(repo.name)) checkScout(repo.name, false);
    } catch (e) {
      CFG.loadError = e.message;
    } finally {
      CFG.loading = false;
      invalidate('settings', 'agents');
    }
  }
  function cfgRepos() { return CFG.draft && Array.isArray(CFG.draft.repos) ? CFG.draft.repos.filter((r) => r && r.name) : []; }
  function sectionDirty(sec) { return !!CFG.orig && stable(CFG.draft[sec]) !== stable(CFG.orig[sec]); }
  function configDirty() { return !!CFG.orig && CONFIG_KEYS.some(sectionDirty); }
  /** An entry as the GUI shows it, minus what config_view adds (key_present). Masked "***" values stay: an
   *  unchanged one is not in the diff (the server keeps the stored value), a new entry never has one. */
  function cleanEntry(sec, d) {
    if (!isObj(d)) return d;
    const e = clone(d);
    if (sec === 'providers') delete e.key_present;
    return e;
  }
  /** The POST /api/config body: per dirty section, the merge patch from the saved section to the draft (changed
   *  entries and keys only; null removes an entry, an override or a key, since the server merges dicts). */
  function buildPatch() {
    const patch = {};
    for (const sec of CONFIG_KEYS) {
      if (!sectionDirty(sec)) continue;
      const d = CFG.draft[sec];
      const o = CFG.orig[sec];
      if (isObj(d) && isObj(o)) {
        const clean = (obj) => { const out = {}; for (const k of Object.keys(obj)) out[k] = DICT_SECTIONS.has(sec) ? cleanEntry(sec, obj[k]) : obj[k]; return out; };
        const diff = mergeDiff(clean(o), clean(d));
        if (Object.keys(diff).length) patch[sec] = diff;
      } else {
        patch[sec] = clone(d);
      }
    }
    return patch;
  }
  /** Adopt a config view the server sent (after a preset or a direct POST): sections with unsaved edits keep the
   *  draft, except those in `take`, which follow the server. */
  function adoptConfig(view, take) {
    const fresh = normalizeConfig(view);
    if (!CFG.orig) { CFG.orig = fresh; CFG.draft = clone(fresh); invalidate('settings', 'agents'); return; }
    const draft = clone(fresh);
    for (const k of CONFIG_KEYS) if (sectionDirty(k) && !(take && take.indexOf(k) >= 0)) draft[k] = CFG.draft[k];
    for (const k of take || []) for (const key of Array.from(CFG.invalid.keys())) if (key === k || key.indexOf(k + '.') === 0) CFG.invalid.delete(key);
    CFG.orig = fresh;
    CFG.draft = draft;
    invalidate('settings', 'agents');
  }
  function setInvalid(key, msg, input) {
    if (msg) CFG.invalid.set(key, msg); else CFG.invalid.delete(key);
    if (input) { input.classList.toggle('invalid', !!msg); input.setAttribute('aria-invalid', msg ? 'true' : 'false'); input.title = msg || ''; }
    const holder = document.querySelector('[data-err-for="' + CSS.escape(key) + '"]');
    if (holder) { holder.textContent = msg || ''; holder.hidden = !msg; }
  }
  function errSlot(key) { return h('div', { class: 'field-error', dataset: { errFor: key }, hidden: !CFG.invalid.has(key) }, CFG.invalid.get(key) || ''); }
  function onDraftChange() { updateSaveBar(); updateSettingsNav(); }
  function textIn(get, set, attrs) {
    const v = get();
    const inp = h('input', Object.assign({ class: 'input', type: 'text', value: v === null || v === undefined ? '' : String(v), autocomplete: 'off', spellcheck: 'false' }, attrs || {}));
    inp.addEventListener('input', () => { set(inp.value); onDraftChange(); });
    return inp;
  }
  function nullableText(obj, key, attrs) {
    return textIn(() => obj[key], (v) => { if (v.trim() === '') { if (obj[key] !== undefined) obj[key] = null; } else obj[key] = v.trim(); }, attrs);
  }
  function numIn(obj, key, opts, attrs) {
    opts = opts || {};
    const v = obj[key];
    const errKey = opts.errKey || key;
    const inp = h('input', Object.assign({ class: 'input', type: 'number', inputmode: 'numeric', value: v === null || v === undefined ? '' : String(v), min: opts.min !== undefined ? opts.min : null, step: opts.int === false ? 'any' : '1' }, attrs || {}));
    inp.addEventListener('input', () => {
      const raw = inp.value.trim();
      if (raw === '') {
        if (opts.nullable) { if (obj[key] !== undefined) obj[key] = null; setInvalid(errKey, null, inp); }
        else setInvalid(errKey, 'Required', inp);
      } else {
        const n = Number(raw);
        if (!isFinite(n)) setInvalid(errKey, 'Not a number', inp);
        else if (opts.int !== false && !Number.isInteger(n)) setInvalid(errKey, 'Must be a whole number', inp);
        else if (opts.min !== undefined && n < opts.min) setInvalid(errKey, 'Must be ≥ ' + opts.min, inp);
        else { obj[key] = n; setInvalid(errKey, null, inp); }
      }
      onDraftChange();
    });
    if (CFG.invalid.has(errKey)) { inp.classList.add('invalid'); inp.title = CFG.invalid.get(errKey); }
    return inp;
  }
  function selIn(options, obj, key, attrs, emptyIsDelete) {
    const sel = selectEl(options, obj[key], Object.assign({ class: 'select' }, attrs || {}));
    sel.addEventListener('change', () => { if (sel.value === '' && emptyIsDelete) delete obj[key]; else obj[key] = sel.value; onDraftChange(); });
    return sel;
  }
  function checkIn(label, obj, key, defaultValue) {
    const inp = h('input', { type: 'checkbox' });
    inp.checked = obj[key] === undefined ? !!defaultValue : !!obj[key];
    inp.addEventListener('change', () => { obj[key] = inp.checked; onDraftChange(); });
    return h('label', { class: 'check' }, inp, label);
  }
  function linesIn(obj, key, attrs) {
    const v = Array.isArray(obj[key]) ? obj[key] : [];
    const ta = h('textarea', Object.assign({ class: 'textarea mono-input', rows: Math.min(Math.max(v.length + 1, 3), 14), spellcheck: 'false' }, attrs || {}));
    ta.value = v.join('\n');
    ta.addEventListener('input', () => { obj[key] = ta.value.split('\n').map((s) => s.trim()).filter(Boolean); onDraftChange(); });
    return ta;
  }
  function removeBtn(label, onClick) {
    const b = h('button', { type: 'button', class: 'btn btn-ghost btn-icon btn-sm', title: label, 'aria-label': label }, icon('trash', 'icon-sm'));
    b.addEventListener('click', onClick);
    return b;
  }
  function rerenderSettings() { invalidate('settings'); }
  function setCard(id, title, desc, body, extra) {
    const anchor = secAnchor(id);
    const err = CFG.saveError && CFG.saveSection === id ? h('div', { style: { padding: '12px 18px 0' } }, h('div', { class: 'form-error', role: 'alert' }, icon('alert', 'icon-sm'), h('span', null, CFG.saveError))) : null;
    return h('section', { class: 'set-card', id: 'set-' + anchor, 'aria-labelledby': 'set-' + anchor + '-t', dataset: { sec: id } },
      h('div', { class: 'set-head' },
        h('div', { class: 'set-head-text' }, h('h2', { class: 'set-title', id: 'set-' + anchor + '-t' }, title, h('span', { class: 'tag tag-warn', dataset: { dirtyFor: id }, hidden: !secDirty(id) }, 'unsaved')), desc ? h('p', { class: 'set-desc' }, desc) : null),
        extra || null),
      err,
      h('div', { class: 'set-body' }, body));
  }
  function addNameRow(placeholder, label, onAdd, extra) {
    const inp = h('input', { class: 'input input-sm', type: 'text', placeholder, 'aria-label': placeholder, spellcheck: 'false' });
    const err = h('span', { class: 'field-error', hidden: true });
    const go = () => {
      const name = inp.value.trim();
      const msg = !name ? 'Enter a name.' : !NAME_RE.test(name) ? 'Use letters, digits, . _ -' : onAdd(name, true);
      if (msg) { err.textContent = msg; err.hidden = false; inp.focus(); return; }
      onAdd(name, false);
      rerenderSettings();
    };
    const b = btn(label, { cls: 'btn-sm', icon: 'plus', onClick: go });
    inp.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); go(); } });
    return h('div', { class: 'add-row' }, inp, extra || null, b, err);
  }

  /** A provider's or model's extension fields: one full-width cell under the row (none when no module adds any). */
  function extRow(nodes) { return nodes.length ? h('div', { class: 'pc pc-ext' }, nodes) : null; }
  function secProviders() {
    const provs = isObj(CFG.draft.providers) ? CFG.draft.providers : (CFG.draft.providers = {});
    const origProvs = isObj(CFG.orig.providers) ? CFG.orig.providers : {};
    // One grid row per provider; a container query turns rows into labelled cards when the card is narrow,
    // so every field (and Remove) stays visible without a sideways scroll.
    const cell = (label, control, cls) => h('label', { class: 'pc ' + cls }, h('span', { class: 'pc-label' }, label), control);
    const head = h('div', { class: 'cfg-row prov-row cfg-head', 'aria-hidden': 'true' },
      ['Name', 'Type', 'Base URL', 'Key env var', 'Key file', 'Auth', 'Max conc.', ''].map((t, i) => h('div', { class: 'pc pc-h' + i }, t)));
    const rows = [];
    for (const name of Object.keys(provs).sort()) {
      const p = provs[name];
      if (!isObj(p)) continue;
      const o = origProvs[name];
      const keyTag = !p.api_key_env && !p.api_key_file ? h('span', { class: 'tag', title: 'No key: no auth header is sent (local servers)' }, 'keyless')
        : !o ? h('span', { class: 'tag tag-outline', title: 'Save to check the key' }, 'unsaved')
          : o.key_present ? h('span', { class: 'tag tag-ok', title: 'The key resolves on the server' }, icon('check', 'icon-xs'), 'key set')
            : h('span', { class: 'tag tag-warn', title: (p.api_key_env || '') + ' is not set in the server environment' }, icon('alert', 'icon-xs'), 'missing');
      rows.push(h('div', { class: 'cfg-row prov-row', role: 'group', 'aria-label': 'Provider ' + name },
        h('div', { class: 'pc pc-name' }, h('span', { class: 'cell-name' }, name), keyTag),
        cell('Type', selIn([['openai', 'openai'], ['anthropic', 'anthropic']], p, 'type', { class: 'select', 'aria-label': name + ' type' }), 'pc-type'),
        cell('Base URL', nullableText(p, 'base_url', { class: 'input mono-input', placeholder: 'host:port/v1', 'aria-label': name + ' base URL' }), 'pc-url'),
        cell('Key env var', nullableText(p, 'api_key_env', { class: 'input mono-input', placeholder: 'none', 'aria-label': name + ' key env var' }), 'pc-env'),
        cell('Key file', nullableText(p, 'api_key_file', { class: 'input mono-input', placeholder: 'none', 'aria-label': name + ' key file', title: p.api_key_file || '' }), 'pc-file'),
        cell('Auth', selIn([['', 'default'], ['bearer', 'bearer'], ['x-api-key', 'x-api-key'], ['both', 'both']], p, 'auth', { class: 'select', 'aria-label': name + ' auth header' }, true), 'pc-auth'),
        cell('Max conc.', numIn(p, 'max_concurrency', { nullable: true, min: 1, errKey: 'providers.' + name + '.max_concurrency' }, { class: 'input', placeholder: '∞', 'aria-label': name + ' max concurrency' }), 'pc-max'),
        h('div', { class: 'pc pc-del' }, removeBtn('Remove provider ' + name, () => { delete provs[name]; rerenderSettings(); })),
        extRow(settingsExt('provider', { name, entry: p }))));
    }
    const grid = h('div', { class: 'cfg-list' }, head, rows);
    const typeSel = selectEl([['openai', 'openai'], ['anthropic', 'anthropic']], 'openai', { class: 'select select-sm', style: { width: 'auto' }, 'aria-label': 'New provider type' });
    return setCard('providers', 'Providers', 'OpenAI- or Anthropic-compatible endpoints. Keys are read from environment variables or a dotenv file on the server and are never shown here. Local servers (Strata, Ollama, vLLM) can be keyless; set max concurrency to queue parallel agents on a one-slot GPU server.', [
      grid,
      addNameRow('new-provider', 'Add provider', (name, check) => {
        if (check) return provs[name] ? 'A provider with that name exists.' : null;
        provs[name] = { type: typeSel.value, base_url: '', api_key_env: null };
        return null;
      }, typeSel),
    ]);
  }

  function secModels() {
    const models = isObj(CFG.draft.models) ? CFG.draft.models : (CFG.draft.models = {});
    const provNames = Object.keys(isObj(CFG.draft.providers) ? CFG.draft.providers : {}).sort();
    // Same responsive grid as Providers: one row per model when wide, labelled cards when narrow.
    const cell = (label, control, cls) => h('label', { class: 'pc ' + cls }, h('span', { class: 'pc-label' }, label), control);
    const head = h('div', { class: 'cfg-row mdl-row cfg-head', 'aria-hidden': 'true' },
      ['Key', 'Provider', 'Model id', 'Max tokens', 'Context', 'Stream', 'extra_body (JSON)', '', ''].map((t, i) => h('div', { class: 'pc mdl-h' + i }, t)));
    const rows = [];
    for (const key of Object.keys(models).sort((a, b) => {
      const tier = ['opus', 'sonnet', 'haiku'];
      const ra = tier.indexOf(a) >= 0 ? tier.indexOf(a) : 9;
      const rb = tier.indexOf(b) >= 0 ? tier.indexOf(b) : 9;
      return ra - rb || a.localeCompare(b);
    })) {
      const m = models[key];
      if (!isObj(m)) continue;
      const errKey = 'models.' + key + '.extra_body';
      const extra = h('textarea', { class: 'textarea mono-input w-extra', rows: 1, spellcheck: 'false', 'aria-label': key + ' extra_body JSON' });
      // An invalid draft keeps its text across re-renders (the field stays marked until it parses or is discarded).
      extra.value = CFG.invalid.has(errKey) && CFG.rawText.has(errKey) ? CFG.rawText.get(errKey) : m.extra_body === undefined ? '{}' : JSON.stringify(m.extra_body);
      extra.rows = Math.min(4, Math.max(1, Math.ceil(extra.value.length / 30)));
      extra.addEventListener('input', () => {
        const raw = extra.value.trim();
        let msg = null;
        if (raw === '') { m.extra_body = {}; }
        else {
          try {
            const v = JSON.parse(raw);
            if (!isObj(v)) msg = 'extra_body must be a JSON object';
            else m.extra_body = v;
          } catch (e) { msg = 'Invalid JSON: ' + e.message; }
        }
        if (msg) CFG.rawText.set(errKey, extra.value); else CFG.rawText.delete(errKey);
        setInvalid(errKey, msg, extra);
        onDraftChange();
      });
      if (CFG.invalid.has(errKey)) { extra.classList.add('invalid'); extra.title = CFG.invalid.get(errKey); }
      const result = h('div', { class: 'test-result', 'aria-live': 'polite' });
      const resultRow = h('div', { class: 'pc pc-result', hidden: true }, result);
      const test = btn('Test', { cls: 'btn-sm', icon: 'zap', title: 'Two probes with the saved config: a plain reply ("Reply with OK"), then a call of a trivial get_time tool' });
      const showResult = () => {
        clear(result);
        const t = CFG.tests.get(key);
        resultRow.hidden = !t;
        setBusy(test, !!(t && t.busy));
        if (!t) return;
        add(result, testResultParts(key, t));
        result.className = 'test-result ' + testTone(t);
      };
      test.addEventListener('click', () => runModelTest(key));
      TEST_VIEWS.add((k) => { if (!k || k === key) showResult(); });
      showResult();
      if (sectionDirty('models') && CFG.orig.models && stable(CFG.orig.models[key]) !== stable(m)) test.title = 'Unsaved changes: the test uses the saved config';
      rows.push(h('div', { class: 'cfg-row mdl-row', role: 'group', 'aria-label': 'Model ' + key, dataset: { model: key } },
        h('div', { class: 'pc pc-key' }, h('span', { class: 'cell-name' }, key)),
        cell('Provider', selIn(provNames.map((p) => [p, p]), m, 'provider', { class: 'select', 'aria-label': key + ' provider' }), 'pc-prov'),
        cell('Model id', textIn(() => m.model, (v) => { m.model = v.trim(); }, { class: 'input mono-input', placeholder: 'model id', 'aria-label': key + ' model id' }), 'pc-model'),
        cell('Max tokens', numIn(m, 'max_tokens', { min: 1, errKey: 'models.' + key + '.max_tokens' }, { class: 'input', 'aria-label': key + ' max tokens' }), 'pc-maxt'),
        cell('Context', numIn(m, 'context_tokens', { nullable: true, min: 1, errKey: 'models.' + key + '.context_tokens' }, { class: 'input', placeholder: 'auto', 'aria-label': key + ' context tokens' }), 'pc-ctx'),
        h('div', { class: 'pc pc-stream' }, h('span', { class: 'pc-label', 'aria-hidden': 'true' }, 'Stream'), checkIn(h('span', { class: 'sr-only' }, key + ' stream'), m, 'stream', true)),
        h('div', { class: 'pc pc-extra' }, h('span', { class: 'pc-label', 'aria-hidden': 'true' }, 'extra_body (JSON)'), extra, errSlot(errKey)),
        h('div', { class: 'pc pc-test' }, test),
        h('div', { class: 'pc pc-del' }, removeBtn('Remove model ' + key, () => { delete models[key]; rerenderSettings(); })),
        resultRow, extRow(settingsExt('model', { name: key, entry: m }))));
    }
    const table = h('div', { class: 'cfg-list' }, head, rows);
    const addRow = addNameRow('new-model-key', 'Add model', (name, check) => {
      if (check) return models[name] ? 'A model with that key exists.' : null;
      models[name] = { provider: provNames[0] || '', model: '', max_tokens: 8192, stream: true, extra_body: {} };
      return null;
    });
    const templates = CFG.orig && Array.isArray(CFG.orig.model_templates) ? CFG.orig.model_templates.filter((t) => isObj(t) && isObj(t.entry)) : [];
    if (templates.length) addRow.appendChild(btn('Add from template', { cls: 'btn-sm btn-primary', icon: 'layers', dataset: { action: 'model-template' }, title: 'Pre-fill a model for a local server (llama.cpp, vLLM, Ollama, LM Studio, TabbyAPI) or an API', onClick: () => openModelTemplate() }));
    return setCard('models', 'Models', 'Named model entries. Agents and chats refer to these keys.', [
      callout('info', 'info', 'Tiers are just model keys.', 'Agent files say model: opus or model: sonnet; that word is looked up here. Point a tier at any provider (for example sonnet → a local model on your GPU server) or add entries such as local-coder and route them per step in Routing.'),
      table,
      addRow,
    ]);
  }
  function testTone(t) {
    if (t.busy) return '';
    const r = t.result || {};
    return r.ok && r.tool_call && !r.error ? 'ok' : r.ok ? 'warn' : 'err';
  }
  /** Both probes of POST /api/models/<key>/test: the plain reply (ok, latency, text), the tool call, tokens/s. */
  function testResultParts(key, t) {
    if (t.busy) return [h('span', { class: 'spinner' }), 'testing ' + key + '… (plain reply, then a tool call)'];
    const r = t.result || {};
    if (!r.ok) return [icon('error', 'icon-xs'), h('span', null, key + ' failed: ' + String(r.error || t.error || 'no answer') + (r.latency_ms !== undefined && r.latency_ms !== null ? ' (' + r.latency_ms + ' ms)' : ''))];
    const probe = (ok, label, title) => h('span', { class: 'probe ' + (ok ? 'ok' : 'bad'), title: title || '' }, icon(ok ? 'check' : 'x', 'icon-xs'), label);
    const tps = Number(r.tokens_per_second);
    return [
      probe(true, 'reply ' + (r.latency_ms !== undefined && r.latency_ms !== null ? r.latency_ms + ' ms' : ''), 'Plain reply' + (r.text ? ': ' + String(r.text).slice(0, 200) : '')),
      r.text ? h('span', { class: 'probe-text' }, '“' + String(r.text).slice(0, 60) + '”') : null,
      probe(r.tool_call === true, r.tool_call === true ? 'tool call' : 'no tool call', r.tool_call === true ? 'The model called the get_time probe tool' : 'The model did not call the probe tool: do not give it to an agent that needs tools'),
      h('span', { class: 'probe probe-tps', title: 'Output tokens per second over both probes' }, icon('gauge', 'icon-xs'), isFinite(tps) && tps > 0 ? fmtTps(tps) : 'tok/s n/a'),
      r.error ? h('span', { class: 'probe-err' }, String(r.error)) : null,
    ];
  }

  // ---------------------------------------------------------------- Add model from template (DESIGN §22)
  // note(ctx): how to start the server so its context matches Context tokens (ctx: the field's value).
  const SERVER_KINDS = [
    { id: 'llamacpp', label: 'llama.cpp (llama-server)', provider: 'llamacpp', port: 8080,
      note: (c) => ['Start llama-server with ', h('code', null, '--jinja'), ' (tool calling needs the model’s chat template), ', h('code', null, '-c ' + c), ', ', h('code', null, '-ngl 99'), ' and ', h('code', null, '--parallel 1'), '.'] },
    { id: 'vllm', label: 'vLLM', provider: 'vllm', port: 8000,
      note: (c) => ['Start vllm serve with ', h('code', null, '--max-model-len ' + c), ', plus ', h('code', null, '--enable-auto-tool-choice'), ' and the model’s ', h('code', null, '--tool-call-parser'), '.'] },
    { id: 'ollama', label: 'Ollama', provider: 'ollama', port: 11434, colon: true,
      note: (c) => ['Ollama starts models with a small context: set ', h('code', null, 'OLLAMA_CONTEXT_LENGTH=' + c), ' (or ', h('code', null, 'PARAMETER num_ctx ' + c), ').'] },
    { id: 'lmstudio', label: 'LM Studio', provider: 'lmstudio', port: 1234,
      note: (c) => ['Load the model in LM Studio with context length ', h('code', null, c), ', then start its local server.'] },
    { id: 'tabby', label: 'TabbyAPI', provider: 'tabby', port: 5000, keyEnv: 'TABBY_API_KEY', auth: 'both',
      note: (c) => ['Set ', h('code', null, 'max_seq_len: ' + c), ' in TabbyAPI’s config.yml. It checks ', h('code', null, 'x-api-key'), ': the provider sends both headers.'] },
    { id: 'existing', label: 'An existing provider', note: null },
  ];
  const loopbackUrl = (port) => 'http://127.0.0.1:' + port + '/v1';  // a loopback default for a new local provider
  const ENTRY_KEY_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;
  function openModelTemplate() {
    if (document.querySelector('dialog[open]')) return;
    const templates = (CFG.orig.model_templates || []).filter((t) => isObj(t) && isObj(t.entry));
    const provs = isObj(CFG.draft.providers) ? CFG.draft.providers : (CFG.draft.providers = {});
    const models = isObj(CFG.draft.models) ? CFG.draft.models : (CFG.draft.models = {});
    const kindSel = selectEl(SERVER_KINDS.map((k) => [k.id, k.label]), 'llamacpp', { id: 'mt-kind' });
    const provSel = selectEl(Object.keys(provs).sort().map((p) => [p, p + (isLoopback(provs[p] && provs[p].base_url) ? '  (local)' : '')]), Object.keys(provs).sort()[0] || '', { id: 'mt-provider' });
    const tplSel = selectEl(templates.map((t) => [t.id, t.label || t.id]), templates[0].id, { id: 'mt-template' });
    const urlIn = h('input', { class: 'input mono-input', type: 'text', id: 'mt-url', spellcheck: 'false', autocomplete: 'off' });
    const provInfo = h('div', { class: 'field-hint mt-provinfo' });
    const keyIn = h('input', { class: 'input mono-input', type: 'text', id: 'mt-key', spellcheck: 'false', autocomplete: 'off', maxlength: 64 });
    const idIn = h('input', { class: 'input mono-input', type: 'text', id: 'mt-model', spellcheck: 'false', autocomplete: 'off', list: 'mt-hints' });
    const hints = h('datalist', { id: 'mt-hints' });
    const maxIn = h('input', { class: 'input', type: 'number', id: 'mt-max', min: 1, step: 1, inputmode: 'numeric' });
    const ctxIn = h('input', { class: 'input', type: 'number', id: 'mt-ctx', min: 1, step: 1, inputmode: 'numeric' });
    const extraIn = h('textarea', { class: 'textarea mono-input', id: 'mt-extra', rows: 3, spellcheck: 'false' });
    const notes = h('div', { class: 'mt-notes' });
    const roles = h('div', { class: 'mt-roles' });
    const routeIt = h('input', { type: 'checkbox', id: 'mt-route' });
    const provField = field('Provider', provSel);
    const urlField = field('Base URL', urlIn);
    let flags = {};
    let extraEdited = false;
    const kind = () => SERVER_KINDS.find((k) => k.id === kindSel.value) || SERVER_KINDS[0];
    const tpl = () => templates.find((t) => t.id === tplSel.value) || templates[0];
    const providerName = () => (kind().id === 'existing' ? provSel.value : kind().provider);
    // gpt-oss reasoning depth (DESIGN §22): llama.cpp reads chat_template_kwargs; vLLM, Ollama, LM Studio and the
    // APIs a top-level reasoning_effort. An existing local server is taken to be llama.cpp-like.
    const reasoningTopLevel = () => {
      const k = kind();
      if (k.id === 'existing') return !isLoopback((provs[provSel.value] || {}).base_url);
      return k.id !== 'llamacpp';
    };
    const templateKwargs = (t) => isObj(t.entry.extra_body) && isObj(t.entry.extra_body.chat_template_kwargs) && !!t.entry.extra_body.chat_template_kwargs.reasoning_effort;
    const extraFor = (t) => {
      let extra = isObj(t.entry.extra_body) ? clone(t.entry.extra_body) : {};
      if (templateKwargs(t) && reasoningTopLevel()) {
        extra = Object.assign({}, extra, { reasoning_effort: extra.chat_template_kwargs.reasoning_effort });
        delete extra.chat_template_kwargs;
      }
      return extra;
    };
    const paintExtra = () => { if (!extraEdited) extraIn.value = JSON.stringify(extraFor(tpl())); };
    extraIn.addEventListener('input', () => { extraEdited = true; });
    const paintProvider = () => {
      const k = kind();
      provField.hidden = k.id !== 'existing';
      const name = providerName();
      const existing = isObj(provs[name]);
      urlField.hidden = k.id === 'existing' || existing;
      if (!existing && k.port && !urlIn.value) urlIn.value = loopbackUrl(k.port);
      clear(provInfo);
      if (existing) add(provInfo, ['Uses provider ', h('code', null, name), ' at ', h('code', null, String(provs[name].base_url || '?')), isLoopback(provs[name].base_url) ? ' (LOCAL).' : ' (API).']);
      else add(provInfo, ['Creates provider ', h('code', null, name), k.keyEnv ? ' (key from ' + k.keyEnv + ', one request at a time)' : ' (no key, one request at a time)', '; edit it under Providers later.']);
    };
    const prefill = () => {
      const t = tpl();
      const e = t.entry;
      if (t.provider && isObj(provs[t.provider])) { kindSel.value = 'existing'; provSel.value = t.provider; }
      else if (!t.provider && kindSel.value === 'existing' && !isLoopback((provs[provSel.value] || {}).base_url)) kindSel.value = 'llamacpp';
      const k = kind();
      const hintsList = Array.isArray(t.model_hints) && t.model_hints.length ? t.model_hints.map(String) : [String(e.model || '')];
      clear(hints);
      for (const m of hintsList) hints.appendChild(h('option', { value: m }));
      idIn.value = (k.colon && hintsList.find((m) => m.indexOf(':') > 0)) || hintsList[0] || String(e.model || '');
      let key = String(t.id || 'model').replace(/[^A-Za-z0-9._-]/g, '-');
      for (let n = 2; models[key]; n++) key = String(t.id).replace(/[^A-Za-z0-9._-]/g, '-') + '-' + n;
      keyIn.value = key;
      maxIn.value = e.max_tokens !== undefined ? String(e.max_tokens) : '';
      ctxIn.value = e.context_tokens !== undefined && e.context_tokens !== null ? String(e.context_tokens) : '';
      extraEdited = false;
      paintExtra();
      flags = {};
      for (const f of ['stream', 'keep_reasoning', 'allow_history_edits', 'eager_tools']) if (e[f] !== undefined) flags[f] = e[f];
      // "Suggested for builder, coder": an offer to route those steps to the new model (unsaved, like the model).
      clear(roles);
      routeIt.checked = false;
      const steps = (Array.isArray(t.roles) ? t.roles.map(String) : []).filter((r) => r && r !== 'any');
      if (steps.length) add(roles, h('label', { class: 'check mt-route' }, routeIt, h('span', null, 'Route ', h('strong', null, steps.join(', ')), ' to it (Settings › Routing; saved with the model)')));
      else if (Array.isArray(t.roles) && t.roles.indexOf('any') >= 0) add(roles, h('span', { class: 'field-hint' }, 'Suits any step: route it in Settings › Routing.'));
      roles._steps = steps;
      paintProvider();
      paintNotes();
    };
    const paintNotes = () => {
      clear(notes);
      const t = tpl();
      const k = kind();
      const local = k.id !== 'existing' || isLoopback((provs[provSel.value] || {}).base_url);
      const list = h('ul', { class: 'mt-note-list' });
      // The server kind's note names its context flag; the generic note (context + Test) closes the list once.
      if (local && k.note) list.appendChild(h('li', null, k.note(ctxIn.value.trim() || '<Context>')));
      if (templateKwargs(t)) {
        list.appendChild(h('li', null, reasoningTopLevel()
          ? ['Reasoning depth uses the top-level ', h('code', null, 'reasoning_effort'), ' (vLLM, Ollama, LM Studio, OpenRouter). If your server ignores it, set it under ', h('code', null, 'chat_template_kwargs'), ' as llama.cpp does.']
          : ['Reasoning depth goes through ', h('code', null, 'chat_template_kwargs'), ' (llama.cpp).']));
      } else if (t.note) list.appendChild(h('li', null, t.note));
      if (CFG.orig.model_template_note) list.appendChild(h('li', null, String(CFG.orig.model_template_note)));
      notes.appendChild(callout('info', 'info', 'Starting points, still editable.', list));
    };
    kindSel.addEventListener('change', () => { urlIn.value = ''; paintProvider(); paintExtra(); paintNotes(); });
    provSel.addEventListener('change', () => { paintProvider(); paintExtra(); paintNotes(); });
    tplSel.addEventListener('change', prefill);
    ctxIn.addEventListener('input', paintNotes);
    prefill();
    openModal({
      title: 'Add a model from a template', wide: true,
      description: 'Pick the server it runs on and a model template; the entry is pre-filled and stays editable. Save, then Test it (a plain reply and a tool call) before you route a step to it.',
      body: [
        h('div', { class: 'grid-form mt-grid' }, field('Server', kindSel), provField, field('Template', tplSel)),
        h('div', { class: 'mt-prov' }, urlField, provInfo),
        h('div', { class: 'grid-form mt-grid mt-grid-4' }, field('Model key', keyIn, 'How routes refer to it.'), field('Model id', idIn, 'What the server calls it.'),
          field('Max tokens', maxIn, 'Per reply.'), field('Context tokens', ctxIn, 'The server’s context window.'), hints),
        field('extra_body (JSON)', extraIn, 'Merged into every request body (reasoning depth, chat template options).'),
        roles, notes,
      ],
      actions: [{ label: 'Cancel', value: 'cancel' }, { label: 'Add model', value: 'ok', primary: true, icon: 'plus' }],
      onSubmit: async () => {
        const key = keyIn.value.trim();
        if (!ENTRY_KEY_RE.test(key)) { keyIn.focus(); throw new Error('The model key uses letters, digits, . _ - (max 64).'); }
        if (models[key]) { keyIn.focus(); throw new Error('A model named ' + key + ' exists: pick another key.'); }
        const id = idIn.value.trim();
        if (!id) { idIn.focus(); throw new Error('Enter the model id the server uses.'); }
        const max = Number(maxIn.value);
        if (!Number.isInteger(max) || max < 1) { maxIn.focus(); throw new Error('Max tokens must be a whole number ≥ 1.'); }
        const ctx = ctxIn.value.trim() === '' ? null : Number(ctxIn.value);
        if (ctx !== null && (!Number.isInteger(ctx) || ctx < 1)) { ctxIn.focus(); throw new Error('Context tokens must be a whole number ≥ 1, or empty.'); }
        let extra;
        try { extra = extraIn.value.trim() ? JSON.parse(extraIn.value) : {}; } catch (e) { extraIn.focus(); throw new Error('extra_body is not valid JSON: ' + e.message); }
        if (!isObj(extra)) { extraIn.focus(); throw new Error('extra_body must be a JSON object.'); }
        const prov = providerName();
        if (!prov) throw new Error('Pick a provider.');
        if (!isObj(provs[prov])) {
          const k = kind();
          const url = urlIn.value.trim();
          if (!/^[a-z][a-z0-9+.-]*:\/\/[^\s/]+/i.test(url)) { urlIn.focus(); throw new Error('Enter the server’s base URL, e.g. ' + loopbackUrl(k.port || 8080) + '.'); }
          provs[prov] = { type: 'openai', base_url: url, api_key_env: k.keyEnv || null, timeout: 1800, max_concurrency: 1, concurrency_scope: 'run' };
          if (k.auth) provs[prov].auth = k.auth;
        }
        models[key] = Object.assign({ provider: prov, model: id, max_tokens: max, context_tokens: ctx, stream: true }, flags, { extra_body: extra });
        if (ctx === null) delete models[key].context_tokens;
        const routed = routeIt.checked && Array.isArray(roles._steps) ? roles._steps : [];
        if (routed.length) {
          const ov = isObj(CFG.draft.agents) ? CFG.draft.agents : (CFG.draft.agents = {});
          for (const step of routed) ov[step] = Object.assign({}, isObj(ov[step]) ? ov[step] : {}, { model: key });
        }
        onDraftChange();
        rerenderSettings();
        toast(['Added ', h('strong', { class: 'mono' }, key), routed.length ? ' and routed ' + routed.join(', ') + ' to it' : '', ' (unsaved). Save, then Test it.'], { kind: 'ok' });
        requestAnimationFrame(() => requestAnimationFrame(() => {
          const row = document.querySelector('#set-models .mdl-row[data-model="' + CSS.escape(key) + '"]');
          if (row) { row.scrollIntoView({ block: 'center' }); row.classList.add('flash-row'); }
        }));
        return key;
      },
    });
  }

  // ---------------------------------------------------------------- Settings › Routing (DESIGN §23)
  // The factory line drawn as boxes, each with its model, driver and an Advanced extra_body override (agents.<name>).
  const RT_OPEN = new Set();          // agents whose Advanced field is open (kept across re-renders)
  const TEST_VIEWS = new Set();       // repaint callbacks for a model's Test result: fn(key)
  function refreshTestViews(key) { for (const fn of Array.from(TEST_VIEWS)) { try { fn(key); } catch (e) { TEST_VIEWS.delete(fn); } } }
  async function runModelTest(key) {
    if (!CFG.orig || !isObj(CFG.orig.models) || !CFG.orig.models[key]) {
      CFG.tests.set(key, { result: { ok: false, error: 'Save this model first: tests use the saved config.' } });
      refreshTestViews(key);
      return;
    }
    CFG.tests.set(key, { busy: true });
    refreshTestViews(key);
    try {
      const r = await api('/api/models/' + enc(key) + '/test', { method: 'POST' });
      CFG.tests.set(key, { result: r || {} });
      recordTest(key, r);
    } catch (e) {
      CFG.tests.set(key, { error: e.message, result: { ok: false, error: e.message } });
    } finally {
      refreshTestViews(key);
    }
  }
  const ROUTE_ROLE = { 'spec-writer': 'Step 1 · spec', builder: 'Step 2 · build', approver: 'Final gate', lead: 'Plans · above the line', coder: 'Chat', 'repo-scout': 'Repo scout', '*': 'Default for every agent' };
  function routeRole(name) { return ROUTE_ROLE[name] || (/^reviewer-/.test(name) ? 'Review lens' : 'Not on the line'); }
  function agentTier(name) {
    const a = agentDef(name);
    if (a && a.tier) return String(a.tier);
    const l = CFG.orig && Array.isArray(CFG.orig.agent_list) ? CFG.orig.agent_list.find((x) => x && x.name === name) : null;
    return l && l.tier ? String(l.tier) : 'opus';
  }
  /** The Test chip of a model key: the last tokens/s, a failure in a few words, or "Test" (never measured).
   *  It is also the button that runs Test again. */
  function testChip(key) {
    const t = CFG.tests.get(key);
    const m = TPS[key];
    let tag;
    let what;
    if (t && t.busy) {
      tag = h('span', { class: 'tag tag-outline' }, h('span', { class: 'spinner', 'aria-hidden': 'true' }), 'testing…');
      what = 'Testing ' + key;
    } else if (t && t.result && !t.result.ok) {
      const err = String(t.result.error || t.error || 'no answer');
      tag = h('span', { class: 'tag tag-err' }, icon('error', 'icon-xs'), testReason(err));
      what = 'Test of ' + key + ' failed: ' + err;
    } else if (m && (m.tps || m.at)) {
      const noTools = m.tool === false;
      tag = h('span', { class: 'tag ' + (noTools ? 'tag-warn' : 'tag-mono') }, icon('gauge', 'icon-xs'), (m.tps ? fmtTps(m.tps) : 'ok') + (noTools ? ' · no tools' : ''));
      what = 'Last Test of ' + key + ' ' + relTime(m.at) + (m.tps ? ': ' + fmtTps(m.tps) : '') + (noTools ? '; it did not call the probe tool, so do not give it to an agent that needs tools' : '; tool call ok');
    } else {
      tag = h('span', { class: 'tag tag-outline rt-untested' }, icon('gauge', 'icon-xs'), 'Test');
      what = key + ' is not measured yet';
    }
    const b = h('button', { type: 'button', class: 'rt-test', title: what + '. Click to Test ' + key + ' (a plain reply, a tool call, tokens/s).', 'aria-label': what + '. Test ' + key + ' again', disabled: !!(t && t.busy) }, tag);
    b.addEventListener('click', () => runModelTest(key));
    return b;
  }
  /** Inherited extra_body, pretty-printed so it wraps between keys, not inside them. */
  function jsonBlock(v) { return h('pre', { class: 'rt-base' }, JSON.stringify(v, null, 1)); }
  function routeBox(name, ov) {
    const cfg = CFG.draft;
    const isStar = name === '*';
    const entry = () => (isObj(ov[name]) ? ov[name] : {});
    const star = () => (isObj(ov['*']) ? ov['*'] : {});
    const tier = isStar ? null : agentTier(name);
    const fallbackModel = () => (!isStar && star().model ? String(star().model) : tier);
    const fallbackDriver = () => (!isStar && star().driver ? String(star().driver) : 'builtin');
    const commit = (key, value, rerender) => {
      const e = Object.assign({}, entry());
      if (value === '' || value === null || value === undefined) delete e[key]; else e[key] = value;
      if (Object.keys(e).length) ov[name] = e; else delete ov[name];
      onDraftChange();
      if (rerender) rerenderSettings(); else paint();
    };
    const label = isStar ? 'Default' : name;
    const keys = modelKeys(cfg);
    const fm = fallbackModel();
    const fmEntry = cfgModels(cfg)[fm];
    const fmId = fmEntry && fmEntry.model && fmEntry.model !== fm ? ' (' + fmEntry.model + ')' : '';
    // Inherit (no route of its own) and the pins sit in separate groups: "Inherit: tier opus (…)" cannot be
    // mistaken for pinning opus, which later tier or preset changes would no longer move.
    // The closed select stays short ("Inherit: tier opus"); its title names the model the fallback resolves to.
    const inheritModel = isStar ? 'None: each agent’s file tier' : 'Inherit: ' + (star().model ? 'Default * (' + fm + ')' : 'tier ' + (fm || '?'));
    const msel = selectEl([{ group: isStar ? 'No default' : 'Inherit', options: [['', inheritModel]] },
      { group: isStar ? 'Default model for every agent' : 'Pin a model', options: keys.map((k) => [k, modelOptionLabel(cfg, k, false)]) }],
    entry().model || '', { class: 'select select-sm rt-model', 'aria-label': label + ' model',
      title: entry().model ? label + ': pinned to ' + modelOptionLabel(cfg, entry().model) : label + ': ' + inheritModel + (fmId ? ' → ' + fmId.slice(2, -1) : '') });
    const modelSubs = [];   // a view module's fields of this box that follow its model (settingsExt 'route' onModel)
    msel.addEventListener('change', () => { commit('model', msel.value, isStar); if (!isStar) for (const fn of modelSubs) safely('route ' + name + ' model', () => fn()); });
    const drivers = driverNames(cfg);
    const dsel = selectEl([{ group: isStar ? 'No default' : 'Inherit', options: [['', isStar ? 'builtin (default)' : 'Inherit: ' + fallbackDriver()]] },
      { group: isStar ? 'Default driver' : 'Pin a driver', options: drivers.map((d) => [d, d]) }],
    entry().driver || '', { class: 'select select-sm rt-driver', 'aria-label': label + ' driver', title: label + ' driver' });
    dsel.addEventListener('change', () => commit('driver', dsel.value, isStar));
    const meta = h('div', { class: 'rt-meta' });
    const ownTag = h('span', { class: 'tag rt-own' });
    const head = h('div', { class: 'rt-box-head', title: routeRole(name) },
      h('span', { class: 'dot ' + (isStar ? 'c-gray' : agentColorCls(name)) }),
      h('span', { class: 'rt-name' }, isStar ? 'Default (*)' : name),
      ROUTE_ROLE[name] && !/^(spec-writer|builder|approver|\*)$/.test(name) ? h('span', { class: 'rt-role' }, ROUTE_ROLE[name].replace(' · above the line', '')) : null,
      h('span', { class: 'spacer' }), ownTag);
    const box = h('div', { class: 'rt-box' + (isStar ? ' rt-star' : ''), dataset: { agent: name } }, head, msel, meta);
    box.appendChild(h('div', { class: 'rt-drv' }, h('span', { class: 'rt-drv-label', 'aria-hidden': 'true' }, 'driver'), dsel));
    let advBtn = null;
    let advBody = null;
    const errKey = 'agents.' + name + '.extra_body';
    if (!isStar) {
      const ta = h('textarea', { class: 'textarea mono-input rt-extra', rows: 3, spellcheck: 'false', placeholder: '{"reasoning_effort": "high"}', 'aria-label': name + ' extra_body override (JSON)' });
      // An invalid draft keeps its text across re-renders (the field stays marked until it parses or is discarded).
      ta.value = CFG.invalid.has(errKey) && CFG.rawText.has(errKey) ? CFG.rawText.get(errKey) : isObj(entry().extra_body) ? JSON.stringify(entry().extra_body, null, 1) : '';
      ta.addEventListener('input', () => {
        const raw = ta.value.trim();
        if (!raw) { CFG.rawText.delete(errKey); setInvalid(errKey, null, ta); commit('extra_body', null); return; }
        let v;
        const bad = (msg) => { CFG.rawText.set(errKey, ta.value); setInvalid(errKey, msg, ta); onDraftChange(); };
        try { v = JSON.parse(raw); } catch (e) { bad('Invalid JSON: ' + e.message); return; }
        if (!isObj(v)) { bad('extra_body must be a JSON object'); return; }
        CFG.rawText.delete(errKey);
        setInvalid(errKey, null, ta);
        commit('extra_body', Object.keys(v).length ? v : null);
      });
      if (CFG.invalid.has(errKey)) { ta.classList.add('invalid'); ta.title = CFG.invalid.get(errKey); ta.setAttribute('aria-invalid', 'true'); }
      const hint = h('div', { class: 'field-hint rt-adv-hint' });
      const bodyId = 'rt-adv-' + name.replace(/[^a-z0-9-]/gi, '_');
      const open = RT_OPEN.has(name) || CFG.invalid.has(errKey);
      advBody = h('div', { class: 'rt-adv-body', id: bodyId, hidden: !open }, h('label', { class: 'rt-adv-title', for: bodyId + '-ta' }, 'extra_body override (JSON)'), ta, errSlot(errKey), hint);
      ta.id = bodyId + '-ta';
      advBody._hint = hint;
      advBtn = h('button', { type: 'button', class: 'rt-adv-btn', 'aria-expanded': open ? 'true' : 'false', 'aria-controls': bodyId, title: 'A per-step extra_body override (JSON), deep-merged over the model’s', dataset: { action: 'advanced' } },
        icon('chevron', 'icon-xs chev'), h('span', null, 'Advanced'), h('span', { class: 'rt-adv-set' }, 'extra_body set'));
      advBtn.addEventListener('click', () => {
        const nowOpen = advBody.hidden;
        advBody.hidden = !nowOpen;
        advBtn.setAttribute('aria-expanded', nowOpen ? 'true' : 'false');
        if (nowOpen) { RT_OPEN.add(name); ta.focus(); } else RT_OPEN.delete(name);
      });
      box.appendChild(advBtn);
      box.appendChild(advBody);
      add(box, settingsExt('route', { name, entry: () => entry(), set: (key, value) => commit(key, value, false), onModel: (fn) => { if (typeof fn === 'function') modelSubs.push(fn); } }));
    }
    function paint() {
      const e = entry();
      const key = e.model || (isStar ? '' : fallbackModel());
      clear(meta);
      if (key && cfgModels(cfg)[key]) {
        const prov = modelProvider(cfg, key) || '?';
        add(meta, [h('span', { class: 'tag tag-mono rt-prov', title: 'Provider of ' + key + ': ' + prov }, prov), locBadge(cfg, key), testChip(key)]);
      } else if (key) {
        meta.appendChild(h('span', { class: 'tag tag-err', title: key + ' is not a key of models' }, key + ' not configured'));
      } else {
        meta.appendChild(h('span', { class: 'muted rt-none' }, 'each agent falls back to its file tier'));
      }
      const hasExtra = isObj(e.extra_body) && Object.keys(e.extra_body).length > 0;
      const own = !!(e.model || e.driver || hasExtra);
      ownTag.className = 'tag rt-own ' + (e.model ? 'tag-accent' : 'tag-outline');
      ownTag.textContent = isStar ? 'fallback' : e.model ? 'own route' : star().model ? 'via *' : 'tier ' + tier;
      ownTag.title = isStar ? 'Steps without a model route of their own use this one'
        : e.model ? 'Routed to ' + e.model + ' (overrides tier ' + tier + ')'
          : 'No model route of its own: runs on ' + (star().model ? 'Default (*) → ' + fallbackModel() : 'its file tier ' + tier) + (cfgModels(cfg)[fallbackModel()] ? ' → ' + (cfgModels(cfg)[fallbackModel()].model || '?') : '');
      box.classList.toggle('is-own', own);
      if (advBtn) {
        advBtn.classList.toggle('is-set', hasExtra);
        clear(advBody._hint);
        const base = key && cfgModels(cfg)[key] ? cfgModels(cfg)[key].extra_body : null;
        if (isObj(base) && Object.keys(base).length) add(advBody._hint, ['Deep-merged over ', h('span', { class: 'mono' }, key), '’s extra_body, for this step only:', jsonBlock(base)]);
        else add(advBody._hint, ['Deep-merged over ', h('span', { class: 'mono' }, key || 'the model'), '’s extra_body (empty), for this step only.']);
      }
    }
    paint();
    TEST_VIEWS.add((k) => { if (!k || k === (entry().model || fallbackModel())) paint(); });
    return box;
  }
  async function applyPreset(name, sel) {
    const dirty = ['providers', 'models', 'agents'].filter(sectionDirty);
    await confirmModal({
      title: 'Apply the ' + name + ' preset?',
      description: presetInfo(name) || 'Routing from examples/' + name + '.json.',
      body: h('div', { class: 'preset-explain' },
        h('p', null, 'examples/' + name + '.json is merged into your saved config:'),
        h('ul', null,
          h('li', null, h('strong', null, 'Replaced: '), 'each provider, model (the opus, sonnet and haiku tiers included) and agent route the preset defines, entry by entry. A route it sets loses its own driver and Advanced extra_body.'),
          h('li', null, h('strong', null, 'Kept: '), 'every entry it does not name (an added review lens keeps its route), your repos, pipeline, limits, harness, MCP servers and the server token.'),
          h('li', null, h('strong', null, 'Jobs: '), 'running and stopped jobs keep the routing snapshot they were submitted with.')),
        dirty.length ? callout('warn', 'alert', 'Unsaved changes are discarded:', dirty.map((k) => (SETTINGS_SECTIONS.find((x) => x[0] === k) || [k, k])[1]).join(', ') + '.') : null),
      confirm: 'Apply ' + name, icon: 'route',
      onSubmit: async () => {
        const r = await api('/api/config/preset', { method: 'POST', body: { name } });
        adoptConfig(r, ['providers', 'models', 'agents']);
        toast(['Applied the ', h('strong', null, name), ' preset. New jobs use it.'], { kind: 'ok' });
        refreshState();
        loadSetup();
      },
    });
    if (sel && sel.isConnected) sel.value = '';
  }
  function secRouting() {
    const ov = isObj(CFG.draft.agents) ? CFG.draft.agents : (CFG.draft.agents = {});
    const reviewers = pipelineReviewers();
    const known = new Set(sortedAgents().map((a) => a.name));
    const arrow = (label) => h('div', { class: 'rt-arrow', 'aria-hidden': 'true' }, icon('arrow-right', 'icon-sm'), label ? h('span', { class: 'rt-arrow-label' }, label) : null);
    const lineNames = ['spec-writer', 'builder'].concat(reviewers.map((r) => 'reviewer-' + r), ['approver']);
    const pipe = S.pipeline || {};
    // The boxes hang from the top of the line (aligned with the first reviewer), so opening one box's Advanced
    // field or a longer reviewer column never moves the others.
    const line = h('div', { class: 'rt-line', role: 'group', 'aria-label': 'The factory line' },
      h('div', { class: 'rt-col' }, routeBox('spec-writer', ov)),
      arrow(),
      h('div', { class: 'rt-col' }, routeBox('builder', ov)),
      arrow(),
      h('div', { class: 'rt-col rt-revs', role: 'group', 'aria-label': 'Reviewers (in parallel)' },
        h('div', { class: 'rt-revs-label' }, icon('hourglass', 'icon-xs'), reviewers.length + ' in parallel · wait for all'),
        h('div', { class: 'rt-revs-grid' }, reviewers.map((r) => routeBox('reviewer-' + r, ov))),
        h('div', { class: 'rt-revs-foot' }, h('span', { class: 'wire-label loop' }, 'any CHANGES → resume builder (max ' + (pipe.max_resumes !== undefined ? pipe.max_resumes : 2) + ')'))),
      arrow('PASS'),
      h('div', { class: 'rt-col' }, routeBox('approver', ov)),
      h('div', { class: 'rt-end', 'aria-hidden': 'true' }, icon('arrow-right', 'icon-sm'), h('span', { class: 'rt-end-node' }, icon('merge', 'icon-xs'), 'merge')));
    const caption = h('div', { class: 'rt-caption muted' }, 'All PASS → approver → merge' + (pipe.auto_merge === false ? ' (auto-merge off)' : '') + '. Escalations go to Needs human.');
    const side = ['lead', 'coder', 'repo-scout'].filter((n) => known.has(n) || isObj(ov[n]));
    const others = Array.from(new Set(Array.from(known).concat(Object.keys(ov)))).filter((n) => n !== '*' && lineNames.indexOf(n) < 0 && side.indexOf(n) < 0).sort();
    const presets = presetList(CFG.orig);
    let extra = null;
    if (presets.length) {
      const psel = h('select', { class: 'select select-sm rt-preset', 'aria-label': 'Apply a preset' }, h('option', { value: '' }, 'Apply a preset…'), presets.map((p) => h('option', { value: p.name, title: p.description || '' }, presetLabel(p.name))));
      psel.addEventListener('change', () => { if (psel.value) applyPreset(psel.value, psel); });
      extra = h('div', { class: 'rt-head-extra' }, psel);
    }
    return setCard('agents', 'Routing', 'Which model and driver every step runs on. Jobs take a snapshot when they are submitted, so saving never changes a running job.', [
      // Default (*) governs every step without a route of its own: it leads, next to the legend.
      h('div', { class: 'rt-top' },
        h('div', { class: 'rt-top-star' }, routeBox('*', ov)),
        h('div', { class: 'rt-top-text' },
          h('p', { class: 'rt-top-explain' }, 'A step without a route of its own falls back to ', h('strong', null, 'Default (*)'), ', then to its agent file’s tier (', h('span', { class: 'tag tag-outline' }, 'tier opus'), '). Pin a model on a step to route it on its own.'),
          h('div', { class: 'rt-legend' },
            h('span', { class: 'rt-legend-item' }, h('span', { class: 'tag tag-local' }, icon('server', 'icon-xs'), 'LOCAL'), 'loopback base_url'),
            h('span', { class: 'rt-legend-item' }, h('span', { class: 'tag tag-api' }, icon('cloud', 'icon-xs'), 'API'), 'hosted'),
            h('span', { class: 'rt-legend-item' }, h('span', { class: 'tag tag-mono' }, icon('gauge', 'icon-xs'), 'tok/s'), 'last Test in this browser (click to test)')))),
      line, caption,
      h('div', { class: 'rt-group-title' }, 'Above and beside the line'),
      h('div', { class: 'rt-grid' }, side.map((n) => routeBox(n, ov))),
      others.length ? [h('div', { class: 'rt-group-title' }, 'Other agents'), h('div', { class: 'rt-grid' }, others.map((n) => routeBox(n, ov)))] : null,
    ], extra);
  }

  function secPipeline() {
    const p = isObj(CFG.draft.pipeline) ? CFG.draft.pipeline : (CFG.draft.pipeline = {});
    const rev = h('input', { class: 'input mono-input', type: 'text', value: (Array.isArray(p.reviewers) ? p.reviewers : []).join(', '), spellcheck: 'false' });
    rev.addEventListener('input', () => {
      const list = rev.value.split(/[,\s]+/).map((s) => s.trim()).filter(Boolean);
      const bad = list.filter((x) => !NAME_RE.test(x));
      setInvalid('pipeline.reviewers', !list.length ? 'At least one reviewer' : bad.length ? 'Invalid name: ' + bad.join(', ') : null, rev);
      if (list.length && !bad.length) p.reviewers = list;
      onDraftChange();
    });
    return setCard('pipeline', 'Pipeline', 'The review line. Each reviewer name needs an agents/reviewer-<name>.md file.', [
      field('Reviewers', rev, 'Comma-separated; they run in parallel and the builder waits for all of them.'),
      errSlot('pipeline.reviewers'),
      h('div', { class: 'grid-form' },
        field('Max resumes', numIn(p, 'max_resumes', { min: 0, errKey: 'pipeline.max_resumes' }), 'Builder resumes after CHANGES before escalating.'),
        field('Max parallel jobs', numIn(p, 'max_parallel_jobs', { min: 1, errKey: 'pipeline.max_parallel_jobs' }), 'Worker threads.'),
        field('Max auto-merge lines', numIn(p, 'max_auto_merge_lines', { min: 0, errKey: 'pipeline.max_auto_merge_lines' }), 'Bigger diffs go to a human.')),
      checkIn('Auto-merge when every review passes and the approver approves', p, 'auto_merge', true),
    ]);
  }
  function secLimits() {
    const l = isObj(CFG.draft.limits) ? CFG.draft.limits : (CFG.draft.limits = {});
    const f = (label, key, hint) => field(label, numIn(l, key, { min: 1, errKey: 'limits.' + key }), hint);
    return setCard('limits', 'Limits', 'Per agent run (builtin driver).', [h('div', { class: 'grid-form' },
      f('Max turns', 'max_turns', 'Model calls per run.'), f('Max wall seconds', 'max_wall_seconds', 'Per run.'), f('Bash timeout (s)', 'bash_timeout', 'Per command.'),
      f('Tool output chars', 'tool_output_chars', 'Head + tail kept.'), f('Max context chars', 'max_context_chars', 'Oldest tool results are elided beyond this.'))]);
  }
  function secHarness() {
    const hz = isObj(CFG.draft.harness) ? CFG.draft.harness : (CFG.draft.harness = {});
    return setCard('harness', 'Harness', 'How the builtin agent loop runs tools.', [
      h('div', { class: 'grid-form' },
        field('Bash sandbox', selIn([['none', 'none'], ['bwrap', 'bwrap (bubblewrap)']], hz, 'sandbox'), 'bwrap confines Bash writes to the worktree.'),
        h('div', { class: 'field', style: { 'justify-content': 'flex-end' } }, checkIn('Allow network inside the sandbox', hz, 'sandbox_network', true))),
      field('Skills directories', linesIn(hz, 'skills_dirs', { placeholder: 'one absolute path per line' }), 'Searched after the default skill locations (.claude/.qwen/.agents skills).'),
      field('Extra read roots', linesIn(hz, 'extra_read_roots', { placeholder: 'one absolute path per line' }), 'Directories agents may read besides the worktree and job folder.'),
    ]);
  }
  function secMcp() {
    const servers = isObj(CFG.draft.mcp_servers) ? CFG.draft.mcp_servers : (CFG.draft.mcp_servers = {});
    const cards = Object.keys(servers).sort().map((name) => {
      const s = servers[name];
      if (!isObj(s)) return null;
      const args = h('textarea', { class: 'textarea mono-input', rows: Math.min(Math.max((s.args || []).length, 2), 8), spellcheck: 'false', placeholder: 'one argument per line' });
      args.value = (Array.isArray(s.args) ? s.args : []).join('\n');
      args.addEventListener('input', () => { s.args = args.value.split('\n').filter((x) => x.trim() !== ''); onDraftChange(); });
      const envKey = 'mcp_servers.' + name + '.env';
      const env = h('textarea', { class: 'textarea mono-input', rows: 2, spellcheck: 'false', placeholder: 'KEY=value, one per line' });
      env.value = Object.keys(isObj(s.env) ? s.env : {}).map((k) => k + '=' + s.env[k]).join('\n');
      env.addEventListener('input', () => {
        const out = {};
        let bad = null;
        for (const line of env.value.split('\n')) {
          if (!line.trim()) continue;
          const i = line.indexOf('=');
          if (i <= 0) { bad = 'Use KEY=value: ' + line.trim().slice(0, 40); break; }
          out[line.slice(0, i).trim()] = line.slice(i + 1);
        }
        setInvalid(envKey, bad, env);
        if (!bad) s.env = out;
        onDraftChange();
      });
      const tools = h('input', { class: 'input mono-input', type: 'text', value: Array.isArray(s.tools) ? s.tools.join(', ') : '', placeholder: 'all tools', spellcheck: 'false' });
      tools.addEventListener('input', () => { const list = tools.value.split(/[,\s]+/).filter(Boolean); s.tools = list.length ? list : null; onDraftChange(); });
      return h('div', { class: 'mcp-card' },
        h('div', { class: 'mcp-card-head' }, icon('layers', 'icon-sm'), h('span', { class: 'repo-name' }, name), h('span', { class: 'spacer' }), removeBtn('Remove MCP server ' + name, () => { delete servers[name]; rerenderSettings(); })),
        h('div', { class: 'grid-form' },
          field('Command', textIn(() => s.command, (v) => { s.command = v.trim(); }, { class: 'input mono-input', placeholder: '/path/to/server' })),
          field('Tools allowlist', tools, 'Comma-separated; empty exposes every tool.')),
        h('div', { class: 'grid-form' },
          field('Args', args),
          h('div', { class: 'field' }, h('span', { class: 'field-label' }, 'Environment'), env, h('div', { class: 'field-hint' }, 'Stored values show as ***; leave them to keep them.'), errSlot(envKey))));
    });
    return setCard('mcp_servers', 'MCP servers', 'stdio MCP servers the builtin harness can attach as tools (agents opt in with mcp: <name>).', [
      cards.filter(Boolean).length ? cards : h('p', { class: 'muted' }, 'No MCP servers configured.'),
      addNameRow('server-name', 'Add server', (name, check) => {
        if (check) return servers[name] ? 'A server with that name exists.' : null;
        servers[name] = { command: '', args: [], env: {}, tools: null };
        return null;
      }),
    ]);
  }
  function secEscalate() {
    return setCard('escalate_paths', 'Escalate paths', 'Globs (with ** support). A job that changes any matching path never auto-merges; it goes to needs-human. Repos can add their own.', [
      field('Protected globs', linesIn(CFG.draft, 'escalate_paths', { rows: 10, placeholder: '.github/**' }), 'One glob per line.'),
    ]);
  }
  async function checkScout(repo, notify) {
    try {
      const r = await api('/api/repos/' + enc(repo) + '/scout');
      const prev = CFG.scout.get(repo);
      CFG.scout.set(repo, { running: !!(r && r.running), draft: r && typeof r.draft === 'string' ? r.draft : null });
      if (notify && prev && prev.running && !(r && r.running)) {
        toast(r && r.draft ? ['Scout finished for ', h('strong', null, repo), '.'] : 'Scout finished without a draft for ' + repo + '.', { kind: r && r.draft ? 'ok' : 'error', action: r && r.draft ? { label: 'View draft', onClick: () => showScoutDraft(repo) } : null });
      }
    } catch (e) {
      if (notify) CFG.scout.set(repo, { running: false, draft: null, error: e.message });
    }
    refreshReposCard();
  }
  function refreshReposCard() {
    if (S.route.view === 'flow') invalidate('flow');
    const el = document.getElementById('set-repos');
    if (!el || !CFG.orig || S.route.view !== 'settings') return;
    const active = document.activeElement;
    const keep = active && el.contains(active) ? (active.getAttribute('placeholder') || active.textContent) : null;
    const inputs = Array.from(el.querySelectorAll('.mcp-card input')).map((i) => i.value);
    const fresh = secRepos();
    el.replaceWith(fresh);
    Array.from(fresh.querySelectorAll('.mcp-card input')).forEach((i, k) => { if (inputs[k]) i.value = inputs[k]; });
    if (keep) { const t = Array.from(fresh.querySelectorAll('input, button')).find((x) => (x.getAttribute('placeholder') || x.textContent) === keep); if (t) t.focus({ preventScroll: true }); }
  }
  async function pollScout(repo) {
    for (let i = 0; i < 900; i++) {
      await sleep(2000);
      const st = CFG.scout.get(repo);
      if (!st || !st.running) return;
      await checkScout(repo, true);
    }
  }
  async function startScout(repo) {
    CFG.scout.set(repo, { running: true, draft: (CFG.scout.get(repo) || {}).draft || null });
    refreshReposCard();
    try {
      await api('/api/repos/' + enc(repo) + '/scout', { method: 'POST' });
      toast(['Scouting ', h('strong', null, repo), '… the draft appears here when ready.'], { kind: 'info' });
      pollScout(repo);
    } catch (e) {
      CFG.scout.set(repo, { running: false, draft: null, error: e.message });
      toastError('Scout failed', e);
      refreshReposCard();
    }
  }
  function showScoutDraft(repo) {
    const st = CFG.scout.get(repo);
    if (!st || !st.draft) return;
    openModal({
      title: 'AGENTS.draft.md · ' + repo, wide: true,
      description: 'Drafted by repo-scout. Review it, then save it as AGENTS.md in the repo so every agent reads it.',
      body: h('div', { style: { border: '1px solid var(--border)', 'border-radius': '8px', padding: '14px 16px', 'max-height': '60vh', overflow: 'auto' } }, MD.render(st.draft)),
      actions: [{ label: 'Close', value: 'cancel' }, { label: 'Copy markdown', value: 'ok', primary: true, icon: 'copy' }],
      onSubmit: async () => { await copyText(st.draft, 'Draft copied'); return false; },
    });
  }
  function secRepos() {
    const repos = Array.isArray(CFG.draft.repos) ? CFG.draft.repos : (CFG.draft.repos = []);
    const rows = repos.filter((r) => r && r.name).map((r) => {
      const sc = CFG.scout.get(r.name) || {};
      const scoutBtn = btn(sc.running ? 'Scouting…' : 'Scout', { cls: 'btn-sm', icon: 'scout', disabled: !!sc.running, title: 'Run repo-scout to draft an AGENTS.md for this repo' });
      if (sc.running) scoutBtn.prepend(h('span', { class: 'spinner', 'aria-hidden': 'true' }));
      scoutBtn.addEventListener('click', () => startScout(r.name));
      const view = sc.draft ? btn('View draft', { cls: 'btn-sm btn-ghost', icon: 'eye', onClick: () => showScoutDraft(r.name) }) : null;
      return h('div', { class: 'repo-row' },
        h('div', { class: 'repo-row-main' }, h('span', { class: 'repo-name' }, r.name), h('span', { class: 'repo-path', title: r.path }, r.path)),
        field('Base branch', nullableText(r, 'base', { class: 'input input-sm mono-input', placeholder: 'auto-detect', 'aria-label': r.name + ' base branch' }), null, { cls: 'repo-base' }),
        field('Setup command', nullableText(r, 'setup', { class: 'input input-sm mono-input', placeholder: 'none', 'aria-label': r.name + ' setup command' }), null, { cls: 'repo-setup' }),
        h('div', { class: 'repo-actions' }, view, scoutBtn,
          removeBtn('Unregister repo ' + r.name, () => { CFG.draft.repos = repos.filter((x) => x !== r); rerenderSettings(); })),
        sc.error ? h('div', { class: 'field-error repo-error' }, 'Scout: ' + sc.error) : null);
    });
    const pathIn = h('input', { class: 'input mono-input', type: 'text', placeholder: '/home/you/code/my-app', spellcheck: 'false', autocomplete: 'off' });
    const nameIn = h('input', { class: 'input mono-input', type: 'text', placeholder: 'derived from the folder', spellcheck: 'false', autocomplete: 'off' });
    const err = h('div', { class: 'form-error', role: 'alert', hidden: true });
    const addBtn = btn('Add repo', { cls: 'btn-primary btn-sm', icon: 'plus' });
    const doAdd = async () => {
      const path = pathIn.value.trim();
      const name = nameIn.value.trim();
      err.hidden = true;
      if (!path || path.charAt(0) !== '/') { clear(err); err.appendChild(h('span', null, 'Enter the absolute path of the repo on the server (it starts with /).')); err.hidden = false; pathIn.focus(); return; }
      if (name && !/^[a-z0-9][a-z0-9._-]*$/.test(name)) { clear(err); err.appendChild(h('span', null, 'Names use lowercase letters, digits, . _ - and start with a letter or digit.')); err.hidden = false; nameIn.focus(); return; }
      setBusy(addBtn, true);
      try {
        const body = { path };
        if (name) body.name = name;
        const r = await api('/api/repos', { method: 'POST', body });
        const repo = r && r.repo;
        toast(['Added repo ', h('strong', null, (repo && repo.name) || name || path), '.'], { kind: 'ok' });
        pathIn.value = ''; nameIn.value = '';
        try {
          const fresh = await api('/api/config');
          if (isObj(fresh)) { CFG.orig.repos = clone(fresh.repos); CFG.draft.repos = clone(fresh.repos); }
        } catch (e2) { /* the next load picks it up */ }
        refreshState();
        invalidate('settings');
      } catch (e) {
        clear(err); err.appendChild(icon('alert', 'icon-sm')); err.appendChild(h('span', null, e.message)); err.hidden = false;
      } finally { setBusy(addBtn, false); }
    };
    addBtn.addEventListener('click', doAdd);
    for (const i of [pathIn, nameIn]) i.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); doAdd(); } });
    return setCard('repos', 'Repos', 'Git repositories the factory works on. The factory only touches them through worktrees, branches and the final merge.', [
      rows.length ? rows : h('p', { class: 'muted' }, 'No repos yet. Add one below.'),
      h('div', { class: 'mcp-card' },
        h('div', { class: 'grid-form' }, field('Absolute path', pathIn, 'The top level of a git checkout on the server.'), field('Name (optional)', nameIn, 'Used in URLs and job folders.')),
        err, h('div', null, addBtn)),
    ]);
  }
  function revealNavLink(nav) {
    const cur = nav && nav.querySelector('[aria-current="true"]');
    if (cur && nav.scrollWidth > nav.clientWidth) nav.scrollLeft = Math.max(0, cur.offsetLeft - nav.clientWidth / 2 + cur.offsetWidth / 2);
  }
  function updateSettingsNav() {
    for (const [id] of SETTINGS_SECTIONS) {
      const dot = document.querySelector('.settings-nav [data-sec="' + id + '"] .dirty-dot');
      if (dot) dot.hidden = !secDirty(id);
      const tag = document.querySelector('[data-dirty-for="' + id + '"]');
      if (tag) tag.hidden = !secDirty(id);
    }
  }
  function updateSaveBar() {
    const bar = CFG.saveBar;
    if (!bar) return;
    clear(bar);
    const dirty = CONFIG_KEYS.filter(sectionDirty);
    bar.hidden = !dirty.length && !CFG.saveError && !CFG.invalid.size;
    if (bar.hidden) return;
    const labels = Array.from(new Set(dirty.map((k) => (SETTINGS_SECTIONS.find((s) => s[0] === (SEC_OF[k] || k)) || [k, k])[1])));
    bar.appendChild(icon(CFG.invalid.size ? 'alert' : 'info', 'icon-sm'));
    bar.appendChild(h('span', { class: 'save-msg' }, dirty.length ? ['Unsaved changes in ', h('strong', null, labels.join(', ')), CFG.invalid.size ? ' · fix ' + plural(CFG.invalid.size, 'invalid field') + ' first' : '']
      : CFG.invalid.size ? 'Fix ' + plural(CFG.invalid.size, 'invalid field') + ' before saving.' : 'Not saved.'));
    // Name the first invalid field and why (its title holds setInvalid's message), with a way to it: a phone has no
    // hover, and the field may sit in a closed section further down.
    const bad = CFG.invalid.size ? Array.from(document.querySelectorAll('#settings-root [aria-invalid="true"]')).find((x) => x.closest('.save-bar') === null) : null;
    if (bad) {
      const name = bad.getAttribute('aria-label') || (bad.labels && bad.labels[0] ? bad.labels[0].textContent : '') || 'a field';
      const go = btn('Show it', { cls: 'btn-xs btn-ghost', icon: 'arrow-right', dataset: { action: 'show-invalid' } });
      go.addEventListener('click', () => {
        for (let el = bad.parentElement; el && el.id !== 'settings-root'; el = el.parentElement) {
          if (el.hidden) { el.hidden = false; const opener = el.id && document.querySelector('[aria-controls="' + CSS.escape(el.id) + '"]'); if (opener) opener.setAttribute('aria-expanded', 'true'); }
          if (el.tagName === 'DETAILS') el.open = true;
        }
        bad.scrollIntoView({ block: 'center' });
        bad.focus({ preventScroll: true });
      });
      bar.appendChild(h('span', { class: 'save-invalid' }, icon('alert', 'icon-xs'), h('span', null, '“' + name + '”' + (bad.title ? ': ' + bad.title : '')), go));
    }
    const discard = btn('Discard', { cls: 'btn-sm btn-ghost', disabled: CFG.saving });
    discard.addEventListener('click', () => { CFG.draft = clone(CFG.orig); CFG.invalid.clear(); CFG.saveError = null; CFG.saveSection = null; rerenderSettings(); });
    const save = btn('Save changes', { cls: 'btn-sm btn-primary', icon: 'check', disabled: !dirty.length || CFG.invalid.size > 0 || CFG.saving });
    if (CFG.saving) setBusy(save, true);
    save.addEventListener('click', saveConfig);
    add(bar, [discard, save]);
    if (CFG.saveError) bar.appendChild(h('div', { class: 'form-error', role: 'alert' }, icon('alert', 'icon-sm'), h('span', null, 'The server rejected the change: ' + CFG.saveError)));
  }
  async function saveConfig() {
    if (CFG.invalid.size) { toast('Fix the highlighted fields first.', { kind: 'error' }); return; }
    const patch = buildPatch();
    if (!Object.keys(patch).length) return;
    CFG.saving = true;
    updateSaveBar();
    try {
      const r = await api('/api/config', { method: 'POST', body: patch });
      CFG.orig = isObj(r) ? normalizeConfig(r) : CFG.orig;
      CFG.draft = clone(CFG.orig);
      CFG.saveError = null;
      CFG.saveSection = null;
      toast('Settings saved.', { kind: 'ok' });
      refreshState();
      loadSetup();
    } catch (e) {
      CFG.saveError = e.message;
      const m = /^([a-z_]+)[.:\s]/.exec(e.message || '');
      CFG.saveSection = m && CONFIG_KEYS.indexOf(m[1]) >= 0 ? m[1] : (Object.keys(patch).length === 1 ? Object.keys(patch)[0] : null);
      toast('Settings not saved: ' + e.message, { kind: 'error' });
    } finally {
      CFG.saving = false;
      invalidate('settings');
    }
  }
  RENDER.settings = function () {
    if (S.route.view !== 'settings') return;
    const root = document.getElementById('settings-root');
    const main = document.getElementById('main');
    const y = main.scrollTop;
    const active = document.activeElement;
    const activeId = active && root.contains(active) ? active.getAttribute('aria-label') || active.id : null;
    const oldNav = root.querySelector('.settings-nav');
    const navX = oldNav ? oldNav.scrollLeft : null;
    clear(root);
    const reload = btn('Reload', { cls: 'btn-sm btn-ghost', icon: 'refresh', title: 'Reload from the server (drops unsaved changes)' });
    reload.addEventListener('click', () => { CFG.orig = null; CFG.draft = null; invalidate('settings'); loadConfig(); });
    root.appendChild(viewHead('Settings', 'Saved to config.json on the server. Secrets never leave the server: keys show only as set or missing.', reload));
    if (!CFG.orig) {
      if (CFG.loadError) root.appendChild(emptyState('alert', 'Cannot load the config', CFG.loadError, [btn('Retry', { cls: 'btn-primary', icon: 'refresh', onClick: () => loadConfig() })]));
      else { root.appendChild(skeletonLines(12)); if (!CFG.loading) loadConfig(); }
      return;
    }
    const nav = h('nav', { class: 'settings-nav', 'aria-label': 'Settings sections' });
    for (const [id, label, ic] of SETTINGS_SECTIONS) {
      nav.appendChild(h('a', { href: '#/settings/' + secAnchor(id), dataset: { sec: id, anchor: secAnchor(id) }, 'aria-current': anchorSec(S.route.section) === secAnchor(id) ? 'true' : 'false' }, icon(ic, 'icon-sm'), label, h('span', { class: 'dirty-dot', hidden: !secDirty(id), title: 'Unsaved changes' })));
    }
    const sections = h('div', { class: 'settings-sections' });
    TEST_VIEWS.clear();
    const builders = [secRouting, () => settingsExt('page'), secProviders, secModels, secPipeline, secLimits, secHarness, secMcp, secEscalate, secRepos];
    for (const b of builders) {
      try { add(sections, b()); } catch (e) { console.error('settings section failed', e); sections.appendChild(callout('err', 'alert', 'This section could not be rendered.', String(e && e.message))); }
    }
    CFG.saveBar = h('div', { class: 'save-bar', role: 'region', 'aria-label': 'Unsaved changes' });
    sections.appendChild(CFG.saveBar);
    root.appendChild(h('div', { class: 'settings-layout' }, h('div', { class: 'settings-nav-wrap' }, nav), sections));
    updateSaveBar();
    main.scrollTop = y;
    // Narrow screens: the section nav is a sideways strip; keep its position, or show the current section.
    if (navX !== null) nav.scrollLeft = navX;
    else revealNavLink(nav);
    trackFades(nav);
    for (const t of root.querySelectorAll('.table-wrap')) trackFades(t);
    if (activeId) {
      const el = Array.from(root.querySelectorAll('input, select, textarea, button')).find((x) => (x.getAttribute('aria-label') || x.id) === activeId);
      if (el) el.focus({ preventScroll: true });
    }
  };

  // ================================================================ §25: setup status, secrets, procedures data
  // Feature-detected: a server without these endpoints (404) keeps every other view working; the new views say so.
  const F = window.SFFlows || null;
  const gone = (e) => e && (e.status === 404 || e.status === 405 || e.status === 501);
  // look: the answer of the owner's last look for model servers from this page (POST /api/setup/look-around).
  const SETUPST = { status: null, loading: null, error: null, missing: false, at: 0, look: null };
  /** The host's model_servers (or model_servers_error, for a look that broke) and model_servers_at from the newer of
   *  GET /api/setup's last look and this page's own (a status read while the look ran may arrive after its answer). */
  function keepLook() {
    const host = SETUPST.status && isObj(SETUPST.status.host) ? SETUPST.status.host : null;
    const mine = SETUPST.look;
    if (!host || !mine || (host.model_servers_at && String(host.model_servers_at) >= String(mine.at || ''))) return;
    delete host.model_servers;
    delete host.model_servers_error;
    if (mine.error) host.model_servers_error = String(mine.error);
    else host.model_servers = Array.isArray(mine.model_servers) ? mine.model_servers : [];
    host.model_servers_at = mine.at || null;
  }
  function loadSetup() {
    if (SETUPST.loading) return SETUPST.loading;
    SETUPST.loading = api('/api/setup')
      .then((r) => { SETUPST.status = isObj(r) ? r : null; SETUPST.error = null; SETUPST.missing = false; keepLook(); })
      .catch((e) => { if (gone(e)) SETUPST.missing = true; else if (e.status !== 401) SETUPST.error = e.message; })
      .finally(() => { SETUPST.loading = null; SETUPST.at = Date.now(); invalidate('grid', 'flow', 'doctorBadge'); });
    return SETUPST.loading;
  }
  const SECRETS = { map: null, loading: null, missing: false };
  function loadSecrets() {
    if (SECRETS.loading) return SECRETS.loading;
    SECRETS.loading = api('/api/secrets').then((r) => {
      const map = {};
      const list = r && Array.isArray(r.secrets) ? r.secrets : Array.isArray(r) ? r : null;
      if (list) { for (const x of list) if (isObj(x) && x.name) map[String(x.name)] = x.set === true; }
      else if (isObj(r)) for (const k of Object.keys(r)) map[k] = isObj(r[k]) ? r[k].set === true : r[k] === true;
      SECRETS.map = map;
      SECRETS.missing = false;
    }).catch((e) => { if (gone(e)) SECRETS.missing = true; })
      .finally(() => { SECRETS.loading = null; invalidate('flow'); });
    return SECRETS.loading;
  }
  const DOCS = { data: null, loading: null, missing: false, error: null };
  function loadDocs() {
    if (DOCS.loading) return DOCS.loading;
    DOCS.loading = api('/api/docs/procedures').then((r) => { DOCS.data = F.normalizeDocs(r); DOCS.missing = false; DOCS.error = null; })
      .catch((e) => { if (gone(e)) DOCS.missing = true; else DOCS.error = e.message; })
      .finally(() => { DOCS.loading = null; invalidate('procedures', 'grid'); });
    return DOCS.loading;
  }
  /** agent.updated (§25): an agent file changed (here or elsewhere); the lists and its detail follow. */
  function onAgentUpdated(d) {
    const name = d && (typeof d.agent === 'string' ? d.agent : isObj(d.agent) ? d.agent.name : d.name);
    // {name, agent: null}: the agent is gone (a user-only agent was reset); nothing to load again.
    const gone = isObj(d) && d.agent === null;
    if (name) AG.cache.delete(name);
    refreshState();
    if (CFG.orig && !configDirty()) loadConfig();
    if (!gone && S.route.view === 'agents' && name && AG.sel === name) loadAgent(name, true);
  }
  const CHAT_AGENT_TEXT = { coder: 'all tools, on its own branch', analyst: 'reverse engineering with REA over MCP' };
  /** Agents a chat can run: what the server lists (chat_agents), else coder and analyst where they exist. */
  function chatAgents() {
    if (Array.isArray(S.chatAgents) && S.chatAgents.length) return S.chatAgents.map(String);
    const names = ['coder', 'analyst'].filter((n) => agentDef(n));
    return names.length ? names : ['coder'];
  }
  const DELIVERY_SHORT = { merge: 'Merge', branch: 'Keep branch', folder: 'Export folder' };
  function deliveryDefault() { return F ? F.deliveryDefault({ cfg: CFG.orig }) : 'merge'; }
  /** "Output: Merge | Keep branch | Export folder" with one line on the chosen mode (§25). value() is undefined until
   *  a mode is picked, so servers without delivery modes never see the field unless you choose one. */
  function deliveryChooser(o) {
    o = o || {};
    let def = deliveryDefault();
    let chosen = null;
    const name = (o.id || 'dl') + '-' + (++modalSeq);
    const inputs = {};
    const line = h('div', { class: 'field-hint dl-line' });
    const segs = Object.keys(DELIVERY_UI).map((m) => {
      const inp = h('input', { type: 'radio', name, value: m, class: 'dl-radio' });
      inputs[m] = inp;
      inp.addEventListener('change', () => { if (inp.checked) { chosen = m; paint(); if (o.onChange) o.onChange(m); } });
      return h('label', { class: 'dl-seg', dataset: { mode: m } }, inp, icon(DELIVERY_UI[m].icon, 'icon-xs'), h('span', null, DELIVERY_SHORT[m]), h('span', { class: 'dl-def', hidden: true }, 'default'));
    });
    const group = h('div', { class: 'dl-segs', role: 'radiogroup', 'aria-label': 'Output' }, segs);
    const wrap = h('div', { class: 'field dl-field', dataset: { field: 'delivery' } }, h('span', { class: 'field-label' }, 'Output'), group, line, o.hint ? h('div', { class: 'field-hint' }, o.hint) : null);
    const paint = () => {
      const cur = chosen || def;
      for (const m of Object.keys(inputs)) {
        inputs[m].checked = m === cur;
        const seg = inputs[m].parentNode;
        seg.classList.toggle('is-on', m === cur);
        seg.querySelector('.dl-def').hidden = m !== def;
      }
      line.textContent = F ? F.DELIVERY_LINE[cur] : '';
    };
    paint();
    if (!CFG.orig) ensureConfig().then(() => { def = deliveryDefault(); paint(); if (o.onChange) o.onChange(chosen || def); }).catch(() => {});
    // A mode you picked is always sent (the job runs with what you saw, even if the saved default changes meanwhile);
    // untouched, the field is left out and the server's default applies.
    return { el: wrap, value: () => chosen || undefined, mode: () => chosen || def, differs: () => !!chosen && chosen !== def };
  }
  /** The push of base after a merge (job.push, DESIGN §40), as a chip on the job card: its outcome in words, never a
   *  bare colour; no chip before the push was asked for. */
  function pushChip(job) {
    const p = isObj(job.push) ? job.push : null;
    if (!p) return null;
    const o = p.outcome;
    const words = o === 'succeeded' ? 'pushed' : o === 'failed' ? 'push failed' : o === 'unknown' ? 'push: outcome unknown' : 'pushing…';
    const cls = o === 'succeeded' ? 'tag-outline' : o === 'failed' ? 'tag-err' : 'tag-warn';
    return h('span', { class: 'tag ' + cls + ' tag-push', title: 'Push of ' + (job.base || 'the base branch') + ' to ' + String(p.remote || 'the remote') + ': ' + (o ? WORDS.outcome[o] || o : 'asked, no answer yet'), dataset: { push: String(o || 'pending') } },
      icon('git', 'icon-xs'), words);
  }
  /** The drawer's push box (job.push): the outcome in words, the remote and the commit, what git answered (its own
   *  line for the ref), and, after a look at the remote, what the remote has; an unknown one links its tracker item. */
  function pushBox(job) {
    const p = job.push;
    const o = p.outcome;
    const resp = isObj(p.response) ? p.response : null;
    const seen = isObj(p.observed) ? p.observed : null;
    const base = job.base || 'the base branch';
    const remote = String(p.remote || 'the remote');
    const title = o === 'succeeded' ? 'Pushed ' + base + ' to ' + remote : o === 'failed' ? 'The push of ' + base + ' to ' + remote + ' failed'
      : o === 'unknown' ? 'The push of ' + base + ' to ' + remote + ': outcome unknown' : 'Pushing ' + base + ' to ' + remote + '…';
    const tone = o ? WORDS.outcomeTone[o] || 'warn' : 'accent';
    return h('div', { class: 'push-box tone-' + tone, dataset: { push: String(o || 'pending') } },
      h('span', { class: 'push-icon' }, icon(o === 'succeeded' ? 'git' : 'alert', 'icon-sm')),
      h('div', { class: 'push-main' },
        h('div', { class: 'push-title' }, title, ' ', h('span', { class: 'pill no-dot tone-' + tone }, o ? WORDS.outcome[o] || String(o) : 'No answer yet')),
        o === 'unknown' ? h('p', { class: 'push-sub' }, 'It may or may not have reached ' + remote + '. Look at the remote before you push again: nothing is pushed again by itself.') : null,
        h('dl', { class: 'sf-kv push-kv' },
          h('dt', null, 'Remote'), h('dd', null, h('code', null, remote), p.ref ? [' ', h('code', { class: 'sf-muted' }, String(p.ref))] : null),
          p.commit ? [h('dt', null, 'Commit'), h('dd', null, h('code', { title: String(p.commit) }, String(p.commit).slice(0, 12)))] : null,
          resp && Array.isArray(resp.ids) && resp.ids.length ? [h('dt', null, 'Git said'), h('dd', null, resp.ids.map((x) => h('code', { class: 'push-said' }, String(x))))] : null,
          resp && resp.status !== null && resp.status !== undefined ? [h('dt', null, 'Exit status'), h('dd', null, String(resp.status))] : null,
          seen ? [h('dt', null, 'Looked again'), h('dd', { dataset: { part: 'observed' } }, seen.error ? 'the remote could not be asked: ' + String(seen.error)
            : seen.remote_commit ? ['the remote has ' + base + ' at ', h('code', { title: String(seen.remote_commit) }, String(seen.remote_commit).slice(0, 12)), seen.remote_commit === p.commit ? ' (this commit)' : ' (not this commit)']
              : 'the remote does not have it')] : null,
          p.detail ? [h('dt', null, 'Detail'), h('dd', { class: 'push-detail' }, String(p.detail))] : null,
          p.item ? [h('dt', null, 'To check'), h('dd', null, itemLink(String(p.item)))] : null)));
  }
  /** The drawer of a delivered job: where its work is, with a copy button; Merge it now is in the actions. */
  function deliveredBox(job) {
    const mode = jobDelivery(job) || 'branch';
    const path = mode === 'folder' ? jobOutputPath(job) : null;
    const what = path || job.branch || '';
    const copy = what ? h('button', { type: 'button', class: 'btn btn-ghost btn-icon btn-xs', title: path ? 'Copy the folder path' : 'Copy the branch name', 'aria-label': path ? 'Copy the output folder path' : 'Copy the branch name', dataset: { action: 'copy-output' } }, icon('copy', 'icon-xs')) : null;
    if (copy) copy.addEventListener('click', () => copyText(what, path ? 'Folder path copied' : 'Branch name copied'));
    return h('div', { class: 'delivered-box', dataset: { mode } },
      h('span', { class: 'delivered-icon' }, icon(DELIVERY_UI[mode].icon, 'icon-sm')),
      h('div', { class: 'delivered-main' },
        h('div', { class: 'delivered-title' }, mode === 'folder' ? 'Delivered to the output folder' : 'Delivered on its branch'),
        what ? h('div', { class: 'delivered-path' }, h('span', { class: 'mono', title: what }, what), copy) : null,
        h('div', { class: 'delivered-sub' }, mode === 'folder'
          ? 'changes.patch, the changed files and REPORT.md. The branch is kept: Merge it now merges it into ' + (job.base || 'the base branch') + '.'
          : 'Approved and kept for you. Merge it now merges it into ' + (job.base || 'the base branch') + ', or open a pull request from the branch.')));
  }

  // ================================================================ maps (§25): layered layout + drawing
  // One small engine draws the flow maps (wizard → Map), the process maps in Procedures and the Grid Card in the
  // calm document style of the owner's maps: rounded boxes, pills, decision diamonds and labelled arrows.
  let mapFamily = null;
  function mfont(kind) {
    if (!mapFamily) mapFamily = getComputedStyle(document.body).fontFamily;
    return (kind === 'node' ? '600 12.5px ' : kind === 'small' ? '600 11.5px ' : '500 11px ') + mapFamily;
  }
  function labelLines(text, maxW, kind, max) {
    const out = [];
    for (const part of String(text || '').split('\n')) if (part.trim()) out.push.apply(out, wrapLines(part, maxW, mfont(kind), max || 3));
    return out.slice(0, max || 4);
  }
  /** text cut to width with an ellipsis (a long single word such as a model id too). */
  function fitText(text, width, font) {
    let t = String(text || '');
    if (textWidth(t, font) <= width) return t;
    while (t.length > 1 && textWidth(t + '…', font) > width) t = t.slice(0, -1);
    return t.replace(/[\s·-]+$/, '') + '…';
  }
  /** A node's size from its label and optional one-line sub (status). */
  function measureNode(n) {
    const shape = n.shape || 'box';
    const kind = shape === 'small' || n.small ? 'small' : 'node';
    const maxW = n.maxW || (n.small ? 104 : shape === 'diamond' ? 124 : 168);
    const lines = labelLines(n.label, maxW, kind, 3);
    const lw = Math.max(36, ...lines.map((l) => textWidth(l, mfont(kind))));
    const sub = n.sub ? [fitText(n.sub, n.w ? n.w - 14 : Math.max(lw, n.subW || 124), mfont('edge'))] : [];
    const sw = sub.length ? textWidth(sub[0], mfont('edge')) : 0;
    const inner = Math.max(lw, sw);
    if (shape === 'diamond') return { lines, sub, w: n.w || Math.max(n.small ? 104 : 124, Math.round(inner * 1.55 + (n.small ? 30 : 44))), h: n.h || Math.max(n.small ? 50 : 60, lines.length * 15 + (n.small ? 30 : 40) + (sub.length ? 13 : 0)) };
    const h0 = shape === 'small' ? 14 : 22;
    return { lines, sub, w: n.w || Math.max(n.minW || 90, Math.round(inner + (shape === 'pill' ? 36 : 30))), h: n.h || lines.length * 15 + h0 + (sub.length ? 14 : 0) };
  }
  /** Where the segment from a node's centre towards p leaves its outline (diamonds, boxes, pills). */
  function portToward(n, p) {
    const dx = p.x - n.x;
    const dy = p.y - n.y;
    if (!dx && !dy) return { x: n.x, y: n.y };
    if (n.shape === 'diamond') {
      const t = 1 / (Math.abs(dx) / (n.w / 2) + Math.abs(dy) / (n.h / 2));
      return { x: n.x + dx * t, y: n.y + dy * t };
    }
    const tx = dx ? (n.w / 2) / Math.abs(dx) : Infinity;
    const ty = dy ? (n.h / 2) / Math.abs(dy) : Infinity;
    const t = Math.min(tx, ty);
    return { x: n.x + dx * t, y: n.y + dy * t };
  }
  const r1 = (v) => Math.round(v * 10) / 10;
  /** A smooth top-to-bottom path through points (vertical tangents). */
  function curvePath(pts) {
    let d = 'M' + r1(pts[0].x) + ',' + r1(pts[0].y);
    for (let i = 1; i < pts.length; i++) {
      const a = pts[i - 1];
      const b = pts[i];
      const dy = Math.max(10, (b.y - a.y) * 0.5);
      d += ' C' + r1(a.x) + ',' + r1(a.y + dy) + ' ' + r1(b.x) + ',' + r1(b.y - dy) + ' ' + r1(b.x) + ',' + r1(b.y);
    }
    return d;
  }
  /** An orthogonal path through points with rounded corners. */
  function orthoPath(pts, radius) {
    const rr = radius === undefined ? 9 : radius;
    let d = 'M' + r1(pts[0].x) + ',' + r1(pts[0].y);
    for (let i = 1; i < pts.length - 1; i++) {
      const a = pts[i - 1];
      const b = pts[i];
      const c = pts[i + 1];
      const lab = Math.hypot(b.x - a.x, b.y - a.y);
      const lbc = Math.hypot(c.x - b.x, c.y - b.y);
      const k = Math.min(rr, lab / 2, lbc / 2);
      const p1 = { x: b.x + (a.x - b.x) * (k / (lab || 1)), y: b.y + (a.y - b.y) * (k / (lab || 1)) };
      const p2 = { x: b.x + (c.x - b.x) * (k / (lbc || 1)), y: b.y + (c.y - b.y) * (k / (lbc || 1)) };
      d += ' L' + r1(p1.x) + ',' + r1(p1.y) + ' Q' + r1(b.x) + ',' + r1(b.y) + ' ' + r1(p2.x) + ',' + r1(p2.y);
    }
    const z = pts[pts.length - 1];
    return d + ' L' + r1(z.x) + ',' + r1(z.y);
  }
  /** Ranks by dependency depth (longest path; back edges found by a depth-first search are left out), the shared
   *  layering of the plan graph and the process maps. */
  function dagRanks(ids, edges) {
    const out = new Map(ids.map((id) => [id, []]));
    for (const e of edges) if (out.has(e.from) && out.has(e.to) && e.from !== e.to) out.get(e.from).push(e);
    const state = new Map();
    const post = [];
    const dfs = (id) => {
      state.set(id, 1);
      for (const e of out.get(id)) { const s = state.get(e.to); if (s === 1) e.back = true; else if (!s) dfs(e.to); }
      state.set(id, 2);
      post.push(id);
    };
    for (const id of ids) if (!state.get(id)) dfs(id);
    const topo = post.slice().reverse();
    const rank = new Map(ids.map((id) => [id, 0]));
    for (const id of topo) for (const e of out.get(id)) if (!e.back) rank.set(e.to, Math.max(rank.get(e.to), rank.get(id) + 1));
    return { rank, topo };
  }
  /** Layered layout, top to bottom (Sugiyama-style): ranks, dummy points on long edges, barycentre ordering, then
   *  x positions packed under their neighbours. Back edges loop around the right side with their label. */
  function layoutMap(graph, opts) {
    opts = opts || {};
    const RG = opts.rankGap || 52;
    const NG = opts.nodeGap || 26;
    const PAD = opts.pad || 16;
    const nodes = new Map();
    graph.nodes.forEach((n, i) => nodes.set(n.id, Object.assign({}, n, measureNode(n), { i, x: 0, y: 0 })));
    const edges = graph.edges.filter((e) => nodes.has(e.from) && nodes.has(e.to) && e.from !== e.to).map((e, i) => Object.assign({ i }, e));
    const { rank, topo } = dagRanks(Array.from(nodes.keys()), edges);
    for (const [id, r] of rank) nodes.get(id).rank = r;
    const layers = [];
    const put = (n) => { (layers[n.rank] = layers[n.rank] || []).push(n); };
    // Layers start in the graph's own order (the order the author listed: Welcome's cards stay in card order).
    void topo;
    for (const n of nodes.values()) put(n);
    const up = new Map();
    const down = new Map();
    const link = (m, k, v) => { if (!m.has(k)) m.set(k, []); m.get(k).push(v); };
    for (const e of edges) {
      if (e.back) continue;
      const a = nodes.get(e.from);
      const b = nodes.get(e.to);
      const chain = [a];
      for (let r = a.rank + 1; r < b.rank; r++) { const d = { id: '_d' + e.i + '_' + r, dummy: true, rank: r, w: 8, h: 0, x: 0, y: 0 }; put(d); chain.push(d); }
      chain.push(b);
      e.chain = chain;
      for (let k = 0; k + 1 < chain.length; k++) { link(down, chain[k], chain[k + 1]); link(up, chain[k + 1], chain[k]); }
    }
    const pos = new Map();
    layers.forEach((L) => L && L.forEach((n, i) => pos.set(n, i)));
    const bary = (n, nb) => { const l = nb.get(n); return l && l.length ? l.reduce((s, x) => s + pos.get(x), 0) / l.length : null; };
    for (let it = 0; it < 8; it++) {
      const goingDown = it % 2 === 0;
      for (let k = 0; k < layers.length; k++) {
        const r = goingDown ? k : layers.length - 1 - k;
        const L = layers[r];
        if (!L || L.length < 2) continue;
        const keyed = L.map((n) => { const b = bary(n, goingDown ? up : down); return { n, b: b === null ? pos.get(n) : b, o: pos.get(n) }; });
        keyed.sort((p, q) => p.b - q.b || p.o - q.o);
        layers[r] = keyed.map((x) => x.n);
        layers[r].forEach((n, i) => pos.set(n, i));
      }
    }
    const gap = (a, b) => (a.dummy && b.dummy ? NG * 0.9 : a.dummy || b.dummy ? NG * 0.8 : NG);
    const pack = (L, ideal) => {
      const x = ideal.slice();
      for (let i = 1; i < L.length; i++) x[i] = Math.max(x[i], x[i - 1] + (L[i - 1].w + L[i].w) / 2 + gap(L[i - 1], L[i]));
      const shift = ideal.reduce((s, v, i) => s + v - x[i], 0) / (L.length || 1);
      return x.map((v) => v + shift);
    };
    for (const L of layers) {
      if (!L) continue;
      let acc = 0;
      const xs = L.map((n, i) => { if (i) acc += (L[i - 1].w + n.w) / 2 + gap(L[i - 1], n); return acc; });
      L.forEach((n, i) => { n.x = xs[i] - acc / 2; });
    }
    const mean = (l) => l.reduce((s, q) => s + q.x, 0) / l.length;
    for (let it = 0; it < 5; it++) {
      for (let r = 1; r < layers.length; r++) {
        const L = layers[r];
        if (!L) continue;
        const xs = pack(L, L.map((n) => (up.get(n) && up.get(n).length ? mean(up.get(n)) : n.x)));
        L.forEach((n, i) => { n.x = xs[i]; });
      }
      if (it === 4) break;
      for (let r = layers.length - 2; r >= 0; r--) {
        const L = layers[r];
        if (!L) continue;
        const xs = pack(L, L.map((n) => (down.get(n) && down.get(n).length ? (mean(down.get(n)) + n.x) / 2 : n.x)));
        L.forEach((n, i) => { n.x = xs[i]; });
      }
    }
    // y: rank bands
    let y = PAD;
    const bandY = [];
    for (let r = 0; r < layers.length; r++) {
      const L = layers[r] || [];
      const hh = Math.max(8, ...L.map((n) => n.h));
      bandY[r] = y + hh / 2;
      for (const n of L) n.y = bandY[r];
      y += hh + RG;
    }
    const real = Array.from(nodes.values());
    let minX = Infinity;
    for (const L of layers) if (L) for (const n of L) minX = Math.min(minX, n.x - n.w / 2);
    const dx = PAD - minX;
    for (const L of layers) if (L) for (const n of L) n.x += dx;
    let width = Math.max(...real.map((n) => n.x + n.w / 2)) + PAD;
    const height = y - RG + PAD;
    // ports: spread several edges along a box's top or bottom, diamonds use their outline
    const outs = new Map();
    const ins = new Map();
    for (const e of edges) { if (e.back) continue; link(outs, e.from, e); link(ins, e.to, e); }
    const spread = (n, list, side) => {
      list.sort((p, q) => (side === 'out' ? p.chain[1].x - q.chain[1].x : p.chain[p.chain.length - 2].x - q.chain[q.chain.length - 2].x));
      list.forEach((e, k) => {
        const next = side === 'out' ? e.chain[1] : e.chain[e.chain.length - 2];
        let pt;
        if (n.shape === 'diamond') {
          // Several routes leave (or enter) a diamond spread along its lower (upper) edges, as in the owner's maps.
          if (list.length === 1) pt = { x: n.x, y: n.y + (side === 'out' ? n.h / 2 : -n.h / 2) };
          else {
            const t = 0.2 + 0.6 * (k / (list.length - 1));
            const dy = t <= 0.5 ? t * n.h : n.h / 2 - (t - 0.5) * n.h;
            pt = { x: n.x - n.w / 2 + t * n.w, y: n.y + (side === 'out' ? dy : -dy) };
          }
        }
        else {
          // Arrows into or out of one box stay at least 20px apart (when the box is wide enough) so each reads alone.
          const step = Math.min(22, (n.w - 24) / Math.max(1, list.length - 1));
          pt = { x: n.x + (k - (list.length - 1) / 2) * step, y: n.y + (side === 'out' ? n.h / 2 : -n.h / 2) };
        }
        if (side === 'out') e.p0 = pt; else e.p1 = pt;
      });
    };
    for (const [id, list] of outs) spread(nodes.get(id), list, 'out');
    for (const [id, list] of ins) spread(nodes.get(id), list, 'in');
    const out = [];
    const labels = [];
    const boxes = real.map((n) => ({ x: n.x - n.w / 2 - 2, y: n.y - n.h / 2 - 2, w: n.w + 4, h: n.h + 4 }));
    const overlap = (b, o) => b.x < o.x + o.w && b.x + b.w > o.x && b.y < o.y + o.h && b.y + b.h > o.y;
    const hits = (b) => boxes.concat(labels).some((o) => overlap(b, o));
    // Straight segments, as in the owner's maps: a near-vertical drop is snapped to a true vertical.
    const routes = [];
    for (const e of edges) {
      if (e.back) continue;
      const pts = [e.p0].concat(e.chain.slice(1, -1).map((d) => ({ x: d.x, y: d.y })), [e.p1]).map((q) => ({ x: q.x, y: q.y }));
      for (let k = 1; k < pts.length; k++) if (Math.abs(pts[k].x - pts[k - 1].x) < 7) pts[k].x = pts[k - 1].x;
      routes.push({ e, pts });
    }
    // Points along every route, so a label is never put across another arrow.
    const samples = routes.map((r) => {
      const outp = [];
      for (let k = 1; k < r.pts.length; k++) {
        const a0 = r.pts[k - 1];
        const b0 = r.pts[k];
        const len = Math.hypot(b0.x - a0.x, b0.y - a0.y);
        for (let t = 0; t <= len; t += 5) outp.push({ x: a0.x + (b0.x - a0.x) * (t / (len || 1)), y: a0.y + (b0.y - a0.y) * (t / (len || 1)) });
      }
      return outp;
    });
    const crosses = (box, ri) => samples.some((list, j) => j !== ri && list.some((q) => q.x > box.x && q.x < box.x + box.w && q.y > box.y && q.y < box.y + box.h));
    routes.forEach((r, ri) => {
      const e = r.e;
      const item = { from: e.from, to: e.to, label: e.label || '', pts: r.pts, cls: e.cls || '' };
      if (item.label) {
        // The label sits just after the arrow leaves its node, beside it (the owner's "Ready" under the diamond): first
        // a spot near the start clear of nodes, labels and other arrows; then one near the start that only crosses an
        // arrow (its background hides the line there); only then further along the route.
        const lw = textWidth(item.label, mfont('edge')) + 10;
        const near = [];
        const far = [];
        let walked = 0;
        for (let k = 1; k < r.pts.length; k++) {
          const a0 = r.pts[k - 1];
          const b0 = r.pts[k];
          const len = Math.hypot(b0.x - a0.x, b0.y - a0.y) || 1;
          const ux = (b0.x - a0.x) / len;
          const uy = (b0.y - a0.y) / len;
          const pref = ux < -0.15 ? -1 : 1;
          for (const side of [pref, -pref]) {
            for (const dist of [14, 24, 36, 50, len / 2]) {
              if (dist > len - 8 && dist !== len / 2) continue;
              const m = { x: a0.x + ux * dist, y: a0.y + uy * dist };
              const box = { x: side > 0 ? m.x + 5 : m.x - 5 - lw, y: m.y - 8, w: lw, h: 16 };
              const c = { x: side > 0 ? m.x + 8 : m.x - 8, y: m.y, box, anchor: side > 0 ? 'start' : 'end' };
              (walked + dist <= 64 ? near : far).push(c);
            }
          }
          walked += len;
        }
        let placed = near.find((c) => !hits(c.box) && !crosses(c.box, ri)) || near.find((c) => !hits(c.box)) || far.find((c) => !hits(c.box) && !crosses(c.box, ri)) || null;
        if (!placed) {
          const m = r.pts[Math.floor(r.pts.length / 2)];
          placed = { x: m.x + 8, y: m.y, box: { x: m.x + 5, y: m.y - 8, w: lw, h: 16 }, anchor: 'start' };
        }
        labels.push(placed.box);
        Object.assign(item, { lx: placed.x, ly: placed.y, lw, anchor: placed.anchor, lbox: placed.box });
      }
      out.push(item);
    });
    // Fit the bounds to everything drawn, labels included (a label on the left edge is never cut off).
    let left = Infinity;
    for (const n of real) left = Math.min(left, n.x - n.w / 2);
    for (const l of labels) left = Math.min(left, l.x);
    if (left < PAD) {
      const sh = PAD - left;
      for (const L of layers) if (L) for (const n of L) n.x += sh;
      for (const it of out) { for (const q of it.pts) q.x += sh; if (it.lbox) { it.lx += sh; it.lbox.x += sh; } }
    }
    width = Math.max(width, ...real.map((n) => n.x + n.w / 2 + PAD), ...out.filter((it) => it.lbox).map((it) => it.lbox.x + it.lbox.w + PAD));
    for (const it of out) { it.d = orthoPath(it.pts, 8); delete it.pts; delete it.lbox; }
    // back edges: out of the source's right side, up the right margin, into the target's right side
    let lane = 0;
    for (const e of edges) {
      if (!e.back) continue;
      const a = nodes.get(e.from);
      const b = nodes.get(e.to);
      const lo = Math.min(a.rank, b.rank);
      const hi = Math.max(a.rank, b.rank);
      let right = Math.max(a.x + a.w / 2, b.x + b.w / 2);
      for (const n of real) if (n.rank >= lo && n.rank <= hi) right = Math.max(right, n.x + n.w / 2);
      const xr = right + 22 + 14 * lane++;
      const pa = a.shape === 'diamond' ? { x: a.x + a.w / 2, y: a.y } : { x: a.x + a.w / 2, y: a.y };
      const pb = b.shape === 'diamond' ? { x: b.x + b.w / 2, y: b.y } : { x: b.x + b.w / 2, y: b.y };
      const item = { from: e.from, to: e.to, label: e.label || '', d: orthoPath([pa, { x: xr, y: pa.y }, { x: xr, y: pb.y }, pb], 10), cls: 'back ' + (e.cls || ''), back: true };
      if (item.label) {
        const ls = labelLines(item.label, 150, 'edge', 3);
        item.lines = ls;
        // Near where the loop leaves its node (the bottom of the side lane), like every other label.
        item.lx = xr + 7;
        item.ly = pa.y - 14 - (ls.length - 1) * 14;
        item.anchor = 'start';
        width = Math.max(width, xr + 12 + Math.max(...ls.map((l) => textWidth(l, mfont('edge')))) + PAD);
      } else width = Math.max(width, xr + PAD);
      out.push(item);
    }
    return { width: Math.ceil(width), height: Math.ceil(height), nodes: real, edges: out };
  }
  /** Draw a layout as SVG. o: {label, onNode(node, el), nodeClass(node), selected, tone(node), title(node)}. */
  function drawMap(layout, o) {
    o = o || {};
    const svg = svgEl('svg', { class: 'map-svg' + (o.cls ? ' ' + o.cls : ''), width: layout.width, height: layout.height, viewBox: '0 0 ' + layout.width + ' ' + layout.height, role: 'group', 'aria-label': o.label || 'Map' });
    const uid = 'm' + (++modalSeq);
    const defs = svgEl('defs');
    for (const kind of ['base', 'hot', 'loop', 'human', 'ok']) {
      const mk = svgEl('marker', { id: uid + '-' + kind, viewBox: '0 0 10 10', refX: 9, refY: 5, markerWidth: 7, markerHeight: 7, orient: 'auto-start-reverse' });
      mk.appendChild(svgEl('path', { d: 'M1 1.2 L9 5 L1 8.8 z', class: 'map-arrow ' + kind }));
      defs.appendChild(mk);
    }
    svg.appendChild(defs);
    for (const p of layout.panels || []) {
      const g = svgEl('g', { class: 'map-panel' + (p.cls ? ' ' + p.cls : '') });
      g.appendChild(svgEl('rect', { x: p.x, y: p.y, width: p.w, height: p.h, rx: 14 }));
      const t = svgEl('text', { x: p.x + 14, y: p.titleY || p.y + 19, class: 'map-panel-title' });
      t.textContent = p.title;
      g.appendChild(t);
      svg.appendChild(g);
    }
    const eg = svgEl('g', { class: 'map-edges' });
    const lg = svgEl('g', { class: 'map-labels' });
    for (const e of layout.edges) {
      const hot = o.hotEdge ? o.hotEdge(e) : false;
      // A route's meaning (to a human, a loop back, the default output) keeps its colour hot or not; its label matches.
      const sem = /\bhuman\b/.test(e.cls || '') ? 'human' : /\bok\b/.test(e.cls || '') ? 'ok' : /\bloop\b|\bback\b/.test(e.cls || '') ? 'loop' : null;
      const kind = sem || (hot ? 'hot' : 'base');
      const path = svgEl('path', { d: e.d, class: 'map-edge' + (e.cls ? ' ' + e.cls : '') + (hot ? ' is-hot' : ''), 'marker-end': 'url(#' + uid + '-' + kind + ')' });
      path.dataset.from = e.from;
      path.dataset.to = e.to;
      eg.appendChild(path);
      if (e.label) {
        const lines = e.lines || [e.label];
        const anchor = e.anchor || 'middle';
        const w = Math.max(...lines.map((l) => textWidth(l, mfont('edge')))) + 8;
        const x0 = anchor === 'start' ? e.lx - 4 : anchor === 'end' ? e.lx - w + 4 : e.lx - w / 2;
        lg.appendChild(svgEl('rect', { class: 'map-label-bg', x: r1(x0), y: r1(e.ly - 8), width: r1(w), height: lines.length * 14 + 2, rx: 4 }));
        lines.forEach((l, i) => {
          const t = svgEl('text', { class: 'map-label' + (sem ? ' is-' + sem : hot ? ' is-hot' : '') + (hot && sem ? ' is-strong' : ''), x: r1(e.lx), y: r1(e.ly + 4 + i * 14), 'text-anchor': anchor });
          t.textContent = l;
          lg.appendChild(t);
        });
      }
    }
    svg.appendChild(eg);
    svg.appendChild(lg);
    const ng = svgEl('g', { class: 'map-nodes' });
    for (const n of layout.nodes) {
      const extra = o.nodeClass ? o.nodeClass(n) : '';
      const g = svgEl('g', { class: 'map-node shape-' + (n.shape || 'box') + (extra ? ' ' + extra : '') + (o.selected === n.id ? ' is-selected' : ''), transform: 'translate(' + r1(n.x) + ',' + r1(n.y) + ')' });
      g.dataset.node = n.id;
      const label = (n.lines || [n.label]).join(' ') + (n.sub && n.sub.length ? '. ' + n.sub.join(' ') : '');
      if (o.onNode) { g.setAttribute('tabindex', '0'); g.setAttribute('role', 'button'); g.setAttribute('aria-label', (o.title ? o.title(n) : label)); }
      const tt = svgEl('title');
      tt.textContent = o.title ? o.title(n) : label;
      g.appendChild(tt);
      if (n.shape === 'diamond') g.appendChild(svgEl('polygon', { class: 'map-shape', points: [[0, -n.h / 2], [n.w / 2, 0], [0, n.h / 2], [-n.w / 2, 0]].map((q) => r1(q[0]) + ',' + r1(q[1])).join(' ') }));
      else g.appendChild(svgEl('rect', { class: 'map-shape', x: r1(-n.w / 2), y: r1(-n.h / 2), width: r1(n.w), height: r1(n.h), rx: n.shape === 'pill' ? r1(n.h / 2) : n.shape === 'small' ? 7 : 9 }));
      const lines = n.lines || [n.label];
      const subN = n.sub && n.sub.length ? 1 : 0;
      const total = lines.length * 15 + subN * 14;
      let ty = -total / 2 + 11;
      for (const l of lines) {
        const t = svgEl('text', { class: 'map-text' + (n.shape === 'small' || n.small ? ' small' : ''), x: 0, y: r1(ty), 'text-anchor': 'middle' });
        t.textContent = l;
        g.appendChild(t);
        ty += 15;
      }
      if (subN) {
        const t = svgEl('text', { class: 'map-sub', x: 0, y: r1(ty + 1), 'text-anchor': 'middle' });
        t.textContent = n.sub[0];
        g.appendChild(t);
      }
      const tone = o.tone ? o.tone(n) : null;
      if (tone) g.appendChild(svgEl('circle', { class: 'map-dot tone-' + tone, cx: n.shape === 'diamond' ? r1(n.w / 4 + 4) : r1(n.w / 2 - 9), cy: n.shape === 'diamond' ? r1(-n.h / 4 - 4) : r1(-n.h / 2 + 9), r: 4 }));
      if (o.badge) { const b = o.badge(n); if (b) { const bw = textWidth(b, mfont('edge')) + 10; g.appendChild(svgEl('rect', { class: 'map-badge-bg', x: r1(n.w / 2 - bw + 4), y: r1(-n.h / 2 - 9), width: r1(bw), height: 16, rx: 8 })); const bt = svgEl('text', { class: 'map-badge', x: r1(n.w / 2 - bw / 2 + 4), y: r1(-n.h / 2 + 3), 'text-anchor': 'middle' }); bt.textContent = b; g.appendChild(bt); } }
      if (o.onNode) {
        g.addEventListener('click', () => o.onNode(n, g));
        g.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); o.onNode(n, g); } });
      }
      ng.appendChild(g);
    }
    svg.appendChild(ng);
    return svg;
  }
  /** A map in a sideways scroller (fades at the edges); scaled down to fit, but never below minScale (text stays
   *  readable: past that the map scrolls). A few pixels too wide fits anyway rather than scroll for nothing. On a phone a
   *  wider map opens on its spine (o.center: a node id) with a "scroll sideways" cue. */
  function mapBox(svg, layout, cls, minScale, o) {
    o = o || {};
    const box = h('div', { class: 'map-scroll' + (cls ? ' ' + cls : '') });
    const cue = h('div', { class: 'map-cue', hidden: true, 'aria-hidden': 'true' }, 'Scroll sideways for the whole map →');
    box.appendChild(svg);
    let centered = false;
    box._fit = () => {
      const cs = getComputedStyle(box);
      const avail = box.clientWidth - (parseFloat(cs.paddingLeft) || 0) - (parseFloat(cs.paddingRight) || 0);
      if (avail <= 0) return;
      const narrow = window.innerWidth <= 720;
      const min = narrow && o.narrowMin ? o.narrowMin : minScale || 0.82;
      const need = Math.min(1, avail / layout.width);
      const s = need >= min - 0.03 ? need : min;
      svg.setAttribute('width', String(Math.floor(layout.width * s)));
      svg.setAttribute('height', String(Math.round(layout.height * s)));
      box._scale = s;
      const over = Math.floor(layout.width * s) > avail + 1;
      cue.hidden = !(over && narrow);
      if (over && narrow && o.center && !centered) {
        const n = layout.nodes.find((x) => x.id === o.center);
        if (n) { box.scrollLeft = Math.max(0, n.x * s - avail / 2); centered = true; }
      }
      if (box._fades) box._fades();
    };
    box._cue = cue;
    requestAnimationFrame(() => { box._fit(); trackFades(box); if (box._fades) box._fades(); });
    return box;
  }
  /** Numbered steps in the owner's style: "1. **Bold lead phrase.** One or two plain sentences." */
  /** `code` spans (agent files use them for paths and commands) become <code>; everything else stays text. */
  const inlineCode = (text) => String(text || '').split(/(`[^`\n]+`)/).filter(Boolean).map((part) => (/^`[^`]+`$/.test(part) ? h('code', null, part.slice(1, -1)) : part.replace(/\*\*/g, '')));
  function procedureList(steps, cls) {
    // An item without a lead phrase is a plain sentence, not a numbered step with a missing title.
    return h('ol', { class: 'proc' + (cls ? ' ' + cls : '') }, (steps || []).map((s) => h('li', { class: s[0] ? null : 'is-plain' }, s[0] ? h('strong', null, inlineCode(s[0])) : null, s[0] && s[1] ? ' ' : null, s[1] ? inlineCode(s[1]) : null)));
  }

  // ================================================================ flows (§25): Welcome, Setup and Start as a wizard or a map
  // Drafts persist per flow in localStorage (a per-viewer convenience) until applied. Typed keys never do: they live
  // in WZ.secrets (memory) until they are saved to the server, and are dropped from memory as soon as they are.
  const WZ = { drafts: {}, secrets: new Map(), saved: new Set(), tests: new Map(), notes: new Map(), err: null, apply: null, starting: false,
    agentMcp: null, agentMcpLoading: false, scoutAsked: new Set(), adjusted: '', lastStep: '', focus: null,
    look: { busy: false, error: null, gone: false } };   // the look for model servers: only when its button is pressed
  const flowKey = (id) => 'sf.flow.' + id;
  function draftOf(id) {
    if (!WZ.drafts[id]) {
      let d = null;
      try { d = JSON.parse(lsGet(flowKey(id)) || 'null'); } catch (e) { d = null; }
      WZ.drafts[id] = F.normDraft(d);
    }
    return WZ.drafts[id];
  }
  function saveDraft(id) { lsSet(flowKey(id), JSON.stringify(draftOf(id))); }
  function secretMap() { const m = Object.assign({}, SECRETS.map || {}); for (const n of WZ.saved) m[n] = true; return m; }
  function flowCtx() {
    return { cfg: CFG.orig || {}, state: { repos: S.repos, agents: S.agents, pipeline: S.pipeline, models: S.models }, setup: SETUPST.status,
      secrets: SECRETS.map || WZ.saved.size ? secretMap() : null, typed: Array.from(WZ.secrets.keys()), chatAgents: chatAgents(),
      modelKeys: modelKeys(CFG.orig || {}).filter((k) => k.indexOf(TEMP_KEY) !== 0),
      lastRepo: lsGet('sf.lastRepo'), agentMcp: WZ.agentMcp || {}, used: S.jobs.size > 0 || S.plans.size > 0 || S.chats.size > 0 };
  }
  /** Start work with some answers already given (Welcome cards, empty states): a fresh Start draft. */
  function startFlowWith(seed) {
    const d = F.emptyDraft();
    if (seed && seed.what) { d.choices.what = seed.what; d.done.what = true; }
    WZ.drafts.start = d;
    saveDraft('start');
    location.hash = '#/start/' + (seed && seed.what ? 'repo' : 'what');
  }
  /** Make a step reachable (a Grid "Configure" or a deep link): pick, on the steps before it, the first option whose
   *  path leads there. False when no choice does. */
  function ensureOnPath(flow, d, target, ctx) {
    if (F.pathOf(flow, d, ctx).some((p) => p.id === target)) return true;
    for (const p of F.pathOf(flow, d, ctx)) {
      const step = p.step;
      if (step.kind !== 'choice' || step.multi) continue;
      const prev = d.choices[step.id];
      for (const o of F.optionsOf(step, d, ctx)) {
        d.choices[step.id] = o.id;
        if (F.pathOf(flow, d, ctx).some((q) => q.id === target)) return true;
      }
      if (prev === undefined) delete d.choices[step.id]; else d.choices[step.id] = prev;
    }
    return false;
  }
  function currentStepId(flowId, flow, d, ctx) {
    const path = F.pathOf(flow, d, ctx);
    const want = S.route.step || d.at;
    if (want && path.some((p) => p.id === want)) return want;
    const open = path.find((p) => !p.done);
    return (open || path[path.length - 1]).id;
  }
  /** What a step was answered with, in a few words (the path bar, the map's side card). */
  function answerText(step, d, ctx) {
    if (step.kind === 'choice') {
      const c = F.choiceOf(d, step.id);
      const opts = F.optionsOf(step, d, ctx);
      const ids = F.listOf(c);
      const names = ids.map((id) => { const o = opts.find((x) => x.id === id); return o ? o.title : null; }).filter(Boolean);
      return names.length > 2 ? names.slice(0, 2).join(', ') + ' +' + (names.length - 2) : names.join(', ');
    }
    if (step.kind === 'form') {
      const parts = [];
      for (const g of F.groupsOf(step, d, ctx)) {
        if (g.widget === 'routes' || g.widget === 'run-routes') { const r = F.valueOf(d, step.id, 'routes', {}); const n = isObj(r) ? Object.keys(r).length : 0; parts.push(n ? plural(n, 'step') + ' changed' : 'no change'); continue; }
        const key = (g.fields || []).find((x) => x.secret && x.type === 'password');
        if (key && step.id === 'api-keys') { parts.push((g.title || key.secret).replace(/ \(.*\)$/, '') + ': ' + (WZ.secrets.has(key.secret) ? 'key typed' : secretMap()[key.secret] || F.keySet(ctx, key.secret) ? 'key set' : 'no key')); continue; }
        const f = (g.fields || []).find((x) => !x.secret && x.type !== 'radio' && x.type !== 'checks' && (!x.showIf || x.showIf(d)));
        if (!f) continue;
        const v = String(F.fieldValue(d, step, f) || '').replace(/\s+/g, ' ').trim();
        if (v) parts.push(g.title && parts.length < 3 ? g.title + ' ' + v.slice(0, 28) : v.slice(0, 40));
      }
      return parts.slice(0, 2).join(' · ') + (parts.length > 2 ? ' …' : '');
    }
    return '';
  }
  const BADGE_CLS = { Recommended: 'tag-rec', Local: 'tag-local', API: 'tag-api', 'Needs a key': 'tag-warn', 'Needs setup': 'tag-warn', 'Local + API': 'tag-accent', CLI: 'tag-outline',
    'Key set': 'tag-ok', 'Keys set': 'tag-ok', 'Key in this setup': 'tag-ok', Current: 'tag-current', Default: 'tag-outline', Registered: 'tag-outline' };
  function badgeCls(b) {
    if (BADGE_CLS[b]) return BADGE_CLS[b];
    if (/^Needs [A-Z]/.test(b)) return 'tag-warn';
    if (/^base /.test(b)) return 'tag-outline tag-branch';
    return 'tag-outline';
  }
  function badgeEl(b) {
    const ic = b === 'Local' ? icon('server', 'icon-xs') : b === 'API' ? icon('cloud', 'icon-xs') : /^base /.test(b) ? icon('branch', 'icon-xs') : null;
    return h('span', { class: 'tag opt-badge ' + badgeCls(b) }, ic, b);
  }
  /** An option card: title, badges, "What it does" and "Choose this when". role: radio | checkbox | button. A compact
   *  card (a registered repository) shows its path and badges only. */
  function optionCard(o, role, checked) {
    const rec = (o.badges || []).indexOf(F.BADGE.rec) >= 0;
    const el = h(role === 'button' ? 'button' : 'div', { class: 'opt-card' + (checked ? ' is-selected' : '') + (rec ? ' is-rec' : '') + (o.disabled ? ' is-disabled' : '') + (o.compact ? ' is-compact' : ''),
      role: role === 'button' ? null : role, type: role === 'button' ? 'button' : null, 'aria-checked': role === 'button' ? null : checked ? 'true' : 'false',
      'aria-disabled': o.disabled ? 'true' : null, dataset: { opt: o.id } },
    h('span', { class: 'opt-head' },
      h('span', { class: 'opt-icon', 'aria-hidden': 'true' }, icon(o.icon || 'circle', 'icon-sm')),
      h('span', { class: 'opt-title' + (o.mono ? ' mono' : '') }, o.title),
      role === 'button' ? h('span', { class: 'opt-go', 'aria-hidden': 'true' }, icon('arrow-right', 'icon-sm')) : h('span', { class: 'opt-mark ' + (role === 'checkbox' ? 'is-box' : 'is-radio'), 'aria-hidden': 'true' }, icon('check', 'icon-xs'))),
    o.sub ? h('span', { class: 'opt-sub mono', title: o.sub }, o.sub) : null,
    o.badges && o.badges.length ? h('span', { class: 'opt-badges' }, o.badges.map(badgeEl)) : null,
    o.compact ? null : h('span', { class: 'opt-dl' },
      o.does ? h('span', { class: 'opt-row' }, h('span', { class: 'opt-dt' }, 'What it does'), h('span', { class: 'opt-dd' + (o.mono ? ' mono' : '') }, o.does)) : null,
      o.when ? h('span', { class: 'opt-row' }, h('span', { class: 'opt-dt' }, 'Choose this when'), h('span', { class: 'opt-dd' }, o.when)) : null));
    return el;
  }
  /** Option cards as a radio group (or checkboxes): arrows move (and select a radio), Space picks, Enter goes Next.
   *  Picking only records the answer: answers a new choice no longer needs are cleared when you continue (Next), so
   *  arrowing across the cards, or a mis-click put right, loses nothing. */
  function optionGrid(flowId, step, d, ctx, labelId) {
    const opts = F.optionsOf(step, d, ctx);
    const multi = !!step.multi;
    const chosen = F.listOf(F.choiceOf(d, step.id));
    const grid = h('div', { class: 'opt-grid n-' + Math.min(opts.length, 7), role: multi ? 'group' : 'radiogroup', 'aria-labelledby': labelId, 'aria-describedby': 'wz-hint' });
    const cards = opts.map((o) => optionCard(o, multi ? 'checkbox' : 'radio', chosen.indexOf(o.id) >= 0));
    const pick = (o) => {
      if (o.disabled) return;
      WZ.err = null;
      const prev = F.choiceOf(d, step.id);
      if (multi) {
        const cur = F.listOf(prev);
        d.choices[step.id] = cur.indexOf(o.id) >= 0 ? cur.filter((x) => x !== o.id) : cur.concat([o.id]);
      } else {
        if (prev === o.id) return;
        d.choices[step.id] = o.id;
      }
      if (!WZ.undo || WZ.undo.flow !== flowId || WZ.undo.step !== step.id) WZ.undo = { flow: flowId, step: step.id, prev: prev === undefined ? undefined : clone(prev) };
      WZ.notes.delete(flowId);
      saveDraft(flowId);
      WZ.focus = { opt: o.id };
      invalidate('flow');
    };
    const focusable = Math.max(0, cards.findIndex((c, i) => chosen.indexOf(opts[i].id) >= 0));
    let section = null;
    cards.forEach((c, i) => {
      const sec = opts[i].section || null;
      if (sec !== section) { section = sec; if (sec) grid.appendChild(h('div', { class: 'opt-section', role: 'presentation' }, sec)); }
      c.tabIndex = i === focusable ? 0 : -1;
      c.addEventListener('click', () => pick(opts[i]));
      c.addEventListener('keydown', (e) => {
        const k = e.key;
        const cols = Math.max(1, Math.round(grid.clientWidth / Math.max(1, c.offsetWidth)));
        let j = null;
        if (k === 'ArrowRight') j = i + 1; else if (k === 'ArrowLeft') j = i - 1;
        else if (k === 'ArrowDown') j = i + cols; else if (k === 'ArrowUp') j = i - cols;
        else if (k === 'Home') j = 0; else if (k === 'End') j = cards.length - 1;
        if (j !== null) {
          e.preventDefault();
          j = (j + cards.length) % cards.length;
          cards.forEach((x, n) => { x.tabIndex = n === j ? 0 : -1; });
          cards[j].focus();
          if (!multi) pick(opts[j]);
          return;
        }
        if (k === ' ' || k === 'Spacebar') { e.preventDefault(); pick(opts[i]); return; }
        if (k === 'Enter') { e.preventDefault(); if (!multi && chosen.indexOf(opts[i].id) < 0) pick(opts[i]); wizardNext(flowId); }
      });
      grid.appendChild(c);
    });
    return grid;
  }
  /** The selected card's longer text (a preset's own description), one click away instead of clipped on the card. */
  function optionAbout(step, d, ctx) {
    const c = F.choiceOf(d, step.id);
    const o = F.optionsOf(step, d, ctx).find((x) => x.id === c && x.details);
    if (!o) return null;
    return h('details', { class: 'opt-about', dataset: { about: o.id } },
      h('summary', null, icon('chevron', 'icon-xs chev'), 'About ' + o.title, o.detailsLabel ? h('span', { class: 'opt-about-src mono' }, o.detailsLabel) : null),
      h('p', null, o.details));
  }
  /** One form field from flows.js: text, number, password (a key, memory only), textarea, lines, select, radio,
   *  toggle or checks. Inputs write the draft without re-rendering; choices that show or hide fields re-render. */
  function fieldBlock(flowId, step, f, d, errs) {
    const id = 'wz-' + step.id + '-' + f.id.replace(/[^A-Za-z0-9_-]/g, '_');
    const val = F.fieldValue(d, step, f);
    const set = (v) => { F.setValue(d, step.id, f.id, v); saveDraft(flowId); if (WZ.err && WZ.err.fields && WZ.err.fields[f.id]) { delete WZ.err.fields[f.id]; const e = document.querySelector('[data-err-of="' + CSS.escape(id) + '"]'); if (e) e.hidden = true; } };
    const rerender = () => { WZ.focus = { id }; invalidate('flow'); };
    let control;
    let status = null;
    if (f.type === 'password') {
      const has = secretMap()[f.secret];
      const typed = WZ.secrets.get(f.secret) || '';
      control = h('input', { class: 'input mono-input', type: 'password', id, autocomplete: 'off', spellcheck: 'false', value: typed, placeholder: has ? 'set: leave empty to keep it' : 'paste the key', 'aria-describedby': id + '-h' });
      control.addEventListener('input', () => { const v = control.value.trim(); if (v) WZ.secrets.set(f.secret, v); else WZ.secrets.delete(f.secret); });
      status = WZ.saved.has(f.secret) ? h('span', { class: 'tag tag-ok' }, icon('check', 'icon-xs'), 'saved') : has ? h('span', { class: 'tag tag-ok' }, icon('check', 'icon-xs'), 'set') : h('span', { class: 'tag tag-warn' }, 'not set');
    } else if (f.type === 'textarea' || f.type === 'lines') {
      control = h('textarea', { class: 'textarea' + (f.mono || f.type === 'lines' ? ' mono-input' : ''), id, rows: f.rows || (f.type === 'lines' ? 3 : 5), placeholder: f.placeholder || null, spellcheck: f.type === 'lines' ? 'false' : 'true' });
      control.value = Array.isArray(val) ? val.join('\n') : String(val);
      control.addEventListener('input', () => set(control.value));
    } else if (f.type === 'select') {
      control = selectEl(f.options || [], val, { id });
      control.addEventListener('change', () => set(control.value));
    } else if (f.type === 'radio') {
      const name = id + '-r';
      control = h('div', { class: 'wz-radios', role: 'radiogroup', id, 'aria-label': f.label }, (f.options || []).map(([v, label, hint]) => {
        const inp = h('input', { type: 'radio', name, value: v, checked: String(val) === String(v) });
        inp.addEventListener('change', () => { if (inp.checked) { set(v); rerender(); } });
        return h('label', { class: 'wz-radio' + (String(val) === String(v) ? ' is-on' : '') }, inp,
          h('span', { class: 'wz-radio-text' }, h('span', { class: 'wz-radio-label' }, label), hint ? h('span', { class: 'wz-radio-hint' }, hint) : null));
      }));
    } else if (f.type === 'toggle') {
      const inp = h('input', { type: 'checkbox', id, checked: !!val });
      inp.addEventListener('change', () => { set(inp.checked); rerender(); });
      return h('div', { class: 'field wz-field wz-toggle', dataset: { field: f.id } }, h('label', { class: 'check' }, inp, f.label), f.hint ? h('div', { class: 'field-hint' }, f.hint) : null);
    } else if (f.type === 'checks') {
      const cur = F.listOf(val);
      control = h('div', { class: 'wz-checks', id, role: 'group', 'aria-label': f.label }, (f.options || []).map(([v, label]) => {
        const inp = h('input', { type: 'checkbox', value: v, checked: cur.indexOf(v) >= 0 });
        inp.addEventListener('change', () => { const now = F.listOf(F.fieldValue(d, step, f)).filter((x) => x !== v); if (inp.checked) now.push(v); set(now); });
        return h('label', { class: 'check' }, inp, h('span', { class: 'mono' }, label));
      }));
      if (!(f.options || []).length) control = h('p', { class: 'muted', id }, 'No agent can take it yet.');
    } else {
      control = h('input', { class: 'input' + (f.mono ? ' mono-input' : ''), type: f.type === 'number' ? 'number' : 'text', id, value: val === null || val === undefined ? '' : String(val),
        placeholder: f.placeholder || null, autocomplete: 'off', spellcheck: 'false', inputmode: f.type === 'number' ? 'numeric' : null });
      control.addEventListener('input', () => set(f.type === 'number' && control.value.trim() !== '' && isFinite(Number(control.value)) ? Number(control.value) : control.value));
    }
    const err = errs && errs[f.id];
    if (err && control.classList) { control.classList.add('invalid'); control.setAttribute('aria-invalid', 'true'); }
    const labelEl = f.type === 'radio' || f.type === 'checks' ? h('span', { class: 'field-label' }, f.label) : h('label', { class: 'field-label', for: id }, f.label);
    return h('div', { class: 'field wz-field' + (f.type === 'textarea' || f.type === 'lines' || f.type === 'radio' || f.type === 'checks' ? ' is-wide' : ''), dataset: { field: f.id } },
      h('span', { class: 'wz-label-row' }, labelEl, status),
      control,
      f.hint ? h('div', { class: 'field-hint', id: id + '-h' }, f.hint) : null,
      h('div', { class: 'field-error', dataset: { errOf: id }, hidden: !err }, err || ''));
  }
  /** The wizard's own Test: an existing model entry when one matches, else a temporary entry saved for the probe and
   *  removed afterwards (§25). A typed key is saved first. */
  /** Tests run one at a time (each saves and removes its own temporary entry); the other Test buttons wait. */
  let testChain = Promise.resolve();
  function runWizardTest(test, key) {
    if (WZ.tests.get(key) && WZ.tests.get(key).busy) return testChain;
    WZ.tests.set(key, { busy: true });
    invalidate('flow');
    testChain = testChain.then(() => runWizardTestNow(test, key), () => runWizardTestNow(test, key));
    return testChain;
  }
  async function runWizardTestNow(test, key) {
    const d = draftOf('setup');
    const cfg = CFG.orig || {};
    let label = key;
    try {
      if (test.keyEnv && WZ.secrets.get(test.keyEnv)) await saveSecretNow(test.keyEnv);
      let result;
      const trim = (u) => String(u || '').trim().replace(/\/+$/, '');
      if (test.kind === 'local') {
        const s = F.LOCAL_SERVERS.find((x) => x.id === test.server);
        const g = (f, dflt) => F.valueOf(d, 'local-details', s.id + '.' + f, dflt);
        const prov = cfgProviders(cfg)[s.provider];
        const url = String(g('url', prov && prov.base_url ? prov.base_url : loopbackUrl(s.port))).trim();
        const modelId = String(g('model', s.model) || '').trim();
        if (!/^https?:\/\/[^\s/]+/i.test(url)) throw new Error('Enter the server’s address first.');
        if (!modelId) throw new Error('Enter the model id first.');
        const existing = modelKeys(cfg).find((k) => { const m = cfgModels(cfg)[k]; const p = cfgProviders(cfg)[m.provider]; return m.provider === s.provider && isObj(p) && trim(p.base_url) === trim(url) && m.model === modelId; });
        label = existing || modelId;
        if (existing) result = await api('/api/models/' + enc(existing) + '/test', { method: 'POST' });
        else {
          const pe = { type: 'openai', base_url: url, api_key_env: s.keyEnv || null, timeout: 300, max_concurrency: Number(g('slots', s.slots)) || 1 };
          if (s.auth) pe.auth = s.auth;
          if (s.keyFile && isObj(prov) && prov.api_key_file) pe.api_key_file = prov.api_key_file;
          const ctx = Number(g('context', s.context));
          result = await tempTest(pe, Object.assign({ model: modelId }, ctx > 0 ? { context_tokens: ctx } : {}), s.id);
        }
      } else {
        const p = F.API_PROVIDERS.find((x) => x.provider === test.provider);
        if (!secretMap()[p.keyEnv] && !F.keySet(flowCtx(), p.keyEnv)) throw new Error(p.keyEnv + ' is not set: paste the key first.');
        const modelId = String(F.valueOf(d, 'api-keys', p.id + '.model', p.model) || '').trim();
        const existing = modelKeys(cfg).find((k) => cfgModels(cfg)[k].provider === p.provider && (!modelId || cfgModels(cfg)[k].model === modelId));
        label = existing || modelId || p.title;
        if (existing) result = await api('/api/models/' + enc(existing) + '/test', { method: 'POST' });
        else if (modelId) {
          if (!isObj(cfgProviders(cfg)[p.provider])) throw new Error('This server has no ' + p.provider + ' provider: add it in Settings › Providers.');
          result = await tempTest(null, { provider: p.provider, model: modelId }, p.id);
        } else throw new Error('Enter a model id to test.');
      }
      WZ.tests.set(key, { result: result || {}, label });
    } catch (e) {
      WZ.tests.set(key, { result: { ok: false, error: e.message }, error: e.message, label });
    }
    invalidate('flow');
  }
  /** Temporary test entries are named sf-setup-test-<what>-<random>: never shared, so two tests (or two tabs) cannot
   *  overwrite each other's entry or remove one still in use. */
  const TEMP_KEY = 'sf-setup-test';
  async function tempTest(providerEntry, modelEntry, what) {
    const name = TEMP_KEY + '-' + String(what || 'probe').replace(/[^A-Za-z0-9_-]/g, '') + '-' + Math.random().toString(36).slice(2, 7);
    const patch = { models: {} };
    patch.models[name] = Object.assign({ max_tokens: 1024, stream: true, extra_body: {} }, modelEntry);
    if (providerEntry) { patch.providers = {}; patch.providers[name] = providerEntry; patch.models[name].provider = name; }
    await api('/api/config', { method: 'POST', body: patch });
    try { return await api('/api/models/' + enc(name) + '/test', { method: 'POST' }); }
    finally {
      const undo = { models: {} };
      undo.models[name] = null;
      if (providerEntry) { undo.providers = {}; undo.providers[name] = null; }
      try { const v = await api('/api/config', { method: 'POST', body: undo }); if (!CFG.orig || !configDirty()) adoptConfig(v, ['providers', 'models']); }
      catch (e) { toast('The temporary test entry ' + name + ' could not be removed: ' + e.message + '. Remove it in Settings › Models.', { kind: 'error' }); }
    }
  }
  /** POST /api/secrets with a typed key; the value leaves memory once the server has it. */
  async function saveSecretNow(name) {
    const value = String(WZ.secrets.get(name) || '').trim();
    if (!value) return;
    const r = await api('/api/secrets', { method: 'POST', body: { name, value } });
    WZ.secrets.delete(name);
    WZ.saved.add(name);
    // The reply carries names and booleans only ({secret, secrets}); the typed value is gone from memory.
    if (r && Array.isArray(r.secrets)) { SECRETS.map = {}; for (const x of r.secrets) if (isObj(x) && x.name) SECRETS.map[String(x.name)] = x.set === true; }
    if (SECRETS.map) SECRETS.map[name] = true;
    if (CFG.orig) for (const p of Object.values(cfgProviders(CFG.orig))) if (isObj(p) && p.api_key_env === name) p.key_present = true;
  }
  function testBlock(test, key) {
    const t = WZ.tests.get(key);
    // A typed key is saved to the env file before the probe, for a local server too: the button says so.
    const savesKey = !!(test.keyEnv && WZ.secrets.get(test.keyEnv));
    const b = btn(savesKey ? 'Save key & test' : 'Test', { cls: 'btn-sm', icon: 'zap', dataset: { action: 'wz-test', test: key } });
    const waiting = !(t && t.busy) && Array.from(WZ.tests.values()).some((x) => x && x.busy);
    if (t && t.busy) setBusy(b, true);
    else if (waiting) b.disabled = true;
    b.addEventListener('click', () => runWizardTest(test, key));
    const res = t && !t.busy ? h('div', { class: 'test-result wz-test-result ' + testTone(t), 'aria-live': 'polite' }, testResultParts(t.label || key, t)) : null;
    const hint = t && t.busy ? 'Testing: a plain reply, then a tool call…' : waiting ? 'Waiting for the other test to finish.'
      : test.kind === 'local' ? (savesKey ? 'Saves ' + test.keyEnv + ' to the env file, then sends a plain reply and a tool call; the test entry is removed.' : 'A plain reply and a tool call on a temporary entry, removed afterwards.')
        : savesKey ? 'Saves ' + test.keyEnv + ' to the env file now, then sends one short request.' : 'Sends one short request with the key that is set.';
    return h('div', { class: 'wz-test', dataset: { test: key } }, b, res || h('span', { class: 'wz-test-hint' }, hint));
  }
  function routesWidget(flowId, step, g, d, ctx) {
    const cfg = clone(CFG.orig || {});
    if (flowId === 'setup') {
      for (const it of F.setupPlan(d, ctx, []).items) if (it.id === 'models') for (const r of it.requests) if (r.body && isObj(r.body.models)) cfg.models = Object.assign({}, cfg.models || {}, r.body.models);
    }
    const steps = (g.steps || []).filter((n, i, a) => a.indexOf(n) === i);
    const editor = routeEditor(cfg, steps, savedRoutes(cfg), {});
    const saved = F.valueOf(d, step.id, 'routes', {});
    for (const name of Object.keys(isObj(saved) ? saved : {})) {
      for (const k of ['model', 'driver']) {
        const sel = editor.el.querySelector('select[aria-label="' + CSS.escape(name + ' ' + k) + '"]');
        if (sel && saved[name][k]) { sel.value = saved[name][k]; sel.dispatchEvent(new Event('change')); }
      }
    }
    editor.el.addEventListener('change', () => {
      let ch = {};
      try { ch = editor.changes(); } catch (e) { ch = {}; }
      F.setValue(d, step.id, 'routes', ch);
      saveDraft(flowId);
    });
    return h('div', { class: 'rt-editor wz-routes' }, h('div', { class: 'rt-editor-head', 'aria-hidden': 'true' }, h('span', null, 'Step'), h('span', null, 'Model'), h('span', null, 'Driver'), h('span', null, 'Runs')), editor.el);
  }
  function scoutWidget(repo) {
    const sc = CFG.scout.get(repo) || {};
    if (!CFG.scout.has(repo) && !WZ.scoutAsked.has(repo)) { WZ.scoutAsked.add(repo); checkScout(repo, false); }
    const run = btn(sc.running ? 'Scouting…' : sc.draft ? 'Scout again' : 'Run Scout', { cls: 'btn-sm', icon: 'scout', disabled: !!sc.running, dataset: { action: 'wz-scout' } });
    if (sc.running) run.prepend(h('span', { class: 'spinner', 'aria-hidden': 'true' }));
    run.addEventListener('click', () => startScout(repo));
    return h('div', { class: 'wz-scout' },
      h('div', { class: 'wz-scout-text' }, h('strong', null, 'AGENTS.md draft'), h('span', { class: 'muted' }, sc.running ? ' Scout is reading ' + repo + '…' : sc.draft ? ' A draft is ready for ' + repo + '.' : ' Scout reads ' + repo + ' and drafts how to set it up, run it and test it.')),
      h('span', { class: 'spacer' }),
      sc.draft ? btn('View draft', { cls: 'btn-sm btn-ghost', icon: 'eye', dataset: { action: 'wz-scout-view' }, onClick: () => showScoutDraft(repo) }) : null, run);
  }
  function browserWidget() {
    const c = S.doctor ? S.doctor.checks.find((x) => /agent-browser/i.test(String(x.name) + ' ' + String(x.detail || ''))) : null;
    const on = c ? !!c.ok : null;
    if (!S.doctor && !S.doctorLoading && !S.doctorError) loadDoctor(false);
    // Doctor reports agent-browser when it checks for it; with no such check, no status is shown rather than a guess.
    const chip = on === true ? h('span', { class: 'tag tag-ok' }, icon('check', 'icon-xs'), 'Installed')
      : on === false ? h('span', { class: 'tag tag-warn' }, 'Not installed') : !S.doctor && !S.doctorError ? h('span', { class: 'tag tag-outline' }, 'Checking…') : null;
    return h('div', { class: 'wz-browser' }, chip,
      h('p', null, 'The builder and the UX and UI reviewers open pages in a real browser when the ', h('code', null, 'agent-browser'), ' CLI is on the server’s PATH, and read them as text with curl otherwise. ',
        on === true ? 'Nothing to set here.' : ['Install it on the server with ', h('code', null, 'npm install -g agent-browser'), '; nothing to set here.']));
  }
  /** Drop a refusal ("You looked just now…") once its Retry-After has passed: it is no longer true then. */
  function lookRefusalOver() {
    const L = WZ.look;
    if (L.until && Date.now() >= L.until) { L.error = null; L.until = 0; }
  }
  /** POST /api/setup/look-around, once per press (the server refuses a second one too soon: 429). The answer goes
   *  into the setup status's host as GET /api/setup would give it, so the address hints offer what it found. A
   *  refusal shows until its Retry-After has passed (one redraw then; it does not look again). */
  async function lookAround() {
    const L = WZ.look;
    if (L.busy) return;
    const focused = !!(document.activeElement && document.activeElement.dataset && document.activeElement.dataset.action === 'wz-look-around');
    L.busy = true;
    L.error = null;
    L.until = 0;
    invalidate('flow');
    try {
      const r = await api('/api/setup/look-around', { method: 'POST', body: {} });
      SETUPST.look = isObj(r) ? r : { model_servers: [] };
      keepLook();
      L.note = isObj(r) && r.note ? String(r.note) : null;
    } catch (e) {
      if (gone(e)) L.gone = true;
      else if (e.status !== 401) L.error = e.message;
      if (e.status === 429 && e.retryAfter) {
        const until = L.until = Date.now() + e.retryAfter * 1000;
        setTimeout(() => { if (L.until === until) { lookRefusalOver(); invalidate('flow'); } }, e.retryAfter * 1000 + 50);
      }
    } finally {
      L.busy = false;
      if (focused && !WZ.focus) WZ.focus = { action: 'wz-look-around' };   // the busy button could not keep focus
      invalidate('flow');
    }
  }
  /** The look-around on the server-details step: a sentence on what pressing it does, the button (busy while it
   *  looks), and the last look's answer (GET /api/setup keeps it, with when it was taken). Nothing runs by itself. */
  function lookAroundWidget() {
    const L = WZ.look;
    lookRefusalOver();
    const host = SETUPST.status && isObj(SETUPST.status.host) ? SETUPST.status.host : null;
    const last = host && host.model_servers_at ? { model_servers: host.model_servers, error: host.model_servers_error, at: host.model_servers_at } : SETUPST.look;
    // A look that broke found nothing: it gets its error and a next step, not "nothing answered" or what was sent.
    const broke = last && last.error ? String(last.error) : null;
    const found = last && !broke && Array.isArray(last.model_servers) ? last.model_servers.filter((m) => isObj(m) && m.url) : null;
    const b = btn(L.busy ? 'Looking…' : 'Look for model servers', { cls: 'btn-sm', icon: 'search', dataset: { action: 'wz-look-around' }, disabled: L.gone });
    if (L.busy) setBusy(b, true);
    b.addEventListener('click', lookAround);
    const what = (m) => (Array.isArray(m.ids) && m.ids.length ? F.listsText(m.ids) : m.status === 200 ? 'lists no models' : 'wants a key');
    const answer = [];
    if (L.gone) answer.push(h('p', { class: 'wz-look-empty' }, 'This server cannot look for model servers. Type each server’s address below.'));
    if (L.error) answer.push(h('div', { class: 'test-result warn', role: 'alert' }, icon('alert', 'icon-xs'), h('span', null, L.error)));
    if (broke) {
      answer.push(h('div', { class: 'test-result warn wz-look-broke', role: 'alert' }, icon('alert', 'icon-xs'),
        h('span', null, 'The look did not work: ' + broke + '. Look again in a moment, or type each server’s address below.')));
    } else if (found && found.length) {
      answer.push(h('ul', { class: 'wz-look-list', 'aria-label': 'Model servers found' }, found.map((m) => h('li', { dataset: { url: String(m.url) } },
        h('code', null, F.idText(m.url)), h('span', { class: 'muted' }, ' ' + what(m))))));
    } else if (found) {
      answer.push(h('p', { class: 'wz-look-empty' }, 'No program listening on this machine answered like a model server. Start your model server and wait until it has loaded its model, then look again. If it runs on another machine, type its address below.'));
    }
    return h('div', { class: 'wz-look', 'aria-busy': L.busy ? 'true' : 'false' },
      h('p', { class: 'wz-look-text' }, 'Asks each program listening on this machine whether it is a model server; it sends no key and changes nothing.'),
      h('div', { class: 'wz-look-row' }, b,
        L.busy ? h('span', { class: 'wz-test-hint' }, 'Asking each program listening here…')
          : last && last.at ? h('span', { class: 'wz-test-hint' }, 'Last look: ', timeEl(last.at)) : null),
      h('div', { class: 'wz-look-answer', 'aria-live': 'polite' }, L.busy ? null : answer,
        !L.busy && found && L.note ? h('p', { class: 'wz-look-note muted' }, L.note) : null));
  }
  function loadAgentMcp() {
    if (WZ.agentMcp || WZ.agentMcpLoading || !F) return;
    WZ.agentMcpLoading = true;
    const names = F.MCP_AGENTS.filter((n) => agentDef(n));
    Promise.all(names.map((n) => api('/api/agents/' + enc(n)).then((r) => [n, listify(r && r.mcp)]).catch(() => [n, []])))
      .then((pairs) => { WZ.agentMcp = Object.fromEntries(pairs); })
      .finally(() => { WZ.agentMcpLoading = false; invalidate('flow'); });
  }
  function groupCard(flowId, step, g, d, ctx, errs) {
    const fields = (g.fields || []).filter((f) => !f.showIf || f.showIf(d));
    if (!fields.length && !g.widget && !g.test && !g.title) return null;
    let widget = null;
    if (g.widget === 'routes' || g.widget === 'run-routes') widget = routesWidget(flowId, step, g, d, ctx);
    else if (g.widget === 'scout') widget = scoutWidget(g.repo);
    else if (g.widget === 'agent-browser') widget = browserWidget();
    else if (g.widget === 'look-around') widget = lookAroundWidget();
    const key = g.test ? g.test.kind + ':' + (g.test.server || g.test.provider) : null;
    return h('section', { class: 'wz-group' + (g.title ? '' : ' is-plain'), dataset: { group: g.id } },
      g.title ? h('div', { class: 'wz-group-head' }, h('h3', { class: 'wz-group-title' }, g.title),
        g.status ? h('span', { class: 'tag ' + (g.status === 'ok' ? 'tag-ok' : 'tag-warn') }, g.status === 'ok' ? 'set' : 'not set') : null,
        g.sub ? h('span', { class: 'wz-group-sub' }, inlineCode(g.sub)) : null) : null,
      g.tip ? h('p', { class: 'wz-tip' }, icon('info', 'icon-xs'), h('span', null, g.tip)) : null,
      fields.length ? h('div', { class: 'wz-fields' }, fields.map((f) => fieldBlock(flowId, step, f, d, errs))) : null,
      g.about && g.about.text ? h('details', { class: 'opt-about' }, h('summary', null, icon('chevron', 'icon-xs chev'), 'About this example', h('span', { class: 'opt-about-src mono' }, g.about.label)), h('p', null, g.about.text)) : null,
      widget,
      g.test ? testBlock(g.test, key) : null);
  }
  /** Step validation: flows.js rules plus the keys only this page holds. */
  function validateWizardStep(flowId, step, d, ctx) {
    const v = F.validateStep(F.flowOf(flowId), step.id, d, ctx);
    for (const f of F.visibleFields(step, d, ctx)) {
      if (!f.secret || !f.required) continue;
      if (!WZ.secrets.get(f.secret) && !secretMap()[f.secret] && !F.keySet(ctx, f.secret)) { v.ok = false; v.fields[f.id] = 'Paste the key, or go back and drop this provider.'; v.message = v.message || 'A key is missing.'; }
    }
    if (step.kind === 'choice' && !step.multi && F.choiceOf(d, step.id) === undefined && !F.optionsOf(step, d, ctx).length && step.empty) v.empty = step.empty;
    return v;
  }
  /** The draft as Apply and Start read it: every unanswered single choice on the path takes its default. */
  function projectedDraft(flow, d, ctx) {
    const p = F.normDraft(clone({ choices: d.choices, values: d.values, done: d.done, at: d.at }));
    for (let guard = 0; guard < 40; guard++) {
      const open = F.pathOf(flow, p, ctx).find((x) => x.step.kind === 'choice' && !x.step.multi && F.choiceOf(p, x.id) === undefined
        && F.defaultChoice(x.step, p, ctx) !== undefined && F.optionsOf(x.step, p, ctx).some((o) => o.id === F.defaultChoice(x.step, p, ctx)));
      if (!open) break;
      p.choices[open.id] = F.defaultChoice(open.step, p, ctx);
    }
    return p;
  }
  /** The first step on the path that does not validate. Apply and Start check the whole path, however the review was
   *  reached (Next, the path, the mini map or a link): a step left invalid by a later edit is never sent. Start needs
   *  every step; Setup checks the steps you answered or visited (one it never reached changes nothing). */
  function firstInvalid(flowId, d, ctx) {
    const flow = F.flowOf(flowId);
    const p = projectedDraft(flow, d, ctx);
    const touched = (id) => !!(d.done && d.done[id]) || (d.choices && d.choices[id] !== undefined) || (d.values && d.values[id] !== undefined);
    for (const x of F.pathOf(flow, p, ctx)) {
      if (x.step.kind === 'review' || x.step.kind === 'run') continue;
      if (flowId === 'setup' && !touched(x.id)) continue;
      const v = validateWizardStep(flowId, x.step, p, ctx);
      if (!v.ok) return { step: x.step, v };
    }
    return null;
  }
  function blockAt(flowId, bad) {
    WZ.err = { step: bad.step.id, message: (flowId === 'setup' ? 'Apply needs this step first. ' : 'Start needs this step first. ') + (bad.v.message || ''), fields: bad.v.fields || {} };
    WZ.focus = { invalid: true };
    const want = '#/' + flowId + '/' + bad.step.id;
    if (location.hash !== want) location.hash = want; else invalidate('flow');
  }
  function wizardNext(flowId) {
    const flow = F.flowOf(flowId);
    const d = draftOf(flowId);
    const ctx = flowCtx();
    const stepId = currentStepId(flowId, flow, d, ctx);
    const step = F.stepOf(flow, stepId);
    if (step.kind === 'review') { applySetup(); return; }
    if (step.kind === 'run') { runStart(); return; }
    const v = validateWizardStep(flowId, step, d, ctx);
    if (!v.ok) {
      WZ.err = { step: stepId, message: v.message, fields: v.fields };
      WZ.focus = { invalid: true };
      invalidate('flow');
      return;
    }
    WZ.err = null;
    d.done[stepId] = true;
    const next = F.nextOf(flow, stepId, d, ctx);
    // Answers the new path no longer needs are cleared now, on the way forward (never while you look at the cards).
    const dropped = F.pruneDraft(flow, d, ctx);
    if (dropped.length) WZ.notes.set(flowId, { text: dropped });
    else WZ.notes.delete(flowId);
    WZ.undo = null;
    saveDraft(flowId);
    if (next) location.hash = '#/' + flowId + '/' + next;
  }
  function wizardBack(flowId) {
    const flow = F.flowOf(flowId);
    const d = draftOf(flowId);
    const ctx = flowCtx();
    const path = F.pathOf(flow, d, ctx);
    const i = path.findIndex((p) => p.id === currentStepId(flowId, flow, d, ctx));
    WZ.err = null;
    if (i > 0) location.hash = '#/' + flowId + '/' + path[i - 1].id;
    else location.hash = '#/welcome';
  }
  async function startOver(flowId) {
    const ok = await confirmModal({ title: 'Start over?', description: 'Your choices on this page are cleared' + (flowId === 'setup' ? ', and keys you typed but did not save are forgotten' : '') + '. Nothing on the server changes.', confirm: 'Start over', icon: 'rework' });
    if (!ok) return;
    WZ.drafts[flowId] = F.emptyDraft();
    lsSet(flowKey(flowId), null);
    if (flowId === 'setup') { WZ.secrets.clear(); WZ.tests.clear(); WZ.apply = null; }
    WZ.notes.delete(flowId);
    WZ.undo = null;
    WZ.err = null;
    location.hash = '#/' + flowId;
    invalidate('flow');
  }
  function modeToggle(flowId, mode) {
    return h('div', { class: 'seg', role: 'group', 'aria-label': 'View' },
      h('a', { class: 'seg-btn', href: '#/' + flowId, 'aria-current': mode === 'wizard' ? 'true' : 'false', dataset: { mode: 'wizard' } }, icon('list', 'icon-sm'), 'Step by step'),
      h('a', { class: 'seg-btn', href: '#/' + flowId + '/map', 'aria-current': mode === 'map' ? 'true' : 'false', dataset: { mode: 'map' } }, icon('route', 'icon-sm'), 'Map'));
  }
  function flowHead(flowId, flow, mode) {
    const actions = [modeToggle(flowId, mode)];
    if (flowId !== 'welcome') actions.push(btn('Start over', { cls: 'btn-sm btn-ghost', icon: 'rework', dataset: { action: 'start-over' }, onClick: () => startOver(flowId) }));
    return h('div', { class: 'wz-head' },
      h('div', { class: 'wz-head-text' }, h('div', { class: 'wz-eyebrow' }, flowId === 'setup' ? 'Guided setup' : flowId === 'start' ? 'Start work' : PRODUCT),
        h('h1', { class: 'wz-h1' }, flowId === 'welcome' ? 'What would you like to do?' : flow.title), flow.sub && flowId !== 'welcome' ? h('p', { class: 'wz-sub' }, flow.sub) : null),
      h('div', { class: 'wz-head-actions' }, actions));
  }
  /** A fresh install: setup not complete and nothing ever run here. Unready steps then read "to do", not alarms. */
  function freshInstall() {
    const st = SETUPST.status;
    return !!st && st.configured === false && !(S.jobs.size || S.plans.size || S.chats.size);
  }
  /** Where the wizard fixes a setup step (GET /api/setup's keys). */
  const SETUP_FIX = { providers: 'api-providers', models: 'where', routing: 'roles', repos: 'repo', tools: 'tools', output: 'output' };
  /** Open the wizard on the step that fixes a setup problem. A missing key opens the Keys step with that provider. */
  function fixSetup(key) {
    const st = SETUPST.status && isObj(SETUPST.status.steps) ? SETUPST.status.steps[key] : null;
    if (key === 'providers' && st) {
      const names = String(st.detail || '').match(/\b[A-Z][A-Z0-9_]*_API_KEY\b/g) || [];
      const provs = F.API_PROVIDERS.filter((p) => names.indexOf(p.keyEnv) >= 0).map((p) => p.id);
      if (provs.length) {
        const d = F.emptyDraft();
        d.choices.where = 'api';
        d.choices['api-providers'] = provs;
        d.done.where = true;
        d.done['api-providers'] = true;
        WZ.drafts.setup = d;
        WZ.apply = null;
        saveDraft('setup');
        location.hash = '#/setup/api-keys';
        return;
      }
    }
    location.hash = '#/setup/' + (SETUP_FIX[key] || '');
  }
  /** The setup status from GET /api/setup as small chips (Welcome, the review's success panel). */
  function setupChips() {
    const st = SETUPST.status;
    if (!st || !isObj(st.steps)) return null;
    const keys = Object.keys(st.steps);
    const ok = keys.filter((k) => isObj(st.steps[k]) && st.steps[k].ok).length;
    const names = { providers: 'Providers', models: 'Models', routing: 'Routing', repos: 'Repos', tools: 'Tools', output: 'Output' };
    const fresh = freshInstall();
    return h('div', { class: 'setup-chips', role: 'list', 'aria-label': 'Setup status' },
      h('span', { class: 'setup-chips-sum' }, st.configured ? icon('check-circle', 'icon-sm') : icon(fresh ? 'circle' : 'alert', 'icon-sm'),
        (st.configured ? 'Set up' : fresh ? 'Not set up yet' : 'Setup needs attention') + ' · ' + ok + ' of ' + keys.length + ' ready'),
      keys.map((k) => {
        const s = isObj(st.steps[k]) ? st.steps[k] : {};
        return h('a', { class: 'setup-chip ' + (s.ok ? 'is-ok' : fresh ? 'is-todo' : 'is-warn'), role: 'listitem', href: '#/grid/' + enc(k === 'repos' ? 'repos' : k), title: String(s.detail || '') },
          icon(s.ok ? 'check' : fresh ? 'circle' : 'alert', 'icon-xs'), names[k] || k);
      }));
  }
  function renderWelcome(root) {
    const flow = F.flowOf('welcome');
    const step = flow.steps[0];
    const ctx = flowCtx();
    const st = SETUPST.status;
    const fresh = !!st && st.configured === false && !ctx.used;
    root.appendChild(flowHead('welcome', flow, 'wizard'));
    const intro = h('p', { class: 'wz-lead' }, 'Welcome to ' + PRODUCT + ': AI agents do the expert work on your own server, and every change has a way back. Its Software Factory writes the spec, builds, reviews and approves each change to your repositories, and you decide what lands.');
    root.appendChild(intro);
    const chips = setupChips();
    if (chips) root.appendChild(chips);
    if (fresh) {
      const go = h('a', { class: 'btn btn-primary', href: '#/setup', dataset: { action: 'first-step' } }, icon('settings', 'icon-sm'), 'Set up the harness');
      root.appendChild(h('div', { class: 'first-step', role: 'note' },
        h('span', { class: 'first-step-n', 'aria-hidden': 'true' }, '1'),
        h('div', { class: 'first-step-text' }, h('strong', null, 'First, set up the harness.'), ' Pick where your models run, paste any keys, add a repository and choose where finished work goes. Nothing is saved until you apply, except a key you test.'),
        go));
    } else if (st && st.configured === false && isObj(st.steps)) {
      // In use, but a required step is not ready (a key went missing, a route broke): say which, and open the step that fixes it.
      const bad = Object.keys(st.steps).filter((k) => isObj(st.steps[k]) && !st.steps[k].ok);
      const first = bad[0];
      if (first) {
        const names = { providers: 'Providers', models: 'Models', routing: 'Routing', repos: 'Repositories', tools: 'Tools', output: 'Output' };
        const fix = h('a', { href: '#/setup/' + (SETUP_FIX[first] || ''), dataset: { action: 'attention' } }, first === 'providers' ? 'Add the key' : 'Fix it in setup');
        fix.addEventListener('click', (e) => { e.preventDefault(); fixSetup(first); });
        const box = callout('warn', 'alert', 'Setup needs attention:', [bad.map((k) => names[k] || k).join(', ') + '. ' + String(st.steps[first].detail || '') + ' ', fix]);
        box.classList.add('wz-attn');
        root.appendChild(box);
      }
    }
    root.appendChild(h('section', { class: 'wz-proc-card' }, h('h2', { class: 'wz-proc-title', id: 'wz-title', tabindex: '-1' }, 'How it works'), procedureList(step.procedure)));
    // On a fresh install the first-step banner is the way into setup; its card would say the same thing twice.
    const opts = F.optionsOf(step, F.emptyDraft(), ctx).filter((o) => !(fresh && o.id === 'setup'));
    const list = h('div', { class: 'opt-grid opt-launch', role: 'list', 'aria-label': 'What would you like to do?' });
    for (const o of opts) {
      const card = optionCard(o, 'button', false);
      card.addEventListener('click', () => { if (o.start && o.go.indexOf('#/start') === 0) startFlowWith(o.start); else location.hash = o.go; });
      list.appendChild(h('div', { role: 'listitem', class: 'opt-cell' }, card));
    }
    root.appendChild(list);
    root.appendChild(h('p', { class: 'wz-foot-links' }, 'Every section and what needs you: ', h('a', { href: '#/home' }, 'Home'), ' · The Software Factory on one map: ', h('a', { href: '#/grid' }, 'Grid Card'), ' · Every agent’s working procedure: ', h('a', { href: '#/procedures' }, 'Procedures')));
  }
  /** The steps of your path beside the step (wide screens): each answered step shows its answer and goes back to it. */
  function miniMap(flowId, flow, path, stepId, ctx) {
    const groups = flow.groups || {};
    const d = draftOf(flowId);
    const list = h('ol', { class: 'mini-map', 'aria-label': 'Where you are' });
    let lastGroup = null;
    path.forEach((p, i) => {
      if (p.step.group && p.step.group !== lastGroup && groups[p.step.group]) { list.appendChild(h('li', { class: 'mm-group', 'aria-hidden': 'true' }, groups[p.step.group])); lastGroup = p.step.group; }
      const state = p.id === stepId ? 'current' : p.done ? 'done' : 'todo';
      const reach = p.done || p.id === stepId || (i > 0 && path[i - 1].done);
      const mark = h('span', { class: 'mm-dot' + (F.branching(flow, p.id, ctx) ? ' is-branch' : '') }, state === 'done' ? icon('check', 'icon-xs') : null);
      const ans = p.done && p.id !== stepId ? answerText(p.step, d, ctx) : '';
      const label = h('span', { class: 'mm-label' }, h('span', { class: 'mm-name' }, p.step.short || F.titleOf(p.step, d)), ans ? h('span', { class: 'mm-answer', title: ans }, ans) : null);
      const el = reach ? h('a', { class: 'mm-step is-' + state, href: '#/' + flowId + '/' + p.id, 'aria-current': state === 'current' ? 'step' : null, title: p.done && p.id !== stepId ? 'Back to: ' + F.titleOf(p.step, d) : null }, mark, label)
        : h('span', { class: 'mm-step is-' + state }, mark, label);
      list.appendChild(h('li', null, el));
    });
    return h('nav', { class: 'wz-side', 'aria-label': 'Steps' }, h('div', { class: 'wz-side-head' }, h('span', null, 'Your path'), h('a', { href: '#/' + flowId + '/map', class: 'wz-side-link' }, 'Map')), list);
  }
  /** Your answers so far on narrow screens (the side path is hidden there): one line that opens to the full list. */
  function pathBar(flowId, path, stepId, d, ctx) {
    const done = path.filter((p) => p.done && p.id !== stepId);
    if (!done.length) return null;
    const det = h('details', { class: 'wz-path', open: !!WZ.pathOpen },
      h('summary', null, icon('chevron', 'icon-xs chev'), h('span', { class: 'wz-path-label' }, 'So far'), h('span', { class: 'wz-path-count' }, plural(done.length, 'answer')),
        h('span', { class: 'wz-path-last' }, (done[done.length - 1].step.short || F.titleOf(done[done.length - 1].step, d)) + ': ' + (answerText(done[done.length - 1].step, d, ctx) || '✓'))),
      h('nav', { class: 'wz-path-chips', 'aria-label': 'Your choices so far' },
        done.map((p) => h('a', { class: 'wz-chip', href: '#/' + flowId + '/' + p.id, title: 'Back to: ' + F.titleOf(p.step, d) },
          h('span', { class: 'wz-chip-k' }, p.step.short || F.titleOf(p.step, d)), h('span', { class: 'wz-chip-v' }, answerText(p.step, d, ctx) || '✓')))));
    det.addEventListener('toggle', () => { WZ.pathOpen = det.open; });
    return det;
  }
  function renderWizard(root, flowId) {
    const flow = F.flowOf(flowId);
    const d = draftOf(flowId);
    const ctx = flowCtx();
    const routeKey = flowId + '/' + (S.route.step || '');
    if (S.route.step && WZ.adjusted !== routeKey) {
      WZ.adjusted = routeKey;
      if (F.stepOf(flow, S.route.step) && ensureOnPath(flow, d, S.route.step, ctx)) saveDraft(flowId);
    }
    const stepId = currentStepId(flowId, flow, d, ctx);
    const step = F.stepOf(flow, stepId);
    if (d.at !== stepId) { d.at = stepId; saveDraft(flowId); }
    // An error belongs to its step: another step drops it (coming back shows the step clean).
    if (WZ.err && WZ.err.step !== stepId) WZ.err = null;
    // A finished Apply is shown until you leave the review; after that a new review starts from your new answers.
    if (flowId === 'setup' && stepId !== 'review' && WZ.apply && !WZ.apply.running && WZ.apply.done && !WZ.apply.failed) WZ.apply = null;
    if (step.kind === 'choice' && !step.multi && F.choiceOf(d, stepId) === undefined) {
      const def = F.defaultChoice(step, d, ctx);
      if (def !== undefined && F.optionsOf(step, d, ctx).some((o) => o.id === def)) { d.choices[stepId] = def; saveDraft(flowId); }
    }
    if (flowId === 'setup' && (stepId === 'tools' || stepId === 'review')) loadAgentMcp();
    const path = F.pathOf(flow, d, ctx);
    const idx = path.findIndex((p) => p.id === stepId);
    root.appendChild(flowHead(flowId, flow, 'wizard'));
    const bar = pathBar(flowId, path, stepId, d, ctx);
    if (bar) root.appendChild(bar);
    const main = h('div', { class: 'wz-main', dataset: { step: stepId } });
    const groupName = (flow.groups || {})[step.group] || '';
    main.appendChild(h('div', { class: 'wz-kicker' }, h('span', { class: 'wz-num' }, 'Step ' + (idx + 1) + ' of ' + path.length), groupName ? h('span', { class: 'wz-group-name' }, groupName) : null));
    main.appendChild(h('h2', { class: 'wz-title', id: 'wz-title', tabindex: '-1' }, F.titleOf(step, d)));
    main.appendChild(procedureList(step.procedure, 'wz-proc'));
    // What a changed answer clears when you continue (with Undo), or what the last Next cleared.
    const pending = F.pruneDraft(flow, F.normDraft(clone({ choices: d.choices, values: d.values, done: d.done })), ctx);
    const note = WZ.notes.get(flowId);
    if (pending.length) {
      const u = WZ.undo && WZ.undo.flow === flowId ? WZ.undo : null;
      const undo = u ? btn('Undo', { cls: 'btn-xs', icon: 'rework', dataset: { action: 'wz-undo' }, onClick: () => {
        if (u.prev === undefined) delete d.choices[u.step]; else d.choices[u.step] = u.prev;
        WZ.undo = null;
        saveDraft(flowId);
        WZ.focus = { opt: Array.isArray(u.prev) ? null : u.prev };
        invalidate('flow');
      } }) : null;
      main.appendChild(h('div', { class: 'wz-note', role: 'status' }, icon('info', 'icon-sm'),
        h('span', null, h('strong', null, 'No longer applies: '), pending.join(', ') + '. These answers are cleared when you continue.'), undo));
    } else if (note && note.text.length) {
      const x = h('button', { type: 'button', class: 'btn btn-ghost btn-icon btn-xs', 'aria-label': 'Dismiss' }, icon('x', 'icon-xs'));
      x.addEventListener('click', () => { WZ.notes.delete(flowId); invalidate('flow'); });
      main.appendChild(h('div', { class: 'wz-note', role: 'status' }, icon('info', 'icon-sm'), h('span', null, h('strong', null, 'No longer applies, so it was cleared: '), note.text.join(', ') + '.'), x));
    }
    const errs = WZ.err && WZ.err.step === stepId ? WZ.err.fields || {} : {};
    const body = h('div', { class: 'wz-body' });
    if (step.kind === 'choice') {
      const opts = F.optionsOf(step, d, ctx);
      if (!opts.length && step.empty) body.appendChild(emptyState('folder', step.empty.title, step.empty.text, [h('a', { class: 'btn btn-primary', href: step.empty.href }, icon('plus', 'icon-sm'), step.empty.action)], true));
      else if (step.solo && opts.length === 1) {
        // One possible answer (no repository yet: add one): the form alone, no lone card to pick.
        if (F.choiceOf(d, stepId) !== opts[0].id) { d.choices[stepId] = opts[0].id; saveDraft(flowId); }
      } else {
        body.appendChild(h('p', { class: 'sr-only', id: 'wz-hint' }, step.multi ? 'Pick one or more. Space toggles a card, Enter continues.' : 'Arrow keys move between the cards, Enter continues.'));
        body.appendChild(optionGrid(flowId, step, d, ctx, 'wz-title'));
        if (step.add) {
          const add = h('a', { class: 'opt-add', href: step.add.href, dataset: { action: 'wz-add' } }, icon('plus', 'icon-sm'), h('span', null, h('strong', null, step.add.title), ' ', h('span', { class: 'muted' }, step.add.text)));
          body.appendChild(add);
        }
        // The saved setting, when it is not the recommended one, said in words (badges alone are easy to miss).
        const cur = F.currentOf(step, d, ctx);
        const rec = opts.find((o) => (o.badges || []).indexOf(F.BADGE.rec) >= 0);
        const curOpt = cur ? opts.find((o) => o.id === cur) : null;
        if (rec && curOpt && rec.id !== curOpt.id && F.choiceOf(d, stepId) === curOpt.id) body.appendChild(h('p', { class: 'wz-tip wz-current' }, icon('info', 'icon-xs'),
          h('span', null, (step.currentText || 'Your current setting is ' + curOpt.title) + '; ' + rec.title + ' is recommended' + (step.id === 'roles' ? ' for these choices.' : '.'))));
        const about = optionAbout(step, d, ctx);
        if (about) body.appendChild(about);
      }
      const extraNote = typeof step.note === 'function' ? step.note(d, ctx) : null;
      if (extraNote) body.appendChild(h('p', { class: 'wz-tip' }, icon('info', 'icon-xs'), h('span', null, extraNote)));
      for (const g of F.groupsOf(step, d, ctx)) { const c = groupCard(flowId, step, g, d, ctx, errs); if (c) body.appendChild(c); }
    } else if (step.kind === 'form') {
      const groups = F.groupsOf(step, d, ctx);
      if (!groups.length) body.appendChild(callout('info', 'info', 'Nothing to fill in here.', 'Go back and pick at least one card, or continue.'));
      for (const g of groups) { const c = groupCard(flowId, step, g, d, ctx, errs); if (c) body.appendChild(c); }
    } else if (step.kind === 'review') body.appendChild(setupReview(d, ctx));
    else if (step.kind === 'run') body.appendChild(startReview(d, ctx));
    main.appendChild(body);
    const errBox = h('div', { class: 'form-error wz-error', role: 'alert', hidden: !(WZ.err && WZ.err.step === stepId && WZ.err.message) }, icon('alert', 'icon-sm'), h('span', null, WZ.err && WZ.err.step === stepId ? WZ.err.message || '' : ''));
    main.appendChild(errBox);
    const back = btn(idx > 0 ? 'Back' : 'Welcome', { cls: '', icon: 'chevron-left', dataset: { action: 'wz-back' }, onClick: () => wizardBack(flowId) });
    let nextLabel = 'Next';
    let nextIcon = 'arrow-right';
    const ap = flowId === 'setup' ? currentApply(d) : null;
    if (step.kind === 'review') { nextLabel = ap && ap.done && !ap.failed ? 'Applied' : ap && ap.failed ? 'Retry failed items' : 'Apply'; nextIcon = 'check'; }
    if (step.kind === 'run') { nextLabel = 'Start'; nextIcon = 'play'; }
    const next = btn(nextLabel, { cls: 'btn-primary', icon: nextIcon, dataset: { action: 'wz-next' }, onClick: () => wizardNext(flowId) });
    if (step.kind === 'review' && ap && (ap.running || (ap.done && !ap.failed))) next.disabled = true;
    if (step.kind === 'review' && ap && ap.running) setBusy(next, true);
    if (step.kind === 'run' && WZ.starting) setBusy(next, true);
    if (step.kind === 'review' && !ap && !F.setupPlan(d, ctx, typedNames()).items.length) next.disabled = true;
    main.appendChild(h('div', { class: 'wz-foot' }, back, h('span', { class: 'spacer' }), h('span', { class: 'wz-foot-hint' }, h('kbd', null, 'Enter'), step.kind === 'review' || step.kind === 'run' ? '' : ' next'), next));
    root.appendChild(h('div', { class: 'wz-layout' }, main, miniMap(flowId, flow, path, stepId, ctx)));
    const sig = flowId + '/' + stepId;
    if (WZ.lastStep !== sig) { WZ.lastStep = sig; WZ.focus = WZ.focus || { title: true }; document.getElementById('main').scrollTop = 0; }
  }
  // ---------------------------------------------------------------- setup review → apply
  function changeRows(changes) {
    return h('div', { class: 'chg-list', role: 'table', 'aria-label': 'Changes' }, changes.map((c) => c.note
      ? h('div', { class: 'chg-row is-note' + (c.warn ? ' is-warn' : ''), role: 'row' }, h('span', { class: 'chg-k', role: 'cell' }, c.warn ? icon('alert', 'icon-xs') : null, c.label), h('span', { class: 'chg-note', role: 'cell' }, c.after))
      : h('div', { class: 'chg-row', role: 'row' },
        h('span', { class: 'chg-k mono', role: 'cell', title: c.path }, c.label || c.path),
        h('span', { class: 'chg-before', role: 'cell' }, F.fmtValue(c.before)),
        h('span', { class: 'chg-arrow', 'aria-hidden': 'true' }, '→'),
        h('span', { class: 'chg-after', role: 'cell' }, h('span', { class: 'sr-only' }, 'becomes '), F.fmtValue(c.after)))));
  }
  const APPLY_UI = { pending: ['to apply', 'tag-outline'], running: ['applying…', 'tag-accent'], ok: ['done', 'tag-ok'], failed: ['failed', 'tag-err'], skipped: ['skipped', 'tag-warn'] };
  /** Names (never values) of keys typed on this page or saved from it. */
  const typedNames = () => Array.from(WZ.secrets.keys());
  /** What an Apply was built from: the answers and the key names typed or saved here. A plan is only retried while
   *  this is unchanged; any other change of answers makes a new plan, reviewed before anything is sent. */
  function setupSig(d) {
    return F.stable([d.choices, d.values, Array.from(new Set(typedNames().concat(Array.from(WZ.saved)))).sort()]);
  }
  /** The Apply this review shows: running, finished (until you leave the review) or failed with the same answers. */
  function currentApply(d) {
    const ap = WZ.apply;
    if (!ap) return null;
    if (ap.running || (ap.done && !ap.failed)) return ap;
    if (ap.sig !== setupSig(d)) { WZ.apply = null; return null; }
    return ap;
  }
  function setupReview(d, ctx) {
    const ap = currentApply(d);
    const items = ap ? ap.items : F.setupPlan(d, ctx, typedNames()).items;
    const wrap = h('div', { class: 'wz-review' });
    if (WZ.saved.size) wrap.appendChild(callout('info', 'check-circle', 'Saved while testing:', Array.from(WZ.saved).join(', ') + ' (in the server’s env file).'));
    if (!items.length) {
      wrap.appendChild(emptyState('check-circle', 'Nothing to change', 'Your settings already match these choices. Go back to change something, or start work.', [h('a', { class: 'btn btn-primary', href: '#/start' }, icon('play', 'icon-sm'), 'Start work')], true));
      return wrap;
    }
    wrap.appendChild(h('p', { class: 'wz-review-sum' }, plural(items.length, 'item') + ', applied in this order. Each line shows a setting as it is now and as it will be.'));
    items.forEach((it, i) => {
      const st = ap ? ap.status[it.id] || { s: 'pending' } : { s: 'pending' };
      const ui = APPLY_UI[st.s];
      const retry = st.s === 'failed' && ap && !ap.running ? btn('Retry', { cls: 'btn-xs', icon: 'refresh', dataset: { action: 'retry-item', item: it.id }, onClick: () => applySetup([it.id]) }) : null;
      wrap.appendChild(h('section', { class: 'apply-item is-' + st.s, dataset: { item: it.id } },
        h('div', { class: 'apply-head' }, h('span', { class: 'apply-n' }, String(i + 1)), icon(it.icon || 'settings', 'icon-sm'), h('h3', { class: 'apply-title' }, it.title),
          h('span', { class: 'spacer' }), st.s === 'running' ? h('span', { class: 'spinner', 'aria-hidden': 'true' }) : null, h('span', { class: 'tag ' + ui[1] }, ui[0]), retry),
        changeRows(it.changes),
        st.message ? h('div', { class: st.s === 'failed' ? 'form-error' : 'field-hint', role: st.s === 'failed' ? 'alert' : null }, st.s === 'failed' ? icon('alert', 'icon-sm') : null, h('span', null, st.message)) : null));
    });
    if (ap && ap.done && ap.failed && !ap.running) wrap.appendChild(h('p', { class: 'wz-review-sum' }, 'Retry sends only the items that failed or were skipped, exactly as listed. Change an answer and this list is built again for you to review first.'));
    if (ap && ap.done && !ap.failed) {
      wrap.appendChild(h('section', { class: 'apply-done', role: 'status' },
        h('div', { class: 'apply-done-head' }, icon('check-circle', 'icon-lg'), h('div', null, h('h3', null, 'Applied'), h('p', { class: 'muted' }, 'The factory runs with these settings now. Settings shows and edits all of them.'))),
        setupChips(),
        h('div', { class: 'apply-next' }, h('a', { class: 'btn btn-primary', href: '#/start', dataset: { action: 'go-start' } }, icon('play', 'icon-sm'), 'Start work'),
          h('a', { class: 'btn', href: '#/grid' }, icon('route', 'icon-sm'), 'Grid Card'), h('a', { class: 'btn', href: '#/factory' }, icon('factory', 'icon-sm'), 'Open the board'))));
    }
    return wrap;
  }
  /** Sends one request of the setup plan; the three special kinds are resolved here (see flows.setupPlan). */
  async function sendPlanRequest(req) {
    if (req.secret) {
      const value = WZ.secrets.get(req.secret);
      if (!value) { if (WZ.saved.has(req.secret)) return; throw new Error('The key is no longer on this page: go back to the keys and paste it again.'); }
      await saveSecretNow(req.secret);
      return;
    }
    if (req.repoBase) {
      const cfg = normalizeConfig(await api('/api/config'));
      const repos = clone(Array.isArray(cfg.repos) ? cfg.repos : []);
      const r = repos.find((x) => x && x.name === req.repoBase.name);
      if (!r) throw new Error('No repository named ' + req.repoBase.name + ' to set the base branch of.');
      r.base = req.repoBase.base || null;
      adoptConfig(await api('/api/config', { method: 'POST', body: { repos } }), ['repos']);
      return;
    }
    if (req.agentMcp) {
      const det = await api('/api/agents/' + enc(req.agentMcp.agent));
      const text = F.addMcp(det && det.text, req.agentMcp.server);
      if (text === null) throw new Error(req.agentMcp.agent + ': the agent file has no frontmatter to add mcp: to.');
      if (text !== det.text) await api('/api/agents/' + enc(req.agentMcp.agent), { method: 'POST', body: { text } });
      AG.cache.delete(req.agentMcp.agent);
      return;
    }
    const r = await api(req.path, { method: req.method || 'POST', body: req.body });
    if (req.path === '/api/config' || req.path === '/api/config/preset') adoptConfig(r, Object.keys(req.body || {}).concat(req.path === '/api/config/preset' ? ['providers', 'models', 'agents'] : []));
  }
  /** Apply the reviewed plan. With a failed Apply of the same answers, only its failed and skipped items are sent
   *  again (`only` narrows that to some); any change of answers builds a new plan, shown before anything is sent. */
  async function applySetup(only) {
    if (WZ.apply && WZ.apply.running) return;
    const d = draftOf('setup');
    const ctx = flowCtx();
    let ap = currentApply(d);
    if (ap && ap.done && !ap.failed) return;
    if (!ap) {
      const bad = firstInvalid('setup', d, ctx);
      if (bad) { blockAt('setup', bad); return; }
      const plan = F.setupPlan(d, ctx, typedNames());
      if (!plan.items.length) return;
      ap = WZ.apply = { sig: setupSig(d), items: plan.items, status: {}, running: false, done: false, failed: false };
      only = null;
    }
    ap.running = true;
    ap.failed = false;
    invalidate('flow');
    const failed = new Set(Object.keys(ap.status).filter((k) => ap.status[k].s === 'failed'));
    const todo = ap.items.filter((it) => (only ? only.indexOf(it.id) >= 0 : !ap.status[it.id] || ap.status[it.id].s !== 'ok'));
    for (const it of todo) {
      const dep = (it.after || []).find((k) => failed.has(k) || (ap.status[k] && ap.status[k].s === 'failed'));
      if (dep) { ap.status[it.id] = { s: 'skipped', message: 'Skipped: it needs ' + dep + ', which failed.' }; failed.add(it.id); continue; }
      if (it.id === 'routing' && failed.has('models')) { ap.status[it.id] = { s: 'skipped', message: 'Skipped: it routes to the models above, which failed.' }; failed.add(it.id); continue; }
      ap.status[it.id] = { s: 'running' };
      invalidate('flow');
      try {
        for (const req of it.requests) await sendPlanRequest(req);
        ap.status[it.id] = { s: 'ok' };
        failed.delete(it.id);
      } catch (e) {
        ap.status[it.id] = { s: 'failed', message: e.message };
        failed.add(it.id);
      }
      invalidate('flow');
    }
    ap.failed = ap.items.some((it) => !ap.status[it.id] || ap.status[it.id].s !== 'ok');
    ap.done = true;
    // Keys saved during this Apply move from typed to saved: the answers are the same, so the plan stays retryable.
    ap.sig = setupSig(d);
    AG.cache.clear();
    WZ.agentMcp = null;
    await Promise.all([refreshState(), loadConfig(), loadSetup(), loadSecrets()].map((p) => Promise.resolve(p).catch(() => {})));
    ap.running = false;
    if (!ap.failed) {
      toast('Setup applied: ' + plural(ap.items.length, 'item') + '.', { kind: 'ok', action: { label: 'Start work', href: '#/start' } });
      WZ.drafts.setup = F.emptyDraft();
      WZ.drafts.setup.at = 'review';
      lsSet(flowKey('setup'), null);
      WZ.tests.clear();
    } else toast('Some setup items failed: retry them, or fix them in Settings.', { kind: 'error' });
    invalidate('flow');
  }
  // ---------------------------------------------------------------- start review → start
  function startReview(d, ctx) {
    const req = F.startRequest(projectedDraft(F.flowOf('start'), d, ctx), ctx);
    const rows = req.summary.map(([k, v]) => [h('dt', null, k), h('dd', null, String(v))]);
    const text = String(F.valueOf(d, 'describe', 'text', '')).trim();
    const pre = req.requests.map((r) => r.method + ' ' + r.path + '\n' + JSON.stringify(r.body, null, 2)).join('\n\n') + (req.message ? '\n\nPOST /api/chats/<new chat>/messages\n' + JSON.stringify({ text: req.message }, null, 2) : '');
    const first = req.requests[0] || {};
    const fx = formExt('start', { request: Object.freeze({ kind: req.kind, path: first.path, body: clone(first.body || {}) }),
      repo: () => (first.body && first.body.repo) || null, routing: () => (first.body ? first.body.routing : undefined) });
    return h('div', { class: 'wz-run' },
      h('dl', { class: 'kv wz-kv' }, rows, text ? [h('dt', null, req.kind === 'chat' ? 'First message' : 'Request'), h('dd', { class: 'wz-text' }, text.length > 400 ? text.slice(0, 400) + '…' : text)] : null),
      fx.nodes,
      h('details', { class: 'wz-raw' }, h('summary', null, icon('chevron', 'icon-xs chev'), 'Exactly what the server receives'), h('pre', { class: 'code-block wz-json' }, pre)),
      WZ.startError ? h('div', { class: 'form-error', role: 'alert' }, icon('alert', 'icon-sm'), h('span', null, WZ.startError)) : null);
  }
  async function runStart() {
    if (WZ.starting) return;
    const d = draftOf('start');
    const ctx = flowCtx();
    const bad = firstInvalid('start', d, ctx);
    if (bad) { blockAt('start', bad); return; }
    const req = F.startRequest(projectedDraft(F.flowOf('start'), d, ctx), ctx);
    WZ.starting = true;
    WZ.startError = null;
    invalidate('flow');
    try {
      const since = S.lastSeq;
      const first = req.requests[0];
      const r = await api(first.path, { method: 'POST', body: first.body });
      if (req.kind === 'job' && r && r.job) upsertJob(r.job, 0, since);
      if (req.kind === 'plan' && r && r.plan) upsertPlan(r.plan, 0, since);
      if (req.kind === 'chat' && r && r.chat) {
        upsertChat(r.chat);
        const st = chatStore(r.chat.id);
        st.loaded = true; st.history = []; st.baseSeq = S.lastSeq; st.completed = 0; st.firstUser = ''; st.locals = [];
        if (req.message) {
          st.locals.push({ key: 'l' + (++localSeq), text: req.message, anchor: S.lastSeq + 0.5 });
          try { const m = await api('/api/chats/' + enc(r.chat.id) + '/messages', { method: 'POST', body: { text: req.message } }); if (m && m.chat) upsertChat(m.chat); }
          catch (e) { st.locals = []; toast('The chat started, but the first message was not sent: ' + e.message, { kind: 'error' }); }
        }
      }
      if (first.body && first.body.repo) lsSet('sf.lastRepo', first.body.repo);
      const where = F.hashFor(req.kind, r);
      toast(req.kind === 'job' ? ['Job ', h('strong', { class: 'mono' }, r.job.id), ' queued.'] : req.kind === 'plan' ? ['Plan ', h('strong', { class: 'mono' }, r.plan.id), ' created: the lead is planning.'] : 'Chat started.', { kind: 'ok' });
      WZ.drafts.start = F.emptyDraft();
      lsSet(flowKey('start'), null);
      if (req.kind === 'job') D.back = '#/factory';
      location.hash = where;
    } catch (e) {
      WZ.startError = (e.status === 400 ? 'The server refused it: ' : 'Not started: ') + e.message;
    } finally {
      WZ.starting = false;
      invalidate('flow');
    }
  }
  // ---------------------------------------------------------------- the flow as a map
  /** A map card's head: its title takes focus when the card opens from the map, so a keyboard user lands in it. */
  function mapCardHead(title, close, eyebrow) {
    return h('div', { class: 'map-card-head' }, h('div', null, eyebrow ? h('div', { class: 'wz-eyebrow' }, eyebrow) : null, h('h2', { class: 'map-card-title', tabindex: '-1' }, title)), close);
  }
  function flowStepCard(flowId, flow, node, d, ctx, path, cur) {
    const close = h('a', { class: 'btn btn-ghost btn-icon btn-sm', href: '#/' + flowId + '/map', 'aria-label': 'Close the card', dataset: { action: 'close-card' } }, icon('x', 'icon-sm'));
    close.addEventListener('click', () => { WZ.focus = { node: node.id }; });
    const card = (label, ...kids) => h('aside', { class: 'map-card', 'aria-label': label, dataset: { node: node.id } }, ...kids);
    if (node.id.indexOf('go:') === 0) {
      const o = F.optionsOf(flow.steps[0], F.emptyDraft(), ctx).find((x) => 'go:' + x.id === node.id);
      if (!o) return null;
      const open = btn(o.id === 'board' ? 'Open the board' : o.id === 'setup' ? 'Open setup' : 'Start this', { cls: 'btn-primary btn-sm', icon: 'arrow-right', onClick: () => { if (o.start && o.go.indexOf('#/start') === 0) startFlowWith(o.start); else location.hash = o.go; } });
      return card(o.title, mapCardHead(o.title, close),
        h('dl', { class: 'map-card-dl' }, h('dt', null, 'What it does'), h('dd', null, o.does), h('dt', null, 'Choose this when'), h('dd', null, o.when)), h('div', { class: 'map-card-actions' }, open));
    }
    if (node.id === 'end') return card(node.label, mapCardHead(node.label, close),
      h('p', { class: 'muted' }, flowId === 'setup' ? 'Apply saves every choice to the server in order; then Start work opens.' : 'Start sends the request and opens the job, the plan or the chat.'));
    if (node.id.indexOf('if:') === 0) {
      // A check on an earlier answer: say which answer decides, and go to that step.
      const of = F.stepOf(flow, node.id.slice(3));
      const decider = of && of.id === 'local-details' ? F.stepOf(flow, 'where') : of && of.id === 'describe' ? F.stepOf(flow, 'what') : null;
      return card(node.label, mapCardHead(node.label, close, 'A check, not a choice'),
        h('p', { class: 'grid-card-sum' }, 'Nothing is chosen here: ' + (decider ? 'your answer to “' + F.titleOf(decider, d) + '” decides which way this goes.' : 'an earlier answer decides which way this goes.')),
        decider ? h('div', { class: 'map-card-actions' }, h('a', { class: 'btn btn-sm', href: '#/' + flowId + '/' + decider.id, dataset: { action: 'go-step' } }, icon('arrow-right', 'icon-sm'), 'Go to that question')) : null);
    }
    const step = F.stepOf(flow, node.id);
    if (!step) return null;
    const pi = path.findIndex((p) => p.id === step.id);
    const opts = step.kind === 'choice' ? F.optionsOf(step, d, ctx) : [];
    const chosen = F.listOf(F.choiceOf(d, step.id));
    const ans = answerText(step, d, ctx);
    return card(F.titleOf(step, d),
      mapCardHead(F.titleOf(step, d), close, ((flow.groups || {})[step.group] || '') + (pi >= 0 ? ' · step ' + (pi + 1) : ' · not on your path')),
      procedureList(step.procedure, 'proc-sm'),
      opts.length ? h('ul', { class: 'map-opts' }, opts.slice(0, 10).map((o) => h('li', { class: chosen.indexOf(o.id) >= 0 ? 'is-on' : '' }, h('strong', null, o.title), o.does && !o.compact ? h('span', { class: 'muted' }, ' — ' + o.does) : o.sub ? h('span', { class: 'muted mono' }, ' — ' + o.sub) : null))) : null,
      ans ? h('p', { class: 'map-card-now' }, h('span', { class: 'muted' }, 'Your answer: '), ans) : null,
      h('div', { class: 'map-card-actions' }, h('a', { class: 'btn btn-primary btn-sm', href: '#/' + flowId + '/' + step.id, dataset: { action: 'go-step' } }, icon('arrow-right', 'icon-sm'), step.id === cur ? 'Continue here' : 'Go to this step')));
  }
  function renderFlowMap(root, flowId) {
    const flow = F.flowOf(flowId);
    const d = draftOf(flowId);
    const ctx = flowCtx();
    root.appendChild(flowHead(flowId, flow, 'map'));
    const path = flowId === 'welcome' ? [] : F.pathOf(flow, d, ctx);
    const on = new Set(path.map((p) => p.id));
    const cur = flowId === 'welcome' ? null : currentStepId(flowId, flow, d, ctx);
    const graph = F.flowMap(flow, ctx);
    const ids = new Set(graph.nodes.map((n) => n.id));
    // The highlighted path runs through the checks: step → its check → the next step.
    const pairs = new Set();
    for (let i = 0; i + 1 < path.length; i++) {
      const a = path[i].id;
      const b = path[i + 1].id;
      if (ids.has('if:' + a)) { pairs.add(a + '>if:' + a); pairs.add('if:' + a + '>' + b); on.add('if:' + a); } else pairs.add(a + '>' + b);
    }
    if (path.length && !F.nextOf(flow, path[path.length - 1].id, d, ctx)) pairs.add(path[path.length - 1].id + '>end');
    const layout = layoutMap(graph, { rankGap: 44 });
    const sel = S.route.node || null;
    const svg = drawMap(layout, {
      label: flow.title + ' as a map: steps, their choices and where they lead',
      selected: sel,
      nodeClass: (n) => [on.has(n.id) ? 'is-path' : '', path.some((p) => p.id === n.id && p.done) ? 'is-done' : '', n.id === cur ? 'is-current' : '', n.cond ? 'is-cond' : ''].filter(Boolean).join(' '),
      hotEdge: (e) => pairs.has(e.from + '>' + e.to),
      onNode: (n) => { WZ.focus = { card: true }; location.hash = '#/' + flowId + '/map/' + enc(n.id); },
      title: (n) => n.label.replace(/\n/g, ' ') + (n.id === cur ? ' (you are here)' : on.has(n.id) ? ' (on your path)' : '') + '. Opens its card.',
    });
    const card = sel ? flowStepCard(flowId, flow, { id: sel, label: (graph.nodes.find((n) => n.id === sel) || {}).label || sel }, d, ctx, path, cur) : null;
    const hasCond = graph.nodes.some((n) => n.cond);
    const legend = h('div', { class: 'map-legend', 'aria-hidden': 'true' },
      flowId === 'welcome' ? null : [h('span', { class: 'ml-item' }, h('span', { class: 'ml-sw is-path' }), 'your path'), h('span', { class: 'ml-item' }, h('span', { class: 'ml-sw is-current' }), 'you are here')],
      h('span', { class: 'ml-item' }, h('span', { class: 'ml-sw is-diamond' }), 'you choose here'),
      hasCond ? h('span', { class: 'ml-item' }, h('span', { class: 'ml-sw is-diamond is-cond' }), 'a check on an earlier answer') : null,
      h('span', { class: 'ml-item' }, 'Select a step for its card'));
    const box = mapBox(svg, layout, 'flow-map', 0.9, { narrowMin: 0.78, center: graph.nodes[0] && graph.nodes[0].id });
    root.appendChild(h('div', { class: 'map-layout' + (card ? ' has-card' : '') }, h('div', { class: 'map-pane' }, legend, box), card, card ? mapScrim('#/' + flowId + '/map') : null));
  }
  /** Behind a map card shown over the map (narrower screens): dims the map; a click closes the card. */
  function mapScrim(href) {
    const el = h('div', { class: 'map-scrim', 'aria-hidden': 'true' });
    el.addEventListener('click', () => { location.hash = href; });
    return el;
  }
  RENDER.flow = function () {
    if (S.route.view !== 'flow') return;
    // A navigation is pending (Start, Start over or a link just set the hash): do not draw, and so re-save, the old
    // step; the route change draws the new one.
    const pr = parseRoute();
    if (pr.view !== 'flow' || pr.flow !== S.route.flow || pr.mode !== S.route.mode || pr.step !== S.route.step || pr.node !== S.route.node) return;
    const root = document.getElementById('flow-root');
    const main = document.getElementById('main');
    const flowId = S.route.flow || 'welcome';
    const y = main.scrollTop;
    const active = document.activeElement && root.contains(document.activeElement) ? document.activeElement : null;
    const ds = active && active.dataset ? active.dataset : {};
    const keep = WZ.focus || (active ? { id: active.id || null, opt: ds.opt || null, node: ds.node && active.matches('.map-node') ? ds.node : null, test: ds.test || null, action: ds.action || null } : null);
    WZ.focus = null;
    clear(root);
    root.dataset.flow = flowId;
    if (!F) { root.appendChild(emptyState('alert', 'The guided flows did not load', 'flows.js is missing from this server.')); return; }
    if (!CFG.orig && flowId !== 'welcome') {
      if (CFG.loadError) root.appendChild(emptyState('alert', 'Cannot load the settings', CFG.loadError, [btn('Retry', { cls: 'btn-primary', icon: 'refresh', onClick: () => { CFG.loadError = null; ensureConfig().catch(() => {}); invalidate('flow'); } })]));
      else { root.appendChild(skeletonLines(10)); ensureConfig().then(() => invalidate('flow')).catch(() => invalidate('flow')); }
      return;
    }
    if (S.route.mode === 'map') renderFlowMap(root, flowId);
    else if (flowId === 'welcome') renderWelcome(root);
    else renderWizard(root, flowId);
    const q = (sel) => root.querySelector(sel);
    if (keep && keep.title) { const t = document.getElementById('wz-title'); if (t) t.focus({ preventScroll: true }); }
    else if (keep && keep.invalid) { const el = q('.invalid, .wz-error:not([hidden])'); if (el) { el.scrollIntoView({ block: 'center' }); if (el.focus) el.focus({ preventScroll: true }); } main.scrollTop = Math.max(0, main.scrollTop); }
    else if (keep && keep.card) { const t = q('.map-card .map-card-title'); if (t) t.focus({ preventScroll: true }); main.scrollTop = y; }
    else {
      const el = keep && keep.id ? document.getElementById(keep.id)
        : keep && keep.opt ? q('.opt-card[data-opt="' + CSS.escape(keep.opt) + '"]')
          : keep && keep.node ? q('.map-node[data-node="' + CSS.escape(keep.node) + '"]')
            : keep && keep.test ? q('[data-action="wz-test"][data-test="' + CSS.escape(keep.test) + '"]')
              : keep && keep.action ? q('[data-action="' + CSS.escape(keep.action) + '"]') : null;
      if (el) el.focus({ preventScroll: true });
      main.scrollTop = y;
    }
    const mb = root.querySelector('.map-scroll');
    if (mb && mb._fit) requestAnimationFrame(() => mb._fit());
    placeToasts();
  };

  // ================================================================ Grid Card (§25): the whole harness on one map
  // Lanes left → right: Set up, the factory line, Chat, Plans; rows top → bottom. Hand-placed so the routes read like
  // the owner's GRID_CARD: every module a node, every route a labelled arrow, live status on each node.
  const GRIDV = { showMap: false, focusNode: null, focusCard: false };
  function gridNodeDef(id) { return F ? F.GRID.nodes.find((n) => n.id === id) || null : null; }
  /** {tone, text, detail} for a Grid node: setup steps from GET /api/setup, the rest from live jobs, plans and chats.
   *  Tones: ok (ready), todo (not set up yet, on a fresh install), warn (needs attention), accent (work here now),
   *  human (waiting for you). */
  function gridStatus(n) {
    const st = SETUPST.status && isObj(SETUPST.status.steps) ? SETUPST.status.steps : null;
    const fresh = freshInstall();
    if (n.status === 'safety') {
      if (!CFG.orig) return { tone: null, text: '', detail: '' };
      const hz = isObj(CFG.orig.harness) ? CFG.orig.harness : {};
      const pp = isObj(CFG.orig.pipeline) ? CFG.orig.pipeline : {};
      const t = (hz.sandbox === 'bwrap' ? 'bwrap sandbox' : 'no sandbox') + ' · ' + (pp.auto_merge === false ? 'always ask' : 'auto-merge');
      return { tone: hz.sandbox === 'bwrap' ? 'ok' : fresh ? 'todo' : 'warn', text: hz.sandbox === 'bwrap' ? 'sandboxed' : 'no sandbox', detail: t + (Array.isArray(CFG.orig.escalate_paths) ? ' · ' + plural(CFG.orig.escalate_paths.length, 'protected glob') : '') };
    }
    if (n.status) {
      const s = st && isObj(st[n.status]) ? st[n.status] : null;
      if (!s) return { tone: null, text: SETUPST.missing ? '' : SETUPST.loading ? 'checking…' : '', detail: '' };
      return { tone: s.ok ? 'ok' : fresh ? 'todo' : 'warn', text: s.ok ? 'ready' : fresh ? 'to do' : 'needs attention', detail: String(s.detail || '') };
    }
    if (n.stages) {
      let count = 0;
      for (const j of visibleJobs()) {
        for (const sg of n.stages) {
          const [stage, mode] = sg.split(':');
          if (j.stage === stage && (!mode || (jobDelivery(j) || 'branch') === mode)) count++;
        }
      }
      if (n.kind === 'output') {
        const def = deliveryDefault() === n.mode;
        return { tone: count ? 'ok' : null, text: count ? plural(count, 'job') : def ? 'the default' : '', detail: (DELIVERY_UI[n.mode] ? 'APPROVE: ' + DELIVERY_UI[n.mode].text : '') + (def ? ' (the configured default)' : '') };
      }
      if (count) return { tone: n.id === 'human' ? 'human' : 'accent', text: plural(count, 'job') + ' here', detail: plural(count, 'job') + ' at this step now' };
      if (n.agent) { const m = modelOf(n.agent); return { tone: null, text: m.model !== '—' ? m.model : '', detail: runsOn(n.agent, m) }; }
      return { tone: null, text: n.id === 'human' ? 'nothing waits' : '', detail: '' };
    }
    if (n.plans) {
      const c = visiblePlans().filter((p) => n.plans.indexOf(p.stage) >= 0).length;
      if (n.agent) { const m = modelOf(n.agent); if (!c) return { tone: null, text: m.model !== '—' ? m.model : '', detail: runsOn(n.agent, m) }; }
      return { tone: c ? (n.id === 'plan-ready' ? 'human' : 'accent') : null, text: c ? plural(c, 'plan') : '', detail: c ? plural(c, 'plan') + ' at this step' : '' };
    }
    if (n.view) {   // a view module's node: its rail badge (the tracker's: items waiting for you)
      const v = REG.views.get(n.view);
      const c = v && v.badge ? v.badge.n : 0;
      return { tone: c ? 'human' : null, text: c ? c + ' waiting' : '', detail: c ? (v.badge.label || c + ' waiting for you') : '' };
    }
    if (n.chats) {
      const all = Array.from(S.chats.values()).filter((c) => !S.filterRepo || c.repo === S.filterRepo);
      const run = all.filter((c) => c.status === 'running').length;
      return { tone: run ? 'accent' : null, text: run ? run + ' running' : all.length ? plural(all.length, 'chat') : '', detail: plural(all.length, 'chat') + (run ? ', ' + run + ' running' : '') };
    }
    return { tone: null, text: '', detail: '' };
  }
  /** "builder runs on deepseek-v4-pro (provider deepseek)": the model, not the tier alias it is reached by. */
  function runsOn(agent, m) {
    if (!m || m.model === '—') return '';
    const drv = m.driver && m.driver !== 'builtin' ? ' through the ' + m.driver + ' CLI' : '';
    return agent + ' runs on ' + m.model + (m.provider ? ' (provider ' + m.provider + ')' : '') + drv + '.';
  }
  function gridLayout() {
    // The line's reviewers (any count); before state loads, one placeholder box stands for them.
    const revs = pipelineReviewers().length ? pipelineReviewers() : ['…'];
    const PAD = 16;
    const GAP = 34;
    const ROW = 64;
    const NH = 40;
    const TOP = PAD + 2;
    const revW = 108;
    const revGap = 8;
    const revGroupW = revs.length * revW + (revs.length - 1) * revGap;
    const half = Math.max(revGroupW / 2, 200);
    const lanes = { setup: { x: PAD, w: 140 } };
    lanes.line = { x: lanes.setup.x + lanes.setup.w + GAP };
    const cx = lanes.line.x + 16 + half;
    const loopX = cx + half + 20;
    // Needs human sits right of the approver, below the review loop: ESCALATE and the gate's "protected · too big"
    // both turn right into it, each labelled where it leaves.
    const humanW = 124;
    const hx = cx + 76 + 72 + humanW / 2;
    lanes.line.w = Math.max(loopX + 16, hx + humanW / 2 + 14) - lanes.line.x;
    lanes.chat = { x: lanes.line.x + lanes.line.w + GAP, w: 144 };
    lanes.plans = { x: lanes.chat.x + lanes.chat.w + GAP, w: 150 };
    lanes.housekeeping = lanes.chat;   // under Chat, beside the line's outputs (row 9)
    const width = lanes.plans.x + lanes.plans.w + PAD;
    const rowTop = (r) => TOP + r * ROW;
    const rowC = (r) => rowTop(r) + NH / 2;
    const laneC = (l) => lanes[l].x + lanes[l].w / 2;
    const nodes = [];
    const byId = {};
    const place = (def, x, y, extra) => {
      const st = gridStatus(def);
      const n = Object.assign({ id: def.id, label: def.label, shape: def.shape || 'box', sub: st.text || '' }, extra || {});
      Object.assign(n, measureNode(n));
      n.x = x;
      n.y = y;
      n.def = def;
      n.status = st;
      nodes.push(n);
      byId[n.id] = n;
      return n;
    };
    for (const def of F.GRID.nodes) {
      if (def.kind === 'reviewers' || def.kind === 'output') continue;
      if (def.view && !REG.views.has(def.view)) continue;   // a view module's node, without that module
      if (def.lane === 'setup') place(def, laneC('setup'), rowC(def.row), { w: def.shape === 'pill' ? 116 : 124, h: def.shape === 'pill' ? 34 : NH });
      else if (def.lane === 'line') {
        if (def.id === 'human') place(def, hx, rowC(def.row), { w: humanW, h: NH });
        else if (def.shape === 'diamond') place(def, cx, rowC(def.row), { w: def.id === 'start' ? 120 : 100, h: def.id === 'start' ? 50 : 46 });
        else if (def.shape === 'small') place(def, cx, rowC(def.row), { w: 112, h: 28 });
        else place(def, cx, rowC(def.row), { w: 152, h: NH });
      } else place(def, laneC(def.lane), rowC(def.row), { w: def.lane === 'chat' ? 132 : def.lane === 'housekeeping' ? 168 : 140, h: def.shape === 'pill' ? 34 : NH });
    }
    const revDef = gridNodeDef('reviewers');
    revs.forEach((r, i) => {
      const x = cx - revGroupW / 2 + revW / 2 + i * (revW + revGap);
      const running = visibleJobs().filter((j) => j.stage === 'review' && j.reviews && j.reviews[r] === 'running').length;
      const m = modelOf('reviewer-' + r);
      const def = Object.assign({}, revDef, { id: 'rev:' + r, label: r, agent: 'reviewer-' + r, lens: r });
      // A model id too long for the box drops its vendor prefix (deepseek-v4-flash → v4-flash); the card and the
      // tooltip keep the full name.
      let short = m.model !== '—' ? m.model : '';
      if (short && textWidth(short, mfont('edge')) > revW - 14 && /^[a-z0-9.]+-./i.test(short)) short = short.replace(/^[a-z0-9.]+-/i, '');
      const n = { id: def.id, label: r, shape: 'box', sub: running ? running + ' reviewing' : short, w: revW, h: NH, maxW: revW - 14, subW: revW - 12 };
      Object.assign(n, measureNode(n), { x, y: rowC(revDef.row), def, status: { tone: running ? 'accent' : null, text: n.sub, detail: runsOn('reviewer-' + r, m) + (running ? ' ' + running + ' reviewing now.' : '') } });
      nodes.push(n);
      byId[n.id] = n;
    });
    const outs = F.GRID.nodes.filter((d) => d.kind === 'output');
    outs.forEach((def, i) => place(def, cx + (i - 1) * 134, rowC(def.row), { w: 122, h: NH }));
    // panels (modules): the title sits inside, above the first row
    const panel = (lane, r0, r1, title) => ({ x: lanes[lane].x, y: rowTop(r0) - 24, w: lanes[lane].w, h: rowTop(r1) + NH - rowTop(r0) + 34, title });
    const panels = [panel('setup', 1, 7, 'SET UP'), panel('line', 2, 9, 'FACTORY LINE'), panel('chat', 3, 4, 'CHAT'), panel('plans', 3, 7, 'PLANS')];
    panels[2].h += 30; // the hand-off leaves Hand off downwards, labelled inside Chat
    for (const p of panels) p.titleY = p.y + 15;
    // routes: straight segments; each label sits just after its route leaves the node, as in the owner's maps
    const E = [];
    const N = (id) => byId[id];
    const bottom = (n) => ({ x: n.x, y: n.y + n.h / 2 });
    const topOf = (n) => ({ x: n.x, y: n.y - n.h / 2 });
    const leftOf = (n) => ({ x: n.x - n.w / 2, y: n.y });
    const rightOf = (n) => ({ x: n.x + n.w / 2, y: n.y });
    const edge = (from, to, pts, label, lab, cls) => E.push(Object.assign({ from, to, d: orthoPath(pts, 8), label: label || '', cls: cls || '' }, lab || {}));
    const vert = (a, b, label) => {
      const p0 = bottom(N(a));
      const p1 = topOf(N(b));
      edge(a, b, [p0, { x: p0.x, y: p1.y }], label, label ? { lx: p0.x + 7, ly: p0.y + 11, anchor: 'start' } : null);
    };
    const chain = ['welcome', 'providers', 'models', 'routing', 'tools', 'safety', 'repos', 'output'];
    for (let i = 0; i + 1 < chain.length; i++) vert(chain[i], chain[i + 1], i === 0 ? 'first run' : '');
    const gx = lanes.setup.x + lanes.setup.w + GAP / 2;
    const start = N('start');
    const sl = { x: start.x - start.w / 2, y: start.y };
    edge('output', 'start', [rightOf(N('output')), { x: gx, y: N('output').y }, { x: gx, y: start.y }, sl], 'ready', { lx: gx + 8, ly: start.y - 9, anchor: 'start' });
    const w0 = rightOf(N('welcome'));
    edge('welcome', 'start', [w0, { x: start.x, y: w0.y }, topOf(start)], 'set up already', { lx: w0.x + 8, ly: w0.y - 9, anchor: 'start' });
    const sb = bottom(start);
    edge('start', 'queue', [sb, topOf(N('queue'))], 'feature', { lx: sb.x + 7, ly: sb.y + 10, anchor: 'start' });
    const sr = rightOf(start);
    const ccx = laneC('chat');
    const pcx = laneC('plans');
    edge('start', 'chat-agent', [sr, { x: ccx, y: sr.y }, topOf(N('chat-agent'))], 'chat · analyse', { lx: ccx - 7, ly: (sr.y + N('chat-agent').y - N('chat-agent').h / 2) / 2 + 4, anchor: 'end' });
    edge('start', 'lead', [sr, { x: pcx, y: sr.y }, topOf(N('lead'))], 'plan', { lx: pcx - 7, ly: (sr.y + N('lead').y - N('lead').h / 2) / 2 + 4, anchor: 'end' });
    vert('queue', 'spec', '');
    vert('spec', 'builder', 'spec.md');
    const b = N('builder');
    const w = N('wait');
    revs.forEach((r, i) => {
      const rn = N('rev:' + r);
      const k = i - (revs.length - 1) / 2;
      const p0 = { x: b.x + k * Math.min(22, (b.w - 30) / Math.max(1, revs.length - 1)), y: b.y + b.h / 2 };
      E.push({ from: 'builder', to: rn.id, d: orthoPath([p0, topOf(rn)], 0), label: i === 0 ? '' : '' });
      const q1 = { x: w.x + k * Math.min(18, (w.w - 24) / Math.max(1, revs.length - 1)), y: w.y - w.h / 2 };
      E.push({ from: rn.id, to: 'wait', d: orthoPath([bottom(rn), q1], 0), label: '' });
    });
    const maxRes = S.pipeline && S.pipeline.max_resumes !== undefined ? S.pipeline.max_resumes : 2;
    const wr = rightOf(w);
    const loopLab = labelLines('any CHANGES → resume builder (max ' + maxRes + ')', Math.max(110, loopX - wr.x - 14), 'edge', 3);
    edge('wait', 'builder', [wr, { x: loopX, y: wr.y }, { x: loopX, y: b.y }, rightOf(b)], 'loop', { label: loopLab.join(' '), lines: loopLab, lx: wr.x + 8, ly: wr.y + 15, anchor: 'start' }, 'loop');
    vert('wait', 'approver', 'all PASS');
    vert('approver', 'gate', 'APPROVE');
    const ap = N('approver');
    const hu = N('human');
    edge('approver', 'human', [rightOf(ap), leftOf(hu)], 'ESCALATE', { lx: ap.x + ap.w / 2 + 6, ly: ap.y - 8, anchor: 'start' }, 'human');
    const gt = N('gate');
    const gr = rightOf(gt);
    edge('gate', 'human', [gr, { x: hu.x, y: gr.y }, bottom(hu)], 'protected · too big', { lx: gr.x + 6, ly: gr.y - 8, anchor: 'start' }, 'human');
    for (const def of outs) {
      const o = N(def.id);
      const t = topOf(o);
      const p0 = Math.abs(o.x - gt.x) < 4 ? bottom(gt) : portToward(gt, { x: o.x, y: t.y });
      const left = o.x < gt.x - 4;
      const mid = Math.abs(o.x - gt.x) < 4;
      const lp = { x: p0.x + (t.x - p0.x) * 0.3, y: p0.y + (t.y - p0.y) * 0.3 };
      E.push({ from: 'gate', to: def.id, d: orthoPath([p0, t], 0), label: def.mode, lx: mid ? lp.x + 7 : left ? lp.x - 7 : lp.x + 7, ly: lp.y + 2, anchor: left ? 'end' : 'start', cls: def.mode === deliveryDefault() ? 'ok' : '' });
    }
    vert('lead', 'plan-ready', 'plan.md');
    vert('plan-ready', 'plan-jobs', 'Run');
    vert('plan-jobs', 'final', 'all merged');
    vert('final', 'plan-done', 'PASS');
    vert('chat-agent', 'handoff', 'branch ready');
    // A hand-off and a plan's jobs join one route into Queued (each labelled where it starts).
    const q = N('queue');
    const qr = rightOf(q);
    const chX = lanes.chat.x - GAP / 2;
    const ho = N('handoff');
    const pj = N('plan-jobs');
    const hb = bottom(ho);
    const hy = hb.y + 20;
    edge('handoff', 'queue', [hb, { x: hb.x, y: hy }, { x: chX, y: hy }, { x: chX, y: q.y }, qr], 'branch as round 1', { lines: ['branch as', 'round 1'], lx: hb.x + 7, ly: hb.y + 9, anchor: 'start' });
    edge('plan-jobs', 'queue', [leftOf(pj), { x: chX, y: pj.y }, { x: chX, y: q.y }, qr], 'each job', { lx: leftOf(pj).x - 6, ly: pj.y - 8, anchor: 'end' });
    // The Housekeeping Tracker (a view module's node): what a job leaves open comes in from the outputs; a follow-up
    // goes back to Queued as a new job, joining the plan's "each job" route.
    const tr = N('tracker');
    const of = N('out-folder');
    if (tr && of) {
      const a = rightOf(of);
      const lab = labelLines('open items when a job ends', Math.max(70, leftOf(tr).x - a.x - 14), 'edge', 2);
      edge('out-folder', 'tracker', [a, leftOf(tr)], 'open items when a job ends', { label: lab.join(' '), lines: lab, lx: a.x + 7, ly: a.y - 8 - (lab.length - 1) * 12, anchor: 'start' });
      const t0 = topOf(tr);
      edge('tracker', 'queue', [t0, { x: t0.x, y: pj.y }, { x: chX, y: pj.y }, { x: chX, y: q.y }, qr], 'make a job', { lx: t0.x + 7, ly: t0.y - 9, anchor: 'start' });
    }
    const height = rowTop(9) + NH + PAD;
    return { width, height, nodes, edges: E, panels };
  }
  /** The card's main button says where it goes. */
  function openLabel(href) {
    if (/^#\/agents\//.test(href)) return 'Open the agent file';
    if (href === '#/factory') return 'Open the board';
    if (href === '#/plans') return 'Open Plans';
    if (href === '#/chat') return 'Open Chat';
    if (href === '#/start') return 'Start work';
    if (href === '#/welcome') return 'Open Welcome';
    if (href === '#/settings/routing') return 'Open Settings › Routing';
    if (href === '#/settings/repos') return 'Open Settings › Repositories';
    for (const v of REG.views.values()) if (href === '#/' + v.routes[0]) return 'Open ' + v.title;   // a view module's node
    return 'Open';
  }
  function gridCardFor(id) {
    const n = gridNodeDef(id) || (id.indexOf('rev:') === 0 ? Object.assign({}, gridNodeDef('reviewers'), { id, label: id.slice(4), agent: 'reviewer-' + id.slice(4), lens: id.slice(4) }) : null);
    if (!n) return null;
    const st = gridStatus(n);
    const close = h('a', { class: 'btn btn-ghost btn-icon btn-sm', href: '#/grid', 'aria-label': 'Close the card', dataset: { action: 'close-card' } }, icon('x', 'icon-sm'));
    close.addEventListener('click', () => { GRIDV.focusNode = id; });
    let proc = [];
    let title = n.label;
    let summary = '';
    let source = '';
    let bookId = null;
    if (n.proc && n.proc.flow) {
      const step = F.stepOf(F.flowOf(n.proc.flow), n.proc.step);
      if (step) { proc = step.procedure; summary = F.titleOf(step, draftOf(n.proc.flow)); source = 'From the ' + (n.proc.flow === 'setup' ? 'setup' : n.proc.flow) + ' guide'; }
    } else if (n.proc && n.proc.agent) {
      const key = n.proc.agent;
      const agentName = n.agent || (n.lens ? 'reviewer-' + n.lens : key);
      const fromServer = DOCS.data ? DOCS.data.sections.find((s) => s.id === agentName || s.id === key) || (key === 'reviewer' ? DOCS.data.sections.find((s) => /^reviewer-/.test(s.id)) : null) : null;
      const local = F.AGENT_PROCEDURES[key] || {};
      // The card's short procedure; the agent file's own sections are in Procedures.
      const serverSteps = fromServer ? (fromServer.procedure.length ? fromServer.procedure : [].concat.apply([], (fromServer.parts || []).map((pt) => pt.steps))).filter((x) => x[0]) : [];
      proc = local.procedure && local.procedure.length ? local.procedure : serverSteps;
      summary = (fromServer && fromServer.summary) || local.summary || '';
      source = 'Working procedure';
      // Where the booklet covers it: the agent's own section, else the flow it belongs to.
      const serverBook = !!(DOCS.data && DOCS.data.sections.length);
      if (fromServer) bookId = fromServer.id;
      else if (local.procedure) bookId = !serverBook || key === 'factory-line' || key === 'plans' ? key : n.lane === 'plans' ? 'plans' : 'factory-line';
      if (n.lens) title = 'reviewer-' + n.lens;
    }
    const actions = [];
    if (n.configure) actions.push(h('a', { class: 'btn btn-primary btn-sm', href: '#/setup/' + n.configure, dataset: { action: 'configure' } }, icon('settings', 'icon-sm'), 'Configure'));
    const open = n.open || (n.agent ? '#/agents/' + enc(n.agent) : n.lens ? '#/agents/reviewer-' + enc(n.lens) : null);
    if (open) actions.push(h('a', { class: 'btn btn-sm' + (n.configure ? '' : ' btn-primary'), href: open, dataset: { action: 'open' } }, icon('arrow-right', 'icon-sm'), openLabel(open)));
    const lane = F.GRID.lanes.find((l) => l.id === n.lane);
    // Which model the agent runs on, whatever is happening at the step now.
    const agentName = n.agent || (n.lens ? 'reviewer-' + n.lens : null);
    const model = agentName ? runsOn(agentName, modelOf(agentName)) : '';
    return h('aside', { class: 'map-card grid-card', 'aria-label': title, dataset: { node: id } },
      mapCardHead(title, close, lane ? lane.title : ''),
      st.text || st.detail ? h('div', { class: 'grid-card-status tone-' + (st.tone || 'none') }, st.tone ? h('span', { class: 'map-dot-sm tone-' + st.tone }) : null, h('span', null, st.detail || st.text)) : null,
      model && model !== st.detail ? h('p', { class: 'grid-card-model' }, model) : null,
      summary ? h('p', { class: 'grid-card-sum' }, summary) : null,
      proc.length ? [h('div', { class: 'proc-caption' }, source), procedureList(proc, 'proc-sm')] : null,
      bookId ? h('a', { class: 'grid-card-more', href: '#/procedures/' + enc(bookId), dataset: { action: 'procedure' } }, icon('book', 'icon-xs'), 'The full procedure in the booklet') : null,
      actions.length ? h('div', { class: 'map-card-actions' }, actions) : null);
  }
  /** Phones: the map as lists, in the map's own order (row by row), each row with the routes that leave it. */
  function gridList(layout) {
    const groups = [['setup', 'Set up'], ['line', 'Factory line'], ['chat', 'Chat'], ['plans', 'Plans'], ['housekeeping', 'Housekeeping']];
    const byId = Object.fromEntries(layout.nodes.map((n) => [n.id, n]));
    const name = (n) => (n.id.indexOf('rev:') === 0 ? 'reviewer-' + n.label : String(n.label).replace(/\n/g, ' '));
    return h('div', { class: 'grid-list' }, groups.map(([lane, title]) => {
      // Row by row as on the map; Needs human (beside the approver) follows the gate, the last place a job can stop.
      const rank = (n) => (n.id === 'human' ? (gridNodeDef('gate') || {}).row + 0.5 : n.def && typeof n.def.row === 'number' ? n.def.row : 0);
      const items = layout.nodes.filter((n) => (n.def && n.def.lane) === lane || (lane === 'line' && n.id.indexOf('rev:') === 0))
        .sort((a, c) => rank(a) - rank(c) || a.x - c.x);
      if (!items.length) return null;
      return h('section', { class: 'grid-list-sec' }, h('h2', { class: 'grid-list-title' }, title),
        h('ul', null, items.map((n) => {
          // Labelled routes leaving this node (the reviewers' fan-in has none).
          const routes = layout.edges.filter((e) => e.from === n.id && e.label && byId[e.to] && !/^(spec\.md|build\.md|plan\.md|first run)$/.test(e.label))
            .map((e) => (/^any CHANGES/.test(e.label) ? 'any CHANGES → builder (max ' + (S.pipeline && S.pipeline.max_resumes !== undefined ? S.pipeline.max_resumes : 2) + ')' : e.label + ' → ' + name(byId[e.to])));
          return h('li', null, h('a', { class: 'grid-row' + (S.route.node === n.id ? ' is-on' : ''), href: '#/grid/' + enc(n.id), dataset: { node: n.id } },
            h('span', { class: 'map-dot-sm tone-' + (n.status.tone || 'none') }), h('span', { class: 'grid-row-name' }, name(n)),
            h('span', { class: 'grid-row-st' }, n.status.text || ''), icon('chevron', 'icon-xs')),
          routes.length ? h('div', { class: 'grid-row-routes' }, routes.join(' · ')) : null);
        })));
    }));
  }
  /** The Grid legend: dots on phones (the list has no boxes), dots and boxes beside the map. */
  function gridLegend(narrow) {
    const fresh = freshInstall();
    const dot = (tone, text) => h('span', { class: 'ml-item' }, h('span', { class: 'map-dot-sm tone-' + tone }), text);
    return h('div', { class: 'map-legend grid-legend', 'aria-hidden': 'true' },
      dot('ok', 'ready'), fresh ? dot('todo', 'to do') : null, dot('warn', 'needs attention'),
      narrow ? [dot('accent', 'work here now'), dot('human', 'waiting for you')]
        : [h('span', { class: 'ml-item' }, h('span', { class: 'ml-sw is-accent' }), 'work here now'), h('span', { class: 'ml-item' }, h('span', { class: 'ml-sw is-human' }), 'waiting for you'),
          h('span', { class: 'ml-item' }, h('span', { class: 'ml-sw is-ok' }), 'where finished work goes (the default)')]);
  }
  RENDER.grid = function () {
    if (S.route.view !== 'grid') return;
    const root = document.getElementById('grid-root');
    const old = root.querySelector('.map-scroll');
    const sx = old ? old.scrollLeft : 0;
    const act = document.activeElement && root.contains(document.activeElement) && document.activeElement.dataset ? document.activeElement : null;
    const focused = act ? (act.matches('.map-node') ? { node: act.dataset.node } : act.dataset.action ? { action: act.dataset.action } : act.dataset.node ? { row: act.dataset.node } : null) : null;
    clear(root);
    if (!F) { root.appendChild(emptyState('alert', 'The Grid Card did not load', 'flows.js is missing from this server.')); return; }
    root.appendChild(viewHead('Grid Card', 'The Software Factory on one map: set up, start, the factory line, chat and plans, joined by their routes. Select a module for its working procedure. Every other section is on Home.',
      [h('a', { class: 'btn btn-sm', href: '#/procedures' }, icon('book', 'icon-sm'), 'Procedures'), h('a', { class: 'btn btn-sm btn-primary', href: '#/setup' }, icon('settings', 'icon-sm'), 'Setup')]));
    if (SETUPST.error) root.appendChild(callout('warn', 'alert', 'Setup status unavailable:', SETUPST.error));
    const layout = gridLayout();
    const sel = S.route.node;
    const narrow = window.matchMedia && window.matchMedia('(max-width: 760px)').matches;
    const svg = drawMap(layout, {
      label: 'Grid Card: the harness as one map', selected: sel,
      tone: (n) => n.status && n.status.tone,
      nodeClass: (n) => 'g-' + ((n.def && n.def.lane) || 'line') + (n.status && n.status.tone ? ' has-' + n.status.tone : '') + (n.def && n.def.kind === 'output' && n.def.mode === deliveryDefault() ? ' is-default' : ''),
      title: (n) => (n.id.indexOf('rev:') === 0 ? 'reviewer-' + n.label : String(n.label).replace(/\n/g, ' ')) + (n.status && (n.status.detail || n.status.text) ? ': ' + (n.status.detail || n.status.text) : '') + '. Opens its card.',
      onNode: (n) => { GRIDV.focusCard = true; location.hash = '#/grid/' + enc(n.id); },
      hotEdge: (e) => { const a = layout.nodes.find((x) => x.id === e.to); return !!(a && a.status && (a.status.tone === 'accent' || a.status.tone === 'human') && e.from !== 'wait'); },
    });
    const card = sel ? gridCardFor(sel) : null;
    // Beside the map only while the map stays readable (85% or more); else the card opens over the map.
    const MIN = 0.85;
    const roomBeside = root.clientWidth - 356 - 32;
    const over = !!card && !narrow && roomBeside / layout.width < MIN;
    const box = mapBox(svg, layout, 'grid-map', MIN);
    const pane = h('div', { class: 'map-pane' }, gridLegend(narrow));
    if (narrow) {
      pane.appendChild(gridList(layout));
      const det = h('details', { class: 'pg-list-wrap grid-map-wrap', open: GRIDV.showMap }, h('summary', null, icon('chevron', 'icon-xs chev'), 'As a map'), box);
      det.addEventListener('toggle', () => { GRIDV.showMap = det.open; if (det.open) box._fit(); });
      pane.appendChild(det);
    } else pane.appendChild(box);
    root.appendChild(h('div', { class: 'map-layout grid-layout' + (card ? ' has-card' : '') + (over ? ' card-over' : '') }, pane, card, card ? mapScrim('#/grid') : null));
    requestAnimationFrame(() => { box._fit(); box.scrollLeft = sx; });
    const q = (s) => root.querySelector(s);
    if (GRIDV.focusCard && card) { GRIDV.focusCard = false; const t = q('.grid-card .map-card-title'); if (t) t.focus({ preventScroll: true }); }
    else if (GRIDV.focusNode) { const el = q('.map-node[data-node="' + CSS.escape(GRIDV.focusNode) + '"]') || q('.grid-row[data-node="' + CSS.escape(GRIDV.focusNode) + '"]'); GRIDV.focusNode = null; if (el) el.focus({ preventScroll: true }); }
    else if (focused) {
      const el = focused.node ? q('.map-node[data-node="' + CSS.escape(focused.node) + '"]') : focused.row ? q('.grid-row[data-node="' + CSS.escape(focused.row) + '"]') : q('[data-action="' + CSS.escape(focused.action) + '"]');
      if (el) el.focus({ preventScroll: true });
    }
  };

  // ================================================================ Procedures (§25): the booklet
  const PROC = { scrolled: null };
  /** The booklet's sections: GET /api/docs/procedures when the server has it, else the procedures flows.js ships. */
  /** The booklet: the server's procedures (docs.procedures: one section per agent, read from its file)
   *  after the two flow sections built into this page, so the factory line and plans lead with their maps. */
  function bookletData() {
    const builtIn = (k) => ({ id: k, title: F.AGENT_PROCEDURES[k].title, summary: F.AGENT_PROCEDURES[k].summary, procedure: F.AGENT_PROCEDURES[k].procedure, parts: [], notes: [], maps: [], flow: true });
    if (DOCS.data && DOCS.data.sections.length) {
      const d = DOCS.data;
      const ids = new Set(d.sections.map((s) => s.id));
      const rest = d.maps.slice();
      const take = (id) => { const out = []; for (let i = rest.length - 1; i >= 0; i--) if (rest[i].module === id) out.unshift(rest.splice(i, 1)[0]); return out; };
      const flow = (k) => { if (ids.has(k) || !F.AGENT_PROCEDURES[k]) return null; const s = builtIn(k); s.maps = take(k); return s; };
      const sections = [];
      const line = flow('factory-line');
      if (line) sections.push(line);
      for (const s of d.sections) {
        if (s.id === 'lead') { const pl = flow('plans'); if (pl) sections.push(pl); }
        sections.push(s);
      }
      if (!sections.some((s) => s.id === 'plans' || s.id === 'lead')) { const pl = flow('plans'); if (pl) sections.push(pl); }
      return { title: d.title, intro: d.intro, build: d.build, generated: d.generated, sections, maps: rest, server: true };
    }
    const order = ['factory-line', 'spec-writer', 'builder', 'reviewer', 'approver', 'gate', 'human', 'plans', 'lead', 'coder', 'analyst', 'handoff', 'repo-scout'];
    return { title: '', intro: '', build: '', sections: order.filter((k) => F.AGENT_PROCEDURES[k]).map(builtIn), maps: [], server: false };
  }
  function mapFigure(title, graph, cls, description, id) {
    const layout = layoutMap(graph, { rankGap: 44 });
    const svg = drawMap(layout, { label: title, nodeClass: (n) => [graph.highlight && graph.highlight.indexOf(n.id) >= 0 ? 'is-default' : '', n.tone === 'human' ? 'has-human' : ''].filter(Boolean).join(' ') });
    // On a phone a wide map opens on its first node (the spine), with a cue that it scrolls.
    const box = mapBox(svg, layout, 'book-map' + (cls ? ' ' + cls : ''), 0.86, { narrowMin: 0.74, center: graph.nodes[0] && graph.nodes[0].id });
    return h('figure', { class: 'book-fig', dataset: { map: id || '' } }, h('figcaption', null, h('span', { class: 'book-fig-title' }, title), description ? h('span', { class: 'book-fig-desc' }, description) : null,
      id ? extNodes('map', { id, title }) : null), box, box._cue);
  }
  /** "Runs on deepseek-v4-pro (provider deepseek) · tools: Read, Grep, …": the model itself, not the tier alias. */
  function bookMeta(s) {
    const a = s.agent || {};
    if (!a.model && !a.driver && !(a.tools || []).length) return s.meta || '';
    const m = modelOf(s.id);
    const model = m.model && m.model !== '—' ? m.model : a.model;
    const parts = [];
    if (model) parts.push('Runs on ' + model + (m.provider ? ' (provider ' + m.provider + ')' : ''));
    if (a.driver && a.driver !== 'builtin') parts.push('through the ' + a.driver + ' CLI');
    if ((a.tools || []).length) parts.push('tools: ' + a.tools.join(', '));
    return parts.join(' · ');
  }
  /** A module's short working procedure: the booklet's own for the factory's agents, else the numbered steps of the
   *  agent file that have a lead phrase. */
  function shortProcedure(s) {
    if (s.flow) return s.procedure;
    const own = F.AGENT_PROCEDURES[s.id] || (/^reviewer-/.test(s.id) ? F.AGENT_PROCEDURES.reviewer : null);
    if (own && own.procedure) return own.procedure;
    const steps = s.procedure.length ? s.procedure : [].concat.apply([], (s.parts || []).map((pt) => pt.steps));
    return steps.filter((x) => x[0]);
  }
  RENDER.procedures = function () {
    if (S.route.view !== 'procedures') return;
    const root = document.getElementById('procedures-root');
    const main = document.getElementById('main');
    const y = main.scrollTop;
    clear(root);
    if (!F) { root.appendChild(emptyState('alert', 'Procedures did not load', 'flows.js is missing from this server.')); return; }
    if (!DOCS.data && !DOCS.missing && !DOCS.error) { if (!DOCS.loading) loadDocs(); root.appendChild(skeletonLines(12)); return; }
    const data = bookletData();
    const reviewers = pipelineReviewers();
    const maxRes = S.pipeline && S.pipeline.max_resumes !== undefined ? S.pipeline.max_resumes : 2;
    const book = h('article', { class: 'book' });
    const actions = h('div', { class: 'book-actions' });
    if (!DOCS.missing) actions.appendChild(h('a', { class: 'btn btn-sm btn-primary', href: '/api/docs/procedures.html?download=1', download: 'software-factory-procedures.html', dataset: { action: 'download' } }, icon('download', 'icon-sm'), 'Download as HTML'));
    actions.appendChild(h('a', { class: 'btn btn-sm', href: '#/grid' }, icon('route', 'icon-sm'), 'Grid Card'));
    book.appendChild(h('header', { class: 'book-head' },
      h('div', { class: 'wz-eyebrow' }, 'Working procedures and maps'),
      h('h1', { class: 'book-title' }, data.title || 'Software Factory: how each module works'),
      h('p', { class: 'book-intro' }, data.intro || 'Every agent’s working procedure, in the order the factory runs them, followed by its process maps. This is a viewing copy: the agents read their own files, which Agents shows and edits.'),
      data.build || data.generated ? h('p', { class: 'book-build' }, [data.build ? 'Build ' + data.build : '', data.generated ? 'Read from the agent files ' + (relTime(data.generated) || data.generated) : ''].filter(Boolean).join(' · ')) : null,
      DOCS.missing ? h('p', { class: 'book-build' }, 'This server has no procedures endpoint yet: the booklet shows the procedures built into this page.') : null,
      actions));
    book.appendChild(h('nav', { class: 'book-nav', 'aria-label': 'Modules' }, data.sections.map((s) => h('a', { href: '#/procedures/' + enc(s.id), dataset: { sec: s.id } }, s.title))));
    const placed = new Set();
    for (const s of data.sections) {
      // Each module leads with its short numbered procedure and its map; the agent's whole file (the prompt it
      // reads) is one click away, closed, as in Agents.
      const lead = shortProcedure(s);
      const parts = (s.parts || []).filter((pt) => pt.steps.length || pt.text);
      const hasFile = !s.flow && (s.intro || parts.length || (s.procedure.length && lead !== s.procedure) || (s.notes || []).length);
      const meta = bookMeta(s);
      const sec = h('section', { class: 'book-sec', id: 'proc-' + s.id, 'aria-labelledby': 'proc-' + s.id + '-t' },
        h('h2', { class: 'book-h2', id: 'proc-' + s.id + '-t' }, s.title),
        s.summary ? h('p', { class: 'book-sum' }, s.summary) : null,
        meta ? h('p', { class: 'book-meta' }, meta) : null,
        lead.length ? procedureList(lead, 'proc-book') : null);
      for (const m of s.maps || []) { sec.appendChild(mapFigure(m.title, m.graph, '', m.description, m.id)); placed.add(m.id); }
      const builtIn = !data.server || !(data.maps.length || data.sections.some((x) => (x.maps || []).length));
      if (builtIn && s.id === 'factory-line') sec.appendChild(mapFigure('Factory line: from request to output', F.factoryMap(reviewers, maxRes, deliveryDefault()), 'is-line'));
      if (builtIn && (s.id === 'plans' || (s.id === 'lead' && !data.sections.some((x) => x.id === 'plans')))) sec.appendChild(mapFigure('Plans: plan, run, review', F.planMap()));
      if (hasFile) {
        const steps = (list) => (list.every((x) => x[0]) ? procedureList(list, 'proc-book') : MD.render(list.map((x, i) => (i + 1) + '. ' + (x[0] ? '**' + x[0] + '** ' : '') + x[1]).join('\n'), 'md-sm'));
        sec.appendChild(h('details', { class: 'book-file', open: !lead.length },
          h('summary', null, icon('chevron', 'icon-xs chev'), 'The full agent file', h('span', { class: 'book-file-note' }, 'what ' + s.title + ' reads, word for word')),
          s.intro ? h('div', { class: 'book-intro-md' }, MD.render(s.intro, 'md-sm')) : null,
          s.procedure.length && lead !== s.procedure ? steps(s.procedure) : null,
          parts.map((pt) => h('div', { class: 'book-part' },
            pt.title ? h('h3', { class: 'book-h3' }, pt.title) : null,
            pt.steps.length ? steps(pt.steps) : null,
            pt.text ? MD.render(pt.text, 'md-sm') : null)),
          (s.notes || []).map((n) => h('p', { class: 'book-note' }, n))));
      }
      book.appendChild(sec);
    }
    const rest = (data.maps || []).filter((m) => !placed.has(m.id));
    if (rest.length) book.appendChild(h('section', { class: 'book-sec', id: 'proc-maps' }, h('h2', { class: 'book-h2' }, 'Maps'), rest.map((m) => mapFigure(m.title, m.graph, '', m.description, m.id))));
    root.appendChild(book);
    main.scrollTop = y;
    for (const b of root.querySelectorAll('.map-scroll')) requestAnimationFrame(() => b._fit && b._fit());
    if (S.route.section && PROC.scrolled !== S.route.section) {
      PROC.scrolled = S.route.section;
      requestAnimationFrame(() => { const el = document.getElementById('proc-' + S.route.section); if (el) el.scrollIntoView({ block: 'start' }); });
    }
  };

  // ================================================================ view modules (Seraphina OS Harness)
  // A section of the harness lives in its own file, swfactory/web/views/<area>.js, loaded after app.js (index.html,
  // defer, in order). It calls SF.registerView({...}) once: its routes (#/<name>/…?query), its rail entry, its card
  // on Home and its renderer. Everything a view needs from this file is on window.SF (below); nothing else here is
  // reachable from a view file, so a view never depends on app.js internals that another section may change.
  const PRODUCT = 'Seraphina OS Harness';
  const REG = { views: new Map(), routes: new Map(), booted: false };
  const VIEW_ID_RE = /^[a-z][a-z0-9-]{0,31}$/;
  const EVENT_NAME_RE = /^[a-z][a-z0-9_-]*(\.[a-z0-9_-]+)*$/;
  /** First hash segments the core owns: a view module cannot take them. */
  const CORE_ROUTES = new Set(['home', 'factory', 'job', 'plans', 'chat', 'agents', 'settings', 'doctor', 'welcome', 'setup', 'start', 'grid', 'procedures']);
  const TONES = new Set(['ok', 'accent', 'human', 'warn', 'todo', 'err']);
  const safely = (what, fn) => { try { return fn(); } catch (e) { console.error(what + ' failed', e); return undefined; } };

  // ---------------------------------------------------------------- live events for view modules
  const EVENT_SUBS = new Map();
  const RECONNECT_SUBS = new Set();
  /** The SSE event types the stream listens to: the core's, then every type a view module subscribed to. */
  function eventTypes() {
    const out = EVENT_TYPES.slice();
    for (const t of EVENT_SUBS.keys()) if (out.indexOf(t) < 0) out.push(t);
    return out;
  }
  /** SF.on(type | [types], fn(ev)) → unsubscribe. ev is {seq, ts, type, repo, job, agent, data}. Events carry
   *  counts and refs, never the text a person or an agent wrote: fetch the record to show it. */
  function onEvent(types, fn) {
    const list = (Array.isArray(types) ? types : [types]).map(String);
    if (typeof fn !== 'function') throw new Error('SF.on: give a function');
    for (const t of list) {
      if (!EVENT_NAME_RE.test(t)) throw new Error('SF.on: not an event type: ' + t);
      if (!EVENT_SUBS.has(t)) {
        EVENT_SUBS.set(t, new Set());
        if (es && es._onMsg && EVENT_TYPES.indexOf(t) < 0) es.addEventListener(t, es._onMsg);
      }
      EVENT_SUBS.get(t).add(fn);
    }
    return () => { for (const t of list) { const s = EVENT_SUBS.get(t); if (s) s.delete(fn); } };
  }
  function dispatchToViews(ev) {
    const subs = EVENT_SUBS.get(ev.type);
    if (!subs) return;
    for (const fn of Array.from(subs)) safely('event ' + ev.type + ' handler', () => fn(ev));
  }
  /** SF.onReconnect(fn(why)): the stream came back after a gap ('reconnected') or the server restarted
   *  ('restarted', its event numbers began again): reload what the view shows. */
  function onReconnect(fn) { RECONNECT_SUBS.add(fn); return () => RECONNECT_SUBS.delete(fn); }
  function notifyReconnect(why) {
    for (const fn of Array.from(RECONNECT_SUBS)) safely('reconnect handler', () => fn(why));
    if (S.route.view === 'home') loadHome(true);
  }

  // ---------------------------------------------------------------- extension points of the Software Factory views
  // Chips on a job card, items in the job drawer's meta row and blocks under it: SF.extend(kind, fn(job) → Node,
  // [Node] or null). A job card re-renders when its job changes; after data of your own arrived, SF.refreshJobs()
  // (it redraws the open chat too). More kinds, each fn(arg) → Node, [Node] or null:
  //   runHead      {mode: 'job'|'chat', owner, name, agent, round, status, start, end}: chips on a run's header (a job's
  //                Live run, a chat turn's foot); start / end are the agent.start / agent.end data, or null
  //   chatHead     chat: items in a chat's meta row          planHead  plan: items in a plan detail's meta row
  //   chatBanner   chat: blocks under a chat's head (banners, a button that starts another chat)
  //   chatReply    {chat, turn, message, run}: lines under one reply; message is a history entry {role, text, turn,
  //                label?} and run null, or (a turn shown live) message null and run {name, no (the log's NN, not
  //                the transcript's turn), status, started (agent.start seen), text (it wrote any)}
  //   chatStart    {form: 'chat'}: one more start for New chat, returned as {id, title, desc, disabled?, fields() →
  //                [Node], body() → the POST /api/chats body (throw an Error the dialog shows), onError?(e) → nodes
  //                shown after the server's refusal (where to fix it), claims?() → agent names only this start
  //                offers (the No repository start leaves them out)} instead of a Node
  //   form         a form of the Software Factory (formExt below): New feature, New plan, New chat (under every
  //                start), Hand off, Change routing, the Start review and the Approve dialog (form 'approve',
  //                f.job); fields it returns go into the form, f.contribute adds to the body
  //   map          {id, title}: nodes under a process map's title in Procedures (id: the map's, as GET /api/docs/maps)
  //   settings     {section: 'page'|'route'|'model'|'provider', …} (settingsExt below): Settings fields and cards
  //   doctorCheck  a Doctor check {name, ok, level, detail}: a line under it
  const EXT = { jobCard: [], jobMeta: [], jobHead: [], runHead: [], chatHead: [], planHead: [], chatBanner: [], chatReply: [], chatStart: [], form: [], map: [], settings: [], doctorCheck: [] };
  function extend(kind, fn) {
    if (!EXT[kind]) throw new Error('SF.extend: unknown kind ' + kind + ' (use ' + Object.keys(EXT).join(', ') + ')');
    if (typeof fn !== 'function') throw new Error('SF.extend: give a function');
    EXT[kind].push(fn);
    refreshJobs();
    return () => { const i = EXT[kind].indexOf(fn); if (i >= 0) EXT[kind].splice(i, 1); refreshJobs(); };
  }
  function extNodes(kind, arg) {
    const out = [];
    for (const fn of EXT[kind]) {
      const r = safely('SF.extend ' + kind, () => fn(arg));
      for (const n of [].concat(r === undefined || r === null || r === false ? [] : r)) if (n instanceof Node) out.push(n);
    }
    return out;
  }
  /** chatStart's specs (objects, not nodes): those with an id, a title, fields() and body(). */
  function extSpecs(kind, arg) {
    const out = [];
    for (const fn of EXT[kind]) {
      const r = safely('SF.extend ' + kind, () => fn(arg));
      for (const x of [].concat(r || [])) if (isObj(x) && typeof x.id === 'string' && x.title && typeof x.body === 'function') out.push(x);
    }
    return out;
  }
  function refreshJobs() {
    for (const item of BOARD.cards.values()) item._card._sig = null;
    invalidate('board', 'drawerHead', 'chatConv');
  }
  /** A form's view-module fields (SF.extend('form', fn(f))). f: {form: 'feature'|'plan'|'chat'|'handoff'|'routing'|
   *  'start'|'approve', repo(), routing() (undefined, a preset name or per-step routes), job, chat, request (the Start
   *  request), onChange(fn) (the repository or the routing changed), contribute(fn(body, action)), onError(fn(err,
   *  action) → text | null)}. apply(body, action) runs every contribution before the POST (one may throw an Error the
   *  dialog shows); action is the button's value. error(err, action): after the server refused (an ApiError, its body
   *  in err.data), the first plain-words text a module gives for it (it may also redraw its fields, e.g. from a 409's
   *  open_blocking), or null: the server's words then. */
  function formExt(form, o) {
    o = o || {};
    const subs = [];
    const fills = [];
    const errs = [];
    const f = Object.freeze({
      form, job: o.job || null, chat: o.chat || null, request: o.request || null,
      repo: () => (o.repo ? o.repo() : null),
      routing: () => { try { return o.routing ? o.routing() : undefined; } catch (e) { return undefined; } },
      onChange: (fn) => { if (typeof fn === 'function') subs.push(fn); },
      contribute: (fn) => { if (typeof fn === 'function') fills.push(fn); },
      onError: (fn) => { if (typeof fn === 'function') errs.push(fn); },
    });
    const nodes = extNodes('form', f);
    const changed = () => { for (const fn of subs) safely('form ' + form + ' change', () => fn()); };
    for (const el of o.watch || []) if (el) el.addEventListener('change', changed);
    const error = (err, action) => {
      for (const fn of errs) { const t = safely('form ' + form + ' error', () => fn(err, action)); if (typeof t === 'string' && t) return t; }
      return null;
    };
    return { nodes, changed, error, apply(body, action) { for (const fn of fills) fn(body, action); return body; } };
  }
  /** Settings fields and cards of a view module (SF.extend('settings', fn(s))). s: {section, draft (the whole draft
   *  config: edit it in place), orig (as saved), changed(rerender?), invalid(key, msg, input), isInvalid(key),
   *  errSlot(key), card(id, title, desc, body, extra), saveError()} plus, per section: 'route' {name, entry(),
   *  set(key, value), onModel(fn): fn() after the box's model changes} (an agent's box in Settings › Routing),
   *  'model' / 'provider' {name, entry} (a row's draft
   *  entry), 'page' {} (whole cards, after Routing). */
  function settingsExt(section, o) {
    const s = Object.freeze(Object.assign({
      section, draft: CFG.draft, orig: CFG.orig,
      changed: (rerender) => { onDraftChange(); if (rerender) rerenderSettings(); },
      invalid: (key, msg, input) => setInvalid(key, msg, input), isInvalid: (key) => CFG.invalid.has(key), errSlot, card: setCard,
      saveError: () => (CFG.saveError ? { message: CFG.saveError, section: CFG.saveSection } : null),
    }, o || {}));
    return extNodes('settings', s);
  }
  /** SF.registerJobTab({id, label, icon, render(panel, job, {repo, id})}): a tab in the job drawer, before Raw;
   *  its hash is #/job/<repo>/<id>/<tab id>. render runs when the tab opens and on each job.updated; job is null
   *  while the drawer still loads it. Register it at load time (before the first drawer opens). */
  const JOB_TABS = new Map();
  function registerJobTab(spec) {
    if (!spec || !VIEW_ID_RE.test(spec.id || '') || TABS.some((t) => t[0] === spec.id)) throw new Error('SF.registerJobTab: id missing, invalid or taken');
    if (typeof spec.render !== 'function') throw new Error('SF.registerJobTab: give render(panel, job, ref)');
    const at = TABS.findIndex((t) => t[0] === 'raw');
    TABS.splice(at < 0 ? TABS.length : at, 0, [spec.id, String(spec.label || spec.id), spec.icon || 'layers']);
    JOB_TABS.set(spec.id, spec);
  }
  function renderJobTab(id) {
    const spec = JOB_TABS.get(id);
    const panel = D.els && D.els.panels ? D.els.panels[id] : null;
    if (!spec || !panel || !D.key) return;
    const job = currentJob();
    const ref = { repo: D.repo, id: D.id };
    safely('job tab ' + id, () => spec.render(panel, job, ref));
  }

  // ---------------------------------------------------------------- a panel for view modules (SF.drawer)
  // The same right-hand drawer as a job's, modal like it (the app behind is inert, Esc and the scrim close it).
  const GD = { open: false, key: null, opts: null, view: null, opener: null, els: null };
  /** SF.drawer.open({key, title, eyebrow?, sub?, body, actions?, back?, onClose?}) → {el, head, body, setTitle,
   *  setBody, setActions, close}. body and actions are nodes (or arrays of nodes); strings become text. back: the
   *  hash without the panel (#/messages for #/messages/m/M-1): closing goes there, so the URL stays the truth.
   *  Opening the same key again updates the panel in place without moving focus. */
  function openPanel(opts) {
    opts = opts || {};
    const drawer = document.getElementById('drawer');
    if (D.key) hideDrawer();
    const again = GD.open && GD.key === (opts.key || null);
    if (!again) GD.opener = document.activeElement;
    Object.assign(GD, { open: true, key: opts.key || null, opts, view: S.route.view });
    if (!again || !GD.els) {
      clear(drawer);
      const close = h('button', { type: 'button', class: 'btn btn-ghost btn-icon drawer-close', 'aria-label': 'Close panel', title: 'Close (Esc)', dataset: { action: 'close' } }, icon('x'));
      close.addEventListener('click', () => closePanel(true));
      const els = { row: h('div', { class: 'drawer-row' }), title: h('h2', { class: 'drawer-title', id: 'drawer-title', tabindex: '-1' }), sub: h('p', { class: 'drawer-sub' }),
        actions: h('div', { class: 'actions' }), body: h('div', { class: 'panel', role: 'region', 'aria-labelledby': 'drawer-title' }) };
      els.head = h('header', { class: 'drawer-head' }, els.row, els.title, els.sub, els.actions, close);
      const offline = h('div', { class: 'drawer-offline', id: 'drawer-offline', role: 'status', hidden: !S.offline },
        icon('alert', 'icon-sm'), h('span', null, h('strong', null, 'Reconnecting…'), ' What this panel shows may be out of date.'), btn('Retry now', { cls: 'btn-xs', onClick: retryNow }));
      add(drawer, [els.head, offline, h('div', { class: 'drawer-body' }, els.body)]);
      drawer.classList.add('is-panel');
      drawer.classList.toggle('is-wide', !!opts.wide);
      GD.els = els;
    }
    const ctl = panelCtl();
    ctl.setTitle(opts.title || '');
    clear(GD.els.row);
    add(GD.els.row, opts.eyebrow || null);
    GD.els.row.hidden = !GD.els.row.firstChild;
    GD.els.sub.textContent = opts.sub ? String(opts.sub) : '';
    GD.els.sub.hidden = !opts.sub;
    ctl.setActions(opts.actions || null);
    ctl.setBody(opts.body || null);
    if (!again) {
      drawer.hidden = false;
      document.getElementById('scrim').hidden = false;
      setDrawerModal(true);
      setTimeout(() => { if (GD.els) GD.els.title.focus({ preventScroll: true }); }, 0);
    }
    return ctl;
  }
  function panelCtl() {
    return {
      get el() { return document.getElementById('drawer'); },
      get head() { return GD.els ? GD.els.head : null; },
      get body() { return GD.els ? GD.els.body : null; },
      setTitle(t) { if (GD.els) GD.els.title.textContent = String(t || ''); },
      setBody(nodes) { if (GD.els) { clear(GD.els.body); add(GD.els.body, nodes); } },
      setActions(nodes) { if (GD.els) { clear(GD.els.actions); add(GD.els.actions, nodes); GD.els.actions.hidden = !GD.els.actions.firstChild; } },
      close() { closePanel(false); },
    };
  }
  /** user: the person closed it (Esc, ×, the scrim): go to its back hash when it has one (the route closes it). */
  function closePanel(user) {
    if (!GD.open) return;
    const opts = GD.opts || {};
    if (user && opts.back && location.hash !== opts.back) { location.hash = opts.back; return; }
    const opener = GD.opener;
    Object.assign(GD, { open: false, key: null, opts: null, view: null, opener: null, els: null });
    const drawer = document.getElementById('drawer');
    drawer.hidden = true;
    clear(drawer);
    drawer.classList.remove('is-panel', 'is-wide');
    document.getElementById('scrim').hidden = true;
    setDrawerModal(false);
    const target = refocusTarget(opener);
    if (target && typeof target.focus === 'function') target.focus({ preventScroll: true });
    if (opts.onClose) safely('panel onClose', () => opts.onClose(user));
  }

  // ---------------------------------------------------------------- registering a view
  /** SF.registerView(spec) → a handle. spec:
   *   id       'tracker' (a-z, 0-9, -): the section #view-<id> and its root #<id>-root are made for it
   *   title    'Housekeeping Tracker': the page title and the section's accessible name
   *   routes   ['housekeeping'] (default [id]): the first hash segments it owns; #/housekeeping/<a>/<b>?x=1 routes
   *            to it as {view: id, ext: true, name: 'housekeeping', parts: ['a', 'b'], query: {x: '1'}}
   *   nav      {label, icon, order, href?, phone?, hidden?} or null: its rail entry (order: Home 0, Factory 10,
   *            Plans 20, Tracker 25, Chat 30, Messages 40, Operator 70, Personas 75, Agents 80, Settings 90, Grid
   *            100, Procedures 110, Doctor 120); phone: also in the phone's bottom bar (it holds six: Home, Factory,
   *            Plans, Chat, Messages, Settings), else the phone reaches it from Home; hidden: until handle.setNav
   *            (the Operator's, until an operator is marked or could be); null: no entry (Placement)
   *   home     {title?, text, icon?, order, href?, routes?: [{to, label}], load?: async () => status,
   *            events?: [types], card?: false} or null: its card on Home. status is {tone: ok|accent|human|warn|
   *            todo|null, text, detail?, hidden?, needs?: [{key, text, sub?, href, tone?}]}; hidden: true leaves the
   *            card and its needs out (the operator's while nobody is marked and nothing could make one); needs feed
   *            Home's "Needs you" strip, every section's merged, most urgent first. card: false feeds "Needs you"
   *            without a card of its own (Placement: a section of the Software Factory)
   *   wide     true for a full-width page (no 1400 px cap)
   *   start(handle)              once, after the page booted (a badge count, whether the nav entry shows)
   *   enter(route, prev, handle) each time the route comes to this view or moves within it (prev: the previous
   *                              route of this view, or null when it was another view): load data here
   *   render(root, route, handle) draw into root; runs after enter and after handle.refresh() (batched per frame)
   *   leave(next, handle)        the route left this view
   *   events   {type: fn(ev, handle)}: SSE subscriptions for as long as the page lives */
  function registerView(spec) {
    if (!spec || !VIEW_ID_RE.test(spec.id || '')) throw new Error('SF.registerView: id must match ' + VIEW_ID_RE);
    const id = spec.id;
    if (REG.views.has(id) || VIEWS.indexOf(id) >= 0) throw new Error('SF.registerView: a view called ' + id + ' exists');
    if (typeof spec.render !== 'function') throw new Error('SF.registerView: ' + id + ' needs render(root, route, handle)');
    const routes = (Array.isArray(spec.routes) && spec.routes.length ? spec.routes : [id]).map(String);
    for (const r of routes) if (!VIEW_ID_RE.test(r) || CORE_ROUTES.has(r) || REG.routes.has(r)) throw new Error('SF.registerView: route #/' + r + ' is invalid or taken');
    const title = String(spec.title || id);
    const root = h('div', { class: 'view-pad' + (spec.wide ? ' view-pad-wide' : ''), id: id + '-root' });
    const section = h('section', { class: 'view view-ext', id: 'view-' + id, 'aria-label': title, hidden: true, dataset: { module: id } }, root);
    document.getElementById('main').appendChild(section);
    const v = { id, title, spec, routes, root, section, nav: spec.nav || null, home: spec.home || null, navEl: null, navHidden: !!(spec.nav && spec.nav.hidden),
      homeStatus: null, homeLoading: null, homeAt: 0, homeStale: true };
    v.handle = Object.freeze({
      id, root, title,
      /** Re-render (batched) if the view is showing. */
      refresh() { invalidate('view:' + id); },
      isShown() { return S.route.view === id; },
      route() { return S.route.view === id ? S.route : null; },
      /** The rail entry's count (0 or null hides it), with its tooltip. */
      badge(n, label) { v.badge = { n: Number(n) || 0, label: label || '' }; paintBadge(v); if (S.route.view === 'grid') invalidate('grid'); },
      /** Show or hide the rail entry (the operator's only shows while an operator is configured). */
      setNav(show) { v.navHidden = !show; renderRail(); },
      /** Run home.load again (now if Home shows, else when Home next opens). */
      refreshHome() { v.homeStale = true; if (S.route.view === 'home') loadHomeCard(v, true); },
    });
    REG.views.set(id, v);
    for (const r of routes) REG.routes.set(r, v);
    RENDER['view:' + id] = () => {
      if (S.route.view !== id) return;
      try { spec.render(root, S.route, v.handle); }
      catch (e) {
        console.error('view ' + id + ' failed', e);
        clear(root);
        root.appendChild(emptyState('alert', title + ' could not be shown', e && e.message ? e.message : String(e)));
      }
    };
    if (spec.events) for (const t of Object.keys(spec.events)) onEvent(t, (ev) => spec.events[t](ev, v.handle));
    if (v.home && Array.isArray(v.home.events) && v.home.events.length) onEvent(v.home.events, () => { v.homeStale = true; if (S.route.view === 'home') scheduleHomeCard(v); });
    if (REG.booted) {
      renderRail();
      if (spec.start) safely('view ' + id + ' start', () => spec.start(v.handle));
      route();
    }
    return v.handle;
  }
  function routeModules(r, prev) {
    const was = prev && prev.ext ? REG.views.get(prev.view) : null;
    const now = r.ext ? REG.views.get(r.view) : null;
    if (was && was !== now && was.spec.leave) safely('view ' + was.id + ' leave', () => was.spec.leave(r, was.handle));
    if (now) {
      if (now.spec.enter) safely('view ' + now.id + ' enter', () => now.spec.enter(r, was === now ? prev : null, now.handle));
      invalidate('view:' + now.id);
    }
  }
  /** The rail: the static entries of index.html (data-order) with each view module's entry in its place. */
  function renderRail() {
    const rail = document.querySelector('.rail');
    if (!rail) return;
    for (const v of REG.views.values()) {
      if (!v.nav) continue;
      if (!v.navEl) {
        const n = v.nav;
        v.navEl = h('a', { class: 'rail-item' + (n.phone ? '' : ' rail-extra'), href: n.href || '#/' + v.routes[0], dataset: { view: v.id, order: String(Number(n.order) || 50) } },
          icon(n.icon || 'layers'), h('span', null, String(n.label || v.title)), h('span', { class: 'rail-badge', hidden: true }));
      }
      v.navEl.hidden = v.navHidden;
      if (!v.navEl.isConnected) {
        const order = Number(v.navEl.dataset.order);
        const after = Array.from(rail.querySelectorAll('.rail-item')).find((a) => a !== v.navEl && Number(a.dataset.order) > order);
        rail.insertBefore(v.navEl, after || null);
      }
      paintBadge(v);
    }
    for (const a of $$('.rail-item')) {
      if (a.dataset.view === S.route.view) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
    }
  }
  function paintBadge(v) {
    const b = v.navEl ? v.navEl.querySelector('.rail-badge') : null;
    if (!b) return;
    const n = v.badge ? v.badge.n : 0;
    b.textContent = n > 99 ? '99+' : String(n);
    b.hidden = !n;
    b.title = n && v.badge.label ? v.badge.label : '';
    if (n && v.badge.label) v.navEl.setAttribute('aria-label', String(v.nav.label || v.title) + ': ' + v.badge.label);
    else v.navEl.removeAttribute('aria-label');
  }

  /** Replace root's children with nodes, keeping focus on the element with the same data-focus (or id). */
  function rebuild(root, nodes) {
    const act = document.activeElement;
    let sel = null;
    if (act && act !== root && root.contains(act)) {
      if (act.dataset && act.dataset.focus) sel = '[data-focus="' + CSS.escape(act.dataset.focus) + '"]';
      else if (act.id) sel = '#' + CSS.escape(act.id);
    }
    clear(root);
    add(root, nodes);
    if (sel) { const el = root.querySelector(sel); if (el && typeof el.focus === 'function') el.focus({ preventScroll: true }); }
    return root;
  }
  /** "This server does not offer …" for a 404/405/501 (an older backend), else the error itself. */
  function unavailable(e, what) {
    if (gone(e)) return emptyState('info', what + ' is not on this server yet', 'The server answered ' + e.status + ': update the harness to get it.', null, true);
    return callout('err', 'alert', 'Could not load ' + what + ':', e && e.message ? e.message : String(e));
  }
  /** A view module's screen that is not built yet: its title, one sentence and where its design is. */
  function placeholder(o) {
    return h('div', { class: 'sf-placeholder' },
      viewHead(o.title, o.text || null, o.actions || null),
      emptyState(o.icon || 'layers', o.empty || 'This screen is being built', o.note || 'Its routes and API are ready; the screen comes with the next build.', null, false),
      o.doc ? h('p', { class: 'home-doc' }, icon('book', 'icon-xs'), 'Interface notes: ', h('code', null, o.doc)) : null);
  }
  const NARROW_MQ = window.matchMedia ? window.matchMedia('(max-width: 720px)') : null;
  /** The phone layout (≤ 720 px; the smallest target is 390 px): one column, no sideways scroll of the page. */
  const isNarrow = () => !!(NARROW_MQ && NARROW_MQ.matches);

  // ================================================================ Home (Seraphina OS Harness)
  // The default route. A Grid Card of the harness's sections: one card per section with one plain sentence, its live
  // status and an Open action, the routes between them labelled; the v2 sections as "Planned" cards; and above them
  // "Needs you", everything that waits for the owner, merged from every section (DESIGN §31, the v2 design §2.1).
  const DESIGN_DOC = 'docs/seraphina/SERAPHINA_OS_HARNESS.md';
  const HOME_PLANNED = [
    { id: 'maintenance', title: 'System Maintenance', icon: 'tool', anchor: '§2.2 System Maintenance',
      text: 'Says in one sentence how your server is, explains what the checks found and fixes it with a way back.', routes: [{ to: 'tracker', label: 'fixes, incidents' }] },
    { id: 'upgrades', title: 'Upgrades & Enhancements', icon: 'refresh', anchor: '§2.2 Upgrades & Enhancements',
      text: 'Shows each app’s updates with what changes for your setup, and runs them in a window you approve.', routes: [{ to: 'tracker', label: 'post-upgrade checks' }] },
    { id: 'research', title: 'Systems Research', icon: 'search', anchor: '§2.2 Systems Research',
      text: 'Answers questions about your server with sources, then turns the answer you pick into a plan.', routes: [{ to: 'upgrades', label: 'proposals' }] },
    { id: 'audit', title: 'Systems Audit', icon: 'eye', anchor: '§2.2 Systems Audit',
      text: 'A checkup of exposure, backups, drift, updates, storage and privileges, each check passing or a finding.', routes: [{ to: 'tracker', label: 'findings' }] },
    { id: 'model-factory', title: 'Model Factory', icon: 'gauge', anchor: '§2.2 Model Factory',
      text: 'Finds or makes a model that fits your GPUs, measures it, and offers to route agents to it.', routes: [{ to: 'factory', label: 'a model that fits' }] },
  ];
  const HOME = { showAll: false, loadedAt: 0 };
  /** The Software Factory's own card: the line, plans and chats from /api/state. */
  function factoryHomeStatus() {
    if (!S.loaded) return { tone: null, text: S.stateError ? 'cannot load' : 'checking…', detail: S.stateError || '' };
    const jobs = visibleJobs();
    const active = jobs.filter((j) => ACTIVE.has(j.stage)).length;
    const human = jobs.filter((j) => j.stage === 'needs-human').length;
    const plans = visiblePlans();
    const chats = Array.from(S.chats.values()).filter((c) => !S.filterRepo || c.repo === S.filterRepo);
    const detail = [plural(S.repos.length, 'repository', 'repositories'), plural(plans.length, 'plan'), plural(chats.length, 'chat')].join(' · ');
    if (!S.repos.length) return { tone: freshInstall() ? 'todo' : 'warn', text: 'no repository yet', detail: 'Add a repository in setup to start.' };
    if (human) return { tone: 'human', text: plural(human, 'job') + ' waiting for you' + (active ? ' · ' + active + ' running' : ''), detail };
    if (active) return { tone: 'accent', text: plural(active, 'job') + ' running', detail };
    return { tone: 'ok', text: 'idle, ready for work', detail };
  }
  function factoryNeeds() {
    const out = [];
    for (const j of visibleJobs().filter((x) => x.stage === 'needs-human').sort((a, b) => String(b.updated_at || '').localeCompare(String(a.updated_at || '')))) {
      out.push({ key: 'job:' + jobKey(j), tone: 'human', icon: 'user', text: featureTitle(j) || j.id,
        sub: 'Job ' + j.id + ' stopped for your decision' + (j.reason ? ': ' + firstLine(j.reason) : ''), href: '#/job/' + enc(j.repo) + '/' + enc(j.id) });
    }
    for (const p of visiblePlans().filter((x) => x.stage === 'needs-human' || x.stage === 'blocked')) {
      out.push({ key: 'plan:' + p.repo + '/' + p.id, tone: 'human', icon: 'plan', text: firstLine(p.request) || p.id,
        sub: 'Plan ' + p.id + (p.stage === 'blocked' ? ' is blocked' : ' waits for your answer') + (p.reason ? ': ' + firstLine(p.reason) : ''), href: '#/plans/' + enc(p.repo) + '/' + enc(p.id) });
    }
    return out;
  }
  function homeCardsLive() {
    const cards = [{ id: 'factory', title: 'Software Factory', icon: 'factory', order: 10, href: '#/factory', open: 'Open the board',
      text: 'Describe a change: agents write the spec, build it, review it and hand it to you or merge it.',
      routes: [{ to: 'tracker', label: 'acceptance criteria, findings' }], status: factoryHomeStatus(), needs: factoryNeeds(), core: true }];
    for (const v of REG.views.values()) {
      if (!v.home) continue;
      const st = v.homeStatus;
      if (st && st.hidden) continue;
      cards.push({ id: v.id, title: v.home.title || v.title, icon: v.home.icon || (v.nav && v.nav.icon) || 'layers', order: Number(v.home.order) || 50,
        href: v.home.href || (v.nav && v.nav.href) || '#/' + v.routes[0], text: v.home.text || '', routes: v.home.routes || [], open: v.home.open || null,
        status: st || { tone: null, text: v.home.load ? (v.homeLoading ? 'checking…' : '') : '', detail: '' }, needs: st && Array.isArray(st.needs) ? st.needs : [],
        card: v.home.card !== false });
    }
    return cards.sort((a, b) => a.order - b.order);
  }
  function homeTitleOf(id) {
    if (id === 'factory') return 'Software Factory';
    const v = REG.views.get(id);
    if (v) return (v.home && v.home.title) || v.title;
    const p = HOME_PLANNED.find((x) => x.id === id);
    return p ? p.title : id;
  }
  function loadHomeCard(v, force) {
    if (!v.home || typeof v.home.load !== 'function') return null;
    if (v.homeLoading) return v.homeLoading;
    if (!force && !v.homeStale && Date.now() - v.homeAt < 15000) return null;
    v.homeLoading = Promise.resolve().then(() => v.home.load(v.handle))
      .then((st) => { v.homeStatus = isObj(st) ? st : null; })
      .catch((e) => {
        v.homeStatus = gone(e) ? { tone: null, text: 'not on this server yet', detail: 'The server answered ' + e.status + '.' }
          : e && e.status === 401 ? v.homeStatus : { tone: 'warn', text: 'could not load', detail: e && e.message ? e.message : String(e) };
      })
      .finally(() => { v.homeLoading = null; v.homeAt = Date.now(); v.homeStale = false; invalidate('home'); });
    invalidate('home');
    return v.homeLoading;
  }
  const homeTimers = new Map();
  function scheduleHomeCard(v) {
    if (homeTimers.has(v.id)) return;
    homeTimers.set(v.id, setTimeout(() => { homeTimers.delete(v.id); loadHomeCard(v, true); }, 1200));
  }
  function loadHome(force) {
    for (const v of REG.views.values()) loadHomeCard(v, force);
    if (!S.doctor && !S.doctorLoading) loadDoctor(false);
  }
  const NEED_RANK = { err: 0, human: 1, warn: 2, accent: 3, todo: 4, ok: 5 };
  function needsRow(n) {
    const tone = TONES.has(n.tone) ? n.tone : 'human';
    return h('li', null, h('a', { class: 'needs-item', href: n.href || '#/home', dataset: { focus: 'need:' + n.key, need: n.key, tone } },
      icon(n.icon || (tone === 'err' || tone === 'warn' ? 'alert' : 'user'), 'icon-sm needs-icon'),
      h('span', { class: 'needs-text' }, String(n.text || '')),
      h('span', { class: 'needs-where' }, n.where || ''),
      icon('chevron', 'icon-xs needs-go'),
      n.sub ? h('span', { class: 'needs-sub' }, String(n.sub)) : null));
  }
  function homeNeeds(cards) {
    const all = [];
    for (const c of cards) for (const n of c.needs || []) if (isObj(n) && n.text) all.push(Object.assign({ key: c.id + ':' + all.length }, n, { where: c.title }));
    all.sort((a, b) => (NEED_RANK[a.tone] !== undefined ? NEED_RANK[a.tone] : 1) - (NEED_RANK[b.tone] !== undefined ? NEED_RANK[b.tone] : 1));
    const loading = Array.from(REG.views.values()).some((v) => v.homeLoading && !v.homeStatus);
    const sec = h('section', { class: 'needs' + (all.length ? ' has-items' : ''), 'aria-labelledby': 'needs-t', dataset: { count: String(all.length) } });
    sec.appendChild(h('div', { class: 'needs-head' }, icon(all.length ? 'user' : 'check-circle', 'icon-sm'),
      h('h2', { class: 'needs-title', id: 'needs-t' }, 'Needs you'), h('span', { class: 'count-pill' + (all.length ? ' has' : '') }, String(all.length)),
      h('span', { class: 'spacer' }), loading ? h('span', { class: 'needs-note' }, 'checking…') : null));
    if (!all.length) {
      sec.appendChild(h('p', { class: 'needs-empty' }, loading ? 'Checking every section…' : 'Nothing waits for you right now. Decisions, product questions and messages for you appear here first.'));
      return sec;
    }
    const MAX = 4;
    const shown = HOME.showAll ? all : all.slice(0, MAX);
    sec.appendChild(h('ul', { class: 'needs-list' }, shown.map(needsRow)));
    if (all.length > MAX) {
      const more = btn(HOME.showAll ? 'Show the ' + MAX + ' most important' : 'Show all ' + all.length, { cls: 'btn-ghost btn-sm needs-more', dataset: { focus: 'needs-more', action: 'needs-more' } });
      more.setAttribute('aria-expanded', HOME.showAll ? 'true' : 'false');
      more.addEventListener('click', () => { HOME.showAll = !HOME.showAll; invalidate('home'); });
      sec.appendChild(h('div', { class: 'needs-foot' }, h('span', { class: 'needs-note' }, HOME.showAll ? 'All ' + all.length + ', most urgent first.' : 'The ' + MAX + ' most important of ' + all.length + '.'), more));
    }
    return sec;
  }
  function homeRoutes(routes) {
    const list = (routes || []).filter((r) => isObj(r) && r.to && r.label);
    if (!list.length) return null;
    return h('p', { class: 'home-card-routes' }, list.map((r, i) => [i ? ' · ' : null, h('span', { class: 'home-route-label' }, String(r.label)), ' → ', h('span', { class: 'home-route-to' }, homeTitleOf(r.to))]));
  }
  function homeCard(c) {
    const st = c.status || {};
    const tone = TONES.has(st.tone) ? st.tone : null;
    const tid = 'home-' + c.id + '-t';
    const actions = [h('a', { class: 'btn btn-sm btn-primary', href: c.href, dataset: { action: 'open', focus: 'open:' + c.id }, 'aria-label': (c.open || 'Open') + ': ' + c.title }, icon('arrow-right', 'icon-sm'), c.open || 'Open')];
    if (c.core) {
      actions.push(h('nav', { class: 'home-sub-links', 'aria-label': 'Software Factory' },
        [['#/plans', 'Plans'], ['#/chat', 'Chat'], ['#/agents', 'Agents'], ['#/grid', 'Grid Card'], ['#/procedures', 'Procedures']].map(([href, label]) => h('a', { href, dataset: { focus: 'sub:' + href } }, label))));
    }
    return h('article', { class: 'home-card' + (tone ? ' has-' + tone : ''), 'aria-labelledby': tid, dataset: { section: c.id } },
      h('div', { class: 'home-card-head' }, h('span', { class: 'home-card-icon' }, icon(c.icon)), h('h2', { class: 'home-card-title', id: tid }, c.title)),
      h('p', { class: 'home-card-status' }, h('span', { class: 'map-dot-sm tone-' + (tone === 'err' ? 'warn' : tone || 'none') }), h('span', { class: 'home-status-text' }, st.text ? String(st.text) : '')),
      h('p', { class: 'home-card-text' }, c.text),
      st.detail ? h('p', { class: 'home-card-detail' }, String(st.detail)) : null,
      homeRoutes(c.routes),
      h('div', { class: 'home-card-actions' }, actions));
  }
  /** A path that wraps after its slashes, never inside a name (the text is unchanged: <wbr> adds none). */
  function pathBreaks(path) {
    const parts = String(path).split('/');
    return parts.map((part, i) => (i < parts.length - 1 ? [part + '/', h('wbr')] : part));
  }
  function plannedCard(p) {
    const tid = 'home-' + p.id + '-t';
    const copy = h('button', { type: 'button', class: 'btn btn-ghost btn-icon btn-xs', 'aria-label': 'Copy the design document’s path', title: 'Copy the path', dataset: { focus: 'doc:' + p.id } }, icon('copy', 'icon-xs'));
    copy.addEventListener('click', () => copyText(DESIGN_DOC, 'Path copied: ' + DESIGN_DOC));
    return h('article', { class: 'home-card is-planned', 'aria-labelledby': tid, dataset: { section: p.id, planned: '1' } },
      h('div', { class: 'home-card-head' }, h('span', { class: 'home-card-icon' }, icon(p.icon)), h('h2', { class: 'home-card-title', id: tid }, p.title), h('span', { class: 'tag tag-outline' }, 'Planned')),
      h('p', { class: 'home-card-text' }, p.text),
      homeRoutes(p.routes),
      h('div', { class: 'home-doc is-planned-doc' }, h('span', { class: 'home-doc-what' }, icon('book', 'icon-xs'), 'Design: ' + p.anchor), h('span', { class: 'home-doc-path' }, h('code', null, pathBreaks(DESIGN_DOC)), copy)));
  }
  function homeHealth(needCount) {
    const level = doctorLevel();
    const fresh = freshInstall();
    const parts = [];
    let tone = 'ok';
    if (fresh) { parts.push('This harness is not set up yet.'); tone = 'todo'; }
    else if (level === 'error' || level === 'warn') {
      const bad = S.doctor.checks.filter((c) => !c.ok && (c.level || 'error') !== 'info').length;
      parts.push('Doctor found ' + plural(bad, 'thing') + ' to look at.');
      tone = level === 'error' ? 'warn' : 'todo';
    } else if (level === 'ok') parts.push('Your harness is healthy.');
    else parts.push('Checking your harness…');
    parts.push(needCount ? plural(needCount, 'thing') + (needCount === 1 ? ' needs' : ' need') + ' you.' : 'Nothing needs you.');
    const link = fresh ? h('a', { href: '#/welcome', dataset: { focus: 'health-link' } }, 'Start with Welcome')
      : level === 'error' || level === 'warn' ? h('a', { href: '#/doctor', dataset: { focus: 'health-link' } }, 'Open Doctor') : null;
    return h('p', { class: 'home-health' }, h('span', { class: 'map-dot-sm tone-' + tone }), h('span', null, parts.join(' ')), link);
  }
  RENDER.home = function () {
    if (S.route.view !== 'home') return;
    const root = document.getElementById('home-root');
    const cards = homeCardsLive();
    const needs = homeNeeds(cards);
    const needCount = Number(needs.dataset.count) || 0;
    const nodes = [
      h('div', { class: 'view-head home-head' },
        h('div', null, h('div', { class: 'wz-eyebrow' }, PRODUCT), h('h1', { class: 'view-title', id: 'home-title', tabindex: '-1' }, 'Home'),
          h('p', { class: 'view-sub' }, 'Your harness on one card: what needs you first, then every section with how it is doing now.')),
        h('span', { class: 'spacer' }),
        h('div', { class: 'home-head-actions' }, h('a', { class: 'btn btn-sm', href: '#/welcome', dataset: { focus: 'welcome' } }, icon('help', 'icon-sm'), 'Welcome'),
          h('a', { class: 'btn btn-sm', href: '#/doctor', dataset: { focus: 'doctor' } }, icon('doctor', 'icon-sm'), 'Doctor'))),
      homeHealth(needCount),
      freshInstall() ? setupChips() : null,
      needs,
      h('h2', { class: 'home-group-title', id: 'home-sections-t' }, 'Sections'),
      h('div', { class: 'home-grid', role: 'list', 'aria-labelledby': 'home-sections-t' }, cards.filter((c) => c.card !== false).map((c) => h('div', { class: 'home-cell', role: 'listitem' }, homeCard(c)))),
      h('h2', { class: 'home-group-title', id: 'home-planned-t' }, 'Planned for v2'),
      h('p', { class: 'home-group-sub' }, 'Designed, not built yet: each card says what the section will do and where its design is written down.'),
      h('div', { class: 'home-grid is-planned', role: 'list', 'aria-labelledby': 'home-planned-t' }, HOME_PLANNED.map((p) => h('div', { class: 'home-cell', role: 'listitem' }, plannedCard(p)))),
      h('section', { class: 'wz-proc-card home-proc', 'aria-labelledby': 'home-proc-t' }, h('h2', { class: 'wz-proc-title', id: 'home-proc-t' }, 'How Home works'),
        procedureList([
          ['Start with Needs you.', 'Everything that waits for your decision is listed first, most urgent at the top, and each line opens the place that owns it.'],
          ['Open a section to work in it.', 'Each card says in one sentence what the section does and how it is doing now; the arrows name what one section hands to another.'],
          ['Planned sections come later.', 'Their cards say what they will do and where the design is written down; nothing there runs yet.'],
        ])),
    ];
    rebuild(root, nodes);
  };

  // ---------------------------------------------------------------- one vocabulary for every section
  // The words a status has on every page, whichever section shows it: the tracker's own (items.WORDS) for an item,
  // the drift checks' (driftsources.STATES) and GPU custody's (custody.CustodyClient's history and leases). A view
  // module reads them from SF.words instead of keeping a copy.
  const WORDS = Object.freeze({
    /** items.WORDS: an item's status. */
    items: Object.freeze({ proposed: 'Suggested', open: 'To do', doing: 'Working on it', done: 'Waiting for a check', verified: 'Proven', deferred: 'Later', dropped: 'Dropped' }),
    /** driftsources.STATES: a drift source's last check. Only "Checked" (ok) ever says anything about drift. */
    drift: Object.freeze({ ok: 'Checked', cannot_read: 'Cannot read', busy: 'Check in progress', unreachable: 'Cannot reach it', stale: 'Observation too old', error: 'Error', never: 'Not checked yet' }),
    driftTone: Object.freeze({ ok: 'ok', cannot_read: 'warn', busy: 'accent', unreachable: 'warn', stale: 'warn', error: 'err', never: 'muted' }),
    /** custody.Lease.state: a GPU lease. */
    lease: Object.freeze({ held: 'Held', released: 'Given back', lost: 'Lost', unknown: 'Outcome unknown', unreleased: 'Not given back' }),
    /** outcomes.STATES: what an outside call did (DESIGN §40), in words, never as a bare colour. */
    outcome: Object.freeze({ succeeded: 'Succeeded', failed: 'Failed', unknown: 'Outcome unknown: it may or may not have happened' }),
    outcomeTone: Object.freeze({ succeeded: 'ok', failed: 'err', unknown: 'warn' }),
    /** outcomes.UNKNOWN_TITLE: the start of a tracker item's title for an outcome a person must check. */
    unknownTitle: 'Outcome unknown: check before retrying',
    /** receipts.OUTCOMES: a receipt's state. Never green: the harness never re-checks her checks; an unknown outcome
     *  is amber and a reported failure red. */
    receiptTone: Object.freeze({ nothing_verified: 'warn', partly_verified: 'warn', reported_verified: 'neutral', outcome_unknown: 'warn', reported_failed: 'err' }),
    /** The operator persona in one sentence (Settings, Personas and the Operator page). */
    operatorHelp: 'Acts on the server with her own tools, outside the harness. The harness records what she reports; it does not approve or block her actions.',
  });
  /** A custody reason code in plain words: the register's own (codes: GET /api/custody reason_codes, custody.REFUSAL_WORDS),
   *  else the code as it came. FULL "the card cannot fit this model now", HELD "the card is held by someone else". */
  function custodyReason(code, codes) {
    const c = String(code || '');
    return (isObj(codes) && typeof codes[c] === 'string' && codes[c]) || CUSTODY_FALLBACK[c] || c;
  }
  const CUSTODY_FALLBACK = Object.freeze({ HELD: 'the card is held by someone else', FULL: 'the card cannot fit this model now' });
  const secsWords = (n) => (isFinite(Number(n)) ? String(Math.round(Number(n) * 10) / 10) + 's' : 'a while');
  /** One line of a custody register's history or journal (CustodyClient._note: claim, renew, release, call, epoch,
   *  residency, wait; the journal's "requested") in plain words: a refused claim's reason in words, a HELD wait and how
   *  it ended, and a renew or release "not sent" to a register that restarted. codes: GET /api/custody reason_codes. */
  function custodyAnswer(x, codes) {
    const op = String((x && x.op) || '');
    const o = String((x && x.outcome) || '');
    const reason = String((x && x.reason) || '');
    const notSent = /^not sent: /.test(reason);
    if (o === 'requested') return 'Asked, with no answer recorded: outcome unknown';
    if (op === 'claim') {
      if (o === 'denied') return 'Lease denied' + (x.words ? ': ' + x.words : reason ? ': ' + custodyReason(reason, codes) : '');
      return { granted: 'Lease granted', unknown: 'Outcome unknown: the request was not sent again', stale: 'Stale answer refused' }[o] || 'Lease ' + o;
    }
    if (op === 'wait') {
      const s2 = secsWords(x.seconds);
      return { waiting: 'Waits up to ' + s2 + ': the card is held by someone else', granted: 'The hold ended after ' + s2 + ': lease granted',
        gave_up: 'Gave up after ' + s2 + ': the card is still held', cancelled: 'Stopped waiting: the run was cancelled',
        ended: 'Stopped waiting: ' + (reason ? custodyReason(reason, codes) : 'another answer') }[o] || 'Wait ' + o;
    }
    if (op === 'release') {
      if (o === 'lost' && notSent) return 'Not released: the register restarted, so it no longer knows this lease';
      return { released: 'Lease given back', unreleased: 'Could not be given back', unknown: 'Give-back outcome unknown', lost: 'Lost: not given back' }[o] || 'Release ' + o;
    }
    if (op === 'renew') {
      if (o === 'lost' && notSent) return 'Not renewed: the register restarted, so it no longer knows this lease';
      return o === 'held' ? 'Renewal failed: held until it expires' : o === 'lost' ? 'Renewal refused: lease lost' : o === 'unknown' ? 'Renewal outcome unknown' : 'Renewal ' + o;
    }
    if (op === 'residency') return o === 'stale' ? 'Stale residency refused' : o === 'unread' ? 'Residency could not be read' : 'Residency ' + o;
    if (op === 'epoch') return 'The register restarted';
    if (op === 'call') return 'A call under a lease was cut off: outcome unknown';
    return (op + ' ' + o).trim();
  }
  /** Whether a custody answer is a plain grant or give-back (anything else is amber: never red or green, the register
   *  is the operator's and the harness only asked). */
  const custodyPlain = (x) => !!x && ((x.op === 'claim' && x.outcome === 'granted') || (x.op === 'release' && x.outcome === 'released') || (x.op === 'wait' && x.outcome === 'granted'));
  /** Text from the server as a sentence of the page: a full stop added when it has none, after a closing bracket too
   *  ("(not listed: kate)."); it stays the server's words. */
  function sentence(text) {
    const s = String(text === null || text === undefined ? '' : text).trim();
    return s && !/[.!?…]$/.test(s) ? s + '.' : s;
  }
  /** SF.announce(text): the page's one polite live region, outside every view's root (a root is rebuilt on each
   *  render, which would announce it all again): a count, a pairing's progress, a new banner. Said once per call. */
  const LIVE = { el: null, timer: null };
  function announce(text) {
    if (!LIVE.el) {
      LIVE.el = h('div', { class: 'sr-only', id: 'sf-announce', role: 'status', 'aria-live': 'polite', dataset: { live: 'app' } });
      document.body.appendChild(LIVE.el);
    }
    const s = String(text === null || text === undefined ? '' : text);
    LIVE.el.textContent = '';
    clearTimeout(LIVE.timer);
    LIVE.timer = setTimeout(() => { LIVE.el.textContent = s; }, 60);
  }
  /** An item on the Housekeeping Tracker: its page, and a link to it that shows its ref (job:repo/id/I-3, system:S-2). */
  const itemHref = (ref) => '#/housekeeping/' + enc(ref);
  function itemLink(ref, cls) {
    return h('a', { class: 'item-link' + (cls ? ' ' + cls : ''), href: itemHref(ref), title: 'Open it on the Housekeeping Tracker', dataset: { item: String(ref) } },
      icon('checklist', 'icon-xs'), String(ref));
  }

  // ---------------------------------------------------------------- changes before import: DESIGN §15 and §33
  /** "Changes before import" on a profile card page (Personas) and a wake persona's page (Messages & Wakes), from the
   *  answer's import block (null before an import: nothing). The baseline sentence first, word for word: the first
   *  import is the baseline, so there is no diff or history of what came before it. Then the operator's receipts
   *  attached here by id, in the order given, each as she reported it: outside text (text nodes only), labelled
   *  "Reported by the operator", never green or ticked, not verified above verified. Attach takes receipt ids (typed,
   *  or picked by id from her receipts) and who attaches them; Detach is on every row, a missing one too. A 400 changes
   *  nothing and is shown as the field's error, as the server words it.
   *  o: {persona, onChange(import), state, bare}: state is the caller's object, kept across renders ({ids, by, error,
   *  open}); bare: the caller's own section gives the heading. */
  function beforeImport(block, o) {
    if (!isObj(block)) return null;
    const st = o.state || {};
    const rows = (Array.isArray(block.changes_before_import) ? block.changes_before_import : []).filter(isObj);
    const LABEL = 'Reported by the operator';
    const tone = (state) => WORDS.receiptTone[state] || 'warn';
    const reportedTone = (state) => (state === 'unknown' ? 'warn' : state === 'failed' ? 'err' : 'neutral');
    const pillOf = (text, t, title) => h('span', { class: 'pill no-dot tone-' + t, title: title || null }, text);
    const when = (iso) => { const t = parseTs(iso); return isFinite(t) ? h('time', { datetime: String(iso), title: String(iso) + ' (UTC)' }, new Date(t).toLocaleString()) : h('span', null, String(iso || '—')); };
    const byWords = (by) => (by === 'operator' ? 'by the operator' : 'by the owner');
    const list = (items, cls) => h('ul', { class: 'bi-list ' + cls }, items.map((x) => h('li', null, String(x))));
    const err = h('p', { class: 'field-error bi-err', role: 'alert', hidden: !st.error, dataset: { errFor: 'before-import' } }, st.error || '');
    const showErr = (msg) => { st.error = msg || null; err.textContent = msg || ''; err.hidden = !msg; };
    async function post(body, button, okText) {
      showErr(null);
      setBusy(button, true);
      try {
        const r = await api('/api/personas/' + enc(o.persona) + '/before-import', { method: 'POST', body });
        st.ids = '';
        toast(okText, { kind: 'ok' });
        if (o.onChange) o.onChange(r && isObj(r.import) ? r.import : null);
      } catch (e) {
        showErr(e && e.message ? e.message : String(e));
      } finally {
        if (button.isConnected) setBusy(button, false);
      }
    }
    const detach = (id) => btn('Detach', { cls: 'btn-xs btn-ghost', icon: 'x', title: 'Take receipt ' + id + ' off this import', dataset: { action: 'detach', receipt: id, focus: 'bi-detach:' + id },
      onClick: (e) => post({ detach: [id], by: st.by || 'owner' }, e.currentTarget, 'Detached ' + id + '.') });
    const rowEl = (r) => {
      const id = String(r.id || '');
      const attached = h('p', { class: 'bi-attached sf-muted' }, 'Attached ', r.attached_at ? when(r.attached_at) : '', ' ', byWords(r.by), '.');
      if (r.missing) {
        return h('li', { class: 'bi-row is-missing', dataset: { receipt: id, state: 'missing' } },
          h('div', { class: 'bi-row-head' }, h('code', { class: 'bi-id' }, id), h('span', { class: 'tag tag-warn' }, 'Missing'), h('span', { class: 'tag tag-outline bi-label' }, icon('shield', 'icon-xs'), LABEL), h('span', { class: 'spacer' }), detach(id)),
          h('p', { class: 'bi-note' }, ((n) => n.charAt(0).toUpperCase() + n.slice(1))(sentence(String(r.note || 'the receipts index no longer holds this receipt')))), attached);
      }
      const out = isObj(r.outcome) ? r.outcome : {};
      const rep = isObj(r.reported_outcome) ? r.reported_outcome : null;
      const nv = Array.isArray(r.not_verified) ? r.not_verified : [];
      const v = Array.isArray(r.verified) ? r.verified : [];
      return h('li', { class: 'bi-row', dataset: { receipt: id, state: String(out.state || '') } },
        h('div', { class: 'bi-row-head' }, r.time ? when(r.time) : null, h('code', { class: 'bi-id' }, id),
          h('span', { class: 'tag tag-outline bi-label', title: 'What she wrote, as she wrote it: the harness never re-checks it' }, icon('shield', 'icon-xs'), LABEL),
          r.gone ? h('span', { class: 'tag tag-outline' }, 'File gone') : null, h('span', { class: 'spacer' }), detach(id)),
        h('p', { class: 'bi-intent' }, String(r.intent || '')),
        h('div', { class: 'bi-outcome' }, pillOf(String(out.words || out.state || ''), tone(out.state)),
          rep ? pillOf('The change: ' + String(rep.words || ''), reportedTone(rep.state), 'The outcome her receipt itself reports') : null),
        h('h4', { class: 'bi-h4' }, 'Not verified (' + nv.length + ')'), nv.length ? list(nv, 'bi-nv') : h('p', { class: 'sf-muted bi-none' }, 'None reported.'),
        h('h4', { class: 'bi-h4' }, 'Verified, as she reports (' + v.length + ')'), v.length ? list(v, 'bi-v') : h('p', { class: 'bi-nothing' }, 'Nothing verified'),
        attached);
    };
    // Attach: ids typed (one per line, or spaces and commas between), or ticked in her receipts list (by id).
    const ids = h('textarea', { class: 'textarea mono-input', rows: '2', spellcheck: 'false', placeholder: 'r-20261025-013', 'aria-label': 'Receipt ids to attach', dataset: { focus: 'bi-ids', field: 'before-import-ids' } });
    ids.value = st.ids || '';
    ids.addEventListener('input', () => { st.ids = ids.value; });
    const by = selectEl([['owner', 'You (the owner)'], ['operator', 'The operator, through you']], st.by || 'owner', { class: 'select select-sm', 'aria-label': 'Who attaches them', dataset: { focus: 'bi-by', field: 'before-import-by' } });
    by.addEventListener('change', () => { st.by = by.value; });
    const picks = h('div', { class: 'bi-picks' });
    const pickBtn = btn('Pick from her receipts', { cls: 'btn-sm btn-ghost', icon: 'list', dataset: { action: 'pick-receipts', focus: 'bi-pick' }, onClick: async (e) => {
      const b = e.currentTarget;
      setBusy(b, true);
      try {
        const r = await api('/api/operator/receipts?limit=200');
        const have = new Set(rows.map((x) => String(x.id)));
        const seen = new Set();
        const read = (Array.isArray(r && r.receipts) ? r.receipts : []).filter((x) => isObj(x) && x.status === 'read' && x.id && !have.has(String(x.id)) && !seen.has(x.id) && seen.add(x.id));
        clear(picks);
        add(picks, read.length ? h('ul', { class: 'bi-pick-list', 'aria-label': 'Her receipts, newest first' }, read.map((x) => {
          const box = h('input', { type: 'checkbox', dataset: { receipt: String(x.id) } });
          box.addEventListener('change', () => {
            const cur = (ids.value.match(/[A-Za-z0-9][A-Za-z0-9._:-]*/g) || []).filter((y) => y !== x.id);
            if (box.checked) cur.push(String(x.id));
            ids.value = cur.join('\n'); st.ids = ids.value;
          });
          return h('li', null, h('label', { class: 'check' }, box, h('code', null, String(x.id)), ' ', x.time ? h('span', { class: 'sf-muted' }, new Date(parseTs(x.time)).toLocaleDateString()) : null,
            ' ', h('span', { class: 'bi-pick-intent' }, String(x.intent || ''))));
        })) : h('p', { class: 'sf-muted' }, 'No receipt the harness has read is left to attach.'));
      } catch (x) { showErr(x && x.message ? x.message : String(x)); }
      finally { if (b.isConnected) setBusy(b, false); }
    } });
    const attach = btn('Attach', { cls: 'btn-sm btn-primary', icon: 'plus', dataset: { action: 'attach', focus: 'bi-attach' }, onClick: (e) => {
      const list2 = Array.from(new Set((ids.value.match(/[^\s,]+/g) || [])));
      if (!list2.length) { showErr('Type or pick at least one receipt id.'); return; }
      post({ attach: list2, by: by.value }, e.currentTarget, 'Attached ' + plural(list2.length, 'receipt') + '.');
    } });
    const form = h('details', { class: 'bi-form', open: st.open ? true : null, dataset: { focus: 'bi-form' } },
      h('summary', null, icon('plus', 'icon-sm'), 'Attach receipts…'),
      h('div', { class: 'bi-form-body' },
        field('Receipt ids', ids, 'Only a receipt the harness has read can be attached. They are chosen by id, never by what they say.'),
        h('div', { class: 'sf-row bi-form-row' }, field('Who attaches them', by), pickBtn), picks, h('div', { class: 'actions' }, attach)));
    form.addEventListener('toggle', () => { st.open = form.open; });
    return h(o.bare ? 'div' : 'section', { class: 'bi-sec', 'aria-labelledby': o.bare ? null : 'bi-t-' + o.persona, dataset: { part: 'before-import' } },
      o.bare ? null : h('h2', { class: 'bi-title', id: 'bi-t-' + o.persona, tabindex: '-1' }, 'Changes before import'),
      h('p', { class: 'bi-baseline', dataset: { part: 'baseline' } }, String(block.baseline || '')),
      h('p', { class: 'sf-muted bi-model' }, 'Workspace model ', h('code', null, String(block.model || '')), block.version_id ? [' · version ', h('code', null, String(block.version_id))] : null,
        block.imported_at ? [' · imported ', when(block.imported_at)] : null),
      rows.length ? h('ol', { class: 'bi-rows', 'aria-label': 'Receipts attached as the changes before this import, in the order given' }, rows.map(rowEl))
        : h('p', { class: 'sf-muted bi-empty' }, 'No receipt is attached: nothing is recorded about what changed before the import.'),
      err, form);
  }

  // ---------------------------------------------------------------- window.SF: the one namespace view modules use
  // Text-only DOM building (strings always become text nodes, never HTML), the API client, toasts, the live event
  // stream, the panel, empty states and the phone rules. Frozen: a view module cannot replace a helper for the others.
  const SF = Object.freeze({
    version: 1,
    product: PRODUCT,
    // views and extension points
    registerView, registerJobTab, extend, refreshJobs,
    // DOM: h(tag, props, ...children); strings are text, props.text sets textContent, on<event> props add listeners
    h, add, clear, icon, btn, field, selectEl, callout, emptyState, setBusy, timeEl, viewHead,
    skeleton: skeletonLines,
    procedureList, rebuild, placeholder, unavailable, trackFades,
    text: (s) => document.createTextNode(s === null || s === undefined ? '' : String(s)),
    /** Escape-first Markdown for text that is meant as Markdown (agent files, specs): no raw HTML, http(s) links only.
     *  Never for messages, receipts, inbound text or anything DESIGN §34-§36 says is textContent only. */
    md: (text, cls) => MD.render(String(text || ''), cls),
    safeHref,
    // formatting
    relTime, fmtDur, fmtNum, plural, firstLine, enc, isObj, sentence,
    // words every section shares (statuses of items, drift checks and leases), and links to the tracker
    custodyAnswer, custodyReason, custodyPlain, itemHref, itemLink, beforeImport,
    words: WORDS,
    itemWord: (status) => WORDS.items[status] || String(status || ''),
    // the server: api(path, {method?, body?, text?, retry409?}) → JSON (or text); errors throw ApiError {status,
    // message} with the server's {error} verbatim; a 401 goes to the login page
    api, ApiError, gone,
    // feedback
    toast, toastError, copyText, openModal, confirmModal, announce,
    // live: on(type | [types], fn(ev)) → off(); onReconnect(fn(why)) → off(); conn() → open | connecting | retrying
    onReconnect,
    on: onEvent,
    conn: () => S.conn,
    // the right-hand panel (one at a time; a job's drawer replaces it)
    drawer: Object.freeze({ open: openPanel, close: () => closePanel(false), isOpen: (key) => GD.open && (key === undefined || GD.key === key) }),
    // read-only state of the page
    route: () => S.route,
    go: (hash) => { if (location.hash !== hash) location.hash = hash; },
    jobs: () => Array.from(S.jobs.values()),
    job: (repo, id) => S.jobs.get(repo + '/' + id) || null,
    repos: () => S.repos.slice(),
    agents: () => S.agents.slice(),
    filterRepo: () => S.filterRepo,
    isNarrow,
    /** Per-viewer conveniences only (a remembered tab or filter; keys 'sf.<area>.<name>'): never a token, a
     *  passphrase or message text. */
    storage: Object.freeze({ get: lsGet, set: lsSet }),
  });
  Object.defineProperty(window, 'SF', { value: SF, writable: false, configurable: false, enumerable: true });

  // ================================================================ routing
  const VIEWS = ['home', 'factory', 'plans', 'chat', 'agents', 'settings', 'doctor', 'flow', 'grid', 'procedures'];
  /** "a=1&b=two" → {a: '1', b: 'two'} (a view's #/route?query; the last value of a repeated key wins). */
  function parseQuery(q) {
    const out = {};
    for (const pair of String(q || '').split('&')) {
      if (!pair) continue;
      const i = pair.indexOf('=');
      let k = i >= 0 ? pair.slice(0, i) : pair;
      let v = i >= 0 ? pair.slice(i + 1) : '';
      try { k = decodeURIComponent(k.replace(/\+/g, ' ')); v = decodeURIComponent(v.replace(/\+/g, ' ')); } catch (e) { continue; }
      out[k] = v;
    }
    return out;
  }
  function parseRoute() {
    const full = location.hash.replace(/^#\/?/, '');
    const qi = full.indexOf('?');
    const raw = qi >= 0 ? full.slice(0, qi) : full;
    let parts;
    try { parts = raw.split('/').filter(Boolean).map(decodeURIComponent); } catch (e) { parts = []; }
    const v = parts[0] || 'home';
    if (v === 'home') return { view: 'home' };
    if (v === 'factory') return { view: 'factory' };
    if (v === 'job' && parts.length >= 3) {
      // A job opened from a plan's graph keeps the plan behind the drawer.
      const fromPlan = S.route.view === 'plans' && !!D.back;
      // A job opened from Home ("Needs you") keeps Home behind the drawer, like a plan's.
      const behind = fromPlan ? 'plans' : S.route.view === 'home' ? 'home' : 'factory';
      return { view: behind, plan: fromPlan ? S.route.plan : null, job: { repo: parts[1], id: parts[2], tab: parts[3] || null } };
    }
    if (v === 'plans') return { view: 'plans', plan: parts[1] && parts[2] ? { repo: parts[1], id: parts[2] } : null };
    if (v === 'chat') return { view: 'chat', chat: parts[1] || null };
    if (v === 'agents') return { view: 'agents', agent: parts[1] || null };
    if (v === 'settings') return { view: 'settings', section: parts[1] || null };
    if (v === 'doctor') return { view: 'doctor' };
    if (v === 'welcome' || v === 'setup' || v === 'start') {
      const map = parts[1] === 'map';
      return { view: 'flow', flow: v, mode: map ? 'map' : 'wizard', step: map ? null : parts[1] || null, node: map ? parts[2] || null : null };
    }
    if (v === 'grid') return { view: 'grid', node: parts[1] || null };
    if (v === 'procedures') return { view: 'procedures', section: parts[1] || null };
    // A view module's route: #/<name>/<part>/…?<query> → {view: its id, name, parts, query}.
    const mod = REG.routes.get(v);
    if (mod) return { view: mod.id, ext: true, name: v, parts: parts.slice(1), query: qi >= 0 ? parseQuery(full.slice(qi + 1)) : {} };
    return { view: 'home' };
  }
  function route() {
    // In-page anchors (the skip link's #main) are not routes: keep the current view.
    if (location.hash && !/^#\/?$|^#\//.test(location.hash) && S.route.view) return;
    const r = parseRoute();
    const prev = S.route;
    r.hash = location.hash;
    S.route = r;
    // A job opened from another page (Home, a view module's link) closes back to that page, focus on its opener.
    if (r.job && !prev.job && !D.key) {
      if (!D.back && prev.hash && prev.view !== 'factory') D.back = prev.hash;
      if (!D.opener && document.activeElement && document.activeElement !== document.body) D.opener = document.activeElement;
    }
    for (const v of VIEWS.concat(Array.from(REG.views.keys()))) {
      const sec = document.getElementById('view-' + v);
      if (sec) sec.hidden = v !== r.view;
    }
    for (const a of $$('.rail-item')) {
      if (a.dataset.view === r.view) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
    }
    const flowChanged = r.view === 'flow' && (prev.view !== 'flow' || prev.flow !== r.flow);
    const changedView = prev.view !== r.view;
    if (changedView) document.getElementById('main').scrollTop = 0;
    // A panel a view module opened (SF.drawer) closes when its view is left, or when the route goes back to the
    // hash the panel names as its way out; a view that still wants it opens it again.
    if (GD.open && (r.view !== GD.view || (GD.opts.back && location.hash === GD.opts.back))) closePanel(false);
    if (r.job) openDrawer(r.job.repo, r.job.id, r.job.tab);
    else if (D.key) hideDrawer();
    if (r.view === 'home') { loadHome(prev.view !== 'home'); invalidate('home'); }
    routeModules(r, prev);
    if (r.view === 'factory') invalidate('strip', 'board', 'backlog');
    if (r.view === 'plans') {
      if (!PL.loaded && !PL.loading) loadPlans();
      const k = r.plan ? r.plan.repo + '/' + r.plan.id : null;
      if (PL.sel !== k) {
        // An unsaved plan.md edit is kept as that plan's draft, and comes back with it.
        if (PL.edit && PL.edit.key !== k) {
          const ed = PL.edit;
          if (planEditDirty(ed)) {
            ed.ta = null;
            PL.drafts.set(ed.key, ed);
            const [dr, di] = splitKey(ed.key);
            toast(['Your unsaved plan.md edit of ', h('strong', { class: 'mono' }, di), ' is kept as a draft.'], { kind: 'info', action: { label: 'Back to it', href: '#/plans/' + enc(dr) + '/' + enc(di) } });
          }
          PL.edit = null;
        }
        PL.sel = k;
        if (k && !PL.edit && PL.drafts.has(k)) {
          PL.edit = PL.drafts.get(k);
          PL.drafts.delete(k);
          Object.assign(PL.edit, { ta: null, restored: true, focus: false, saving: false });
        }
        if (k) loadPlanDetail(k);
      }
      invalidate('plans');
    }
    if (r.view === 'chat') { if (!CH.listLoaded) loadChats(); selectChat(r.chat); }
    if (r.view === 'agents') { if (prev.view !== 'agents') AG.shown = null; AG.sel = r.agent || AG.sel; invalidate('agents'); }
    if (r.view === 'settings') {
      if (!CFG.orig && !CFG.loading) loadConfig();
      invalidate('settings');
      if (r.section) {
        const anchor = anchorSec(r.section);
        const go = () => {
          const el = document.getElementById('set-' + anchor);
          if (el) el.scrollIntoView({ block: 'start' });
          const nav = document.querySelector('.settings-nav');
          if (nav) {
            for (const a of nav.querySelectorAll('a')) a.setAttribute('aria-current', a.dataset.anchor === anchor ? 'true' : 'false');
            revealNavLink(nav);
            if (nav._fades) nav._fades();
          }
        };
        requestAnimationFrame(() => requestAnimationFrame(go));
      }
    }
    if (r.view === 'doctor') { invalidate('doctor'); if (!S.doctor && !S.doctorLoading) loadDoctor(false); }
    if (r.view === 'flow') {
      if (flowChanged || Date.now() - SETUPST.at > 15000) loadSetup();
      if (r.flow === 'setup' && (flowChanged || !SECRETS.map) && !SECRETS.missing) loadSecrets();
      if (r.flow !== 'welcome' && !CFG.orig && !CFG.loading) ensureConfig().catch(() => {});
      if (r.flow === 'start' && !CH.listLoaded) loadChats();
      if (flowChanged) WZ.err = null;
      invalidate('flow');
    }
    if (r.view === 'grid') {
      if (prev.view !== 'grid' || Date.now() - SETUPST.at > 15000) loadSetup();
      if (!CFG.orig && !CFG.loading) ensureConfig().catch(() => {});
      if (!DOCS.data && !DOCS.missing && !DOCS.loading) loadDocs();
      invalidate('grid');
    }
    if (r.view === 'procedures') { if (prev.view !== 'procedures') PROC.scrolled = null; if (!DOCS.data && !DOCS.missing && !DOCS.loading) loadDocs(); invalidate('procedures'); }
    placeToasts();
    updateTitle();
  }

  // ================================================================ keyboard
  function isTyping(t) { return !!t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName)); }
  function focusSearch() {
    const v = S.route.view;
    const mod = REG.views.get(v);
    const own = mod && mod.section ? mod.section.querySelector('[data-sf-search]') : null;
    const el = own || (v === 'factory' && !D.key ? document.getElementById('board-search') : v === 'chat' ? document.getElementById('chat-search') : document.getElementById('repo-filter'));
    if (el) { el.focus(); if (el.select) el.select(); }
  }
  function showShortcuts() {
    const row = (keys, what) => [h('span', null, keys.map((k, i) => [i ? ' ' : null, h('kbd', null, k)])), h('span', null, what)];
    openModal({
      title: 'Keyboard shortcuts',
      body: h('div', { class: 'shortcut-list' }, row(['/'], 'Focus the search or filter'), row(['n'], 'New feature'), row(['p'], 'New plan'), row(['Esc'], 'Close the drawer or dialog'),
        row(['Enter'], 'Send a chat message'), row(['Shift', 'Enter'], 'New line in a chat message'), row(['Ctrl', 'Enter'], 'Run a new feature now'), row(['?'], 'This help')),
      actions: [{ label: 'Close', value: 'ok', primary: true }],
    });
  }
  document.addEventListener('keydown', (e) => {
    if (e.defaultPrevented) return;
    const dialogOpen = !!document.querySelector('dialog[open]');
    if (e.key === 'Escape') {
      if (dialogOpen) return;
      const t = e.target;
      if (t && (t.id === 'board-search' || t.id === 'chat-search') && t.value) { t.value = ''; t.dispatchEvent(new Event('input')); e.preventDefault(); return; }
      if (D.key) { e.preventDefault(); closeDrawer(); return; }
      if (GD.open) { e.preventDefault(); closePanel(true); return; }
      if (isTyping(t)) { t.blur(); return; }
      // A map card closes and focus goes back to its node on the map.
      if (S.route.view === 'grid' && S.route.node) { e.preventDefault(); GRIDV.focusNode = S.route.node; location.hash = '#/grid'; return; }
      if (S.route.view === 'flow' && S.route.mode === 'map' && S.route.node) { e.preventDefault(); WZ.focus = { node: S.route.node }; location.hash = '#/' + S.route.flow + '/map'; return; }
      return;
    }
    if (dialogOpen || isTyping(e.target) || e.metaKey || e.ctrlKey || e.altKey) return;
    if (e.key === '/') { e.preventDefault(); focusSearch(); }
    else if (e.key === 'n' || e.key === 'N') { e.preventDefault(); openNewFeature(); }
    else if (e.key === 'p' || e.key === 'P') { e.preventDefault(); openNewPlan(); }
    else if (e.key === '?') { e.preventDefault(); showShortcuts(); }
  });

  // ================================================================ init
  function init() {
    document.getElementById('theme-toggle').addEventListener('click', () => {
      themeMode = THEMES[(THEMES.indexOf(themeMode) + 1) % THEMES.length];
      lsSet('sf.theme', themeMode === 'auto' ? null : themeMode);
      applyTheme();
    });
    const repoFilter = document.getElementById('repo-filter');
    repoFilter.addEventListener('change', () => {
      S.filterRepo = repoFilter.value;
      lsSet('sf.repo', S.filterRepo || null);
      invalidate('strip', 'board', 'topbar', 'chatList', 'backlog');
      if (BL.open) loadBacklog();
    });
    document.getElementById('new-feature-btn').addEventListener('click', () => openNewFeature());
    document.getElementById('new-chat-btn').addEventListener('click', () => openNewChat());
    document.getElementById('strip-toggle').addEventListener('click', () => {
      const card = document.getElementById('strip-card');
      const expanding = card.classList.contains('collapsed');
      lsSet('sf.stripCollapsed', expanding ? '0' : '1');
      STRIP.sig = '';
      invalidate('strip');
      if (expanding) {
        requestAnimationFrame(() => requestAnimationFrame(() => {
          const sc = document.getElementById('strip-scroll');
          const hot = sc.querySelector('.node.active');
          if (hot && sc.scrollWidth > sc.clientWidth) sc.scrollLeft = Math.max(0, hot.offsetLeft + hot.offsetWidth / 2 - sc.clientWidth / 2);
        }));
      }
    });
    document.getElementById('backlog-toggle').addEventListener('click', () => setBacklogOpen(!BL.open));
    const search = document.getElementById('board-search');
    search.addEventListener('input', () => { S.search = search.value; invalidate('board'); });
    const chatSearch = document.getElementById('chat-search');
    chatSearch.addEventListener('input', () => { CH.search = chatSearch.value; invalidate('chatList'); });
    document.getElementById('scrim').addEventListener('click', () => { if (GD.open) closePanel(true); else closeDrawer(); });
    // The skip link must not change the hash: that is the router's.
    document.querySelector('.skip-link').addEventListener('click', (e) => {
      e.preventDefault();
      const main = document.getElementById('main');
      main.focus({ preventScroll: true });
    });
    document.getElementById('offline-retry').addEventListener('click', retryNow);
    trackFades(document.getElementById('strip-scroll'));
    trackFades(document.getElementById('board-wrap'), boardFades);
    if (window.ResizeObserver) {
      const ro = new ResizeObserver(() => { invalidate('strip'); refreshFades(); });
      ro.observe(document.getElementById('strip-scroll'));
      ro.observe(document.getElementById('board-wrap'));
    }
    window.addEventListener('resize', () => { invalidate('strip'); refreshFades(); });
    if (window.matchMedia) {
      const mq = window.matchMedia('(prefers-color-scheme: dark)');
      const onScheme = () => { if (themeMode === 'auto') applyTheme(); };
      if (mq.addEventListener) mq.addEventListener('change', onScheme);
    }
    setInterval(() => {
      for (const el of document.querySelectorAll('[data-rel]')) el.textContent = relTime(el.dataset.rel);
    }, 30000);
    setInterval(() => {
      const now = Date.now();
      for (const el of document.querySelectorAll('[data-since]')) el.textContent = fmtDur(now - Number(el.dataset.since));
      for (const el of document.querySelectorAll('[data-clock]')) el.textContent = fmtClock(now - Number(el.dataset.clock));
      if (S.conn === 'retrying') renderConn();
    }, 1000);
    window.addEventListener('hashchange', route);
    window.addEventListener('resize', () => {
      for (const b of document.querySelectorAll('.map-scroll')) if (b._fit) b._fit();
      placeToasts();
      if (S.route.view === 'grid') invalidate('grid');
    });
    renderConn();
    invalidate('board', 'strip', 'doctorBadge', 'backlog');
    // A bare "/" on a fresh install (setup not complete and nothing ever run: no repo, or no job, plan or chat)
    // opens Welcome with its first step; the board waits a moment for that answer. A factory in use stays on
    // the board even with a key missing: the Grid Card and Doctor show what needs attention.
    const firstState = refreshState();
    if (/^#?\/?$/.test(location.hash)) {
      const mainEl = document.getElementById('main');
      mainEl.classList.add('is-booting');
      Promise.race([Promise.all([loadSetup(), firstState]), sleep(1500)]).then(() => {
        mainEl.classList.remove('is-booting');
        const unused = S.loaded && (!S.repos.length || (!S.jobs.size && !S.plans.size && !S.chats.size));
        if (/^#?\/?$/.test(location.hash) && SETUPST.status && SETUPST.status.configured === false && unused) location.replace('#/welcome');
      });
    } else loadSetup();
    REG.booted = true;
    renderRail();
    route();
    for (const v of REG.views.values()) if (v.spec.start) safely('view ' + v.id + ' start', () => v.spec.start(v.handle));
    connect();
    loadDoctor(false);
    loadChats();
    loadPlans();
  }
  // Deferred scripts run in document order before DOMContentLoaded: app.js defines window.SF, each views/<area>.js
  // registers itself, and only then does the page start (routes, the rail and the event stream know every view).
  let booted = false;
  function boot() {
    if (booted) return;
    booted = true;
    init();
  }
  if (document.readyState === 'complete') boot();
  else {
    document.addEventListener('DOMContentLoaded', boot);
    window.addEventListener('load', boot);
  }
})();
