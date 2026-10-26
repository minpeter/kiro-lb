//! Device-authorization login: social (Google/GitHub) on the Kiro desktop auth host,
//! and Builder ID / IAM Identity Center over AWS SSO OIDC. The two contracts are near-inverses, so the
//! polling logic is deliberately not shared.

use parking_lot::Mutex;
use serde_json::{json, Value};
use std::collections::HashMap;
use std::time::Duration;

use crate::{config, store::now_f64};

const AUTH_HOST: &str = "https://prod.us-east-1.auth.desktop.kiro.dev";
const CLIENT_ID: &str = "kiro-cli";
const SOCIAL_REGION: &str = "us-east-1";
const BUILDER_ID_START_URL: &str = "https://view.awsapps.com/start";
const BUILDER_ID_REGION: &str = "us-east-1";
const BUILDER_ID_SCOPES: [&str; 3] = [
    "codewhisperer:completions",
    "codewhisperer:analysis",
    "codewhisperer:conversations",
];
const FLOW_GRACE: f64 = 60.0;
// The same discovery regions as kiro-cli; an organization's SSO region need
// not be the region that hosts its CodeWhisperer profile.
const PROFILE_ENDPOINTS: [&str; 2] = [
    "https://q.us-east-1.amazonaws.com/",
    "https://q.eu-central-1.amazonaws.com/",
];

pub struct IdentityCenterConfig {
    start_url: String,
    region: String,
}

impl IdentityCenterConfig {
    pub fn from_payload(payload: &Value) -> Result<Self, String> {
        let start_url = payload
            .get("startUrl")
            .and_then(Value::as_str)
            .map(str::trim)
            .ok_or("Identity Center requires a start URL")?;
        let url = reqwest::Url::parse(start_url)
            .map_err(|_| "Identity Center requires an absolute HTTPS start URL")?;
        if !start_url
            .get(..8)
            .is_some_and(|scheme| scheme.eq_ignore_ascii_case("https://"))
            || url.scheme() != "https"
            || url.host_str().is_none()
            || !url.username().is_empty()
            || url.password().is_some()
            || url.query().is_some()
            || url.fragment().is_some()
        {
            return Err("Identity Center requires an HTTPS start URL without credentials, query, or fragment".into());
        }
        let region = payload
            .get("region")
            .and_then(Value::as_str)
            .map(str::trim)
            .ok_or("Identity Center requires a region")?;
        config::validate_region(region).map_err(|e| e.to_string())?;
        Ok(Self {
            start_url: start_url.to_owned(),
            region: region.to_owned(),
        })
    }
}

#[derive(Clone, serde::Deserialize, serde::Serialize)]
#[serde(rename_all = "camelCase")]
struct Profile {
    arn: String,
    profile_name: String,
}

#[derive(serde::Deserialize)]
#[serde(rename_all = "camelCase")]
struct ProfilePage {
    #[serde(default)]
    profiles: Vec<Profile>,
    next_token: Option<String>,
}

#[derive(Clone)]
pub struct DeviceFlow {
    pub id: String,
    pub provider: &'static str,
    device_code: String,
    user_code: String,
    verification_uri: String,
    verification_uri_complete: String,
    expires_at: f64,
    interval: f64,
    pub status: String,
    detail: Option<String>,
    pub token: Option<Value>,
    registration: Option<Value>,
    profiles: Vec<Profile>,
    next_poll_at: f64,
    polling: bool,
}

impl DeviceFlow {
    pub fn expires_at_ms(&self) -> i64 {
        (self.expires_at * 1000.0) as i64
    }
    pub fn interval_seconds(&self) -> u64 {
        self.interval.max(1.0) as u64
    }
    pub fn authorization_url(&self) -> &str {
        if self.verification_uri_complete.starts_with("https://") {
            &self.verification_uri_complete
        } else {
            &self.verification_uri
        }
    }
    pub fn user_code(&self) -> &str {
        &self.user_code
    }

