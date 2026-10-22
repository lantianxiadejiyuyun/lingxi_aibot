use crate::{
    error::{Error, Result, invalid},
    state::App,
};
use std::{
    net::{IpAddr, SocketAddr},
    time::Duration,
};

fn is_public(ip: IpAddr) -> bool {
    match ip {
        IpAddr::V4(ip) => {
            !(ip.is_private()
                || ip.is_loopback()
                || ip.is_link_local()
                || ip.is_multicast()
                || ip.is_unspecified()
                || ip.is_broadcast()
                || ip.is_documentation()
                || ip.octets()[0] == 0
                || ip.octets()[0] >= 240
                || (ip.octets()[0] == 100 && (64..=127).contains(&ip.octets()[1]))
                || ip.octets()[0] == 198 && (ip.octets()[1] == 18 || ip.octets()[1] == 19))
        }
        IpAddr::V6(ip) => {
            if let Some(v4) = ip.to_ipv4_mapped() {
                is_public(IpAddr::V4(v4))
            } else {
                !ip.is_loopback()
                    && !ip.is_unspecified()
                    && !ip.is_multicast()
                    && (ip.segments()[0] & 0xe000) == 0x2000
                    && !(ip.segments()[0] == 0x2001 && ip.segments()[1] == 0xdb8)
            }
        }
    }
}
pub async fn validate_url(app: &App, text: &str) -> Result<(url::Url, Vec<SocketAddr>)> {
    let url = url::Url::parse(text).map_err(|_| invalid("URL 无效"))?;
    if !["http", "https"].contains(&url.scheme())
        || !url.username().is_empty()
        || url.password().is_some()
        || url.fragment().is_some()
    {
        return Err(invalid("只支持不含凭据的 HTTP/HTTPS URL"));
    }
    let host = url.host_str().ok_or_else(|| invalid("URL 缺少主机"))?;
    let host = host.trim_start_matches('[').trim_end_matches(']');
    let port = url
        .port_or_known_default()
        .ok_or_else(|| invalid("URL 端口无效"))?;
    let addresses = tokio::time::timeout(
        Duration::from_secs(5),
        tokio::net::lookup_host((host, port)),
    )
    .await
    .map_err(|_| Error::Provider("域名解析超时".into()))?
    .map_err(|_| invalid("无法解析服务地址"))?
    .collect::<Vec<_>>();
    if addresses.is_empty()
        || (!app.allow_private_network && addresses.iter().any(|a| !is_public(a.ip())))
    {
        return Err(invalid(
            "私网服务需要由管理员启用 LINGXI_ALLOW_PRIVATE_NETWORK",
        ));
    }
    Ok((url, addresses))
}
/// Pin validated DNS answers for this connection; disable redirects and environment proxies.
pub async fn client(app: &App, url: &str) -> Result<reqwest::Client> {
    let (url, addresses) = validate_url(app, url).await?;
    reqwest::Client::builder()
        .connect_timeout(Duration::from_secs(10))
        .timeout(Duration::from_secs(180))
        .redirect(reqwest::redirect::Policy::none())
        .no_proxy()
        .resolve_to_addrs(url.host_str().unwrap(), &addresses)
        .build()
        .map_err(|e| Error::Internal(e.into()))
}
