# The frontend as a static site: Node builds it, an unprivileged nginx serves the files.
# Build from the repo root:  docker compose --profile app build frontend

# ------------------------------------------------------------------ build
FROM node:22-alpine AS build
WORKDIR /app

# Where the browser will reach the API. Vite builds it into the JavaScript, so it is fixed
# per image. Public, never a secret.
ARG VITE_API_BASE_URL=http://localhost:8000
ENV VITE_API_BASE_URL=${VITE_API_BASE_URL}

# Dependencies first: this layer is reused until package.json or the lockfile changes.
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY . .
RUN npm run build

# ------------------------------------------------------------------ serve
# nginx that runs as a non-root user and listens on 8080.
FROM nginxinc/nginx-unprivileged:1.28-alpine AS runtime

# The API's origin, for the Content-Security-Policy's connect-src (filled in at startup
# by the image's template step, from the API_ORIGIN environment variable).
ENV API_ORIGIN=http://localhost:8000

COPY nginx.conf.template /etc/nginx/templates/default.conf.template
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 8080
