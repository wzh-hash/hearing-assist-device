"""板子侧链路状态机（IOT-5、IOT-6 的固件部分）。

与硬件无关：它只依赖一个很小的客户端协议和一个"界面"协议，所以能在 PC 上
用假客户端 + 假界面完整测试（`tools/board_selftest.py`）。真正碰 K10 的代码
只有 `k10_mqtt.py` 与 `ui_k10.py` 两个薄适配器。

调用方（`main.py`）只需要：

    link = BoardLink(K10Mqtt, K10Ui(), topic_event, topic_status=..., device="k10-01")
    link.run()          # 死循环，永不返回

接口里写明的约束：

- `step()` 一次只做一次推进：重连、收消息、发心跳、看门狗、界面动画
- 同一 `ts` 只处理一次（1 秒窗口）；不同 `ts` 一律重新触发（IOT-6.10、IOT-6.6）
- 短码没有 `ts`，用 300 毫秒同类去抖（`protocol.SHORT_DEBOUNCE_MS`）
- 心跳 30 秒一次；连续 3 次失败或 45 秒无成功 → 断开重连（IOT-5.5）
- 重连退避 1→16 秒（IOT-2.2 的固件侧同样适用）
- 云端断开时界面显示「云端断开」，恢复后回到最近事件（IOT-6.7）
- 任何异常都不得让主循环退出（NFR-IOT-3 的板子侧版本）
"""

import time

import protocol


def _ticks_ms():
    """单调毫秒。MicroPython 用 ticks_ms；PC 与裁剪固件各留一条退路。"""
    ticks = getattr(time, "ticks_ms", None)
    if ticks is not None:
        return ticks()
    monotonic = getattr(time, "monotonic", None)
    if monotonic is not None:
        return int(monotonic() * 1000)
    return int(time.time() * 1000)


def _sleep_ms(ms):
    sleeper = getattr(time, "sleep_ms", None)
    if sleeper is not None:
        sleeper(ms)
    else:  # pragma: no cover - PC
        time.sleep(ms / 1000.0)


def _wall():
    return time.time()


