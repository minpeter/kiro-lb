use flate2::read::GzDecoder;
use serde_json::{json, Value};
use std::io::Read;
use std::path::PathBuf;

fn corpus(kind: &str) -> Vec<Value> {
    let path = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("tools/golden/corpus")
        .join(format!("{kind}.json.gz"));
    let Ok(file) = std::fs::File::open(&path) else {
        return vec![];
    };
    let mut s = String::new();
    GzDecoder::new(file).read_to_string(&mut s).unwrap();
    serde_json::from_str(&s).unwrap()
}

fn report(kind: &str, total: usize, failures: &[String], min_rate: f64) {
    let ok = total - failures.len();
    let rate = if total == 0 {
        1.0
    } else {
        ok as f64 / total as f64
    };
    eprintln!("[golden] {kind}: {ok}/{total} match ({:.1}%)", rate * 100.0);
    for f in failures.iter().take(5) {
        eprintln!("   {f}");
    }
    assert!(
        rate >= min_rate,
        "{kind}: match rate {rate:.3} below {min_rate}"
    );
}

#[test]
fn normalize_model_name() {
    let cases = corpus("normalize_model_name");
    let mut failures = vec![];
    for c in &cases {
        let (Some(i), Some(o)) = (c["name"].as_str(), c["output"].as_str()) else {
            continue;
        };
        let got = kiro_lb::model_resolver::normalize_model_name(i);
        if got != o {
            failures.push(format!("{i:?}: want {o:?} got {got:?}"));
        }
    }
    report("normalize_model_name", cases.len(), &failures, 1.0);
}

fn drop_additional_properties(v: &mut Value) {
    match v {
        Value::Object(o) => {
            o.remove("additionalProperties");
            o.values_mut().for_each(drop_additional_properties);
        }
        Value::Array(a) => a.iter_mut().for_each(drop_additional_properties),
        _ => {}
    }
}

#[test]
fn sanitize_json_schema() {
    let cases = corpus("sanitize_json_schema");
    let mut failures = vec![];
    for c in &cases {
        let mut got = kiro_lb::convert_core::sanitize_json_schema(Some(&c["schema"]));
        drop_additional_properties(&mut got);
        if got != c["output"] {
            failures.push(format!("{} -> {} vs {}", c["schema"], c["output"], got));
        }
    }
    report("sanitize_json_schema", cases.len(), &failures, 1.0);
}

#[test]
fn claude_code_prompt_detection() {
    let cases = corpus("is_claude_code_prompt");
    let mut failures = vec![];
    for c in &cases {
        let got = kiro_lb::prompt_filter::is_claude_code_prompt(c["text"].as_str().unwrap_or(""));
        if Some(got) != c["output"].as_bool() {
            failures.push(format!("test {}", c["test"]));
        }
    }
    report("is_claude_code_prompt", cases.len(), &failures, 1.0);
}

#[test]
fn measure_payload() {
    let cases = corpus("measure_payload");
    let mut failures = vec![];
    for c in &cases {
        let (t, b) = kiro_lb::payload_guard::measure(&c["payload"]);
        let want = (
            c["output"][0].as_u64().unwrap_or(0) as usize,
            c["output"][1].as_u64().unwrap_or(0) as usize,
        );
        if (t, b) != want {
            failures.push(format!("{} want {want:?} got {:?}", c["test"], (t, b)));
        }
    }
    report("measure_payload", cases.len(), &failures, 0.95);
}

#[test]
fn count_tokens() {
    let cases = corpus("count_tokens");
    let mut failures = vec![];
    let mut total = 0;
    for c in &cases {
        let args = c["args"].as_array().cloned().unwrap_or_default();
        let Some(text) = args.first().and_then(Value::as_str) else {
            continue;
        };
        total += 1;
        let correct = args
            .get(1)
            .and_then(Value::as_bool)
            .or_else(|| c["kwargs"]["apply_claude_correction"].as_bool())
            .unwrap_or(true);
        let model = args
            .get(2)
            .and_then(Value::as_str)
            .or_else(|| c["kwargs"]["model"].as_str());
        let got = kiro_lb::tokenizer::count_tokens(text, correct, model);
        if Some(got as u64) != c["output"].as_u64() {
            failures.push(format!("{} want {} got {got}", c["test"], c["output"]));
        }
    }
    report("count_tokens", total, &failures, 0.95);
}

