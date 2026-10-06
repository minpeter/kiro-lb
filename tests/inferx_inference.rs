mod common;

use axum::{
    body::{to_bytes, Body},
    http::{Request, StatusCode},
    routing::get,
    Router,
};
use kiro_lb::{routes_inferx as api, store};
use serde_json::{json, Value};
use tower::ServiceExt;

static TEST_LOCK: tokio::sync::Mutex<()> = tokio::sync::Mutex::const_new(());

const PNG: &str =
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a5N8AAAAASUVORK5CYII=";

#[tokio::test]
async fn metering_upgrade_preserves_legacy_receipts_without_inventing_provenance() {
    let _test_lock = TEST_LOCK.lock().await;
    let dir = common::data_dir("inferx-metering-migration");
    common::seed(&[]);
    std::env::set_var("INFERX_CONTROL_TOKEN", "fixture-control");
    let id = uuid::Uuid::new_v4().to_string();
    store::with(|c| {
        c.execute_batch("ALTER TABLE inferx_requests DROP COLUMN metering_json")?;
        c.execute("INSERT INTO inferx_requests(request_id,request_hash,owner_id,connection_id,status,input_tokens,output_tokens,duration_ms,created_at,updated_at) VALUES(?1,'hash','seller','connection','succeeded',17,9,50,0,0)", [&id])?;
        Ok(())
    }).unwrap();
    store::initialize().unwrap();
    store::initialize().unwrap();
    let app = Router::new().route("/requests/{id}", get(api::get_request));
    let receipt = call(&app, &id, "seller", "GET", None).await.1;
    assert_eq!(receipt["usage"]["inputTokens"], 17);
    assert_eq!(receipt["usage"]["outputTokens"], 9);
    assert!(receipt["usage"].get("metering").is_none());
    // Corrupt persisted usage must not become a valid zero-cost success.
    store::with(|c| c.execute("UPDATE inferx_requests SET input_tokens=-1,output_tokens=NULL,metering_json='broken' WHERE request_id=?1", [&id]).map(|_|())).unwrap();
    let corrupt = call(&app, &id, "seller", "GET", None).await.1;
    assert_eq!(corrupt["usage"]["inputTokens"], -1);
    assert!(corrupt["usage"]["outputTokens"].is_null());
    assert!(corrupt["usage"]["metering"].is_null());
    std::fs::remove_dir_all(dir).unwrap();
}

