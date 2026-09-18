# -*- coding: utf-8 -*-

"""
Unit tests for the OpenAI Responses API endpoint (POST /v1/responses).

Three layers:
- request translation, Responses -> Chat Completions (converters_responses)
- stream translation, chat chunks -> Responses events (streaming_responses)
- the route itself, end to end through the real chat pipeline with a mocked
  upstream

The event-name test is the load-bearing one: the Codex CLI dispatches on those
exact strings (codex-rs/codex-api/src/sse/responses.rs), so a rename here breaks
it silently.
"""

import json
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi import HTTPException
from fastapi.responses import JSONResponse

from kiro.converters_responses import (
    chat_completion_to_responses,
    freeform_body,
    freeform_tool_names,
    normalize_effort,
    responses_request_to_chat,
)
from kiro.models_responses import ResponsesRequest
from kiro.routes_openai import router
from kiro.streaming_responses import translate_chat_stream_to_responses

# Every event type the Codex CLI parses or explicitly ignores. Anything this
# gateway emits has to be in here, or the client drops it on the floor.
CODEX_KNOWN_EVENTS = {
    "response.created",
    "response.in_progress",
    "response.output_item.added",
    "response.output_item.done",
    "response.output_text.delta",
    "response.output_text.done",
    "response.content_part.added",
    "response.content_part.done",
    "response.function_call_arguments.delta",
    "response.function_call_arguments.done",
    "response.custom_tool_call_input.delta",
    "response.custom_tool_call_input.done",
    "response.reasoning_summary_part.added",
    "response.reasoning_summary_part.done",
    "response.reasoning_summary_text.delta",
    "response.reasoning_summary_text.done",
    "response.reasoning_text.delta",
    "response.completed",
    "response.failed",
    "response.incomplete",
}


def build_request(**overrides) -> ResponsesRequest:
    payload = {
        "model": "gpt-5.6-luna",
        "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Hello"}]}],
    }
    payload.update(overrides)
    return ResponsesRequest(**payload)


async def collect(chunks, model="gpt-5.6-luna"):
    """Run the translator and return the parsed events."""

    async def source():
        for chunk in chunks:
            yield chunk

    events = []
    async for raw in translate_chat_stream_to_responses(source(), model=model):
        for line in raw.splitlines():
            if line.startswith("data:"):
                events.append(json.loads(line[len("data:") :].strip()))
    return events


def chat_chunk(**delta) -> bytes:
    payload = {
        "id": "chatcmpl-test",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "gpt-5.6-luna",
        "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
    }
    return f"data: {json.dumps(payload)}\n\n".encode()


def chat_final(finish_reason="stop", usage=None) -> bytes:
    payload = {
        "id": "chatcmpl-test",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "gpt-5.6-luna",
        "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
        "usage": usage or {"prompt_tokens": 11, "completion_tokens": 2, "total_tokens": 13},
    }
    return f"data: {json.dumps(payload)}\n\n".encode()


# =============================================================================
# Route registration
# =============================================================================


class TestRouteRegistration:
    """The endpoint has to exist, as POST, on the OpenAI router."""

    def test_responses_route_is_registered(self):
        """
        What it does: Verifies /v1/responses exists on the router.
        Purpose: The Codex CLI 0.94+ talks to no other path.
        """
        paths = [route.path for route in router.routes if hasattr(route, "methods")]
        print(f"Result: {paths}")
        assert "/v1/responses" in paths

    def test_responses_route_accepts_post_only(self):
        """
        What it does: Verifies the method set.
        Purpose: A GET on this path is a client bug, not a listing.
        """
        route = next(r for r in router.routes if getattr(r, "path", None) == "/v1/responses")
        print(f"Result: {route.methods}")
        assert route.methods == {"POST"}

    def test_responses_route_declares_auth(self):
        """
        What it does: Verifies the route carries its own auth dependency.
        Purpose: Calling chat_completions() as a function skips the dependencies
                 FastAPI attached to that route, so this one must declare its own
                 or the endpoint is open.
        """
        route = next(r for r in router.routes if getattr(r, "path", None) == "/v1/responses")
        names = [getattr(d.call, "__name__", "") for d in route.dependant.dependencies]
        print(f"Result: {names}")
        assert any("verify_api_key" in name for name in names)


# =============================================================================
# Request translation
# =============================================================================


