# -*- coding: utf-8 -*-
"""
Module for fast token counting.

Uses tiktoken to approximate what the upstream tokenizer would charge. No
vendor here publishes their tokenizer, so the encoding and the correction are
chosen per model family from measurements against Kiro's own
contextUsagePercentage.

Measured slopes (upstream tokens per encoding token, derived from two payload
sizes per case so the fixed per-request overhead cancels out):

    model              lang  per cl100k  per o200k
    claude-sonnet-4    en        0.999      0.999
    claude-sonnet-4    ko        1.166      1.936
    claude-sonnet-4    zh        0.870      1.354
    claude-opus-4.7    ko        1.158      1.922
    gpt-5.6-sol        en        1.000      1.000
    gpt-5.6-sol        ko        0.602      0.999
    gpt-5.6-sol        zh        0.638      0.993
    deepseek-3.2       en        1.004      1.004
    deepseek-3.2       ko        0.707      1.174
    qwen3-coder-next   ko        0.761      1.263
    minimax-m2.5       ko        0.582      0.965
    glm-5              ko        0.958      1.590

Two conclusions drive this module:

1. The correction is a property of the SCRIPT, not of the model. Every family
   sits at 1.00 per cl100k token for Latin text, so a blanket factor inflates
   English and code. Only CJK needs correcting.
2. o200k_base is the GPT family's actual tokenizer (0.99-1.00 across scripts),
   so selecting it removes the need for any correction there.

The large fixed per-request overhead the measurements exposed (roughly 1.7k
tokens for gpt-5.6, 3.9k-4.7k mid-tier, 5.1k-7.5k for Claude) is upstream
prompt scaffolding. It is deliberately NOT modelled here: it depends on the
account and the upstream deployment, not on the text being counted.
"""

import json
import unicodedata
from typing import Any, Dict, List, NamedTuple, Optional

from loguru import logger

# Lazy loading of tiktoken to speed up import, keyed by encoding name
_encodings: Dict[str, Any] = {}

# Correction coefficient for Claude models on CJK text.
# Measured Hangul slope is 1.158-1.166 per cl100k token; Latin sits at 1.00.
# This is what the historical blanket 1.15 was actually describing.
CLAUDE_CORRECTION_FACTOR = 1.15


class TokenProfile(NamedTuple):
    """How one model family should be counted."""

    encoding_name: str
    cjk_correction: float


# cl100k_base plus the measured Hangul slope. Chinese measured lower (0.870),
# so this overestimates Han for Claude rather than underestimating it, which is
# the safe direction for a compaction decision.
_CLAUDE_PROFILE = TokenProfile(encoding_name="cl100k_base", cjk_correction=CLAUDE_CORRECTION_FACTOR)

# o200k_base IS this family's tokenizer: 0.999 ko, 0.993 zh, 1.000 en.
_GPT_PROFILE = TokenProfile(encoding_name="o200k_base", cjk_correction=1.0)

# deepseek/qwen/minimax/glm all sit near o200k for CJK (0.965-1.263, mean ~1.15)
# and at 1.00 per cl100k for Latin. o200k with a mild correction covers both.
_MULTILINGUAL_PROFILE = TokenProfile(encoding_name="o200k_base", cjk_correction=1.15)

_PROFILE_PREFIXES = (
    ("gpt-", _GPT_PROFILE),
    ("o1", _GPT_PROFILE),
    ("o3", _GPT_PROFILE),
    ("deepseek", _MULTILINGUAL_PROFILE),
    ("qwen", _MULTILINGUAL_PROFILE),
    ("minimax", _MULTILINGUAL_PROFILE),
    ("glm", _MULTILINGUAL_PROFILE),
    ("claude", _CLAUDE_PROFILE),
)


def resolve_token_profile(model: Optional[str] = None) -> TokenProfile:
    """
    Selects the encoding and CJK correction for a model name.

    Unknown names fall back to the Claude profile: it charges the most per
    token, so an unrecognised model is over-counted rather than under-counted.
    Never raises - a bad model name must not break token accounting.
    """
    if not model:
        return _CLAUDE_PROFILE

    normalized = model.strip().lower()
    for prefix, profile in _PROFILE_PREFIXES:
        if normalized.startswith(prefix):
            return profile

    return _CLAUDE_PROFILE


