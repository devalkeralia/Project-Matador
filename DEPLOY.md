# Deploy — Hetzner US (forward-CLV paper test)

One-time provisioning for the always-on paper run. Paper only: the bot never places orders (no
signer, no order endpoint reachable). For how the service itself works — the mount contract, the
weekly refresh, the systemd alternative — see README **"Run as a service"**.

**Why a US region:** Kalshi is CFTC-regulated and US-only. The bot reads market data
unauthenticated, which works fine from a US connection, but whether Kalshi geo-blocks or throttles a
*foreign datacenter IP* is unverified — and discovering that mid-run would cost the sample. Pick
Ashburn (`ash`) or Hillsboro (`hil`) and the question never arises. Step 4 verifies it before you
commit weeks to the box.

**Measured requirements** (so don't overpay): peak RSS **186 MB** (`build_ratings`, the heaviest
step), refresh runtime **~17 s**, match archives **~70 MB**, no inbound ports — Telegram long-polls,
so there is no webhook to expose. The smallest shared-vCPU tier is 5–20× oversized. Verify current
Hetzner tier naming/pricing yourself; anything with ≥1 vCPU / 1 GB RAM / 20 GB disk is ample.

---

## 0. Buy the server (first time — start here)

**Where:** [console.hetzner.cloud](https://console.hetzner.cloud) (Hetzner *Cloud* — not "Hetzner
Robot", which is their dedicated-server product and far more expensive than this needs).

1. **Sign up** at [hetzner.com/cloud](https://www.hetzner.com/cloud) → *Sign up*. Use an email you
   read; server notices go there.
2. **Identity verification.** New Hetzner accounts are frequently asked for ID or a payment-method
   verification before the first server can be created, and it is not always instant. Do this step
   *before* the evening you plan to deploy — it is the one part of this that can block on someone
   else. If you're asked to upload ID, that is normal for them, not a phishing page.
3. **Add a payment method** — credit card, PayPal, or SEPA direct debit. Prices are listed **ex-VAT**;
   VAT is added based on your billing country, so the invoice will read higher than the sticker.
4. **Create a project** (e.g. `matador`). A project is just a container for servers/firewalls/keys.

**What it costs:** the smallest shared-vCPU tier is roughly **€4–6/month**, billed **hourly** up to a
monthly cap. Check the live price on the server-creation page — I'd rather you read the current number
than trust one written here. Add ~€0.50–1/month if you keep an IPv4 address (do keep it; see step 1).

> **Billing gotcha:** Hetzner charges for a server that is merely **powered off**. Stopping it does
> *not* stop the bill. To stop paying you must **delete** the server. So when the paper test ends,
> back up `data/` first (below), then delete the server and its IPv4 — don't just shut it down.

**Backups:** skip Hetzner's paid backup add-on. The only irreplaceable thing on the box is
`data/matador.db` (the paper-bet log — tens of KB), and everything else rebuilds from git plus the
upstream feed. A weekly pull from your laptop is enough and free:

```bash
scp matador@<server-ip>:~/matador/data/matador.db ~/matador-backup-$(date +%F).db
```

**Generate your SSH key first** (do this before creating the server, so you can paste it during
creation). In WSL, from anywhere:

```bash
ls ~/.ssh/id_ed25519.pub 2>/dev/null || ssh-keygen -t ed25519 -C "matador-laptop" -N ""
cat ~/.ssh/id_ed25519.pub     # copy this entire line into Hetzner in step 1
```

Keep the **private** key (`~/.ssh/id_ed25519`, no `.pub`) on your laptop only — never upload or commit
it. Use WSL rather than Windows for this, since the repo and the `secrets/` you'll copy in step 3 both
live on the WSL side.

## 1. Create the server

In your project → **Add Server**, and set these (defaults are fine for anything not listed):

| Field | Choose | Why |
|-------|--------|-----|
| **Location** | **Ashburn, VA** or **Hillsboro, OR** | US region — the Kalshi geo constraint above. Do not pick a German/Finnish location. |
| **Image** | Ubuntu 24.04 LTS | What the commands below assume. |
| **Type** | *Shared vCPU* → smallest tier (CPX11 / CX22 class) | Measured need is 186 MB RAM; the smallest tier is already 5–20× that. Note US locations may only offer the AMD (`CPX`) line. |
| **Networking** | Leave **Public IPv4 enabled** | IPv6-only is a little cheaper but GitHub and some APIs get awkward over v6-only. Not worth the debugging. |
| **SSH keys** | Paste the public key from step 0 | Do **not** set a root password — key-only from the start. |
| **Firewall** | Create one: **inbound TCP 22 only** | Leave **outbound unrestricted** — the bot needs Kalshi, Telegram, the-odds-api, and GitHub. There is nothing to expose inbound; Telegram is long-polled, so no webhook. |
| **Volumes / Backups / Placement** | Skip all | Nothing here needs them (see the backup note in step 0). |
| **Name** | `matador` | Cosmetic. |

Click **Create & Buy now**. Billing starts at creation. The public IPv4 shown on the server page is
your `<server-ip>` for every command below.

## 2. Harden and install Docker

```bash
ssh root@<server-ip>

adduser --disabled-password --gecos "" matador     # gets uid 1000 -> matches the container user
usermod -aG sudo matador
mkdir -p /home/matador/.ssh && cp ~/.ssh/authorized_keys /home/matador/.ssh/
chown -R matador:matador /home/matador/.ssh && chmod 700 /home/matador/.ssh

# key-only SSH, no root login
sed -i 's/^#*PermitRootLogin.*/PermitRootLogin no/;s/^#*PasswordAuthentication.*/PasswordAuthentication no/' \
  /etc/ssh/sshd_config
systemctl restart ssh

timedatectl set-timezone UTC     # cron and occurrence_datetime both assume UTC -- do not skip

# Docker (official apt repo)
apt-get update && apt-get install -y ca-certificates curl gnupg git
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
chmod a+r /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
  > /etc/apt/sources.list.d/docker.list
apt-get update && apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
usermod -aG docker matador

ufw default deny incoming && ufw default allow outgoing && ufw allow 22/tcp && ufw --force enable
```

Reconnect as `matador` from here on. `id -u` must print **1000** — the container runs as uid 1000, so
a mismatch makes the writable mounts fail (README documents the `user:` escape hatch if it differs).

## 3. Get the repo and the three ignored things onto the box

The repo is private, so use a **read-only deploy key** — no PAT on the server:

```bash
ssh-keygen -t ed25519 -C "matador-vps" -f ~/.ssh/id_ed25519 -N ""
cat ~/.ssh/id_ed25519.pub
# GitHub -> Project-Matador -> Settings -> Deploy keys -> Add, paste, leave "Allow write access" OFF
git clone git@github.com:devalkeralia/Project-Matador.git ~/matador
```

**`config.yaml`, `secrets/`, and `data/` are gitignored — the clone does NOT include them.** Push the
first two from your laptop (never commit them):

```bash
# from the local repo root
scp config.yaml matador@<server-ip>:~/matador/config.yaml
scp -r secrets   matador@<server-ip>:~/matador/          # .env + kalshi pem + odds_api_key.txt
```

Then on the server:

```bash
cd ~/matador
chmod 700 secrets && chmod 600 secrets/*
mkdir -p data logs        # pre-create so they're owned by you, not root (README explains why)
```

## 4. Verify Kalshi is reachable FROM THIS BOX — before going further

```bash
curl -s -o /dev/null -w '%{http_code}\n' \
  'https://external-api.kalshi.com/trade-api/v2/events?series_ticker=KXATPMATCH&status=open&limit=1'
```

**200 = good.** Anything else (403 in particular) means this IP is restricted — stop and rebuild in a
different US location rather than discovering it three weeks into the run.

## 5. Build the image and bootstrap the model

Everything runs through Docker, so the host needs no venv and no pandas:

```bash
cd ~/matador
docker compose build

# fetch the match archives and build data/model.json (~17s; also proves the refresh path works)
docker compose run --rm --entrypoint python matador scripts/prepare_matches.py --fetch
docker compose run --rm --entrypoint python matador scripts/build_ratings.py
```

Check the output: `latest 2026MMDD` should be within a week or so of today, and there must be **no**
`WARNING: N year file(s) rejected` line. That warning means upstream changed format again and the
model is frozen — the exact failure that went unnoticed for weeks before 2026-07-27.

## 6. Start and verify

```bash
docker compose up -d
docker compose logs -f matador          # also written to logs/matador.log, rotated
```

Then, in Telegram:

- `/help` → replies (long-poll is alive)
- `/find` → lists open matches (Kalshi reads work from this box)
- `/check <match>` → a VALUE ALERT or a self-explaining no-value breakdown (the model loaded)
- `/stats` → confirm `sharp_coverage` is non-zero once bets accumulate

Reboot survival — do this now, not after a real outage:

```bash
sudo reboot
# reconnect
docker compose ps       # matador should be Up again via restart: unless-stopped
```

Pending closing-line captures rebuild from the DB on startup, so a restart loses none.

## 7. Weekly model refresh (cron)

`crontab -e` as `matador`. One toolchain — no host venv:

```cron
0 6 * * 1 cd /home/matador/matador && docker compose run --rm --entrypoint python matador scripts/prepare_matches.py --fetch >> logs/refresh.log 2>&1 && docker compose run --rm --entrypoint python matador scripts/build_ratings.py >> logs/refresh.log 2>&1 && docker compose restart matador >> logs/refresh.log 2>&1
```

**Check `logs/refresh.log` after the first Monday.** Two things to confirm: `latest` advanced
week-over-week, and no `rejected` warning. A refresh that stops advancing silently freezes the model
for the rest of the paper test.

## 8. During the run

- A **daily heartbeat DM** arrives — a silent outage otherwise looks exactly like "no edge".
- `docker compose logs --since 24h matador` for a quick health read.
- `docker compose run --rm --entrypoint python matador scripts/clv_report.py` for the segmented
  CLV report, including the **layoff axis** (the pre-registered decay instrument — see
  DESIGN-DECISIONS "Layoff / inactivity").
- **Do not change `p_model` mid-run.** Any model change invalidates the accumulated sample; the gate
  needs 200+ sharp-referenced bets across 12+ ISO weeks.
- The liquidity gate is **frozen** at `min_liquidity 500` / `max_spread 0.03` for the whole sample so
  it stays homogeneous. Don't recalibrate partway.

## 9. When the paper test ends — tear down so billing stops

A powered-off Hetzner server still bills. In order:

```bash
# 1. from your laptop -- save the only irreplaceable artifact
scp matador@<server-ip>:~/matador/data/matador.db ~/matador-final.db
scp matador@<server-ip>:~/matador/logs/matador.log ~/matador-final.log
```

2. Hetzner console → server → **Delete**. Then check **Networking → Primary IPs** and delete the
   released IPv4 too — an unassigned IP keeps billing on its own.
3. Remove the deploy key from the GitHub repo (Settings → Deploy keys).

Read the final verdict off the backed-up DB locally with
`.venv/bin/python scripts/clv_report.py` (point `db_path` at the copy).
