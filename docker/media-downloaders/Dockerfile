# Official Debian packages; no third-party installation script or image wrapper.
FROM debian:trixie-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends aria2 qbittorrent-nox ca-certificates iptables util-linux \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -g 1000 media \
    && useradd -u 1000 -g 1000 -M -d /config media \
    && mkdir -p /config /media
COPY entrypoint.sh /usr/local/bin/lingxi-media-entrypoint
RUN chmod 0755 /usr/local/bin/lingxi-media-entrypoint
ENV HOME=/config XDG_CONFIG_HOME=/config XDG_CACHE_HOME=/tmp/cache
ENTRYPOINT ["/usr/local/bin/lingxi-media-entrypoint"]
CMD ["aria2"]
