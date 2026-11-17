# 实施计划（v0.1 · 2026-11-15）

> 配套文档：`docs/REQ-01-iot.md`（v1.1）、`docs/REQ-02-webui.md`（v1.1）、`docs/decisions.md`（裁决台账）。
> 本文件回答"怎么拆、拆多久、先做哪一块、现场怎么演示"。
>
> **2026-11-15 进展**：T1（网页服务端 / 单实例 / 端口退让 / 启动切换）与 T2–T7 的页面接线、T8 的 IoT 页面接线已完成；`run_gui.bat` 已切到网页，`run_gui_classic.bat` 回退。真实 K10/EasyIoT 凭据联调、长稳和文档收尾（T9/T10）待做。下文保留原始拆分与排期。

---

## 一、结论先行

1. 网页前端是**新建**，不是改造：仓库里没有 HTTP 服务、没有端口、没有单实例锁（`start_silent.vbs:1-2` 还在教用户用任务管理器杀进程）。
2. 现有资产可以直接复用、且不该改动：`Detector` / `Recorder` / `learn_doorbell` / 事件结构（`pc_recognizer.py:687`、`:980`、`:1087`、`:838-859`）。C-1 要求识别零改动，新代码只在外围加壳。
3. **一个进程**：识别线程 + 播放线程 + IoT 线程 + HTTP 线程，共享一个状态总线；天然满足单实例、多端一致（D-15）。
4. 冷却是**全局 8 秒**、演示命中绕开（D-07）；播放期间必须抑制识别（D-06），否则"放一次、报两次"。
5. 全量约 **113 人时**。按"两周 / 每天 2 小时"（28 小时）**排不下**——这是本计划唯一需要重新拍板的地方（见第五节）。

---

## 二、技术基线（现状事实）

| 项 | 现状 | 影响 |
|---|---|---|
| 解释器 | `run.bat:6` 硬编码 DSH 私有路径的 **3.12.14**；系统 python 3.14.5 缺 sounddevice，`lib/` 的 cffi 是 **cp312** 编译 | 启动器必须三级回退（D-09）；不能用系统 python 跑 |
| 依赖 | `lib/` 内有 numpy、sounddevice 0.5.6、paho-mqtt 2.1.0；`lib/` 被 `.gitignore:2` 忽略 | 拷贝目录必须连 `lib/` 一起；paho 已就位，无需联网装包 |
| 识别接口 | `Detector`（`:687`）、`Recorder`（`:980`）、`learn_doorbell(..., takes=)`（`:1087`）、`build_templates` / `save_config` / `load_config` | 全部原样调用，不改一行 |
| 事件字段 | `type/cls/label/tier/who`、`conf/score/dur/latency/tonal/mod/decay/stab/freq_ratio/conc_p90/sustain`、`n_knock/n_pulse`、`trace`、`wav` | 页面展示与 IoT 载荷都从这些字段取；`score`=相似度，`conf`=置信度 |
| 配置 | 只持久化 `threshold`；`save_config` 全量覆盖 | 需要扩字段（WEB-9.5），并保证旧文件缺字段不报错 |
| 播放 | `sounddevice` 持久 `OutputStream` + 独立线程 + 播放锁；**播放不抑制识别** | 必须补抑制窗口（T4 的硬需求） |
| 录音 | `sounds/events/{时间}_{hit\|ignore}_{cls}_s{score}_c{conf}.wav`，保留 300 | 页面录音列表直接扫这个目录 |
| 单实例 | 无 | 用端口健康探测实现（D-15） |

---

## 三、目标架构

```
pc_web.py（唯一进程）
├── 识别线程   Recorder → Detector → 事件 → 状态总线 / 事件队列
├── 播放线程   试听 / 测试音 / 模板播放（抑制窗口内丢弃麦克风块）
├── IoT 线程   事件队列 → paho 发布（QoS 1，不做应用层重试）
│              状态主题订阅 → 心跳 → 板子在线标记
├── HTTP 线程  ThreadingHTTPServer：静态文件 + /api/* + /events (SSE)
└── 状态总线   dict + seq 版本号 + 订阅者队列（线程安全，唯一真相）
```

