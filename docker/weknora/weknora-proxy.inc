# Shared reverse-proxy contract for the WeKnora public gateway.
# Mounted at /etc/nginx/weknora-proxy.inc and included by both the HTTP and
# HTTPS server blocks in gateway.conf.

proxy_http_version 1.1;
proxy_set_header Host $http_host;
proxy_set_header X-Real-IP $remote_addr;
proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
proxy_set_header X-Forwarded-Proto $scheme;
proxy_set_header Upgrade $http_upgrade;
proxy_set_header Connection $connection_upgrade;

# WeKnora streams chat/agent responses (SSE) and uses WebSockets for sandbox
# terminals/desktop sessions. Keep the same long-lived, unbuffered contract the
# upstream UI applies to /api/.
proxy_connect_timeout 30s;
proxy_read_timeout 3600s;
proxy_send_timeout 3600s;
proxy_buffering off;
proxy_cache off;
proxy_request_buffering off;
