# syntax=docker/dockerfile:1.7
# LexAI web image: the built UI on unprivileged nginx, proxying /api to the API.
#   docker build -t lexai-frontend frontend

ARG NODE_IMAGE=node:22.23.5-alpine
ARG NGINX_IMAGE=nginxinc/nginx-unprivileged:1.31.9-alpine

FROM ${NODE_IMAGE} AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci --no-audit --no-fund
COPY . .
RUN npm run build

FROM ${NGINX_IMAGE} AS runtime
LABEL org.opencontainers.image.title="lexai-frontend" \
      org.opencontainers.image.source="https://github.com/allu0786ansari/LexAI"
COPY nginx/default.conf.template /etc/nginx/templates/default.conf.template
COPY --from=build /app/dist /usr/share/nginx/html
# Where the API is, and Docker's DNS (re-resolved per request, so API replicas share the load).
ENV LEXAI_API_UPSTREAM=api:8000 \
    NGINX_RESOLVER=127.0.0.11
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=3s --retries=3 CMD wget -q -O /dev/null http://127.0.0.1:8080/healthz || exit 1
