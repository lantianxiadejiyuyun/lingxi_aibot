FROM node:24-bookworm-slim AS frontend
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci
COPY web ./
RUN npm run build

FROM rust:1.98-bookworm AS build
WORKDIR /build
COPY Cargo.toml Cargo.lock ./
COPY backend ./backend
COPY --from=frontend /web/dist ./web/dist
RUN --mount=type=cache,target=/usr/local/cargo/registry \
    --mount=type=cache,target=/build/target \
    cargo build --locked --release --bin lingxi && cp target/release/lingxi /build/lingxi

FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -m -u 1000 lingxi && mkdir -p /data && chown lingxi:lingxi /data
COPY --from=build /build/lingxi /usr/local/bin/lingxi
ENV LINGXI_DATA_DIR=/data LINGXI_BIND=0.0.0.0:8000 RUST_LOG=lingxi=info,tower_http=info
USER lingxi
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s CMD curl -fsS http://127.0.0.1:8000/readyz || exit 1
ENTRYPOINT ["lingxi"]
CMD ["serve"]
