# AgentCap landing — v2

Live since 8 November 2026 at https://agentcap.launchbench.studio/.
The previous design in `landing/index.html` is kept untouched.

## Preview

```bash
cd landing
python3 -m http.server 8787
# open http://127.0.0.1:8787/v2/
```

## What is here

- `index.html` — the page (new design 26 November 2026; plain three-part hero and quota meter 27 November). No build step, no framework. Cookieless Umami from the same origin (`/u/script.js`).
- `landing.css` — the page design. No inline styles: the live CSP is `style-src 'self'; script-src 'self'`.
- `app.js` — the hero quota meter, the usage / quota / loop demo, tabs and copy buttons. Sets widths through CSSOM, never `style=""` attributes.
- `style.css` / `script.js` — the previous design, still used by `privacy/`.
- `fonts.css` / `fonts/` — self-hosted Instrument Serif and IBM Plex.
- `agentcap-0.2.0.vsix` — served from the site root, not from this folder.

## Deploy target (verified 8 November 2026)

Caddy serves the `agentcap.launchbench.studio` block from `/data/agentcap-site`
inside the `qpa-caddy-1` container. On the host that volume is:

```
/var/lib/docker/volumes/qpa_caddy_data/_data/agentcap-site
```

The canonical copy is `/srv/agentcap`. Deploy to both:

```bash
# from the repo root
rsync -av --exclude README.md --exclude '*.vsix*' --exclude '*.gif' \
  landing/v2/ quilt-vps:/srv/agentcap/

ssh quilt-vps \
  'sudo rsync -a /srv/agentcap/ /var/lib/docker/volumes/qpa_caddy_data/_data/agentcap-site/'
```

Do not use `--delete`: the root also holds `agentcap-0.2.0.vsix`, its
`.sha256`, and the two demo GIFs.

`~/agentcap-staging` is a separate staging copy. It is not the live root; do not
deploy there expecting the site to change.

The Caddyfile is at
`/home/ubuntu/apps/quilt-pattern-draft-auditor/Caddyfile`. Its CSP for this
site must include `font-src 'self'` and `script-src 'self'`; without them the
self-hosted fonts and `script.js` are blocked. Reload with:

```bash
ssh quilt-vps 'sudo docker exec qpa-caddy-1 caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile'
```

Backup made before the 8 November deploy:
`/home/ubuntu/backups/agentcap-live-20261108-0030/`.

## Direction

Warm paper, ink, one signal red. Editorial serif display, IBM Plex for text and
evidence. The hero is a real `agentcap demo` receipt, not a stock illustration.
No gradients, no glass, no icon grid, no fake logos or testimonials.

Content was checked against the 0.3.x tree. Product claims, pricing and limits
are the same as the previous landing page; the OAuth FAQ answer distinguishes
the dollar proxy from quota mode.