class TestRequestTranslation:
    """Responses request -> ChatCompletionRequest."""

    def test_instructions_become_a_leading_system_message(self):
        """
        What it does: Verifies `instructions` maps to a system message, first.
        Purpose: Codex puts the whole agent prompt there.
        """
        chat = responses_request_to_chat(build_request(instructions="Be terse."))

        print(f"Result: {[(m.role, m.content) for m in chat.messages]}")
        assert chat.messages[0].role == "system"
        assert chat.messages[0].content == "Be terse."

    def test_message_item_becomes_a_chat_message(self):
        """
        What it does: Verifies an input_text part flattens to a string body.
        Purpose: The common case of every turn.
        """
        chat = responses_request_to_chat(build_request())

        print(f"Result: {[(m.role, m.content) for m in chat.messages]}")
        assert chat.messages[-1].role == "user"
        assert chat.messages[-1].content == "Hello"

    def test_bare_string_input_is_accepted(self):
        """
        What it does: Verifies `input` as a plain string works.
        Purpose: The spec allows it and clients use it for one-shot prompts.
        """
        chat = responses_request_to_chat(build_request(input="just this"))

        print(f"Result: {[(m.role, m.content) for m in chat.messages]}")
        assert chat.messages == [chat.messages[0]]
        assert chat.messages[0].content == "just this"

    def test_untyped_item_with_a_role_is_treated_as_a_message(self):
        """
        What it does: Verifies an item with a role but no type still converts.
        Purpose: The type is implied in the spec and clients omit it.
        """
        chat = responses_request_to_chat(build_request(input=[{"role": "user", "content": "hi"}]))

        print(f"Result: {[(m.role, m.content) for m in chat.messages]}")
        assert chat.messages[0].role == "user"
        assert chat.messages[0].content == "hi"

    def test_function_call_item_becomes_assistant_tool_calls(self):
        """
        What it does: Verifies a function_call item becomes an assistant message
                      carrying tool_calls, keyed by call_id.
        Purpose: Without the preceding call, the tool result has nothing to attach to.
        """
        chat = responses_request_to_chat(
            build_request(
                input=[{"type": "function_call", "name": "read_file", "arguments": '{"path":"a"}', "call_id": "call_1"}]
            )
        )

        message = chat.messages[0]
        print(f"Result: {message.role} {message.tool_calls}")
        assert message.role == "assistant"
        assert message.tool_calls[0]["id"] == "call_1"
        assert message.tool_calls[0]["function"]["name"] == "read_file"
        assert message.tool_calls[0]["function"]["arguments"] == '{"path":"a"}'

    def test_function_call_output_becomes_a_tool_message(self):
        """
        What it does: Verifies function_call_output maps to role=tool with the id.
        Purpose: This is how a turn reports what a tool returned.
        """
        chat = responses_request_to_chat(
            build_request(input=[{"type": "function_call_output", "call_id": "call_1", "output": "file contents"}])
        )

        message = chat.messages[0]
        print(f"Result: {message.role} {message.tool_call_id} {message.content}")
        assert message.role == "tool"
        assert message.tool_call_id == "call_1"
        assert message.content == "file contents"

    def test_structured_function_call_output_is_flattened(self):
        """
        What it does: Verifies a list-shaped output flattens to text.
        Purpose: The payload type is either a string or content items.
        """
        chat = responses_request_to_chat(
            build_request(
                input=[
                    {
                        "type": "function_call_output",
                        "call_id": "c",
                        "output": [{"type": "output_text", "text": "line one"}],
                    }
                ]
            )
        )

        print(f"Result: {chat.messages[0].content}")
        assert chat.messages[0].content == "line one"

    def test_reasoning_item_is_dropped(self):
        """
        What it does: Verifies a reasoning item produces no message.
        Purpose: Project convention - reasoning is forwarded only from native
                 upstream frames, never replayed as request text.
        """
        chat = responses_request_to_chat(
            build_request(
                input=[
                    {"type": "reasoning", "summary": [{"type": "summary_text", "text": "thought"}]},
                    {"type": "message", "role": "user", "content": "hi"},
                ]
            )
        )

        print(f"Result: {[(m.role, m.content) for m in chat.messages]}")
        assert len(chat.messages) == 1
        assert chat.messages[0].content == "hi"

    def test_unknown_item_type_is_dropped_not_fatal(self):
        """
        What it does: Verifies an unrecognised item type does not raise.
        Purpose: New item types ship on the client's schedule; a 400 on one the
                 model did not need is worse than answering without it.
        """
        chat = responses_request_to_chat(
            build_request(
                input=[
                    {"type": "local_shell_call", "call_id": "x", "status": "completed"},
                    {"type": "message", "role": "user", "content": "hi"},
                ]
            )
        )

        print(f"Result: {[(m.role, m.content) for m in chat.messages]}")
        assert len(chat.messages) == 1

    def test_image_part_becomes_a_multimodal_content_list(self):
        """
        What it does: Verifies input_image maps to the chat image_url shape.
        Purpose: converters_openai reads images in that shape only.
        """
        chat = responses_request_to_chat(
            build_request(
                input=[
                    {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "look"},
                            {"type": "input_image", "image_url": "data:image/png;base64,AAA"},
                        ],
                    }
                ]
            )
        )

        parts = chat.messages[0].content
        print(f"Result: {parts}")
        assert parts[0] == {"type": "text", "text": "look"}
        assert parts[1]["image_url"]["url"] == "data:image/png;base64,AAA"

    def test_flat_tool_becomes_a_nested_chat_tool(self):
        """
        What it does: Verifies a Responses tool maps onto the chat tool shape.
        Purpose: Responses puts the fields flat and names the schema `parameters`.
        """
        chat = responses_request_to_chat(
            build_request(
                tools=[
                    {
                        "type": "function",
                        "name": "shell",
                        "description": "run",
                        "parameters": {"type": "object", "properties": {}},
                    }
                ]
            )
        )

        tool = chat.tools[0]
        print(f"Result: {tool}")
        assert tool.type == "function"
        assert tool.function.name == "shell"
        assert tool.function.parameters == {"type": "object", "properties": {}}

    def test_non_function_tools_are_dropped(self):
        """
        What it does: Verifies local_shell and custom tools do not cross over.
        Purpose: Kiro has no equivalent, and declaring one invites a rejected payload.
        """
        chat = responses_request_to_chat(
            build_request(tools=[{"type": "local_shell"}, {"type": "function", "name": "ok", "parameters": {}}])
        )

        print(f"Result: {[t.function.name for t in chat.tools]}")
        assert len(chat.tools) == 1
        assert chat.tools[0].function.name == "ok"

    def test_additional_tools_item_declares_tools(self):
        """
        What it does: Verifies tools declared in an `additional_tools` input item
                      reach the chat request, including through a `namespace` group.
        Purpose: The Codex CLI sends no top-level `tools` field at all - it puts the
                 declarations here, nested one level down. Treating the item as
                 unknown left the turn with no tools, and the model answered by
                 inventing the output it could not go and fetch.
        """
        chat = responses_request_to_chat(
            build_request(
                input=[
                    {
                        "type": "additional_tools",
                        "role": "developer",
                        "tools": [
                            {
                                "type": "namespace",
                                "name": "functions",
                                "tools": [
                                    {"type": "custom", "name": "exec", "format": {"type": "grammar"}},
                                    {"type": "function", "name": "wait", "parameters": {"type": "object"}},
                                ],
                            }
                        ],
                    },
                    {"type": "message", "role": "user", "content": "hi"},
                ]
            )
        )

        print(f"Result: {[t.function.name for t in chat.tools or []]}")
        assert sorted(t.function.name for t in chat.tools) == ["exec", "wait"]

    def test_additional_tools_item_produces_no_message(self):
        """
        What it does: Verifies the declaration block is not sent as prompt text.
        Purpose: It is a tool list, not a turn; forwarding it as a developer message
                 would put a JSON blob in the conversation.
        """
        chat = responses_request_to_chat(
            build_request(
                input=[
                    {"type": "additional_tools", "role": "developer", "tools": []},
                    {"type": "message", "role": "user", "content": "hi"},
                ]
            )
        )

        print(f"Result: {[(m.role, m.content) for m in chat.messages]}")
        assert len(chat.messages) == 1
        assert chat.messages[0].role == "user"

    def test_custom_tool_call_history_becomes_tool_calls(self):
        """
        What it does: Verifies a custom_tool_call item replays as a tool call, with
                      its raw `input` wrapped into JSON arguments.
        Purpose: A freeform call carries `input`, not `arguments`; dropping it would
                 leave the following output with nothing to attach to.
        """
        chat = responses_request_to_chat(
            build_request(
                input=[
                    {"type": "custom_tool_call", "name": "exec", "call_id": "c1", "input": "console.log(1)"},
                    {"type": "custom_tool_call_output", "call_id": "c1", "output": "1"},
                ]
            )
        )

        call, result = chat.messages
        print(f"Result: {call.tool_calls} / {result.content}")
        assert call.role == "assistant"
        assert json.loads(call.tool_calls[0]["function"]["arguments"]) == {"input": "console.log(1)"}
        assert result.role == "tool"
        assert result.tool_call_id == "c1"

    def test_freeform_tool_is_bridged_as_a_single_string_field(self):
        """
        What it does: Verifies a custom tool becomes a function with one string field.
        Purpose: Kiro tools need a JSON schema, so a grammar-constrained body has to
                 travel inside one field. Dropping the tool instead left the model
                 with no way to act, and it invented the result.
        """
        chat = responses_request_to_chat(
            build_request(
                tools=[
                    {
                        "type": "custom",
                        "name": "exec",
                        "description": "Run code",
                        "format": {"type": "grammar", "syntax": "lark"},
                    }
                ]
            )
        )

        tool = chat.tools[0]
        print(f"Result: {tool.function.name} {tool.function.parameters}")
        assert tool.type == "function"
        assert tool.function.name == "exec"
        assert tool.function.parameters["required"] == ["input"]
        assert tool.function.parameters["properties"]["input"]["type"] == "string"

    def test_freeform_bridge_tells_the_model_how_to_answer(self):
        """
        What it does: Verifies the description carries the one-field instruction and
                      names the body syntax.
        Purpose: Without it the model splits a program into fields the schema lacks.
        """
        chat = responses_request_to_chat(
            build_request(
                tools=[
                    {
                        "type": "custom",
                        "name": "exec",
                        "description": "Run code",
                        "format": {"type": "grammar", "syntax": "lark"},
                    }
                ]
            )
        )

        description = chat.tools[0].function.description
        print(f"Result: {description}")
        assert "Run code" in description
        assert "`input`" in description
        assert "lark source" in description

    def test_freeform_tool_names_are_reported(self):
        """
        What it does: Verifies freeform_tool_names finds custom tools in both places.
        Purpose: The response side needs the set to pick the item type, and Codex
                 declares its tools inside an additional_tools item.
        """
        request = build_request(
            tools=[{"type": "custom", "name": "top_level"}],
            input=[
                {
                    "type": "additional_tools",
                    "tools": [
                        {
                            "type": "namespace",
                            "name": "functions",
                            "tools": [
                                {"type": "custom", "name": "exec"},
                                {"type": "function", "name": "wait", "parameters": {}},
                            ],
                        }
                    ],
                },
                {"type": "message", "role": "user", "content": "hi"},
            ],
        )

        names = freeform_tool_names(request)
        print(f"Result: {sorted(names)}")
        assert names == frozenset({"top_level", "exec"})

    def test_local_shell_tool_is_still_dropped(self):
        """
        What it does: Verifies local_shell does not cross over.
        Purpose: Unlike a freeform tool there is nothing to bridge it to - it names a
                 capability the upstream does not have.
        """
        chat = responses_request_to_chat(
            build_request(tools=[{"type": "local_shell"}, {"type": "function", "name": "ok", "parameters": {}}])
        )

        print(f"Result: {[t.function.name for t in chat.tools]}")
        assert [t.function.name for t in chat.tools] == ["ok"]

    def test_tool_choice_function_is_remapped(self):
        """
        What it does: Verifies the object form is translated to the chat shape.
        Purpose: Responses names the function inline; chat nests it.
        """
        chat = responses_request_to_chat(build_request(tool_choice={"type": "function", "name": "shell"}))

        print(f"Result: {chat.tool_choice}")
        assert chat.tool_choice == {"type": "function", "function": {"name": "shell"}}

    def test_max_output_tokens_maps_to_max_tokens(self):
        """
        What it does: Verifies the token ceiling is carried over.
        Purpose: Different field name, same meaning.
        """
        chat = responses_request_to_chat(build_request(max_output_tokens=4096))

        print(f"Result: {chat.max_tokens}")
        assert chat.max_tokens == 4096

    def test_previous_response_id_is_refused(self):
        """
        What it does: Verifies a stored-state request gets a 400.
        Purpose: The gateway keeps no responses. Answering with only the current
                 input would silently drop history the client believes is there.
        """
        with pytest.raises(HTTPException) as exc_info:
            responses_request_to_chat(build_request(previous_response_id="resp_abc"))

        print(f"Result: {exc_info.value.status_code} {exc_info.value.detail}")
        assert exc_info.value.status_code == 400
        assert "previous_response_id" in exc_info.value.detail

    def test_empty_input_is_refused(self):
        """
        What it does: Verifies a request with nothing to answer gets a 400.
        Purpose: An empty payload upstream is a REQUEST_BODY_INVALID round trip.
        """
        with pytest.raises(HTTPException) as exc_info:
            responses_request_to_chat(build_request(input=[]))

        print(f"Result: {exc_info.value.status_code}")
        assert exc_info.value.status_code == 400

    def test_ignored_fields_do_not_break_the_request(self):
        """
        What it does: Verifies store/include/prompt_cache_key are accepted.
        Purpose: Codex always sends them; rejecting them breaks every turn.
        """
        chat = responses_request_to_chat(
            build_request(store=False, include=["reasoning.encrypted_content"], prompt_cache_key="k")
        )

        print(f"Result: {chat.model}")
        assert chat.model == "gpt-5.6-luna"


