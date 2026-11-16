    location /api/ {
        # 后端单文件上限为 200MB；为 multipart 边界和请求头预留余量，
        # 同时让大文件直接流向后端，避免 Nginx 先完整落盘再转发。
        client_max_body_size 210m;
        client_body_timeout 300s;
        proxy_request_buffering off;
        proxy_pass http://backend:8000/api/;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        # $host 不含端口：文件夹同步脚本等按请求地址自推导下发内容的端点
        # 依赖带端口的原始 Host（$http_host），否则推导出的公网地址缺端口。
        proxy_set_header X-Forwarded-Host $http_host;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 300s;
    }

    # 外部 n8n 实例回调平台的接口内部代理（Bearer 服务令牌鉴权）。
    # API-Hub 的 MCP 端点已退役，不再整体放行 /api-hub/ 前缀。
    location /api-hub/internal/ {
        proxy_pass http://backend:8000/api-hub/internal/;
        proxy_http_version 1.1;
        proxy_read_timeout 300s;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # API-Hub MCP 开放端点已退役；显式 404，避免回落到 SPA 入口页。
    location /api-hub/mcp {
        return 404;
    }

    # 通用 /mcp 端点与平台 MinIO MCP 均已退役；显式 404，避免回落到 SPA 入口页。
    location = /mcp {
        return 404;
    }

    location /mcp/ {
        return 404;
    }

    # 普通调用方使用的接口转发入口。公开请求不经过平台 JWT，
    # 每次调用由后端使用接口专属调用凭证鉴权。
    location /proxy/ {
        proxy_pass http://backend:8000/proxy/;
        proxy_http_version 1.1;
        proxy_buffering off;
        proxy_request_buffering off;
        proxy_read_timeout 300s;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # ===== Grafana 监控看板（/grafana/ 同源子路径鉴权代理）=====
    # auth_request 子请求：带原始 Cookie 到 backend 的 grafana-auth 校验端点
    # （该端点无 Bearer/菜单/DB 依赖；浏览器不会把 Path=/grafana/ 的 cookie
    # 发给 /api/... 路径，必须在此显式转发原 Cookie）。上游用变量 + resolver，
    # 避免 nginx 在上游容器暂不可解析时启动失败（同 kaneo /sso/entry 先例）。
    location = /_grafana_auth {
        internal;
        resolver 127.0.0.11 valid=30s ipv6=off;
        set $oo_grafana_auth_backend backend:8000;
        proxy_pass http://$oo_grafana_auth_backend/api/v2/operations/dashboards/grafana-auth;
        proxy_pass_request_body off;
        proxy_set_header Content-Length "";
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Cookie $http_cookie;
    }

    # /grafana（无尾斜杠）显式 301，避免落入 SPA 兜底 location / 返回 index.html。
    location = /grafana {
        absolute_redirect off;
        return 301 /grafana/;
    }

    location /grafana/ {
        auth_request /_grafana_auth;
        auth_request_set $grafana_user $upstream_http_x_grafana_user;
        # 校验失败（401）→ 本地中文提示页；校验器不可达/5xx/超时同样不放行
        # （auth_request 对非 2xx/401/403 一律失败，fail-closed）。
        error_page 401 = @grafana_unauthorized;

        resolver 127.0.0.11 valid=30s ipv6=off;
        # map 在内容阶段读取校验结果；无可用身份只进入本地拒绝 listener。
        # 不带 URI 部分：保留完整 /grafana/ 前缀（serve_from_sub_path 契约）。
        proxy_pass http://$oo_grafana_safe_upstream;
        proxy_http_version 1.1;
        # Host 含入口端口（$http_host）；协议头由可信代理链传递。
        proxy_set_header Host $http_host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        # 覆盖客户端自带身份头：仅放行校验器 204 响应中的共享管理员。
        proxy_set_header X-WEBAUTH-USER $grafana_user;
        proxy_set_header Authorization "";
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 3600s;
    }

    location @grafana_unauthorized {
        default_type text/html;
        charset utf-8;
        return 401 '<p>看板会话已过期，请返回平台刷新页面后重试。</p>';
    }

    location ^~ /assets/ {
        try_files $uri =404;
        add_header Cache-Control "public, max-age=31536000, immutable";
    }

    location / {
        try_files $uri $uri/ /index.html;
        # 入口页必须每次回源校验（有 ETag，未变时 304 很便宜）——
        # 否则浏览器启发式缓存旧 index.html，引用已被替换的旧 bundle，
        # 部署后用户持续看到旧版界面
        add_header Cache-Control "no-cache";
    }
