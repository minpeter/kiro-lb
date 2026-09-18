# -*- coding: utf-8 -*-
"""Translation between the OpenAI Responses API and Chat Completions.

The Responses API is item-centered and Chat Completions is message-centered, so
one turn maps like this:

    instructions                  -> a leading system message
    message item                  -> ChatMessage(role, content)
    function_call item            -> assistant message carrying tool_calls
    function_call_output item     -> tool message keyed by tool_call_id
    reasoning item                -> dropped

Reasoning is dropped rather than reinjected: this project forwards reasoning only
from native upstream frames and never feeds it back as request text. Kiro has no
field for a client-supplied reasoning item, so replaying one would either be
silently ignored or rejected.

An item type this module does not know is dropped with a debug line instead of
raising. New item types appear on the client's schedule, and a 400 on an item the
model did not need is worse than answering without it.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Dict, FrozenSet, List, Optional, Tuple

from fastapi import HTTPException
from loguru import logger

from kiro.models_openai import ChatCompletionRequest, ChatMessage, Tool, ToolFunction
from kiro.models_responses import ResponsesInputItem, ResponsesRequest

# Effort values this gateway can forward. ChatCompletionRequest declares a closed
# Literal, so anything outside it has to be mapped or dropped before the model is
# instantiated - a stray value would surface as a 422 on a valid request.
_FORWARDABLE_EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh", "max"}

# Codex knows efforts above the published ceiling. They mean "as much as possible",
# which is what max already asks for.
_EFFORT_ALIASES = {"ultra": "max", "persistent": "max"}


def new_response_id() -> str:
    return f"resp_{uuid.uuid4().hex}"


def new_item_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


# ==================================================================================================
# Request: Responses -> Chat Completions
# ==================================================================================================


def _content_to_text(content: Any) -> Tuple[str, List[Dict[str, Any]]]:
    """Flatten an item's content into text plus OpenAI-shaped image parts.

    Returns the concatenated text and any image parts, so the caller can decide
    between a plain string body and a multimodal list. Images are emitted in the
    Chat Completions shape (``image_url.url``) because that is what
    ``converters_openai`` already knows how to read.
    """
    if content is None:
        return "", []
    if isinstance(content, str):
        return content, []

    texts: List[str] = []
    images: List[Dict[str, Any]] = []
    if not isinstance(content, list):
        return str(content), []

    for part in content:
        data = part if isinstance(part, dict) else getattr(part, "model_dump", lambda: {})()
        if not isinstance(data, dict):
            continue
        kind = data.get("type") or ""
        if kind in ("input_text", "output_text", "text", "summary_text"):
            value = data.get("text")
            if value:
                texts.append(str(value))
        elif kind in ("input_image", "image_url", "image"):
            url = data.get("image_url")
            if isinstance(url, dict):
                url = url.get("url")
            if url:
                images.append({"type": "image_url", "image_url": {"url": url}})
        elif data.get("text"):
            # Unknown part that still carries text: keep the text rather than the
            # label. Dropping it would silently shorten the prompt.
            texts.append(str(data["text"]))

    return "\n".join(texts), images


def _message_from_item(item: ResponsesInputItem) -> Optional[ChatMessage]:
    text, images = _content_to_text(item.content)
    role = item.role or "user"
    if images:
        parts: List[Any] = []
        if text:
            parts.append({"type": "text", "text": text})
        parts.extend(images)
        return ChatMessage(role=role, content=parts)
    if not text:
        return None
    return ChatMessage(role=role, content=text)


def _assistant_tool_call(item: ResponsesInputItem) -> ChatMessage:
    """An assistant message whose only job is to carry one tool call.

    The gateway repairs histories that hold a tool result with no preceding call
    (`converters_core`), but emitting the call properly is cheaper than relying on
    that repair.

    A ``custom_tool_call`` carries its body in ``input`` as a raw string rather than
    JSON ``arguments``; it is wrapped so the history stays valid for a protocol
    where every tool call has arguments.
    """
    if isinstance(item.arguments, str):
        arguments = item.arguments
    elif isinstance(item.input, str):
        arguments = json.dumps({"input": item.input}, ensure_ascii=False)
    else:
        arguments = "{}"

    return ChatMessage(
        role="assistant",
        content=None,
        tool_calls=[
            {
                "id": item.call_id or new_item_id("call"),
                "type": "function",
                "function": {"name": item.name or "", "arguments": arguments},
            }
        ],
    )


def _tool_result(item: ResponsesInputItem) -> ChatMessage:
    """A tool message from a ``function_call_output`` item.

    ``output`` is either a plain string or a list of structured content items, so
    both are flattened to text; a dict is serialized rather than str()-ed, so the
    model sees JSON instead of Python repr.
    """
    output = item.output
    if isinstance(output, str):
        content = output
    elif isinstance(output, list):
        content, _ = _content_to_text(output)
    elif isinstance(output, dict):
        text = output.get("content") or output.get("text")
        content = text if isinstance(text, str) else json.dumps(output, ensure_ascii=False)
    else:
        content = "" if output is None else str(output)

    return ChatMessage(role="tool", content=content, tool_call_id=item.call_id or "")


def _convert_input(
    payload: Optional[Any],
) -> Tuple[List[ChatMessage], List[Dict[str, Any]]]:
    """Turn ``input`` into chat messages, plus any tools declared inline.

    Returns the messages and the tool declarations found in ``additional_tools``
    items. The Codex CLI declares its tools there rather than in the top-level
    ``tools`` field, so the caller has to merge both sources.
    """
    if payload is None:
        return [], []
    if isinstance(payload, str):
        return ([ChatMessage(role="user", content=payload)] if payload else []), []

    messages: List[ChatMessage] = []
    inline_tools: List[Dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, ResponsesInputItem):
            item = ResponsesInputItem.model_validate(item)
        # An item with a role and no type is a message; the spec allows the type
        # to be implied, and clients rely on it.
        kind = item.type or ("message" if item.role else "")

        if kind in ("message", "input_text", "output_text"):
            message = _message_from_item(item)
            if message is not None:
                messages.append(message)
        elif kind in ("function_call", "custom_tool_call"):
            messages.append(_assistant_tool_call(item))
        elif kind in ("function_call_output", "custom_tool_call_output"):
            messages.append(_tool_result(item))
        elif kind == "additional_tools":
            # Not a message: a tool declaration block riding in the input list.
            inline_tools.extend(_flatten_tool_declarations(getattr(item, "tools", None)))
        elif kind == "reasoning":
            logger.debug("Dropping reasoning item: reasoning is never replayed upstream")
        else:
            logger.debug("Dropping unsupported Responses input item: {}", kind or "<untyped>")

    return messages, inline_tools


def _flatten_tool_declarations(entries: Any) -> List[Dict[str, Any]]:
    """Flatten a tool list, descending into ``namespace`` groups.

    The Codex CLI does not send a top-level ``tools`` array. It sends an
    ``additional_tools`` input item whose ``tools`` hold a ``namespace`` entry, and
    the real declarations sit one level down inside it. Treating a namespace as an
    unknown tool type is how a turn ends up with no tools at all, which the model
    answers by inventing the output it could not go and fetch.
    """
    flat: List[Dict[str, Any]] = []
    for entry in entries or []:
        data = entry if isinstance(entry, dict) else getattr(entry, "model_dump", lambda: {})()
        if not isinstance(data, dict):
            continue
        if (data.get("type") or "").lower() == "namespace":
            flat.extend(_flatten_tool_declarations(data.get("tools")))
            continue
        flat.append(data)
    return flat


#: The single property a bridged freeform tool declares. The model answers a JSON
#: schema, so the freeform body has to travel inside one string field, and both
#: directions of the bridge have to agree on its name.
FREEFORM_BODY_FIELD = "input"


def _freeform_description(entry: Dict[str, Any]) -> str:
    """The tool description, plus the instruction the bridge depends on.

    Without this the model has no way to know that the whole program text belongs in
    one field: it would try to split a shell command into arguments that the schema
    does not have.
    """
    description = str(entry.get("description") or "").rstrip()
    syntax = ""
    fmt = entry.get("format")
    if isinstance(fmt, dict):
        syntax = str(fmt.get("syntax") or fmt.get("type") or "")

    note = (
        f"\n\nCall this tool with a single JSON field `{FREEFORM_BODY_FIELD}` holding the "
        "complete tool body verbatim, exactly as it would be written by hand. Do not "
        "wrap it in markdown fences, do not escape it beyond what JSON requires, and do "
        "not split it into other fields."
    )
    if syntax:
        note += f" The body is {syntax} source."
    return description + note


def freeform_tool_names(request_data: ResponsesRequest) -> frozenset[str]:
    """Names declared as freeform (``custom``) tools in this request.

    The response side needs this: a call to one of these names must come back as a
    ``custom_tool_call`` item carrying a raw string, not a ``function_call`` carrying
    JSON arguments. The client dispatches on the item type, so getting it wrong means
    the call is silently ignored.

    Recomputed from the request rather than threaded out of the request converter, so
    that converter keeps one job and one return value.
    """
    declared: List[Any] = list(request_data.tools or [])
    if isinstance(request_data.input, list):
        for item in request_data.input:
            if not isinstance(item, ResponsesInputItem):
                item = ResponsesInputItem.model_validate(item)
            if (item.type or "") == "additional_tools":
                declared.extend(_flatten_tool_declarations(item.tools))

    names = {
        str(entry.get("name"))
        for entry in _flatten_tool_declarations(declared)
        if (entry.get("type") or "").lower() == "custom" and entry.get("name")
    }
    return frozenset(names)


def _convert_tools(tools: Optional[Any]) -> Optional[List[Tool]]:
    """Map Responses tools onto Chat Completions tools.

    ``function`` tools cross over unchanged. A ``custom`` tool is freeform: its input
    is constrained by a grammar rather than a JSON schema, which Kiro's tool spec
    cannot express. Rather than drop it - which leaves the model with no way to act,
    and it answers by inventing the result it could not go and fetch - it is bridged
    as a function with one string field carrying the body verbatim, and the call is
    unwrapped back into a freeform item on the way out.

    The grammar itself is not enforced: Kiro cannot constrain generation, so a
    malformed body reaches the client, which rejects it the same way it would reject
    one from any other provider. That is a weaker guarantee than the native tool, and
    a better one than no tool at all.

    ``local_shell`` and the hosted tools have no Kiro equivalent at all and are still
    dropped: there is nothing to bridge them to.
    """
    if not tools:
        return None

    converted: List[Tool] = []
    for entry in _flatten_tool_declarations(tools):
        kind = (entry.get("type") or "function").lower()
        if kind == "custom":
            name = entry.get("name")
            if not name:
                logger.debug("Dropping freeform tool with no name")
                continue
            logger.debug("Bridging freeform tool {} as a single-field function", name)
            converted.append(
                Tool(
                    type="function",
                    function=ToolFunction(
                        name=str(name),
                        description=_freeform_description(entry),
                        parameters={
                            "type": "object",
                            "properties": {
                                FREEFORM_BODY_FIELD: {
                                    "type": "string",
                                    "description": "The complete tool body, verbatim.",
                                }
                            },
                            "required": [FREEFORM_BODY_FIELD],
                        },
                    ),
                )
            )
            continue
        if kind != "function":
            logger.debug("Dropping unsupported Responses tool type: {}", kind)
            continue

        # Responses puts the fields flat; a client mixing in the nested Chat shape
        # is honoured too.
        candidate = entry.get("function")
        nested: Dict[str, Any] = candidate if isinstance(candidate, dict) else {}
        name = entry.get("name") or nested.get("name")
        if not name:
            logger.debug("Dropping Responses tool with no name")
            continue
        description = entry.get("description") if entry.get("description") is not None else nested.get("description")
        parameters = entry.get("parameters") if entry.get("parameters") is not None else nested.get("parameters")

        converted.append(
            Tool(
                type="function",
                function=ToolFunction(name=name, description=description, parameters=parameters),
            )
        )

    return converted or None


def _convert_tool_choice(choice: Optional[Any]) -> Optional[Any]:
    """Map ``tool_choice`` onto the Chat Completions shape."""
    if choice is None:
        return None
    if isinstance(choice, str):
        return choice
    if isinstance(choice, dict):
        if choice.get("type") == "function":
            name = choice.get("name") or (choice.get("function") or {}).get("name")
            if name:
                return {"type": "function", "function": {"name": name}}
        return choice
    return None


def normalize_effort(effort: Optional[str]) -> Optional[str]:
    """Map a Responses effort onto one this gateway can forward.

    Returns None for a value with no equivalent, which leaves the model on its own
    default instead of guessing a level the caller never asked for.
    """
    if not effort:
        return None
    value = str(effort).strip().lower()
    if value in _FORWARDABLE_EFFORTS:
        return value
    mapped = _EFFORT_ALIASES.get(value)
    if mapped:
        return mapped
    logger.debug("Ignoring unknown Responses reasoning effort: {}", effort)
    return None


def responses_request_to_chat(request_data: ResponsesRequest) -> ChatCompletionRequest:
    """Build the Chat Completions request that serves one Responses request."""
    if request_data.previous_response_id:
        # Honouring this needs the stored prior turn. Answering with only the
        # current input would silently drop the history the client believes is
        # there, so it is refused instead.
        raise HTTPException(
            status_code=400,
            detail=(
                "previous_response_id is not supported: this gateway stores no responses. "
                "Send the full conversation in `input` with store=false."
            ),
        )

    messages: List[ChatMessage] = []
    if request_data.instructions:
        messages.append(ChatMessage(role="system", content=request_data.instructions))
    converted_messages, inline_tools = _convert_input(request_data.input)
    messages.extend(converted_messages)

    if not messages:
        raise HTTPException(status_code=400, detail="`input` must contain at least one message")

    payload: Dict[str, Any] = {
        "model": request_data.model,
        "messages": messages,
        "stream": bool(request_data.stream),
    }

    # Two sources, because Codex uses the second one exclusively: the top-level
    # `tools` field and the `additional_tools` items found in `input`.
    declared: List[Any] = list(request_data.tools or [])
    declared.extend(inline_tools)
    tools = _convert_tools(declared)
    if tools:
        payload["tools"] = tools
    tool_choice = _convert_tool_choice(request_data.tool_choice)
    if tool_choice is not None:
        payload["tool_choice"] = tool_choice
    if request_data.parallel_tool_calls is not None:
        payload["parallel_tool_calls"] = request_data.parallel_tool_calls
    if request_data.max_output_tokens:
        payload["max_tokens"] = request_data.max_output_tokens
    if request_data.temperature is not None:
        payload["temperature"] = request_data.temperature
    if request_data.top_p is not None:
        payload["top_p"] = request_data.top_p

    effort = normalize_effort(request_data.reasoning.effort if request_data.reasoning else None)
    if effort:
        payload["reasoning_effort"] = effort

    return ChatCompletionRequest(**payload)


# ==================================================================================================
# Response: Chat Completions -> Responses
# ==================================================================================================


def usage_block(usage: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Translate chat usage into the Responses shape.

    The nested details objects are always present: Codex parses them into a typed
    struct and a missing key there is a stream-level failure, not a soft one
    (`codex-rs/codex-api/src/sse/responses.rs`).
    """
    usage = usage or {}
    prompt = int(usage.get("prompt_tokens") or 0)
    completion = int(usage.get("completion_tokens") or 0)
    total = int(usage.get("total_tokens") or (prompt + completion))
    return {
        "input_tokens": prompt,
        "input_tokens_details": {"cached_tokens": 0},
        "output_tokens": completion,
        "output_tokens_details": {"reasoning_tokens": 0},
        "total_tokens": total,
    }


