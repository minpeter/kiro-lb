# -*- coding: utf-8 -*-
"""Request schemas for the OpenAI Responses API (`POST /v1/responses`).

The Responses API is item-centered where Chat Completions is message-centered: a
turn is a list of typed items (`message`, `function_call`, `function_call_output`,
`reasoning`) rather than a list of messages. This module only models the request;
the response side is assembled as plain dicts, because the event stream carries
partial objects that no single model describes.

Every model is extra-open, matching `models_openai.py`. The Responses schema
grows faster than this gateway can track, and rejecting an unknown field would
turn a harmless addition into a 422 on a valid request.

Field notes worth keeping:

* ``input`` is a string or a list of items. The bare-string form is in the spec
  and clients do use it for one-shot prompts.
* ``reasoning.effort`` is deliberately a free-form string. Codex can send
  ``ultra``, ``persistent`` or a model-defined value this gateway has never heard
  of (``codex-rs/protocol/src/openai_models.rs``); a closed Literal here would
  reject a legitimate request before the converter ever gets a chance to clamp it.
* ``store``, ``include``, ``prompt_cache_key``, ``service_tier``, ``text`` and
  ``client_metadata`` are accepted and ignored. They describe server-side
  behaviour this gateway does not implement, and refusing them would break
  clients that always send them.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel


class ResponsesContentPart(BaseModel):
    """One part of an item's content: ``input_text``, ``output_text``, ``input_image``."""

    type: Optional[str] = None
    text: Optional[str] = None
    image_url: Optional[str] = None
    file_id: Optional[str] = None
    detail: Optional[str] = None

    model_config = {"extra": "allow"}


class ResponsesInputItem(BaseModel):
    """One item of ``input``.

    A single flat model rather than a tagged union: the item types share most of
    their fields, the discriminator is read explicitly by the converter, and an
    item type this gateway does not know must be droppable rather than fatal.
    """

    type: Optional[str] = None

    # message
    role: Optional[str] = None
    content: Optional[Union[str, List[ResponsesContentPart], List[Any], Any]] = None

    # function_call / function_call_output
    name: Optional[str] = None
    arguments: Optional[str] = None
    call_id: Optional[str] = None
    output: Optional[Any] = None

    # custom_tool_call: a freeform tool's body is a raw string, not JSON arguments
    input: Optional[str] = None

    # additional_tools: the Codex CLI declares its tools here rather than in the
    # top-level `tools` field, nested one level down inside a `namespace` entry
    tools: Optional[List[Any]] = None

    # reasoning
    summary: Optional[List[Any]] = None
    encrypted_content: Optional[str] = None

    id: Optional[str] = None
    status: Optional[str] = None

    model_config = {"extra": "allow"}


class ResponsesTool(BaseModel):
    """A tool declaration.

    Responses puts the function fields flat on the tool and calls the schema
    ``parameters``; Chat Completions nests them under ``function``. Both shapes
    are accepted here so a client mixing them still works.
    """

    type: Optional[str] = "function"
    name: Optional[str] = None
    description: Optional[str] = None
    parameters: Optional[Dict[str, Any]] = None
    strict: Optional[bool] = None
    function: Optional[Dict[str, Any]] = None

    model_config = {"extra": "allow"}


class ResponsesReasoning(BaseModel):
    """The ``reasoning`` block. ``effort`` is free-form on purpose."""

    effort: Optional[str] = None
    summary: Optional[str] = None

    model_config = {"extra": "allow"}


class ResponsesRequest(BaseModel):
    """Request body for ``POST /v1/responses``."""

    model: str
    input: Optional[Union[str, List[ResponsesInputItem]]] = None
    instructions: Optional[str] = None
    stream: bool = False

    tools: Optional[List[ResponsesTool]] = None
    tool_choice: Optional[Union[str, Dict[str, Any]]] = None
    parallel_tool_calls: Optional[bool] = None

    reasoning: Optional[ResponsesReasoning] = None
    max_output_tokens: Optional[int] = None
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    metadata: Optional[Dict[str, Any]] = None

    # Server-side state this gateway does not keep. Declared so the converter can
    # refuse it explicitly instead of answering a truncated conversation.
    previous_response_id: Optional[str] = None

    # Accepted and ignored.
    store: Optional[bool] = None
    include: Optional[List[str]] = None
    prompt_cache_key: Optional[str] = None
    service_tier: Optional[str] = None
    text: Optional[Dict[str, Any]] = None
    client_metadata: Optional[Dict[str, Any]] = None

    model_config = {"extra": "allow"}
