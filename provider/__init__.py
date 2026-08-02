"""LLM 适配层：OpenAI SDK 封装。"""
from .provider import Provider, StreamResult

__all__ = ["Provider", "StreamResult"]