    pub fn view(&self) -> Value {
        json!({
            "flowId": self.id, "provider": self.provider, "status": self.status, "detail": self.detail,
            "userCode": self.user_code, "verificationUri": self.verification_uri,
            "verificationUriComplete": self.verification_uri_complete,
            "expiresInSeconds": (self.expires_at - now_f64()).max(0.0) as i64,
            "profiles": self.profiles,
        })
    }
}

static FLOWS: Mutex<Option<HashMap<String, DeviceFlow>>> = Mutex::new(None);

pub fn resolve_provider(raw: &str) -> Result<&'static str, String> {
    match raw.trim().to_lowercase().as_str() {
        "google" => Ok("Google"),
        "github" => Ok("Github"),
        "builder-id" | "builderid" | "builder_id" => Ok("BuilderId"),
        "idc" => Ok("IdentityCenter"),
        _ => Err("provider must be google, github, builder-id, or idc".into()),
    }
}

fn flow_id() -> String {
    use base64::Engine;
    use rand::RngCore;
    let mut b = [0u8; 12];
    rand::thread_rng().fill_bytes(&mut b);
    base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(b)
}

async fn social_post(http: &reqwest::Client, path: &str, body: Value) -> Result<Value, String> {
    let resp = http
        .post(format!("{AUTH_HOST}{path}"))
        .json(&body)
        .timeout(Duration::from_secs(20))
        .send()
        .await
        .map_err(|e| e.to_string())?;
    let status = resp.status().as_u16();
    let text = resp.text().await.unwrap_or_default();
    if status >= 400 {
        let msg = serde_json::from_str::<Value>(&text)
            .ok()
            .and_then(|v| v.get("message").and_then(Value::as_str).map(str::to_owned))
            .unwrap_or(text);
        return Err(format!("HTTP {status}: {msg}"));
    }
    serde_json::from_str(&text).map_err(|e| e.to_string())
}

async fn oidc_call(
    http: &reqwest::Client,
    region: &str,
    path: &str,
    body: Value,
) -> Result<Value, String> {
    let region = config::validate_region(region).map_err(|e| e.to_string())?;
    let resp = http
        .post(format!("https://oidc.{region}.amazonaws.com{path}"))
        .json(&body)
        .timeout(Duration::from_secs(20))
        .send()
        .await
        .map_err(|e| e.to_string())?;
    let status = resp.status().as_u16();
    let errtype = resp
        .headers()
        .get("x-amzn-errortype")
        .and_then(|v| v.to_str().ok())
        .unwrap_or("")
        .split(':')
        .next()
        .unwrap_or("")
        .to_owned();
    let text = resp.text().await.unwrap_or_default();
    let payload: Value = serde_json::from_str(&text).unwrap_or(json!({}));
    if status >= 400 {
        let code = payload
            .get("error")
            .and_then(Value::as_str)
            .map(str::to_owned)
            .filter(|s| !s.is_empty())
            .unwrap_or(if errtype.is_empty() {
                format!("HTTP_{status}")
            } else {
                errtype
            });
        // Error descriptions can echo private organization configuration.
        // Diagnostics retain only AWS's machine code, never request bodies.
        let diagnostic_code =
            if code.len() <= 64 && code.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'_') {
                code.as_str()
            } else {
                "unrecognized"
            };
        tracing::debug!(
            oidc_path = path,
            http_status = status,
            error_code = diagnostic_code,
            "AWS SSO OIDC request rejected"
        );
        let msg = payload
            .get("error_description")
            .or_else(|| payload.get("message"))
            .and_then(Value::as_str)
            .map(str::to_owned)
            .unwrap_or(text);
        return Err(format!("{code}: {msg}"));
    }
    Ok(payload)
}

fn num(v: &Value, k: &str) -> Option<f64> {
    v.get(k).and_then(Value::as_f64).filter(|n| *n != 0.0)
}

fn s(v: &Value, k: &str) -> String {
    v.get(k)
        .map(|x| match x {
            Value::String(s) => s.clone(),
            Value::Null => String::new(),
            o => o.to_string(),
        })
        .unwrap_or_default()
}

