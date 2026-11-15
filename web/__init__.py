"""网页控制台的服务端实现（仅本仓库使用）。

- ``config``：`sounds/config.json` 的读取/校验/口令散列
- ``state``：唯一状态总线（快照、事件、日志、SSE 订阅）
- ``audio``：服务端扬声器播放（试听/测试音/模板试听）
- ``app``：识别线程、学习、试听、设置与 IoT 的编排
- ``http_api``：静态页面、JSON API、SSE、CSV、录音
"""
