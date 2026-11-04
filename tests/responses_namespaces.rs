use futures_util::StreamExt;
use kiro_lb::convert_responses::{
    chat_completion_to_responses, responses_request_to_chat_with_tools, ToolMap,
};
use serde_json::{json, Value};
use std::collections::HashSet;

fn function(name: &str) -> Value {
    json!({"type": "function", "name": name, "description": "Return a test value", "parameters": {"type": "object", "properties": {}, "additionalProperties": false}})
}

fn namespace(name: &str, tools: Vec<Value>) -> Value {
    json!({"type": "namespace", "name": name, "description": "group", "tools": tools})
}

fn request(tools: Vec<Value>, input: Value) -> Value {
    json!({"model": "m", "input": input, "tools": tools, "store": false})
}

fn chat_names(chat: &Value) -> Vec<String> {
    chat["tools"]
        .as_array()
        .unwrap()
        .iter()
        .map(|t| t["function"]["name"].as_str().unwrap().to_owned())
        .collect()
}

fn issue_request() -> Value {
    request(
        vec![
            namespace("group_a", vec![function("lookup")]),
            namespace("group_b", vec![function("lookup")]),
        ],
        json!("Reply with OK only. Do not use tools."),
    )
}

fn call(name: &str, arguments: &str) -> Value {
    json!({"id": "call_1", "type": "function", "function": {"name": name, "arguments": arguments}})
}

#[test]
fn the_issue_request_reaches_kiro_with_distinct_tool_names() {
    let (chat, _) = responses_request_to_chat_with_tools(&issue_request()).unwrap();
    let names = chat_names(&chat);
    assert_eq!(names, ["group_a__lookup", "group_b__lookup"]);
    let kiro = kiro_lb::convert_openai::openai_to_kiro(&chat, "cid", "").unwrap();
    let specs: Vec<&str> = kiro
        .payload
        .pointer("/conversationState/currentMessage/userInputMessage/userInputMessageContext/tools")
        .unwrap()
        .as_array()
        .unwrap()
        .iter()
        .map(|t| t["toolSpecification"]["name"].as_str().unwrap())
        .collect();
    assert_eq!(specs.iter().collect::<HashSet<_>>().len(), 2, "{specs:?}");
}

#[test]
fn tools_that_do_not_collide_keep_their_own_names() {
    let req = request(
        vec![
            function("read_file"),
            namespace("github", vec![function("get_issue")]),
            json!({"type": "custom", "name": "apply_patch", "description": "patch"}),
        ],
        json!("hi"),
    );
    let (chat, _) = responses_request_to_chat_with_tools(&req).unwrap();
    assert_eq!(chat_names(&chat), ["read_file", "get_issue", "apply_patch"]);
}

#[test]
fn a_top_level_tool_keeps_the_bare_name_and_aliases_avoid_taken_names() {
    let req = request(
        vec![
            function("lookup"),
            function("group_a__lookup"),
            namespace("group_a", vec![function("lookup")]),
        ],
        json!("hi"),
    );
    let (chat, tools) = responses_request_to_chat_with_tools(&req).unwrap();
    let names = chat_names(&chat);
    assert_eq!(names[0], "lookup");
    assert_eq!(names[1], "group_a__lookup");
    assert!(
        names[2].starts_with("group_a__lookup_") && names[2].len() <= 64,
        "{}",
        names[2]
    );
    assert_eq!(names.iter().collect::<HashSet<_>>().len(), 3);
    let origin = tools.origin(&names[2]);
    assert_eq!(origin.name, "lookup");
    assert_eq!(origin.namespace.as_deref(), Some("group_a"));
    assert_eq!(tools.origin("lookup").namespace, None);
}

#[test]
fn aliases_stay_within_64_characters_and_are_deterministic() {
    let long_ns = "an_mcp_server_with_a_really_long_descriptive_namespace_name_xyz";
    let long_tool = "fetch_every_record_from_the_remote_inventory_service_v2";
    let build = || {
        let req = request(
            vec![
                namespace(long_ns, vec![function(long_tool)]),
                namespace(&format!("{long_ns}2"), vec![function(long_tool)]),
                namespace("dotted.ns-1", vec![function("lookup")]),
                namespace("other ns", vec![function("lookup")]),
            ],
            json!("hi"),
        );
        chat_names(&responses_request_to_chat_with_tools(&req).unwrap().0)
    };
    let names = build();
    assert_eq!(names, build(), "the same request maps the same way");
    assert_eq!(names.iter().collect::<HashSet<_>>().len(), 4);
    for name in &names {
        assert!(name.len() <= 64, "{name}");
        assert!(
            name.chars()
                .all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '-'),
            "{name}"
        );
    }
    assert_eq!(names[2], "dotted_ns-1__lookup");
    assert_eq!(names[3], "other_ns__lookup");
}

