## Session Continuous Integration docker build images

This repo manages the Session project CI docker images used to build and test various Session
packages.

The repository is publicly available (read-only) at https://registry.session.codes.

If you aren't part of the Session team, you'll likely need to set up your own registry and change
reg.session.codes:80 (which resolves to an internal VPN IP address usable by for machines authorized
to push to the registry) to your own domain name to do anything useful with this.

### How the images are built

Each distro's `DISTRO-base`, `DISTRO-builder` and `DISTRO` images are the stages of `Dockerfile`.
Every other image has its own `images/NAME.Dockerfile`, whose `FROM` names the image it is built on
(`android` is `FROM debian-testing-base`, `debian-sid-clang` is `FROM debian-sid`); its arches are listed in `EXTRAS`
in `rebuild-docker-images.py`.

`rebuild-docker-images.py` runs one `docker buildx bake` per distro, building its own images on each
arch (pushed both as `IMAGE/ARCH` and as the multi-arch `IMAGE`), then one per image built on them,
together with the images built on that one.  Distros build in parallel and fail independently, and
an image built on a distro is still attempted when the distro's own images fail.  A failed build is
retried, a build attempt running longer than `--bake-timeout` (2 hours) is killed and counts as
failed, and every failure is listed at the end with its log.  On SIGTERM the running builds are
stopped, and the run records what finished and reports the rest.

A distro that names a moving release (`debian-stable`, `debian-testing`, `ubuntu-lts`,
`ubuntu-rolling`) is built by the distro named after that release when there is one (`debian-trixie`
today), whose images are pushed under both names.  Its images then carry the release's codename in
their apt sources, not the alias.

An image is only rebuilt when apt would upgrade something in it.  For each image of a released
distro, the run pulls the image CI uses (`IMAGE/amd64`, standing for every arch) and asks its own apt
with `apt-get -s dist-upgrade`; nothing to upgrade means the image is left as it is.  sid's and
testing's images, and those built on them, are rebuilt on every run: some package of theirs changes
every day.  An image that can't be checked (not pushed yet, registry unreachable) is rebuilt.
`--no-cache` rebuilds everything without checking.

`./rebuild-docker-images.py --print` shows the bake definitions; `--distro debian-sid` and
`--arch amd64,i386` narrow a run.  A run that doesn't build every arch of an image pushes only its
`IMAGE/ARCH` tags and leaves the multi-arch `IMAGE` as it is, since CI pulls that on amd64 and
arm64.

### The builder

The script creates a buildx builder (`session-ci`, using the `docker-container` driver) on first
use, configured for the registry's plain HTTP.  On its own it builds the host's platforms (amd64 and
i386 on an x86 host).  The ARM images need either qemu (`apt install qemu-user-static
binfmt-support`), or, much faster, a native ARM machine added as a node:

    docker buildx create --append --name session-ci --platform linux/arm64,linux/arm/v7 \
        --buildkitd-config buildkitd.toml ssh://user@arm-host

where `buildkitd.toml` holds the same `[registry."reg.session.codes:80"] http = true` section the
script writes.  Each node pushes its own images, so the ARM host needs the VPN too.  When the
builder can't build a requested arch, the run builds the others and reports a failure.

### Nightly rebuilds

Every new image is a fresh pull on each CI runner that uses it, so images are refreshed only as often
as their release moves: nightly while Debian's archive calls it `unstable` or `testing` (sid and
forky today), weekly once it is released (forky turns weekly by itself when it becomes stable), and
`android` (2 GB) weekly; `EXTRAS` in `rebuild-docker-images.py` sets the extras' overrides.
With `--scheduled`, a run builds only the images due for a refresh; it records when each was last
built and pushed on every arch (or checked and found current) in `schedule.json`, under
`/var/lib/session-docker-ci` for the service, next to the script otherwise.  A run without
`--scheduled` builds everything it is asked to and leaves the schedule alone.

