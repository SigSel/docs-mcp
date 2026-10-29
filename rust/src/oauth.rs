//! docs-mcp as an OAuth resource server: the protected resource metadata, and the checks on access tokens
//! (SPEC.md §2.2).

use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use base64::Engine;
use base64::alphabet;
use base64::engine::general_purpose::{GeneralPurpose, NO_PAD};
use jsonwebtoken::{Algorithm, DecodingKey};
use serde_json::{Map, Value, json};
use tokio::sync::Mutex;

use crate::config::{Config, https_or_local, split_url};

pub const METADATA: &str = "/.well-known/oauth-protected-resource";
const LEEWAY: f64 = 60.0;
const KEYS_MAX_AGE: Duration = Duration::from_secs(3600);
/// Tokens with made-up `kid`s cannot keep us fetching.
const UNKNOWN_KID_EVERY: Duration = Duration::from_secs(60);
const RETRY_AFTER_FAILURE: Duration = Duration::from_secs(10);
const MAX_BYTES: usize = 1_000_000;
/// Like Python's decoder, it ignores the unused bits of the last character.
const BASE64URL: GeneralPurpose = GeneralPurpose::new(
    &alphabet::URL_SAFE,
    NO_PAD.with_decode_allow_trailing_bits(true),
);

type Object = Map<String, Value>;

pub enum Refusal {
    /// `401`, `invalid_token`, with the reason.
    Invalid(&'static str),
    /// `403`, `insufficient_scope`, naming the scope.
    MissingScope(String),
    /// No key set, and the issuer did not hand one over: `503`.
    Unreachable,
}

/// The algorithm, and the `kty` and `crv` of the key it needs. Never `none` or HMAC.
fn algorithm(alg: &str) -> Option<(Algorithm, &'static str, Option<&'static str>)> {
    Some(match alg {
        "RS256" => (Algorithm::RS256, "RSA", None),
        "RS384" => (Algorithm::RS384, "RSA", None),
        "RS512" => (Algorithm::RS512, "RSA", None),
        "PS256" => (Algorithm::PS256, "RSA", None),
        "PS384" => (Algorithm::PS384, "RSA", None),
        "PS512" => (Algorithm::PS512, "RSA", None),
        "ES256" => (Algorithm::ES256, "EC", Some("P-256")),
        "ES384" => (Algorithm::ES384, "EC", Some("P-384")),
        _ => return None,
    })
}

pub fn metadata_path(public_url: &str) -> String {
    format!(
        "{METADATA}{}",
        split_url(public_url).2.trim_end_matches('/')
    )
}

pub fn metadata_url(public_url: &str) -> String {
    let (scheme, authority, _) = split_url(public_url);
    format!("{scheme}://{authority}{}", metadata_path(public_url))
}

pub fn metadata(config: &Config) -> Value {
    let mut out = Object::new();
    out.insert("resource".into(), config.public_url.clone().into());
    out.insert("authorization_servers".into(), json!([config.oauth_issuer]));
    if !config.oauth_scopes.is_empty() {
        out.insert("scopes_supported".into(), json!(config.oauth_scopes));
    }
    out.insert("bearer_methods_supported".into(), json!(["header"]));
    out.insert("resource_name".into(), config.name.clone().into());
    Value::Object(out)
}

struct Parsed<'a> {
    header: Object,
    payload: Object,
    signing_input: &'a str,
    signature: &'a str,
}

fn is_base64url(part: &str) -> bool {
    part.bytes()
        .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
}

fn object(part: &str) -> Option<Object> {
    match serde_json::from_slice(&BASE64URL.decode(part).ok()?).ok()? {
        Value::Object(map) => Some(map),
        _ => None,
    }
}