#[tokio::test]
async fn images_and_tools_reach_upstream_and_survive_completion_stream_and_recovery() {
    let _test_lock = TEST_LOCK.lock().await;
    let dir = common::data_dir("inferx-modalities");
    common::seed(&[]);
    let previous_endpoints = kiro_lb::settings::endpoint_settings().as_json();
    let mut endpoints = previous_endpoints.clone();
    endpoints["rotation"] = json!(false);
    store::save_setting("endpoints", &endpoints).unwrap();
    kiro_lb::settings::load_endpoint_settings();
    std::env::set_var("INFERX_CONTROL_TOKEN", "fixture-control");
    let payloads = std::sync::Arc::new(tokio::sync::Mutex::new(Vec::<Value>::new()));
    let captured = payloads.clone();
    let runtime = Router::new().route("/", axum::routing::post(move |bytes: axum::body::Bytes| {
        let captured = captured.clone();
        async move {
            let body: Value = serde_json::from_slice(&bytes).unwrap();
            let continuation = body.pointer("/conversationState/currentMessage/userInputMessage/userInputMessageContext/toolResults").is_some();
            captured.lock().await.push(body);
            if continuation {
                "{\"content\":\"Seoul is sunny\"}{\"usage\":1}{\"stopReason\":\"end_turn\"}"
            } else {
                r#"{"name":"weather","toolUseId":"call-1","input":"{\"city\":"}{"input":"\"Seoul\"}","toolUseId":"call-1","stop":true}{"usage":1}{"stopReason":"tool_use"}"#
            }
        }
    }));
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    std::env::set_var(
        "KIRO_TEST_RUNTIME_URL",
        format!("http://{}/", listener.local_addr().unwrap()),
    );
    let runtime_task = tokio::spawn(async move {
        axum::serve(listener, runtime).await.unwrap();
    });
    let http = reqwest::Client::new();
    let state = common::state(common::pool(&http, &[]), &http, false);
    let app = Router::new()
        .route(
            "/requests/{id}",
            get(api::get_request).post(api::post_request),
        )
        .with_state(state);
    let connection = uuid::Uuid::new_v4().to_string();
    let credential = json!({"accessToken":"fixture-access","refreshToken":"fixture-refresh","expiresAt":"2999-01-01T00:00:00Z","region":"us-east-1","profileArn":"arn:aws:codewhisperer:us-east-1:000000000000:profile/test"});
    store::with(|c| c.execute("INSERT INTO inferx_connections(id,owner_id,provider,status,credential_json,created_at,updated_at) VALUES(?1,'seller','github','registered',?2,0,0)", rusqlite::params![connection,credential.to_string()]).map(|_|())).unwrap();
    let tool_call = json!({"id":"call-1","type":"function","function":{"name":"weather","arguments":"{\"city\":\"Seoul\"}"}});
    let initial = json!({"ownerId":"seller","connectionId":connection,"request":{
        "model":"claude-sonnet-4","stream":false,"max_tokens":256,
        "messages":[{"role":"user","content":[{"type":"text","text":"Describe weather"},{"type":"image_url","image_url":{"url":format!("data:image/png;base64,{PNG}")}}]}],
        "tools":[{"type":"function","function":{"name":"weather","description":"Weather by city","parameters":{"type":"object","properties":{"city":{"type":"string"}}}}}]
    }});
    for streaming in [false, true] {
        let mut body = initial.clone();
        body["request"]["stream"] = json!(streaming);
        let id = uuid::Uuid::new_v4().to_string();
        let response = app
            .clone()
            .oneshot(
                Request::post(format!("/requests/{id}"))
                    .header("Authorization", "Bearer fixture-control")
                    .body(Body::from(body.to_string()))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::OK);
        let bytes = to_bytes(response.into_body(), 2_000_000).await.unwrap();
        if streaming {
            let stream = std::str::from_utf8(&bytes).unwrap();
            let chunks: Vec<Value> = stream
                .split("\n\n")
                .filter_map(|frame| frame.strip_prefix("data: "))
                .map(|s| serde_json::from_str(s).unwrap())
                .collect();
            let call = chunks
                .iter()
                .find_map(|v| v.pointer("/choices/0/delta/tool_calls/0"))
                .unwrap();
            assert_eq!(call["function"]["name"], "weather");
            assert_eq!(
                serde_json::from_str::<Value>(call["function"]["arguments"].as_str().unwrap())
                    .unwrap(),
                json!({"city":"Seoul"})
            );
            assert!(stream.contains("\"finish_reason\":\"tool_calls\""));
            assert!(stream.contains("event: inferx.receipt"));
        } else {
            let value: Value = serde_json::from_slice(&bytes).unwrap();
            assert_eq!(value["status"], "succeeded");
            assert_eq!(
                value["response"]["choices"][0]["message"]["tool_calls"][0]["id"],
                "call-1"
            );
        }
        let receipt = call(&app, &id, "seller", "GET", None).await.1;
        assert_eq!(
            receipt["usage"]["metering"],
            kiro_lb::inferx_contract::metering()
        );
        assert!(
            receipt["usage"]["outputTokens"].as_i64().unwrap() > 0,
            "tool-only outputs must be metered"
        );
        assert_eq!(
            call(&app, &id, "seller", "POST", Some(body.clone()))
                .await
                .1,
            receipt
        );
        store::initialize().unwrap();
        assert_eq!(call(&app, &id, "seller", "GET", None).await.1, receipt);
        let input = body["request"]["messages"].as_array_mut().unwrap();
        input.push(json!({"role":"assistant","content":null,"tool_calls":[tool_call.clone()]}));
        input.push(json!({"role":"tool","tool_call_id":"call-1","content":"Sunny"}));
        body["request"]["stream"] = json!(false);
        let continued = call(
            &app,
            &uuid::Uuid::new_v4().to_string(),
            "seller",
            "POST",
            Some(body),
        )
        .await
        .1;
        assert_eq!(continued["status"], "succeeded");
        assert_eq!(
            continued["response"]["choices"][0]["message"]["content"],
            "Seoul is sunny"
        );
    }
    let payloads = payloads.lock().await;
    assert_eq!(
        payloads.len(),
        4,
        "replaying a request must not execute again"
    );
    for payload in payloads.iter().step_by(2) {
        let current = &payload["conversationState"]["currentMessage"]["userInputMessage"];
        assert_eq!(current["images"][0]["source"]["bytes"], PNG);
        assert_eq!(current["images"][0]["format"], "png");
        assert_eq!(
            current["userInputMessageContext"]["tools"][0]["toolSpecification"]["name"],
            "weather"
        );
    }
    for payload in payloads.iter().skip(1).step_by(2) {
        let state = &payload["conversationState"];
        assert_eq!(
            state["currentMessage"]["userInputMessage"]["userInputMessageContext"]["toolResults"]
                [0]["toolUseId"],
            "call-1"
        );
        assert!(state["history"].as_array().unwrap().iter().any(|v| v
            .pointer("/assistantResponseMessage/toolUses/0/toolUseId")
            == Some(&json!("call-1"))));
    }
    drop(payloads);
    // max_tokens is enforced before output leaves the companion. A complete
    // tool call is atomic, so one that does not fit is omitted and the bounded
    // turn succeeds with truthful usage instead of becoming free output.
    for streaming in [false, true] {
        let mut body = initial.clone();
        body["request"]["stream"] = json!(streaming);
        body["request"]["max_tokens"] = json!(1);
        let id = uuid::Uuid::new_v4().to_string();
        let response = app
            .clone()
            .oneshot(
                Request::post(format!("/requests/{id}"))
                    .header("Authorization", "Bearer fixture-control")
                    .body(Body::from(body.to_string()))
                    .unwrap(),
            )
            .await
            .unwrap();
        let bytes = to_bytes(response.into_body(), 2_000_000).await.unwrap();
        let text = std::str::from_utf8(&bytes).unwrap();
        assert!(text.contains("\"status\":\"succeeded\""));
        assert!(!text.contains("call-1"), "over-budget tool leaked: {text}");
        assert!(text.contains("\"finish_reason\":\"length\""));
        let receipt = call(&app, &id, "seller", "GET", None).await.1;
        assert_eq!(receipt["status"], "succeeded");
        assert!(receipt["usage"]["outputTokens"].as_i64().unwrap() <= 1);
    }
    std::env::remove_var("KIRO_TEST_RUNTIME_URL");
    store::save_setting("endpoints", &previous_endpoints).unwrap();
    kiro_lb::settings::load_endpoint_settings();
    runtime_task.abort();
    std::fs::remove_dir_all(dir).unwrap();
}

