/* ==========================================================================
   环境事件识别台 · 客户端
   - 取数只有一个入口：bus.request / bus.start（Electron 壳里替换 window.__DOORBELL__ 即可）
   - ?mock=1 时改用 mock.js 的本地状态机，脱离 Python 也能完整走界面
   ========================================================================== */

'use strict';

(function () {
  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

  const IS_MOCK = new URLSearchParams(location.search).has('mock');
  const API_BASE = (window.__DOORBELL__ && window.__DOORBELL__.apiBase) || '';
  const BASE_TITLE = IS_MOCK ? '模拟数据 · 环境事件识别台' : '环境事件识别台';

  const MODE_LABEL = { idle: '未就绪', listening: '监听中', learning: '学习中', hit: '命中' };
  const IOT_LABEL = {
    unset: '未配置',
    connecting: '连接中',
    online: '已连接',
    reconnecting: '重连中',
    failed: '失败'
  };
  const ENV_LABEL = { quiet: '环境安静', loud: '环境偏吵', clip: '麦克风削波' };

  const S = {
    snap: null,
    events: [],
    logs: [],
    connected: false,
    lastUpdate: 0,
    view: null,
    viewData: null,
    lastFocus: null,
    closeTimer: 0,
    toastTimer: 0
  };

  const el = {};

  /* ------------------------------------------------------------------------
     工具函数
     ------------------------------------------------------------------------ */

  const pad2 = (n) => String(n).padStart(2, '0');

  function fmtClock(ts) {
    if (!ts) return '--:--:--';
    const d = new Date(ts);
    return pad2(d.getHours()) + ':' + pad2(d.getMinutes()) + ':' + pad2(d.getSeconds());
  }

  function fmtFull(ts) {
    if (!ts) return '—';
    const d = new Date(ts);
    return (
      d.getFullYear() + '-' + pad2(d.getMonth() + 1) + '-' + pad2(d.getDate()) +
      ' ' + fmtClock(ts)
    );
  }

  function esc(value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }[c]));
  }

  function num(value, digits, fallback) {
    const n = Number(value);
    if (!isFinite(n)) return fallback == null ? '—' : fallback;
    return n.toFixed(digits);
  }

  function stripProto(url) {
    return String(url || '').replace(/^https?:\/\//, '');
  }

  function clamp(n, lo, hi) {
    return Math.min(hi, Math.max(lo, n));
  }

  function setText(node, text) {
    if (node && node.textContent !== text) node.textContent = text;
  }

  function friendly(err) {
    if (!err) return '操作失败';
    if (err.status === 401) return '需要访问口令';
    if (!err.status) return '连接不上服务，请确认服务仍在运行';
    return err.message || '操作失败';
  }

  /* ------------------------------------------------------------------------
     取数总线
     ------------------------------------------------------------------------ */

  function createHttpBus() {
    let source = null;
    let onMessage = null;
    let retryTimer = 0;
    let watchdog = 0;
    let lastSeen = 0;
    let stopped = false;

    async function request(path, init) {
      const options = Object.assign({}, init || {});
      options.credentials = 'same-origin';
      options.cache = 'no-store';
      options.headers = Object.assign({}, (init && init.headers) || {});

      const res = await fetch(API_BASE + path, options);
      const type = res.headers.get('content-type') || '';

      if (!res.ok) {
        let message = res.statusText || ('请求失败 ' + res.status);
        try {
          if (type.indexOf('json') >= 0) {
            const body = await res.json();
            message = body.error || body.message || message;
          } else {
            const text = (await res.text()).trim();
            if (text) message = text;
          }
        } catch (_) { /* keep status text */ }
        const err = new Error(message);
        err.status = res.status;
        throw err;
      }

      if (type.indexOf('application/json') >= 0) return res.json();
      return res.text();
    }

    function send(message) {
      if (onMessage) onMessage(message);
    }

    function closeSource() {
      if (source) {
        try { source.close(); } catch (_) {}
        source = null;
      }
      if (watchdog) {
        clearInterval(watchdog);
        watchdog = 0;
      }
    }

    function schedule() {
      if (stopped || retryTimer) return;
      retryTimer = window.setTimeout(function () {
        retryTimer = 0;
        connect();
      }, 3000);
    }

    function connect() {
      if (stopped) return;
      closeSource();
      lastSeen = Date.now();

      try {
        source = new EventSource(API_BASE + '/events');
      } catch (_) {
        send({ type: 'connection', connected: false });
        schedule();
        return;
      }

      source.onopen = function () {
        lastSeen = Date.now();
        send({ type: 'connection', connected: true });
      };

      source.onerror = function () {
        send({ type: 'connection', connected: false });
        closeSource();
        schedule();
      };

      const on = (name) => function (event) {
        lastSeen = Date.now();
        let data = null;
        try { data = JSON.parse(event.data); } catch (_) { return; }
        send({ type: name, data: data });
      };

      source.addEventListener('state', on('state'));
      source.addEventListener('event', on('event'));
      source.addEventListener('log', on('log'));
      source.addEventListener('notice', on('notice'));
      source.addEventListener('heartbeat', function () { lastSeen = Date.now(); });

      watchdog = window.setInterval(function () {
        if (source && Date.now() - lastSeen > 6000) {
          send({ type: 'connection', connected: false });
          closeSource();
          schedule();
        }
      }, 1000);
    }

    return {
      request: request,
      start: function (handler) {
        onMessage = handler;
        connect();
      },
      stop: function () {
        stopped = true;
        closeSource();
        if (retryTimer) clearTimeout(retryTimer);
      }
    };
  }

  const bus = (window.__DOORBELL__ && window.__DOORBELL__.bus) ||
    (IS_MOCK ? window.createMockBus() : createHttpBus());

  /* ------------------------------------------------------------------------
     启动
     ------------------------------------------------------------------------ */

  function cacheElements() {
    el.app = $('#app');
    el.offline = $('#offline');
    el.offlineNote = $('#offline-note');
    el.addrCopy = $('#addr-copy');
    el.addrLan = $('#addr-lan');
    el.addrLocal = $('#addr-local');
    el.iotChip = $('#iot-chip');
    el.iotDot = $('#iot-dot');
    el.iotValue = $('#iot-value');
    el.gauge = $('#gauge');
    el.gaugeFill = $('#gauge-fill');
    el.gaugeFloor = $('#gauge-floor');
    el.gaugeKnob = $('#gauge-knob');
    el.dbValue = $('#db-value');
    el.floorText = $('#floor-text');
    el.envText = $('#env-text');
    el.stateDot = $('#state-dot');
    el.stateChip = $('#state-chip');
    el.heard = $('#heard');
    el.stateWord = $('#state-word');
    el.subline = $('#subline');
    el.hitMeta = $('#hit-meta');
    el.hitTier = $('#hit-tier');
    el.hitScore = $('#hit-score');
    el.hitLatency = $('#hit-latency');
    el.hitTime = $('#hit-time');
    el.hitDemo = $('#hit-demo');
    el.learn = $('#learn');
    el.learnSegments = $('#learn-segments');
    el.learnBar = $('#learn-bar');
    el.learnHint = $('#learn-hint');
    el.actions = $('#actions');
    el.btnListen = $('#btn-listen');
    el.btnLearn = $('#btn-learn');
    el.btnLearnCancel = $('#btn-learn-cancel');
    el.railList = $('#rail-list');
    el.railCount = $('#rail-count');
    el.railMore = $('#rail-more');
    el.fTemplates = $('#f-templates');
    el.fHit = $('#f-hit');
    el.fIgnore = $('#f-ignore');
    el.scrim = $('#scrim');
    el.drawer = $('#drawer');
    el.drawerTitle = $('#drawer-title');
    el.drawerBody = $('#drawer-body');
    el.drawerFoot = $('#drawer-foot');
    el.drawerClose = $('#drawer-close');
    el.toast = $('#toast');
    el.gate = $('#gate');
    el.gateForm = $('#gate-form');
    el.gatePassword = $('#gate-password');
    el.gateError = $('#gate-error');
  }

  function boot() {
    cacheElements();
    buildGauge();
    wireEvents();

    if (IS_MOCK) ensureMockBadge();

    document.title = BASE_TITLE;

    bus.start(handleMessage);
    bus.request('/api/state').then(applySnapshot).catch(function (err) {
      if (err && err.status === 401) showGate();
      else setConnected(false);
    });

    window.setTimeout(function () {
      if (!S.snap) setConnected(false);
    }, 6000);
  }

  function handleMessage(message) {
    if (!message) return;
    switch (message.type) {
      case 'connection':
        setConnected(!!message.connected);
        break;
      case 'state':
        applySnapshot(message.data);
        break;
      case 'event':
        addEvent(message.data);
        break;
      case 'log':
        addLog(message.data);
        break;
      case 'notice':
        showNotice(message.data);
        break;
      default:
        break;
    }
  }

  /* ------------------------------------------------------------------------
     连接状态
     ------------------------------------------------------------------------ */

  function setConnected(ok) {
    const wasConnected = S.connected;
    S.connected = ok;
    el.offline.hidden = ok;

    if (!ok) {
      el.offlineNote.textContent = S.lastUpdate
        ? '最后更新 ' + fmtClock(S.lastUpdate)
        : '正在等待服务';
      if (!S.snap) setText(el.subline, '连接已断开，正在重试…');
      return;
    }

    if (!wasConnected) {
      bus.request('/api/state').then(applySnapshot).catch(function (err) {
        if (err && err.status === 401) showGate();
      });
    }
  }

  function ensureMockBadge() {
    if ($('.mock-badge')) return;
    const badge = document.createElement('span');
    badge.className = 'mock-badge';
    badge.textContent = '模拟数据';
    const net = $('.net');
    if (net) net.prepend(badge);
  }

  /* ------------------------------------------------------------------------
     状态应用
     ------------------------------------------------------------------------ */

  function applySnapshot(snapshot) {
    if (!snapshot) return;
    const hadEvents = Array.isArray(snapshot.events);

    S.snap = snapshot;
    S.lastUpdate = Date.now();
    S.connected = true;
    el.offline.hidden = true;
    if (snapshot.mock) ensureMockBadge();

    if (hadEvents) {
      S.events = snapshot.events.slice(0, 300);
      renderEvents();
    }
    if (Array.isArray(snapshot.logs)) {
      S.logs = snapshot.logs.slice(-300);
      if (S.view === 'records') renderLogs();
    }

    renderHeader(snapshot);
    renderGauge(snapshot);
    renderHero(snapshot);
    renderFoot(snapshot);

    if (S.view) updateDrawerLive(snapshot);
  }

  /* ------------------------------------------------------------------------
     顶栏 / 底栏
     ------------------------------------------------------------------------ */

  function renderHeader(snapshot) {
    const net = snapshot.net || {};
    setText(el.addrLan, stripProto(net.lan) || '—');
    setText(el.addrLocal, stripProto(net.local) || '—');

    const iot = (snapshot.iot && snapshot.iot.state) || 'unset';
    el.iotDot.dataset.state = iot;
    setText(el.iotValue, IOT_LABEL[iot] || IOT_LABEL.unset);
  }

  function renderFoot(snapshot) {
    const templates = snapshot.templates || {};
    const stats = snapshot.stats || {};
    setText(el.fTemplates, String(templates.count || 0));
    setText(el.fHit, String(stats.hit || 0));
    setText(el.fIgnore, String(stats.ignore || 0));
  }

  /* ------------------------------------------------------------------------
     仪表
     ------------------------------------------------------------------------ */

  const SVG_NS = 'http://www.w3.org/2000/svg';
  const START_ANGLE = 135;
  const SWEEP = 270;
  const GAUGE_R = 76;

  function pointAt(angleDeg, radius) {
    const a = (angleDeg * Math.PI) / 180;
    return [100 + radius * Math.cos(a), 100 + radius * Math.sin(a)];
  }

  function buildGauge() {
    const ticks = $('#gauge-ticks');
    const scale = $('#gauge-scale');

    for (let value = 0; value <= 100; value += 2) {
      const angle = START_ANGLE + (SWEEP * value) / 100;
      const major = value % 10 === 0;
      const inner = major ? 83 : 85;
      const outer = major ? 92 : 89;
      const p1 = pointAt(angle, inner);
      const p2 = pointAt(angle, outer);
      const line = document.createElementNS(SVG_NS, 'line');
      line.setAttribute('x1', p1[0].toFixed(2));
      line.setAttribute('y1', p1[1].toFixed(2));
      line.setAttribute('x2', p2[0].toFixed(2));
      line.setAttribute('y2', p2[1].toFixed(2));
      if (major) line.setAttribute('class', 'is-major');
      ticks.appendChild(line);
    }

    [
      [0, '−70'],
      [50, '−40'],
      [100, '−10']
    ].forEach(function (entry) {
      const angle = START_ANGLE + (SWEEP * entry[0]) / 100;
      const p = pointAt(angle, 96);
      const text = document.createElementNS(SVG_NS, 'text');
      text.setAttribute('x', p[0].toFixed(2));
      text.setAttribute('y', p[1].toFixed(2));
      text.textContent = entry[1];
      scale.appendChild(text);
    });
  }

  function stateOf(snapshot) {
    if (snapshot.mode === 'learning') return 'learning';
    if (snapshot.mode === 'hit') return 'hit';
    if (snapshot.heard) return 'heard';
    if (snapshot.mode === 'listening') return 'listening';
    return 'idle';
  }

  function renderGauge(snapshot) {
    const db = typeof snapshot.level_db === 'number' ? snapshot.level_db : null;
    const value = db == null ? 0 : clamp(((db + 70) / 60) * 100, 0, 100);
    const state = stateOf(snapshot);

    el.gauge.dataset.state = state;
    el.gaugeFill.style.strokeDashoffset = String(100 - value);
    el.gaugeFill.style.opacity = db == null ? '0' : '1';
    setText(el.dbValue, db == null ? '--' : db.toFixed(1));

    if (db == null) {
      el.gaugeKnob.style.display = 'none';
    } else {
      const knobAngle = START_ANGLE + (SWEEP * value) / 100;
      const p = pointAt(knobAngle, GAUGE_R);
      el.gaugeKnob.style.display = '';
      el.gaugeKnob.setAttribute('cx', p[0].toFixed(2));
      el.gaugeKnob.setAttribute('cy', p[1].toFixed(2));
    }

    const floor = typeof snapshot.floor_db === 'number' ? snapshot.floor_db : null;
    if (floor == null) {
      el.gaugeFloor.style.display = 'none';
      setText(el.floorText, '底噪 --');
    } else {
      const floorValue = clamp(((floor + 70) / 60) * 100, 0, 100);
      const angle = START_ANGLE + (SWEEP * floorValue) / 100;
      const p1 = pointAt(angle, 82);
      const p2 = pointAt(angle, 91);
      el.gaugeFloor.style.display = '';
      el.gaugeFloor.setAttribute('x1', p1[0].toFixed(2));
      el.gaugeFloor.setAttribute('y1', p1[1].toFixed(2));
      el.gaugeFloor.setAttribute('x2', p2[0].toFixed(2));
      el.gaugeFloor.setAttribute('y2', p2[1].toFixed(2));
      setText(el.floorText, '底噪 ' + floor.toFixed(1) + ' dB');
    }

    setText(el.envText, ENV_LABEL[snapshot.env] || '');
  }

  /* ------------------------------------------------------------------------
     大字区
     ------------------------------------------------------------------------ */

  function noticeOf(snapshot) {
    const notice = snapshot.notice;
    if (!notice) return null;
    if (snapshot.mode !== 'idle') return null;
    const serverNow = snapshot.now || Date.now();
    if (notice.until && serverNow > notice.until) return null;
    return notice;
  }

  function hitLabel(hit) {
    return (hit && hit.label) || '门铃';
  }

  function renderHero(snapshot) {
    const state = stateOf(snapshot);
    const learning = snapshot.mode === 'learning' && snapshot.learn;
    const hit = snapshot.mode === 'hit' && snapshot.hit;
    const notice = noticeOf(snapshot);
    const listening = snapshot.mode === 'listening';
    const onAir = snapshot.on_air != null ? !!snapshot.on_air : listening;
    const templates = snapshot.templates || { count: 0 };
    const config = snapshot.config || {};

    let word = '待机';
    let sub = '点「开始监听」开始识别。';
    let chip = MODE_LABEL[snapshot.mode] || MODE_LABEL.idle;

    if (hit) {
      word = hitLabel(hit) + '！';
      sub = '';
      chip = '命中';
    } else if (learning) {
      word = '请按门铃';
      const remaining = Math.max(0, Math.ceil(learning.remaining || 0));
      sub = '第 ' + learning.take + ' 次，共 ' + learning.total + ' 次，还剩 ' + remaining + ' 秒';
      chip = '学习中';
    } else if (listening) {
      word = '监听中';
      if (snapshot.playing) {
        sub = '正在播放：' + hitLabel(snapshot.playing);
      } else if (snapshot.heard) {
        sub = '听到声音，正在判断…';
      } else if (snapshot.last_hit) {
        sub = '上一声' + hitLabel(snapshot.last_hit) + ' ' + fmtClock(snapshot.last_hit.ts);
      } else {
        sub = '还没有听到门铃。';
      }
      chip = '监听中';
    } else if (notice) {
      word = notice.title || word;
      sub = notice.detail || '';
      chip = MODE_LABEL.idle;
    } else if (snapshot.stopped) {
      word = '已停止';
      sub = '点「开始监听」重新开始。';
      chip = MODE_LABEL.idle;
    } else if (!templates.count) {
      word = '请先学习门铃';
      sub = '点「学习门铃」，按 ' + (config.takes || 3) + ' 次门铃。';
      chip = MODE_LABEL.idle;
    }

    setText(el.stateWord, word);
    setText(el.subline, sub);
    setText(el.stateChip, chip);
    el.stateDot.dataset.state = state;
    el.heard.hidden = !(listening && snapshot.heard && !snapshot.playing);

    el.hitMeta.hidden = !hit;
    el.subline.hidden = !!hit;

    if (hit) {
      const tier = ['L1', 'L2', 'L3'].indexOf(hit.tier) >= 0 ? hit.tier : 'L1';
      el.hitTier.className = 'tier tier--' + tier;
      el.hitTier.textContent = tier;
      setText(el.hitScore, num(hit.score, 2, '0.00'));
      setText(el.hitLatency, String(Math.round(Number(hit.latency) || 0)));
      setText(el.hitTime, fmtClock(hit.ts));
      el.hitDemo.hidden = !hit.demo;
    }

    // 学习态
    el.learn.hidden = !learning;
    el.actions.hidden = !!learning;
    el.learnHint.hidden = !!learning;

    if (learning) {
      const total = Math.max(1, learning.total || 1);
      const current = Math.max(1, learning.take || 1);
      const segments = [];
      for (let i = 1; i <= total; i += 1) {
        const cls = i < current ? 'is-done' : i === current ? 'is-current' : '';
        segments.push('<span class="' + cls + '"></span>');
      }
      el.learnSegments.innerHTML = segments.join('');
      const windowSec = learning.window || 15;
      const pct = clamp(((learning.remaining || 0) / windowSec) * 100, 0, 100);
      el.learnBar.style.width = pct.toFixed(1) + '%';
    }

    // 动作
    const noTemplates = !templates.count;
    el.btnListen.textContent = onAir ? '停止监听' : '开始监听';
    el.btnListen.disabled = !onAir && noTemplates;
    el.btnListen.title = el.btnListen.disabled ? '没有可用模板，请先学习门铃' : '';
    el.btnLearn.disabled = onAir || !!learning;
    el.btnLearn.title = onAir ? '先停止监听再学习' : '';

    const takes = config.takes || 3;
    setText($('#hint-takes'), String(takes));

    // 试听 chip
    $$('[data-preview]').forEach(function (button) {
      const key = button.dataset.preview;
      let playing = false;
      if (snapshot.playing) {
        playing = key === 'test' ? !!snapshot.playing.test : snapshot.playing.cls === key;
      }
      button.classList.toggle('is-playing', playing);
      button.disabled = !!learning;
    });

    // 标题
    const nextTitle = hit ? hitLabel(hit) + '！· ' + BASE_TITLE : BASE_TITLE;
    if (document.title !== nextTitle) document.title = nextTitle;
  }

  /* ------------------------------------------------------------------------
     事件
     ------------------------------------------------------------------------ */

  function tierClass(tier, hollow) {
    const safe = ['L1', 'L2', 'L3'].indexOf(tier) >= 0 ? tier : 'L1';
    return 'tier tier--' + safe + (hollow ? ' is-hollow' : '');
  }

  function rowHtml(event) {
    const hit = event.type === 'hit';
    return (
      '<span class="row__top">' +
        '<span class="row__time">' + esc(event.time || fmtClock(event.ts)) + '</span>' +
        '<span class="row__cls">' + esc(event.label) + '</span>' +
        '<span class="row__tags">' +
          '<span class="' + tierClass(event.tier, !hit) + '">' + esc(event.tier) + '</span>' +
          (event.demo ? '<span class="tag-demo">演示</span>' : '') +
        '</span>' +
      '</span>' +
      '<span class="row__meta">' +
        '<span>' + (hit ? '命中' : '忽略') + '</span>' +
        '<span>得分 <b>' + num(event.score, 2, '0.00') + '</b></span>' +
        '<span>延迟 <b>' + Math.round(Number(event.latency) || 0) + '</b> ms</span>' +
        '<span>时长 <b>' + num(event.dur, 2, '0.00') + '</b> s</span>' +
      '</span>'
    );
  }

  function makeRow(event, isNew) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'row' + (event.type === 'hit' ? '' : ' row--ignore') + (isNew ? ' is-new' : '');
    button.dataset.seq = String(event.seq);
    button.innerHTML = rowHtml(event);
    button.setAttribute('aria-label', (event.time || '') + ' ' + (event.label || '') + ' ' + (event.type === 'hit' ? '命中' : '忽略'));
    return button;
  }

  function renderEvents() {
    const list = el.railList;
    list.textContent = '';

    if (!S.events.length) {
      const empty = document.createElement('p');
      empty.className = 'rail__empty';
      empty.textContent = '还没有事件。开始监听后，每次判定都会出现在这里。';
      list.appendChild(empty);
    } else {
      const fragment = document.createDocumentFragment();
      S.events.forEach(function (event) { fragment.appendChild(makeRow(event, false)); });
      list.appendChild(fragment);
    }

    updateRailCount();
  }

  function updateRailCount() {
    const total = (S.snap && S.snap.events_total) || S.events.length;
    setText(el.railCount, S.events.length + ' 条');
    el.railMore.hidden = !(total > S.events.length);
    if (total > S.events.length) {
      el.railMore.textContent = '载入更早（共 ' + total + ' 条）';
    }
  }

  function addEvent(event) {
    if (!event) return;
    S.events.unshift(event);
    if (S.events.length > 300) S.events.pop();

    const empty = el.railList.querySelector('.rail__empty');
    if (empty) empty.remove();

    el.railList.insertBefore(makeRow(event, true), el.railList.firstElementChild);
    while (el.railList.children.length > 300) {
      el.railList.removeChild(el.railList.lastElementChild);
    }
    updateRailCount();
  }

  function addLog(entry) {
    if (!entry) return;
    S.logs.push(entry);
    if (S.logs.length > 300) S.logs.shift();
    if (S.view === 'records') appendLog(entry);
  }

  function loadOlder() {
    if (!S.events.length) return;
    const oldest = S.events[S.events.length - 1];
    el.railMore.disabled = true;
    el.railMore.textContent = '载入中…';

    bus.request('/api/events?before=' + encodeURIComponent(oldest.seq) + '&limit=40')
      .then(function (data) {
        const list = (data && data.events) || [];
        const known = new Set(S.events.map(function (e) { return e.seq; }));
        list.forEach(function (event) {
          if (!known.has(event.seq)) S.events.push(event);
        });
        renderEvents();
      })
      .catch(function (err) {
        toast(friendly(err), true);
        updateRailCount();
      })
      .finally(function () {
        el.railMore.disabled = false;
      });
  }

  /* ------------------------------------------------------------------------
     抽屉
     ------------------------------------------------------------------------ */

  const DRAWER_TITLES = { settings: '设置', iot: '上报', records: '记录', event: '事件详情' };

  function openDrawer(view, data) {
    if (view === 'event' && !data) return;
    S.view = view;
    S.viewData = data || null;
    S.lastFocus = document.activeElement;

    if (S.closeTimer) {
      clearTimeout(S.closeTimer);
      S.closeTimer = 0;
    }

    el.drawerTitle.textContent = DRAWER_TITLES[view] || '面板';
    renderDrawer();

    el.drawer.hidden = false;
    el.scrim.hidden = false;
    void el.drawer.offsetWidth;
    el.drawer.classList.add('is-open');
    el.scrim.classList.add('is-open');

    if (el.app) el.app.inert = true;
    el.drawerClose.focus();
  }

  function closeDrawer() {
    if (el.drawer.hidden) return;
    el.drawer.classList.remove('is-open');
    el.scrim.classList.remove('is-open');
    if (el.app) el.app.inert = false;

    S.view = null;
    S.viewData = null;

    S.closeTimer = window.setTimeout(function () {
      el.drawer.hidden = true;
      el.scrim.hidden = true;
      S.closeTimer = 0;
    }, 190);

    if (S.lastFocus && typeof S.lastFocus.focus === 'function') {
      try { S.lastFocus.focus(); } catch (_) {}
    }
  }

  function renderDrawer() {
    el.drawerFoot.hidden = true;
    el.drawerFoot.textContent = '';

    switch (S.view) {
      case 'settings': renderSettings(); break;
      case 'iot': renderIot(); break;
      case 'records': renderRecords(); break;
      case 'event': renderEventDetail(); break;
      default: break;
    }
  }

  function renderSettings() {
    const snapshot = S.snap;
    if (!snapshot) return;
    const config = snapshot.config || {};
    const devices = snapshot.devices || {};
    const templates = snapshot.templates || {};

    el.drawerBody.innerHTML =
      '<section class="section">' +
        '<h3 class="section__title">音频设备</h3>' +
        '<label class="field">' +
          '<span class="field__k">麦克风</span>' +
          '<select class="select" id="set-mic">' + deviceOptions(devices.inputs, config.mic_index) + '</select>' +
        '</label>' +
        '<label class="field">' +
          '<span class="field__k">播放设备</span>' +
          '<select class="select" id="set-speaker">' + deviceOptions(devices.outputs, config.speaker_index) + '</select>' +
          '<span class="field__hint">试听从电脑音箱出声；手机只负责显示，不会采集麦克风。</span>' +
        '</label>' +
      '</section>' +
      '<section class="section">' +
        '<h3 class="section__title">判定参数</h3>' +
        '<div class="field">' +
          '<span class="field__k">判定阈值 <output id="thr-out">' + num(config.threshold, 2, '0.60') + '</output></span>' +
          '<div class="range"><input type="range" id="set-threshold" min="0.40" max="0.95" step="0.01" value="' + (config.threshold || 0.6) + '" aria-label="判定阈值"></div>' +
          '<span class="field__hint">越高越严格。学习后阈值通常落在 0.50 到 0.62 之间。</span>' +
        '</div>' +
        '<div class="field">' +
          '<span class="field__k">门控 <output id="gate-out">' + (config.gate_db || 8) + ' dB</output></span>' +
          '<div class="range"><input type="range" id="set-gate" min="6" max="30" step="1" value="' + (config.gate_db || 8) + '" aria-label="门控"></div>' +
          '<span class="field__hint">比底噪高多少分贝才算一次声响。</span>' +
        '</div>' +
      '</section>' +
      '<section class="section">' +
        '<h3 class="section__title">学习</h3>' +
        '<label class="field">' +
          '<span class="field__k">学习次数</span>' +
          '<select class="select" id="set-takes">' + takesOptions(config.takes) + '</select>' +
          '<span class="field__hint">3 次以上可能让阈值低于 2 次的结果；只学到 1 次时阈值固定为 0.55。</span>' +
        '</label>' +
      '</section>' +
      '<section class="section">' +
        '<h3 class="section__title">模板</h3>' +
        '<p class="field__hint">当前 ' + (templates.count || 0) + ' 个模板' +
          (templates.skipped ? '，跳过 ' + templates.skipped + ' 个不像门铃的参考音' : '') + '。</p>' +
        '<ul class="tpl-list" id="tpl-list">' + templateRows(templates.names) + '</ul>' +
        '<button class="btn btn--ghost btn--sm" id="tpl-refresh" type="button">刷新模板</button>' +
      '</section>' +
      '<section class="section">' +
        '<h3 class="section__title">局域网与访问</h3>' +
        '<label class="switch">' +
          '<span class="switch__text">允许同一 Wi-Fi 的设备访问</span>' +
          '<input type="checkbox" id="set-lan" ' + (config.lan ? 'checked' : '') + '>' +
          '<span class="switch__track"></span>' +
        '</label>' +
        '<dl class="kv" style="margin-top:10px">' +
          '<dt>本机</dt><dd>' + esc(stripProto(snapshot.net && snapshot.net.local)) + '</dd>' +
          '<dt>局域网</dt><dd>' + esc(stripProto(snapshot.net && snapshot.net.lan)) + '</dd>' +
          '<dt>端口</dt><dd>' + esc(snapshot.net && snapshot.net.port || '—') + '</dd>' +
        '</dl>' +
        '<p class="section__note">手机打不开时，请在 Windows 防火墙中允许本程序。</p>' +
        '<label class="field" style="margin-top:14px">' +
          '<span class="field__k">访问口令</span>' +
          '<input class="input" type="password" id="set-password" autocomplete="new-password" placeholder="' +
            (config.password_set ? '已设置，留空表示不修改' : '未设置') + '">' +
          '<span class="field__hint">开启后，未带口令的访问会被拒绝；口令不会出现在地址栏或日志里。</span>' +
        '</label>' +
        (config.password_set ? '<button class="btn btn--ghost btn--sm" id="clear-password" type="button">清除口令</button>' : '') +
      '</section>';

    el.drawerFoot.hidden = false;
    el.drawerFoot.innerHTML =
      '<button class="btn btn--primary" id="save-settings" type="button">保存设置</button>' +
      '<span class="dirty" id="dirty" hidden>未保存</span>';

    const threshold = $('#set-threshold');
    const thresholdOut = $('#thr-out');
    const gate = $('#set-gate');
    const gateOut = $('#gate-out');

    threshold.addEventListener('input', function () {
      thresholdOut.textContent = Number(threshold.value).toFixed(2);
      markDirty();
    });
    gate.addEventListener('input', function () {
      gateOut.textContent = gate.value + ' dB';
      markDirty();
    });

    ['set-mic', 'set-speaker', 'set-takes'].forEach(function (id) {
      const node = $('#' + id);
      if (node) node.addEventListener('change', markDirty);
    });
    const lan = $('#set-lan');
    if (lan) lan.addEventListener('change', markDirty);

    $('#save-settings').addEventListener('click', saveSettings);

    const refresh = $('#tpl-refresh');
    if (refresh) {
      refresh.addEventListener('click', function () {
        refresh.disabled = true;
        post('/api/templates/refresh', {}).then(function () {
          toast('模板已刷新');
          renderSettings();
        }).catch(function () { refresh.disabled = false; });
      });
    }

    const list = $('#tpl-list');
    if (list) {
      list.addEventListener('click', function (event) {
        const button = event.target.closest('[data-template]');
        if (button) previewTemplate(button.dataset.template);
      });
    }

    const clear = $('#clear-password');
    if (clear) {
      clear.addEventListener('click', function () {
        post('/api/settings', { clear_password: true }).then(function () {
          toast('口令已清除');
          renderSettings();
        }).catch(function () {});
      });
    }
  }

  function deviceOptions(list, selected) {
    if (!list || !list.length) return '<option value="-1">（未找到设备）</option>';
    return list.map(function (device) {
      const value = device.index == null ? -1 : device.index;
      return '<option value="' + esc(value) + '"' + (value === selected ? ' selected' : '') + '>' +
        esc(device.name) + '</option>';
    }).join('');
  }

  function takesOptions(selected) {
    const current = selected || 3;
    return [1, 2, 3, 4, 5].map(function (n) {
      return '<option value="' + n + '"' + (n === current ? ' selected' : '') + '>' + n + ' 次</option>';
    }).join('');
  }

  function templateRows(names) {
    if (!names || !names.length) {
      return '<li class="field__hint" style="padding:8px 0">没有可用模板，先学习一次门铃。</li>';
    }
    return names.map(function (name) {
      return '<li class="tpl"><span class="tpl__name">' + esc(name) + '</span>' +
        '<button class="tpl__play" type="button" data-template="' + esc(name) + '" aria-label="播放 ' + esc(name) + '">▶</button></li>';
    }).join('');
  }

  function markDirty() {
    const dirty = $('#dirty');
    if (dirty) dirty.hidden = false;
  }

  function saveSettings() {
    const payload = {
      threshold: Number($('#set-threshold').value),
      gate_db: Number($('#set-gate').value),
      takes: Number($('#set-takes').value),
      mic_index: Number($('#set-mic').value),
      speaker_index: Number($('#set-speaker').value),
      lan: $('#set-lan').checked
    };
    const password = $('#set-password');
    if (password && password.value) payload.password = password.value;

    post('/api/settings', payload).then(function () {
      const dirty = $('#dirty');
      if (dirty) dirty.hidden = true;
      toast('设置已保存');
    }).catch(function () {});
  }

  function renderIot() {
    const snapshot = S.snap;
    if (!snapshot) return;
    const iot = snapshot.iot || {};
    const config = iot.config || {};
    const board = snapshot.board || {};

    el.drawerBody.innerHTML =
      '<section class="section">' +
        '<h3 class="section__title">连接状态</h3>' +
        '<p class="iot-state-line"><span class="dot" id="iot-state-dot" data-state="' + esc(iot.state || 'unset') + '"></span>' +
          '<b id="iot-state-text">' + esc(IOT_LABEL[iot.state] || IOT_LABEL.unset) + '</b></p>' +
        '<dl class="kv">' +
          '<dt>板子</dt><dd id="board-online">' + (board.online ? '在线' : '离线') + '</dd>' +
          '<dt>最后成功</dt><dd id="iot-last-ok">' + (iot.last_ok ? fmtClock(iot.last_ok) : '—') + '</dd>' +
          '<dt>已发送</dt><dd id="iot-sent">' + (iot.sent || 0) + '</dd>' +
          '<dt>已丢弃</dt><dd id="iot-dropped">' + (iot.dropped || 0) + '</dd>' +
        '</dl>' +
        (iot.last_error ? '<p class="field__err" id="iot-error">' + esc(iot.last_error) + '</p>' : '<p class="field__err" id="iot-error" hidden></p>') +
        '<button class="btn btn--ghost btn--sm" id="iot-test" type="button" style="margin-top:12px">测试连接</button>' +
        '<p class="section__note">上报状态异常不影响识别与事件记录。</p>' +
      '</section>' +
      '<section class="section">' +
        '<h3 class="section__title">上报配置</h3>' +
        '<div class="grid-2">' +
          '<label class="field"><span class="field__k">地址</span>' +
            '<input class="input" id="iot-address" value="' + esc(config.address || '') + '" placeholder="iot.dfrobot.com.cn"></label>' +
          '<label class="field"><span class="field__k">端口</span>' +
            '<input class="input" id="iot-port" type="number" min="1" max="65535" value="' + esc(config.port || 1883) + '"></label>' +
        '</div>' +
        '<label class="field"><span class="field__k">Iot_id</span>' +
          '<input class="input" id="iot-id" value="' + esc(config.iot_id || '') + '" autocomplete="off"></label>' +
        '<label class="field"><span class="field__k">Iot_pwd</span>' +
          '<input class="input" id="iot-pwd" type="password" autocomplete="new-password" placeholder="' +
            (config.password_set ? '已设置，留空表示不修改' : '未设置') + '"></label>' +
        '<label class="field"><span class="field__k">事件主题</span>' +
          '<input class="input" id="iot-topic-event" value="' + esc(config.topic_event || '') + '"></label>' +
        '<label class="field"><span class="field__k">状态主题</span>' +
          '<input class="input" id="iot-topic-state" value="' + esc(config.topic_state || '') + '"></label>' +
        '<label class="switch"><span class="switch__text">启用上报</span>' +
          '<input type="checkbox" id="iot-enabled" ' + (config.enabled ? 'checked' : '') + '><span class="switch__track"></span></label>' +
        '<label class="field" style="margin-top:14px"><span class="field__k">QoS</span>' +
          '<select class="select" id="iot-qos">' +
            '<option value="0"' + (config.qos === 0 ? ' selected' : '') + '>0</option>' +
            '<option value="1"' + (config.qos !== 0 ? ' selected' : '') + '>1</option>' +
          '</select>' +
          '<span class="field__hint">QoS 只影响本机侧；平台端到端为至多一次。</span>' +
        '</label>' +
        '<label class="switch"><span class="switch__text">上报忽略事件</span>' +
          '<input type="checkbox" id="iot-report-ignore" ' + (config.report_ignore ? 'checked' : '') + '><span class="switch__track"></span></label>' +
      '</section>';

    el.drawerFoot.hidden = false;
    el.drawerFoot.innerHTML = '<button class="btn btn--primary" id="save-iot" type="button">保存上报设置</button>';

    $('#save-iot').addEventListener('click', saveIot);
    $('#iot-test').addEventListener('click', function () {
      const button = $('#iot-test');
      button.disabled = true;
      button.textContent = '测试中…';
      post('/api/iot/test', {}).then(function (result) {
        toast(result && result.ok ? '连接成功' : '连接失败：' + ((result && result.error) || '未知原因'), !(result && result.ok));
      }).catch(function () {
        /* toast already shown */
      }).finally(function () {
        button.disabled = false;
        button.textContent = '测试连接';
      });
    });
  }

  function saveIot() {
    const payload = {
      address: $('#iot-address').value.trim(),
      port: Number($('#iot-port').value),
      iot_id: $('#iot-id').value.trim(),
      topic_event: $('#iot-topic-event').value.trim(),
      topic_state: $('#iot-topic-state').value.trim(),
      enabled: $('#iot-enabled').checked,
      qos: Number($('#iot-qos').value),
      report_ignore: $('#iot-report-ignore').checked
    };
    const pwd = $('#iot-pwd').value;
    if (pwd) payload.password = pwd;

    post('/api/iot/config', payload).then(function () {
      toast('上报设置已保存');
    }).catch(function () {});
  }

  function renderRecords() {
    const snapshot = S.snap;
    if (!snapshot) return;
    const stats = snapshot.stats || {};
    const byClass = stats.by_class || {};
    const labels = { doorbell: '门铃', knock: '敲门', kettle: '水壶鸣笛', fire: '火警警报' };

    el.drawerBody.innerHTML =
      '<section class="section">' +
        '<h3 class="section__title">本次统计</h3>' +
        '<div class="stats">' +
          '<div class="stat"><b id="stat-hit">' + (stats.hit || 0) + '</b><span>命中</span></div>' +
          '<div class="stat"><b id="stat-ignore">' + (stats.ignore || 0) + '</b><span>忽略</span></div>' +
          '<div class="stat"><b id="stat-demo">' + (stats.demo || 0) + '</b><span>演示命中</span></div>' +
        '</div>' +
        '<dl class="kv" style="margin-top:14px">' +
          '<dt>平均延迟</dt><dd id="stat-latency">' + (stats.avg_latency == null ? '—' : Math.round(stats.avg_latency) + ' ms') + '</dd>' +
          '<dt>最高得分</dt><dd id="stat-score">' + (stats.max_score == null ? '—' : num(stats.max_score, 2)) + '</dd>' +
          Object.keys(labels).map(function (key) {
            return '<dt>' + labels[key] + '</dt><dd id="stat-cls-' + key + '">' + (byClass[key] || 0) + '</dd>';
          }).join('') +
        '</dl>' +
        '<button class="btn btn--ghost btn--sm" id="export-csv" type="button" style="margin-top:14px">导出 CSV</button>' +
        '<p class="section__note">只统计本次运行；心跳、连接事件与学习过程不计入。</p>' +
      '</section>' +
      '<section class="section">' +
        '<h3 class="section__title">事件录音</h3>' +
        '<p class="field__hint">' + (snapshot.rec_count || 0) + ' 个文件，最多保留 300 个。在事件详情里播放或下载。</p>' +
        '<button class="btn btn--danger btn--sm" id="clear-rec" type="button" style="margin-top:10px">清空录音</button>' +
      '</section>' +
      '<section class="section">' +
        '<h3 class="section__title">系统日志</h3>' +
        '<div class="logs" id="logs"></div>' +
      '</section>';

    el.drawerFoot.hidden = true;
    renderLogs();

    const exportButton = $('#export-csv');
    if (exportButton) {
      exportButton.addEventListener('click', exportCsv);
    }

    const clear = $('#clear-rec');
    if (clear) {
      let armed = false;
      let timer = 0;
      clear.addEventListener('click', function () {
        if (!armed) {
          armed = true;
          clear.textContent = '确认清空？';
          clear.classList.add('is-armed');
          timer = window.setTimeout(function () {
            armed = false;
            clear.textContent = '清空录音';
            clear.classList.remove('is-armed');
          }, 4000);
          return;
        }
        clearTimeout(timer);
        post('/api/recordings/clear', {}).then(function () {
          toast('录音已清空');
          clear.textContent = '清空录音';
          clear.classList.remove('is-armed');
          armed = false;
          return bus.request('/api/state').then(applySnapshot);
        }).catch(function () {
          armed = false;
          clear.textContent = '清空录音';
          clear.classList.remove('is-armed');
        });
      });
    }
  }

  function renderLogs() {
    const container = $('#logs');
    if (!container) return;
    container.textContent = '';
    const fragment = document.createDocumentFragment();
    S.logs.forEach(function (entry) {
      fragment.appendChild(logNode(entry));
    });
    container.appendChild(fragment);
    container.scrollTop = container.scrollHeight;
  }

  function logNode(entry) {
    const line = document.createElement('div');
    line.className = 'log';

    const time = document.createElement('time');
    time.textContent = entry.time || fmtClock(entry.ts);

    const text = document.createElement('span');
    text.textContent = entry.text || '';

    line.appendChild(time);
    line.appendChild(text);
    return line;
  }

  function appendLog(entry) {
    const container = $('#logs');
    if (!container) return;
    const nearBottom = container.scrollHeight - container.scrollTop - container.clientHeight < 40;
    container.appendChild(logNode(entry));
    while (container.children.length > 300) container.removeChild(container.firstElementChild);
    if (nearBottom) container.scrollTop = container.scrollHeight;
  }

  function exportCsv() {
    if (!IS_MOCK) {
      const link = document.createElement('a');
      link.href = API_BASE + '/api/export.csv';
      link.download = '';
      document.body.appendChild(link);
      link.click();
      link.remove();
      toast('正在导出 CSV');
      return;
    }

    const header = ['时间', '类型', '类别', 'tier', '是否演示', '得分', '置信度', '延迟ms', '时长s', '调性', '调制', '衰减', '稳定度', '主频比'];
    const rows = S.events.slice().reverse().map(function (e) {
      return [
        fmtFull(e.ts), e.type === 'hit' ? '命中' : '忽略', e.label || '', e.tier || '',
        e.demo ? '是' : '否', num(e.score, 2, ''), num(e.conf, 2, ''),
        Math.round(Number(e.latency) || 0), num(e.dur, 2, ''),
        num(e.tonal, 2, ''), num(e.mod, 2, ''), num(e.decay, 2, ''),
        num(e.stab, 2, ''), num(e.freq_ratio, 2, '')
      ].join(',');
    });
    const csv = '\uFEFF' + header.join(',') + '\r\n' + rows.join('\r\n');
    const blob = new Blob([csv], { type: 'text/csv;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = 'session_' + stamp() + '.csv';
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(function () { URL.revokeObjectURL(url); }, 2000);
    toast('已导出模拟数据 CSV');
  }

  function stamp() {
    const d = new Date();
    return (
      d.getFullYear() + pad2(d.getMonth() + 1) + pad2(d.getDate()) + '_' +
      pad2(d.getHours()) + pad2(d.getMinutes()) + pad2(d.getSeconds())
    );
  }

  function renderEventDetail() {
    const event = S.viewData;
    if (!event) return;
    const hit = event.type === 'hit';
    const tier = ['L1', 'L2', 'L3'].indexOf(event.tier) >= 0 ? event.tier : 'L1';

    const metrics = [
      ['调性', num(event.tonal, 2)],
      ['包络调制', num(event.mod, 2)],
      ['衰减', num(event.decay, 2)],
      ['音高稳定度', num(event.stab, 2)],
      ['主频比', num(event.freq_ratio, 2)],
      ['敲击数', event.n_knock == null ? '—' : event.n_knock],
      ['脉冲数', event.n_pulse == null ? '—' : event.n_pulse]
    ];

    const tplScores = Array.isArray(event.tpl_scores) ? event.tpl_scores : [];
    const trace = event.trace || {};

    el.drawerBody.innerHTML =
      '<div class="detail__head">' +
        '<span class="detail__cls">' + esc(event.label) + '</span>' +
        '<span class="' + tierClass(tier, !hit) + '">' + tier + '</span>' +
        (event.demo ? '<span class="tag-demo">演示</span>' : '') +
        '<span class="detail__time">' + fmtFull(event.ts) + '</span>' +
      '</div>' +
      (event.reason ? '<p class="detail__reason">' + esc(event.reason) + '</p>' : '') +
      '<section class="section">' +
        '<h3 class="section__title">判定</h3>' +
        '<dl class="kv">' +
          '<dt>结论</dt><dd>' + (hit ? '命中' : '忽略') + '</dd>' +
          '<dt>类别</dt><dd>' + esc(event.label) + '</dd>' +
          '<dt>等级</dt><dd>' + tier + '</dd>' +
          '<dt>得分</dt><dd>' + num(event.score, 2) + '</dd>' +
          '<dt>置信度</dt><dd>' + num(event.conf, 2) + '</dd>' +
          '<dt>延迟</dt><dd>' + Math.round(Number(event.latency) || 0) + ' ms</dd>' +
          '<dt>时长</dt><dd>' + num(event.dur, 2) + ' s</dd>' +
          '<dt>演示</dt><dd>' + (event.demo ? '是' : '否') + '</dd>' +
        '</dl>' +
      '</section>' +
      '<section class="section">' +
        '<h3 class="section__title">声学指标</h3>' +
        '<dl class="kv">' +
          metrics.map(function (pair) {
            return '<dt>' + pair[0] + '</dt><dd>' + pair[1] + '</dd>';
          }).join('') +
        '</dl>' +
      '</section>' +
      '<section class="section">' +
        '<h3 class="section__title">信号</h3>' +
        '<dl class="kv">' +
          '<dt>峰值</dt><dd>' + (event.peak_db == null ? '—' : num(event.peak_db, 1) + ' dB') + '</dd>' +
          '<dt>SNR</dt><dd>' + (event.snr == null ? '—' : num(event.snr, 1) + ' dB') + '</dd>' +
          '<dt>trace 最高分</dt><dd>' + num(trace.score, 2) + '</dd>' +
          '<dt>trace 时刻</dt><dd>' + (trace.at == null ? '—' : num(trace.at, 2) + ' s') + '</dd>' +
        '</dl>' +
      '</section>' +
      '<section class="section">' +
        '<h3 class="section__title">模板得分</h3>' +
        (tplScores.length
          ? '<dl class="kv">' + tplScores.map(function (pair) {
              return '<dt>' + esc(pair[0]) + '</dt><dd>' + num(pair[1], 2) + '</dd>';
            }).join('') + '</dl>'
          : '<p class="field__hint">没有模板得分记录。</p>') +
      '</section>' +
      '<section class="section">' +
        '<h3 class="section__title">录音</h3>' +
        (event.wav && !IS_MOCK
          ? '<audio class="detail__audio" controls preload="none" src="' + esc(API_BASE + event.wav) + '"></audio>' +
            '<p style="margin-top:10px"><a class="link" href="' + esc(API_BASE + event.wav) + '" download>下载录音</a></p>'
          : '<p class="field__hint">' + (IS_MOCK ? '模拟数据没有录音文件。' : '这次判定没有保存录音。') + '</p>') +
      '</section>';
  }

  function updateDrawerLive(snapshot) {
    if (S.view === 'records') {
      const stats = snapshot.stats || {};
      const byClass = stats.by_class || {};
      setText($('#stat-hit'), String(stats.hit || 0));
      setText($('#stat-ignore'), String(stats.ignore || 0));
      setText($('#stat-demo'), String(stats.demo || 0));
      setText($('#stat-latency'), stats.avg_latency == null ? '—' : Math.round(stats.avg_latency) + ' ms');
      setText($('#stat-score'), stats.max_score == null ? '—' : num(stats.max_score, 2));
      Object.keys(byClass).forEach(function (key) {
        setText($('#stat-cls-' + key), String(byClass[key]));
      });
    } else if (S.view === 'iot') {
      const iot = snapshot.iot || {};
      const board = snapshot.board || {};
      const dot = $('#iot-state-dot');
      if (dot) dot.dataset.state = iot.state || 'unset';
      setText($('#iot-state-text'), IOT_LABEL[iot.state] || IOT_LABEL.unset);
      setText($('#board-online'), board.online ? '在线' : '离线');
      setText($('#iot-last-ok'), iot.last_ok ? fmtClock(iot.last_ok) : '—');
      setText($('#iot-sent'), String(iot.sent || 0));
      setText($('#iot-dropped'), String(iot.dropped || 0));
      const error = $('#iot-error');
      if (error) {
        error.hidden = !iot.last_error;
        setText(error, iot.last_error || '');
      }
    }
  }

  /* ------------------------------------------------------------------------
     操作
     ------------------------------------------------------------------------ */

  function post(path, body) {
    return bus.request(path, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body || {})
    }).catch(function (err) {
      if (err && err.status === 401) showGate();
      else toast(friendly(err), true);
      throw err;
    });
  }

  function toggleListen() {
    const onAir = S.snap && (S.snap.on_air != null ? S.snap.on_air : S.snap.mode === 'listening');
    const on = !onAir;
    el.btnListen.disabled = true;
    post('/api/listen', { on: on }).then(function () {
      el.btnListen.disabled = false;
    }).catch(function () {
      el.btnListen.disabled = false;
    });
  }

  function startLearn() {
    const takes = (S.snap && S.snap.config && S.snap.config.takes) || 3;
    el.btnLearn.disabled = true;
    post('/api/learn', { action: 'start', takes: takes }).then(function () {
      el.btnLearn.disabled = false;
    }).catch(function () {
      el.btnLearn.disabled = false;
    });
  }

  function cancelLearn() {
    post('/api/learn', { action: 'cancel' }).catch(function () {});
  }

  function preview(key) {
    if (key === 'test') post('/api/preview', { test: true }).catch(function () {});
    else post('/api/preview', { cls: key }).catch(function () {});
  }

  function previewTemplate(file) {
    post('/api/preview', { file: file }).catch(function () {});
  }

  function copyAddress() {
    const lan = (S.snap && S.snap.net && S.snap.net.lan) || '';
    if (!lan) return;

    const fallback = function () {
      const area = document.createElement('textarea');
      area.value = lan;
      area.setAttribute('readonly', '');
      area.style.position = 'fixed';
      area.style.opacity = '0';
      document.body.appendChild(area);
      area.select();
      let ok = false;
      try { ok = document.execCommand('copy'); } catch (_) { ok = false; }
      area.remove();
      toast(ok ? '局域网地址已复制' : '复制失败，请手动输入', !ok);
    };

    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(lan).then(function () {
        toast('局域网地址已复制');
      }).catch(fallback);
    } else {
      fallback();
    }
  }

  function toast(message, isError) {
    el.toast.textContent = message;
    el.toast.classList.toggle('is-error', !!isError);
    el.toast.hidden = false;
    clearTimeout(S.toastTimer);
    S.toastTimer = window.setTimeout(function () {
      el.toast.hidden = true;
    }, isError ? 5000 : 2800);
  }

  function showNotice(notice) {
    if (!notice || !notice.title) return;
    el.stateWord.textContent = notice.title;
    if (notice.detail) setText(el.subline, notice.detail);
  }

  /* ------------------------------------------------------------------------
     口令
     ------------------------------------------------------------------------ */

  function showGate() {
    el.gate.hidden = false;
    if (el.app) el.app.inert = true;
    el.gateError.hidden = true;
    el.gatePassword.value = '';
    window.setTimeout(function () { el.gatePassword.focus(); }, 30);
  }

  function submitGate(event) {
    event.preventDefault();
    const value = el.gatePassword.value;
    if (!value) return;

    bus.request('/api/auth', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ password: value })
    }).then(function () {
      el.gate.hidden = true;
      el.gateError.hidden = true;
      if (el.app) el.app.inert = false;
      return bus.request('/api/state');
    }).then(function (snapshot) {
      applySnapshot(snapshot);
    }).catch(function (err) {
      el.gateError.textContent = err && err.status === 401 ? '口令不正确' : '连接不上服务，请稍后重试';
      el.gateError.hidden = false;
      el.gatePassword.select();
    });
  }

  /* ------------------------------------------------------------------------
     事件绑定
     ------------------------------------------------------------------------ */

  function wireEvents() {
    el.btnListen.addEventListener('click', toggleListen);
    el.btnLearn.addEventListener('click', startLearn);
    el.btnLearnCancel.addEventListener('click', cancelLearn);
    el.addrCopy.addEventListener('click', copyAddress);
    el.drawerClose.addEventListener('click', closeDrawer);
    el.scrim.addEventListener('click', closeDrawer);
    el.gateForm.addEventListener('submit', submitGate);
    el.railMore.addEventListener('click', loadOlder);

    document.addEventListener('click', function (event) {
      const opener = event.target.closest('[data-open]');
      if (opener) openDrawer(opener.dataset.open);
    });

    document.addEventListener('click', function (event) {
      const button = event.target.closest('[data-preview]');
      if (button && !button.disabled) preview(button.dataset.preview);
    });

    el.railList.addEventListener('click', function (event) {
      const row = event.target.closest('.row');
      if (!row) return;
      const seq = Number(row.dataset.seq);
      const found = S.events.find(function (item) { return Number(item.seq) === seq; });
      if (found) openDrawer('event', found);
    });

    document.addEventListener('keydown', function (event) {
      if (event.key !== 'Escape') return;
      if (!el.drawer.hidden) closeDrawer();
    });

    el.drawer.addEventListener('keydown', function (event) {
      if (event.key !== 'Tab') return;
      const focusable = $$('button, [href], input, select, textarea, audio, [tabindex]:not([tabindex="-1"])', el.drawer)
        .filter(function (node) { return !node.disabled && node.offsetParent !== null; });
      if (!focusable.length) return;

      const first = focusable[0];
      const last = focusable[focusable.length - 1];

      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    });
  }

  boot();
})();