def message_item(text: str, item_id: str, status: str = "completed") -> Dict[str, Any]:
    return {
        "id": item_id,
        "type": "message",
        "role": "assistant",
        "status": status,
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }


def reasoning_item(text: str, item_id: str) -> Dict[str, Any]:
    """A reasoning item carrying only a summary.

    ``encrypted_content`` is null because Kiro emits no opaque reasoning blob, and
    inventing one would produce a token the client cannot replay.
    """
    return {
        "id": item_id,
        "type": "reasoning",
        "summary": [{"type": "summary_text", "text": text}] if text else [],
        "content": [],
        "encrypted_content": None,
    }


def function_call_item(call: Dict[str, Any], item_id: str) -> Dict[str, Any]:
    """A function_call item.

    ``arguments`` stays a JSON *string*: the Responses API defines it that way and
    Codex parses it itself.
    """
    function = call.get("function") or {}
    arguments = function.get("arguments")
    if not isinstance(arguments, str):
        arguments = json.dumps(arguments or {}, ensure_ascii=False)
    return {
        "id": item_id,
        "type": "function_call",
        "status": "completed",
        "name": function.get("name") or "",
        "arguments": arguments,
        "call_id": call.get("id") or new_item_id("call"),
    }


def freeform_body(arguments: Any) -> str:
    """Recover the verbatim body a bridged freeform call was asked to carry.

    The model answers the bridge's one-field schema, so the body normally arrives as
    ``{"input": "..."}``. Two fallbacks matter in practice: a model that named the
    single field something else, and one that ignored the schema and emitted the body
    as bare text. Both are recovered rather than surfaced as an empty call, because
    the client cannot act on an empty one either way.
    """
    if not isinstance(arguments, str):
        arguments = json.dumps(arguments or {}, ensure_ascii=False)
    try:
        parsed = json.loads(arguments)
    except (json.JSONDecodeError, TypeError):
        return arguments
    if isinstance(parsed, str):
        return parsed
    if not isinstance(parsed, dict):
        return arguments

    value = parsed.get(FREEFORM_BODY_FIELD)
    if isinstance(value, str):
        return value
    if len(parsed) == 1:
        only = next(iter(parsed.values()))
        if isinstance(only, str):
            return only
    return arguments