fn strip_identity(entry: &mut Value) {
    let Some(user) = entry.as_object_mut() else {
        return;
    };
    user.remove("origin");
    user.remove("modelId");
    let empty = user
        .get_mut("userInputMessageContext")
        .and_then(Value::as_object_mut)
        .map(|ctx| {
            ctx.remove("envState");
            ctx.is_empty()
        })
        .unwrap_or(false);
    if empty {
        user.remove("userInputMessageContext");
    }
}

fn fold_system_pair(state: &mut serde_json::Map<String, Value>) {
    let ack = kiro_lb::convert_core::SYSTEM_PROMPT_ACK;
    let Some(history) = state.get_mut("history").and_then(Value::as_array_mut) else {
        return;
    };
    let is_pair = history.len() >= 2
        && history[0].get("userInputMessage").is_some()
        && history[1].pointer("/assistantResponseMessage/content") == Some(&json!(ack));
    if !is_pair {
        return;
    }
    let system = history[0]
        .pointer("/userInputMessage/content")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_owned();
    history.drain(..2);
    let target = if history.is_empty() {
        state.remove("history");
        state
            .get_mut("currentMessage")
            .and_then(|c| c.pointer_mut("/userInputMessage/content"))
    } else {
        state
            .get_mut("history")
            .and_then(|h| h.pointer_mut("/0/userInputMessage/content"))
    };
    if let Some(content) = target {
        let rest = content.as_str().unwrap_or_default();
        *content = json!(format!("{system}\n\n{rest}"));
    }
}

fn strip_volatile(v: &mut Value) {
    if let Some(top) = v.as_object_mut() {
        top.remove("agentMode");
    }
    if let Some(state) = v
        .pointer_mut("/conversationState")
        .and_then(Value::as_object_mut)
    {
        state.remove("agentContinuationId");
        state.remove("rootConversationId");
        fold_system_pair(state);
        if let Some(current) = state
            .get_mut("currentMessage")
            .and_then(|c| c.get_mut("userInputMessage"))
        {
            strip_identity(current);
        }
        if let Some(history) = state.get_mut("history").and_then(Value::as_array_mut) {
            for entry in history {
                if let Some(user) = entry.get_mut("userInputMessage") {
                    strip_identity(user);
                }
            }
        }
    }
}

#[test]
fn anthropic_to_kiro() {
    let cases = corpus("anthropic_to_kiro");
    let mut failures = vec![];
    let mut total = 0;
    for c in &cases {
        let Some(want) = c.get("output").and_then(|o| o.get("payload")) else {
            continue;
        };
        total += 1;
        let conv = c["conversation_id"].as_str().unwrap_or("");
        let arn = c["profile_arn"].as_str().unwrap_or("");
        match kiro_lb::convert_anthropic::anthropic_to_kiro(&c["request"], conv, arn) {
            Ok(r) => {
                let (mut got, mut want) = (r.payload, want.clone());
                strip_volatile(&mut got);
                strip_volatile(&mut want);
                drop_additional_properties(&mut got);
                if got != want {
                    failures.push(format!(
                        "{}\n      want {}\n      got  {}",
                        c["test"], want, got
                    ));
                }
            }
            Err(e) => failures.push(format!("{}: error {e}", c["test"])),
        }
    }
    report("anthropic_to_kiro", total, &failures, 0.9);
}

#[test]
fn openai_to_kiro() {
    let cases = corpus("openai_to_kiro");
    let mut failures = vec![];
    for c in &cases {
        let conv = c["conversation_id"].as_str().unwrap_or("");
        let arn = c["profile_arn"].as_str().unwrap_or("");
        let got = kiro_lb::convert_openai::openai_to_kiro(&c["request"], conv, arn);
        match (got, c.get("output")) {
            (Ok(r), Some(want)) => {
                let (mut got, mut want) = (r.payload, want.clone());
                strip_volatile(&mut got);
                strip_volatile(&mut want);
                drop_additional_properties(&mut got);
                if got != want {
                    failures.push(format!(
                        "{}\n      want {}\n      got  {}",
                        c["test"], want, got
                    ));
                }
            }
            (Err(_), None) => {}
            (Ok(_), None) => failures.push(format!(
                "{}: python raised {}, rust succeeded",
                c["test"], c["error"]
            )),
            (Err(e), Some(_)) => failures.push(format!("{}: rust error {e}", c["test"])),
        }
    }
    report("openai_to_kiro", cases.len(), &failures, 0.9);
}

