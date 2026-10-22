use crate::{
    db::{digest, id, now},
    error::{Error, Result, invalid},
    state::App,
};
use argon2::{
    Argon2, PasswordHash, PasswordHasher, PasswordVerifier,
    password_hash::{SaltString, rand_core::OsRng},
};
use axum::{
    Json,
    extract::{FromRequestParts, State},
    http::request::Parts,
};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

#[derive(Clone, Debug, Serialize, sqlx::FromRow)]
pub struct User {
    pub id: String,
    pub username: String,
    pub timezone: String,
    pub is_admin: bool,
}
impl FromRequestParts<App> for User {
    type Rejection = Error;
    async fn from_request_parts(parts: &mut Parts, state: &App) -> Result<Self> {
        let token = parts
            .headers
            .get("authorization")
            .and_then(|v| v.to_str().ok())
            .and_then(|v| v.strip_prefix("Bearer "))
            .filter(|v| !v.is_empty() && v.len() < 512)
            .ok_or(Error::Unauthorized)?;
        resolve(state, token).await
    }
}
pub async fn resolve(app: &App, token: &str) -> Result<User> {
    sqlx::query_as::<_,User>("SELECT u.id,u.username,u.timezone,u.is_admin FROM users u JOIN tokens t ON t.user_id=u.id WHERE t.hash=? AND t.expires_at>? AND u.disabled=0")
        .bind(digest(token)).bind(now()).fetch_optional(&app.pool).await?.ok_or(Error::Unauthorized)
}
pub async fn user(app: &App, id: &str) -> Result<User> {
    sqlx::query_as::<_, User>(
        "SELECT id,username,timezone,is_admin FROM users WHERE id=? AND disabled=0",
    )
    .bind(id)
    .fetch_optional(&app.pool)
    .await?
    .ok_or(Error::NotFound)
}
pub async fn hash_password(app: &App, password: String) -> Result<String> {
    if password.chars().count() < 12 || password.len() > 1024 {
        return Err(invalid("密码长度须为 12–1024 字节"));
    }
    let permit = app
        .password_workers
        .clone()
        .try_acquire_owned()
        .map_err(|_| Error::Busy)?;
    tokio::task::spawn_blocking(move || {
        let _permit = permit;
        Argon2::default()
            .hash_password(password.as_bytes(), &SaltString::generate(&mut OsRng))
            .map(|v| v.to_string())
            .map_err(|_| Error::Internal(anyhow::anyhow!("password hashing failed")))
    })
    .await
    .map_err(|e| Error::Internal(e.into()))?
}
pub async fn create_user(
    app: &App,
    username: &str,
    password: String,
    is_admin: bool,
) -> Result<User> {
    let username = username.trim();
    if username.is_empty() || username.chars().count() > 64 {
        return Err(invalid("用户名长度须为 1–64 字符"));
    }
    let hash = hash_password(app, password).await?;
    let uid = id();
    let result = sqlx::query(
        "INSERT INTO users(id,username,password_hash,is_admin,created_at) VALUES(?,?,?,?,?)",
    )
    .bind(&uid)
    .bind(username)
    .bind(hash)
    .bind(is_admin)
    .bind(now())
    .execute(&app.pool)
    .await;
    if let Err(sqlx::Error::Database(e)) = &result {
        if e.is_unique_violation() {
            return Err(Error::Conflict("用户名已存在".into()));
        }
    }
    result?;
    user(app, &uid).await
}
pub async fn issue_token(app: &App, user: &User, name: &str, seconds: i64) -> Result<String> {
    use rand::RngCore;
    let mut bytes = [0u8; 32];
    rand::rngs::OsRng.fill_bytes(&mut bytes);
    use base64::Engine;
    let token = base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(bytes);
    sqlx::query("INSERT INTO tokens(hash,user_id,name,expires_at,created_at) VALUES(?,?,?,?,?)")
        .bind(digest(&token))
        .bind(&user.id)
        .bind(name)
        .bind(now() + seconds)
        .bind(now())
        .execute(&app.pool)
        .await?;
    Ok(token)
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Credentials {
    pub username: String,
    pub password: String,
}
pub async fn login(State(app): State<App>, Json(body): Json<Credentials>) -> Result<Json<Value>> {
    if body.username.len() > 256 || body.password.len() > 1024 {
        return Err(Error::Unauthorized);
    }
    if !app.allow_login(&body.username.to_lowercase()) {
        return Err(Error::Busy);
    }
    let permit = app
        .password_workers
        .clone()
        .try_acquire_owned()
        .map_err(|_| Error::Busy)?;
    let row: Option<(String, String)> =
        sqlx::query_as("SELECT id,password_hash FROM users WHERE username=? AND disabled=0")
            .bind(body.username.trim())
            .fetch_optional(&app.pool)
            .await?;
    let Some((uid, hash)) = row else {
        return Err(Error::Unauthorized);
    };
    let valid = tokio::task::spawn_blocking(move || {
        let _permit = permit;
        PasswordHash::new(&hash)
            .map(|hash| {
                Argon2::default()
                    .verify_password(body.password.as_bytes(), &hash)
                    .is_ok()
            })
            .unwrap_or(false)
    })
    .await
    .map_err(|e| Error::Internal(e.into()))?;
    if !valid {
        return Err(Error::Unauthorized);
    }
    let user = user(&app, &uid).await?;
    let token = issue_token(&app, &user, "web", 86400).await?;
    Ok(Json(
        json!({"ok":true,"data":{"user":user,"token":token,"expires_in":86400}}),
    ))
}
pub async fn logout(
    State(app): State<App>,
    _user: User,
    headers: axum::http::HeaderMap,
) -> Result<Json<Value>> {
    if let Some(token) = headers
        .get("authorization")
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.strip_prefix("Bearer "))
    {
        sqlx::query("DELETE FROM tokens WHERE hash=?")
            .bind(digest(token))
            .execute(&app.pool)
            .await?;
    }
    Ok(Json(json!({"ok":true})))
}
pub async fn me(user: User) -> Json<Value> {
    Json(json!({"ok":true,"data":user}))
}
