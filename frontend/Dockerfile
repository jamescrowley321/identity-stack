FROM node:22-alpine AS build
WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm install
COPY . .
ARG VITE_DESCOPE_PROJECT_ID
# VITE_API_BASE_URL is the absolute base URL the browser uses to call
# the API. Empty string (the default) means the frontend uses relative
# `/api/...` URLs and nginx's `/api/` proxy forwards them to the backend
# (standalone mode). In gateway mode the docker-compose.gateway.yml
# override sets this to http://localhost:8080 so the browser hits Tyk
# directly. Vite inlines this value at build time as a string literal,
# so switching modes requires a rebuild — `make dev-gateway` and CI's
# `make test-integration-gateway` both pass `--build` for that reason.
ARG VITE_API_BASE_URL=""
RUN npm run build

FROM nginxinc/nginx-unprivileged:alpine
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 8080
