//! API docs: an OpenAPI description of the public routes and a Swagger UI page.

use axum::response::{Html, IntoResponse, Response};
use serde_json::{json, Value};

use crate::config;

fn op(tag: &str, summary: &str, auth: bool) -> Value {
    let mut v =
        json!({"tags": [tag], "summary": summary, "responses": {"200": {"description": "OK"}}});
    if auth {
        v["security"] = json!([{"bearer": []}, {"apiKey": []}]);
    }
    v
}

fn with_body(mut v: Value, example: Value) -> Value {
    v["requestBody"] = json!({"required": true, "content": {"application/json": {"schema": {"type": "object"}, "example": example}}});
    v
}

pub async fn openapi() -> Response {
    let msg = json!({"model": "claude-opus-5.5", "max_tokens": 1024, "messages": [{"role": "user", "content": "Hello"}]});
    let chat =
        json!({"model": "claude-opus-5.5", "messages": [{"role": "user", "content": "Hello"}]});
    let dash: &[(&str, &str, &str)] = &[
        ("/api/dashboard/login", "post", "Log in"),
        ("/api/dashboard/logout", "post", "Log out"),
        ("/api/dashboard/keys", "get", "List API keys"),
        ("/api/dashboard/keys", "post", "Create API key"),
        ("/api/dashboard/keys/usage", "get", "API key usage"),
        ("/api/dashboard/keys/{key_id}", "delete", "Delete API key"),
        ("/api/dashboard/keys/{key_id}", "patch", "Rename API key"),
        ("/api/dashboard/overview", "get", "Overview"),
        ("/api/dashboard/accounts", "get", "List accounts"),
        ("/api/dashboard/accounts", "post", "Register account"),
        ("/api/dashboard/accounts/usage", "get", "Account usage"),
        (
            "/api/dashboard/accounts/refresh-usage",
            "post",
            "Refresh usage",
        ),
        (
            "/api/dashboard/accounts/device-login",
            "post",
            "Start device login",
        ),
        (
            "/api/dashboard/accounts/device-login/{flow_id}",
            "get",
            "Poll device login",
        ),
        (
            "/api/dashboard/accounts/device-login/{flow_id}",
            "delete",
            "Cancel device login",
        ),
        (
            "/api/dashboard/accounts/device-login/{flow_id}/register",
            "post",
            "Register device login",
        ),
        (
            "/api/dashboard/accounts/browser-login",
            "post",
            "Start browser sign-in",
        ),
        (
            "/api/dashboard/accounts/browser-login/{flow_id}",
            "get",
            "Poll browser sign-in",
        ),
        (
            "/api/dashboard/accounts/browser-login/{flow_id}",
            "delete",
            "Cancel browser sign-in",
        ),
        (
            "/api/dashboard/accounts/browser-login/{flow_id}/callback",
            "post",
            "Finish browser sign-in from a pasted callback URL",
        ),
        (
            "/api/dashboard/accounts/browser-login/{flow_id}/register",
            "post",
            "Register browser sign-in",
        ),
        (
            "/api/dashboard/accounts/{label}",
            "delete",
            "Delete account",
        ),
        (
            "/api/dashboard/accounts/{label}/enabled",
            "post",
            "Enable or disable account",
        ),
        ("/api/dashboard/request-rate", "get", "Request rate"),
        (
            "/api/dashboard/models/refresh",
            "post",
            "Re-read every account's model catalog",
        ),
        ("/api/dashboard/endpoints", "get", "Get endpoints"),
        ("/api/dashboard/endpoints", "put", "Update endpoints"),
        ("/api/dashboard/endpoints/test", "post", "Test endpoints"),
        ("/api/dashboard/endpoints/ping", "post", "Ping endpoints"),
        ("/api/dashboard/request-logs", "get", "Request logs"),
        (
            "/api/dashboard/request-logs/{log_id}",
            "get",
            "Request log detail",
        ),
        ("/api/dashboard/data", "get", "Data overview"),
        ("/api/dashboard/data/clear", "post", "Clear data"),
        ("/api/dashboard/proxies", "get", "Get proxies"),
        ("/api/dashboard/proxies", "put", "Update proxies"),
        ("/api/dashboard/concurrency", "get", "Concurrency"),
        ("/api/dashboard/tunables", "get", "Get tunables"),
        ("/api/dashboard/tunables", "put", "Update tunables"),
        ("/api/dashboard/model-costs", "get", "Model costs"),
        ("/api/dashboard/prompt-filter", "get", "Get prompt filter"),
        (
            "/api/dashboard/prompt-filter",
            "put",
            "Update prompt filter",
        ),
        ("/api/dashboard/models", "get", "Dashboard models"),
        (
            "/api/dashboard/models",
            "put",
            "Choose the models listed in /v1/models",
        ),
    ];
    let mut spec = json!({
        "openapi": "3.0.3",
        "info": {"title": config::APP_TITLE, "version": config::APP_VERSION, "description": "Private Kiro API load balancer. OpenAI and Anthropic compatible; never fabricates reasoning content."},
        "components": {"securitySchemes": {
            "bearer": {"type": "http", "scheme": "bearer"},
            "apiKey": {"type": "apiKey", "in": "header", "name": "x-api-key"},
        }},
        "paths": {
            "/v1/messages": {"post": with_body(op("Anthropic", "Create a message (streaming with \"stream\": true)", true), msg.clone())},
            "/v1/messages/count_tokens": {"post": with_body(op("Anthropic", "Count input tokens", true), msg)},
            "/v1/chat/completions": {"post": with_body(op("OpenAI", "Chat completion (streaming with \"stream\": true)", true), chat)},
            "/v1/responses": {"post": with_body(op("OpenAI", "Responses API (Codex CLI)", true), json!({"model": "claude-opus-5.5", "input": "Hello"}))},
            "/v1/models": {"get": op("Models", "List models", true)},
            "/v1/models/{model_id}": {"get": {
                "tags": ["Models"], "summary": "Get one model", "security": [{"bearer": []}, {"apiKey": []}],
                "parameters": [{"name": "model_id", "in": "path", "required": true, "schema": {"type": "string"}}],
                "responses": {"200": {"description": "OK"}, "404": {"description": "Not found"}},
            }},
            "/health": {"get": op("Health", "Health check", false)},
            "/healthz": {"get": op("Health", "Liveness", false)},
            "/metrics": {"get": {"tags": ["Observability"], "summary": "Prometheus exposition", "security": [{"bearer": []}], "responses": {"200": {"description": "OK"}}}},
            "/": {"get": op("Dashboard", "Dashboard page", false)},
        },
    });
    spec["components"]["securitySchemes"]["session"] =
        json!({"type": "apiKey", "in": "cookie", "name": "kiro_lb_session"});
    for (path, method, summary) in dash {
        let mut o = json!({"tags": ["Dashboard"], "summary": summary, "responses": {"200": {"description": "OK"}}});
        if !path.ends_with("/login") {
            o["security"] = json!([{"session": []}]);
        }
        let params: Vec<Value> = path
            .split('/')
            .filter_map(|s| s.strip_prefix('{')?.strip_suffix('}'))
            .map(|n| json!({"name": n, "in": "path", "required": true, "schema": {"type": "string"}}))
            .collect();
        if !params.is_empty() {
            o["parameters"] = Value::Array(params);
        }
        if *path == "/api/dashboard/models" && *method == "put" {
            o["requestBody"] = json!({"required": true, "content": {"application/json": {
                "schema": {"type": "object", "required": ["hidden"], "properties": {"hidden": {"type": "array", "items": {"type": "string"}, "description": "Model ids left out of /v1/models"}}},
                "example": {"hidden": ["claude-opus-4-5"]},
            }}});
        } else if matches!(*method, "post" | "put" | "patch") {
            o["requestBody"] =
                json!({"content": {"application/json": {"schema": {"type": "object"}}}});
        }
        spec["paths"][*path][*method] = o;
    }
    axum::Json(spec).into_response()
}

pub async fn swagger() -> Html<String> {
    Html(format!(
        r##"<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{} {} - API Docs</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css"></head>
<body><div id="swagger-ui"></div>
<script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
<script>window.ui = SwaggerUIBundle({{url: "/openapi.json", dom_id: "#swagger-ui", persistAuthorization: true}});</script>
</body></html>"##,
        config::APP_TITLE,
        config::APP_VERSION
    ))
}
