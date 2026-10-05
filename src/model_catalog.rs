//! Account model catalogue and quota usage, both served by the management host
//! with the same calls and headers the Kiro IDE uses.

use serde_json::{json, Value};
use std::time::Duration;

use crate::auth::{region_from_arn, AuthError, KiroAuth};
use crate::config;
use crate::utils::{management_headers, CODEWHISPERER_API, CONTROL_PLANE_API};

#[derive(Debug)]
pub(crate) enum ManagementError {
    Auth(AuthError),
    Http { status: u16, body: String },
    Other(String),
}

impl std::fmt::Display for ManagementError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Auth(e) => e.fmt(f),
            Self::Http { status, .. } => write!(f, "management host answered {status}"),
            Self::Other(message) => f.write_str(message),
        }
    }
}

fn region(auth: &KiroAuth) -> Result<String, String> {
    let arn = auth.profile_arn().unwrap_or_default();
    let parts: Vec<&str> = arn.split(':').collect();
    if parts.get(2) == Some(&"codewhisperer") {
        return region_from_arn(&arn).ok_or_else(|| {
            "invalid profile ARN region: expected a lowercase AWS region such as us-east-1 or us-gov-west-1".into()
        });
    }
    config::validate_region(&auth.api_region)
        .map(|_| auth.api_region.clone())
        .map_err(|e| e.to_string())
}

enum ManagementCall<'a> {
    Json {
        target: &'a str,
    },
    Get {
        path: &'a str,
        query: &'a [(&'a str, &'a str)],
    },
}

async fn management_call(
    auth: &KiroAuth,
    http: &reqwest::Client,
    call: ManagementCall<'_>,
    label: &str,
) -> Result<Value, ManagementError> {
    let region = region(auth).map_err(ManagementError::Other)?;
    let token = auth.access_token().await.map_err(ManagementError::Auth)?;
    let arn = auth.request_profile_arn().or_else(|| auth.profile_arn());
    let base = format!("https://management.{region}.kiro.dev/");
    #[cfg(debug_assertions)]
    let base = std::env::var("KIRO_TEST_MANAGEMENT_URL").unwrap_or(base);
    let (mut req, target) = match call {
        ManagementCall::Json { target } => {
            let mut body = json!({"origin": "AI_EDITOR"});
            if let Some(a) = &arn {
                body["profileArn"] = json!(a);
            }
            (http.post(base).json(&body), Some(target))
        }
        ManagementCall::Get { path, query } => {
            let mut params: Vec<(&str, String)> = vec![("origin", "AI_EDITOR".into())];
            if let Some(a) = &arn {
                params.push(("profileArn", a.clone()));
            }
            params.extend(query.iter().map(|(k, v)| (*k, (*v).to_owned())));
            (http.get(format!("{base}{path}")).query(&params), None)
        }
    };
    req = req.timeout(Duration::from_secs(20));
    for (k, v) in management_headers(&token, target, label, &auth.machine_id()) {
        req = req.header(k, v);
    }
    let resp = req
        .send()
        .await
        .map_err(|e| ManagementError::Other(e.to_string()))?;
    let status = resp.status();
    if !status.is_success() {
        // Bound error bodies; retain them only in memory for classification.
        let mut resp = resp;
        let mut body = Vec::new();
        while let Some(chunk) = resp
            .chunk()
            .await
            .map_err(|e| ManagementError::Other(e.to_string()))?
        {
            if body.len() + chunk.len() > 16 * 1024 {
                body.clear();
                break;
            }
            body.extend_from_slice(&chunk);
        }
        return Err(ManagementError::Http {
            status: status.as_u16(),
            body: String::from_utf8_lossy(&body).into_owned(),
        });
    }
    resp.json::<Value>()
        .await
        .map_err(|e| ManagementError::Other(e.to_string()))
}

pub async fn fetch_available_models(auth: &KiroAuth, http: &reqwest::Client) -> Option<Vec<Value>> {
    match management_call(
        auth,
        http,
        ManagementCall::Json {
            target: "KiroControlPlaneBearerService.ListAvailableModels",
        },
        CONTROL_PLANE_API,
    )
    .await
    {
        Ok(v) => {
            let models: Vec<Value> = v
                .get("models")?
                .as_array()?
                .iter()
                .filter(|m| {
                    m.get("modelId")
                        .is_some_and(|x| x.as_str().is_some_and(|s| !s.is_empty()))
                })
                .cloned()
                .collect();
            (!models.is_empty()).then_some(models)
        }
        Err(e) => {
            tracing::debug!("[Models] Could not list models: {e}");
            None
        }
    }
}

