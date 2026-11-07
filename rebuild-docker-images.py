#!/usr/bin/env python3
"""
Rebuilds the Session CI images from ./Dockerfile with `docker buildx bake`, and pushes them.

Each distro is one bake run, building its -base, -builder and final images on every arch plus the
images built on top of them (sid-clang, android, lint...).  Distros run in parallel and fail
independently, and a failed one is retried.  An image is only rebuilt when its distro's apt archives
have changed since the cached build (see archive_state), so a nightly run is cheap where nothing
moved and fresh where something did.
"""

import fcntl
import hashlib
import json
import optparse
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

import requests

KEEP_LOGS_DAYS = 30

parser = optparse.OptionParser()
parser.add_option("--registry", default="reg.session.codes:80",
                  help="Registry to push to [default: %default]")
parser.add_option("--registry-https", action="store_true",
                  help="The registry speaks HTTPS (by default it is plain HTTP, as ours is over the VPN)")
parser.add_option("--builder", default="session-ci",
                  help="buildx builder to use, created on first use [default: %default]")
parser.add_option("--cache-size", type="int", default=100,
                  help="GB of build cache the builder keeps, when it creates it [default: %default]")
parser.add_option("--distro", "-d", action="append", default=[],
                  help="Only build this distro, e.g. debian-sid, or one arch of it, e.g. debian-sid/amd64 "
                       "(repeatable).  The multi-arch tags are only pushed by a run that builds every arch")
parser.add_option("--debug", action="store_true",
                  help="Show the build output instead of writing it to log files; implies -j1")
parser.add_option("--arch", default="",
                  help="Only build these arches, comma separated: amd64,i386,arm64v8,arm32v7.  The multi-arch "
                       "tags are only pushed by a run that builds every arch")
parser.add_option("--parallel", "-j", type="int", default=3,
                  help="Build this many distros at once [default: %default]")
parser.add_option("--retries", type="int", default=2,
                  help="Retry a failed distro this many times [default: %default]")
parser.add_option("--retry-delay", type="int", default=60,
                  help="Seconds before the first retry; doubles on each further retry [default: %default]")
parser.add_option("--no-push", action="store_true", help="Build only, keeping the result in the build cache")
parser.add_option("--no-cache", action="store_true", help="Ignore the build cache")
parser.add_option("--log-dir", default=os.environ.get('LOGS_DIRECTORY', 'logs'),
                  help="Where each run writes its build logs; runs older than %d days are deleted "
                       "[default: $LOGS_DIRECTORY, else logs/ next to this script]" % KEEP_LOGS_DAYS)
parser.add_option("--print", action="store_true", dest="print_only",
                  help="Print the bake definitions and exit")
(options, args) = parser.parse_args()

here = os.path.dirname(os.path.abspath(__file__))
os.chdir(here)

