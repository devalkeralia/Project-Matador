# Deploy — DigitalOcean US (forward-CLV paper test)

One-time provisioning for the always-on paper run. Paper only: the bot never places orders (no
signer, no order endpoint reachable). For how the service itself works — the mount contract, the
weekly refresh, the systemd alternative — see README **"Run as a service"**.

**Why a US region:** Kalshi is CFTC-regulated and US-only. The bot reads market data
unauthenticated, which works fine from a US connection, but whether Kalshi geo-blocks or throttles a
*foreign datacenter IP* is unverified — and discovering that mid-run would cost the sample. Pick a US
region and the question never arises. Step 4 verifies it before you commit weeks to the box.

**Measured requirements** (so don't overpay): peak RSS **186 MB** (`build_ratings`, the heaviest
step), refresh runtime **~17 s**, match archives **~70 MB**, no inbound ports — Telegram long-polls,
so there is no webhook to expose. Anything with ≥1 vCPU / **1 GB** RAM / 20 GB disk is ample.

**Why DigitalOcean** (chosen 2026-07-28): Hetzner raised US prices in 2026 — `cpx11` in Ashburn was
observed at **$20.49/mo**, against DO's **$6/mo** for equivalent-enough hardware. Kamatera is $4/mo
and equally capable, but reviewers consistently note it suits experienced users, its creation flow
configures every resource independently, and its cloud firewall is a paid add-on; the $2/mo saving
isn't worth that on a first deploy. **Steps 3–9 are provider-agnostic** — only steps 0–2 below are
DO-specific, so switching hosts later costs little. Always read live pricing rather than these lines.

---

## 0. Account + SSH key (first time — start here)

