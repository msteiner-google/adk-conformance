# Runner image for nightly adk-monitor runs on Cloud Build.
# Bazel fetches the Python/Go/Rust toolchains hermetically; uv, Go and cargo
# are only here for tools/bump_sdks.py, which re-resolves dependency locks.
FROM debian:trixie-slim

ARG BAZELISK_VERSION=v1.29.0
ARG UV_VERSION=0.13.1
ARG GO_VERSION=1.26.6
ARG RUST_VERSION=1.98.0

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates curl git gcc g++ libc6-dev make patch python3 unzip xz-utils zip \
 && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL -o /usr/local/bin/bazel \
      "https://github.com/bazelbuild/bazelisk/releases/download/${BAZELISK_VERSION}/bazelisk-linux-amd64" \
 && chmod +x /usr/local/bin/bazel

RUN curl -fsSL "https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/uv-x86_64-unknown-linux-gnu.tar.gz" \
      | tar -xz -C /usr/local/bin --strip-components=1

RUN curl -fsSL "https://dl.google.com/go/go${GO_VERSION}.linux-amd64.tar.gz" | tar -xz -C /usr/local

ENV RUSTUP_HOME=/opt/rustup CARGO_HOME=/opt/cargo
RUN curl -fsSL https://sh.rustup.rs | sh -s -- -y --no-modify-path --profile minimal \
      --default-toolchain "${RUST_VERSION}" \
 && chmod -R a+rwX /opt/rustup /opt/cargo

ENV PATH=/usr/local/go/bin:/opt/cargo/bin:${PATH}

# Pre-download the Bazel binary pinned by .bazelversion at image build time.
ARG BAZEL_VERSION=9.3.0
RUN USE_BAZEL_VERSION="${BAZEL_VERSION}" bazel version >/dev/null

WORKDIR /workspace