def _cjk_ratio(text: str) -> float:
    """Share of CJK characters, used to scale the correction and the fallback.

    Hangul, Han, Hiragana and Katakana all tokenize far denser than Latin under
    a Latin-trained BPE, and they are what the measured correction describes.
    """
    if not text:
        return 0.0

    cjk = 0
    counted = 0
    for char in text:
        if char.isspace():
            continue
        counted += 1
        if unicodedata.category(char) == "Lo":
            cjk += 1

    if counted == 0:
        return 0.0
    return cjk / counted


def _apply_cjk_correction(base_tokens: int, text: str, profile: TokenProfile) -> int:
    """Scales the correction by how much of the text is actually CJK."""
    if profile.cjk_correction == 1.0:
        return base_tokens

    ratio = _cjk_ratio(text)
    if ratio <= 0.0:
        return base_tokens

    factor = 1.0 + (profile.cjk_correction - 1.0) * ratio
    return int(base_tokens * factor)


def _get_encoding(encoding_name: str = "cl100k_base"):
    """
    Lazy initialization of a tiktoken encoding, cached per name.

    Returns:
        tiktoken.Encoding or None if tiktoken is unavailable
    """
    cached = _encodings.get(encoding_name)
    if cached is None:
        try:
            import tiktoken

            _encodings[encoding_name] = tiktoken.get_encoding(encoding_name)
            logger.debug(f"[Tokenizer] Initialized tiktoken with {encoding_name} encoding")
        except ImportError:
            logger.warning(
                "[Tokenizer] tiktoken not installed. "
                "Token counting will use fallback estimation. "
                "Install with: pip install tiktoken"
            )
            _encodings[encoding_name] = False
        except Exception as e:
            logger.error(f"[Tokenizer] Failed to initialize {encoding_name}: {e}")
            _encodings[encoding_name] = False
        cached = _encodings[encoding_name]
    return cached if cached else None


def _fallback_estimate(text: str) -> int:
    """Character-based estimate for when tiktoken is unavailable.

    Latin text runs ~4 characters per token, but CJK measures 1.0-1.1
    characters per token under cl100k_base, so a flat len//4 undercounts
    Korean and Chinese by roughly 3.5x. Splitting the text by script keeps
    both within ~35% of the real count.
    """
    if not text:
        return 0

    cjk_chars = 0
    other_chars = 0
    for char in text:
        if char.isspace():
            continue
        if unicodedata.category(char) == "Lo":
            cjk_chars += 1
        else:
            other_chars += 1

    return int(cjk_chars * 1.05 + other_chars / 3.6) + 1


def count_tokens(text: str, apply_claude_correction: bool = True, model: Optional[str] = None) -> int:
    """
    Counts the number of tokens in text.

    Args:
        text: Text to count tokens for
        apply_claude_correction: Apply the CJK correction (default True)
        model: Model name, selecting the encoding and correction

    Returns:
        Number of tokens (approximate)
    """
    if not text:
        return 0

    profile = resolve_token_profile(model)

    encoding = _get_encoding(profile.encoding_name)
    if encoding:
        try:
            base_tokens = len(encoding.encode(text))
            if apply_claude_correction:
                return _apply_cjk_correction(base_tokens, text, profile)
            return base_tokens
        except Exception as e:
            logger.warning(f"[Tokenizer] Error encoding text: {e}")

    base_estimate = _fallback_estimate(text)
    if apply_claude_correction:
        return _apply_cjk_correction(base_estimate, text, profile)
    return base_estimate


