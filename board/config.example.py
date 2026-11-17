"""板子配置模板。

复制本文件为 `board/config.py` 再填写（`config.py` 已被 `.gitignore` 忽略，
不会进版本库）。上传到板子时，把它和 `main.py` 放在同一层（根目录）。

注意：`Iot_id` / `Iot_pwd` 是平台"工作间"分配的，**不是**网站登录账号；
事件主题与状态主题是两个不同设备页上"自动生成、不可修改"的编码（IOT-4.2、D-4）。
"""

# ---------- 现场 WiFi ----------
WIFI_SSID = "替换成安装现场 WiFi"
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
# 界面固定英文（DOOR / KNOCK / KETTLE / FIRE / CLOUD / WAIT / SCORE）。
# 该选项仅为兼容旧配置保留，当前固件忽略它。
SHOW_CHINESE = False
# 事件时间按这个时区显示（不依赖板子 RTC，用时间戳直接换算）
TZ_OFFSET_HOURS = 8

# ---------- 外接提醒 ----------
# K10 板载没有蜂鸣器、也没有震动马达，需要外接；不接就保持 None。
# IO 限制（官方文档）：只有 P0 / P1 是原生 GPIO 且支持 PWM；其余引脚只能拉高/拉低。
#   有源蜂鸣器 → 任意数字口；无源蜂鸣器 → 只能 P0 或 P1（靠 write_analog 出音调）
#   震动马达 → 必须经三极管/MOSFET 模块驱动，不能直接接 GPIO
BUZZER_PIN = None
BUZZER_MODE = "active"        # "active" 有源（高低电平） / "passive" 无源（PWM 音调）
VIBRATION_PIN = None
BUZZER_ACTIVE_HIGH = True     # 模块是低电平触发就改 False
VIBRATION_ACTIVE_HIGH = True
