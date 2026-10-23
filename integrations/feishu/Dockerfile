FROM node:24-bookworm-slim
WORKDIR /app
COPY package*.json ./
RUN npm ci --omit=dev --ignore-scripts --no-fund && npm cache clean --force
COPY bridge.mjs ./
RUN mkdir -p /data/inbox && chown -R node:node /data
USER node
ENV NODE_ENV=production
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s CMD ["node", "-e", "const f=require('fs');process.exit(Date.now()-Number(f.readFileSync('/tmp/lingxi-feishu-health','utf8'))<60000?0:1)"]
CMD ["node", "bridge.mjs"]
