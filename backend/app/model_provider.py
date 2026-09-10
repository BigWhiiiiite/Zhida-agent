from __future__ import annotations

import json
import logging
import os
import time
from functools import lru_cache
from pathlib import Path

import httpx
from agents import Agent, ModelSettings, Runner, set_tracing_disabled
from agents.models.openai_responses import OpenAIResponsesModel
from openai import (APIConnectionError, APITimeoutError, AsyncOpenAI,
                    DefaultAsyncHttpxClient, InternalServerError, RateLimitError)
from dotenv import load_dotenv

from .models import ModelHealth


ENV_PATH = Path(__file__).resolve().parents[1] / ".env"


# Agents SDK 0.8.4 logs the full model input on terminal errors in one run-loop
# branch. Application code already turns those errors into safe API messages, so
# silence the SDK logger to prevent resumes from appearing in server logs.
logging.getLogger("openai.agents").setLevel(logging.CRITICAL)


async def normalize_proxy_response(response: httpx.Response) -> None:
    """Fill harmless usage counters omitted by some OpenAI-compatible proxies."""
    if response.status_code >= 400 or response.headers.get("content-type", "").split(";", 1)[0] != "application/json":
        return
    if not response.request.url.path.rstrip("/").endswith("/responses"):
        return

    await response.aread()
    try:
        payload = response.json()
    except (ValueError, UnicodeDecodeError):
        return
    if not isinstance(payload, dict) or not isinstance(payload.get("usage"), dict):
        return

    usage = payload["usage"]
    changed = False
    input_details = usage.get("input_tokens_details")
    if not isinstance(input_details, dict):
        usage["input_tokens_details"] = {"cached_tokens": 0, "cache_write_tokens": 0}
        changed = True
    else:
        if "cached_tokens" not in input_details:
            input_details["cached_tokens"] = 0
            changed = True
        if "cache_write_tokens" not in input_details:
            input_details["cache_write_tokens"] = 0
            changed = True

    output_details = usage.get("output_tokens_details")
    if not isinstance(output_details, dict):
        usage["output_tokens_details"] = {"reasoning_tokens": 0}
        changed = True
    elif "reasoning_tokens" not in output_details:
        output_details["reasoning_tokens"] = 0
        changed = True

    if changed:
        content = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        # HTTPX response hooks do not expose a public body setter. The body has already
        # been read, so replacing its cached bytes is safe before OpenAI parses it.
        response._content = content
        response.headers["content-length"] = str(len(content))


@lru_cache(maxsize=16)
def configured_model(model_name: str | None = None, reasoning_effort: str | None = None,
                     timeout_seconds: float | None = None) -> tuple[OpenAIResponsesModel, ModelSettings]:
    # The local .env is the source of truth. override=True matters when uvicorn's
    # reloader inherited an older key from its parent process.
    load_dotenv(ENV_PATH, override=True)
    key_env = os.getenv("APP_MODEL_API_KEY_ENV", "ISRC_API_KEY")
    api_key = os.getenv(key_env)
    if not api_key:
        raise RuntimeError(f"缺少模型密钥：请先设置环境变量 {key_env}")

    model_name = model_name or os.getenv("APP_AGENT_MODEL", "gpt-5.6-sol")
    base_url = os.getenv("APP_MODEL_BASE_URL", "https://llmapi.isrc.ac.cn/v1")
    effort = reasoning_effort or os.getenv("APP_MODEL_REASONING_EFFORT", "ultra")
    # `ultra` is a Codex configuration preset. The Responses wire API exposed by
    # this proxy currently accepts `max` as its highest reasoning effort.
    wire_effort = "max" if effort.lower() == "ultra" else effort.lower()
    max_retries = max(0, min(int(os.getenv("APP_MODEL_MAX_RETRIES", "0")), 6))
    configured_timeout = timeout_seconds if timeout_seconds is not None else float(os.getenv("APP_MODEL_TIMEOUT_SECONDS", "180"))
    timeout = max(10, min(configured_timeout, 300))
    http_client = DefaultAsyncHttpxClient(event_hooks={"response": [normalize_proxy_response]})
    client = AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=timeout, max_retries=max_retries,
                         http_client=http_client)

    # The custom proxy is OpenAI-compatible but tracing should not be sent to a second endpoint.
    set_tracing_disabled(True)
    model = OpenAIResponsesModel(model=model_name, openai_client=client)
    settings = ModelSettings(store=False, extra_body={"reasoning": {"effort": wire_effort}})
    return model, settings


def reload_model_configuration() -> None:
    """Reload backend/.env without ever returning or logging the API key."""
    load_dotenv(ENV_PATH, override=True)
    configured_model.cache_clear()


async def check_model_health() -> ModelHealth:
    """Run a minimal, non-user-data request and return only sanitized diagnostics."""
    reload_model_configuration()
    model_name = os.getenv("APP_AGENT_MODEL", "gpt-5.6-sol").strip()
    started = time.monotonic()
    try:
        model, settings = configured_model(model_name, "low", 30)
        agent = Agent(name="Zhida model health", instructions="Reply with exactly OK.",
                      model=model, model_settings=settings)
        await Runner.run(agent, "OK", max_turns=1)
        return ModelHealth(status="ok", model=model_name,
                           latency_ms=round((time.monotonic() - started) * 1000),
                           message="主模型当前可用")
    except RuntimeError as exc:
        return ModelHealth(status="misconfigured", model=model_name,
                           latency_ms=round((time.monotonic() - started) * 1000), message=str(exc))
    except RateLimitError:
        message = "代理上游账号池正在限流，请稍后重试"
    except APITimeoutError:
        message = "模型请求超时，请稍后重试"
    except APIConnectionError:
        message = "无法连接模型代理"
    except InternalServerError as exc:
        if getattr(exc, "code", "") == "model_not_found":
            message = f"当前 Key 所属分组没有 {model_name} 可用通道"
        else:
            message = "代理或上游模型暂时不可用"
    except Exception:
        message = "模型健康检查失败"
    return ModelHealth(status="unavailable", model=model_name,
                       latency_ms=round((time.monotonic() - started) * 1000), message=message)
