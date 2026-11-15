"""板子配置模板。

复制本文件为 `board/config.py` 再填写（`config.py` 已被 `.gitignore` 忽略，
不会进版本库）。上传到板子时，把它和 `main.py` 放在同一层（根目录）。

注意：`Iot_id` / `Iot_pwd` 是平台"工作间"分配的，**不是**网站登录账号；
事件主题与状态主题是两个不同设备页上"自动生成、不可修改"的编码（IOT-4.2、D-4）。
"""

# ---------- 现场 WiFi ----------
WIFI_SSID = "替换成演示现场 WiFi"
WIFI_PASSWORD = "替换成 WiFi 密码"
WIFI_TIMEOUT_MS = 50000

# ---------- EasyIoT ----------
HOST = "iot.dfrobot.com.cn"
PORT = 1883
IOT_ID = "工作间分配的 Iot_id"
IOT_PWD = "工作间分配的 Iot_pwd"
EVENT_TOPIC = "平台分配的事件设备编码"
STATUS_TOPIC = "平台分配的状态设备编码"

# ---------- 板子身份 ----------
DEVICE_NAME = "k10-01"
CLIENT_ID = "doorbell-k10-01"  # 必须与上位机的 doorbell-pc-01 不同（D-1）
HEARTBEAT_MS = 30000

# ---------- 显示 ----------
# 中文是否可用取决于固件字体（R-2）；显示异常时改成 False，退化为 DOOR / KNOCK / ...
SHOW_CHINESE = True
