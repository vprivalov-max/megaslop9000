# Deploy guide — series.gamegears.online

One-time server setup, then ongoing updates via `./deploy.sh`.

---

## 1. Prerequisites

- VPS with Docker + Docker Compose v2 (`apt install docker.io docker-compose-plugin`)
- DNS A record: `series.gamegears.online → <VPS IP>`
- Google Cloud OAuth 2.0 Client ID
  ([console.cloud.google.com](https://console.cloud.google.com)):
  - Application type: Web application
  - Authorized redirect URI: `https://series.gamegears.online/auth/google/callback`
- API keys: Anthropic, AVAI, Reteller

## 2. First-time setup on the server

```bash
# As root or a sudo user with docker access
git clone git@github.com:gamegears/series-writer.git
cd series-writer

# Create .env with real values
cp .env.example .env
nano .env

# Edit Caddyfile — replace `series.gamegears.online` if you use a different host
nano Caddyfile

# Generate Flask session secret and paste it into .env (FLASK_SECRET_KEY=...)
python3 -c "import secrets; print(secrets.token_hex(32))"

# Boot it
docker compose up -d --build

# Tail logs to verify
docker compose logs -f
```

Caddy fetches a Let's Encrypt cert on first request — give it ~30 sec, then
open `https://series.gamegears.online`.

## 3. Updates

```bash
ssh user@server
cd /path/to/series-writer
./deploy.sh
```

That's it. Pulls latest from `origin/main`, rebuilds the app image, restarts
the container. Caddy stays untouched (cert is preserved in the `caddy_data`
volume).

## 4. Backups

User data lives in the `app_data` Docker volume (mounted at `/data`).

```bash
# Snapshot to a tarball
docker run --rm -v app_data:/data -v $PWD:/backup busybox \
  tar czf /backup/series-writer-data-$(date +%F).tar.gz -C /data .

# Recommended: cron job pushing to Cloudflare R2 / S3
```

## 5. Operations

| Task | Command |
|------|---------|
| Tail logs | `docker compose logs -f app` |
| Shell into container | `docker compose exec app bash` |
| Restart app only | `docker compose restart app` |
| Health check | `curl https://series.gamegears.online/healthz` |
| List users | `docker compose exec app ls /data` |
| Free disk | `docker system prune -af` (be careful — also removes images) |

## 6. Adding a new employee

Nothing to do server-side. The first time they visit
`https://series.gamegears.online` and log in with their `@gamegears.online`
Google account, their personal `/data/<email>/projects/` directory is
created automatically.

## 7. Removing an employee

Their data is at `/data/<email-slug>/`. Either:

- Move it elsewhere: `docker compose exec app mv /data/<slug> /data/_archive/<slug>`
- Or delete: `docker compose exec app rm -rf /data/<slug>`

To revoke active sessions, restart the app — `FLASK_SECRET_KEY` rotation
invalidates all logins.
