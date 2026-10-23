//! Durable automatic-image pacing shared by normal replies and outreach.
//! Group counters always live in the group DB, including replies to the owner.
use super::store;
use crate::{auth::User, error::Result, state::App};
use sqlx::{SqliteConnection, SqlitePool};

const COOLDOWN: i64 = 5 * 60;
const REPEAT_WINDOW: i64 = 30 * 60;
const HOURLY_LIMIT: i64 = 3;
const TEXT_TURNS: i64 = 2;

async fn limits_enabled(app: &App, user: &User, group: Option<&str>) -> Result<bool> {
    let Some(group) = group else { return Ok(true) };
    Ok(super::config(app, user)
        .await?
        .group_settings
        .get(group)
        .is_none_or(|settings| settings.sticker_limits_enabled))
}

async fn pool<'a>(app: &'a App, group: Option<&str>) -> Result<&'a SqlitePool> {
    if group.is_some() {
        store::pool(app).await
    } else {
        Ok(&app.pool)
    }
}

async fn eligible(conn: &mut SqliteConnection, user: &User, scope: &str, at: i64) -> Result<bool> {
    let latest: Option<(i64, i64)> = sqlx::query_as(
        "SELECT image_at,image_turn FROM sticker_reply_turns WHERE user_id=? AND scope=? AND image_at IS NOT NULL ORDER BY image_at DESC,id DESC LIMIT 1",
    ).bind(&user.id).bind(scope).fetch_optional(&mut *conn).await?;
    if let Some((image_at, marker)) = latest {
        if at < image_at + COOLDOWN {
            return Ok(false);
        }
        let turns: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM sticker_reply_turns WHERE user_id=? AND scope=? AND id>? AND image_at IS NULL")
            .bind(&user.id).bind(scope).bind(marker).fetch_one(&mut *conn).await?;
        if turns < TEXT_TURNS {
            return Ok(false);
        }
    }
    let hourly: i64 = sqlx::query_scalar(
        "SELECT COUNT(*) FROM sticker_reply_turns WHERE user_id=? AND scope=? AND image_at>?",
    )
    .bind(&user.id)
    .bind(scope)
    .bind(at - 3600)
    .fetch_one(&mut *conn)
    .await?;
    Ok(hourly < HOURLY_LIMIT)
}

/// Check before model selection; suppressed replies incur no selection request.
pub async fn candidates(
    app: &App,
    user: &User,
    group: Option<&str>,
    at: i64,
) -> Result<Option<Vec<String>>> {
    if !limits_enabled(app, user, group).await? {
        return Ok(Some(Vec::new()));
    }
    let mut conn = pool(app, group).await?.acquire().await?;
    if !eligible(&mut conn, user, group.unwrap_or(""), at).await? {
        return Ok(None);
    }
    let recent = sqlx::query_scalar("SELECT sticker_id FROM sticker_reply_turns WHERE user_id=? AND scope=? AND image_at>? AND sticker_id IS NOT NULL")
        .bind(&user.id).bind(group.unwrap_or("")).bind(at-REPEAT_WINDOW).fetch_all(&mut *conn).await?;
    Ok(Some(recent))
}

/// One successful text reply is one turn, regardless of how many parts it has.
pub async fn record_text(
    app: &App,
    user: &User,
    group: Option<&str>,
    key: &str,
    at: i64,
) -> Result<()> {
    let pool = pool(app, group).await?;
    sqlx::query("INSERT INTO sticker_reply_turns(user_id,scope,delivery_key,text_at) VALUES(?,?,?,?) ON CONFLICT DO NOTHING")
        .bind(&user.id).bind(group.unwrap_or("")).bind(key).bind(at).execute(pool).await?;
    sqlx::query(
        "DELETE FROM sticker_reply_turns WHERE text_at<? AND (image_at IS NULL OR image_at<?)",
    )
    .bind(at - 7 * 86400)
    .bind(at - 7 * 86400)
    .execute(pool)
    .await?;
    Ok(())
}

