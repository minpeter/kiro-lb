//! Payload size guard. The upstream CONTENT_LENGTH_EXCEEDS_THRESHOLD tracks cl100k
//! tokens of the compact JSON, not bytes; the byte cap is a legacy extra.

use serde_json::{Map, Value};
use std::collections::HashSet;

use crate::tokenizer::count_tokens;

#[derive(Debug, Clone)]
pub struct PayloadTooLarge {
    pub size: usize,
    pub limit: usize,
    pub unit: &'static str,
}

impl std::fmt::Display for PayloadTooLarge {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        let word = if self.unit == "tokens" {
            "token"
        } else {
            "byte"
        };
        write!(
            f,
            "Request payload is {} {}, over the {} {word} limit Kiro accepts. Shorten the conversation or send fewer tools. Set AUTO_TRIM_PAYLOAD=true to drop the oldest history instead (this silently loses earlier context).",
            self.size, self.unit, self.limit
        )
    }
}

pub fn compact_json(payload: &Value) -> String {
    serde_json::to_string(payload).unwrap_or_default()
}

pub fn measure_text(serialized: &str) -> (usize, usize) {
    (
        count_tokens(serialized, false, Some("claude-haiku-4.5")),
        serialized.len(),
    )
}

/// Upstream does not count image data toward CONTENT_LENGTH_EXCEEDS_THRESHOLD
/// (measured 2026-09-27: a 2.9 MB PNG passed a ~195k token text threshold and
/// contextUsage grew by the vision rate only). Image bytes are blanked for the
/// measurement and each image adds its vision estimate instead.
pub fn measure(payload: &Value) -> (usize, usize) {
    let mut image_tokens = 0;
    let mut text = without_image_data(payload, &mut image_tokens);
    // Thinking signatures are opaque attestations, not model context. Only
    // blank the protocol field; tool arguments named `signature` still count.
    if let Some(history) = text
        .pointer_mut("/conversationState/history")
        .and_then(Value::as_array_mut)
    {
        for entry in history {
            if let Some(signature @ Value::String(_)) = entry
                .pointer_mut("/assistantResponseMessage/reasoningContent/reasoningText/signature")
            {
                *signature = Value::String(String::new());
            }
        }
    }
    let (tokens, bytes) = measure_text(&compact_json(&text));
    (tokens + image_tokens, bytes)
}

fn without_image_data(v: &Value, image_tokens: &mut usize) -> Value {
    match v {
        Value::Object(o) => Value::Object(
            o.iter()
                .map(|(k, x)| {
                    let x = match (k.as_str(), x) {
                        ("images", Value::Array(items)) => Value::Array(
                            items.iter().map(|i| blank_image(i, image_tokens)).collect(),
                        ),
                        _ => without_image_data(x, image_tokens),
                    };
                    (k.clone(), x)
                })
                .collect(),
        ),
        Value::Array(a) => Value::Array(
            a.iter()
                .map(|x| without_image_data(x, image_tokens))
                .collect(),
        ),
        other => other.clone(),
    }
}

fn blank_image(image: &Value, image_tokens: &mut usize) -> Value {
    let mut out = image.clone();
    if let Some(bytes) = out.pointer_mut("/source/bytes") {
        *image_tokens += estimate_image_tokens(bytes.as_str().unwrap_or(""));
        *bytes = Value::String(String::new());
    }
    out
}

const IMAGE_TOKEN_FALLBACK: usize = 1600;

/// Anthropic's vision rate, ceil(w*h/750), after the ~1.15 MP resize that caps
/// one image near 1600 tokens. Unknown sizes take that ceiling.
pub fn estimate_image_tokens(base64: &str) -> usize {
    use base64::Engine;
    let prefix: String = base64.chars().take(87_384).collect();
    let prefix = &prefix[..prefix.len() - prefix.len() % 4];
    let Ok(head) = base64::engine::general_purpose::STANDARD.decode(prefix) else {
        return IMAGE_TOKEN_FALLBACK;
    };
    match image_dimensions(&head) {
        Some((w, h)) if w > 0 && h > 0 => ((w * h).div_ceil(750)).min(IMAGE_TOKEN_FALLBACK),
        _ => IMAGE_TOKEN_FALLBACK,
    }
}

