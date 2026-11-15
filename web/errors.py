"""API 错误：带 HTTP 状态码与中文原因，直接映射到页面提示。"""

from __future__ import annotations


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


class ConfigError(ApiError):
    """配置校验失败（400）。"""