async fn successful_upstream() -> (reqwest::Client, String) {
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::Arc;
    use tokio::io::{AsyncReadExt, AsyncWriteExt};

    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = listener.local_addr().unwrap().port();
    let calls = Arc::new(AtomicUsize::new(0));
    tokio::spawn(async move {
        loop {
            let (mut socket, _) = listener.accept().await.unwrap();
            let calls = calls.clone();
            tokio::spawn(async move {
                let mut request = Vec::new();
                let mut buffer = [0; 1024];
                while !request.windows(4).any(|part| part == b"\r\n\r\n") {
                    let count = socket.read(&mut buffer).await.unwrap();
                    if count == 0 {
                        return;
                    }
                    request.extend_from_slice(&buffer[..count]);
                }
                let header_end = request
                    .windows(4)
                    .position(|part| part == b"\r\n\r\n")
                    .unwrap()
                    + 4;
                let headers = String::from_utf8_lossy(&request[..header_end]);
                let content_length = headers
                    .lines()
                    .find_map(|line| {
                        line.to_ascii_lowercase()
                            .strip_prefix("content-length:")
                            .and_then(|value| value.trim().parse::<usize>().ok())
                    })
                    .unwrap_or(0);
                while request.len() - header_end < content_length {
                    let count = socket.read(&mut buffer).await.unwrap();
                    if count == 0 {
                        return;
                    }
                    request.extend_from_slice(&buffer[..count]);
                }
                let pressure = calls.fetch_add(1, Ordering::SeqCst) > 0;
                socket.write_all(b"HTTP/1.1 200 OK\r\ncontent-type: application/vnd.amazon.eventstream\r\ntransfer-encoding: chunked\r\n\r\n").await.unwrap();
                let count = if pressure { 80 } else { 3 };
                for index in 0..count {
                    let event = format!(r#"{{"content":"chunk-{index} "}}"#);
                    if socket
                        .write_all(format!("{:x}\r\n{event}\r\n", event.len()).as_bytes())
                        .await
                        .is_err()
                    {
                        return;
                    }
                    if !pressure {
                        tokio::time::sleep(std::time::Duration::from_millis(15)).await;
                    }
                }
                let usage = r#"{"usage":9}{"stopReason":"end_turn"}"#;
                let _ = socket
                    .write_all(format!("{:x}\r\n{usage}\r\n0\r\n\r\n", usage.len()).as_bytes())
                    .await;
            });
        }
    });
    (reqwest::Client::new(), format!("http://127.0.0.1:{port}/"))
}

async fn call(
    app: &Router,
    id: &str,
    owner: &str,
    method: &str,
    body: Option<Value>,
) -> (StatusCode, Value) {
    let response = app
        .clone()
        .oneshot(
            Request::builder()
                .method(method)
                .uri(format!("/requests/{id}?ownerId={owner}"))
                .header("Authorization", "Bearer fixture-control")
                .body(Body::from(body.map(|v| v.to_string()).unwrap_or_default()))
                .unwrap(),
        )
        .await
        .unwrap();
    let status = response.status();
    let bytes = to_bytes(response.into_body(), 2_000_000).await.unwrap();
    (status, serde_json::from_slice(&bytes).unwrap())
}

#[tokio::test]
async fn receipts_fence_late_dispatch_preserve_owner_and_never_reexecute_after_restart() {
    let _test_lock = TEST_LOCK.lock().await;
    let dir = common::data_dir("inferx-inference");
    common::seed(&[]);
    std::env::set_var("INFERX_CONTROL_TOKEN", "fixture-control");
    let mock = common::upstream().await;
    let state = common::state(common::pool(&mock.http, &[]), &mock.http, false);
    let app = Router::new()
        .route(
            "/requests/{id}",
            get(api::get_request)
                .post(api::post_request)
                .delete(api::fence_request),
        )
        .with_state(state.clone());
    let connection = uuid::Uuid::new_v4().to_string();
    let credential = json!({"accessToken":"fixture-access","refreshToken":"fixture-refresh","expiresAt":"2999-01-01T00:00:00Z","region":"us-east-1"});
    store::with(|c| c.execute("INSERT INTO inferx_connections(id,owner_id,provider,status,credential_json,created_at,updated_at) VALUES(?1,'seller','github','registered',?2,0,0)", rusqlite::params![connection, credential.to_string()]).map(|_|())).unwrap();
    let body = json!({"ownerId":"seller","connectionId":connection,"request":{"model":"claude-sonnet-4","messages":[{"role":"user","content":"private input"}],"max_tokens":64,"stream":false}});
    let stream_id = uuid::Uuid::new_v4().to_string();
    let mut stream_body = body.clone();
    stream_body["request"]["stream"] = json!(true);
    let response = app
        .clone()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri(format!("/requests/{stream_id}"))
                .header("Authorization", "Bearer fixture-control")
                .body(Body::from(stream_body.to_string()))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    assert_eq!(response.headers()["content-type"], "text/event-stream");
    let stream = String::from_utf8(
        to_bytes(response.into_body(), 2_000_000)
            .await
            .unwrap()
            .to_vec(),
    )
    .unwrap();
    assert!(stream.contains("event: inferx.receipt\n"));
    assert!(stream.contains("\"status\":\"failed\""));
    assert!(!stream.contains("[DONE]"));

    // Dropping the HTTP response does not own or cancel execution. The
    // detached engine task drains the upstream failure and persists a receipt.
    let disconnected = uuid::Uuid::new_v4().to_string();
    let response = app
        .clone()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri(format!("/requests/{disconnected}"))
                .header("Authorization", "Bearer fixture-control")
                .body(Body::from(stream_body.to_string()))
                .unwrap(),
        )
        .await
        .unwrap();
    drop(response);
    let disconnected_receipt = loop {
        let (status, value) = call(&app, &disconnected, "seller", "GET", None).await;
        if status == StatusCode::OK && value["status"] != "running" {
            break value;
        }
        tokio::time::sleep(std::time::Duration::from_millis(10)).await;
    };
    assert_eq!(disconnected_receipt["status"], "failed");
    let delayed = uuid::Uuid::new_v4().to_string();
    assert_eq!(
        call(&app, &delayed, "seller", "DELETE", None).await.1["status"],
        "failed"
    );
    assert_eq!(
        call(&app, &delayed, "seller", "POST", Some(body.clone()))
            .await
            .0,
        StatusCode::CONFLICT
    );
    assert_eq!(
        call(&app, &delayed, "other", "GET", None).await.0,
        StatusCode::NOT_FOUND
    );
    assert_eq!(
        call(&app, &delayed, "other", "DELETE", None).await.0,
        StatusCode::NOT_FOUND
    );
    let id = uuid::Uuid::new_v4().to_string();
    let mut impostor = body.clone();
    impostor["ownerId"] = json!("other");
    assert_eq!(
        call(&app, &id, "other", "POST", Some(impostor)).await.0,
        StatusCode::NOT_FOUND
    );
    // Mock proxy rejects AWS; this exercises the real execution failure path,
    // with no live credentials or network access to AWS.
    let (status, receipt) = call(&app, &id, "seller", "POST", Some(body.clone())).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(receipt["status"], "failed");
    assert!(!receipt.to_string().contains("private input"));
    assert!(receipt.get("response").is_none());
    let refreshes = mock.refresh_calls();
    assert_eq!(
        call(&app, &id, "seller", "POST", Some(body.clone()))
            .await
            .1,
        receipt
    );
    assert_eq!(mock.refresh_calls(), refreshes);
    let mut changed = body.clone();
    changed["request"]["max_tokens"] = json!(65);
    assert_eq!(
        call(&app, &id, "seller", "POST", Some(changed)).await.0,
        StatusCode::CONFLICT
    );
    let running = uuid::Uuid::new_v4().to_string();
    store::with(|c| c.execute("INSERT INTO inferx_requests(request_id,request_hash,owner_id,connection_id,status,created_at,updated_at) SELECT ?1,request_hash,owner_id,connection_id,'running',0,0 FROM inferx_requests WHERE request_id=?2",rusqlite::params![running,id]).map(|_|())).unwrap();
    assert_eq!(
        call(&app, &running, "seller", "DELETE", None).await.1["status"],
        "running"
    );
    store::initialize().unwrap();
    assert_eq!(
        call(&app, &running, "seller", "POST", Some(body)).await.1["status"],
        "indeterminate"
    );
    assert!(state.pool.accounts().is_empty());
    assert_eq!(state.inflight.load(std::sync::atomic::Ordering::SeqCst), 0);
    std::fs::remove_dir_all(dir).unwrap();
}