fn be16(b: &[u8], i: usize) -> Option<usize> {
    Some(u16::from_be_bytes([*b.get(i)?, *b.get(i + 1)?]) as usize)
}

fn le16(b: &[u8], i: usize) -> Option<usize> {
    Some(u16::from_le_bytes([*b.get(i)?, *b.get(i + 1)?]) as usize)
}

fn image_dimensions(b: &[u8]) -> Option<(usize, usize)> {
    if b.starts_with(b"\x89PNG\r\n\x1a\n") {
        let w = u32::from_be_bytes(b.get(16..20)?.try_into().ok()?) as usize;
        let h = u32::from_be_bytes(b.get(20..24)?.try_into().ok()?) as usize;
        return Some((w, h));
    }
    if b.starts_with(b"GIF8") {
        return Some((le16(b, 6)?, le16(b, 8)?));
    }
    if b.starts_with(b"RIFF") && b.get(8..12) == Some(b"WEBP") {
        return match b.get(12..16)? {
            b"VP8 " => Some((le16(b, 26)? & 0x3fff, le16(b, 28)? & 0x3fff)),
            b"VP8L" => {
                let bits = u32::from_le_bytes(b.get(21..25)?.try_into().ok()?) as usize;
                Some(((bits & 0x3fff) + 1, ((bits >> 14) & 0x3fff) + 1))
            }
            b"VP8X" => {
                let w = u32::from_le_bytes([*b.get(24)?, *b.get(25)?, *b.get(26)?, 0]) as usize;
                let h = u32::from_le_bytes([*b.get(27)?, *b.get(28)?, *b.get(29)?, 0]) as usize;
                Some((w + 1, h + 1))
            }
            _ => None,
        };
    }
    if b.starts_with(&[0xff, 0xd8]) {
        let mut i = 2;
        while i + 9 < b.len() {
            if b[i] != 0xff {
                i += 1;
                continue;
            }
            let marker = b[i + 1];
            if matches!(marker, 0xc0..=0xcf) && !matches!(marker, 0xc4 | 0xc8 | 0xcc) {
                return Some((be16(b, i + 7)?, be16(b, i + 5)?));
            }
            if marker == 0xd8 || marker == 0x01 || (0xd0..=0xd7).contains(&marker) {
                i += 2;
                continue;
            }
            i += 2 + be16(b, i + 2)?;
        }
    }
    None
}

#[derive(Debug, Default)]
pub struct TrimStats {
    pub original_bytes: usize,
    pub final_bytes: usize,
    pub original_entries: usize,
    pub final_entries: usize,
    pub original_tokens: usize,
    pub final_tokens: usize,
}

fn history_mut(payload: &mut Value) -> Option<&mut Vec<Value>> {
    payload
        .pointer_mut("/conversationState/history")
        .and_then(Value::as_array_mut)
}

fn over_limit(payload: &Value, max_bytes: Option<usize>, max_tokens: Option<usize>) -> bool {
    let (tokens, bytes) = measure(payload);
    max_tokens.is_some_and(|t| tokens > t) || max_bytes.is_some_and(|b| bytes > b)
}

