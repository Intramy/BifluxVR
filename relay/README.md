# Install-report relay

The installer can post one line per install to a private Discord channel:

```
Linux, Gentoo Linux, Linux 7.2.5-gentoo-dist-bin - USER COUNT: 42 - SUCCESSFUL
```

It never talks to Discord itself. A webhook URL in open-source code is public,
and anyone holding it can spam or delete the webhook. Instead the installer
calls this small Cloudflare Worker, which keeps the webhook secret, numbers
the users, rate-limits by IP (a keyed hash kept for 10 minutes, never the IP
itself) and edits the message from `INSTALLING` to `SUCCESSFUL` or
`FAILED: <reason>`.

## Deploy (free plan, ~10 minutes)

1. Discord: channel settings → Integrations → Webhooks → New Webhook → Copy URL.
   Never commit this URL.
2. Cloudflare dashboard → Storage & Databases → KV → Create namespace
   `bifluxvr-reports`.
3. Workers & Pages → Create → Worker → "Hello World" → Deploy, then Edit code:
   replace everything with `worker.js` and Deploy.
4. The worker's Settings → Bindings → Add → KV namespace: variable name
   `REPORTS`, namespace `bifluxvr-reports`.
5. Settings → Variables and Secrets → Add, type **Secret**:
   - `WEBHOOK` = the Discord webhook URL
   - `SIGNING_KEY` = a long random string (`openssl rand -hex 32`)
6. Copy the worker URL (`https://<name>.<you>.workers.dev`) into `RELAY_URL`
   in `wizard/report.py`.

Test it:

```sh
curl -X POST https://<name>.<you>.workers.dev/start \
     -d '{"os":"Linux","distro":"Test","kernel":"Linux 0.0"}'
```

If the webhook ever leaks, delete it in Discord, make a new one and update
the `WEBHOOK` secret. No installer update is needed.
