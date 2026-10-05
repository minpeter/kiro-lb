use kiro_lb::parser::{AwsEventStreamParser, ParsedEvent};

fn thinking(events: &[ParsedEvent]) -> Vec<String> {
    events
        .iter()
        .map(|e| match e {
            ParsedEvent::Thinking { text, is_first } => format!("text:{text}:{is_first}"),
            ParsedEvent::ThinkingSignature(s) => format!("sig:{s}"),
            ParsedEvent::Content(c) => format!("content:{c}"),
            other => format!("other:{other:?}"),
        })
        .collect()
}

#[test]
fn a_signature_led_reasoning_frame_keeps_its_text() {
    let mut parser = AwsEventStreamParser::new();
    let events = parser.feed(br#"{"signature":"s1","text":"first thought"}"#);
    assert_eq!(thinking(&events), ["text:first thought:true", "sig:s1"]);
}

#[test]
fn a_text_led_reasoning_frame_keeps_its_signature() {
    let mut parser = AwsEventStreamParser::new();
    let events = parser.feed(br#"{"text":"thought","signature":"s2"}"#);
    assert_eq!(thinking(&events), ["text:thought:true", "sig:s2"]);
}

#[test]
fn separate_reasoning_frames_still_parse_as_before() {
    let mut parser = AwsEventStreamParser::new();
    let events = parser
        .feed(br#"{"text":"a"}{"text":"b"}{"signature":"s3"}{"text":"c"}{"content":"answer"}"#);
    assert_eq!(
        thinking(&events),
        [
            "text:a:true",
            "text:b:false",
            "sig:s3",
            "text:c:true",
            "content:answer",
        ]
    );
}

#[test]
fn a_reasoning_frame_split_across_chunks_keeps_its_text() {
    let mut parser = AwsEventStreamParser::new();
    let mut events = parser.feed(br#"{"signature":"s4","te"#);
    events.extend(parser.feed(br#"xt":"late thought"}"#));
    assert_eq!(thinking(&events), ["text:late thought:true", "sig:s4"]);
}
