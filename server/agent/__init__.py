"""
AI Agent 模块 - Computer Use 功能
支持 Anthropic Computer Use 协议和 OpenAI 兼容协议，通过截图→分析→操控循环自主完成任务

适配器:
  - AnthropicComputerUseAdapter: Claude 系列模型（/v1/messages 格式）
  - OpenAICompatAdapter: GPT、GLM、Kimi、Minimax 等（/v1/chat/completions 格式）

可用模型:
  - Claude Opus 4.6 / 4.5, Claude Sonnet 4.6 / 4.5 (Anthropic, 支持 Computer Use)
  - GPT-5.1, GPT-5.1-Codex (OpenAI)
  - GLM-5, Kimi-K2.5, Minimax-M2.5 (国产模型)
"""

from .anthropic_adapter import (
    AVAILABLE_MODELS,
    DEFAULT_MODEL_ID,
    get_available_models,
    get_model_info,
)
from .openai_adapter import OpenAICompatAdapter