/// Header, payload, signing input and signature, or `malformed token`.
fn parse(token: &str) -> Result<Parsed<'_>, Refusal> {
    let malformed = Refusal::Invalid("malformed token");
    let parts: Vec<&str> = token.split('.').collect();
    let [h, p, s] = parts[..] else {
        return Err(malformed);
    };
    if !parts.iter().all(|part| is_base64url(part)) || BASE64URL.decode(s).is_err() {
        return Err(malformed);
    }
    let (Some(header), Some(payload)) = (object(h), object(p)) else {
        return Err(malformed);
    };
    if !(header.get("kid").is_some_and(Value::is_string)
        && header.get("alg").is_some_and(Value::is_string))
    {
        return Err(malformed);
    }
    Ok(Parsed {
        header,
        payload,
        signing_input: &token[..h.len() + 1 + p.len()],
        signature: s,
    })
}

fn last(scope: &str) -> &str {
    scope.rsplit('/').next().unwrap_or(scope)
}

fn scopes(payload: &Object) -> Vec<&str> {
    let items: Vec<&str> = match payload.get("scp").or_else(|| payload.get("scope")) {
        Some(Value::String(s)) => s.split_whitespace().collect(),
        Some(Value::Array(a)) => a.iter().filter_map(Value::as_str).collect(),
        _ => Vec::new(),
    };
    items.into_iter().map(last).collect()
}

fn fits(key: &Object, kid: &str, alg: &str) -> bool {
    let Some((_, kty, crv)) = algorithm(alg) else {
        return false;
    };
    let field = |name: &str| key.get(name).map(Value::as_str);
    field("kid") == Some(Some(kid))
        && field("kty") == Some(Some(kty))
        && crv.is_none_or(|c| field("crv") == Some(Some(c)))
        && field("alg").is_none_or(|a| a == Some(alg))
        && field("use").is_none_or(|u| u == Some("sig"))
}

fn decoding_key(key: &Object) -> Option<DecodingKey> {
    let text = |name: &str| key.get(name).and_then(Value::as_str);
    match text("kty")? {
        "RSA" => DecodingKey::from_rsa_components(text("n")?, text("e")?).ok(),
        "EC" => DecodingKey::from_ec_components(text("x")?, text("y")?).ok(),
        _ => None,
    }
}

#[derive(Default)]
struct KeyCache {
    keys: Option<Vec<Object>>,
    next_fetch: Option<Instant>,
    unknown_kid_fetched: Option<Instant>,
}

pub struct TokenChecker {
    issuer: String,
    audience: String,
    scopes: Vec<String>,
    client: reqwest::Client,
    keys: Mutex<KeyCache>,
}

impl TokenChecker {
    pub fn new(config: &Config) -> Self {
        let client = reqwest::Client::builder()
            .timeout(Duration::from_secs(10))
            .redirect(reqwest::redirect::Policy::none())
            .build()
            .expect("HTTP client");
        Self {
            issuer: config.oauth_issuer.clone().unwrap_or_default(),
            audience: config.oauth_audience.clone().unwrap_or_default(),
            scopes: config.oauth_scopes.clone(),
            client,
            keys: Mutex::default(),
        }
    }

    /// `Ok` for a token that passes every check (SPEC.md §2.2).
    pub async fn check(&self, token: &str) -> Result<(), Refusal> {
        let Parsed {
            header,
            payload,
            signing_input,
            signature,
        } = parse(token)?;
        let alg = header.get("alg").and_then(Value::as_str).unwrap_or("");
        let Some((algorithm, _, _)) = algorithm(alg) else {
            return Err(Refusal::Invalid("unsupported algorithm"));
        };
        let kid = header.get("kid").and_then(Value::as_str).unwrap_or("");
        let key = self.key(kid, alg).await?;
        let verified = decoding_key(&key).is_some_and(|key| {
            jsonwebtoken::crypto::verify(signature, signing_input.as_bytes(), &key, algorithm)
                .unwrap_or(false)
        });
        if !verified {
            return Err(Refusal::Invalid("bad signature"));
        }
        if payload.get("iss").and_then(Value::as_str) != Some(self.issuer.as_str()) {
            return Err(Refusal::Invalid("wrong issuer"));
        }
        let audience = match payload.get("aud") {
            Some(Value::String(aud)) => *aud == self.audience,
            Some(Value::Array(auds)) => auds.iter().any(|a| a.as_str() == Some(&self.audience)),
            _ => false,
        };
        if !audience {
            return Err(Refusal::Invalid("wrong audience"));
        }
        let now = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_or(0.0, |d| d.as_secs_f64());
        if payload
            .get("exp")
            .and_then(Value::as_f64)
            .is_none_or(|exp| now >= exp + LEEWAY)
        {
            return Err(Refusal::Invalid("expired"));
        }
        if payload
            .get("nbf")
            .is_some_and(|nbf| nbf.as_f64().is_none_or(|nbf| nbf > now + LEEWAY))
        {
            return Err(Refusal::Invalid("not yet valid"));
        }
        let have = scopes(&payload);
        if let Some(missing) = self.scopes.iter().find(|s| !have.contains(&last(s))) {
            return Err(Refusal::MissingScope(missing.clone()));
        }
        Ok(())
    }