#[test]
fn identical_declarations_are_sent_once_and_conflicting_ones_refused() {
    let req = request(
        vec![
            namespace("group_a", vec![function("lookup")]),
            namespace("group_a", vec![function("lookup")]),
            function("lookup"),
            function("lookup"),
        ],
        json!("hi"),
    );
    let (chat, _) = responses_request_to_chat_with_tools(&req).unwrap();
    assert_eq!(chat_names(&chat), ["group_a__lookup", "lookup"]);

    let mut different = function("lookup");
    different["description"] = json!("Something else");
    for tools in [
        vec![
            namespace("group_a", vec![function("lookup")]),
            namespace("group_a", vec![different.clone()]),
        ],
        vec![function("lookup"), different.clone()],
        vec![
            function("apply_patch"),
            json!({"type": "custom", "name": "apply_patch"}),
        ],
    ] {
        let err = responses_request_to_chat_with_tools(&request(tools, json!("hi"))).unwrap_err();
        assert!(err.contains("declared twice"), "{err}");
    }
}

#[test]
fn inline_additional_tools_share_the_same_mapping() {
    let req = request(
        vec![namespace("group_a", vec![function("lookup")])],
        json!([
            {"type": "additional_tools", "tools": [namespace("group_b", vec![function("lookup")])]},
            {"role": "user", "content": "hi"}
        ]),
    );
    let (chat, _) = responses_request_to_chat_with_tools(&req).unwrap();
    assert_eq!(chat_names(&chat), ["group_a__lookup", "group_b__lookup"]);
    assert_eq!(chat["messages"].as_array().unwrap().len(), 1);
}

