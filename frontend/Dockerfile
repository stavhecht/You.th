# The production site: the built app, served by Caddy, which also terminates
# TLS and proxies /api to the backend (ops/Caddyfile). Build from the repo root:
#   docker compose -f docker-compose.prod.yml build web
FROM node:22-alpine AS build
WORKDIR /app
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM caddy:2-alpine
COPY ops/Caddyfile /etc/caddy/Caddyfile
COPY --from=build /app/dist /srv
