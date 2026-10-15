import logging
import re
import traceback

from sqlalchemy.exc import IntegrityError

from app.core.config import settings

_SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_-]{8,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._-]+"),
    re.compile(r"(?i)(password|passwd|pwd|secret|api_key|apikey|token)\s*[=:]\s*\S+"),
)
_KEY_IDENTIFIER = re.compile(
    r"\bfor key\s+[`'\"]?([A-Za-z0-9_$.-]+)",
    re.IGNORECASE,
)
_COLUMN_IDENTIFIER = re.compile(
    r"\bcolumn\s+[`'\"]?([A-Za-z0-9_$-]+)[`'\"]?\s+"
    r"(?:cannot be null|at row|has no default)",
    re.IGNORECASE,
)
_MYSQL_IDENTIFIER = r"[A-Za-z0-9_$.-]+"
_MYSQL_FOREIGN_KEY_VIOLATION = re.compile(
    r"\bforeign key constraint fails\s*\(\s*"
    rf"(?:`{_MYSQL_IDENTIFIER}`\s*\.\s*)?`(?P<child_table>{_MYSQL_IDENTIFIER})`\s*,\s*"
    rf"CONSTRAINT\s+`(?P<constraint>{_MYSQL_IDENTIFIER})`\s+FOREIGN KEY\s*"
    rf"\(`(?P<child_column>{_MYSQL_IDENTIFIER})`\)\s+REFERENCES\s+"
    rf"(?:`{_MYSQL_IDENTIFIER}`\s*\.\s*)?`(?P<parent_table>{_MYSQL_IDENTIFIER})`\s*"
    rf"\(`(?P<parent_column>{_MYSQL_IDENTIFIER})`\)",
    re.IGNORECASE,
)


def _configured_secrets() -> list[str]:
    values: list[str] = []
    for secret in (
        settings.gemini_api_key,
        settings.openai_api_key,
        settings.jwt_secret_key,
        settings.db_password,
        settings.database_url_override,
    ):
        if secret is not None:
            raw = secret.get_secret_value().strip()
            if raw:
                values.append(raw)
    if settings.database_url_override is not None:
        url_password = settings.database_url.password
        if url_password:
            values.append(str(url_password))
    return values


def contains_sensitive_material(text: str) -> bool:
    if not text:
        return False
    lowered = text.lower()
    if any(marker in lowered for marker in ("password", "secret", "api_key", "apikey", "sk-", "bearer ")):
        return True
    return any(secret in text for secret in _configured_secrets())


def redact_sensitive_text(text: str) -> str:
    if not text:
        return ""
    redacted = text
    for secret in _configured_secrets():
        redacted = redacted.replace(secret, "[REDACTED]")
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)
    return redacted


def _integrity_error_metadata(
    exc: Exception,
) -> tuple[int | None, dict[str, str]]:
    if not isinstance(exc, IntegrityError):
        return None, {}

    sqlstate = getattr(exc.orig, "sqlstate", None)
    if isinstance(sqlstate, str):
        # psycopg (PostgreSQL): diag carries identifiers only, never row values.
        diag = getattr(exc.orig, "diag", None)
        found = {
            "sqlstate": sqlstate,
            "constraint_or_column": getattr(diag, "constraint_name", None)
            or getattr(diag, "column_name", None),
            "child_table": getattr(diag, "table_name", None),
        }
        return None, {key: value for key, value in found.items() if value}

    original_args = getattr(exc.orig, "args", ())
    error_code = (
        original_args[0]
        if original_args and isinstance(original_args[0], int)
        else None
    )
    identifiers: dict[str, str] = {}
    message = next(
        (arg for arg in original_args[1:] if isinstance(arg, str)),
        "",
    )
    if error_code == 1452:
        match = _MYSQL_FOREIGN_KEY_VIOLATION.search(message)
        if match is not None:
            identifiers.update(match.groupdict())
            identifiers["constraint_or_column"] = identifiers["constraint"]
    elif error_code == 1062:
        match = _KEY_IDENTIFIER.search(message)
        if match is not None:
            identifiers["constraint_or_column"] = (
                match.group(1).rsplit(".", maxsplit=1)[-1]
            )
    else:
        match = _COLUMN_IDENTIFIER.search(message)
        if match is not None:
            identifiers["constraint_or_column"] = match.group(1)

    return error_code, identifiers


def log_database_exception(
    logger: logging.Logger,
    operation: str,
    exc: Exception,
    *,
    conflict: bool = False,
    include_exception_details: bool = True,
    include_integrity_diagnostics: bool = False,
) -> None:
    if not include_exception_details:
        frames = traceback.extract_tb(exc.__traceback__)
        stack = "\n".join(
            f'  File "{frame.filename}", line {frame.lineno}, in {frame.name}'
            for frame in frames
        )
        error_code: int | None = None
        identifiers: dict[str, str] = {}
        if include_integrity_diagnostics:
            error_code, identifiers = _integrity_error_metadata(exc)
        logger.error(
            "Database operation failed "
            "[operation=%s error_type=%s mysql_error_code=%s "
            "constraint_or_column=%s child_table=%s child_column=%s "
            "parent_table=%s parent_column=%s sqlstate=%s]\n%s",
            operation,
            type(exc).__name__,
            error_code,
            identifiers.get("constraint_or_column"),
            identifiers.get("child_table"),
            identifiers.get("child_column"),
            identifiers.get("parent_table"),
            identifiers.get("parent_column"),
            identifiers.get("sqlstate"),
            stack,
        )
        return

    raw_message = str(exc)
    if conflict:
        logger.error(
            "Database integrity conflict [operation=%s error_type=%s]",
            operation,
            type(exc).__name__,
        )
        return

    if contains_sensitive_material(raw_message):
        logger.error(
            "Database operation failed [operation=%s error_type=%s detail=%s]",
            operation,
            type(exc).__name__,
            redact_sensitive_text(raw_message),
        )
    else:
        logger.exception(
            "Database operation failed [operation=%s error_type=%s]",
            operation,
            type(exc).__name__,
        )
