# K10 外接蜂鸣器与震动马达

> 结论先说：**K10 板载没有蜂鸣器，也没有震动马达。** 板载能发声的只有一颗 2W 扬声器（播放 WAV / 语音合成），能"动"的只有 3 颗 RGB 灯。想要蜂鸣器与震感，必须外接。
>
> 本文是接线与配置说明；固件侧实现在 `board/feedback.py`，配置项在 `board/config.example.py`。

## 1. 板载清单（官方硬件页）

| 有 | 没有 |
|---|---|
| 2.8" 240×320 彩色 LCD、2W 扬声器、3 颗 WS2812、A/B 按键、双 MEMS 麦克风、摄像头、温湿度、环境光、三轴加速度、TF 卡槽、WiFi/BT | **蜂鸣器**、**震动马达**、触觉反馈 |

## 2. IO 能力（决定能不能接、怎么接）

官方 MicroPython 文档明确：

- **只有 P0 / P1** 是原生 GPIO，`write_digital()` 和 `write_analog(value, freq)`（PWM）都能用；
- **其余引脚**是经 I2C-GPIO 扩展出来的，**只能拉高/拉低**，没有 PWM；
- 板载接口：2× 3Pin PH2.0 全功能 IO、4Pin I2C、金手指 15 路数字 IO。

所以：

| 器件 | 接哪个口 | 能不能 |
|---|---|---|
| 有源蜂鸣器（给电就响） | 任意数字口（含金手指 IO） | 能 |
| 无源蜂鸣器（要频率才响） | **只能 P0 或 P1** | 能（PWM） |
| 震动马达（带驱动模块） | 任意数字口 | 能 |
| 震动马达（裸电机） | 不能直连 | 会烧 GPIO / 带不动 |

## 3. 接线（以 Gravity 3Pin 为例：红=VCC，黑=GND，蓝=SIG）

**有源蜂鸣器模块（3.3V）**
```
VCC → 3.3V
GND → GND
SIG → P0（或任意数字口）
```

**无源蜂鸣器（3.3V）**
```
VCC → 3.3V
GND → GND
SIG → P0 或 P1      ← 必须是有 PWM 的原生口
```

**震动马达模块（带三极管/MOSFET 驱动）**
```
VCC → 3.3V 或 5V（按模块标注）
GND → GND（与板子共地）
SIG → P1（或任意数字口）
IN  → 模块信号输入
```

> 裸马达不要接 GPIO：启动电流通常 60–100mA，超过 GPIO 承受范围，且断电瞬间有反向电动势。买"带驱动"的模块，模块上通常已经带续流二极管。

## 4. 配置

`board/config.py`（由 `config.example.py` 复制）：

```python
BUZZER_PIN = 0          # 有源蜂鸣器接 P0
BUZZER_MODE = "active"  # 无源蜂鸣器改成 "passive" 且引脚必须是 0 或 1
VIBRATION_PIN = 1       # 震动马达模块接 P1
BUZZER_ACTIVE_HIGH = True
VIBRATION_ACTIVE_HIGH = True
```

不接就保持 `None`——固件会静默跳过，不影响屏幕、扬声器与上报。

## 5. 节奏（`board/feedback.py`）

| 事件 | 蜂鸣器 | 震动马达 |
|---|---|---|
| 门铃 | 响 180ms | 震 350ms |
| 敲门 | 两声，各 90ms（间隔 90ms） | 两下，各 130ms（间隔 110ms） |
| 水壶鸣笛 | 长鸣 700ms | 震 700ms |
| 火警 | 连续 6 声（150ms 响 / 100ms 停） | 连续 5 下（220ms / 120ms） |

节奏由主循环每 20ms 推进一次，**不阻塞**收消息与心跳。

## 6. 上板验证（REPL 里直接试）

```python
from unihiker_k10 import pin
p = pin(0)
p.write_digital(1)      # 有源蜂鸣器应开始响
p.write_digital(0)      # 停
p.write_analog(value=500, freq=880)   # 无源蜂鸣器：880Hz
p.write_analog(value=0, freq=880)     # 停
```

然后从电脑发一条事件（板子在线时）：

```bat
python board\sim_board.py send --cls fire
```

应当同时看到：屏幕「火警」+ 红色 + RGB 红闪 + 蜂鸣器连续鸣 + 马达连续震。

## 7. 常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| 一上电就一直响 | 模块低电平触发，配置却是高电平 | `BUZZER_ACTIVE_HIGH = False` |
| 无源蜂鸣器不响 | 接在了非 P0/P1 口（没有 PWM） | 换到 P0 或 P1 |
| 震动马达不动 | 没接驱动模块 / 供电不足 | 用带驱动的模块，VCC 接 5V |
| 只响一下就没声 | 节奏只有 180ms，属正常 | 看第 5 节表格 |
| 扬声器不响 | 4 个 wav 没在板子根目录 | 重新上传 `board/sounds/*.wav` |

## 8. 采购关键词

- `Gravity 有源蜂鸣器模块 3.3V`（或任意 3.3V 有源蜂鸣器模块）
- `震动马达模块 带三极管驱动 3.3V`（不要买裸电机）
- 3Pin PH2.0 转杜邦线（接 Gravity 口）

## 9. 界面字体（上传清单）

屏幕固定英文，使用 LVGL + 外置 ASCII 字库：

| 文件 | 用途 | 大小 |
|---|---|---|
| `font_ui48.bin` | 48px 大字（`DOOR` / `KNOCK` / `KETTLE` / `FIRE`） | ~25 KB |
| `font_ui20.bin` | 20px 状态与得分（`CLOUD` / `SCORE` / 时间） | ~5 KB |

两个文件放板子**根目录**；缺了会自动退回内置字体（字小，但内容不变）。
重新生成：`python tools/make_board_fonts.py`（需要 `lv_font_conv` 与一个中文字体文件，
默认用 Windows 黑体）。

> 界面语言由 D-24 定案为英文：官方固件内置字体没有中文字形，`screen.draw_text`
> 的 `font_size` 也不受控，中文会乱码且字很小。
