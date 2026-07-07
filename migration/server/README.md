# Nightingale QQ Bot Server Migration

This folder contains server-side templates for moving the QQ bot stack to Linux:

- AstrBot and NapCat run in one Docker Compose project.
- NapCat connects to AstrBot through Docker DNS: `ws://astrbot:6199/ws`.
- AstrBot and NapCat admin ports are bound to `127.0.0.1` by default.
- Remote admin access should go through Tailscale.
- The runner runs on the host through systemd and listens on `127.0.0.1:18766`.

## 1. Install Tailscale

Official Linux install docs: https://tailscale.com/docs/install/linux

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up --ssh
tailscale ip
tailscale status
```

After the server appears in the Tailscale admin console, disable key expiry for this trusted server if you want unattended long-running service.

## 2. Prepare Directories

```bash
sudo mkdir -p /opt/nightingale/astrbot
sudo mkdir -p /opt/nightingale/napcat/qq
sudo mkdir -p /opt/nightingale/napcat/config
sudo mkdir -p /opt/nightingale/NightingaleOpsBot/.local
```

Create a service user:

```bash
sudo useradd --system --create-home --shell /usr/sbin/nologin nightingale || true
sudo chown -R nightingale:nightingale /opt/nightingale
```

## 3. Upload Migration Package

Upload the package created by:

```powershell
H:\NightingaleSilenceWeb\NightingaleOpsBot\scripts\migration\New-ServerMigrationPackage.ps1
```

Then unpack it on the server under `/opt/nightingale`.

Expected result:

```text
/opt/nightingale/
├── astrbot/
│   ├── docker-compose.yml
│   └── data/
├── napcat/
│   ├── qq/
│   └── config/
└── NightingaleOpsBot/
    ├── runner/
    ├── package.json
    └── .local/runner.env
```

## 4. Edit Configs

The migration package generates `/opt/nightingale/NightingaleOpsBot/.local/runner.env`
from the local runner config. Review it before starting systemd.

NapCat config must point its OneBot reverse WebSocket to:

```text
ws://astrbot:6199/ws
```

AstrBot plugin configs that call the runner should use:

```text
http://host.docker.internal:18766
```

This works because `docker-compose.yml` includes:

```yaml
extra_hosts:
  - "host.docker.internal:host-gateway"
```

If HAPI Connector is not migrated, disable the HAPI plugin in AstrBot WebUI after startup.

## 5. Install Runtime

Install Docker, Docker Compose plugin, Node.js 20+ or 22+, and Tailscale.

Then install runner dependencies if the project gets dependencies later:

```bash
cd /opt/nightingale/NightingaleOpsBot
npm install --omit=dev
```

The current runner only uses Node built-ins, so `npm install` may be a no-op.

## 6. Start Services

```bash
sudo cp /opt/nightingale/NightingaleOpsBot/migration/server/nightingale-ops-runner.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now nightingale-ops-runner

cd /opt/nightingale/astrbot
sudo docker compose up -d
```

## 7. Validate

```bash
systemctl status nightingale-ops-runner --no-pager
curl http://127.0.0.1:18766/health
docker ps
docker logs --tail 120 astrbot
docker logs --tail 120 napcat
```

From a Tailscale-connected machine:

```bash
ssh <server>
ssh -L 6185:127.0.0.1:6185 -L 6099:127.0.0.1:6099 <server>
```

Then open:

- AstrBot: `http://127.0.0.1:6185`
- NapCat: `http://127.0.0.1:6099`

QQ private-message checks:

```text
help
石之家状态
石之家签到
```

## Risks

- `secret.key` must stay with `risingstone.sqlite3`; otherwise saved Stone House credentials cannot be decrypted.
- NapCat login state might not survive machine migration because QQ may detect a device fingerprint change.
- Do not expose `6185`, `6099`, `3001`, or `6199` to the public internet.
- Migration packages contain QQ login state, cookies, and tokens. Do not commit, upload publicly, or place in a web root.
