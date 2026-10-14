import logging

from app.core.config import settings
from app.core.exceptions import APIError
from app.core.logging_utils import contains_sensitive_material, redact_sensitive_text

logger = logging.getLogger(__name__)


def is_quota_or_rate_limit_error(exc: Exception) -> bool:
    cls_name = type(exc).__name__.lower()
    if "ratelimit" in cls_name or "quota" in cls_name:
        return True
    status_code = getattr(exc, "status_code", None)
    if status_code == 429:
        return True
    code = str(getattr(exc, "code", "") or "").lower()
    if code in {"insufficient_quota", "rate_limit_exceeded"}:
        return True
    text = str(exc).lower()
    return any(
        marker in text
        for marker in (
            "insufficient_quota",
            "rate_limit_exceeded",
            "rate limit",
            "quota exceeded",
            "exceeded your current quota",
            "resource_exhausted",
            "resource exhausted",
            "too many requests",
        )
    )


def map_ai_provider_exception(
    exc: Exception,
    *,
    operation: str,
    default_code: str = "llm_provider_error",
    default_message: str = "The AI assistant could not complete this request.",
) -> APIError:
    raw_message = str(exc)
    if is_quota_or_rate_limit_error(exc):
        logger.error(
            "AI provider quota or rate limit reached [operation=%s error_type=%s]",
            operation,
            type(exc).__name__,
        )
        return APIError(
            "The AI assistant is temporarily unavailable due to provider quota or rate limits.",
            status_code=503,
            code="llm_quota_exceeded",
        )

    if contains_sensitive_material(raw_message):
        logger.error(
            "AI provider failure [operation=%s error_type=%s detail=%s]",
            operation,
            type(exc).__name__,
            redact_sensitive_text(raw_message),
        )
    else:
        logger.exception(
            "AI provider failure [operation=%s error_type=%s]",
            operation,
            type(exc).__name__,
        )
    return APIError(
        default_message,
        status_code=502,
        code=default_code,
    )


def create_chat_model():
    if settings.ai_provider == "ollama":
        from langchain_ollama import ChatOllama

        return ChatOllama(
            model=settings.ollama_model,
            base_url=settings.ollama_base_url,
            num_ctx=settings.ollama_num_ctx,
            client_kwargs={"timeout": settings.ollama_timeout_seconds},
        )

    if settings.ai_provider != "gemini":
        logger.error("Unsupported AI_PROVIDER configured; expected 'gemini' or 'ollama'.")
        raise APIError(
            "The AI assistant is not configured.",
            status_code=503,
            code="llm_not_configured",
        )

    if settings.gemini_api_key is None or not settings.gemini_api_key.get_secret_value().strip():
        raise APIError(
            "The AI assistant is not configured.",
            status_code=503,
            code="llm_not_configured",
        )

    from langchain_google_genai import ChatGoogleGenerativeAI

    return ChatGoogleGenerativeAI(
        model=settings.gemini_model,
        google_api_key=settings.gemini_api_key,
        timeout=settings.gemini_timeout_seconds,
        max_retries=settings.gemini_max_retries,
    )
