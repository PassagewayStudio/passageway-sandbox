# Sandbox host setup (separate EC2 + Cloudflare Tunnel + Access)

## 1. Create the tunnel (Cloudflare dashboard)
1. Zero Trust -> Networks -> Tunnels -> Create a tunnel (type: Cloudflared).
2. Copy the tunnel token (don't run their install command; Terraform handles the host).
3. Add a public hostname: `sandbox.passagewaystudio.com` -> Service type HTTP, URL `caddy:80`.

## 2. Store the token in AWS (never in Terraform or git)
    aws ssm put-parameter \
      --region us-east-1 \
      --name /passageway/sandbox/tunnel_token \
      --type SecureString \
      --value '<paste token>'

## 3. Deploy the host
    terraform init
    terraform validate
    terraform plan
    terraform apply

Shell access (no SSH, no open ports):
    aws ssm start-session --region us-east-1 --target <instance_id>

Check it came up (about 3-5 minutes after apply):
    sudo docker ps            # caddy + cloudflared running
    sudo tail /var/log/cloud-init-output.log

## 4. Put Cloudflare Access in front
1. Zero Trust -> Access -> Applications -> Add -> Self-hosted.
2. Domain: `sandbox.passagewaystudio.com`.
3. Policy: Allow, include your approved emails (manual approval).
   Login method: One-time PIN (emailed code).
4. Later, for auto-approve: allow any email that completes the one-time PIN.

## 5. Expected result right now
Visiting the hostname shows the Cloudflare login. After login you should see a
502 error from Caddy, because the gateway service doesn't exist yet. That
confirms the tunnel, Access, and Caddy are all wired correctly.

## Notes
- The `sandbox-sessions` Docker network is `--internal`: session containers will
  have no internet access. Fine for the Linux portal; revisit for portals that
  need package installs.
- Set an AWS Budgets alert before opening this to users.
