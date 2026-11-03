# Deploy the gateway on the sandbox host

## 1. Values to collect from Cloudflare
- **Team domain:** Zero Trust -> Settings -> General. Looks like `yourteam.cloudflareaccess.com`.
- **AUD tag:** Access -> Applications -> your sandbox app -> Overview -> "Application Audience (AUD) Tag".

## 2. Get the files onto the host
Put all the files in one GitHub repo (e.g. `PassagewayStudio/passageway-sandbox`):
Dockerfile.linux, entrypoint.sh, motd.txt, gateway.py, Dockerfile.gateway,
requirements.txt, host-compose.yml, Caddyfile. A public repo is fine (no secrets are in these files).
Terraform files can stay out of it or in a separate folder.

In an SSM session on the host:
    sudo apt-get install -y git
    sudo git clone https://github.com/<you>/<repo>.git /opt/sandbox-src
    cd /opt/sandbox-src

## 3. Config (secrets stay on the host, not in git)
    sudo cp /opt/sandbox/.env .env
    echo 'CF_TEAM_DOMAIN=yourteam.cloudflareaccess.com' | sudo tee -a .env
    echo 'CF_ACCESS_AUD=<aud tag>' | sudo tee -a .env
    # optional later: echo 'EVENT_WEBHOOK_URL=https://<n8n webhook url>' | sudo tee -a .env

## 4. Build and start
    sudo docker build -f Dockerfile.linux -t passageway/portal-linux:dev .
    sudo docker compose -p sandbox -f host-compose.yml up -d --build --remove-orphans
    sudo docker compose -p sandbox ps
    sudo docker logs gateway --tail 20

`-p sandbox` keeps the same project name as the first deploy, so caddy and
cloudflared are recreated in place instead of duplicated.

## 5. Test
Open https://sandbox.passagewaystudio.com, log in, click "Start session".
You should land in a terminal. "End session" or the 45-minute timer removes the container.
On the host, `sudo docker ps` shows the `sbx-...` container while it runs.

## Behavior and limits
- One session per email, max 3 at once, 45 minutes each (SESSION_MINUTES / MAX_SESSIONS).
- Each session gets its own internal Docker network: no internet, and users can't reach each other.
- Session state lives in gateway memory. If the gateway restarts, it removes all running sessions.
- The gateway reaches Docker only through docker-proxy, but it can still create containers,
  so treat the gateway as the trust boundary. Keep Access limited to approved emails.
- Embedding: Cloudflare's login page can't load inside an iframe, so link to the sandbox
  from the site (new tab) instead of embedding it.
