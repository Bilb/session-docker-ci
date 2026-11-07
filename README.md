## Session Continuous Integration docker build images

This repo manages the Session project CI docker images used to build and test various Session
packages.

The repository is publicly available (read-only) at https://registry.session.codes.

If you aren't part of the Session team, you'll likely need to set up your own registry and change
reg.session.codes:80 (which resolves to an internal VPN IP address usable by for machines authorized
to push to the registry) to your own domain name to do anything useful with this.

### How the images are built

Every image is a stage of `Dockerfile`.  `rebuild-docker-images.py` runs one `docker buildx bake`
per distro, building its `-base`, `-builder` and final images on each arch (pushed both as
`IMAGE/ARCH` and as the multi-arch `IMAGE`), plus the images built on them: `debian-sid-clang`,
`android`, `flutter`, `lint`, `clone`, `debian-win32-cross`, `debian-stable-cross`, `docbuilder` and
`debian-forky-s390x-cross`.  Distros build in parallel and fail independently; a failed distro is
retried, and every failure is listed at the end with its log.

An image is only rebuilt when there is something new to install: the `APT_STATE` build argument is a
fingerprint of the Packages indexes of every apt archive its distro installs from, so unchanged
distros come straight from the build cache.  `--no-cache` rebuilds everything.

`./rebuild-docker-images.py --print` shows the bake definitions; `--distro debian-sid` and
`--arch amd64,i386` narrow a run.

### The builder

The script creates a buildx builder (`session-ci`, using the `docker-container` driver) on first
use, configured for the registry's plain HTTP.  On its own it builds the host's platforms (amd64 and
i386 on an x86 host).  The ARM images need either qemu (`apt install qemu-user-static
binfmt-support`), or, much faster, a native ARM machine added as a node:

    docker buildx create --append --name session-ci --platform linux/arm64,linux/arm/v7 \
        --buildkitd-config buildkitd.toml ssh://user@arm-host

where `buildkitd.toml` holds the same `[registry."reg.session.codes:80"] http = true` section the
script writes.  A run fails, rather than skipping them, when the builder can't build a requested
arch.

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
output, and each distro's full build log is in the run directory the output names.  A manual
`./rebuild-docker-images.py` and the timer share a lock, so neither starts while the other runs.
