use futures_util::StreamExt;
use kiro_lb::convert_responses::{
    chat_completion_to_responses, responses_request_to_chat_with_tools, ResponsesShape,
};
use serde_json::{json, Value};

fn title_request(stream: bool) -> Value {
    json!({
        "model": "m",
        "input": "Generate a title for this conversation.",
        "stream": stream,
        "store": false,
        "text": {"format": {
            "type": "json_schema",
            "name": "GeneratedThreadTitle",
            "strict": true,
            "schema": {
                "type": "object",
                "properties": {"title": {"type": "string"}},
                "required": ["title"],
                "additionalProperties": false
            }
        }}
    })
}

fn shape(req: &Value) -> (Value, ResponsesShape) {
    responses_request_to_chat_with_tools(req).unwrap()
}

#[test]
fn a_json_schema_text_format_becomes_the_chat_response_format() {
    let (chat, shape) = shape(&title_request(false));
    assert_eq!(
        chat["response_format"],
        json!({"type": "json_schema", "json_schema": {
            "name": "GeneratedThreadTitle",
            "strict": true,
            "schema": {
                "type": "object",
                "properties": {"title": {"type": "string"}},
                "required": ["title"],
                "additionalProperties": false
            }
        }})
    );
    assert!(shape.json_text);

    let kiro = kiro_lb::convert_openai::openai_to_kiro(&chat, "cid", "").unwrap();
    let system = kiro
        .payload
        .pointer("/conversationState/history/0/userInputMessage/content")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_owned();
    assert!(
        system.contains("GeneratedThreadTitle") && system.contains("\"title\""),
        "the schema reaches the model: {system}"
    );
}

#[test]
fn json_object_and_plain_text_formats_map_as_expected() {
    let mut req = title_request(false);
    req["text"] = json!({"format": {"type": "json_object"}});
    let (chat, shape) = self::shape(&req);
    assert_eq!(chat["response_format"], json!({"type": "json_object"}));
    assert!(shape.json_text);

    for text in [json!({"format": {"type": "text"}}), json!({}), Value::Null] {
        req["text"] = text;
        let (chat, shape) = self::shape(&req);
        assert!(chat.get("response_format").is_none());
        assert!(!shape.json_text);
    }
}

#[test]
fn non_streaming_structured_output_arrives_without_a_fence() {
    let (_, shape) = shape(&title_request(false));
    let body = json!({"choices": [{"message": {"content": "{\"title\":\"Fix login\"}"}, "finish_reason": "stop"}]});
    let out = chat_completion_to_responses(&body, "m", &shape);
    assert_eq!(out["output_text"], "{\"title\":\"Fix login\"}");
    let parsed: Value = serde_json::from_str(out["output_text"].as_str().unwrap()).unwrap();
    assert_eq!(parsed["title"], "Fix login");
}

fn stream_events(shape: ResponsesShape, pieces: &[&str]) -> Vec<Value> {
    let mut frames: Vec<Result<String, kiro_lb::stream_core::StreamError>> = vec![Ok(format!(
        "data: {}\n\n",
        json!({"choices": [{"delta": {"reasoning": "thinking"}, "finish_reason": null}]})
    ))];
    for piece in pieces {
        frames.push(Ok(format!(
            "data: {}\n\n",
            json!({"choices": [{"delta": {"content": piece}, "finish_reason": null}]})
        )));
    }
    frames.push(Ok(format!(
        "data: {}\n\n",
        json!({"choices": [{"delta": {}, "finish_reason": "stop"}]})
    )));
    let chat: std::pin::Pin<
        Box<
            dyn futures_util::Stream<Item = Result<String, kiro_lb::stream_core::StreamError>>
                + Send,
        >,
    > = Box::pin(futures_util::stream::iter(frames));
    let out = kiro_lb::stream_responses::translate(chat, "m".into(), "resp_1".into(), shape);
    tokio::runtime::Builder::new_current_thread()
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
        .collect()
}

#[test]
fn streamed_structured_output_is_one_bare_json_value() {
    let (_, shape) = shape(&title_request(true));
    let events = stream_events(shape, &["```json\n{\"title\":", " \"Fix login\"}\n```"]);
    let deltas: Vec<&str> = events
        .iter()
        .filter(|e| e["type"] == "response.output_text.delta")
        .map(|e| e["delta"].as_str().unwrap())
        .collect();
    assert_eq!(deltas, ["{\"title\": \"Fix login\"}"]);
    let done = events
        .iter()
        .find(|e| e["type"] == "response.output_text.done")
        .unwrap();
    assert_eq!(done["text"], "{\"title\": \"Fix login\"}");
    let completed = events
        .iter()
        .find(|e| e["type"] == "response.completed")
        .unwrap();
    let output = completed["response"]["output"].as_array().unwrap();
    assert_eq!(output[0]["type"], "reasoning");
    assert_eq!(output[1]["type"], "message");
    assert_eq!(
        output[1]["content"][0]["text"],
        "{\"title\": \"Fix login\"}"
    );
    let indexes: Vec<i64> = events
        .iter()
        .filter(|e| e["type"] == "response.output_item.added")
        .map(|e| e["output_index"].as_i64().unwrap())
        .collect();
    assert_eq!(indexes, [0, 1], "each output item keeps its own index");
    let sequence: Vec<i64> = events
        .iter()
        .map(|e| e["sequence_number"].as_i64().unwrap())
        .collect();
    assert!(sequence.windows(2).all(|w| w[1] == w[0] + 1));
}

#[test]
fn plain_text_still_streams_as_it_arrives() {
    let events = stream_events(ResponsesShape::default(), &["Hello", " world"]);
    let deltas: Vec<&str> = events
        .iter()
        .filter(|e| e["type"] == "response.output_text.delta")
        .map(|e| e["delta"].as_str().unwrap())
        .collect();
    assert_eq!(deltas, ["Hello", " world"]);
}
