"""Explicit, synchronous model adapters with one HTTP attempt per completion.

The configured base URL is the API root, including a version path when needed.
Only caller-selected options are sent; credentials are resolved just before use.
"""

from __future__ import annotations

import copy
import ipaddress
import json
import math
import os
import re
from dataclasses import dataclass, fields
from decimal import Decimal, InvalidOperation
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class ProviderError(Exception):
    """A rejected completion; unknown outcomes must never be blindly retried."""

    def __init__(self, message: str, *, outcome_unknown: bool = False):
        super().__init__(message)
        self.outcome_unknown = outcome_unknown


class IncompleteResponseError(ProviderError):
    """The provider returned a partial, truncated, or unfinished completion."""


_PROTOCOLS = {"deepseek_chat", "openai_chat", "openai_responses"}
_EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh"}


def _price(value: object) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError("Provider prices must be explicit finite nonnegative numbers")
    try:
        price = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError("Provider prices must be explicit finite nonnegative numbers") from None
    if not price.is_finite() or price < 0:
        raise ValueError("Provider prices must be explicit finite nonnegative numbers")
    return price


def _validate_url(url: str) -> None:
    if not isinstance(url, str) or not url or any(c.isspace() for c in url) or "\\" in url or "?" in url or "#" in url:
        raise ValueError("Provider base URL must be an HTTPS API root without query or fragment")
    try:
        parts = urlsplit(url)
        host = parts.hostname
        parts.port  # Reject invalid ports before creating any request.
    except ValueError:
        raise ValueError("Provider base URL is invalid") from None
    if not host or parts.username is not None or parts.password is not None:
        raise ValueError("Provider base URL must have a host and must not contain credentials")
    loopback = host.lower() == "localhost"
    try:
        loopback = loopback or ipaddress.ip_address(host).is_loopback
    except ValueError:
        pass
    if parts.scheme != "https" and not (parts.scheme == "http" and loopback):
        raise ValueError("Provider URL requires HTTPS except for loopback HTTP")


def _validate_options(protocol: str, options: dict) -> dict:
    if not isinstance(options, dict):
        raise ValueError("Provider options must be an object")
    allowed = {
        "deepseek_chat": {"thinking", "json_object"},
        "openai_chat": {"reasoning_effort", "json_object", "token_limit_field"},
        "openai_responses": {"reasoning", "json_object"},
    }[protocol]
    if options.keys() - allowed:
        raise ValueError("Unsupported provider options; endpoint, auth, model, messages and caps are reserved")
    result = copy.deepcopy(options)
    if "json_object" in result and type(result["json_object"]) is not bool:
        raise ValueError("json_object must be a boolean")
    if protocol == "deepseek_chat":
        result.setdefault("thinking", {"type": "disabled"})
        thinking = result["thinking"]
        if not isinstance(thinking, dict) or set(thinking) != {"type"} or thinking["type"] not in ("enabled", "disabled"):
            raise ValueError("DeepSeek thinking must contain only type enabled or disabled")
    if "reasoning_effort" in result:
        if not isinstance(result["reasoning_effort"], str) or result["reasoning_effort"] not in _EFFORTS:
            raise ValueError("Unsupported reasoning effort")
    if "reasoning" in result:
        reasoning = result["reasoning"]
        if (not isinstance(reasoning, dict) or set(reasoning) != {"effort"}
                or not isinstance(reasoning["effort"], str) or reasoning["effort"] not in _EFFORTS):
            raise ValueError("Responses reasoning must contain only a supported effort")
    if "token_limit_field" in result and result["token_limit_field"] not in ("max_tokens", "max_completion_tokens"):
        raise ValueError("Chat token_limit_field must be max_tokens or max_completion_tokens")
    return result


