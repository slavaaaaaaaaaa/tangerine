#!/bin/bash
# Build the archive server from its Rust source tarball.
#   ./build.sh /path/to/server-source.tar.gz
# Output: build/tangerine-server. Uses a local cargo if present, otherwise podman/docker.
set -euo pipefail
cd "$(dirname "$0")"
tarball=${1:?usage: ./build.sh /path/to/server-source.tar.gz}
src=build/src
rm -rf "$src"
mkdir -p "$src"
tar -xzf "$tarball" -C "$src" --strip-components=1
[ -f "$src/Cargo.toml" ] || { echo "no Cargo.toml at the top of $tarball"; exit 1; }
bin=$(sed -n 's/^name *= *"\(.*\)"/\1/p' "$src/Cargo.toml" | head -1)

if command -v cargo >/dev/null; then
    (cd "$src" && cargo build --release)
else
    engine=$(command -v podman || command -v docker) || { echo "need cargo, podman or docker"; exit 1; }
    "$engine" run --rm -v "$PWD/$src":/src:Z -w /src docker.io/library/rust:1 cargo build --release
fi
cp "$src/target/release/$bin" build/tangerine-server
echo "built build/tangerine-server"