pub async fn start(http: &reqwest::Client, provider: &'static str) -> Result<Value, String> {
    start_with_config(http, provider, None).await
}

pub async fn start_identity_center(
    http: &reqwest::Client,
    config: IdentityCenterConfig,
) -> Result<Value, String> {
    start_with_config(http, "IdentityCenter", Some(config))
        .await
        .map_err(|reason| identity_center_error(&reason))
}

fn identity_center_error(reason: &str) -> String {
    // AWS error descriptions can echo the organization's private start URL.
    // Preserve only a machine error code, never the upstream description.
    let code = reason.split(':').next().unwrap_or("");
    if !code.is_empty()
        && code.len() <= 64
        && code.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'_')
    {
        format!("AWS SSO OIDC rejected the organization login ({code})")
    } else {
        "AWS SSO OIDC could not complete the organization login".into()
    }
}

async fn start_with_config(
    http: &reqwest::Client,
    provider: &'static str,
    identity_center: Option<IdentityCenterConfig>,
) -> Result<Value, String> {
    if provider == "IdentityCenter" && identity_center.is_none() {
        return Err("Identity Center requires a start URL and region".into());
    }
    let flow = if matches!(provider, "BuilderId" | "IdentityCenter") {
        let (region, start_url) = identity_center
            .as_ref()
            .map(|c| (c.region.as_str(), c.start_url.as_str()))
            .unwrap_or((BUILDER_ID_REGION, BUILDER_ID_START_URL));
        let mut request =
            json!({"clientName": "kiro-cli", "clientType": "public", "scopes": BUILDER_ID_SCOPES});
        if identity_center.is_some() {
            request["issuerUrl"] = json!(start_url);
            request["grantTypes"] = json!([
                "urn:ietf:params:oauth:grant-type:device_code",
                "refresh_token"
            ]);
        }
        let reg = oidc_call(http, region, "/client/register", request).await?;
        if ["clientId", "clientSecret"]
            .iter()
            .any(|k| reg.get(k).and_then(Value::as_str).is_none_or(str::is_empty))
        {
            return Err("AWS SSO OIDC returned an incomplete client registration".into());
        }
        let auth = oidc_call(http, region, "/device_authorization", json!({"clientId": reg["clientId"], "clientSecret": reg["clientSecret"], "startUrl": start_url})).await?;
        DeviceFlow {
            id: flow_id(),
            provider,
            device_code: s(&auth, "deviceCode"),
            user_code: s(&auth, "userCode"),
            verification_uri: s(&auth, "verificationUri"),
            verification_uri_complete: s(&auth, "verificationUriComplete"),
            expires_at: now_f64() + num(&auth, "expiresIn").unwrap_or(600.0),
            interval: num(&auth, "interval").unwrap_or(5.0).max(1.0),
            status: "pending".into(),
            detail: None,
            token: None,
            registration: Some(
                json!({"clientId": reg["clientId"], "clientSecret": reg["clientSecret"], "region": region, "startUrl": start_url}),
            ),
            profiles: Vec::new(),
            next_poll_at: now_f64() + num(&auth, "interval").unwrap_or(5.0).max(1.0),
            polling: false,
        }
    } else {
        let p = social_post(
            http,
            "/oauth/device/authorization",
            json!({"clientId": CLIENT_ID, "loginProvider": provider}),
        )
        .await?;
        DeviceFlow {
            id: flow_id(),
            provider,
            device_code: s(&p, "deviceCode"),
            user_code: s(&p, "userCode"),
            verification_uri: s(&p, "verificationUri"),
            verification_uri_complete: s(&p, "verificationUriComplete"),
            expires_at: now_f64() + num(&p, "expiresInMilliseconds").unwrap_or(300_000.0) / 1000.0,
            interval: (num(&p, "intervalInMilliseconds").unwrap_or(5000.0) / 1000.0).max(1.0),
            status: "pending".into(),
            detail: None,
            token: None,
            registration: None,
            profiles: Vec::new(),
            next_poll_at: 0.0,
            polling: false,
        }
    };
    let view = flow.view();
    let mut guard = FLOWS.lock();
    let flows = guard.get_or_insert_with(HashMap::new);
    let cutoff = now_f64() - FLOW_GRACE;
    flows.retain(|_, f| f.expires_at >= cutoff);
    tracing::info!("Started {provider} device login {}", flow.id);
    flows.insert(flow.id.clone(), flow);
    Ok(view)
}

