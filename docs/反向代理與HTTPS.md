# 反向代理與 HTTPS（v0.21.0）

ORCH 容器預設只在 `127.0.0.1:5050` 開放。要讓其他電腦或手機連線，請在前面放一個反向代理（Caddy 或 nginx）負責 HTTPS，**不要**直接把 5050 埠對外開放。

## 1. ORCH 設定（`.env`）

```dotenv
SESSION_COOKIE_SECURE=1          # 登入 cookie 只經 HTTPS 傳送
ORCH_PROXY_FIX=1                 # 使用前面「一層」代理的 X-Forwarded-For/Proto/Host
ORCH_TRUSTED_PROXY=172.16.0.0/12 # 代理的來源位址（見下文）；預設 127.0.0.1,::1
ORCH_TRUSTED_HOSTS=orch.example.com
ORCH_SETUP_TOKEN=<一段隨機字串>    # 必填：首次設定頁需要此權杖
```

`ORCH_PROXY_FIX` 的數字等於代理層數；只有在 ORCH 前面確實有代理時才設定。

**`ORCH_TRUSTED_PROXY`（必須正確設定）**：ORCH 只會接受來自這些位址（IP 或網段，逗號分隔）的 `X-Forwarded-*` 標頭；任何其他來源送來的 `X-Forwarded-*` / `Forwarded` 標頭會在進入程式前被刪除，所以直接連到 5050 埠的人無法偽造來源 IP、HTTPS 或 `X-Forwarded-Host`（主機名稱檢查一律用真正的 `Host`）。waitress（`serve.py`）及開發伺服器行為相同。

- 代理與 ORCH 在同一部機器、**不用** Docker：保留預設 `127.0.0.1,::1`。
- Docker（代理在主機上連 `127.0.0.1:5050`，或代理在同一個 compose 內）：容器看到的來源是 Docker 網橋位址，例如 `172.18.0.1`，請設定 `ORCH_TRUSTED_PROXY=172.16.0.0/12`（Docker 預設私有網段；因 5050 只對 `127.0.0.1` 開放，只有本機程式／容器能連到）。可用 `docker network inspect` 查看實際網段後填得更精確。
- `*` 代表信任所有來源，只可在除代理外沒有任何人能連到該埠時使用。

若 `ORCH_PROXY_FIX` 已設定但來源不在名單內，日誌會出現一次 `ignored X-Forwarded-* headers from untrusted peer ...` 警告，並以真正的 `Host` 判斷（通常會得到 400），請按日誌中的位址調整 `ORCH_TRUSTED_PROXY`。

修改後：`docker compose up -d`。

## 2. Caddy（最簡單，自動申請 Let's Encrypt 證書）

`Caddyfile`：

```caddyfile
orch.example.com {
    encode gzip
    reverse_proxy 127.0.0.1:5050
    header {
        Strict-Transport-Security "max-age=31536000"
        X-Content-Type-Options nosniff
        Referrer-Policy same-origin
    }
    request_body {
        max_size 16MB
    }
}
```

## 3. nginx

```nginx
server {
    listen 80;
    server_name orch.example.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    server_name orch.example.com;

    ssl_certificate     /etc/letsencrypt/live/orch.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/orch.example.com/privkey.pem;

    client_max_body_size 16m;
    add_header Strict-Transport-Security "max-age=31536000" always;

    location / {
        proxy_pass http://127.0.0.1:5050;
        proxy_set_header Host              $host;
        proxy_set_header X-Forwarded-For   $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-Host  $host;
        proxy_read_timeout 120s;          # AI 草稿可能需時較長
    }
}
```

證書可用 `certbot --nginx -d orch.example.com` 申請。

## 4. 代理與 ORCH 在同一個 compose 內

如果把 Caddy 加進 `docker-compose.yml`，`reverse_proxy orch:5050`，並移除 orch 服務的 `ports:`，只讓 Caddy 開放 80/443。此時 `ORCH_TRUSTED_HOSTS` 仍需填公開網域名稱。

## 5. 檢查

- `https://orch.example.com/healthz` 回傳 `{"ok": true, ...}`
- 瀏覽器開發者工具中，`orch_session` cookie 有 `Secure`、`HttpOnly`、`SameSite=Lax`
- `http://` 會自動轉到 `https://`
