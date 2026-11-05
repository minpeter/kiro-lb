use futures_util::StreamExt;
use kiro_lb::routes_v1::sse_body;
use kiro_lb::stream_core::StreamError;
use std::pin::Pin;
use std::sync::{Arc, Mutex};

type Chunks = Pin<Box<dyn futures_util::Stream<Item = Result<String, StreamError>> + Send>>;

async fn outcome(s: Chunks) -> (String, Option<bool>) {
    let seen = Arc::new(Mutex::new(None));
    let record = seen.clone();
    let body: Vec<_> = sse_body(s, false, move |ok| *record.lock().unwrap() = Some(ok))
        .collect()
        .await;
    let text = body
        .into_iter()
        .map(|b| String::from_utf8(b.unwrap().to_vec()).unwrap())
        .collect();
    let ok = *seen.lock().unwrap();
    (text, ok)
}

#[tokio::test]
async fn a_failed_responses_turn_is_not_an_account_success() {
    let chat: Chunks = Box::pin(futures_util::stream::iter(vec![Err(
        StreamError::UpstreamStatus(429),
    )]));
    let translated =
        kiro_lb::stream_responses::translate(chat, "m".into(), "resp_1".into(), Default::default());
    let (text, ok) = outcome(translated).await;
    assert!(text.contains("response.failed"));
    assert!(text.contains("rate_limit_exceeded"));
    assert_eq!(ok, Some(false));
}

#[tokio::test]
async fn a_completed_turn_is_an_account_success() {
    let chat: Chunks = Box::pin(futures_util::stream::iter(vec![Ok(
        "data: {\"choices\":[{\"delta\":{\"content\":\"hi\"},\"finish_reason\":\"stop\"}]}\n\n"
            .to_owned(),
    )]));
    let translated =
        kiro_lb::stream_responses::translate(chat, "m".into(), "resp_1".into(), Default::default());
    let (text, ok) = outcome(translated).await;
    assert!(text.contains("response.completed"));
    assert_eq!(ok, Some(true));
}
