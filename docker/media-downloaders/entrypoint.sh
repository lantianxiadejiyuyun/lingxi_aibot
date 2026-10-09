#!/bin/sh
# Fail closed: a downloader never starts unless the network guard was installed.
set -eu
umask 077

iptables -N LX-MEDIA-EGRESS 2>/dev/null || true
iptables -F LX-MEDIA-EGRESS
iptables -A LX-MEDIA-EGRESS -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
# Docker's embedded DNS; arbitrary loopback HTTP remains blocked.
iptables -A LX-MEDIA-EGRESS -p udp -m conntrack --ctorigdst 127.0.0.11 --ctorigdstport 53 -j ACCEPT
iptables -A LX-MEDIA-EGRESS -p tcp -m conntrack --ctorigdst 127.0.0.11 --ctorigdstport 53 -j ACCEPT
for destination in 0.0.0.0/8 10.0.0.0/8 100.64.0.0/10 127.0.0.0/8 \
    169.254.0.0/16 172.16.0.0/12 192.0.0.0/24 192.0.2.0/24 \
    192.168.0.0/16 198.18.0.0/15 198.51.100.0/24 203.0.113.0/24 \
    224.0.0.0/4 240.0.0.0/4; do
    iptables -A LX-MEDIA-EGRESS -d "$destination" -j REJECT
done
iptables -A LX-MEDIA-EGRESS -j RETURN
iptables -C OUTPUT -j LX-MEDIA-EGRESS 2>/dev/null || iptables -I OUTPUT 1 -j LX-MEDIA-EGRESS
# Compose disables IPv6. Also reject IPv6 output if the kernel supports it.
if [ "$(cat /proc/sys/net/ipv6/conf/all/disable_ipv6 2>/dev/null || printf '0')" != "1" ]; then
    ip6tables -P OUTPUT DROP
fi

mkdir -p /config/aria2 /config/qbittorrent
chown 1000:1000 /config /config/aria2 /config/qbittorrent

case "${1:-}" in
    aria2)
        secret_file=/run/secrets/aria2_secret
        [ -f "$secret_file" ] || { printf '%s\n' 'Missing aria2 secret file.' >&2; exit 1; }
        secret=$(cat "$secret_file")
        case "$secret" in
            *[!A-Za-z0-9_=-]*|'') printf '%s\n' 'aria2 secret must be a single ASCII token.' >&2; exit 1 ;;
        esac
        [ "${#secret}" -ge 32 ] || { printf '%s\n' 'aria2 secret must contain at least 32 characters.' >&2; exit 1; }
        touch /config/aria2/session
        {
            printf '%s\n' 'enable-rpc=true' 'rpc-listen-all=true' 'rpc-listen-port=6800'
            printf 'rpc-secret=%s\n' "$secret"
            printf '%s\n' 'dir=/media' 'input-file=/config/aria2/session' \
                'save-session=/config/aria2/session' 'save-session-interval=30' \
                'force-save=true' 'continue=true' 'max-tries=1' \
                'max-download-result=10000' 'allow-overwrite=false' \
                'auto-file-renaming=false' 'follow-torrent=false' 'follow-metalink=false' \
                'enable-dht=false' 'enable-dht6=false' 'enable-peer-exchange=false' \
                'rpc-allow-origin-all=false' 'log-level=warn' 'console-log-level=warn'
        } > /config/aria2/aria2.conf
        unset secret
        chown 1000:1000 /config/aria2/session /config/aria2/aria2.conf
        exec setpriv --reuid=1000 --regid=1000 --init-groups --bounding-set=-all \
            --inh-caps=-all --ambient-caps=-all --no-new-privs \
            aria2c --conf-path=/config/aria2/aria2.conf
        ;;
    qbittorrent)
        # Temporary first-login password is printed by qB itself. Change it in
        # the local admin UI before configuring this connection in Lingxi.
        exec setpriv --reuid=1000 --regid=1000 --init-groups --bounding-set=-all \
            --inh-caps=-all --ambient-caps=-all --no-new-privs \
            qbittorrent-nox --confirm-legal-notice --profile=/config/qbittorrent --webui-port=8080
        ;;
    *) printf '%s\n' 'Expected aria2 or qbittorrent.' >&2; exit 1 ;;
esac