class TestEffortNormalization:
    """`reasoning.effort` is free-form on the wire and closed in our model."""

    @pytest.mark.parametrize("value", ["none", "minimal", "low", "medium", "high", "xhigh", "max"])
    def test_forwardable_efforts_pass_through(self, value):
        """
        What it does: Verifies the supported levels survive untouched.
        Purpose: They are exactly the Literal on ChatCompletionRequest.
        """
        assert normalize_effort(value) == value

    @pytest.mark.parametrize("value", ["ultra", "persistent"])
    def test_efforts_above_the_ceiling_clamp_to_max(self, value):
        """
        What it does: Verifies Codex's higher levels map to max.
        Purpose: They mean "as much as possible", which is what max asks for; a
                 raw pass-through would 422 on a valid request.
        """
        assert normalize_effort(value) == "max"

    def test_unknown_effort_is_dropped(self):
        """
        What it does: Verifies an unrecognised level yields None.
        Purpose: Leave the model on its own default rather than invent a level.
        """
        assert normalize_effort("turbo") is None

    def test_effort_reaches_the_chat_request(self):
        """
        What it does: Verifies the mapped value lands on reasoning_effort.
        Purpose: End of the path the two tests above only cover in isolation.
        """
        chat = responses_request_to_chat(build_request(reasoning={"effort": "ultra", "summary": "auto"}))

        print(f"Result: {chat.reasoning_effort}")
        assert chat.reasoning_effort == "max"

    def test_unknown_effort_leaves_the_field_unset(self):
        """
        What it does: Verifies a dropped effort does not set the field.
        Purpose: None means "model default", which is the intent.
        """
        chat = responses_request_to_chat(build_request(reasoning={"effort": "turbo"}))

        print(f"Result: {chat.reasoning_effort}")
        assert chat.reasoning_effort is None