# Two runs (a timer and a manual one, say) pushing the same tags break each other.
lockfile = open('.rebuild.lock', 'w')
try:
    fcntl.flock(lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    print("Another rebuild-docker-images.py run holds .rebuild.lock; not starting", file=sys.stderr)
    sys.exit(75)


def notify_discord(message):
    """Posts to the Discord webhook in $DISCORD_WEBHOOK_URL, if set."""
    url = os.environ.get('DISCORD_WEBHOOK_URL')
    if not url:
        return
    try:
        requests.post(url, json={"content": message[:2000]}, timeout=30).raise_for_status()
    except Exception as e:
        print(f"Discord notification failed: {e}", file=sys.stderr)


def notify_crash(exc_type, exc, tb):
    if not issubclass(exc_type, KeyboardInterrupt):
        trace = ''.join(traceback.format_exception(exc_type, exc, tb))[-1800:]
        notify_discord(f"**Session CI image rebuild crashed on {socket.gethostname()}**\n```\n{trace}\n```")
    sys.__excepthook__(exc_type, exc, tb)


sys.excepthook = notify_crash

printlock = threading.Lock()
started = time.time()


def say(message):
    with printlock:
        print(f"[{time.time() - started:6.0f}s] {message}", flush=True)


PLATFORMS = {'amd64': 'linux/amd64', 'i386': 'linux/386', 'arm64v8': 'linux/arm64', 'arm32v7': 'linux/arm/v7'}
DEBIAN_ARCHES = ['amd64', 'arm64v8', 'i386', 'arm32v7']
UBUNTU_ARCHES = ['amd64', 'arm64v8']

# name: (upstream image, deb.session.foundation suite, arches).  @SUITE@: see the Dockerfile.
DISTROS = {
    'debian-sid': ('debian:sid', 'sid', DEBIAN_ARCHES),
    'debian-stable': ('debian:stable', 'stable', DEBIAN_ARCHES),
    'debian-testing': ('debian:testing', 'testing', DEBIAN_ARCHES),
    'debian-forky': ('debian:forky', 'forky', DEBIAN_ARCHES),
    'debian-trixie': ('debian:trixie', 'trixie', DEBIAN_ARCHES),
    'debian-bookworm': ('debian:bookworm', 'bookworm', DEBIAN_ARCHES),
    'ubuntu-rolling': ('ubuntu:rolling', '@SUITE@', UBUNTU_ARCHES),
    'ubuntu-lts': ('ubuntu:latest', '@SUITE@', UBUNTU_ARCHES),
    'ubuntu-stonking': ('ubuntu:stonking', 'stonking', UBUNTU_ARCHES),
    'ubuntu-resolute': ('ubuntu:resolute', 'resolute', UBUNTU_ARCHES),
    'ubuntu-noble': ('ubuntu:noble', 'noble', UBUNTU_ARCHES),
    'ubuntu-jammy': ('ubuntu:jammy', 'jammy', UBUNTU_ARCHES),
}

# Images built on a distro's stages, after its own images: distro => units, each unit a list of
# (image, Dockerfile stage, arches) built together and failing together.  bake cancels the rest of a
# run when one target fails, so each unit has its own run, and a broken one costs only itself.
EXTRAS = {
    'debian-sid': [[('debian-sid-clang', 'sid-clang', ['amd64'])],
                   [('android', 'android', ['amd64']), ('flutter', 'flutter', ['amd64'])]],
    'debian-stable': [[('debian-win32-cross', 'win32-cross', ['amd64'])],
                      [('debian-stable-cross', 'stable-cross', ['amd64'])],
                      [('docbuilder', 'docbuilder', ['amd64'])]],
    'debian-forky': [[('debian-forky-s390x-cross', 's390x-cross', ['amd64'])]],
    'debian-bookworm': [[('lint', 'lint', ['amd64'])],
                        [('clone', 'clone', ['amd64', 'arm64v8'])]],
}


def packages_fingerprint(url):
    """The Packages checksums an InRelease file lists, or a marker for an archive that doesn't exist."""
    for attempt in range(3):
        try:
            r = requests.get(url, timeout=30)
            break
        except requests.RequestException as e:
            error = e
            time.sleep(5)
    else:
        raise RuntimeError(f"{url}: {error}")
    if r.status_code == 404:
        return f"{url} absent"
    r.raise_for_status()
    # Only the checksums of the Packages indexes: an InRelease re-signed with a new Date or
    # Valid-Until but nothing new in it must not count as a change.
    lines, in_sha256 = [], False
    for line in r.text.splitlines():
        if not line.startswith(' '):
            in_sha256 = line.startswith('SHA256:')
        elif in_sha256 and 'Packages' in line:
            lines.append(line.strip())
    return url + "\n" + "\n".join(lines)


def codename(distro):
    image, suite, _ = DISTROS[distro]
    if suite != '@SUITE@':
        return suite
    out = subprocess.run(['docker', 'run', '--rm', '--pull', 'always', image, 'sh', '-c',
                          '. /etc/os-release && echo "$VERSION_CODENAME"'],
                         check=True, capture_output=True, text=True)
    return out.stdout.strip()


def archive_state(distro):
    """
    A fingerprint of every apt archive the distro's images install from, used as the APT_STATE
    build argument: it changes exactly when there is something new to install.
    """
    c = codename(distro)
    if distro.startswith('debian-'):
        urls = [f"http://deb.debian.org/debian/dists/{c}/InRelease"]
        if c != 'sid':
            urls += [f"http://deb.debian.org/debian/dists/{c}-updates/InRelease",
                     f"http://security.debian.org/debian-security/dists/{c}-security/InRelease"]
    else:
        urls = [f"{base}/dists/{c}{pocket}/InRelease"
                for base in ("http://archive.ubuntu.com/ubuntu", "http://ports.ubuntu.com/ubuntu-ports")
                for pocket in ("", "-updates", "-security")]
    urls.append(f"https://deb.session.foundation/dists/{c}/InRelease")
    digest = hashlib.sha256()
    for url in urls:
        digest.update(packages_fingerprint(url).encode())
    return digest.hexdigest()[:16]


def image_targets(name, stage, arches, all_arches, args):
    """
    bake targets for one image: X/<arch> for each arch, which CI also uses directly, and the
    multi-arch X.  They share every layer, so BuildKit builds each only once.
    """
    common = {"context": ".", "dockerfile": "Dockerfile", "target": stage, "args": args, "pull": True}
    repo = f"{options.registry}/{name}"
    targets = {f"{name}-{a}": {**common, "platforms": [PLATFORMS[a]], "tags": [f"{repo}/{a}"]}
               for a in arches}
    # CI pulls X on ARM too: a run missing an arch must leave X alone rather than replace it with a
    # list that lacks that arch.
    if arches != all_arches:
        return targets
    if len(arches) == 1:
        targets[f"{name}-{arches[0]}"]["tags"].append(repo)
    else:
        targets[name] = {**common, "platforms": [PLATFORMS[a] for a in arches], "tags": [repo]}
    return targets


def bake_units(distro, arches, state):
    """The distro's bake definitions, in build order: its own images, then each extra unit."""
    image, suite, distro_arches = DISTROS[distro]
    args = {"DISTRO_IMAGE": image, "SESSION_SUITE": suite, "APT_STATE": state}
    units = [(distro, [(distro + suffix, stage, distro_arches)
                       for suffix, stage in (('-base', 'base'), ('-builder', 'builder'), ('', 'final'))])]
    units += [(unit[0][0], unit) for unit in EXTRAS.get(distro, [])]
    definitions = []
    for name, images in units:
        images = [(image_name, stage, [a for a in all_arches if a in arches], all_arches)
                  for image_name, stage, all_arches in images]
        if not all(image_arches for _, _, image_arches, _ in images):
            continue
        targets = {}
        for image_name, stage, image_arches, all_arches in images:
            targets.update(image_targets(image_name, stage, image_arches, all_arches, args))
        definitions.append((name, {"group": {"default": {"targets": list(targets)}}, "target": targets}))
    return definitions


def ensure_builder(rundir):
    """Creates the builder if it doesn't exist; returns the platforms it can build."""
    if subprocess.run(['docker', 'buildx', 'inspect', options.builder], capture_output=True).returncode != 0:
        config = os.path.join(rundir, 'buildkitd.toml')
        with open(config, 'w') as f:
            # Explicit, because BuildKit's defaults are shares of the disk: on a mostly full disk it
            # evicts the cache after every build, and then every run rebuilds everything.
            f.write(f'[worker.oci]\n  maxUsedSpace = "{options.cache_size}GB"\n'
                    f'  reservedSpace = "{options.cache_size // 2}GB"\n  minFreeSpace = "10GB"\n')
            if not options.registry_https:
                f.write(f'[registry."{options.registry}"]\n  http = true\n  insecure = true\n')
        # network=host so that a registry on localhost is the host's, not the builder container's.
        subprocess.run(['docker', 'buildx', 'create', '--name', options.builder, '--driver', 'docker-container',
                        '--driver-opt', 'network=host', '--buildkitd-config', config], check=True)
    out = subprocess.run(['docker', 'buildx', 'inspect', '--bootstrap', options.builder],
                         check=True, capture_output=True, text=True).stdout
    # A node's platforms set with `buildx create --platform` are printed with a trailing *.
    return {p.strip().rstrip('*') for line in out.splitlines() if line.strip().startswith('Platforms:')
            for p in line.split(':', 1)[1].split(',')}


# Without buildx's default provenance attestations, which add "unknown/unknown" platform entries to
# every pushed image list and that some clients list as an extra platform.
bake_env = {**os.environ, 'BUILDX_NO_DEFAULT_ATTESTATIONS': '1'}


def run_bake(name, definition, rundir):
    """Runs one bake, with retries.  Returns None on success, else its log."""
    bakefile = os.path.join(rundir, f"{name}.bake.json")
    with open(bakefile, 'w') as f:
        json.dump(definition, f, indent=2)
    log = os.path.join(rundir, f"{name}.log")
    cmd = ['docker', 'buildx', 'bake', '--builder', options.builder, '--file', bakefile, '--progress', 'plain',
           # Docker's v2 manifest formats rather than OCI's, as the registry has always served.
           '--set', '*.output=type=cacheonly' if options.no_push else '*.output=type=registry,oci-mediatypes=false',
           *(['--no-cache'] if options.no_cache else [])]
    began = time.time()
    for attempt in range(options.retries + 1):
        if attempt:
            delay = options.retry_delay * 2 ** (attempt - 1)
            say(f"{name}: failed, retrying in {delay}s (see {log})")
            time.sleep(delay)
        say(f"{name}: building {len(definition['target'])} targets" + (f", attempt {attempt + 1}" if attempt else ""))
        if options.debug:
            if subprocess.run(cmd, env=bake_env).returncode == 0:
                say(f"{name}: done in {time.time() - began:.0f}s")
                return None
            continue
        with open(log, 'a') as f:
            f.write(f"\n=== attempt {attempt + 1}: {' '.join(cmd)}\n")
            f.flush()
            if subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT, env=bake_env).returncode == 0:
                say(f"{name}: done in {time.time() - began:.0f}s")
                return None
    if options.debug:
        say(f"{name}: FAILED after {options.retries + 1} attempts, see the output above")
        return "(the output above)"
    say(f"{name}: FAILED after {options.retries + 1} attempts, see {log}")
    return log


