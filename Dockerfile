# syntax=docker/dockerfile:1
#
# Every Session CI image, as stages of one Dockerfile.  rebuild-docker-images.py builds them with
# `docker buildx bake`, choosing the stage, platform and distro through build arguments.

ARG DISTRO_IMAGE=debian:sid
ARG APT="apt-get -o=Dpkg::Use-Pty=0 -o=Acquire::Retries=5 -q"


FROM ${DISTRO_IMAGE} AS base
ARG APT
# A fingerprint of the distro's apt archives: this layer, and every image built on it, is rebuilt
# when the archives have changed and taken from the cache when they have not.
ARG APT_STATE
RUN /bin/bash -c 'echo "man-db man-db/auto-update boolean false" | debconf-set-selections'
RUN echo "apt archive state: ${APT_STATE}" \
    && $APT update \
    && $APT dist-upgrade -y \
    && $APT autoremove -y


# (distro)-builder: the tools for building debs, without what a package's debian/control pulls in.
FROM base AS builder
ARG APT
RUN $APT update \
    && $APT dist-upgrade -y \
    && $APT --no-install-recommends install -y \
        ccache \
        devscripts \
        equivs \
        g++ \
        git \
        git-buildpackage \
        openssh-client


# (distro): the image most builds use, with most dependencies of our packages.
FROM builder AS final
ARG APT
# deb.session.foundation suite; @SUITE@ for ubuntu's lts/rolling, which are moving aliases the repo
# doesn't publish, so the image's own codename is used.  /etc/os-release rather than lsb_release,
# which isn't installed.
ARG SESSION_SUITE
COPY session-foundation.gpg /usr/share/keyrings/session-foundation.gpg
RUN suite="${SESSION_SUITE}" \
    && if [ "$suite" = "@SUITE@" ]; then . /etc/os-release && test -n "$VERSION_CODENAME" && suite="$VERSION_CODENAME"; fi \
    && printf 'Types: deb\nURIs: https://deb.session.foundation\nSuites: %s\nComponents: main\nSigned-By: /usr/share/keyrings/session-foundation.gpg\n' \
        "$suite" > /etc/apt/sources.list.d/session.sources
RUN $APT update \
    && $APT dist-upgrade -y \
    && $APT --no-install-recommends install -y \
        automake \
        ccache \
        cmake \
        curl \
        eatmydata \
        g++ \
        gdb \
        git \
        libboost-program-options-dev \
        libboost-serialization-dev \
        libboost-thread-dev \
        libcurl4-gnutls-dev \
        libevent-dev \
        libfmt-dev \
        libgnutls28-dev \
        libgtest-dev \
        libhidapi-dev \
        libjemalloc-dev \
        libminiupnpc-dev \
        libngtcp2-crypto-gnutls-dev \
        libngtcp2-dev \
        liboxenc-dev \
        liboxen-quic-dev \
        libprotobuf-dev \
        libreadline-dev \
        libsodium-dev \
        libspdlog-dev \
        libsqlite3-dev \
        libssl-dev \
        libsystemd-dev \
        libtool \
        libunbound-dev \
        libunwind8-dev \
        libusb-1.0.0-dev \
        libutf8proc-dev \
        libuv1-dev \
        libzmq3-dev \
        libzstd-dev \
        lsb-release \
        make \
        meson \
        nettle-dev \
        ninja-build \
        openssh-client \
        patch \
        pkg-config \
        postgresql-client \
        pybind11-dev \
        python3-coloredlogs \
        python3-cryptography \
        python3-dev \
        python3-flask \
        python3-nacl \
        python3-openssl \
        python3-pil \
        python3-pip \
        python3-protobuf \
        python3-psycopg2 \
        python3-pybind11 \
        python3-pycryptodome \
        python3-pytest \
        python3-qrencode \
        python3-setuptools \
        python3-sqlalchemy \
        python3-sqlalchemy-utils \
        python3-tabulate \
        python3-uwsgidecorators \
        qttools5-dev \
        sqlite3


# debian-sid-clang: debian-sid with sid's default clang/llvm and its oldest release.  The oldest is
# taken from apt rather than named, because sid drops old releases without notice.
FROM final AS sid-clang
ARG APT
RUN $APT update \
    && $APT dist-upgrade -y \
    && oldest=$(apt-cache pkgnames clang- | sed -n 's/^clang-\([0-9][0-9]*\)$/\1/p' | sort -n | head -1) \
    && test -n "$oldest" \
    && echo "oldest clang release: $oldest" \
    && $APT --no-install-recommends install -y \
        clang lld llvm \
        clang-$oldest lld-$oldest llvm-$oldest \
        libc++-dev \
        libc++abi-dev