fn get(id: &str) -> Option<DeviceFlow> {
    FLOWS.lock().as_ref().and_then(|f| f.get(id).cloned())
}

pub fn exists(id: &str) -> bool {
    get(id).is_some()
}

pub fn peek(id: &str) -> Option<DeviceFlow> {
    get(id)
}

/// Publishes a poll result only while the flow is still registered: a poll
/// that was in flight when the operator cancelled must not bring it back.
fn put(flow: DeviceFlow) {
    if let Some(f) = FLOWS.lock().as_mut() {
        if let Some(slot) = f.get_mut(&flow.id) {
            *slot = flow;
        }
    }
}

pub fn discard(id: &str) {
    if let Some(f) = FLOWS.lock().as_mut() {
        f.remove(id);
    }
}

pub async fn poll(http: &reqwest::Client, id: &str) -> Result<DeviceFlow, String> {
    let mut flow = {
        let mut guard = FLOWS.lock();
        let flow = guard
            .as_mut()
            .and_then(|flows| flows.get_mut(id))
            .ok_or("Unknown or expired login flow")?;
        if flow.polling {
            return Ok(flow.clone());
        }
        if flow.status == "pending" && now_f64() <= flow.expires_at {
            if now_f64() < flow.next_poll_at {
                return Ok(flow.clone());
            }
            flow.polling = true;
        }
        flow.clone()
    };
    if matches!(flow.status.as_str(), "approved" | "failed" | "expired") {
        return Ok(flow);
    }
    if now_f64() > flow.expires_at {
        flow.polling = false;
        flow.token = None;
        flow.status = "expired".into();
        flow.detail = Some("The approval window closed before the login was confirmed".into());
        put(flow.clone());
        return Ok(flow);
    }
    if flow.status == "selecting_profile" {
        return Ok(flow);
    }
    if matches!(flow.provider, "BuilderId" | "IdentityCenter") {
        let reg = flow.registration.clone().unwrap_or(json!({}));
        let region = reg
            .get("region")
            .and_then(Value::as_str)
            .unwrap_or(BUILDER_ID_REGION)
            .to_owned();
        tracing::debug!(
            flow_id = %flow.id,
            region,
            interval_seconds = flow.interval,
            "Polling AWS SSO OIDC device authorization"
        );
        match oidc_call(http, &region, "/token", json!({"clientId": reg["clientId"], "clientSecret": reg["clientSecret"], "deviceCode": flow.device_code, "grantType": "urn:ietf:params:oauth:grant-type:device_code"})).await {
            Err(reason) => {
                if reason.contains("authorization_pending") || reason.contains("AuthorizationPending") {
                } else if reason.contains("slow_down") || reason.contains("SlowDown") {
                    flow.interval += 5.0;
                } else {
                    flow.status = if reason.contains("expired_token") || reason.contains("ExpiredToken") { "expired" } else { "failed" }.into();
                    flow.detail = Some(if flow.provider == "IdentityCenter" { identity_center_error(&reason) } else { reason });
                }
            }
            Ok(p) => match p.get("accessToken").filter(|t| t.as_str().is_some_and(|s| !s.is_empty())) {
                Some(t) => {
                    flow.token = Some(json!({"accessToken": t, "refreshToken": p.get("refreshToken"), "expiresIn": p.get("expiresIn"), "profileArn": null}));
                    flow.status = "approved".into();
                    flow.detail = None;
                    if flow.provider == "IdentityCenter" {
                        match discover_profiles(http, &s(&p, "accessToken")).await {
                            Ok(profiles) => {
                                flow.profiles = profiles;
                                if flow.profiles.len() == 1 {
                                    if let Some(token) = flow.token.as_mut() {
                                        token["profileArn"] = json!(flow.profiles[0].arn);
                                    }
                                } else {
                                    flow.status = "selecting_profile".into();
                                }
                            }
                            Err(reason) => {
                                flow.status = "failed".into();
                                flow.detail = Some(reason);
                                flow.token = None;
                            }
                        }
                    }
                }
                None => {
                    flow.status = "failed".into();
                    flow.detail = Some("AWS SSO OIDC returned no access token".into());
                }
            },
        }
    } else {
        match social_post(
            http,
            "/oauth/device/poll",
            json!({"clientId": CLIENT_ID, "deviceCode": flow.device_code}),
        )
        .await
        {
            Err(e) => {
                flow.status = "failed".into();
                flow.detail = Some(e);
            }
            Ok(p) => {
                if let Some(t) = p
                    .get("accessToken")
                    .filter(|t| t.as_str().is_some_and(|s| !s.is_empty()))
                {
                    flow.token = Some(
                        json!({"accessToken": t, "refreshToken": p.get("refreshToken"), "profileArn": p.get("profileArn"), "identityProvider": p.get("identityProvider"), "expiresIn": p.get("expiresIn")}),
                    );
                    flow.status = "approved".into();
                    flow.detail = None;
                } else {
                    let upstream = p
                        .get("status")
                        .and_then(Value::as_str)
                        .filter(|s| !s.is_empty())
                        .unwrap_or("authorization_pending")
                        .to_owned();
                    if upstream != "authorization_pending" {
                        flow.status = if upstream.contains("expired") {
                            "expired"
                        } else {
                            "failed"
                        }
                        .into();
                        flow.detail = Some(format!("Device authorization {upstream}"));
                    }
                }
            }
        }
    }
    flow.polling = false;
    flow.next_poll_at = now_f64() + flow.interval;
    put(flow.clone());
    Ok(flow)
}