@dataclass(frozen=True)
class ProviderProfile:
    protocol: str
    base_url: str
    model: str
    api_key_env: str
    input_usd_per_million: Decimal
    output_usd_per_million: Decimal
    options: dict

    def __post_init__(self) -> None:
        if not isinstance(self.protocol, str) or self.protocol not in _PROTOCOLS:
            raise ValueError("Unsupported provider protocol")
        _validate_url(self.base_url)
        if not isinstance(self.model, str) or not self.model.strip() or any(c in self.model for c in "\r\n"):
            raise ValueError("Provider model must be a nonempty string")
        if not isinstance(self.api_key_env, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", self.api_key_env):
            raise ValueError("api_key_env must name an environment variable")
        object.__setattr__(self, "input_usd_per_million", _price(self.input_usd_per_million))
        object.__setattr__(self, "output_usd_per_million", _price(self.output_usd_per_million))
        object.__setattr__(self, "options", _validate_options(self.protocol, self.options))

    @classmethod
    def from_dict(cls, data: dict) -> ProviderProfile:
        if not isinstance(data, dict):
            raise ValueError("Provider profile must be an object")
        names = {field.name for field in fields(cls)}
        required = names - {"options"}
        if data.keys() - names or required - data.keys():
            raise ValueError("Provider profile has missing or unsupported fields; explicit prices are required")
        return cls(**{**data, "options": data.get("options", {})})

    def public_dict(self) -> dict:
        return {
            "protocol": self.protocol,
            "base_url": self.base_url,
            "model": self.model,
            "api_key_env": self.api_key_env,
            "input_usd_per_million": str(self.input_usd_per_million),
            "output_usd_per_million": str(self.output_usd_per_million),
            "options": _validate_options(self.protocol, self.options),
        }


@dataclass(frozen=True)
class ModelResult:
    content: str
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    request_id: str | None
    model: str
    raw_usage: dict


class _NoRedirect(HTTPRedirectHandler):
    """Do not forward a bearer token to a redirect target or repeat a dispatch."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


# Construct after CLI/environment TLS configuration has been loaded. Creating a
# global HTTPSHandler captures an SSLContext before SSL_CERT_FILE is available.
def urlopen(request, *, timeout):
    return build_opener(_NoRedirect()).open(request, timeout=timeout)


def _token(usage: dict, name: str) -> int | None:
    value = usage.get(name)
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise ProviderError("Provider returned invalid token usage")
    return value


class _Provider:
    def __init__(self, profile: ProviderProfile):
        self.profile = profile

    def _credential(self) -> str:
        key = os.environ.get(self.profile.api_key_env)
        if not key or not key.isascii() or any(ord(c) <= 32 or ord(c) >= 127 for c in key):
            raise ProviderError("Provider credential is missing or invalid; set the configured environment variable")
        return key

    def validate_credentials(self) -> None:
        """Check before recording dispatch; never expose or retain the key value."""
        self._credential()

    def complete(self, messages: list[dict], *, max_output_tokens: int, timeout: float) -> ModelResult:
        if type(max_output_tokens) is not int or max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be a positive integer")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be a finite positive number")
        if not isinstance(messages, list) or not messages:
            raise ValueError("messages must be a nonempty list")
        for message in messages:
            if (not isinstance(message, dict) or set(message) != {"role", "content"}
                    or message["role"] not in ("system", "developer", "user", "assistant")
                    or not isinstance(message["content"], str)):
                raise ValueError("messages must contain role and string content only")
        options = _validate_options(self.profile.protocol, self.profile.options)
        payload = {"model": self.profile.model, "stream": False}
        if self.profile.protocol == "openai_responses":
            endpoint = "/responses"
            payload.update(input=messages, max_output_tokens=max_output_tokens)
            if "reasoning" in options:
                payload["reasoning"] = options["reasoning"]
            if options.get("json_object"):
                payload["text"] = {"format": {"type": "json_object"}}
        else:
            endpoint = "/chat/completions"
            payload["messages"] = messages
            payload[options.get("token_limit_field", "max_tokens")] = max_output_tokens
            for field in ("thinking", "reasoning_effort"):
                if field in options:
                    payload[field] = options[field]
            if options.get("json_object"):
                payload["response_format"] = {"type": "json_object"}
        request = Request(
            self.profile.base_url.rstrip("/") + endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": "Bearer " + self._credential(), "Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                body = response.read()
                request_id = response.headers.get("x-request-id")
        except HTTPError as error:
            # No provider error body, URL, reason, or exception chain is exposed.
            error.close()
            raise ProviderError(f"Provider HTTP request rejected (status {error.code})") from None
        except (URLError, OSError, HTTPException):
            raise ProviderError("Provider transport failed; request outcome is unknown", outcome_unknown=True) from None
        try:
            data = json.loads(body)
        except (ValueError, UnicodeError):
            raise ProviderError("Provider returned an invalid JSON response") from None
        return self._normalize(data, request_id)

    def _normalize(self, data: object, request_id: str | None) -> ModelResult:
        if not isinstance(data, dict):
            raise ProviderError("Provider returned an invalid response object")
        if self.profile.protocol == "openai_responses":
            if data.get("status") in ("incomplete", "in_progress", "queued"):
                raise IncompleteResponseError("Provider Responses output is incomplete")
            if data.get("status") != "completed":
                raise ProviderError("Provider Responses output did not complete")
            output = data.get("output")
            if not isinstance(output, list):
                raise ProviderError("Provider Responses output is missing")
            chunks = []
            for item in output:
                if not isinstance(item, dict):
                    raise ProviderError("Provider Responses output is invalid")
                if item.get("status") in ("incomplete", "in_progress"):
                    raise IncompleteResponseError("Provider Responses message is incomplete")
                if item.get("type") != "message":
                    continue  # Reasoning items are metadata, not extra output tokens.
                content = item.get("content")
                if not isinstance(content, list):
                    raise ProviderError("Provider Responses message content is invalid")
                for part in content:
                    if not isinstance(part, dict) or part.get("type") != "output_text" or not isinstance(part.get("text"), str):
                        raise ProviderError("Provider Responses output contains non-text or refusal content")
                    chunks.append(part["text"])
            content = "".join(chunks)
            input_name, output_name = "input_tokens", "output_tokens"
        else:
            choices = data.get("choices")
            if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
                raise ProviderError("Provider Chat output is missing")
            choice = choices[0]
            if choice.get("finish_reason") == "length":
                raise IncompleteResponseError("Provider Chat output was truncated by the token limit")
            if choice.get("finish_reason") != "stop":
                raise ProviderError("Provider Chat output did not finish normally")
            message = choice.get("message")
            if not isinstance(message, dict) or message.get("refusal"):
                raise ProviderError("Provider Chat output has no accepted text")
            content = message.get("content")
            input_name, output_name = "prompt_tokens", "completion_tokens"
        if not isinstance(content, str) or not content.strip():
            raise ProviderError("Provider output text is missing or empty")
        usage = data.get("usage")
        if usage is None:
            usage = {}
        if not isinstance(usage, dict):
            raise ProviderError("Provider returned invalid token usage")
        model = data.get("model", self.profile.model)
        if not isinstance(model, str) or not model:
            raise ProviderError("Provider returned an invalid model name")
        return ModelResult(
            content=content,
            input_tokens=_token(usage, input_name),
            output_tokens=_token(usage, output_name),
            total_tokens=_token(usage, "total_tokens"),
            request_id=request_id or (data.get("id") if isinstance(data.get("id"), str) else None),
            model=model,
            raw_usage=copy.deepcopy(usage),
        )


def create_provider(profile: ProviderProfile) -> _Provider:
    if not isinstance(profile, ProviderProfile):
        raise ValueError("create_provider requires a validated ProviderProfile")
    return _Provider(profile)
