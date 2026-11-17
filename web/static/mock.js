/* ==========================================================================
   听障人士感知器 · 本地模拟总线（?mock=1）
   只用于脱离 Python 的界面联调；页面必须显式标注「模拟数据」。
   ========================================================================== */

'use strict';

(function () {
  const CLASSES = {
    doorbell: { label: '门铃', tier: 'L1' },
    knock: { label: '敲门', tier: 'L1' },
    kettle: { label: '水壶鸣笛', tier: 'L2' },
    fire: { label: '火警警报', tier: 'L3' }
  };

  const pad2 = (n) => String(n).padStart(2, '0');

  function clock(ts) {
    const d = new Date(ts);
    return pad2(d.getHours()) + ':' + pad2(d.getMinutes()) + ':' + pad2(d.getSeconds());
  }

  function rand(min, max) {
    return min + Math.random() * (max - min);
  }

  function now() {
    return Date.now();
  }

  window.createMockBus = function createMockBus() {
    const hub = {
      seq: 0,
      events: [],
      logs: [],
      templates: {
        count: 4,
        names: ['doorbell.wav', 'knock.wav', 'kettle.wav', 'fire.wav'],
        skipped: 0
      },
      config: {
        threshold: 0.6,
        gate_db: 8,
        mic_index: 0,
        speaker_index: 1,
        takes: 3,
        lan: true,
        password_set: false
      },
      devices: {
        inputs: [
          { index: 0, name: '系统默认（麦克风阵列）' },
          { index: 1, name: '麦克风阵列 (Realtek Audio)' }
        ],
        outputs: [
          { index: 0, name: '系统默认' },
          { index: 1, name: '扬声器 (Realtek Audio)' }
        ]
      },
      mode: 'idle',
      stopped: false,
      heard: false,
      floor: -61.5,
      level: null,
      env: 'quiet',
      last_hit: null,
      hit: null,
      before_hit: 'listening',
      learn: null,
      playing: null,
      notice: null,
      cooldown: 0,
      spike_until: 0,
      pending_hit: 0,
      next_spike: 0,
      iot: {
        state: 'unset',
        last_ok: null,
        last_error: '',
        sent: 0,
        dropped: 0,
        config: {
          address: 'iot.dfrobot.com.cn',
          port: 1883,
          iot_id: '',
          topic_event: '',
          topic_state: '',
          enabled: false,
          qos: 1,
          report_ignore: false,
          password_set: false
        }
      },
      board: { online: false },
      net: { local: 'http://127.0.0.1:8765', lan: 'http://192.168.1.23:8765', port: 8765 },
      onMessage: null,
      timer: 0,
      auth: false
    };

    /* ---------------------------------------------------------------------
       种子数据
       --------------------------------------------------------------------- */

    function makeEvent(cls, preview, agoMs, ignore) {
      const info = CLASSES[cls] || CLASSES.doorbell;
      const ts = now() - agoMs;
      const score = ignore ? rand(0.36, 0.54) : rand(0.68, 0.94);
      hub.seq += 1;

      return {
        seq: hub.seq,
        ts: ts,
        time: clock(ts),
        type: ignore ? 'ignore' : 'hit',
        cls: cls,
        label: info.label,
        tier: info.tier,
        score: score,
        conf: Math.min(0.99, Math.max(0.2, score + rand(-0.08, 0.08))),
        latency: Math.round(rand(320, 760)),
        dur: Number(rand(0.45, 1.6).toFixed(2)),
        preview: !!preview,
        tonal: Number(rand(0.2, 0.85).toFixed(2)),
        mod: Number(rand(0.1, 0.8).toFixed(2)),
        decay: Number(rand(0.1, 0.7).toFixed(2)),
        stab: Number(rand(0.2, 0.9).toFixed(2)),
        freq_ratio: Number(rand(0.85, 1.25).toFixed(2)),
        n_knock: cls === 'knock' ? Math.round(rand(2, 4)) : 0,
        n_pulse: cls === 'fire' || cls === 'kettle' ? Math.round(rand(3, 9)) : 0,
        peak_db: Number(rand(-30, -12).toFixed(1)),
        snr: Number(rand(16, 34).toFixed(1)),
        trace: { score: Math.min(0.99, Number((score + 0.03).toFixed(2))), at: Number(rand(0.2, 0.8).toFixed(2)) },
        tpl_scores: [[cls + '.wav', Number(score.toFixed(2))]].concat(
          Object.keys(CLASSES)
            .filter((key) => key !== cls)
            .slice(0, 2)
            .map((key) => [key + '.wav', Number(rand(0.05, 0.45).toFixed(2))])
        ),
        reason: ignore ? '得分 ' + score.toFixed(2) + ' 低于阈值 ' + hub.config.threshold.toFixed(2) : null,
        wav: null
      };
    }

    (function seed() {
      const seeds = [
        ['doorbell', false, 45 * 1000, false],
        ['knock', false, 4 * 60 * 1000, false],
        ['doorbell', false, 7 * 60 * 1000, true],
        ['kettle', false, 13 * 60 * 1000, false],
        ['fire', true, 21 * 60 * 1000, false],
        ['doorbell', false, 29 * 60 * 1000, false],
        ['knock', false, 38 * 60 * 1000, true],
        ['kettle', false, 47 * 60 * 1000, false],
        ['doorbell', true, 55 * 60 * 1000, false],
        ['doorbell', false, 66 * 60 * 1000, false],
        ['knock', false, 78 * 60 * 1000, false],
        ['doorbell', false, 91 * 60 * 1000, true]
      ];
      seeds.forEach(function (seed) {
        hub.events.push(makeEvent(seed[0], seed[1], seed[2], seed[3]));
      });
      hub.logs = [
        { ts: now() - 95 * 60 * 1000, time: clock(now() - 95 * 60 * 1000), text: '服务已启动，加载 4 个内置模板' },
        { ts: now() - 91 * 60 * 1000, time: clock(now() - 91 * 60 * 1000), text: '忽略一次：得分低于阈值' },
        { ts: now() - 45 * 60 * 1000, time: clock(now() - 45 * 60 * 1000), text: '命中门铃，得分 0.87' }
      ];
    })();

    /* ---------------------------------------------------------------------
       统计 / 快照
       --------------------------------------------------------------------- */

    function stats() {
      let hit = 0;
      let ignore = 0;
      let preview = 0;
      let latency = 0;
      let latencyCount = 0;
      let maxScore = null;
      const byClass = { doorbell: 0, knock: 0, kettle: 0, fire: 0 };

      hub.events.forEach(function (event) {
        if (event.type === 'hit') {
          hit += 1;
          latency += event.latency;
          latencyCount += 1;
          maxScore = maxScore == null ? event.score : Math.max(maxScore, event.score);
          byClass[event.cls] = (byClass[event.cls] || 0) + 1;
        } else {
          ignore += 1;
        }
        if (event.preview) preview += 1;
      });

      return {
        hit: hit,
        ignore: ignore,
        preview: preview,
        avg_latency: latencyCount ? latency / latencyCount : null,
        max_score: maxScore,
        by_class: byClass
      };
    }

    function snapshot() {
      return {
        app: 'doorbell-web',
        mock: true,
        seq: hub.seq,
        now: now(),
        mode: hub.mode,
        on_air: hub.mode === 'listening' || (hub.mode === 'hit' && hub.before_hit === 'listening'),
        stopped: hub.stopped,
        heard: hub.heard,
        level_db: hub.level,
        floor_db: hub.floor,
        env: hub.env,
        templates: {
          count: hub.templates.count,
          names: hub.templates.names.slice(),
          skipped: hub.templates.skipped
        },
        last_hit: hub.last_hit,
        hit: hub.hit,
        learn: hub.learn
          ? {
              take: hub.learn.take,
              total: hub.learn.total,
              remaining: Math.max(0, (hub.learn.deadline - now()) / 1000),
              window: 15,
              attempts: hub.learn.attempts
            }
          : null,
        playing: hub.playing
          ? { cls: hub.playing.cls, label: hub.playing.label, test: hub.playing.test }
          : null,
        notice: hub.notice,
        iot: {
          state: hub.iot.state,
          last_ok: hub.iot.last_ok,
          last_error: hub.iot.last_error,
          sent: hub.iot.sent,
          dropped: hub.iot.dropped,
          config: {
            address: hub.iot.config.address,
            port: hub.iot.config.port,
            iot_id: hub.iot.config.iot_id,
            topic_event: hub.iot.config.topic_event,
            topic_state: hub.iot.config.topic_state,
            enabled: hub.iot.config.enabled,
            qos: hub.iot.config.qos,
            report_ignore: hub.iot.config.report_ignore,
            password_set: hub.iot.config.password_set
          }
        },
        board: { online: hub.board.online },
        stats: stats(),
        config: {
          threshold: hub.config.threshold,
          gate_db: hub.config.gate_db,
          mic_index: hub.config.mic_index,
          speaker_index: hub.config.speaker_index,
          takes: hub.config.takes,
          lan: hub.config.lan,
          password_set: hub.config.password_set
        },
        devices: hub.devices,
        net: hub.net,
        events_total: hub.events.length,
        rec_count: 0
      };
    }

    /* ---------------------------------------------------------------------
       内部动作
       --------------------------------------------------------------------- */

    function log(text) {
      const entry = { ts: now(), time: clock(now()), text: text };
      hub.logs.push(entry);
      if (hub.logs.length > 300) hub.logs.shift();
      emit({ type: 'log', data: entry });
    }

    function pushEvent(event) {
      hub.events.unshift(event);
      if (hub.events.length > 300) hub.events.pop();
      hub.last_hit = event;
      if (event.type === 'hit' && hub.iot.config.enabled) {
        hub.iot.sent += 1;
        hub.iot.last_ok = now();
      }
      emit({ type: 'event', data: event });
    }

    function emit(message) {
      if (hub.onMessage) hub.onMessage(message);
    }

    function fireHit(cls, preview) {
      const event = makeEvent(cls, preview, 0, false);
      event.wav = null;

      hub.before_hit = hub.mode === 'learning' ? 'idle' : hub.mode === 'hit' ? hub.before_hit : hub.mode;
      hub.mode = 'hit';
      hub.hit = Object.assign({}, event, { until: now() + 3500 });
      hub.heard = false;
      hub.playing = null;
      hub.pending_hit = 0;
      hub.cooldown = now() + 8000;

      pushEvent(event);
      log((preview ? '试听命中：' : '命中') + event.label + '，得分 ' + event.score.toFixed(2));
    }

    function startLearn(takes) {
      const total = Math.max(1, Math.min(5, Number(takes) || 3));
      hub.mode = 'learning';
      hub.learn = {
        take: 1,
        total: total,
        attempts: 0,
        deadline: now() + 15000,
        stepAt: now() + rand(2600, 5200)
      };
      hub.stopped = false;
      hub.level = hub.floor + 2;
      log('开始学习，共 ' + total + ' 次');
    }

    function finishLearn(ok) {
      if (!hub.learn) return;
      const total = hub.learn.total;
      hub.learn = null;
      hub.mode = 'idle';
      hub.playing = null;
      hub.level = null;

      if (ok) {
        hub.config.threshold = Number(rand(0.52, 0.62).toFixed(2));
        hub.notice = {
          kind: 'learn_done',
          title: '学习完成 ✓',
          detail: '新阈值 ' + hub.config.threshold.toFixed(2) + '，已可用于监听。',
          ts: now(),
          until: now() + 6000
        };
        log('学习完成，新阈值 ' + hub.config.threshold.toFixed(2));
      } else {
        hub.notice = {
          kind: 'learn_failed',
          title: '没有学到',
          detail: '没有录到有效门铃。原有模板和阈值保持不变。',
          ts: now(),
          until: now() + 6000
        };
        log('学习失败：没有录到有效门铃');
      }
    }

    function tickLearn(t) {
      const learn = hub.learn;
      if (!learn) return;

      if (t >= learn.deadline) {
        learn.attempts += 1;
        if (learn.attempts >= 8) {
          finishLearn(false);
          return;
        }
        learn.deadline = t + 15000;
        learn.stepAt = t + rand(2200, 4800);
        log('等待门铃超时，继续第 ' + learn.take + ' 次');
        return;
      }

      if (t >= learn.stepAt) {
        learn.take += 1;
        if (learn.take > learn.total) {
          finishLearn(true);
          return;
        }
        learn.deadline = t + 15000;
        learn.stepAt = t + rand(2400, 4800);
        log('已录到第 ' + (learn.take - 1) + ' 次');
      }
    }

    function tickAudio(t) {
      if (hub.mode === 'learning') {
        hub.level = hub.floor + rand(1, 5);
        hub.env = 'quiet';
        return;
      }

      if (hub.mode !== 'listening' && hub.mode !== 'hit') {
        hub.level = null;
        hub.heard = false;
        return;
      }

      // 底噪缓慢漂移
      hub.floor += rand(-0.25, 0.25);
      hub.floor = Math.max(-68, Math.min(-56, hub.floor));

      const gate = hub.floor + hub.config.gate_db;

      if (t >= hub.next_spike && hub.mode === 'listening' && !hub.playing) {
        hub.spike_until = t + rand(500, 900);
        hub.next_spike = t + rand(9000, 15000);
        if (t > hub.cooldown) hub.pending_hit = t + 420;
      }
      if (hub.next_spike === 0) hub.next_spike = t + rand(4000, 8000);

      if (t < hub.spike_until) {
        hub.level = Math.max(-30, Math.min(-14, gate + rand(2, 10)));
        hub.heard = true;
      } else {
        hub.level = hub.floor + rand(1.5, 5);
        hub.heard = hub.level > gate && hub.mode === 'listening';
      }

      if (hub.pending_hit && t >= hub.pending_hit) {
        const roll = Math.random();
        const cls = roll < 0.55 ? 'doorbell' : roll < 0.78 ? 'knock' : roll < 0.92 ? 'kettle' : 'fire';
        fireHit(cls, false);
      }

      hub.env = hub.level > -12 ? 'clip' : hub.level > -30 ? 'loud' : 'quiet';
    }

    function tick(t) {
      if (hub.hit && t >= hub.hit.until) {
        hub.hit = null;
        hub.mode = hub.before_hit === 'listening' ? 'listening' : 'idle';
      }
      if (hub.playing && t >= hub.playing.until) hub.playing = null;
      if (hub.notice && hub.notice.until && t >= hub.notice.until) hub.notice = null;

      tickLearn(t);
      tickAudio(t);

      emit({ type: 'state', data: snapshot() });
    }

    /* ---------------------------------------------------------------------
       HTTP 兼容层
       --------------------------------------------------------------------- */

    function fail(message, status) {
      const err = new Error(message);
      err.status = status || 400;
      return Promise.reject(err);
    }

    function handle(path, init) {
      const method = ((init && init.method) || 'GET').toUpperCase();
      const url = new URL(path, 'http://mock.local');
      const route = url.pathname;

      if (method === 'GET' && route === '/api/state') {
        const data = snapshot();
        data.events = hub.events.slice(0, 40);
        data.logs = hub.logs.slice(-300);
        return Promise.resolve(data);
      }

      if (method === 'GET' && route === '/api/events') {
        const before = Number(url.searchParams.get('before'));
        const limit = Math.max(1, Math.min(300, Number(url.searchParams.get('limit')) || 40));
        const pool = isFinite(before) && before > 0
          ? hub.events.filter(function (event) { return event.seq < before; })
          : hub.events;
        return Promise.resolve({ events: pool.slice(0, limit), total: hub.events.length });
      }

      if (method !== 'POST') return fail('不支持的请求', 405);

      let body = {};
      try { body = init && init.body ? JSON.parse(init.body) : {}; } catch (_) { body = {}; }

      if (route === '/api/listen') {
        if (body.on) {
          if (hub.mode === 'learning') return fail('正在学习，请先取消学习');
          if (!hub.templates.count) return fail('没有可用模板，请先学习门铃');
          hub.mode = 'listening';
          hub.stopped = false;
          hub.level = hub.floor + 3;
          hub.next_spike = now() + rand(5000, 9000);
          hub.cooldown = 0;
          log('开始监听');
        } else {
          hub.mode = 'idle';
          hub.stopped = true;
          hub.hit = null;
          hub.level = null;
          hub.heard = false;
          log('停止监听');
        }
        return Promise.resolve({ ok: true });
      }

      if (route === '/api/learn') {
        if (body.action === 'cancel') {
          if (hub.learn) log('取消学习');
          hub.learn = null;
          if (hub.mode === 'learning') hub.mode = 'idle';
          hub.level = null;
          return Promise.resolve({ ok: true });
        }
        if (hub.mode === 'learning') {
          return fail('另一台设备正在学习（' + hub.learn.take + '/' + hub.learn.total + '）', 409);
        }
        if (hub.mode === 'listening' || hub.mode === 'hit') return fail('请先停止监听再学习');
        startLearn(body.takes);
        return Promise.resolve({ ok: true, takes: hub.learn.total });
      }

      if (route === '/api/preview') {
        if (hub.mode === 'learning') return fail('学习进行中，暂时不能试听');
        const isTest = !!body.test;

        if (body.file) {
          const cls = /doorbell/.test(body.file) ? 'doorbell'
            : /knock/.test(body.file) ? 'knock'
            : /kettle/.test(body.file) ? 'kettle'
            : /fire/.test(body.file) ? 'fire' : 'doorbell';
          hub.playing = { cls: cls, label: CLASSES[cls].label, test: false, until: now() + 1600 };
          setTimeout(function () { fireHit(cls, true); }, 350);
          return Promise.resolve({ ok: true });
        }

        if (isTest) {
          hub.playing = { cls: 'test', label: '测试音', test: true, until: now() + 1000 };
          return Promise.resolve({ ok: true });
        }

        const cls = CLASSES[body.cls] ? body.cls : 'doorbell';
        hub.playing = { cls: cls, label: CLASSES[cls].label, test: false, until: now() + 1600 };
        setTimeout(function () { fireHit(cls, true); }, 350);
        return Promise.resolve({ ok: true });
      }

      if (route === '/api/settings') {
        if (body.clear_password) {
          hub.config.password_set = false;
          log('已清除访问口令');
          return Promise.resolve({ ok: true });
        }
        const threshold = Number(body.threshold);
        const gate = Number(body.gate_db);
        const takes = Number(body.takes);
        if (isFinite(threshold)) {
          if (threshold < 0.4 || threshold > 0.95) return fail('阈值需在 0.40 到 0.95 之间');
          hub.config.threshold = Math.round(threshold * 100) / 100;
        }
        if (isFinite(gate)) {
          if (gate < 6 || gate > 30) return fail('门控需在 6 到 30 dB 之间');
          hub.config.gate_db = Math.round(gate);
        }
        if (isFinite(takes)) {
          if (takes < 1 || takes > 5) return fail('学习次数需在 1 到 5 之间');
          hub.config.takes = Math.round(takes);
        }
        if (typeof body.mic_index === 'number') hub.config.mic_index = body.mic_index;
        if (typeof body.speaker_index === 'number') hub.config.speaker_index = body.speaker_index;
        if (typeof body.lan === 'boolean') hub.config.lan = body.lan;
        if (typeof body.password === 'string' && body.password) {
          hub.config.password_set = true;
        }
        log('设置已保存');
        return Promise.resolve({ ok: true });
      }

      if (route === '/api/templates/refresh') {
        hub.templates.names = ['doorbell.wav', 'knock.wav', 'kettle.wav', 'fire.wav'];
        hub.templates.count = 4;
        log('模板已刷新，共 4 个');
        return Promise.resolve({ ok: true });
      }

      if (route === '/api/iot/config') {
        const config = hub.iot.config;
        if (typeof body.address === 'string') config.address = body.address;
        if (isFinite(Number(body.port))) config.port = Number(body.port);
        if (typeof body.iot_id === 'string') config.iot_id = body.iot_id;
        if (typeof body.topic_event === 'string') config.topic_event = body.topic_event;
        if (typeof body.topic_state === 'string') config.topic_state = body.topic_state;
        if (typeof body.enabled === 'boolean') config.enabled = body.enabled;
        if (typeof body.qos === 'number') config.qos = body.qos === 0 ? 0 : 1;
        if (typeof body.report_ignore === 'boolean') config.report_ignore = body.report_ignore;
        if (typeof body.password === 'string' && body.password) config.password_set = true;

        if (config.enabled && (config.iot_id || config.address)) {
          hub.iot.state = 'connecting';
          log('正在连接上报平台');
          setTimeout(function () {
            hub.iot.state = 'online';
            hub.iot.last_ok = now();
            hub.iot.last_error = '';
            hub.board.online = true;
            log('上报平台已连接');
          }, 1200);
        } else if (!config.enabled) {
          hub.iot.state = 'unset';
          hub.iot.last_error = '';
        }
        log('上报设置已保存');
        return Promise.resolve({ ok: true });
      }

      if (route === '/api/iot/test') {
        hub.iot.state = 'connecting';
        log('测试上报连接');
        setTimeout(function () {
          hub.iot.state = 'online';
          hub.iot.last_ok = now();
          hub.board.online = true;
          log('测试连接成功');
        }, 900);
        return Promise.resolve({ ok: true });
      }

      if (route === '/api/recordings/clear') {
        log('已清空事件录音');
        return Promise.resolve({ ok: true });
      }

      if (route === '/api/auth') {
        return Promise.resolve({ ok: true });
      }

      return fail('不支持的接口：' + route, 404);
    }

    return {
      request: function (path, init) {
        try {
          return handle(path, init);
        } catch (err) {
          return Promise.reject(err);
        }
      },
      start: function (handler) {
        hub.onMessage = handler;
        if (hub.timer) return;
        hub.next_spike = now() + rand(4000, 8000);
        hub.timer = window.setInterval(function () { tick(now()); }, 125);
        tick(now());
      },
      stop: function () {
        if (hub.timer) clearInterval(hub.timer);
        hub.timer = 0;
        hub.onMessage = null;
      }
    };
  };
})();