# =============================================================================
# Stream translation
# =============================================================================


class TestStreamTranslation:
    """Chat Completions chunks -> Responses events."""

    @pytest.mark.asyncio
    async def test_text_turn_emits_the_expected_event_order(self):
        """
        What it does: Verifies the lifecycle of a plain text turn.
        Purpose: Codex needs created ... output_item.done ... completed to close a turn.
        """
        events = await collect([chat_chunk(role="assistant", content=""), chat_chunk(content="ok"), chat_final()])

        types = [event["type"] for event in events]
        print(f"Result: {types}")
        assert types[0] == "response.created"
        assert types[-1] == "response.completed"
        assert "response.output_text.delta" in types
        assert "response.output_item.done" in types

    @pytest.mark.asyncio
    async def test_every_emitted_type_is_one_codex_knows(self):
        """
        What it does: Verifies no event name falls outside the client's set.
        Purpose: Codex dispatches on these exact strings; an unknown one is dropped
                 silently, which looks like a hang rather than an error.
        """
        events = await collect(
            [
                chat_chunk(reasoning="thinking"),
                chat_chunk(content="ok"),
                chat_chunk(
                    tool_calls=[
                        {"index": 0, "id": "call_1", "type": "function", "function": {"name": "f", "arguments": "{}"}}
                    ]
                ),
                chat_final(finish_reason="tool_calls"),
            ]
        )

        types = {event["type"] for event in events}
        print(f"Result: {sorted(types)}")
        assert types <= CODEX_KNOWN_EVENTS, f"unknown events: {types - CODEX_KNOWN_EVENTS}"

    @pytest.mark.asyncio
    async def test_sequence_numbers_are_monotonic_from_zero(self):
        """
        What it does: Verifies every event is numbered in order.
        Purpose: Clients use the numbers to detect a dropped event.
        """
        events = await collect([chat_chunk(content="a"), chat_chunk(content="b"), chat_final()])

        numbers = [event["sequence_number"] for event in events]
        print(f"Result: {numbers}")
        assert numbers == list(range(len(numbers)))

    @pytest.mark.asyncio
    async def test_text_deltas_carry_the_content(self):
        """
        What it does: Verifies each content delta becomes one output_text.delta.
        Purpose: This is the visible answer.
        """
        events = await collect([chat_chunk(content="he"), chat_chunk(content="llo"), chat_final()])

        deltas = [e["delta"] for e in events if e["type"] == "response.output_text.delta"]
        print(f"Result: {deltas}")
        assert deltas == ["he", "llo"]

    @pytest.mark.asyncio
    async def test_completed_carries_the_assembled_message(self):
        """
        What it does: Verifies the final response object holds the whole text.
        Purpose: A client that ignores deltas reads the output list instead.
        """
        events = await collect([chat_chunk(content="he"), chat_chunk(content="llo"), chat_final()])

        completed = events[-1]["response"]
        print(f"Result: {completed['output']}")
        assert completed["output"][0]["content"][0]["text"] == "hello"
        assert completed["status"] == "completed"

    @pytest.mark.asyncio
    async def test_completed_carries_usage_in_the_responses_shape(self):
        """
        What it does: Verifies usage is translated, details objects included.
        Purpose: Codex parses usage into a typed struct; a missing nested key is a
                 stream-level failure there, not a soft one.
        """
        events = await collect([chat_chunk(content="ok"), chat_final()])

        usage = events[-1]["response"]["usage"]
        print(f"Result: {usage}")
        assert usage["input_tokens"] == 11
        assert usage["output_tokens"] == 2
        assert usage["total_tokens"] == 13
        assert usage["input_tokens_details"]["cached_tokens"] == 0
        assert usage["output_tokens_details"]["reasoning_tokens"] == 0

    @pytest.mark.asyncio
    async def test_reasoning_becomes_summary_events_before_the_message(self):
        """
        What it does: Verifies reasoning deltas map to summary events, and that the
                      reasoning item closes before the message item opens.
        Purpose: The output list has to come out in the order a client replays it.
        """
        events = await collect([chat_chunk(reasoning="thinking"), chat_chunk(content="ok"), chat_final()])

        types = [event["type"] for event in events]
        print(f"Result: {types}")
        reasoning_done = types.index("response.reasoning_summary_text.done")
        message_added = [
            index
            for index, event in enumerate(events)
            if event["type"] == "response.output_item.added" and event["item"]["type"] == "message"
        ][0]
        assert reasoning_done < message_added

    @pytest.mark.asyncio
    async def test_reasoning_only_turn_still_closes_its_item(self):
        """
        What it does: Verifies a turn that reasons and says nothing is well formed.
        Purpose: The item must not be left open when no content follows.
        """
        events = await collect([chat_chunk(reasoning="thinking"), chat_final()])

        types = [event["type"] for event in events]
        print(f"Result: {types}")
        assert "response.reasoning_summary_text.done" in types
        assert types[-1] == "response.completed"
        assert events[-1]["response"]["output"][0]["type"] == "reasoning"

    @pytest.mark.asyncio
    async def test_tool_call_becomes_a_function_call_item(self):
        """
        What it does: Verifies a tool call maps to a function_call item.
        Purpose: This is how Codex learns it has to run something.
        """
        events = await collect(
            [
                chat_chunk(
                    tool_calls=[
                        {
                            "index": 0,
                            "id": "call_7",
                            "type": "function",
                            "function": {"name": "shell", "arguments": '{"cmd":"ls"}'},
                        }
                    ]
                ),
                chat_final(finish_reason="tool_calls"),
            ]
        )

        item = next(
            event["item"]
            for event in events
            if event["type"] == "response.output_item.done" and event["item"]["type"] == "function_call"
        )
        print(f"Result: {item}")
        assert item["name"] == "shell"
        assert item["call_id"] == "call_7"

    @pytest.mark.asyncio
    async def test_function_call_arguments_stay_a_json_string(self):
        """
        What it does: Verifies arguments are a string, not a parsed object.
        Purpose: The Responses API defines it that way and Codex parses it itself.
        """
        events = await collect(
            [
                chat_chunk(
                    tool_calls=[
                        {
                            "index": 0,
                            "id": "c",
                            "type": "function",
                            "function": {"name": "f", "arguments": '{"a":1}'},
                        }
                    ]
                ),
                chat_final(finish_reason="tool_calls"),
            ]
        )

        item = next(
            event["item"]
            for event in events
            if event["type"] == "response.output_item.done" and event["item"]["type"] == "function_call"
        )
        print(f"Result: {item['arguments']!r}")
        assert isinstance(item["arguments"], str)
        assert json.loads(item["arguments"]) == {"a": 1}

    @pytest.mark.asyncio
    async def test_freeform_call_becomes_a_custom_tool_call_item(self):
        """
        What it does: Verifies a call to a tool declared custom comes back as a
                      custom_tool_call item carrying a raw string.
        Purpose: The client dispatches on the item type; a freeform tool answered as
                 a function_call is dropped on the floor.
        """

        async def source():
            yield chat_chunk(
                tool_calls=[
                    {
                        "index": 0,
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "exec", "arguments": '{"input":"console.log(1)"}'},
                    }
                ]
            )
            yield chat_final(finish_reason="tool_calls")

        events = []
        async for raw in translate_chat_stream_to_responses(
            source(), model="gpt-5.6-luna", freeform_tools=frozenset({"exec"})
        ):
            for line in raw.splitlines():
                if line.startswith("data:"):
                    events.append(json.loads(line[len("data:") :].strip()))

        item = next(
            event["item"]
            for event in events
            if event["type"] == "response.output_item.done" and event["item"]["type"] == "custom_tool_call"
        )
        print(f"Result: {item}")
        assert item["name"] == "exec"
        assert item["input"] == "console.log(1)"
        assert "arguments" not in item

    @pytest.mark.asyncio
    async def test_freeform_call_streams_the_custom_input_events(self):
        """
        What it does: Verifies the argument events use the custom_tool_call_input pair.
        Purpose: That is the pair the client listens on for a freeform tool; the
                 function_call_arguments pair is ignored for it.
        """

        async def source():
            yield chat_chunk(
                tool_calls=[
                    {
                        "index": 0,
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "exec", "arguments": '{"input":"ls"}'},
                    }
                ]
            )
            yield chat_final(finish_reason="tool_calls")

        types = []
        async for raw in translate_chat_stream_to_responses(
            source(), model="gpt-5.6-luna", freeform_tools=frozenset({"exec"})
        ):
            for line in raw.splitlines():
                if line.startswith("data:"):
                    types.append(json.loads(line[len("data:") :].strip())["type"])

        print(f"Result: {types}")
        assert "response.custom_tool_call_input.delta" in types
        assert "response.function_call_arguments.delta" not in types

    @pytest.mark.asyncio
    async def test_a_tool_not_declared_freeform_stays_a_function_call(self):
        """
        What it does: Verifies the bridge only applies to declared freeform names.
        Purpose: A normal function tool must not be rewritten into a freeform item.
        """
        events = await collect(
            [
                chat_chunk(
                    tool_calls=[
                        {
                            "index": 0,
                            "id": "c1",
                            "type": "function",
                            "function": {"name": "wait", "arguments": '{"ms":10}'},
                        }
                    ]
                ),
                chat_final(finish_reason="tool_calls"),
            ]
        )

        item = next(
            event["item"]
            for event in events
            if event["type"] == "response.output_item.done" and event["item"]["type"] == "function_call"
        )
        print(f"Result: {item}")
        assert item["name"] == "wait"

    @pytest.mark.asyncio
    async def test_done_sentinel_is_not_forwarded(self):
        """
        What it does: Verifies the chat [DONE] line produces no event.
        Purpose: A Responses stream ends at response.completed.
        """
        events = await collect([chat_chunk(content="ok"), chat_final(), b"data: [DONE]\n\n"])

        types = [event["type"] for event in events]
        print(f"Result: {types}")
        assert types[-1] == "response.completed"

    @pytest.mark.asyncio
    async def test_payload_split_across_chunks_is_reassembled(self):
        """
        What it does: Verifies an event split on a transport boundary still parses.
        Purpose: Chunks arrive on read boundaries, not event boundaries.
        """
        whole = chat_chunk(content="hello").decode()
        events = await collect([whole[:20].encode(), whole[20:].encode(), chat_final()])

        deltas = [e["delta"] for e in events if e["type"] == "response.output_text.delta"]
        print(f"Result: {deltas}")
        assert deltas == ["hello"]

    @pytest.mark.asyncio
    async def test_truncated_turn_reports_incomplete(self):
        """
        What it does: Verifies finish_reason=length ends in response.incomplete.
        Purpose: Reporting a truncated turn as a clean finish is what makes a
                 client accept half an answer.
        """
        events = await collect([chat_chunk(content="partial"), chat_final(finish_reason="length")])

        print(f"Result: {events[-1]['type']}")
        assert events[-1]["type"] == "response.incomplete"
        assert events[-1]["response"]["incomplete_details"]["reason"] == "max_output_tokens"

    @pytest.mark.asyncio
    async def test_mid_stream_failure_emits_response_failed(self):
        """
        What it does: Verifies an exception mid-stream becomes response.failed.
        Purpose: Without it the stream just stops and the client waits forever.
        """

        async def exploding():
            yield chat_chunk(content="ok")
            raise RuntimeError("upstream died")

        events = []
        async for raw in translate_chat_stream_to_responses(exploding(), model="gpt-5.6-luna"):
            for line in raw.splitlines():
                if line.startswith("data:"):
                    events.append(json.loads(line[len("data:") :].strip()))

        print(f"Result: {events[-1]}")
        assert events[-1]["type"] == "response.failed"
        assert events[-1]["response"]["error"]["message"] == "upstream died"

    @pytest.mark.asyncio
    async def test_rate_limit_failure_uses_the_retryable_code(self):
        """
        What it does: Verifies a 429 maps to rate_limit_exceeded.
        Purpose: Codex reads that code and backs off instead of ending the turn.
        """

        async def rate_limited():
            raise HTTPException(status_code=429, detail="slow down")
            yield  # pragma: no cover - generator shape only

        events = []
        async for raw in translate_chat_stream_to_responses(rate_limited(), model="gpt-5.6-luna"):
            for line in raw.splitlines():
                if line.startswith("data:"):
                    events.append(json.loads(line[len("data:") :].strip()))

        print(f"Result: {events[-1]['response']['error']}")
        assert events[-1]["response"]["error"]["code"] == "rate_limit_exceeded"