def custom_tool_call_item(call: Dict[str, Any], item_id: str) -> Dict[str, Any]:
    """A custom_tool_call item, the freeform counterpart of function_call.

    The client dispatches on the item type, so a freeform tool answered as a
    ``function_call`` is dropped on the floor.
    """
    function = call.get("function") or {}
    return {
        "id": item_id,
        "type": "custom_tool_call",
        "status": "completed",
        "name": function.get("name") or "",
        "input": freeform_body(function.get("arguments")),
        "call_id": call.get("id") or new_item_id("call"),
    }


def output_item_for(
    call: Dict[str, Any],
    item_id: str,
    freeform_tools: FrozenSet[str] = frozenset(),
) -> Dict[str, Any]:
    """Pick the item shape one tool call has to be reported as."""
    name = (call.get("function") or {}).get("name") or ""
    if name in freeform_tools:
        return custom_tool_call_item(call, item_id)
    return function_call_item(call, item_id)


def response_envelope(
    response_id: str,
    model: str,
    status: str,
    output: List[Dict[str, Any]],
    usage: Optional[Dict[str, Any]] = None,
    created_at: Optional[int] = None,
    error: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """The `response` object shared by the events and the non-streaming body."""
    envelope: Dict[str, Any] = {
        "id": response_id,
        "object": "response",
        "created_at": created_at or int(time.time()),
        "status": status,
        "model": model,
        "output": output,
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
        "metadata": {},
        "error": error,
        "incomplete_details": None,
        "instructions": None,
    }
    if usage is not None:
        envelope["usage"] = usage
    return envelope


def chat_completion_to_responses(
    body: Dict[str, Any],
    model: str,
    freeform_tools: FrozenSet[str] = frozenset(),
) -> Dict[str, Any]:
    """Turn a `chat.completion` body into a Responses object.

    Output order is reasoning, then the message, then any tool calls - the order
    the upstream produced them, which is the order a client replays them in.

    ``freeform_tools`` names the tools declared as ``custom``, whose calls have to be
    reported as ``custom_tool_call`` items instead of ``function_call``.
    """
    choices = body.get("choices") or []
    message = (choices[0].get("message") if choices else None) or {}

    output: List[Dict[str, Any]] = []
    reasoning_text = message.get("reasoning") or message.get("reasoning_content") or ""
    if reasoning_text:
        output.append(reasoning_item(str(reasoning_text), new_item_id("rs")))

    text = message.get("content") or ""
    if text:
        output.append(message_item(str(text), new_item_id("msg")))

    for call in message.get("tool_calls") or []:
        if isinstance(call, dict):
            output.append(output_item_for(call, new_item_id("fc"), freeform_tools))

    finish_reason = (choices[0].get("finish_reason") if choices else None) or "stop"
    status = "incomplete" if finish_reason == "length" else "completed"

    envelope = response_envelope(
        response_id=new_response_id(),
        model=body.get("model") or model,
        status=status,
        output=output,
        usage=usage_block(body.get("usage")),
        created_at=body.get("created"),
    )
    if status == "incomplete":
        envelope["incomplete_details"] = {"reason": "max_output_tokens"}
    # The one field a caller needs that the item list buries.
    envelope["output_text"] = str(text)
    return envelope
