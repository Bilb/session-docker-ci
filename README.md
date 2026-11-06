## Session Continuous Integration docker build images

This repo manages the Session project CI docker images used to build and test various Session
packages.

The repository is publicly available (read-only) at https://registry.session.codes.

If you aren't part of the Session team, you'll likely need to set up your own registry and change
reg.session.codes:80 (which resolves to an internal VPN IP address usable by for machines authorized
to push to the registry) to your own domain name to do anything useful with this.

### Nightly rebuilds

`systemd/` holds a service and timer that run the rebuild every night, on a machine that can push
to the registry:

1. Copy both units to `/etc/systemd/system/`, set `User=` and `WorkingDirectory=` in the service.
2. Log that user in to Docker Hub with `docker login` and an access token, stored in
   `~/.docker/config.json` itself rather than a desktop keyring: the timer runs without a login
   session, and anonymous pulls of the base images count against Docker Hub's much lower
   anonymous rate limit.
3. For failure notifications, create a webhook in the Discord channel (Edit Channel → Integrations
   → Webhooks → New Webhook, which needs the Manage Webhooks permission), and put its URL in
   `/etc/session-docker-ci.env` as `DISCORD_WEBHOOK_URL=...`, readable only by root (`chmod 600`).
4. `systemctl daemon-reload && systemctl enable --now session-docker-ci.timer`.

`systemctl start session-docker-ci` runs it once by hand; `journalctl -u session-docker-ci` has the
output, and each failure's full log is in the `/tmp/tmp*.log` file the summary names.  A manual
`./rebuild-docker-images.py` and the timer share a lock, so neither starts while the other runs.