def build_distro(distro, arches, rundir):
    """Builds a distro's images, then each of its extra units.  Returns its failures as (what, log)."""
    try:
        state = archive_state(distro)
    except Exception as e:
        # Unknown archives: rebuild rather than risk serving stale images.
        state = f"unknown-{time.time():.0f}"
        say(f"{distro}: archive state unavailable ({e}); rebuilding")
    say(f"{distro}: apt state {state}")
    (name, definition), *extras = bake_units(distro, arches, state)
    log = run_bake(name, definition, rundir)
    if log:
        return [(distro, log)] + [(f"{extra} (skipped: it builds on {distro})", None) for extra, _ in extras]
    return [(extra, log) for extra, definition in extras if (log := run_bake(extra, definition, rundir))]


def failure_report(failures):
    header = "\n".join([f"**Session CI image rebuild on {socket.gethostname()}: {len(failures)} failure(s)**"]
                       + [f"- `{what}`" + (f" (log: `{log}`)" if log else "") for what, log in failures])
    first_log = next((log for _, log in failures if log), None)
    if not first_log:
        return header
    with open(first_log, errors='replace') as f:
        lines = f.read().splitlines()
    # BuildKit's own summary of what failed, else the end of the log.
    errors = [l for l in lines if l.startswith('ERROR') or ' ERROR: ' in l] or lines[-25:]
    tail = "\n".join(errors[-25:])
    budget = 1900 - len(header)
    return header if budget < 200 else f"{header}\n```\n{tail[-budget:]}\n```"


