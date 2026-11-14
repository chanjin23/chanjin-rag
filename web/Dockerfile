# web 이미지: React 빌드 → Nginx 정적 서빙 + /api 프록시 (TRD 3.2, 11장).
# 빌드 컨텍스트는 저장소 루트(deploy/nginx.conf를 함께 넣기 위해).
# 베이스 이미지는 모두 멀티 아키텍처(amd64·arm64).
FROM node:24-bookworm-slim AS build
WORKDIR /web
ENV COREPACK_ENABLE_DOWNLOAD_PROMPT=0
# 로컬과 같은 pnpm 판(lockfile v9)
RUN corepack enable && corepack install -g pnpm@12.18.0
COPY web/package.json web/pnpm-lock.yaml ./
RUN corepack pnpm install --frozen-lockfile
COPY web/ ./
RUN corepack pnpm build

FROM nginx:1.27-alpine
COPY deploy/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /web/dist /usr/share/nginx/html
EXPOSE 8080
