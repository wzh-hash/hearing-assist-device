"""仓库内唯一的路径解析点。

所有新代码都从这里取路径，不要在模块里重复 `dirname(__file__)` 拼路径——
目录结构调整（例如把识别引擎搬进 `src/`）时，只需要改这一处。
"""

from __future__ import annotations

from pathlib import Path

#: 仓库根（host/ 的上一级）
ROOT = Path(__file__).resolve().parent.parent

#: 音频资产与录音目录（现有识别引擎也用它）
SOUNDS = ROOT / "sounds"

#: 每次判定的录音（与识别引擎的约定一致）
EVENTS = SOUNDS / "events"

#: 物联网配置（含 Iot_pwd，必须被版本库忽略）
IOT_CONFIG = SOUNDS / "iot.json"

#: 识别引擎目录（重构后为 src/，重构前为本仓库根）
ENGINE = ROOT / "src" if (ROOT / "src").is_dir() else ROOT


def ensure_dir(path: Path) -> Path:
    """确保目录存在并返回它。"""
    path.mkdir(parents=True, exist_ok=True)
    return path