# =============================================================================
# Non-streaming translation
# =============================================================================


class TestFreeformBody:
    """Recovering the verbatim body from what the model answered."""

    def test_declared_field_is_unwrapped(self):
        """
        What it does: Verifies the schema's own field is read.
        Purpose: The normal case, when the model followed the bridge's schema.
        """
        assert freeform_body('{"input":"echo hi"}') == "echo hi"

    def test_a_single_differently_named_field_is_still_unwrapped(self):
        """
        What it does: Verifies a lone field under another name is accepted.
        Purpose: Models rename the one field; the body is still unambiguous.
        """
        assert freeform_body('{"code":"echo hi"}') == "echo hi"

    def test_bare_text_is_passed_through(self):
        """
        What it does: Verifies non-JSON arguments survive untouched.
        Purpose: A model that ignored the schema still produced a usable body, and
                 an empty call is no more actionable than a malformed one.
        """
        assert freeform_body("echo hi") == "echo hi"

    def test_multi_field_object_is_passed_through_verbatim(self):
        """
        What it does: Verifies an ambiguous object is not guessed at.
        Purpose: Picking one of several fields would silently drop the rest.
        """
        raw = '{"a":"one","b":"two"}'
        assert freeform_body(raw) == raw


class TestNonStreamingTranslation:
    """`chat.completion` body -> Responses object."""

    def test_message_becomes_an_output_item(self):
        """
        What it does: Verifies the assistant text lands in output.
        Purpose: The whole point of the non-streaming body.
        """
        body = {
            "model": "gpt-5.6-luna",
            "created": 5,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
        }
        result = chat_completion_to_responses(body, "gpt-5.6-luna")

        print(f"Result: {result['output']}")
        assert result["object"] == "response"
        assert result["status"] == "completed"
        assert result["output"][0]["content"][0]["text"] == "hi"
        assert result["output_text"] == "hi"

    def test_tool_calls_become_function_call_items(self):
        """
        What it does: Verifies tool calls are carried into output.
        Purpose: A non-streaming client still has to see what to run.
        """
        body = {
            "model": "gpt-5.6-luna",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{}"}}],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
        }
        result = chat_completion_to_responses(body, "gpt-5.6-luna")

        print(f"Result: {result['output']}")
        assert result["output"][0]["type"] == "function_call"
        assert result["output"][0]["call_id"] == "c1"

    def test_freeform_call_becomes_a_custom_tool_call_item(self):
        """
        What it does: Verifies the non-streaming path applies the same bridge.
        Purpose: Both paths have to agree on the item type for a freeform tool.
        """
        body = {
            "model": "gpt-5.6-luna",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "c1",
                                "type": "function",
                                "function": {"name": "exec", "arguments": '{"input":"ls"}'},
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
        }
        result = chat_completion_to_responses(body, "gpt-5.6-luna", frozenset({"exec"}))

        print(f"Result: {result['output']}")
        assert result["output"][0]["type"] == "custom_tool_call"
        assert result["output"][0]["input"] == "ls"

    def test_length_finish_reports_incomplete(self):
        """
        What it does: Verifies a truncated turn is not reported as completed.
        Purpose: Same rule as the streaming path.
        """
        body = {
            "model": "gpt-5.6-luna",
            "choices": [{"index": 0, "message": {"content": "partial"}, "finish_reason": "length"}],
        }
        result = chat_completion_to_responses(body, "gpt-5.6-luna")

        print(f"Result: {result['status']} {result['incomplete_details']}")
        assert result["status"] == "incomplete"
        assert result["incomplete_details"]["reason"] == "max_output_tokens"