#[tokio::test]
async fn native_sse_is_incremental_and_never_reports_success_after_channel_pressure() {
    let _test_lock = TEST_LOCK.lock().await;
    let dir = common::data_dir("inferx-native-sse");
    common::seed(&[]);
    let previous_endpoints = kiro_lb::settings::endpoint_settings().as_json();
    let mut endpoints = previous_endpoints.clone();
    // Keep this fixture on its local runtime instead of the live endpoint pool.
    endpoints["rotation"] = json!(false);
    store::save_setting("endpoints", &endpoints).unwrap();
    kiro_lb::settings::load_endpoint_settings();
    std::env::set_var("INFERX_CONTROL_TOKEN", "fixture-control");
    let (http, runtime_url) = successful_upstream().await;
    std::env::set_var("KIRO_TEST_RUNTIME_URL", runtime_url);
    let state = common::state(common::pool(&http, &[]), &http, false);
    let app = Router::new()
        .route(
            "/requests/{id}",
            get(api::get_request).post(api::post_request),
        )
        .with_state(state);
    let connection = uuid::Uuid::new_v4().to_string();
    let credential = json!({"accessToken":"fixture-access","refreshToken":"fixture-refresh","expiresAt":"2999-01-01T00:00:00Z","region":"us-east-1","profileArn":"arn:aws:codewhisperer:us-east-1:000000000000:profile/test"});
    store::with(|c| c.execute("INSERT INTO inferx_connections(id,owner_id,provider,status,credential_json,created_at,updated_at) VALUES(?1,'seller','github','registered',?2,0,0)", rusqlite::params![connection, credential.to_string()]).map(|_|())).unwrap();
    let request_body = json!({"ownerId":"seller","connectionId":connection,"request":{"model":"claude-sonnet-4","messages":[{"role":"user","content":"hello"}],"max_tokens":256,"stream":true}});

    let id = uuid::Uuid::new_v4().to_string();
    let response = app
        .clone()
        .oneshot(
            Request::post(format!("/requests/{id}"))
                .header("Authorization", "Bearer fixture-control")
                .body(Body::from(request_body.to_string()))
                .unwrap(),
        )
        .await
        .unwrap();
    let mut body = response.into_body().into_data_stream();
    use futures_util::StreamExt;
    let first = body.next().await.unwrap().unwrap();
    assert!(
        !String::from_utf8_lossy(&first).contains("inferx.receipt"),
        "first event: {}",
        String::from_utf8_lossy(&first)
    );
    let mut stream = String::from_utf8(first.to_vec()).unwrap();
    while let Some(chunk) = body.next().await {
        stream.push_str(std::str::from_utf8(&chunk.unwrap()).unwrap());
    }
    assert!(stream.contains("chunk-1"));
    assert!(stream.contains("event: inferx.receipt"));
    assert!(
        stream.contains("\"status\":\"succeeded\""),
        "stream: {stream}"
    );
    let receipt = call(&app, &id, "seller", "GET", None).await.1;
    assert_eq!(receipt["status"], "succeeded");
    assert!(receipt["usage"]["outputTokens"].as_i64().unwrap() > 0);

    let pressured = uuid::Uuid::new_v4().to_string();
    let response = app
        .clone()
        .oneshot(
            Request::post(format!("/requests/{pressured}"))
                .header("Authorization", "Bearer fixture-control")
                .body(Body::from(request_body.to_string()))
                .unwrap(),
        )
        .await
        .unwrap();
    // Let the detached producer fill the bounded channel before polling it.
    tokio::time::sleep(std::time::Duration::from_millis(100)).await;
    let stream = String::from_utf8(
        to_bytes(response.into_body(), 2_000_000)
            .await
            .unwrap()
            .to_vec(),
    )
    .unwrap();
    assert!(stream.contains("event: inferx.error"));
    assert!(!stream.contains("event: inferx.receipt"));
    assert_eq!(
        call(&app, &pressured, "seller", "GET", None).await.1["status"],
        "succeeded"
    );
    std::env::remove_var("KIRO_TEST_RUNTIME_URL");
    store::save_setting("endpoints", &previous_endpoints).unwrap();
    kiro_lb::settings::load_endpoint_settings();
    std::fs::remove_dir_all(dir).unwrap();
}