**新增文件**

```
pc_web.py                 新入口（run_gui.bat 的目标）
web/state.py              状态总线（dict + seq + 订阅）
web/http_api.py           路由、静态文件、SSE、口令校验
web/iot.py                MQTT 上报、心跳、状态映射、中文错误
web/static/index.html     单页
web/static/app.js         SSE 客户端 + 交互
web/static/style.css      大字区 / 事件流 / 响应式
board/main.py             K10 入口：连 WiFi + 订阅 + 主循环
board/mqtt_link.py        连接/载荷/心跳（与板子无关，可复用）
board/ui_k10.py           屏 / 喇叭 / 3 颗 WS2812
board/fonts/font_ui48.bin 48px ASCII 字库（LVGL）
board/fonts/font_ui20.bin 20px ASCII 字库（LVGL）
board/feedback.py         外接蜂鸣器 / 震动马达的节奏（可选）
board/sim_board.py        本机模拟板子（第二订阅者，验收 AC-IOT-7）
run_gui.bat               改为启动 pc_web.py
run_gui_classic.bat       新增，启动 pc_gui.py（回退）
```

**API 草案**（只为实现约定，可调）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | `{app:"doorbell-web",pid,port}`，供单实例探测 |
| GET | `/api/state` | 全量状态（模式、大字、电平、底噪、iot、板子、统计、模板） |
| GET | `/api/events?since=seq` | 增量事件 |
| GET | `/events` | SSE：状态 8 Hz、事件即时、1 Hz 心跳 |
| POST | `/api/listen` | `{on:true}` 开始/停止（幂等） |
| POST | `/api/learn` | `{action:"start"\|"cancel", takes:3}`；冲突 409 |
| POST | `/api/preview` | `{cls:"doorbell"}` 或 `{test:true}` |
| POST | `/api/settings` | 阈值/门控/设备/局域网/口令/学习次数 |
| POST | `/api/iot/config`、`/api/iot/test` | 上报配置与测试连接 |
| POST | `/api/recordings/clear`、`GET /api/export.csv` | 清空录音、导出 |

---

## 四、任务拆分

> 估时为**独立完成**的净工作小时（不含等待采购/上板）。依赖列只写硬依赖。

| 编号 | 任务 | 覆盖需求 | 依赖 | 估时 | 完成判定 |
|---|---|---|---|---|---|
| T1 | 服务端骨架与状态总线 | WEB-1.1–1.9、1.11、13.2–13.4、13.8、1.5 | — | 22h | 双击启动→浏览器开→`/api/health` 通；第二次双击不启第二进程；端口 8765/8766/8767 退让；3 端刷新一致 |
| T2 | 页面骨架 + 大字区 + 音量 + 监听 | WEB-2、WEB-3、WEB-5 | T1 | 14h | AC-WEB-1 五步过；1920×1080 与 390×844 不溢出 |
| T3 | 学习流程 | WEB-4 | T1、T2 | 10h | AC-WEB-2 五步过；刷新进度不丢 |
| T4 | 试听 / 测试音 / 抑制窗口 | WEB-6、WEB-2.10 | T1、T2 | 9h | AC-WEB-9 过（一次播放一条事件）；播放时不产生额外命中 |
| T5 | 事件流 / 详情 / 录音列表 | WEB-7 | T1、T2 | 11h | AC-WEB-5 第 1/2/4 步过；300 条上限生效 |
| T6 | 统计 / CSV / 清空录音 | WEB-8 | T5 | 4h | CSV 列序固定、BOM 正确、Excel 无乱码 |
| T7 | 高级设置与持久化 | WEB-9、WEB-10 | T1 | 7h | 保存后重启仍生效；非法输入被拒；旧配置缺字段不报错 |
| T8 | IoT 上报子系统 | REQ-01 IOT-1–5 | T1 | 14h | AC-IOT-1/2/3/9 过；断网丢弃计数、不补发；凭证掩码 |
| T9 | K10 固件 | REQ-01 IOT-6 | T8（联调） | 12h | AC-IOT-4/5/6/7 过；断电重启 ≤10s；同 ts 去重 |
| T10 | 联调、文档、演示动线 | WEB-12、13.5–13.7、全部 AC | T1–T9 | 10h | 全 AC 走一遍；README 更新；`run_gui_classic.bat` 回退可用 |

