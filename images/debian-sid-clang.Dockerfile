# Several clang/llvm releases.
FROM debian-sid
ARG APT
RUN $APT update \
    && $APT dist-upgrade -y \
    && $APT --no-install-recommends install -y \
        clang lld llvm \
        clang-19 lld-19 llvm-19 \
        clang-21 lld-21 llvm-21 \
        clang-22 lld-22 llvm-22 \
        libc++-dev \
        libc++abi-dev