fn scrub_ids(v: &mut Value) {
    match v {
        Value::String(s) if s.starts_with("call_") && s.len() > 20 => *s = "call_*".into(),
        Value::Array(a) => a.iter_mut().for_each(scrub_ids),
        Value::Object(o) => o.values_mut().for_each(scrub_ids),
        _ => {}
    }
}

fn prune_nulls(v: &mut Value) {
    match v {
        Value::Array(a) => a.iter_mut().for_each(prune_nulls),
        Value::Object(o) => {
            o.retain(|_, x| !x.is_null());
            o.values_mut().for_each(prune_nulls);
        }
        _ => {}
    }
}

#[test]
fn responses_to_chat() {
    let cases = corpus("responses_to_chat");
    let mut failures = vec![];
    for c in &cases {
        let got = kiro_lb::convert_responses::responses_request_to_chat(&c["request"]);
        match (got, c.get("output")) {
            (Ok(mut got), Some(want)) => {
                let mut want = want.clone();
                for v in [&mut got, &mut want] {
                    scrub_ids(v);
                    prune_nulls(v);
                    if let Some(o) = v.as_object_mut() {
                        o.retain(|k, _| {
                            [
                                "model",
                                "messages",
                                "tools",
                                "tool_choice",
                                "max_tokens",
                                "reasoning_effort",
                                "temperature",
                                "top_p",
                                "parallel_tool_calls",
                            ]
                            .contains(&k.as_str())
                        });
                    }
                }
                if got != want {
                    failures.push(format!(
                        "{}\n      want {}\n      got  {}",
                        c["test"], want, got
                    ));
                }
            }
            (Err(_), None) => {}
            (Ok(_), None) => failures.push(format!("{}: python raised", c["test"])),
            (Err(e), Some(_)) => failures.push(format!("{}: rust error {e}", c["test"])),
        }
    }
    report("responses_to_chat", cases.len(), &failures, 0.9);
}

fn scrub_parser(v: &mut Value) {
    match v {
        Value::Object(o) => {
            if let Some(Value::Object(pe)) = o.get_mut("_parse_error") {
                pe.remove("message");
            }
            if let Some(Value::String(id)) = o.get_mut("id") {
                if id.starts_with("call_") && id.len() == 13 {
                    *id = "call_*".into();
                }
            }
            o.values_mut().for_each(scrub_parser);
        }
        Value::Array(a) => a.iter_mut().for_each(scrub_parser),
        _ => {}
    }
}

#[test]
fn parser_feed() {
    let cases = corpus("parser_feed");
    let mut failures = vec![];
    for c in &cases {
        let mut p = kiro_lb::parser::AwsEventStreamParser::new();
        let mut ok = true;
        for (i, step) in c["steps"].as_array().unwrap().iter().enumerate() {
            let chunk = hex::decode(step["chunk"].as_str().unwrap()).unwrap();
            let mut got = Value::Array(p.feed(&chunk).iter().map(|e| e.to_json()).collect());
            let mut want = step["events"].clone();
            scrub_parser(&mut got);
            scrub_parser(&mut want);
            if got != want {
                failures.push(format!(
                    "{} step {i}\n      want {}\n      got  {}",
                    c["test"], want, got
                ));
                ok = false;
                break;
            }
        }
        let _ = ok;
    }
    report("parser_feed", cases.len(), &failures, 0.95);
}

#[test]
fn dedupe_and_brackets() {
    let cases = corpus("deduplicate_tool_calls");
    let mut failures = vec![];
    for c in &cases {
        let calls = c["calls"].as_array().cloned().unwrap_or_default();
        let got = Value::Array(kiro_lb::parser::deduplicate_tool_calls(&calls));
        if got != c["output"] {
            failures.push(format!("{}", c["test"]));
        }
    }
    report("deduplicate_tool_calls", cases.len(), &failures, 1.0);
    let cases = corpus("parse_bracket_tool_calls");
    let mut failures = vec![];
    for c in &cases {
        let mut got = Value::Array(kiro_lb::parser::parse_bracket_tool_calls(
            c["text"].as_str().unwrap_or(""),
        ));
        let mut want = c["output"].clone();
        scrub_parser(&mut got);
        scrub_parser(&mut want);
        if got != want {
            failures.push(format!(
                "{}\n      want {}\n      got  {}",
                c["test"], want, got
            ));
        }
    }
    report("parse_bracket_tool_calls", cases.len(), &failures, 1.0);
}