/// Reserve immediately before the external send. A lost acknowledgement still
/// consumes the allowance: neither retry nor a different delivery may bypass it.
pub async fn claim(
    app: &App,
    user: &User,
    group: Option<&str>,
    key: &str,
    sticker: &str,
    at: i64,
) -> Result<bool> {
    let limited = limits_enabled(app, user, group).await?;
    let scope = group.unwrap_or("");
    let mut tx = pool(app, group).await?.begin().await?;
    // Acquire SQLite's writer lock before checking limits across worker processes.
    let exists=sqlx::query("UPDATE sticker_reply_turns SET text_at=text_at WHERE user_id=? AND scope=? AND delivery_key=? AND image_at IS NULL")
        .bind(&user.id).bind(scope).bind(key).execute(&mut *tx).await?.rows_affected();
    if exists != 1 || (limited && !eligible(&mut tx, user, scope, at).await?) {
        return Ok(false);
    }
    let repeated: bool=sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM sticker_reply_turns WHERE user_id=? AND scope=? AND sticker_id=? AND image_at>?)")
        .bind(&user.id).bind(scope).bind(sticker).bind(at-REPEAT_WINDOW).fetch_one(&mut *tx).await?;
    if limited && repeated {
        return Ok(false);
    }
    sqlx::query("UPDATE sticker_reply_turns SET sticker_id=?,image_at=?,image_turn=(SELECT COALESCE(MAX(id),0) FROM sticker_reply_turns WHERE user_id=? AND scope=?) WHERE user_id=? AND scope=? AND delivery_key=?")
        .bind(sticker).bind(at).bind(&user.id).bind(scope).bind(&user.id).bind(scope).bind(key).execute(&mut *tx).await?;
    tx.commit().await?;
    Ok(true)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[tokio::test]
    async fn cooldown_repeat_window_hourly_cap_and_tenant_isolation_use_durable_attempts() {
        let dir = tempfile::tempdir().unwrap();
        let app = App::new(
            crate::db::connect(&dir.path().join("main.db"))
                .await
                .unwrap(),
            crate::db::Secrets::from_key(&[3; 32]),
            dir.path().into(),
            true,
        )
        .unwrap();
        let user = crate::auth::create_user(&app, "pacing", "test-pacing-password".into(), false)
            .await
            .unwrap();
        record_text(&app, &user, None, "first", 10_000)
            .await
            .unwrap();
        assert!(
            claim(&app, &user, None, "first", "a", 10_000)
                .await
                .unwrap()
        );
        assert!(
            !claim(&app, &user, None, "first", "a", 10_000)
                .await
                .unwrap()
        );
        record_text(&app, &user, None, "one", 10_050).await.unwrap();
        // Duplicate notification/text parts do not count as another turn.
        record_text(&app, &user, None, "one", 10_051).await.unwrap();
        assert!(
            candidates(&app, &user, None, 10_300)
                .await
                .unwrap()
                .is_none()
        );
        record_text(&app, &user, None, "two", 10_060).await.unwrap();
        assert!(
            candidates(&app, &user, None, 10_299)
                .await
                .unwrap()
                .is_none()
        );
        assert_eq!(
            candidates(&app, &user, None, 10_300)
                .await
                .unwrap()
                .unwrap(),
            vec!["a"]
        );
        record_text(&app, &user, None, "second", 10_300)
            .await
            .unwrap();
        assert!(
            !claim(&app, &user, None, "second", "a", 10_300)
                .await
                .unwrap()
        );
        assert!(
            claim(&app, &user, None, "second", "b", 10_300)
                .await
                .unwrap()
        );
        for (key, at) in [("three", 10_350), ("four", 10_360), ("third", 10_600)] {
            record_text(&app, &user, None, key, at).await.unwrap();
        }
        assert!(
            claim(&app, &user, None, "third", "c", 10_600)
                .await
                .unwrap()
        );
        for (key, at) in [("five", 10_650), ("six", 10_660)] {
            record_text(&app, &user, None, key, at).await.unwrap();
        }
        assert!(
            candidates(&app, &user, None, 10_900)
                .await
                .unwrap()
                .is_none()
        );
        assert!(
            candidates(&app, &user, None, 13_600)
                .await
                .unwrap()
                .unwrap()
                .is_empty()
        );
        record_text(&app, &user, None, "later", 13_600)
            .await
            .unwrap();
        assert!(
            claim(&app, &user, None, "later", "a", 13_600)
                .await
                .unwrap()
        );
        let other = crate::auth::create_user(&app, "other", "test-pacing-password".into(), false)
            .await
            .unwrap();
        assert!(
            candidates(&app, &other, None, 13_600)
                .await
                .unwrap()
                .is_some()
        );
    }
}