fn number(v: Option<&Value>) -> Option<f64> {
    v.filter(|x| x.is_number()).and_then(Value::as_f64)
}

pub async fn fetch_account_usage(
    auth: &KiroAuth,
    http: &reqwest::Client,
    stored_arn: Option<String>,
) -> Result<Value, String> {
    fetch_account_usage_checked(auth, http, stored_arn)
        .await
        .map_err(|e| e.to_string())
}

pub(crate) async fn fetch_account_usage_checked(
    auth: &KiroAuth,
    http: &reqwest::Client,
    stored_arn: Option<String>,
) -> Result<Value, ManagementError> {
    if auth.request_profile_arn().is_none() && stored_arn.is_none() {
        return Err(ManagementError::Other(
            "profile ARN is not available yet for this account".into(),
        ));
    }
    let payload = management_call(
        auth,
        http,
        ManagementCall::Get {
            path: "getUsageLimits",
            query: &[
                ("resourceType", "AGENTIC_REQUEST"),
                ("isEmailRequired", "true"),
            ],
        },
        CODEWHISPERER_API,
    )
    .await?;
    if !payload.is_object() {
        return Err(ManagementError::Other("invalid usage response".into()));
    }
    let breakdowns = payload
        .get("usageBreakdownList")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let b = breakdowns
        .iter()
        .find(|e| e.get("resourceType").and_then(Value::as_str) == Some("AGENTIC_REQUEST"))
        .or(breakdowns.first())
        .cloned()
        .unwrap_or(json!({}));
    let sub = payload
        .get("subscriptionInfo")
        .cloned()
        .unwrap_or(json!({}));
    let over = payload
        .get("overageConfiguration")
        .cloned()
        .unwrap_or(json!({}));
    let current = number(b.get("currentUsageWithPrecision")).or(number(b.get("currentUsage")));
    let limit = number(b.get("usageLimitWithPrecision")).or(number(b.get("usageLimit")));
    let pick = |v: Option<&Value>| {
        v.and_then(Value::as_str)
            .filter(|s| !s.is_empty())
            .map(str::to_owned)
    };
    Ok(json!({
        "userId": pick(payload.pointer("/userInfo/userId")),
        "email": pick(payload.pointer("/userInfo/email")),
        "subscriptionTitle": pick(sub.get("subscriptionTitle")).or(pick(sub.get("type"))).unwrap_or("Unknown".into()),
        "subscriptionType": pick(sub.get("type")).unwrap_or("Unknown".into()),
        "resourceType": pick(b.get("resourceType")).unwrap_or("AGENTIC_REQUEST".into()),
        "currentUsage": current,
        "usageLimit": limit,
        "usagePercent": match (current, limit) { (Some(c), Some(l)) if l > 0.0 => Some(c / l * 100.0), _ => None },
        "unit": pick(b.get("unit")).unwrap_or_default(),
        "overageStatus": pick(over.get("overageStatus")).unwrap_or("UNKNOWN".into()),
        "overageUsed": number(b.get("currentOveragesWithPrecision")).or(number(b.get("currentOverages"))),
        "overageRate": number(b.get("overageRate")),
        "nextDateReset": payload.get("nextDateReset").cloned().unwrap_or(Value::Null),
        "daysUntilReset": payload.get("daysUntilReset").cloned().unwrap_or(Value::Null),
    }))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::auth::Source;
    use std::io::ErrorKind;
    use std::net::TcpListener;

    #[tokio::test]
    async fn management_rejects_an_invalid_region_before_refresh_or_request() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        listener.set_nonblocking(true).unwrap();
        let proxy =
            reqwest::Proxy::all(format!("http://{}", listener.local_addr().unwrap())).unwrap();
        let http = reqwest::Client::builder().proxy(proxy).build().unwrap();
        let mut auth = KiroAuth::new(
            Source::File("/nonexistent/region-validation-credentials.json".into()),
            config::REGION,
            None,
            http.clone(),
        )
        .unwrap();
        auth.api_region = "us-east-1/path".into();

        let error = management_call(
            &auth,
            &http,
            ManagementCall::Json { target: "unused" },
            "unused",
        )
        .await
        .unwrap_err();

        assert!(error.to_string().starts_with("invalid region:"));
        assert_eq!(listener.accept().unwrap_err().kind(), ErrorKind::WouldBlock);
    }
}
