//! Read-only Bilibili adapter. Cookies never leave API/passport hosts; no redirects.
use crate::{
    db::now,
    error::{Error, Result, invalid},
    state::App,
};
use futures_util::StreamExt;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::{collections::BTreeMap, time::Duration};

const API: &str = "https://api.bilibili.com";
const PASSPORT: &str = "https://passport.bilibili.com";
const UA: &str = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36";
#[derive(Clone, Default, Serialize, Deserialize)]
pub struct Credential {
    pub sessdata: String,
    pub buvid3: String,
    pub user_id: String,
}
impl Credential {
    fn cookie(&self) -> String {
        [
            ("SESSDATA", &self.sessdata),
            ("buvid3", &self.buvid3),
            ("DedeUserID", &self.user_id),
        ]
        .into_iter()
        .filter(|(_, v)| !v.is_empty() && v.bytes().all(|b| b > 32 && b < 127 && b != b';'))
        .map(|(k, v)| format!("{k}={v}"))
        .collect::<Vec<_>>()
        .join("; ")
    }
}
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
pub struct VideoId {
    pub bvid: String,
    pub page: u32,
}
pub fn parse_video(raw: &str) -> Result<VideoId> {
    let raw = raw.trim();
    let (bvid, page) = if raw.starts_with("BV") && !raw.contains('/') {
        (raw.to_owned(), 1)
    } else {
        let u = url::Url::parse(raw)
            .map_err(|_| invalid("请输入 BV 号或完整的 bilibili.com/video/ 链接"))?;
        if !matches!(u.scheme(), "http" | "https")
            || !matches!(
                u.host_str(),
                Some("www.bilibili.com" | "bilibili.com" | "m.bilibili.com")
            )
            || !u.username().is_empty()
            || u.password().is_some()
            || u.port().is_some()
        {
            return Err(invalid("只支持 B 站完整视频链接，短链接请先在浏览器展开"));
        }
        let bvid = u
            .path()
            .trim_end_matches('/')
            .strip_prefix("/video/")
            .ok_or_else(|| invalid("视频链接格式不正确"))?
            .to_owned();
        let page = u
            .query_pairs()
            .find(|(k, _)| k == "p")
            .map(|(_, v)| v.parse::<u32>())
            .transpose()
            .map_err(|_| invalid("分 P 页码无效"))?
            .unwrap_or(1);
        (bvid, page)
    };
    if bvid.len() != 12
        || !bvid.starts_with("BV")
        || !bvid.bytes().all(|b| b.is_ascii_alphanumeric())
        || !(1..=1000).contains(&page)
    {
        return Err(invalid("BV 号或分 P 页码无效"));
    }
    Ok(VideoId { bvid, page })
}
pub fn video_url(bvid: &str, page: u32) -> String {
    format!("https://www.bilibili.com/video/{bvid}/?p={page}")
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Segment {
    pub start: f64,
    pub end: f64,
    pub text: String,
}
#[derive(Clone, Default)]
pub struct Video {
    pub title: String,
    pub author: String,
    pub duration: i64,
    pub cid: i64,
}
pub struct Captions {
    pub language: String,
    pub segments: Vec<Segment>,
}
pub trait Source: Sync {
    fn search(
        &self,
        keyword: &str,
        days: i64,
    ) -> impl std::future::Future<Output = Result<Vec<VideoId>>> + Send;
    fn info(&self, id: &VideoId) -> impl std::future::Future<Output = Result<Video>> + Send;
    fn captions(
        &self,
        id: &VideoId,
        video: &Video,
    ) -> impl std::future::Future<Output = Result<Option<Captions>>> + Send;
}
pub struct Bili<'a> {
    app: &'a App,
    credential: Credential,
}
impl<'a> Bili<'a> {
    pub fn new(app: &'a App, credential: Credential) -> Self {
        Self { app, credential }
    }
    pub async fn prepare(&mut self) -> Result<()> {
        if self.credential.buvid3.is_empty() {
            let (v, _) = self
                .raw(&format!("{API}/x/frontend/finger/spi"), false, 50_000)
                .await?;
            check(&v)?;
            self.credential.buvid3 = v["data"]["b_3"].as_str().unwrap_or("").to_owned();
        }
        Ok(())
    }
    async fn raw(
        &self,
        url: &str,
        cookies: bool,
        limit: usize,
    ) -> Result<(Value, reqwest::header::HeaderMap)> {
        let client = crate::network::client(self.app, url).await?;
        let mut req = client
            .get(url)
            .header("User-Agent", UA)
            .header("Referer", "https://www.bilibili.com/")
            .timeout(Duration::from_secs(25));
        if cookies {
            req = req.header("Cookie", self.credential.cookie());
        }
        let response = req
            .send()
            .await
            .map_err(|_| Error::Provider("连接 B 站超时或失败，请稍后重试".into()))?;
        if !response.status().is_success() {
            return Err(Error::Provider(format!(
                "B 站返回 HTTP {}，本次停止访问，请稍后重试",
                response.status().as_u16()
            )));
        }
        let headers = response.headers().clone();
        let mut stream = response.bytes_stream();
        let mut bytes = Vec::new();
        while let Some(chunk) = stream.next().await {
            let chunk = chunk.map_err(|_| Error::Provider("B 站响应中断".into()))?;
            if bytes.len() + chunk.len() > limit {
                return Err(invalid("B 站返回内容超过阅读上限"));
            }
            bytes.extend_from_slice(&chunk);
        }
        let data = serde_json::from_slice(&bytes)
            .map_err(|_| Error::Provider("B 站未返回有效数据，请检查网络或稍后重试".into()))?;
        Ok((data, headers))
    }
    async fn api(&self, path: &str, params: Vec<(&str, String)>, signed: bool) -> Result<Value> {
        let mut params: BTreeMap<String, String> =
            params.into_iter().map(|(k, v)| (k.into(), v)).collect();
        if signed {
            let (nav, _) = self
                .raw(&format!("{API}/x/web-interface/nav"), true, 200_000)
                .await?;
            let key = mixin_key(&nav["data"]["wbi_img"])?;
            params.insert("wts".into(), now().to_string());
            params.insert("web_location".into(), "1550101".into());
            let query = encode(&params);
            let signature = format!("{:x}", md5::compute(format!("{query}{key}")));
            params.insert("w_rid".into(), signature);
        }
        let (body, _) = self
            .raw(&format!("{API}{path}?{}", encode(&params)), true, 2_000_000)
            .await?;
        check(&body)?;
        Ok(body["data"].clone())
    }
    pub async fn qr_start(&self) -> Result<(String, String)> {
        let (body, _) = self
            .raw(
                &format!("{PASSPORT}/x/passport-login/web/qrcode/generate?source=main-fe-header"),
                false,
                50_000,
            )
            .await?;
        check(&body)?;
        let key = body["data"]["qrcode_key"]
            .as_str()
            .filter(|s| !s.is_empty() && s.len() < 500)
            .ok_or_else(|| invalid("B 站没有返回登录码"))?;
        let link = body["data"]["url"]
            .as_str()
            .ok_or_else(|| invalid("B 站没有返回二维码"))?;
        let parsed = url::Url::parse(link).map_err(|_| invalid("二维码地址无效"))?;
        if parsed.scheme() != "https"
            || !matches!(
                parsed.host_str(),
                Some("passport.bilibili.com" | "account.bilibili.com")
            )
            || !parsed.username().is_empty()
            || parsed.password().is_some()
            || parsed.port().is_some()
        {
            return Err(invalid("二维码地址不受支持"));
        }
        Ok((key.into(), link.into()))
    }
    pub async fn qr_poll(&self, key: &str) -> Result<(String, Option<Credential>)> {
        let q = url::form_urlencoded::Serializer::new(String::new())
            .append_pair("qrcode_key", key)
            .finish();
        let (body, headers) = self
            .raw(
                &format!("{PASSPORT}/x/passport-login/web/qrcode/poll?{q}"),
                false,
                100_000,
            )
            .await?;
        check(&body)?;
        match body["data"]["code"].as_i64() {
            Some(86101) => Ok(("waiting".into(), None)),
            Some(86090) => Ok(("scanned".into(), None)),
            Some(86038) => Ok(("expired".into(), None)),
            Some(0) => {
                let mut cred = Credential::default();
                for raw in headers.get_all(reqwest::header::SET_COOKIE) {
                    if let Ok(raw) = raw.to_str() {
                        if let Some((k, v)) = raw.split(';').next().unwrap_or("").split_once('=') {
                            match k.trim() {
                                "SESSDATA" => cred.sessdata = v.into(),
                                "DedeUserID" => cred.user_id = v.into(),
                                "buvid3" => cred.buvid3 = v.into(),
                                _ => (),
                            }
                        }
                    }
                }
                if cred.sessdata.is_empty() {
                    return Err(invalid("登录响应缺少凭据，请重新扫码"));
                }
                if let Ok((spi, _)) = self
                    .raw(&format!("{API}/x/frontend/finger/spi"), false, 50_000)
                    .await
                {
                    cred.buvid3 = spi["data"]["b_3"].as_str().unwrap_or("").into();
                }
                Ok(("ok".into(), Some(cred)))
            }
            _ => Err(Error::Provider("B 站登录状态异常，请重新生成二维码".into())),
        }
    }
    pub async fn account(&self) -> Result<String> {
        let v = self.api("/x/web-interface/nav", vec![], false).await?;
        if v["isLogin"] != true {
            return Err(invalid("B 站登录已失效，请重新扫码"));
        }
        Ok(v["uname"]
            .as_str()
            .unwrap_or("已登录用户")
            .chars()
            .take(80)
            .collect())
    }
}
fn check(v: &Value) -> Result<()> {
    match v["code"].as_i64() {
        Some(0) => Ok(()),
        Some(-101) => Err(Error::Provider(
            "B 站要求登录或登录已过期，请扫码连接账号".into(),
        )),
        Some(-352 | -412 | -403) => Err(Error::Provider(
            "B 站暂时限制了访问，请停止重试，稍后再试".into(),
        )),
        Some(-404 | 62002 | 62004) => Err(Error::Provider(
            "视频不存在、已删除或当前账号无权访问".into(),
        )),
        Some(code) => Err(Error::Provider(format!(
            "B 站接口返回错误 {code}，没有取得内容"
        ))),
        None => Err(Error::Provider("B 站响应格式不正确".into())),
    }
}
fn encode(params: &BTreeMap<String, String>) -> String {
    let mut q = url::form_urlencoded::Serializer::new(String::new());
    for (k, v) in params {
        q.append_pair(
            k,
            &v.chars()
                .filter(|c| !"!'()*".contains(*c))
                .collect::<String>(),
        );
    }
    q.finish()
}
fn mixin_key(v: &Value) -> Result<String> {
    // Protocol permutation, shared by all WBI clients; no SDK runtime is embedded.
    const ORDER: [usize; 32] = [
        46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49, 33, 9, 42, 19,
        29, 28, 14, 39, 12, 38, 41, 13,
    ];
    let mut source = String::new();
    for name in ["img_url", "sub_url"] {
        let key = v[name]
            .as_str()
            .unwrap_or("")
            .rsplit('/')
            .next()
            .unwrap_or("")
            .split('.')
            .next()
            .unwrap_or("");
        source.push_str(key);
    }
    if source.len() != 64 || !source.bytes().all(|b| b.is_ascii_hexdigit()) {
        return Err(Error::Provider("B 站签名信息不可用".into()));
    }
    Ok(ORDER
        .into_iter()
        .map(|i| source.as_bytes()[i] as char)
        .collect())
}
pub fn subtitle_url(raw: &str) -> Result<String> {
    let text = if raw.starts_with("//") {
        format!("https:{raw}")
    } else {
        raw.into()
    };
    let u = url::Url::parse(&text).map_err(|_| invalid("字幕地址无效"))?;
    let host = u.host_str().unwrap_or("");
    if u.scheme() != "https"
        || !u.username().is_empty()
        || u.password().is_some()
        || u.port().is_some()
        || !(host == "hdslb.com"
            || host.ends_with(".hdslb.com")
            || host == "bilibili.com"
            || host.ends_with(".bilibili.com"))
    {
        return Err(invalid("字幕必须来自 B 站媒体域名"));
    }
    Ok(text)
}
pub fn parse_captions(v: &Value) -> Result<Vec<Segment>> {
    let body = v["body"]
        .as_array()
        .ok_or_else(|| invalid("字幕格式无效"))?;
    if body.len() > 15000 {
        return Err(invalid("字幕超过 15000 段，请选择较短视频"));
    }
    let mut out = Vec::new();
    let mut bytes = 0;
    let mut last = 0.0;
    for row in body {
        let text = row["content"].as_str().unwrap_or("").trim();
        if text.is_empty() {
            continue;
        }
        let start = row["from"]
            .as_f64()
            .ok_or_else(|| invalid("字幕时间缺失"))?;
        let end = row["to"].as_f64().ok_or_else(|| invalid("字幕时间缺失"))?;
        if !start.is_finite() || !end.is_finite() || start < last || end < start || end > 86400.0 {
            return Err(invalid("字幕时间顺序无效"));
        }
        bytes += text.len();
        if bytes > 200_000 {
            return Err(invalid("字幕超过 200 KB，请选择较短视频"));
        }
        last = start;
        out.push(Segment {
            start,
            end,
            text: text.into(),
        });
    }
    Ok(out)
}
impl Source for Bili<'_> {
    async fn search(&self, keyword: &str, days: i64) -> Result<Vec<VideoId>> {
        let v = self
            .api(
                "/x/web-interface/wbi/search/type",
                vec![
                    ("search_type", "video".into()),
                    ("keyword", keyword.into()),
                    ("order", "pubdate".into()),
                    ("page", "1".into()),
                    ("page_size", "20".into()),
                ],
                true,
            )
            .await?;
        Ok(v["result"]
            .as_array()
            .into_iter()
            .flatten()
            .filter(|r| {
                r["pubdate"]
                    .as_i64()
                    .is_some_and(|n| n >= now() - days * 86400)
            })
            .filter_map(|r| parse_video(r["bvid"].as_str().unwrap_or("")).ok())
            .take(20)
            .collect())
    }
    async fn info(&self, id: &VideoId) -> Result<Video> {
        let v = self
            .api(
                "/x/web-interface/view",
                vec![("bvid", id.bvid.clone())],
                false,
            )
            .await?;
        let part = v["pages"]
            .as_array()
            .and_then(|p| p.get((id.page - 1) as usize))
            .ok_or_else(|| invalid("视频没有这个分 P"))?;
        let title = if id.page > 1 {
            format!(
                "{} · P{} {}",
                v["title"].as_str().unwrap_or(&id.bvid),
                id.page,
                part["part"].as_str().unwrap_or("")
            )
        } else {
            v["title"].as_str().unwrap_or(&id.bvid).into()
        };
        Ok(Video {
            title: title.chars().take(300).collect(),
            author: v["owner"]["name"]
                .as_str()
                .unwrap_or("")
                .chars()
                .take(80)
                .collect(),
            duration: part["duration"].as_i64().unwrap_or(0),
            cid: part["cid"]
                .as_i64()
                .filter(|n| *n > 0)
                .ok_or_else(|| invalid("视频缺少 CID"))?,
        })
    }
    async fn captions(&self, id: &VideoId, video: &Video) -> Result<Option<Captions>> {
        let v = self
            .api(
                "/x/player/wbi/v2",
                vec![("bvid", id.bvid.clone()), ("cid", video.cid.to_string())],
                true,
            )
            .await?;
        let subs = v["subtitle"]["subtitles"].as_array();
        let target = subs.and_then(|s| {
            s.iter()
                .min_by_key(|v| match v["lan"].as_str().unwrap_or("") {
                    "zh-CN" | "zh-Hans" | "zh" => 0,
                    "ai-zh" => 1,
                    _ => 2,
                })
        });
        let Some(target) = target else {
            return Ok(None);
        };
        let url = subtitle_url(target["subtitle_url"].as_str().unwrap_or(""))?;
        let (body, _) = self.raw(&url, false, 2_000_000).await?;
        let segments = parse_captions(&body)?;
        if segments.is_empty() {
            return Ok(None);
        }
        Ok(Some(Captions {
            language: target["lan_doc"]
                .as_str()
                .unwrap_or("字幕")
                .chars()
                .take(40)
                .collect(),
            segments,
        }))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    #[test]
    fn identifiers_and_media_origins() {
        assert_eq!(
            parse_video("https://www.bilibili.com/video/BV1xx411c7mD/?p=2&tracking=x")
                .unwrap()
                .page,
            2
        );
        for bad in [
            "https://evil.test/video/BV1xx411c7mD",
            "https://www.bilibili.com@127.0.0.1/video/BV1xx411c7mD",
            "BV../evil",
            "https://bilibili.com/video/BV1xx411c7mD/?p=0",
        ] {
            assert!(parse_video(bad).is_err());
        }
        assert!(subtitle_url("//aisubtitle.hdslb.com/bfs/subtitle/test.json").is_ok());
        assert!(subtitle_url("https://hdslb.com.evil.test/x").is_err());
        assert!(subtitle_url("http://127.0.0.1/x").is_err());
    }
    #[test]
    fn captions_reject_corrupt_timeline() {
        assert!(parse_captions(&json!({"body":[{"from":4,"to":2,"content":"text"}]})).is_err());
        assert_eq!(
            parse_captions(&json!({"body":[{"from":0,"to":2,"content":" text "}]})).unwrap()[0]
                .text,
            "text"
        );
    }
}