# android: debian-sid-base with the Android SDK and NDK.
FROM base AS android
ARG APT
RUN /bin/bash -c 'sed -i "s/main/main non-free/g" /etc/apt/sources.list.d/debian.sources'
RUN $APT update \
    && $APT dist-upgrade -y \
    && $APT install --no-install-recommends -y \
        android-sdk \
        automake \
        ccache \
        cmake \
        curl \
        git \
        google-android-ndk-r29-installer \
        libtool \
        make \
        ninja-build \
        openjdk-21-jdk \
        openssh-client \
        patch \
        pkg-config \
        python3 \
        wget \
        xz-utils \
        zip \
    && update-java-alternatives -s java-1.21.0-openjdk-amd64 \
    && git clone https://github.com/Shadowstyler/android-sdk-licenses.git /tmp/android-sdk-licenses \
    && cp -a /tmp/android-sdk-licenses/*-license /usr/lib/android-sdk/licenses \
    && rm -rf /tmp/android-sdk-licenses


FROM android AS flutter
RUN cd /opt \
    && curl https://storage.googleapis.com/flutter_infra_release/releases/stable/linux/flutter_linux_3.7.8-stable.tar.xz \
        | tar xJ --no-same-owner \
    && ln -s /opt/flutter/bin/flutter /usr/local/bin/ \
    && flutter upgrade --force && flutter precache


# lint: debian-bookworm-base with the formatting tools.
FROM base AS lint
ARG APT
RUN $APT install --no-install-recommends -y \
    clang-format-14 \
    clang-format-15 \
    clang-format-16 \
    clang-format-19 \
    eatmydata \
    git \
    jsonnet


# debian-win32-cross: debian-stable-base with the mingw cross compiler.
FROM base AS win32-cross
ARG APT
RUN $APT install --no-install-recommends -y \
        autoconf \
        automake \
        build-essential \
        ca-certificates \
        ccache \
        cmake \
        eatmydata \
        file \
        g++-mingw-w64-x86-64 \
        git \
        gperf \
        libtool \
        make \
        ninja-build \
        nsis \
        openssh-client \
        patch \
        pkg-config \
        qttools5-dev \
        wine \
        zip \
    && update-alternatives --set x86_64-w64-mingw32-gcc /usr/bin/x86_64-w64-mingw32-gcc-posix \
    && update-alternatives --set x86_64-w64-mingw32-g++ /usr/bin/x86_64-w64-mingw32-g++-posix


# debian-stable-cross: debian-stable with cross compilers.
FROM final AS stable-cross
ARG APT
RUN $APT update \
    && $APT dist-upgrade -y \
    && $APT install -y \
        g++-aarch64-linux-gnu gcc-aarch64-linux-gnu \
        g++-arm-linux-gnueabihf gcc-arm-linux-gnueabihf \
        g++-mips-linux-gnu gcc-mips-linux-gnu \
        g++-mips64-linux-gnuabi64 gcc-mips64-linux-gnuabi64 \
        g++-mipsel-linux-gnu gcc-mipsel-linux-gnu \
        g++-powerpc64le-linux-gnu gcc-powerpc64le-linux-gnu


# docbuilder: debian-stable with the documentation tools.
FROM final AS docbuilder
ARG APT
RUN $APT update \
    && $APT dist-upgrade -y \
    && $APT install -y doxygen mkdocs curl zip unzip tar


# debian-forky-s390x-cross: s390x cross compiler with s390x dev libs, and qemu-user to run what it
# builds: s390x is the big-endian target we test on.  forky rather than testing so that this
# doesn't move when forky releases.  No session.foundation repo: it publishes nothing for s390x.
FROM base AS s390x-cross
ARG APT
RUN dpkg --add-architecture s390x \
    && $APT update \
    && $APT dist-upgrade -y \
    && $APT --no-install-recommends install -y \
        automake \
        build-essential \
        ca-certificates \
        ccache \
        cmake \
        crossbuild-essential-s390x \
        eatmydata \
        file \
        git \
        libcli11-dev \
        libtool \
        make \
        ninja-build \
        nlohmann-json3-dev \
        patch \
        pkg-config \
        qemu-user \
        libevent-dev:s390x \
        libfmt-dev:s390x \
        libgnutls28-dev:s390x \
        libngtcp2-crypto-gnutls-dev:s390x \
        libngtcp2-dev:s390x \
        libprotobuf-dev:s390x \
        libsimdutf-dev:s390x \
        libsodium-dev:s390x \
        libspdlog-dev:s390x \
        libsqlite3-dev:s390x \
        libutf8proc-dev:s390x \
        libzstd-dev:s390x \
        nettle-dev:s390x \
        pkgconf:s390x


# clone: the git clone step CI pipelines use, through gitcache.
FROM debian:bookworm-slim AS clone
ARG APT
ARG APT_STATE
RUN /bin/bash -c 'echo "man-db man-db/auto-update boolean false" | debconf-set-selections'
RUN echo "apt archive state: ${APT_STATE}" \
    && $APT update \
    && $APT install -y --no-install-recommends git git-lfs python3 python3-pip python3-portalocker python3-pytimeparse python3-coloredlogs bash \
    && pip3 install --break-system-packages https://github.com/seeraven/gitcache/releases/download/v1.0.22/gitcache-1.0.22-py3-none-any.whl \
    && ln -s gitcache /usr/local/bin/git \
    && $APT remove --autoremove --purge -y python3-pip \
    && $APT clean \
    && rm -rf /var/cache/apt/lists/*
COPY clone-scripts/ /usr/local/bin/
ENTRYPOINT ["/usr/local/bin/clone"]