合计 **113h ≈ 14 人日**。

---

## 五、排期与范围（P-01：需要你拍板）

**四档范围**

| 档 | 内容 | 小时 | 能过哪些验收 |
|---|---|---|---|
| 甲·纯演示 | T1+T2+T4+T8+T9+部分 T10（靠**内置模板**，不碰学习流程） | **79h** | AC-WEB-1/4/9（部分）、AC-IOT-1/4/5/6/7 |
| 乙·演示+学习 | 甲 + T3 | 89h | + AC-WEB-2 |
| 丙·M 全过 | 乙 + T5+T6+T7 | 111h | 全部 M 级 AC |
| 丁·含加分 | 丙 + 回放/日志面板/取消学习/口令/清空等 S | ~120h | + S 级 |

**排期账**

| 每天投入 | 甲 | 乙 | 丙 | 丁 |
|---|---|---|---|---|
| 2 小时 | 40 天 | 45 天 | 56 天 | 60 天 |
| 4 小时 | 20 天 | 23 天 | 28 天 | 30 天 |
| 6 小时 | 13 天 | 15 天 | 19 天 | 20 天 |

**结论**：如果演示**两周内**且每天 **2 小时**（共 28 小时），只够做完 T1 加半个 T2——**一条验收都过不了**。三种走法：

- 两周内、每天约 6 小时 → **甲档**（13 天）或乙档（15 天，紧）。
- 两周内、每天 2 小时 → 目标必须改成"骨架 + 试听链路"，学习/事件流/IoT 全部后置。
- 三到四周、每天 4–6 小时 → **丙档**，M 全过，最稳。

> 建议：演示日不动，先走**甲档**把链路打通（内置模板 + 试听 + IoT + 固件），乙/丙档在演示后继续。这样现场至少有"门铃→大字→板子"的完整闭环。

---

## 六、风险与待实测

| 编号 | 风险 | 影响 | 应对 |
|---|---|---|---|
| R-1 | K10 的 `client_id` 能否自定义（官方例程留空） | 同账号两端可能互相踢下线（D-1） | 上板第一件事就验；不能自定义则错开连接时机，并把限制写进文档 |
| R-2 | K10 中文字体与字号，长词「水壶鸣笛」可能截断 | IOT-6.3/6.13 | 先用「水壶」短标签，实测后决定 |
| R-3 | K10 MicroPython 库官方标注"测试阶段" | 固件可能返工 | 固件保持单文件、最小依赖；`mqtt_link.py` 与 `ui_k10.py` 分层 |
| R-4 | 平台配额 1 万条 / 建议间隔 ≥3 秒 | 长时间演示可能被限流 | 演示 ≤50 条；相邻 ≥300 ms；2 小时长稳测试单列 |
| R-5 | 主分支 `selftest` 在负样本上已有 **2 处误报**（退出码 0 掩盖），严格版在 worktree `dsh-2c29f72a-b3ed` 的 `653c093` 未合并 | AC-WEB-5「放音乐→忽略」今天很可能不过 | 先合并严格版 selftest；演示曲目提前跑一遍负样本 |
| R-6 | 播放与识别**不互斥**是现存缺陷 | 一次播放报两次，直接挂 AC-WEB-9 / AC-IOT-6 | T4 必须做抑制窗口，不能只做播放 |
| R-7 | 系统 python 3.14 缺 sounddevice、`lib/` 是 cp312 | 换机器即崩 | 启动器三级回退 + README 写明"连 lib/ 一起拷" |
| R-8 | Windows 防火墙首次弹窗 | 手机打不开，现场尴尬 | 演示前一天先跑一次点"允许"；页面给排查提示 |
| R-9 | K10 的 MicroPython **没有音调 API**，声音只能播预置 WAV | IOT-6.1–6.4 的四种提示音 | 已生成 `board/sounds/*.wav`（D-20）；真机回放不可用则声音降级，屏+灯兜底 |
| R-10 | `MqttClient()` 无参构造、回调由库线程派发这两点**未证实** | 固件能否收到消息 | 上板第一件事：用 `sim_board.py send` 发一条，看屏幕是否触发；不触发则改用手动轮询 `message()` |
| R-11 | K10 中文显示依赖固件字体，可能需自建 LVGL 字库 | IOT-6.13 | `board/config.py` 里 `SHOW_CHINESE=False` 可退化短码，先保证可读 |
| R-12 | K10 板载无蜂鸣器/震动马达，外接模块的电平与供电未实测 | D-23 的可选增强 | 接线见 `docs/board-wiring.md`；先 REPL 验引脚，再发事件验节奏 |
| R-13 | LVGL 界面与字库在真机上的最终显示（字号、对齐、刷新）尚未由用户验收 | D-24 的显示效果 | 上传 `font_ui48.bin` / `font_ui20.bin` 到根目录；缺字库会自动退回内置字体 |