**Where:** [cloud.digitalocean.com](https://cloud.digitalocean.com). One console, one product line —
none of Hetzner's Console/Robot/konsoleH split to navigate.

1. **Sign up** at [digitalocean.com](https://www.digitalocean.com). Use an email you read; billing and
   incident notices go there.
2. **Add a payment method** (card or PayPal). Expect a small temporary authorisation hold — that's
   verification, not a charge. New accounts sometimes carry promotional credit; if you have any, it
   covers the first months of this outright.
3. **Projects are optional** — DO creates a default one at signup and the droplet form pre-fills it.
   Pure organisation, no functional effect, and resources can be moved between projects later. Making
   a `matador` project is fine tidiness; skipping it costs nothing either. If you do create one, its
   **Environment** field is just a label — pick **Production**: `data/matador.db` holds a 12-week
   sample that cannot be reconstructed, and the box runs unattended for months, so nothing here should
   be treated as disposable.

**What it costs:** the **$6/month** Basic droplet (1 vCPU / 1 GB / 25 GB SSD / 1 TB transfer). A
12-week paper test is therefore **~$18**.

> **Do NOT pick the $4 (512 MB) tier.** Add it up: Ubuntu idle ~200 MB + Docker daemon ~100 MB + our
> container ~250 MB, spiking during the weekly refresh — roughly 550 MB peak. 512 MB would need swap
> and could OOM mid-refresh, which is a silent way to lose the sample. 1 GB is the floor.

> **Billing gotcha:** DO charges for a droplet that is merely **powered off**. Shutting it down does
> *not* stop the bill — you must **Destroy** it. When the test ends, back up `data/` first (below),
> then destroy the droplet; also check **Networking → Reserved IPs**, since an unassigned reserved IP
> bills on its own (we don't use one, but check).

**Backups: enabled 2026-07-28, USAGE-BASED, daily, ~14-day retention.** Worth having at all because
`data/matador.db` accumulates the project's only clean forward instrument, cannot be reconstructed, and
automated backups need no weekly discipline.

Choose **usage-based over plan-based**: plan-based charges a flat 20% of the droplet (~$1.20/mo)
regardless of usage, while usage-based charges per GiB of *restorable size* — observed at **$0.04/GiB,
weekly**, and this box uses only ~4–5 GB of its 25 GB, so **~$0.20/mo**. Retention is set in weeks;
**4 weeks** is enough to reach back past corruption you didn't notice immediately, and extra weeks cost
little since storage is incremental and our data barely changes (static OS/Docker layers, a DB growing
by kilobytes). Weekly frequency is adequate: the worst case is losing ~6 days of bets, which costs a
week of extra runtime rather than invalidating anything, since the gate counts bets and week-clusters.

> A live DO snapshot is crash-consistent, not quiesced — normally a concern for a database mid-write,
> but `storage.py` sets `PRAGMA journal_mode = WAL`, which is designed to survive abrupt termination, so
> a restored snapshot replays the WAL exactly as it would recover from power loss.

Backups are **disaster recovery** (restoring one builds a whole new droplet), so they don't replace
pulling the DB to your laptop when you want to actually read results with `clv_report.py`:

```bash
scp matador@<droplet-ip>:~/matador/data/matador.db ~/matador-backup-$(date +%F).db
```

**Your SSH key** (already generated on 2026-07-28 as `matador-vps`). To re-print the public half:

```bash
cat ~/.ssh/id_ed25519.pub          # paste this entire line into DO in step 1
ssh-keygen -l -E md5 -f ~/.ssh/id_ed25519.pub   # MD5, if a console shows that format
```

Keep the **private** key (`~/.ssh/id_ed25519`, no `.pub`) on your laptop only — never upload it, never
commit it, and **do not put it in `secrets/`**: that directory gets copied to the server in step 3, and
a private key on the server is exactly what you don't want. Use WSL rather than Windows, since the repo
and the `secrets/` you'll copy both live on the WSL side.

## 1. Create the droplet

**Create → Droplets.** Set these; defaults are fine for anything not listed.

| Field | Choose | Why |
|-------|--------|-----|
| **Region** | **New York** or **San Francisco** | US — the Kalshi geo constraint above. Any datacenter number within the region is fine. |
| **Datacenter** | any (e.g. NYC3) | No preference; all are US. |
| **OS image** | Ubuntu **24.04 (LTS) x64** | Not 26.04: Docker's official apt repo has historically lagged new Ubuntu codenames by weeks–months, and step 2 is where you'd find out. 24.04 is supported into 2029 — far beyond this test. |
| **Droplet type** | **Basic** (shared CPU) | DO's own framing — "for workloads that underuse dedicated threads" — is us exactly: 186 MB peak RSS, a ~17 s weekly refresh, otherwise an idle long-poll. *Premium/Dedicated* pays for sustained-load consistency we never generate. |
| **CPU option** | **Regular (SSD)** — the $6 tier | 1 vCPU / 1 GB / 25 GB. Premium Intel/AMD (NVMe) costs more for disk speed that is irrelevant to a 1.6 MB JSON load and a weekly 17 s job. |
| **Authentication** | **SSH Key** → add `matador-vps` | Choose SSH key, **not** password. Paste `~/.ssh/id_ed25519.pub`. If DO emails you a root password, the key didn't attach — stop and fix it. |
| **Improved metrics monitoring** | ✅ enable | Free, tiny agent, gives CPU/RAM graphs and lets you set alerts. Worth having when the box runs unattended for months — a silent outage otherwise looks like "no edge". |
| **IPv6** | ❌ **leave OFF** | Free, so disabling saves nothing — this is outbound reliability. Dual-stack makes Python walk `getaddrinfo` order and wait out a stalled connect, which fights `SharpOddsClient`'s deliberate **5 s** timeout and can silently cost the sharp reference the go-live gate binds on. We run **no inbound services**, and all four dependencies (Kalshi, Telegram, the-odds-api, GitHub) are IPv4-reachable. |
| **Backups** | ❌ off | Paid; see the backup note above. |
| **User data / cloud-init** | empty | Step 2 could go here, but a cloud-init failure means debugging a boot log instead of watching a command fail interactively. |
| **VPC / Private networking** | default, ignore | For droplet-to-droplet traffic; we have one box. |
| **Hostname** | `matador` | Becomes the shell prompt once you SSH in — a cheap guard against running something destructive while thinking you're on your laptop. Letters, digits, hyphens only. |
| **Tags** | none | Metadata for filtering at scale; no functional effect. |

Click **Create Droplet**. Billing starts immediately. The public IPv4 on the droplet page is your
`<droplet-ip>` for every command below.

DO's droplet form has no firewall picker — you attach one afterwards, and it's **free**.

### Then attach the cloud firewall (after the step-4 Kalshi check, not before)

Run the step-4 reachability check first — no point firewalling a box you might destroy. Then
**Networking → Firewalls → Create Firewall**. DO pre-populates sensible rules; you want exactly:

| Direction | Protocol | Port | Source / Destination |
|---|---|---|---|
| Inbound | TCP | **22** | `0.0.0.0/0` |
| Outbound | ICMP + TCP + UDP | all | all destinations (DO's default) |

Delete any pre-populated inbound HTTP/HTTPS rules — we serve nothing. **Leave the outbound rules
alone**: the bot needs Kalshi, Telegram, the-odds-api, GitHub, and apt. Then under **Apply to
Droplets**, select `matador`.

> ⚠️ **Confirm the inbound SSH rule exists BEFORE applying.** A firewall attached with no inbound
> rule drops everything including your live SSH session, and recovery means DO's browser console.
>
> Tightening `Source` to your home IP is tempting, but home IPs rotate and locking yourself out is
> worse than an exposed port that only accepts keys. Step 2's `ufw` adds a second layer on-host.

## 2. Harden and install Docker

```bash
ssh root@<droplet-ip>

adduser --disabled-password --gecos "" matador     # gets uid 1000 -> matches the container user
usermod -aG sudo matador
mkdir -p /home/matador/.ssh && cp ~/.ssh/authorized_keys /home/matador/.ssh/
chown -R matador:matador /home/matador/.ssh && chmod 700 /home/matador/.ssh
chmod 600 /home/matador/.ssh/authorized_keys

# REQUIRED: --disabled-password + sudo group is unusable on its own -- sudo would prompt for a
# password that does not exist. The passphrase-less SSH key is already the only auth, so NOPASSWD
# costs nothing: whoever holds the key owns the box regardless.
echo "matador ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/90-matador
chmod 440 /etc/sudoers.d/90-matador
visudo -c                        # validate before trusting it

timedatectl set-timezone UTC     # cron and occurrence_datetime both assume UTC -- do not skip

# 2 GB swap: OOM guard for the weekly refresh, which runs build_ratings in a SECOND container while
# the bot is up. Measured afterwards: the bot uses only ~137 MB and swap stayed at 0, so this is
# insurance rather than a fix -- but an OOM mid-refresh fails SILENTLY (the model just stops
# advancing), and the margin erodes as the DB and logs grow.
fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
echo '/swapfile none swap sw 0 0' >> /etc/fstab
sysctl -w vm.swappiness=10 && echo 'vm.swappiness=10' >> /etc/sysctl.conf

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

**Now verify `matador` can log in AND use sudo/docker BEFORE locking down root** — otherwise a mistake
locks you out of your own box:

```bash
# from the laptop
ssh matador@<droplet-ip> 'id -u; docker ps >/dev/null && echo docker-ok; sudo -n true && echo sudo-ok'
```

`id -u` must print **1000** — the container runs as uid 1000, so a mismatch makes the writable mounts
fail (README documents the `user:` escape hatch if it differs). Only once that passes, as root:

```bash
sed -i 's/^#*PermitRootLogin.*/PermitRootLogin no/;s/^#*PasswordAuthentication.*/PasswordAuthentication no/' \
  /etc/ssh/sshd_config
# DO images often drop an override in sshd_config.d/ that RE-ENABLES password auth -- editing only
# sshd_config silently leaves it on. Neutralise any override, then trust `sshd -T`, not the files.
for f in /etc/ssh/sshd_config.d/*.conf; do [ -e "$f" ] || continue
  sed -i 's/^\s*PasswordAuthentication.*/PasswordAuthentication no/I;s/^\s*PermitRootLogin.*/PermitRootLogin no/I' "$f"
done
sshd -t && systemctl restart ssh
sshd -T | grep -Ei '^(permitrootlogin|passwordauthentication|pubkeyauthentication)'
```

Then confirm from the laptop that root is refused and `matador` still works.

## 3. Get the repo and the three ignored things onto the box

The repo is **public**, so clone over HTTPS — **no deploy key and no credential on the server at all**
(this is why the visibility check in `CLAUDE.md` matters). As `matador`:

```bash
git clone https://github.com/devalkeralia/Project-Matador.git ~/matador
cd ~/matador && mkdir -p data logs   # pre-create so they're owned by you, not root (see step 0 risk)
```

> **BLOCKING: push local work first.** The droplet builds its model from whatever `origin/main` has.
> Confirm the clone carries current values, e.g.
> `grep -E "min_price: float|shrinkage_n0: float" matador/config.py` → **0.20 / 0.0**. A stale clone
> silently builds the model with the old shrinkage.

**`config.yaml`, `secrets/`, and `data/` are gitignored — the clone does NOT include them.** Copy the
first two from your laptop, from the repo root (relative paths), and **transfer only the secrets the
bot actually references** rather than the whole directory — `secrets/` also holds GitHub PATs that have
no business on an internet-facing box:

```bash
ssh matador@<droplet-ip> 'mkdir -p ~/matador/secrets && chmod 700 ~/matador/secrets'
scp config.yaml matador@<droplet-ip>:~/matador/config.yaml
scp secrets/.env secrets/odds_api_key.txt secrets/kalshi_private_key.pem \
    matador@<droplet-ip>:~/matador/secrets/
ssh matador@<droplet-ip> 'chmod 600 ~/matador/secrets/*'
```

Verify with `ls -la secrets/` — plain `ls` hides `.env`. The Kalshi `.pem` is validated as a config
string but **never read** (`matador/bot.py` builds `KalshiClient` with no signer); it's copied for
completeness.

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

A powered-off DO droplet still bills. In order:

```bash
# 1. from your laptop -- save the only irreplaceable artifact
scp matador@<droplet-ip>:~/matador/data/matador.db ~/matador-final.db
scp matador@<droplet-ip>:~/matador/logs/matador.log ~/matador-final.log
```

2. DO console → droplet → **Destroy**. Then check **Networking → Reserved IPs** — an unassigned
   reserved IP bills on its own (we don't create one, but confirm).
3. Remove the deploy key from the GitHub repo (Settings → Deploy keys).

Read the final verdict off the backed-up DB locally with
`.venv/bin/python scripts/clv_report.py` (point `db_path` at the copy).
