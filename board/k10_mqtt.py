"""K10 的 WiFi / MQTT 适配器（只在板子上导入）。

它只做一件事：把 `k10_base` 的 WiFi 与 MqttClient 包成 `BoardLink` 期望的
那个小接口（connect / subscribe / publish / poll / is_connected / disconnect），
把官方"回调无参数、payload 再取一次"的写法全部收拢在这里。

官方文档对照（详见 docs/decisions.md 与 REQ-01 §10）：

- WiFi：`from k10_base import WiFi`，`connect(ssid=…, psd=…, timeout=…)`——
  密码参数叫 **psd**，不叫 password；
- MQTT：`from k10_base import MqttClient`，`connect(server=…, port=…, client_id=…,
  user=…, psd=…)`，`connected()`、`received(topic=, callback=)`、
  `publish(topic=, content=)`、`message(topic=)`；
- 回调 **无参数**，消息要在回调里用 `message(topic=…)` 再取一次；
- 没有 qos / retain 参数，也没有 `loop()` / `check_msg()`；
- `MqttClient()` 无参构造是推断（官方示例漏写这一行，R-1），上板第一件事就是验它。

重要：库的回调在它自己的线程里执行，不能在里面做屏幕绘制/播放这类慢操作，
所以回调只把消息塞进队列，真正的处理留给主循环的 `poll()`。
"""

_QUEUE_LIMIT = 8
_wifi = None


def _log(message):
    try:
        print("[k10] " + message)
    except Exception:
        pass


def ensure_wifi(ssid, password, timeout_ms=50000):
    """确保 WiFi 已连上；返回是否在线。断线后重连也走这里。"""
    global _wifi
    try:
        from k10_base import WiFi
    except ImportError as exc:
        _log("缺少 k10_base.WiFi：%s" % exc)
        return False

    try:
        if _wifi is None:
            _wifi = WiFi()
        if _wifi.status():
            return True
        _log("连接 WiFi：%s" % ssid)
        _wifi.connect(ssid=ssid, psd=password, timeout=timeout_ms)
        online = bool(_wifi.status())
        _log("WiFi %s" % ("已连接" if online else "连接失败"))
        return online
    except Exception as exc:
        _log("WiFi 异常：%s" % exc)
        return False


class K10MqttClient:
    """把 `MqttClient` 包成板子链路需要的最小客户端。"""

    def __init__(self, on_message, config):
        from k10_base import MqttClient  # 延迟导入，PC 上 import 本模块不会炸

        self._config = config
        self._on_message = on_message
        self._topic = config.EVENT_TOPIC
        self._mqtt = MqttClient()
        self._pending = []
        self._connected = False

    # ---------- 连接 ----------

    def connect(self):
        config = self._config
        if not ensure_wifi(
            config.WIFI_SSID,
            config.WIFI_PASSWORD,
            getattr(config, "WIFI_TIMEOUT_MS", 50000),
        ):
            return False
        try:
            self._mqtt.connect(
                server=config.HOST,
                port=config.PORT,
                client_id=getattr(config, "CLIENT_ID", "hassist-k10-01"),
                user=config.IOT_ID,
                psd=config.IOT_PWD,
            )
        except Exception as exc:
            _log("MQTT 连接异常：%s" % exc)
            return False
        try:
            self._connected = bool(self._mqtt.connected())
        except Exception:
            self._connected = False
        return self._connected

    def subscribe(self, topic):
        try:
            self._mqtt.received(topic=topic, callback=self._on_k10_message)
            return True
        except Exception as exc:
            _log("订阅失败：%s" % exc)
            return False

    # ---------- 收发 ----------

    def publish(self, topic, data):
        try:
            if not self.is_connected():
                return False
        except Exception:
            return False
        try:
            self._mqtt.publish(topic=topic, content=data)
            return True
        except TypeError:
            # 有的固件只收 str；退一步再试一次（心跳/载荷都是 UTF-8 文本）
            try:
                self._mqtt.publish(topic=topic, content=data.decode("utf-8"))
                return True
            except Exception as exc:
                _log("发布失败（str 回退）：%s" % exc)
                return False
        except Exception as exc:
            _log("发布失败：%s" % exc)
            return False

    def _on_k10_message(self):
        """库的回调：无参数，消息要自己再取。只入队，不做慢操作。"""
        try:
            data = self._mqtt.message(topic=self._topic)
            if data is None:
                return
            if len(self._pending) >= _QUEUE_LIMIT:
                self._pending.pop(0)
            self._pending.append(data)
        except Exception as exc:
            _log("取消息失败：%s" % exc)

    def poll(self):
        """把回调线程排到的消息交给主循环处理。"""
        while self._pending:
            data = self._pending.pop(0)
            try:
                self._on_message(self._topic, data)
            except Exception as exc:
                _log("处理消息异常：%s" % exc)

    def is_connected(self):
        try:
            return bool(self._mqtt.connected())
        except Exception:
            return False

    def disconnect(self):
        try:
            self._mqtt.disconnect()
        except Exception:
            pass


def make_factory(config):
    """给 `BoardLink` 用的工厂：每次重连都建一个新的客户端。"""

    def factory(on_message):
        return K10MqttClient(on_message, config)

    return factory