class BoardLink:
    """一条深接口：连接、订阅、心跳、看门狗、去重、界面切换全在里面。"""

    def __init__(
        self,
        client_factory,
        ui,
        topic_event,
        *,
        topic_status="",
        device="k10-01",
        heartbeat_ms=30000,
        watchdog_ms=45000,
        max_backoff_ms=16000,
        clock_ms=None,
        sleep_ms=None,
        wall=None,
        log=None,
    ):
        if not topic_event:
            raise ValueError("事件主题不能为空")
        self._factory = client_factory
        self._ui = ui
        self._topic_event = topic_event
        self._topic_status = topic_status or ""
        self._device = device
        self._heartbeat_ms = heartbeat_ms
        self._watchdog_ms = watchdog_ms
        self._max_backoff_ms = max_backoff_ms
        self._clock = clock_ms or _ticks_ms
        self._sleep = sleep_ms or _sleep_ms
        self._wall = wall or _wall
        self._log = log or (lambda message: print("[iot] " + message))

        self._client = None
        self._connected = False
        self._retry_index = 0
        self._next_retry_ms = 0
        self._last_hb_ms = 0
        self._ok_ms = 0
        self._fails = 0
        self._last_ts = None
        self._last_ts_seen_ms = 0
        self._last_short_cls = None
        self._last_short_ms = 0
        self.last_event = None

    # ---------- 只读状态（给页面/日志看，不在主循环里被依赖） ----------

    @property
    def connected(self):
        return self._connected

    def state(self):
        return {
            "connected": self._connected,
            "retry_index": self._retry_index,
            "fails": self._fails,
            "last_event": self.last_event,
        }

    # ---------- 生命周期 ----------

    def open(self):
        """建立一次连接并订阅；失败返回 False，由 `step()` 退避重试。"""
        client = None
        try:
            client = self._factory(self.handle)
            if not client.connect():
                raise OSError("connect failed")
            if not client.subscribe(self._topic_event):
                raise OSError("subscribe failed")
        except Exception:
            if client is not None:
                try:
                    client.disconnect()
                except Exception:
                    pass
            self._connected = False
            self._client = None
            self._ui.set_cloud(False)
            return False

        self._client = client
        self._connected = True
        self._retry_index = 0
        self._fails = 0
        self._last_hb_ms = -self._watchdog_ms  # 连上立刻发第一拍，方便上位机判在线
        self._ok_ms = self._clock()
        self._ui.set_cloud(True)
        self._log("云端已连接")
        return True

    def close(self):
        self._connected = False
        client, self._client = self._client, None
        if client is not None:
            try:
                client.disconnect()
            except Exception:
                pass
        try:
            self._ui.set_cloud(False)
        except Exception:
            pass

    def run(self):
        """主循环。板子重启后由 `main.py` 调用，不该返回。"""
        while True:
            try:
                self.step()
            except Exception as exc:  # 绝不让主循环死掉
                self._log("主循环异常：%s" % exc)
                try:
                    self._lose("主循环异常")
                except Exception:
                    pass
            self._sleep(20)

    # ---------- 单步推进 ----------

    def step(self):
        now = self._clock()

        if not self._connected:
            if now < self._next_retry_ms:
                try:
                    self._ui.tick()
                except Exception:
                    pass
                return
            if not self.open():
                self._schedule_retry(now)
                try:
                    self._ui.tick()
                except Exception:
                    pass
                return
            # 连上了就继续往下走，同一步里把第一拍心跳发出去，
            # 上位机不用等 30 秒就能判到"板子在线"。

        try:
            self._client.poll()
        except Exception as exc:
            self._log("网络异常：%s" % exc)
            self._lose("网络异常")
            return

        if not self._client.is_connected():
            self._lose("连接断开")
            return

        if self._topic_status and (now - self._last_hb_ms) >= self._heartbeat_ms:
            self._send_heartbeat(now)

        # 看门狗只在配了状态主题时成立：没配就没有心跳，也就没有"心跳失败"可言
        # （IOT-4.3：未填状态主题只关闭心跳与在线检测，不得影响事件上报）。
        if self._topic_status and (self._fails >= 3 or (now - self._ok_ms) >= self._watchdog_ms):
            self._lose("心跳连续失败")
            return

        try:
            self._ui.tick()
        except Exception:
            pass

    # ---------- 收到消息（由适配器回调） ----------

    def handle(self, topic, data):
        try:
            event = protocol.decode(data)
            if event is None:
                self._log("收到无法识别的消息，已忽略")
                return
            if not self._accept(event, self._clock()):
                return
            self.last_event = event
            self._ui.show(event)
            self._log("触发 %s%s" % (event["cls"], "（演示）" if event["demo"] else ""))
        except Exception as exc:
            self._log("处理消息异常：%s" % exc)

    def _accept(self, event, now):
        """去重：同一 ts 一秒内只算一次；短码按同类 300 毫秒去抖。"""
        ts = event.get("ts")
        if ts is None:
            cls = event["cls"]
            if self._last_short_cls == cls and (now - self._last_short_ms) < protocol.SHORT_DEBOUNCE_MS:
                return False
            self._last_short_cls = cls
            self._last_short_ms = now
            return True

        if self._last_ts is not None and abs(ts - self._last_ts) < 0.001 and (now - self._last_ts_seen_ms) <= 1000:
            return False
        self._last_ts = ts
        self._last_ts_seen_ms = now
        return True

    # ---------- 心跳与重连 ----------

    def _send_heartbeat(self, now):
        self._last_hb_ms = now
        try:
            data = protocol.heartbeat_bytes(self._device, self._wall())
            ok = bool(self._client.publish(self._topic_status, data))
        except Exception as exc:
            ok = False
            self._log("心跳异常：%s" % exc)
        if ok:
            self._fails = 0
            self._ok_ms = now
        else:
            self._fails += 1
            self._log("心跳发送失败（%d）" % self._fails)

    def _lose(self, reason):
        self._log("云端断开：%s" % reason)
        self.close()
        self._schedule_retry(self._clock())

    def _schedule_retry(self, now):
        index = self._retry_index
        self._retry_index = index + 1
        delay = 1000 * (2 ** min(index, 4))
        if delay > self._max_backoff_ms:
            delay = self._max_backoff_ms
        self._next_retry_ms = now + delay

    # ---------- 给测试用的合成入口 ----------

    def inject(self, data):
        """绕过网络直接喂一条载荷，等价于适配器回调（仅测试/联调用）。"""
        self.handle(self._topic_event, data)
