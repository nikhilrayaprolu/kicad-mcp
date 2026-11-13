#!/usr/bin/env bash
# One-command, no-sudo installer for Pcb-mcp (llm-pcb).
#   1. pixi environment (Python 3.12, Java 25, mcp, numpy, shapely, ...)
#   2. KiCad 10 (kicad-cli + stock symbol/footprint libraries) unpacked into tools/kicad-root
#      from the official KiCad PPA, plus the shared libraries it needs (Ubuntu 24.04 / x86_64)
#   3. Freerouting 2.5 jar
#   4. the C++ router (router_cpp/hroute)
# Re-running is safe; finished steps are skipped.  Set SKIP_KICAD=1 to use a system KiCad (>= 9) instead.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

say "1/4 pixi environment"
if ! command -v pixi >/dev/null; then
  echo "pixi not found - installing it into ~/.pixi (user space)"
  curl -fsSL https://pixi.sh/install.sh | bash
  export PATH="$HOME/.pixi/bin:$PATH"
fi
pixi install

if [ "${SKIP_KICAD:-0}" != "1" ] && [ ! -x tools/kicad-root/usr/bin/kicad-cli ]; then
  say "2/4 KiCad 10 (user space, from ppa:kicad/kicad-10.0-releases)"
  command -v dpkg-deb >/dev/null && command -v apt-get >/dev/null || {
    echo "dpkg-deb/apt-get not available: install KiCad >= 9 yourself and re-run with SKIP_KICAD=1"; exit 1; }
  mkdir -p tools/debs tools/kicad-root
  PPA=https://ppa.launchpadcontent.net/kicad/kicad-10.0-releases/ubuntu
  DIST="${KICAD_DIST:-noble}"
  curl -fsSL "$PPA/dists/$DIST/main/binary-amd64/Packages.gz" | gunzip > tools/debs/Packages
  for pkg in kicad kicad-symbols kicad-footprints; do
    f=$(awk -v p="$pkg" '$1=="Package:"{cur=$2} $1=="Filename:" && cur==p {print $2; exit}' tools/debs/Packages)
    [ -n "$f" ] || { echo "package $pkg not found in PPA index"; exit 1; }
    [ -f "tools/debs/$(basename "$f")" ] || curl -fsSL -o "tools/debs/$(basename "$f")" "$PPA/$f"
  done
  ( cd tools/debs && apt-get download \
      libglu1-mesa libnng1 libodbc2 libspnav0 libocct-foundation-7.6t64 libocct-modeling-data-7.6t64 \
      libocct-modeling-algorithms-7.6t64 libocct-data-exchange-7.6t64 libocct-ocaf-7.6t64 libocct-visualization-7.6t64 \
      libwxbase3.2-1t64 libwxgtk3.2-1t64 libwxgtk-gl3.2-1t64 libwxgtk-webview3.2-1t64 libngspice0 \
      libfreeimage3 libmbedcrypto7t64 libmbedtls14t64 libmbedx509-1t64 libpcre2-32-0 )
  for d in tools/debs/*.deb; do dpkg-deb -x "$d" tools/kicad-root; done
fi
# shellcheck disable=SC1091
source tools/env.sh
kicad-cli version >/dev/null 2>&1 || { echo "kicad-cli does not run - check missing libraries with: ldd \$(which kicad-cli) | grep 'not found'"; exit 1; }
echo "kicad-cli $(kicad-cli version 2>/dev/null | tail -1)"

say "3/4 Freerouting 2.5"
mkdir -p tools/freerouting
[ -f tools/freerouting/freerouting-2.5.0.jar ] || curl -fsSL -o tools/freerouting/freerouting-2.5.0.jar \
  https://github.com/freerouting/freerouting/releases/download/v2.5.0/freerouting-2.5.0.jar

say "4/4 C++ router (hroute)"
g++ -O3 -march=native -std=c++17 -o router_cpp/hroute router_cpp/hroute.cpp

say "smoke test"
pixi run test
cat <<MSG

Installed.  Register the MCP server with Claude Code:
  claude mcp add -s user pcb-mcp -- bash -c 'source $ROOT/tools/env.sh && exec $ROOT/.pixi/envs/default/bin/python -m llmpcb.server'
MSG