def count_message_tokens(
    messages: List[Dict[str, Any]], apply_claude_correction: bool = True, model: Optional[str] = None
) -> int:
    """
    Counts tokens in a list of chat messages.

    Accounts for OpenAI/Claude message structure:
    - role: ~1 token
    - content: text tokens
    - Service tokens between messages: ~3-4 tokens

    Args:
        messages: List of messages in OpenAI format
        apply_claude_correction: Apply correction coefficient for Claude

    Returns:
        Approximate number of tokens (with Claude correction)
    """
    if not messages:
        return 0

    total_tokens = 0

    for message in messages:
        # Base tokens per message (role, delimiters)
        total_tokens += 4  # ~4 tokens for service information

        # Role tokens (without correction, these are short strings)
        role = message.get("role", "")
        total_tokens += count_tokens(role, apply_claude_correction=apply_claude_correction, model=model)

        # Content tokens
        content = message.get("content")
        if content:
            if isinstance(content, str):
                total_tokens += count_tokens(content, apply_claude_correction=apply_claude_correction, model=model)
            elif isinstance(content, list):
                # Support OpenAI/Anthropic multi-type content blocks
                for item in content:
                    if isinstance(item, dict):
                        item_type = item.get("type")
                        if item_type == "text":
                            total_tokens += count_tokens(
                                item.get("text", ""), apply_claude_correction=apply_claude_correction, model=model
                            )
                        elif item_type in {"image_url", "image"}:
                            # Estimate image as fixed cost to avoid significant undercount
                            total_tokens += 100
                        elif item_type == "tool_use":
                            total_tokens += count_tokens(
                                item.get("id", ""), apply_claude_correction=apply_claude_correction, model=model
                            )
                            total_tokens += count_tokens(
                                item.get("name", ""), apply_claude_correction=apply_claude_correction, model=model
                            )
                            tool_input_str = json.dumps(item.get("input", {}), ensure_ascii=False)
                            total_tokens += count_tokens(
                                tool_input_str, apply_claude_correction=apply_claude_correction, model=model
                            )
                        elif item_type == "tool_result":
                            total_tokens += count_tokens(
                                item.get("tool_use_id", ""),
                                apply_claude_correction=apply_claude_correction,
                                model=model,
                            )
                            if item.get("is_error") is not None:
                                total_tokens += count_tokens(
                                    str(item.get("is_error")),
                                    apply_claude_correction=apply_claude_correction,
                                    model=model,
                                )

                            tool_result_content = item.get("content")
                            if isinstance(tool_result_content, str):
                                total_tokens += count_tokens(
                                    tool_result_content, apply_claude_correction=apply_claude_correction, model=model
                                )
                            elif isinstance(tool_result_content, list):
                                for result_block in tool_result_content:
                                    if isinstance(result_block, dict):
                                        result_type = result_block.get("type")
                                        if result_type == "text":
                                            total_tokens += count_tokens(
                                                result_block.get("text", ""), apply_claude_correction=False
                                            )
                                        elif result_type in {"image_url", "image"}:
                                            total_tokens += 100
                                    else:
                                        total_tokens += count_tokens(
                                            str(result_block),
                                            apply_claude_correction=apply_claude_correction,
                                            model=model,
                                        )
                            elif tool_result_content is not None:
                                total_tokens += count_tokens(
                                    str(tool_result_content),
                                    apply_claude_correction=apply_claude_correction,
                                    model=model,
                                )
                        else:
                            # Unknown block fallback: estimate via JSON to avoid undercount
                            total_tokens += count_tokens(
                                json.dumps(item, ensure_ascii=False), apply_claude_correction=False
                            )
                    else:
                        total_tokens += count_tokens(
                            str(item), apply_claude_correction=apply_claude_correction, model=model
                        )

        # tool_calls tokens (if present)
        tool_calls = message.get("tool_calls")
        if tool_calls:
            for tc in tool_calls:
                total_tokens += 4  # Service tokens
                func = tc.get("function", {})
                total_tokens += count_tokens(
                    func.get("name", ""), apply_claude_correction=apply_claude_correction, model=model
                )
                total_tokens += count_tokens(
                    func.get("arguments", ""), apply_claude_correction=apply_claude_correction, model=model
                )

        # tool_call_id tokens (for tool responses)
        if message.get("tool_call_id"):
            total_tokens += count_tokens(
                message["tool_call_id"], apply_claude_correction=apply_claude_correction, model=model
            )

    # Final service tokens
    total_tokens += 3

    return total_tokens


