# Passageway Sandbox: Linux portal (step 1)

## Run locally
    docker compose up --build
    # open http://127.0.0.1:7681

## What's hardened
- Non-root user, no sudo, all capabilities dropped, no-new-privileges
- Read-only root filesystem; writable tmpfs home (64 MB) and /tmp (32 MB)
- 256 MB RAM, 0.5 CPU, 128 PIDs
- One client per container, container exits when they disconnect

## Still to do before going public
- Restrict egress (block outbound except package mirrors) on the production Docker network
- Reverse proxy (Caddy) with session-token check in front of ttyd
- Auto-teardown after the session timer (n8n + docker rm)
- Exercises for the Linux basics portal