# =============================================================================
# Endpoint, end to end
# =============================================================================


class TestEndpoint:
    """The route, over HTTP, through the real chat pipeline."""

    def test_missing_auth_is_rejected(self, test_client):
        """
        What it does: Verifies the endpoint requires a key.
        Purpose: Calling chat_completions() directly bypasses its own dependency,
                 so this route's auth has to be its own.
        """
        response = test_client.post("/v1/responses", json={"model": "gpt-5.6-luna", "input": "hi"})

        print(f"Result: {response.status_code}")
        assert response.status_code == 401

    def test_invalid_auth_is_rejected(self, test_client):
        """
        What it does: Verifies a wrong key is refused.
        Purpose: Same plane rules as the rest of /v1.
        """
        response = test_client.post(
            "/v1/responses",
            headers={"Authorization": "Bearer wrong"},
            json={"model": "gpt-5.6-luna", "input": "hi"},
        )

        print(f"Result: {response.status_code}")
        assert response.status_code == 401

    def test_previous_response_id_returns_400(self, test_client, valid_proxy_api_key):
        """
        What it does: Verifies the refusal reaches the client as a 400.
        Purpose: The converter raises; the global handler shapes it.
        """
        response = test_client.post(
            "/v1/responses",
            headers={"Authorization": f"Bearer {valid_proxy_api_key}"},
            json={"model": "gpt-5.6-luna", "input": "hi", "previous_response_id": "resp_1"},
        )

        print(f"Result: {response.status_code} {response.text[:200]}")
        assert response.status_code == 400

    def test_error_body_uses_the_openai_envelope(self, test_client, valid_proxy_api_key):
        """
        What it does: Verifies the error shape on this path.
        Purpose: /v1/responses is not in the Anthropic path list, so the OpenAI
                 envelope applies without any new handler.
        """
        response = test_client.post(
            "/v1/responses",
            headers={"Authorization": f"Bearer {valid_proxy_api_key}"},
            json={"model": "gpt-5.6-luna", "input": "hi", "previous_response_id": "resp_1"},
        )

        body = response.json()
        print(f"Result: {body}")
        assert "error" in body
        assert body["error"]["type"] == "invalid_request_error"

    @patch("kiro.routes_openai.KiroHttpClient")
    def test_streaming_turn_reaches_response_completed(self, mock_client_class, test_client, valid_proxy_api_key):
        """
        What it does: Verifies a streamed turn ends in response.completed.
        Purpose: The end-to-end proof that the facade reuses the chat pipeline.
        """
        mock_client_class.return_value = _mock_upstream()

        response = test_client.post(
            "/v1/responses",
            headers={"Authorization": f"Bearer {valid_proxy_api_key}"},
            json={"model": "gpt-5.6-luna", "input": "hi", "stream": True},
        )

        print(f"Result: {response.status_code} {response.text[:200]}")
        assert response.status_code == 200
        assert "text/event-stream" in response.headers["content-type"]
        assert "response.created" in response.text
        assert "response.completed" in response.text
        assert "data: [DONE]" not in response.text

    @pytest.mark.parametrize("stream", [False, True])
    @patch("kiro.routes_openai.chat_completions", new_callable=AsyncMock)
    def test_fatal_error_is_returned_unchanged(self, mock_chat, test_client, valid_proxy_api_key, stream):
        """
        What it does: Verifies a FATAL chat JSONResponse is not reshaped.
        Purpose: chat_completion_to_responses treats a body with no choices as
                 a completed empty response, which would hide the 400.
        """
        error_body = {
            "error": {
                "message": "Input is too long.",
                "type": "kiro_api_error",
                "code": 400,
            }
        }
        mock_chat.return_value = JSONResponse(status_code=400, content=error_body)

        response = test_client.post(
            "/v1/responses",
            headers={"Authorization": f"Bearer {valid_proxy_api_key}"},
            json={"model": "gpt-5.6-luna", "input": "hi", "stream": stream},
        )

        body = response.json()
        print(f"Result: {response.status_code} {body}")
        assert response.status_code == 400
        assert body == error_body
        assert body.get("status") != "completed"
        assert body.get("object") != "response"

    @patch("kiro.routes_openai.KiroHttpClient")
    def test_non_streaming_turn_returns_a_response_object(self, mock_client_class, test_client, valid_proxy_api_key):
        """
        What it does: Verifies the non-streaming body is a Responses object.
        Purpose: Other clients and the official SDK use this path.
        """
        mock_client_class.return_value = _mock_upstream()

        response = test_client.post(
            "/v1/responses",
            headers={"Authorization": f"Bearer {valid_proxy_api_key}"},
            json={"model": "gpt-5.6-luna", "input": "hi", "stream": False},
        )

        body = response.json()
        print(f"Result: {response.status_code} {body}")
        assert response.status_code == 200
        assert body["object"] == "response"
        assert body["status"] == "completed"
        assert body["output"][0]["type"] == "message"

    @patch("kiro.routes_openai.KiroHttpClient")
    def test_the_model_is_forwarded_untouched(self, mock_client_class, test_client, valid_proxy_api_key):
        """
        What it does: Verifies the requested model reaches the upstream payload.
        Purpose: Model names are client-controlled and this gateway never rewrites
                 them into another family.
        """
        upstream = _mock_upstream()
        mock_client_class.return_value = upstream

        test_client.post(
            "/v1/responses",
            headers={"Authorization": f"Bearer {valid_proxy_api_key}"},
            json={"model": "gpt-5.6-sol", "input": "hi", "stream": False},
        )

        payload = upstream.request_with_retry.call_args[0][2]
        model_id = payload["conversationState"]["currentMessage"]["userInputMessage"]["modelId"]
        print(f"Result: {model_id}")
        assert model_id == "gpt-5.6-sol"

    def test_debug_middleware_covers_the_endpoint(self):
        """
        What it does: Verifies /v1/responses is in the debug-logged set.
        Purpose: Otherwise failure capture is silently off for this route only.
        """
        from kiro.debug_middleware import LOGGED_ENDPOINTS

        print(f"Result: {sorted(LOGGED_ENDPOINTS)}")
        assert "/v1/responses" in LOGGED_ENDPOINTS


def _mock_upstream() -> AsyncMock:
    """A KiroHttpClient whose stream yields one short Kiro turn."""

    async def aiter_bytes():
        for chunk in (b'{"content":"ok"}', b'{"contextUsagePercentage":1.0}', b'{"usage":1.0}'):
            yield chunk

    upstream_response = AsyncMock()
    upstream_response.status_code = 200
    upstream_response.aiter_bytes = aiter_bytes
    upstream_response.aclose = AsyncMock()
    upstream_response.raise_for_status = Mock()

    client = AsyncMock()
    client.request_with_retry = AsyncMock(return_value=upstream_response)
    client.close = AsyncMock()
    client.client = AsyncMock()
    return client
