# Multi-stage Dockerfile for Web Interface

FROM node:20-slim as builder

WORKDIR /app

# Copy package files. package-lock.json is optional by project preference.
COPY package.json package-lock.json* ./
RUN if [ -f package-lock.json ]; then npm ci; else npm install; fi

# Copy source
COPY . .

# Build
RUN npm run build

# Production stage with nginx
FROM nginx:alpine

# Copy built assets
COPY --from=builder /app/dist /usr/share/nginx/html

# Runtime defaults. Override these environment variables when starting the container.
# Non-root nginx cannot bind to port 80, so the container listens on 8080 by default.
ENV NGINX_PORT=8080
ENV NGINX_SERVER_NAME=localhost
ENV API_PROXY_PASS=http://backend:8000

# Copy nginx config template. The official nginx image renders templates with envsubst.
COPY nginx.conf /etc/nginx/templates/default.conf.template

RUN touch /var/run/nginx.pid \
    && chown -R nginx:nginx \
        /var/cache/nginx \
        /var/run/nginx.pid \
        /usr/share/nginx/html \
        /etc/nginx/conf.d \
        /etc/nginx/templates

EXPOSE 8080

USER nginx

CMD ["nginx", "-g", "daemon off;"]