---

## 七、演示动线与现场 checklist

**演示前一天**

1. 双击 `run_gui.bat`，在弹出的防火墙上点"允许"。
2. 手机连同一 WiFi 打开页面地址，确认能显示、能点。
3. 跑一次四类试听，确认板子四种表现不同。
4. 跑 `run.bat selftest`，记录结果（含已知负样本误报）。
5. 确认 8765 端口空闲；确认 `lib/` 与 `sounds/builtin_audio/` 都在目录里。

**现场**

1. 双击 `run_gui.bat` → 浏览器自动打开。
2. 页顶确认：IoT「已连接」、板子「在线」（绿）。
3. 手机打开页面地址，确认与电脑同步。
4. 点「开始监听」→ 按真实门铃 → 大字 + 板子动作（≤2 秒）。
5. 四类试听各一次，间隔 ≥4 秒；每次间隔里看板子动作是否可区分。
6. （可选）关 WiFi 30 秒 → 页面大字照常、IoT 变「重连中」→ 恢复后自愈。
7. 收尾：导出 CSV；如需要点「清空录音」。

**回退**：网页起不来 → 双击 `run_gui_classic.bat`，行为与改动前一致。

---

## 八、验收命令

```bat
run.bat devices                      :: 设备列表（配置麦克风/播放设备用）
run.bat selftest                     :: 无麦克风自检
run.bat listen --quiet --log recog.log
curl http://127.0.0.1:8765/api/health
curl http://127.0.0.1:8765/api/state
python board/sim_board.py --subscribe  :: 本机第二订阅者，验证载荷与 QoS
python board/sim_board.py --same-ts    :: 同一 ts 发两次（AC-IOT-7）
python board/sim_board.py --different-ts
```

断网演练（管理员 PowerShell）：

```powershell
netsh interface set interface "WLAN" disable
netsh interface set interface "WLAN" enable
```

---

## 九、来源

- K10 硬件与尺寸：https://www.unihiker.com.cn/wiki/k10/jianjie 、https://wiki.dfrobot.com/dfr0992-en/
- K10 MicroPython 与 MQTT 用法：https://www.unihiker.com.cn/wiki/k10/micropython_unihiker_k10
- EasyIoT 综合教程（Arduino / micro:bit / 掌控板 / ESP32 / K10）：https://mc.dfrobot.com.cn/article-384837
- EasyIoT 接入地址示例：https://wiki.dfrobot.com/tel0182/docs/22146
- `Iot_id` / `Iot_pwd` 重新生成：https://mc.dfrobot.com.cn/article-380847
- 主题为工作间分配的设备编码：https://blog.csdn.net/bit_mike/article/details/145081266
- 设备/主题数量上限：https://mc.dfrobot.com.cn/thread-35758
- 相同 clientId 互踢（MQTT 通用行为）：https://github.com/dromara/mica-mqtt-docs
- K10 物联网案例集（MicroPython 项目暂缺）：https://www.unihiker.com.cn/wiki/k10/iot_project

> 平台能力说明（QoS / retain / payload 上限）官方文档未查到，本计划按"不支持、不依赖"处理（NFR-IOT-9）。