async fn profiles_at(
    http: &reqwest::Client,
    endpoint: &str,
    access_token: &str,
) -> Result<Vec<Profile>, String> {
    let mut profiles = Vec::new();
    let mut next_token = None;
    let mut seen = std::collections::HashSet::new();
    loop {
        let mut body = json!({});
        if let Some(token) = &next_token {
            body["nextToken"] = json!(token);
        }
        let response = http
            .post(endpoint)
            .bearer_auth(access_token)
            .header("content-type", "application/x-amz-json-1.0")
            .header(
                "x-amz-target",
                "AmazonCodeWhispererService.ListAvailableProfiles",
            )
            .json(&body)
            .timeout(Duration::from_secs(20))
            .send()
            .await
            .map_err(|_| "Could not contact the Identity Center profile service")?;
        if !response.status().is_success() {
            return Err(format!(
                "Identity Center profile discovery returned HTTP {}",
                response.status().as_u16()
            ));
        }
        let page: ProfilePage = response
            .json()
            .await
            .map_err(|_| "Identity Center returned an invalid profile list")?;
        for profile in page.profiles {
            if crate::auth::region_from_arn(&profile.arn).is_none()
                || !profile.arn.starts_with("arn:")
                || profile.arn.split(':').nth(2) != Some("codewhisperer")
            {
                return Err("Identity Center returned an invalid profile ARN".into());
            }
            profiles.push(profile);
        }
        match page.next_token.filter(|t| !t.is_empty()) {
            Some(token) => {
                if !seen.insert(token.clone()) {
                    return Err("Identity Center returned a repeated profile page".into());
                }
                next_token = Some(token);
            }
            None => return Ok(profiles),
        }
    }
}

async fn discover_profiles(http: &reqwest::Client, token: &str) -> Result<Vec<Profile>, String> {
    let mut profiles = Vec::new();
    let mut succeeded = false;
    for endpoint in PROFILE_ENDPOINTS {
        match profiles_at(http, endpoint, token).await {
            Ok(found) => {
                succeeded = true;
                for profile in found {
                    if !profiles.iter().any(|p: &Profile| p.arn == profile.arn) {
                        profiles.push(profile);
                    }
                }
            }
            // Like kiro-cli, a regional failure does not hide profiles in the
            // other discovery region. Never log upstream bodies or identities.
            Err(_) => tracing::warn!("Identity Center profile discovery failed in one region"),
        }
    }
    if profiles.is_empty() {
        return Err(if succeeded {
            "No Kiro profiles are available for this organization. Ask your administrator to assign access."
        } else {
            "Could not discover Identity Center profiles. Try signing in again."
        }.into());
    }
    Ok(profiles)
}

