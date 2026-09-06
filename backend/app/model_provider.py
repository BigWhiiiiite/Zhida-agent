from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from agents import ModelSettings, set_tracing_disabled
from agents.models.openai_responses import OpenAIResponsesModel
from openai import AsyncOpenAI
from dotenv import load_dotenv


@lru_cache(maxsize=1)
def configured_model() -> tuple[OpenAIResponsesModel, ModelSettings]:
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    key_env = os.getenv("APP_MODEL_API_KEY_ENV", "ISRC_API_KEY")
    api_key = os.getenv(key_env)
    if not api_key:
        raise RuntimeError(f"缺少模型密钥：请先设置环境变量 {key_env}")

    model_name = os.getenv("APP_AGENT_MODEL", "gpt-5.6-sol")
    base_url = os.getenv("APP_MODEL_BASE_URL", "https://llmapi.isrc.ac.cn/v1")
    effort = os.getenv("APP_MODEL_REASONING_EFFORT", "ultra")
    client = AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=120, max_retries=2)

    # The custom proxy is OpenAI-compatible but tracing should not be sent to a second endpoint.
    set_tracing_disabled(True)
    model = OpenAIResponsesModel(model=model_name, openai_client=client)
    settings = ModelSettings(store=False, extra_body={"reasoning": {"effort": effort}})
    return model, settings
