FROM ubuntu:22.04

ARG DEBIAN_FRONTEND=noninteractive
ARG GAMECRAFT_REPOSITORY=https://github.com/FreedomIntelligence/gamecraft-bench.git
ARG GAMECRAFT_COMMIT=a43347534374df9a0c1a6c001aa9380862783f6d
ARG GODOT_VERSION=4.6.2-stable
ARG TARGETARCH

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates curl ffmpeg git python3 unzip \
        xvfb xdotool x11-utils x11-xserver-utils \
        libxcursor1 libxinerama1 libxrandr2 libxi6 libgl1 libegl1 \
        libfontconfig1 libxkbcommon0 \
    && rm -rf /var/lib/apt/lists/*

RUN case "${TARGETARCH}" in \
        amd64) GODOT_ARCH=x86_64; GODOT_SHA256=30e6b6d141f0cd5bebd629ad1d0ef1324e60091bb20662d026b402ba58c59937 ;; \
        arm64) GODOT_ARCH=arm64; GODOT_SHA256=c9154154de14acb1f38a6c8618f01f4111ecbd1cdbcecd0a5151be42de2bd1c9 ;; \
        *) echo "unsupported Docker architecture: ${TARGETARCH}" >&2; exit 2 ;; \
    esac \
    && mkdir -p /opt/godot \
    && curl -fsSL \
        "https://github.com/godotengine/godot/releases/download/${GODOT_VERSION}/Godot_v${GODOT_VERSION}_linux.${GODOT_ARCH}.zip" \
        -o /tmp/godot.zip \
    && echo "${GODOT_SHA256}  /tmp/godot.zip" | sha256sum --check --strict \
    && unzip -q /tmp/godot.zip -d /opt/godot \
    && mv "/opt/godot/Godot_v${GODOT_VERSION}_linux.${GODOT_ARCH}" /opt/godot/godot \
    && chmod 0755 /opt/godot/godot \
    && ln -s /opt/godot/godot /usr/local/bin/godot \
    && rm /tmp/godot.zip

RUN git clone --filter=blob:none "${GAMECRAFT_REPOSITORY}" /opt/gamecraft \
    && git -C /opt/gamecraft checkout --detach "${GAMECRAFT_COMMIT}" \
    && test "$(git -C /opt/gamecraft rev-parse HEAD)" = "${GAMECRAFT_COMMIT}" \
    && rm -rf /opt/gamecraft/.git

ENV PYTHONPATH=/opt/gamecraft
ENV GAMECRAFT_BENCH_GODOT_BIN=/usr/local/bin/godot
ENV GAMECRAFT_BENCH_JUDGE=stub
ENV GODOT_SILENCE_ROOT_WARNING=1

WORKDIR /opt/gamecraft

CMD ["python3", "-m", "gamecraft_bench.verifier", "--help"]