#[test]
fn history_calls_are_replayed_under_the_same_names() {
    let req = request(
        vec![
            namespace("group_a", vec![function("lookup")]),
            namespace(
                "group_b",
                vec![
                    function("lookup"),
                    json!({"type": "custom", "name": "apply_patch"}),
                ],
            ),
        ],
        json!([
            {"role": "user", "content": "hi"},
            {"type": "function_call", "name": "lookup", "namespace": "group_b", "arguments": "{}", "call_id": "c1"},
            {"type": "function_call_output", "call_id": "c1", "output": "ok"},
            {"type": "custom_tool_call", "name": "apply_patch", "namespace": "group_b", "input": "*** Begin Patch", "call_id": "c2"},
            {"type": "custom_tool_call_output", "call_id": "c2", "output": "done"},
            {"type": "function_call", "name": "lookup", "arguments": "{}", "call_id": "c3"},
            {"type": "function_call_output", "call_id": "c3", "output": "ok"},
            {"type": "function_call", "name": "lookup", "namespace": "gone", "arguments": "{}", "call_id": "c4"},
            {"type": "function_call_output", "call_id": "c4", "output": "ok"},
            {"type": "function_call", "name": "unrelated", "namespace": "gone", "arguments": "{}", "call_id": "c5"},
            {"type": "function_call_output", "call_id": "c5", "output": "ok"}
        ]),
    );
    let (chat, _) = responses_request_to_chat_with_tools(&req).unwrap();
    let replayed: Vec<(String, String)> = chat["messages"]
        .as_array()
        .unwrap()
        .iter()
        .filter_map(|m| m["tool_calls"].get(0))
        .map(|c| {
            (
                c["function"]["name"].as_str().unwrap().to_owned(),
                c["function"]["arguments"].as_str().unwrap().to_owned(),
            )
        })
        .collect();
    assert_eq!(replayed[0].0, "group_b__lookup");
    assert_eq!(replayed[1].0, "apply_patch");
    assert_eq!(replayed[1].1, r#"{"input": "*** Begin Patch"}"#);
    assert_eq!(
        replayed[2].0, "lookup",
        "an ambiguous call without a namespace keeps its local name"
    );
    assert_eq!(
        replayed[3].0, "gone__lookup",
        "a removed namespace never borrows a live tool's name"
    );
    assert_eq!(replayed[4].0, "unrelated");
}

#[test]
fn tool_choice_follows_the_mapping_and_refuses_ambiguity() {
    let mut req = issue_request();
    req["tool_choice"] = json!({"type": "function", "name": "lookup", "namespace": "group_b"});
    let (chat, _) = responses_request_to_chat_with_tools(&req).unwrap();
    assert_eq!(
        chat["tool_choice"],
        json!({"type": "function", "function": {"name": "group_b__lookup"}})
    );

    req["tool_choice"] = json!({"type": "function", "name": "lookup"});
    let err = responses_request_to_chat_with_tools(&req).unwrap_err();
    assert!(err.contains("namespace"), "{err}");

    let custom = request(
        vec![namespace(
            "tools",
            vec![json!({"type": "custom", "name": "apply_patch"})],
        )],
        json!("hi"),
    );
    let mut custom = custom;
    custom["tool_choice"] = json!({"type": "custom", "name": "apply_patch"});
    let (chat, _) = responses_request_to_chat_with_tools(&custom).unwrap();
    assert_eq!(
        chat["tool_choice"],
        json!({"type": "function", "function": {"name": "apply_patch"}})
    );
    custom["tool_choice"] = json!("required");
    let (chat, _) = responses_request_to_chat_with_tools(&custom).unwrap();
    assert_eq!(chat["tool_choice"], "required");
}

fn mapped_tools() -> ToolMap {
    let req = request(
        vec![
            namespace("group_a", vec![function("lookup")]),
            namespace(
                "group_b",
                vec![
                    function("lookup"),
                    json!({"type": "custom", "name": "apply_patch"}),
                ],
            ),
            function("plain"),
        ],
        json!("hi"),
    );
    responses_request_to_chat_with_tools(&req).unwrap().1
}

#[test]
fn non_streaming_output_restores_local_names_and_namespaces() {
    let body = json!({"choices": [{"message": {"content": null, "tool_calls": [
        call("group_b__lookup", "{\"q\":1}"),
        call("apply_patch", "{\"input\":\"*** Begin Patch\"}"),
        call("plain", "{}"),
        call("not_declared", "{}"),
    ]}, "finish_reason": "tool_calls"}]});
    let out = chat_completion_to_responses(&body, "m", &mapped_tools());
    let items = out["output"].as_array().unwrap();
    assert_eq!(items[0]["type"], "function_call");
    assert_eq!(items[0]["name"], "lookup");
    assert_eq!(items[0]["namespace"], "group_b");
    assert_eq!(items[0]["arguments"], "{\"q\":1}");
    assert_eq!(items[1]["type"], "custom_tool_call");
    assert_eq!(items[1]["name"], "apply_patch");
    assert_eq!(items[1]["namespace"], "group_b");
    assert_eq!(items[1]["input"], "*** Begin Patch");
    assert_eq!(items[2]["name"], "plain");
    assert!(items[2].get("namespace").is_none());
    assert_eq!(items[3]["name"], "not_declared");
    assert!(items[3].get("namespace").is_none());
}

#[test]
fn streaming_output_restores_local_names_and_namespaces() {
    let chunk = |delta: Value, finish: Value| {
        format!(
            "data: {}\n\n",
            json!({"choices": [{"delta": delta, "finish_reason": finish}]})
        )
    };
    let frames = vec![
        Ok(chunk(
            json!({"tool_calls": [{"index": 0, "id": "call_a", "type": "function", "function": {"name": "group_a__lookup", "arguments": "{}"}}]}),
            Value::Null,
        )),
        Ok(chunk(
            json!({"tool_calls": [{"index": 1, "id": "call_b", "type": "function", "function": {"name": "apply_patch", "arguments": "{\"input\":\"body\"}"}}]}),
            json!("tool_calls"),
        )),
        Ok("data: [DONE]\n\n".to_owned()),
    ];
    let chat: std::pin::Pin<
        Box<
            dyn futures_util::Stream<Item = Result<String, kiro_lb::stream_core::StreamError>>
                + Send,
        >,
    > = Box::pin(futures_util::stream::iter(frames));
    let out =
        kiro_lb::stream_responses::translate(chat, "m".into(), "resp_1".into(), mapped_tools());
    let events: Vec<Value> = tokio::runtime::Builder::new_current_thread()
        .build()
        .unwrap()
        .block_on(out.collect::<Vec<_>>())
        .into_iter()
        .map(Result::unwrap)
        .map(|frame| {
            let data = frame
                .lines()
                .find_map(|l| l.strip_prefix("data: "))
                .unwrap()
                .to_owned();
            serde_json::from_str(&data).unwrap()
        })
        .collect();
    let added: Vec<&Value> = events
        .iter()
        .filter(|e| e["type"] == "response.output_item.added")
        .map(|e| &e["item"])
        .collect();
    assert_eq!(added[0]["name"], "lookup");
    assert_eq!(added[0]["namespace"], "group_a");
    assert_eq!(added[1]["type"], "custom_tool_call");
    assert_eq!(added[1]["namespace"], "group_b");
    assert!(events
        .iter()
        .any(|e| e["type"] == "response.custom_tool_call_input.done" && e["input"] == "body"));
    let completed = events
        .iter()
        .find(|e| e["type"] == "response.completed")
        .unwrap();
    let output = completed["response"]["output"].as_array().unwrap();
    assert_eq!(output[0]["name"], "lookup");
    assert_eq!(output[0]["namespace"], "group_a");
    assert_eq!(output[1]["name"], "apply_patch");
    assert_eq!(output[1]["input"], "body");
}

#[test]
fn duplicate_tool_names_are_refused_locally_on_every_protocol() {
    let tool = json!({"type": "function", "function": {"name": "lookup", "parameters": {"type": "object", "properties": {}}}});
    let chat = json!({"model": "m", "messages": [{"role": "user", "content": "hi"}], "tools": [tool.clone(), tool]});
    let err = kiro_lb::convert_openai::openai_to_kiro(&chat, "cid", "")
        .err()
        .unwrap()
        .to_string();
    assert!(err.contains("unique") && err.contains("lookup"), "{err}");

    let anthropic = json!({"model": "m", "max_tokens": 8, "messages": [{"role": "user", "content": "hi"}], "tools": [
        {"name": "lookup", "input_schema": {"type": "object"}},
        {"name": "lookup", "input_schema": {"type": "object"}}
    ]});
    let err = kiro_lb::convert_anthropic::anthropic_to_kiro(&anthropic, "cid", "")
        .err()
        .unwrap()
        .to_string();
    assert!(err.contains("unique") && err.contains("lookup"), "{err}");
}