    async fn key(&self, kid: &str, alg: &str) -> Result<Object, Refusal> {
        let mut state = self.keys.lock().await;
        let now = Instant::now();
        if state.keys.is_none() || state.next_fetch.is_some_and(|t| now >= t) {
            self.fetch(&mut state).await?;
        }
        let find = |state: &KeyCache| {
            state
                .keys
                .as_ref()
                .and_then(|keys| keys.iter().find(|k| fits(k, kid, alg)).cloned())
        };
        let mut key = find(&state);
        if key.is_none()
            && state
                .unknown_kid_fetched
                .is_none_or(|t| now.duration_since(t) >= UNKNOWN_KID_EVERY)
        {
            state.unknown_kid_fetched = Some(now);
            self.fetch(&mut state).await?;
            key = find(&state);
        }
        key.ok_or(Refusal::Invalid("unknown signing key"))
    }

    /// Fetches the key set through the issuer's discovery document. A failure keeps the old one, if any.
    async fn fetch(&self, state: &mut KeyCache) -> Result<(), Refusal> {
        match self.fetch_keys().await {
            Ok(keys) => {
                state.keys = Some(keys);
                state.next_fetch = Some(Instant::now() + KEYS_MAX_AGE);
                Ok(())
            }
            Err(e) => {
                tracing::error!("fetching the OAuth issuer's signing keys failed: {e:#}");
                state.next_fetch = Some(Instant::now() + RETRY_AFTER_FAILURE);
                if state.keys.is_none() {
                    Err(Refusal::Unreachable)
                } else {
                    Ok(())
                }
            }
        }
    }

    async fn fetch_keys(&self) -> anyhow::Result<Vec<Object>> {
        let discovery_url = format!(
            "{}/.well-known/openid-configuration",
            self.issuer.trim_end_matches('/')
        );
        let discovery = self.get_json(&discovery_url).await?;
        let named = discovery.get("issuer").and_then(Value::as_str);
        if named != Some(self.issuer.as_str()) {
            anyhow::bail!("the discovery document names the issuer {named:?}");
        }
        let Some(jwks_uri) = discovery.get("jwks_uri").and_then(Value::as_str) else {
            anyhow::bail!("the discovery document has no jwks_uri");
        };
        let Some(Value::Array(keys)) = self.get_json(jwks_uri).await?.remove("keys") else {
            anyhow::bail!("the key set has no keys");
        };
        Ok(keys
            .into_iter()
            .filter_map(|k| match k {
                Value::Object(map) => Some(map),
                _ => None,
            })
            .collect())
    }

    async fn get_json(&self, url: &str) -> anyhow::Result<Object> {
        if !https_or_local(url) {
            anyhow::bail!("{url} is not https://");
        }
        let mut response = self.client.get(url).send().await?;
        if response.status() != reqwest::StatusCode::OK {
            anyhow::bail!("{url} answered {}", response.status().as_u16());
        }
        let mut body = Vec::new();
        while let Some(chunk) = response.chunk().await? {
            body.extend_from_slice(&chunk);
            if body.len() > MAX_BYTES {
                anyhow::bail!("{url} sent more than {MAX_BYTES} bytes");
            }
        }
        match serde_json::from_slice(&body)? {
            Value::Object(map) => Ok(map),
            _ => anyhow::bail!("{url} did not send a JSON object"),
        }
    }
}