fn repair_orphans(history: &mut [Value], current: Option<&mut Value>) {
    let mut entries: Vec<&mut Value> = history.iter_mut().collect();
    if let Some(c) = current {
        entries.push(c);
    }
    let mut prev_ids: HashSet<String> = HashSet::new();
    for entry in entries {
        if let Some(a) = entry.get("assistantResponseMessage") {
            prev_ids = a
                .get("toolUses")
                .and_then(Value::as_array)
                .map(|t| {
                    t.iter()
                        .filter_map(|u| {
                            u.get("toolUseId")
                                .and_then(Value::as_str)
                                .map(str::to_owned)
                        })
                        .collect()
                })
                .unwrap_or_default();
            continue;
        }
        let valid = std::mem::take(&mut prev_ids);
        let Some(user) = entry
            .get_mut("userInputMessage")
            .and_then(Value::as_object_mut)
        else {
            continue;
        };
        let Some(results) = user
            .get("userInputMessageContext")
            .and_then(|c| c.get("toolResults"))
            .and_then(Value::as_array)
            .cloned()
        else {
            continue;
        };
        let (kept, orphaned): (Vec<Value>, Vec<Value>) = results.into_iter().partition(|r| {
            r.get("toolUseId")
                .and_then(Value::as_str)
                .is_some_and(|id| valid.contains(id))
        });
        if orphaned.is_empty() {
            continue;
        }
        let texts: Vec<String> = orphaned
            .iter()
            .flat_map(|r| match r.get("content") {
                Some(Value::Array(parts)) => parts
                    .iter()
                    .filter_map(|p| {
                        p.get("text")
                            .and_then(Value::as_str)
                            .filter(|t| !t.is_empty())
                            .map(str::to_owned)
                    })
                    .collect(),
                Some(Value::String(s)) if !s.is_empty() => vec![s.clone()],
                _ => vec![],
            })
            .collect();
        let ctx = user
            .get_mut("userInputMessageContext")
            .and_then(Value::as_object_mut)
            .unwrap();
        if kept.is_empty() {
            ctx.remove("toolResults");
            if ctx.is_empty() {
                user.remove("userInputMessageContext");
            }
        } else {
            ctx.insert("toolResults".into(), Value::Array(kept));
        }
        if !texts.is_empty() {
            let current = user
                .get("content")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_owned();
            user.insert(
                "content".into(),
                Value::String(format!(
                    "{current}\n[trimmed tool result] {}",
                    texts.join("; ")
                )),
            );
        }
    }
}

pub fn trim_to_limit(
    payload: &mut Value,
    max_bytes: Option<usize>,
    max_tokens: Option<usize>,
    known: (usize, usize),
) -> TrimStats {
    let (original_tokens, original_bytes) = known;
    let Some(history) = history_mut(payload).filter(|h| !h.is_empty()) else {
        return TrimStats {
            original_bytes,
            final_bytes: original_bytes,
            original_tokens,
            final_tokens: original_tokens,
            ..Default::default()
        };
    };
    let original_entries = history.len();
    // The leading system-prompt pair is the instructions, not conversation:
    // dropping it would send the request without them. When the rest of the
    // history cannot make room, the payload stays over the cap and the caller
    // answers TooLarge.
    let keep = if history.len() >= 2
        && history[0].get("userInputMessage").is_some()
        && history[1].pointer("/assistantResponseMessage/content")
            == Some(&Value::String(
                crate::convert_core::SYSTEM_PROMPT_ACK.to_owned(),
            )) {
        2
    } else {
        0
    };
    for entry in history.iter_mut() {
        if let Some(a) = entry
            .get_mut("assistantResponseMessage")
            .and_then(Value::as_object_mut)
        {
            if a.get("toolUses")
                .and_then(Value::as_array)
                .is_some_and(Vec::is_empty)
            {
                a.remove("toolUses");
            }
        }
    }
    let entry_bytes: Vec<usize> = history
        .iter()
        .map(|e| compact_json(&without_image_data(e, &mut 0)).len() + 1)
        .collect();
    let tokens_per_byte = if original_bytes > 0 {
        original_tokens as f64 / original_bytes as f64
    } else {
        0.0
    };
    let (mut rem_t, mut rem_b) = (original_tokens as f64, original_bytes as f64);
    let target = max_tokens.map(|t| t as f64 * 0.97);
    let mut index = keep;
    while index < history.len() {
        let over_t = target.is_some_and(|t| rem_t > t);
        let over_b = max_bytes.is_some_and(|b| rem_b > b as f64);
        if !(over_t || over_b) {
            break;
        }
        for _ in 0..2 {
            if index < history.len() {
                rem_t -= entry_bytes[index] as f64 * tokens_per_byte;
                rem_b -= entry_bytes[index] as f64;
                index += 1;
            }
        }
    }
    history.drain(keep..index);
    loop {
        let snapshot = payload.clone();
        let h = history_mut(payload).unwrap();
        if h.len() <= keep || !over_limit(&snapshot, max_bytes, max_tokens) {
            break;
        }
        let n = (h.len() - keep).min(2);
        h.drain(keep..keep + n);
    }
    let state = payload
        .get_mut("conversationState")
        .and_then(Value::as_object_mut)
        .unwrap();
    let mut history = state
        .remove("history")
        .and_then(|h| {
            if let Value::Array(a) = h {
                Some(a)
            } else {
                None
            }
        })
        .unwrap_or_default();
    while history
        .first()
        .is_some_and(|e| e.get("userInputMessage").is_none())
    {
        history.remove(0);
    }
    repair_orphans(&mut history, state.get_mut("currentMessage"));
    let final_entries = history.len();
    if !history.is_empty() {
        insert_before_key(state, "history", Value::Array(history));
    }
    let (final_tokens, final_bytes) = measure(payload);
    TrimStats {
        original_bytes,
        final_bytes,
        original_entries,
        final_entries,
        original_tokens,
        final_tokens,
    }
}

