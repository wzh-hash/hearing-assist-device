"""`sounds/config.json` 的读取、校验与口令散列。

设计约束：
- 旧配置只有 ``threshold``，其余字段缺失时用默认值补全（WEB-9.9）。
- 识别引擎只读 ``threshold``；其余字段属于网页。
- 访问口令只保存 PBKDF2 散列与随机盐，配置文件里没有明文。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import tempfile
from pathlib import Path

import pc_recognizer as R
from .errors import ApiError, ConfigError

#: 密码散列迭代次数（PBKDF2-HMAC-SHA256）
PBKDF2_ITERATIONS = 150_000

DEFAULT_GATE_DB = 8
DEFAULT_TAKES = 3
DEFAULT_MIC_INDEX = -1       # -1 = 跟随系统
DEFAULT_SPEAKER_INDEX = -1
DEFAULT_RECORD_ENABLED = True   # 判定录音默认保存（首次启动有隐私说明）
DEFAULT_PRIVACY_ACK = False     # 是否已确认首次隐私说明


def _as_bool(value, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on", "开")
    return default


def _as_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_text(value, default: str = "") -> str:
    return default if value is None else str(value)


def load() -> dict:
    """读取并补全配置；任何坏值都回退默认，不抛异常。"""
    raw = R.load_config()
    if not isinstance(raw, dict):
        raw = {}

    threshold = raw.get("threshold", R.DEFAULT_THRESHOLD)
    try:
        threshold = float(threshold)
    except (TypeError, ValueError):
        threshold = float(R.DEFAULT_THRESHOLD)
    threshold = round(min(0.95, max(0.40, threshold)), 2)

    gate = _as_int(raw.get("gate_db"), DEFAULT_GATE_DB)
    gate = min(30, max(6, gate))

    takes = _as_int(raw.get("takes"), DEFAULT_TAKES)
    takes = min(5, max(1, takes))

    cfg = {
        "threshold": threshold,
        "gate_db": gate,
        "mic_index": _as_int(raw.get("mic_index"), DEFAULT_MIC_INDEX),
        "speaker_index": _as_int(raw.get("speaker_index"), DEFAULT_SPEAKER_INDEX),
        "takes": takes,
        "lan": _as_bool(raw.get("lan"), True),
        "record_enabled": _as_bool(raw.get("record_enabled"), DEFAULT_RECORD_ENABLED),
        "privacy_ack": _as_bool(raw.get("privacy_ack"), DEFAULT_PRIVACY_ACK),
        "password_hash": _as_text(raw.get("password_hash")),
        "password_salt": _as_text(raw.get("password_salt")),
        "password_iterations": _as_int(
            raw.get("password_iterations"), PBKDF2_ITERATIONS
        ),
    }
    if cfg["password_iterations"] < 10_000:
        cfg["password_iterations"] = PBKDF2_ITERATIONS
    return cfg


def public(cfg: dict) -> dict:
    """给页面/SSE 的字段：没有散列、没有盐。"""
    return {
        "threshold": round(float(cfg.get("threshold", R.DEFAULT_THRESHOLD)), 2),
        "gate_db": int(cfg.get("gate_db", DEFAULT_GATE_DB)),
        "mic_index": int(cfg.get("mic_index", DEFAULT_MIC_INDEX)),
        "speaker_index": int(cfg.get("speaker_index", DEFAULT_SPEAKER_INDEX)),
        "takes": int(cfg.get("takes", DEFAULT_TAKES)),
        "lan": bool(cfg.get("lan", True)),
        "record_enabled": bool(cfg.get("record_enabled", DEFAULT_RECORD_ENABLED)),
        "privacy_ack": bool(cfg.get("privacy_ack", DEFAULT_PRIVACY_ACK)),
        "password_set": password_set(cfg),
    }


def save(cfg: dict) -> None:
    """原子合并写入；写不进去就明确报错，而不是静默丢失设置（WEB-9.5）。"""
    path = Path(R.CONFIG_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)

    existing: dict = {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            existing = loaded
    except (OSError, ValueError):
        existing = {}

    existing.update({
        "threshold": round(float(cfg.get("threshold", R.DEFAULT_THRESHOLD)), 2),
        "gate_db": int(cfg.get("gate_db", DEFAULT_GATE_DB)),
        "mic_index": int(cfg.get("mic_index", DEFAULT_MIC_INDEX)),
        "speaker_index": int(cfg.get("speaker_index", DEFAULT_SPEAKER_INDEX)),
        "takes": int(cfg.get("takes", DEFAULT_TAKES)),
        "lan": bool(cfg.get("lan", True)),
        "record_enabled": bool(cfg.get("record_enabled", DEFAULT_RECORD_ENABLED)),
        "privacy_ack": bool(cfg.get("privacy_ack", DEFAULT_PRIVACY_ACK)),
        "password_hash": _as_text(cfg.get("password_hash")),
        "password_salt": _as_text(cfg.get("password_salt")),
        "password_iterations": int(cfg.get("password_iterations", PBKDF2_ITERATIONS)),
    })

    try:
        fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".config-", suffix=".json")
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(existing, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            os.replace(tmp, path)
        except BaseException:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise
    except OSError as exc:
        raise ApiError(f"设置保存失败：{exc}", 500) from None


def password_set(cfg: dict) -> bool:
    return bool(cfg.get("password_hash") and cfg.get("password_salt"))


def _hash(password: str, salt_hex: str, iterations: int) -> str:
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), iterations
    )
    return digest.hex()


def set_password(cfg: dict, password: str) -> None:
    salt = secrets.token_hex(16)
    cfg["password_salt"] = salt
    cfg["password_iterations"] = PBKDF2_ITERATIONS
    cfg["password_hash"] = _hash(password, salt, PBKDF2_ITERATIONS)


def clear_password(cfg: dict) -> None:
    cfg["password_hash"] = ""
    cfg["password_salt"] = ""
    cfg["password_iterations"] = PBKDF2_ITERATIONS


def verify(cfg: dict, password: str) -> bool:
    if not password_set(cfg):
        return True
    try:
        candidate = _hash(
            str(password), str(cfg["password_salt"]), int(cfg["password_iterations"])
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(candidate, str(cfg["password_hash"]))


def apply_patch(current: dict, patch: dict) -> dict:
    """校验页面提交的字段，返回新配置；非法值抛 ``ConfigError``。"""
    if not isinstance(patch, dict):
        raise ConfigError("设置内容必须是对象")
    cfg = dict(current)

    if "threshold" in patch:
        try:
            threshold = float(patch["threshold"])
        except (TypeError, ValueError):
            raise ConfigError("判定阈值必须是数字") from None
        if not 0.40 <= threshold <= 0.95:
            raise ConfigError("阈值需在 0.40 到 0.95 之间")
        cfg["threshold"] = round(threshold, 2)

    if "gate_db" in patch:
        try:
            gate = float(patch["gate_db"])
        except (TypeError, ValueError):
            raise ConfigError("门控必须是数字") from None
        if not 6 <= gate <= 30:
            raise ConfigError("门控需在 6 到 30 dB 之间")
        cfg["gate_db"] = int(round(gate))

    if "takes" in patch:
        try:
            takes = int(patch["takes"])
        except (TypeError, ValueError):
            raise ConfigError("学习次数必须是整数") from None
        if not 1 <= takes <= 5:
            raise ConfigError("学习次数需在 1 到 5 之间")
        cfg["takes"] = takes

    if "mic_index" in patch:
        try:
            mic = int(patch["mic_index"])
        except (TypeError, ValueError):
            raise ConfigError("麦克风设备序号必须是整数") from None
        if mic < -1:
            raise ConfigError("麦克风设备序号不合法")
        cfg["mic_index"] = mic

    if "speaker_index" in patch:
        try:
            speaker = int(patch["speaker_index"])
        except (TypeError, ValueError):
            raise ConfigError("播放设备序号必须是整数") from None
        if speaker < -1:
            raise ConfigError("播放设备序号不合法")
        cfg["speaker_index"] = speaker

    if "lan" in patch:
        cfg["lan"] = _as_bool(patch["lan"], True)

    if "record_enabled" in patch:
        cfg["record_enabled"] = _as_bool(patch["record_enabled"], DEFAULT_RECORD_ENABLED)

    if "privacy_ack" in patch:
        cfg["privacy_ack"] = _as_bool(patch["privacy_ack"], DEFAULT_PRIVACY_ACK)

    if patch.get("clear_password"):
        clear_password(cfg)
    else:
        password = patch.get("password")
        if isinstance(password, str) and password:
            if len(password) > 64:
                raise ConfigError("口令最多 64 个字符")
            set_password(cfg, password)

    return cfg
