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
            let mut body = json!({"origin": "KIRO_CLI"});
            let mut params: Vec<(&str, String)> = vec![("origin", "KIRO_CLI".into())];
            if let Some(a) = &arn {
                body["profileArn"] = json!(a);
                params.push(("profileArn", a.clone()));
            }
            (http.post(base).query(&params).json(&body), Some(target))
        }
        ManagementCall::Get { path, query } => {
            let mut params: Vec<(&str, String)> = vec![("origin", "KIRO_CLI".into())];
            if let Some(a) = &arn {
                params.push(("profileArn", a.clone()));
            }
            params.extend(query.iter().map(|(k, v)| (*k, (*v).to_owned())));
            (http.get(format!("{base}{path}")).query(&params), None)
        }
    };
    req = req.timeout(Duration::from_secs(20));
    for (k, v) in management_headers(&token, target, label) {
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
    fetch_models(auth, http, false)
        .await
        .filter(|models| !models.is_empty())
}

/// Unlike the general pool API, a valid empty catalogue is authoritative.
pub(crate) async fn fetch_available_models_authoritative(
    auth: &KiroAuth,
    http: &reqwest::Client,
) -> Option<Vec<Value>> {
    fetch_models(auth, http, true).await
}

async fn fetch_models(auth: &KiroAuth, http: &reqwest::Client, strict: bool) -> Option<Vec<Value>> {
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
            let entries = v.get("models")?.as_array()?;
            if strict
                && entries.iter().any(|m| {
                    m.get("modelId")
                        .and_then(Value::as_str)
                        .is_none_or(str::is_empty)
                })
            {
                return None;
            }
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
            Some(models)
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
    let (base_current, base_limit) = (
        number(b.get("currentUsageWithPrecision")).or(number(b.get("currentUsage"))),
        number(b.get("usageLimitWithPrecision")).or(number(b.get("usageLimit"))),
    );
    let (extra_used, extra_limit) = extra_allowance(&b, crate::store::now_f64());
    let current = base_current.map(|c| c + extra_used);
    let limit = base_limit.map(|l| l + extra_limit);
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

/// Credits Kiro grants on top of the plan: bonuses (first-upgrade trial
/// credits and the like), purchased add-ons and a free trial. Kiro reports the
/// plan's own allowance in `usageLimit` alone, so a plan at 1000/1000 with an
/// active 500-credit bonus still has credits left and keeps serving.
fn extra_allowance(b: &Value, now: f64) -> (f64, f64) {
    let precise = |v: &Value, key: &str| {
        number(v.get(format!("{key}WithPrecision").as_str())).or(number(v.get(key)))
    };
    let live = |v: &Value, status: &str, expiry: &str| {
        let active = v
            .get(status)
            .and_then(Value::as_str)
            .is_none_or(|s| s.eq_ignore_ascii_case("ACTIVE"));
        let unexpired = number(v.get(expiry)).is_none_or(|t| t <= 0.0 || t > now);
        active && unexpired
    };
    let mut used = 0.0;
    let mut limit = 0.0;
    let mut add = |u: Option<f64>, l: Option<f64>| {
        if let Some(l) = l.filter(|l| *l > 0.0) {
            limit += l;
            used += u.unwrap_or(0.0).clamp(0.0, l);
        }
    };
    for bonus in b
        .get("bonuses")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter(|v| live(v, "status", "expiresAt"))
    {
        add(precise(bonus, "currentUsage"), precise(bonus, "usageLimit"));
    }
    if let Some(meta) = b.get("addOnMetadata").filter(|m| m.is_object()) {
        let total = (
            number(meta.get("addOnTotalUsage")),
            number(meta.get("addOnTotalLimit")),
        );
        if total.1.is_some() {
            add(total.0, total.1);
        } else {
            for addon in meta
                .get("addOns")
                .and_then(Value::as_array)
                .into_iter()
                .flatten()
                .filter(|v| live(v, "status", "expiresAt"))
            {
                add(precise(addon, "usage"), precise(addon, "limit"));
            }
        }
    }
    if let Some(trial) = b
        .get("freeTrialInfo")
        .filter(|t| t.is_object() && live(t, "freeTrialStatus", "freeTrialExpiry"))
    {
        add(precise(trial, "currentUsage"), precise(trial, "usageLimit"));
    }
    (used, limit)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::auth::Source;
    use std::io::ErrorKind;
    use std::net::TcpListener;

    #[test]
    fn active_bonuses_add_ons_and_trials_extend_the_plan_allowance() {
        let now = 1_800_000_000.0;
        let captured = json!({
            "currentUsageWithPrecision": 978.0,
            "usageLimitWithPrecision": 978.0,
            "bonuses": [
                {"status": "ACTIVE", "currentUsage": 37.36, "usageLimit": 500.0, "expiresAt": 1825086847.468},
                {"status": "EXPIRED", "currentUsage": 10.0, "usageLimit": 100.0, "expiresAt": 1825086847.0},
                {"status": "ACTIVE", "currentUsage": 1.0, "usageLimit": 50.0, "expiresAt": 1700000000.0}
            ],
            "addOnMetadata": null,
            "freeTrialInfo": null
        });
        let (used, limit) = extra_allowance(&captured, now);
        assert!((used - 37.36).abs() < 1e-9);
        assert_eq!(limit, 500.0);

        let add_ons = json!({
            "addOnMetadata": {"addOnTotalUsage": 120.0, "addOnTotalLimit": 1000.0, "addOns": []},
            "freeTrialInfo": {"freeTrialStatus": "ACTIVE", "currentUsageWithPrecision": 5.0, "usageLimitWithPrecision": 50.0, "freeTrialExpiry": 1900000000.0}
        });
        assert_eq!(extra_allowance(&add_ons, now), (125.0, 1050.0));

        let over_used =
            json!({"bonuses": [{"status": "ACTIVE", "currentUsage": 600.0, "usageLimit": 500.0}]});
        assert_eq!(extra_allowance(&over_used, now), (500.0, 500.0));
        assert_eq!(extra_allowance(&json!({}), now), (0.0, 0.0));
    }

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