def new_rundir():
    """This run's directory for its bake files and logs; deletes runs older than KEEP_LOGS_DAYS."""
    os.makedirs(options.log_dir, exist_ok=True)
    for entry in os.scandir(options.log_dir):
        if entry.is_dir() and entry.name.startswith('run-') \
                and time.time() - entry.stat().st_mtime > KEEP_LOGS_DAYS * 86400:
            shutil.rmtree(entry.path, ignore_errors=True)
    return os.path.abspath(tempfile.mkdtemp(prefix=f"run-{time.strftime('%Y%m%dT%H%M%S')}-", dir=options.log_dir))


def main():
    requested = {}  # distro => arches asked for with --distro
    for d in options.distro:
        name, _, arch = d.partition('/')
        if name not in DISTROS or (arch and arch not in PLATFORMS):
            parser.error(f"bad --distro {d!r}; distros: {', '.join(DISTROS)}; arches: {', '.join(PLATFORMS)}")
        requested.setdefault(name, set()).update([arch] if arch else PLATFORMS)
    wanted_arches = [a for a in options.arch.split(',') if a] or list(PLATFORMS)
    if not set(wanted_arches) <= set(PLATFORMS):
        parser.error(f"--arch takes {', '.join(PLATFORMS)}")
    distros = list(requested) or list(DISTROS)

    failures = []
    if options.print_only:
        supported = set(PLATFORMS.values())
    else:
        rundir = new_rundir()
        supported = ensure_builder(rundir)
        missing = [a for a in wanted_arches if PLATFORMS[a] not in supported]
        if missing:
            # Never skipped silently: a builder that lost its ARM node would otherwise stop
            # refreshing those images without anyone noticing.
            failures.append((f"builder {options.builder} cannot build {', '.join(missing)}; "
                             "add a node for them (see the README) or pass --arch", None))
            wanted_arches = [a for a in wanted_arches if a not in missing]

    plans = {d: [a for a in DISTROS[d][2] if a in wanted_arches and a in requested.get(d, PLATFORMS)]
             for d in distros}
    plans = {d: arches for d, arches in plans.items() if arches}

    if options.print_only:
        for d, arches in plans.items():
            for name, definition in bake_units(d, arches, "<apt state>"):
                print(f"# {name}")
                print(json.dumps(definition, indent=2))
        return 0

    say(f"building {len(plans)} distro(s) for {', '.join(wanted_arches)}; logs in {rundir}")
    if any(arches != DISTROS[d][2] for d, arches in plans.items()):
        say("not every arch is built, so the multi-arch tags are left as they are")
    with ThreadPoolExecutor(max_workers=1 if options.debug else max(options.parallel, 1)) as executor:
        results = {d: executor.submit(build_distro, d, arches, rundir) for d, arches in plans.items()}
    for future in results.values():
        failures += future.result()

    if failures:
        print("\nFailed:", file=sys.stderr)
        for what, log in failures:
            print(f"  {what}" + (f"  (log: {log})" if log else ""), file=sys.stderr)
        notify_discord(failure_report(failures))
        return 1
    say("all done")
    return 0


sys.exit(main())