def count_tools_tokens(
    tools: Optional[List[Dict[str, Any]]], apply_claude_correction: bool = True, model: Optional[str] = None
) -> int:
    """
    Counts tokens in tool definitions.

    Args:
        tools: List of tools in OpenAI format
        apply_claude_correction: Apply correction coefficient for Claude

    Returns:
        Approximate number of tokens (with Claude correction)
    """
    if not tools:
        return 0

    total_tokens = 0

    for tool in tools:
        total_tokens += 4  # Service tokens

        # Support both OpenAI standard tools and Anthropic/OpenAI flat tools
        if tool.get("type") == "function" and isinstance(tool.get("function"), dict):
            tool_payload = tool.get("function", {})
        else:
            tool_payload = tool

        # Name / description
        total_tokens += count_tokens(
            tool_payload.get("name", ""), apply_claude_correction=apply_claude_correction, model=model
        )
        total_tokens += count_tokens(
            tool_payload.get("description", ""), apply_claude_correction=apply_claude_correction, model=model
        )

        # JSON schema（Anthropic: input_schema, OpenAI: parameters）
        params = tool_payload.get("input_schema")
        if params is None:
            params = tool_payload.get("parameters")
        if params is not None:
            params_str = json.dumps(params, ensure_ascii=False)
            total_tokens += count_tokens(params_str, apply_claude_correction=apply_claude_correction, model=model)

    return total_tokens


def count_system_tokens(
    system_prompt: Optional[Any], apply_claude_correction: bool = True, model: Optional[str] = None
) -> int:
    """
    Counts tokens in system prompt.

    Supports both plain string and Anthropic block list.

    Args:
        system_prompt: System prompt (str / list of blocks)
        apply_claude_correction: Apply correction coefficient for Claude

    Returns:
        Approximate number of tokens
    """
    if not system_prompt:
        return 0

    total_tokens = 0

    if isinstance(system_prompt, str):
        total_tokens += count_tokens(system_prompt, apply_claude_correction=apply_claude_correction, model=model)
    elif isinstance(system_prompt, list):
        for block in system_prompt:
            if isinstance(block, dict):
                # Count text content, support prompt caching structure
                total_tokens += count_tokens(
                    block.get("text", ""), apply_claude_correction=apply_claude_correction, model=model
                )
                if block.get("cache_control") is not None:
                    total_tokens += count_tokens(
                        json.dumps(block.get("cache_control"), ensure_ascii=False), apply_claude_correction=False
                    )
            else:
                total_tokens += count_tokens(str(block), apply_claude_correction=apply_claude_correction, model=model)
    else:
        total_tokens += count_tokens(str(system_prompt), apply_claude_correction=apply_claude_correction, model=model)

    return total_tokens


def estimate_request_tokens(
    messages: List[Dict[str, Any]],
    tools: Optional[List[Dict[str, Any]]] = None,
    system_prompt: Optional[Any] = None,
    apply_claude_correction: bool = True,
    model: Optional[str] = None,
) -> Dict[str, int]:
    """
    Estimates total number of tokens in request.

    Args:
        messages: List of messages
        tools: List of tools (optional)
        system_prompt: System prompt (optional, string or Anthropic content blocks)
        apply_claude_correction: Apply correction coefficient for Claude

    Returns:
        Dictionary with token breakdown:
        - messages_tokens: message tokens
        - tools_tokens: tool tokens
        - system_tokens: system prompt tokens
        - total_tokens: total count
    """
    messages_tokens = count_message_tokens(messages, apply_claude_correction=apply_claude_correction, model=model)
    tools_tokens = count_tools_tokens(tools, apply_claude_correction=apply_claude_correction, model=model)
    system_tokens = count_system_tokens(system_prompt, apply_claude_correction=apply_claude_correction, model=model)

    return {
        "messages_tokens": messages_tokens,
        "tools_tokens": tools_tokens,
        "system_tokens": system_tokens,
        "total_tokens": messages_tokens + tools_tokens + system_tokens,
    }