pub fn select_profile(id: &str, arn: &str) -> Result<DeviceFlow, String> {
    let mut guard = FLOWS.lock();
    let flow = guard
        .as_mut()
        .and_then(|flows| flows.get_mut(id))
        .ok_or("Unknown or expired login flow")?;
    if flow.status != "selecting_profile" || now_f64() > flow.expires_at {
        return Err("Login is not waiting for profile selection".into());
    }
    if !flow.profiles.iter().any(|p| p.arn == arn) {
        return Err("Select a profile returned by this login".into());
    }
    let token = flow.token.as_mut().ok_or("Login has no approved token")?;
    token["profileArn"] = json!(arn);
    flow.status = "approved".into();
    Ok(flow.clone())
}

pub fn internal_credentials(flow: &DeviceFlow) -> Result<Value, String> {
    let token = flow
        .token
        .as_ref()
        .filter(|_| flow.status == "approved")
        .ok_or_else(|| format!("{} login is not approved", flow.provider))?;
    if token
        .get("refreshToken")
        .and_then(Value::as_str)
        .is_none_or(str::is_empty)
    {
        return Err("approved login returned no refresh token".into());
    }
    let expires_in = token
        .get("expiresIn")
        .and_then(Value::as_f64)
        .filter(|v| *v != 0.0)
        .unwrap_or(3600.0);
    let expires =
        crate::auth::iso_from_epoch((now_f64() + expires_in).floor()).replace("+00:00", "Z");
    let mut doc = json!({"refreshToken": token.get("refreshToken"), "accessToken": token.get("accessToken"), "expiresAt": expires, "region": SOCIAL_REGION});
    if matches!(flow.provider, "BuilderId" | "IdentityCenter") {
        let reg = flow
            .registration
            .as_ref()
            .ok_or("AWS SSO OIDC login has no client registration")?;
        doc["region"] = reg["region"].clone();
        doc["clientId"] = reg["clientId"].clone();
        doc["clientSecret"] = reg["clientSecret"].clone();
        doc["startUrl"] = reg["startUrl"].clone();
        if flow.provider == "IdentityCenter" {
            let arn = token
                .get("profileArn")
                .and_then(Value::as_str)
                .ok_or("Identity Center login requires a profile")?;
            let region = crate::auth::region_from_arn(arn)
                .ok_or("Identity Center returned an invalid profile ARN")?;
            doc["profileArn"] = json!(arn);
            doc["ssoRegion"] = reg["region"].clone();
            doc["region"] = json!(region);
        }
    } else {
        for k in ["profileArn", "identityProvider"] {
            if let Some(v) = token
                .get(k)
                .filter(|v| v.as_str().is_some_and(|s| !s.is_empty()))
            {
                doc[k] = v.clone();
            }
        }
    }
    Ok(doc)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::ErrorKind;
    use std::net::TcpListener;

    fn flow(id: &str) -> DeviceFlow {
        DeviceFlow {
            id: id.into(),
            provider: "Google",
            device_code: "dc".into(),
            user_code: "uc".into(),
            verification_uri: String::new(),
            verification_uri_complete: String::new(),
            expires_at: now_f64() + 600.0,
            interval: 5.0,
            status: "pending".into(),
            detail: None,
            token: None,
            registration: None,
            profiles: Vec::new(),
            next_poll_at: 0.0,
            polling: false,
        }
    }

    #[tokio::test]
    async fn invalid_registration_region_is_rejected_without_an_outbound_request() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        listener.set_nonblocking(true).unwrap();
        let proxy =
            reqwest::Proxy::all(format!("http://{}", listener.local_addr().unwrap())).unwrap();
        let http = reqwest::Client::builder().proxy(proxy).build().unwrap();

        let error = oidc_call(
            &http,
            "us-east-1@localhost",
            "/token",
            json!({"refreshToken": "unused"}),
        )
        .await
        .unwrap_err();

        assert!(error.starts_with("invalid region:"));
        assert_eq!(listener.accept().unwrap_err().kind(), ErrorKind::WouldBlock);
    }

    #[test]
    fn a_poll_that_completes_after_cancel_does_not_resurrect_the_flow() {
        let id = "cancelled-mid-poll";
        FLOWS
            .lock()
            .get_or_insert_with(HashMap::new)
            .insert(id.into(), flow(id));
        let mut in_flight = get(id).unwrap();
        discard(id);
        in_flight.status = "approved".into();
        in_flight.token = Some(json!({"refreshToken": "rt"}));
        put(in_flight);
        assert!(get(id).is_none());
    }

    #[test]
    fn a_poll_result_updates_a_flow_that_is_still_registered() {
        let id = "still-registered";
        FLOWS
            .lock()
            .get_or_insert_with(HashMap::new)
            .insert(id.into(), flow(id));
        let mut in_flight = get(id).unwrap();
        in_flight.status = "approved".into();
        put(in_flight);
        assert_eq!(get(id).unwrap().status, "approved");
        discard(id);
    }

    fn organization_flow(id: &str) -> DeviceFlow {
        let mut flow = flow(id);
        flow.provider = "IdentityCenter";
        flow.registration = Some(json!({
            "clientId": "client", "clientSecret": "secret",
            "region": "us-west-2", "startUrl": "https://example.awsapps.com/start"
        }));
        flow.token = Some(json!({
            "accessToken": "access", "refreshToken": "refresh", "expiresIn": 3600,
            "profileArn": "arn:aws:codewhisperer:eu-central-1:123456789012:profile/test"
        }));
        flow.status = "approved".into();
        flow
    }

    #[test]
    fn organization_settings_reject_missing_and_unsafe_values() {
        for payload in [
            json!({}),
            json!({"startUrl": "https://example.awsapps.com/start"}),
            json!({"startUrl": 7, "region": "us-east-1"}),
            json!({"startUrl": "http://example.awsapps.com/start", "region": "us-east-1"}),
            json!({"startUrl": "https://user:password@example.awsapps.com/start", "region": "us-east-1"}),
            json!({"startUrl": "https://example.awsapps.com/start?token=secret", "region": "us-east-1"}),
            json!({"startUrl": "https://example.awsapps.com/start", "region": "us-east-1@localhost"}),
        ] {
            assert!(IdentityCenterConfig::from_payload(&payload).is_err());
        }
        let config = IdentityCenterConfig::from_payload(&json!({
            "startUrl": " https://example.awsapps.com/start ", "region": " eu-central-1 "
        }))
        .unwrap();
        assert_eq!(config.start_url, "https://example.awsapps.com/start");
        assert_eq!(config.region, "eu-central-1");
    }

    #[test]
    fn organization_credentials_route_api_and_refresh_to_their_own_regions() {
        let flow = organization_flow("org-region");

        let document = internal_credentials(&flow).unwrap();
        let auth = crate::auth::KiroAuth::from_device_credentials(
            "org-region",
            &document,
            reqwest::Client::new(),
        )
        .unwrap();

        assert_eq!(document["ssoRegion"], "us-west-2");
        assert_eq!(document["region"], "eu-central-1");
        assert_eq!(document["clientId"], "client");
        assert_eq!(document["clientSecret"], "secret");
        assert_eq!(auth.q_host, "https://runtime.eu-central-1.kiro.dev");
        assert_eq!(
            auth.request_profile_arn(),
            Some("arn:aws:codewhisperer:eu-central-1:123456789012:profile/test".into())
        );
    }

    #[test]
    fn organization_login_without_profile_cannot_become_builder_id() {
        let mut flow = organization_flow("org-no-profile");
        flow.token.as_mut().unwrap()["profileArn"] = Value::Null;

        assert!(internal_credentials(&flow).is_err());
    }

    #[test]
    fn profile_selection_only_accepts_this_flows_discovered_profiles() {
        let id = "org-selection";
        let mut flow = organization_flow(id);
        flow.status = "selecting_profile".into();
        flow.token.as_mut().unwrap()["profileArn"] = Value::Null;
        let arn = "arn:aws:codewhisperer:eu-central-1:123456789012:profile/selected";
        flow.profiles = vec![Profile {
            arn: arn.into(),
            profile_name: "Selected".into(),
        }];
        FLOWS
            .lock()
            .get_or_insert_with(HashMap::new)
            .insert(id.into(), flow);

        assert!(select_profile(
            id,
            "arn:aws:codewhisperer:us-east-1:000000000000:profile/foreign"
        )
        .is_err());
        assert_eq!(get(id).unwrap().status, "selecting_profile");
        let selected = select_profile(id, arn).unwrap();

        assert_eq!(selected.status, "approved");
        assert_eq!(internal_credentials(&selected).unwrap()["profileArn"], arn);
        discard(id);
    }

    #[tokio::test]
    async fn oidc_poll_interval_and_in_flight_poll_prevent_outbound_requests() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        listener.set_nonblocking(true).unwrap();
        let proxy =
            reqwest::Proxy::all(format!("http://{}", listener.local_addr().unwrap())).unwrap();
        let http = reqwest::Client::builder().proxy(proxy).build().unwrap();
        for (id, polling, next_poll_at) in [
            ("org-not-due", false, now_f64() + 600.0),
            ("org-in-flight", true, 0.0),
        ] {
            let mut flow = organization_flow(id);
            flow.status = "pending".into();
            flow.token = None;
            flow.polling = polling;
            flow.next_poll_at = next_poll_at;
            FLOWS
                .lock()
                .get_or_insert_with(HashMap::new)
                .insert(id.into(), flow);

            assert_eq!(poll(&http, id).await.unwrap().status, "pending");
            assert_eq!(listener.accept().unwrap_err().kind(), ErrorKind::WouldBlock);
            discard(id);
        }
    }

    #[tokio::test]
    async fn profile_discovery_uses_bearer_auth_and_follows_pages() {
        use axum::{body::Bytes, http::HeaderMap, routing::post, Json, Router};
        let app = Router::new().route(
            "/",
            post(|headers: HeaderMap, body: Bytes| async move {
                let body: Value = serde_json::from_slice(&body).unwrap();
                assert_eq!(headers["authorization"], "Bearer test-access");
                assert_eq!(headers["content-type"], "application/x-amz-json-1.0");
                assert_eq!(
                    headers["x-amz-target"],
                    "AmazonCodeWhispererService.ListAvailableProfiles"
                );
                Json(if body.get("nextToken").is_none() {
                    json!({"profiles": [{
                        "arn": "arn:aws:codewhisperer:us-east-1:123456789012:profile/first",
                        "profileName": "First"
                    }], "nextToken": "second-page"})
                } else {
                    assert_eq!(body["nextToken"], "second-page");
                    json!({"profiles": [{
                        "arn": "arn:aws:codewhisperer:eu-central-1:123456789012:profile/second",
                        "profileName": "Second"
                    }]})
                })
            }),
        );
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let url = format!("http://{}/", listener.local_addr().unwrap());
        let (stop, stopped) = tokio::sync::oneshot::channel::<()>();
        let server = tokio::spawn(async move {
            axum::serve(listener, app)
                .with_graceful_shutdown(async {
                    stopped.await.unwrap();
                })
                .await
                .unwrap();
        });

        let profiles = profiles_at(&reqwest::Client::new(), &url, "test-access")
            .await
            .unwrap();

        assert_eq!(profiles.len(), 2);
        assert_eq!(profiles[0].profile_name, "First");
        assert_eq!(profiles[1].profile_name, "Second");
        stop.send(()).unwrap();
        server.await.unwrap();
    }
}