fn insert_before_key(map: &mut Map<String, Value>, key: &str, value: Value) {
    map.insert(key.to_owned(), value);
}

#[cfg(test)]
mod tests {
    use super::*;
    use base64::Engine;
    use serde_json::json;

    fn b64(bytes: &[u8]) -> String {
        base64::engine::general_purpose::STANDARD.encode(bytes)
    }

    fn png(w: u32, h: u32, extra: usize) -> String {
        let mut b = b"\x89PNG\r\n\x1a\n\0\0\0\rIHDR".to_vec();
        b.extend(w.to_be_bytes());
        b.extend(h.to_be_bytes());
        b.extend(std::iter::repeat_n(0x5a, extra));
        b64(&b)
    }

    #[test]
    fn reads_dimensions_per_format() {
        assert_eq!(estimate_image_tokens(&png(707, 707, 0)), 667);
        let gif = b64(&[
            b"GIF89a".as_slice(),
            &100u16.to_le_bytes(),
            &75u16.to_le_bytes(),
        ]
        .concat());
        assert_eq!(estimate_image_tokens(&gif), 10);
        let jpeg = b64(&[
            0xff, 0xd8, 0xff, 0xe0, 0x00, 0x04, 0, 0, 0xff, 0xc0, 0x00, 0x11, 0x08, 0x01, 0xf4,
            0x02, 0xee, 0x03,
        ]);
        assert_eq!(estimate_image_tokens(&jpeg), 500);
        let mut webp = b"RIFF\0\0\0\0WEBPVP8X".to_vec();
        webp.extend([0u8; 8]);
        webp.extend([0xc7, 0x02, 0x00, 0xc7, 0x02, 0x00]);
        assert_eq!(estimate_image_tokens(&b64(&webp)), 676);
    }

    #[test]
    fn large_or_unknown_images_take_the_ceiling() {
        assert_eq!(
            estimate_image_tokens(&png(4000, 3000, 0)),
            IMAGE_TOKEN_FALLBACK
        );
        assert_eq!(estimate_image_tokens("not-an-image"), IMAGE_TOKEN_FALLBACK);
    }

    fn payload(image: &str, text: &str) -> Value {
        json!({"conversationState": {"currentMessage": {"userInputMessage": {
            "content": text,
            "images": [{"format": "png", "source": {"bytes": image}}],
        }}}})
    }

    #[test]
    fn image_data_is_not_measured_as_text() {
        let image = png(707, 707, 1_500_000);
        let p = payload(&image, "describe this");
        let (tokens, bytes) = measure(&p);
        assert!(tokens < 1000, "{tokens}");
        assert!(bytes < 500, "{bytes}");
        assert_eq!(
            p.pointer("/conversationState/currentMessage/userInputMessage/images/0/source/bytes"),
            Some(&json!(image))
        );
    }

    #[test]
    fn text_over_the_cap_still_counts_with_an_image() {
        let text = "word ".repeat(20_000);
        let (tokens, _) = measure(&payload(&png(10, 10, 0), &text));
        assert!(tokens > 19_000, "{tokens}");
    }
}
