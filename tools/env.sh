# Source me (bash/zsh):  source tools/env.sh
# Puts the user-space KiCad 10 toolchain (installed by scripts/install.sh into tools/kicad-root) on PATH.
# If tools/kicad-root does not exist, a system KiCad (>= 9, kicad-cli on PATH) is used instead.
_LLMPCB_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)"
_KR="$_LLMPCB_ROOT/tools/kicad-root"
if [ -d "$_KR/usr/bin" ]; then
  export LLMPCB_KICAD_ROOT="$_KR"
  export LD_LIBRARY_PATH="$_KR/usr/lib/x86_64-linux-gnu:$_KR/usr/lib:$_KR/usr/lib/kicad${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
  export PATH="$_KR/usr/bin:$PATH"
  export KICAD_STOCK_DATA_HOME="$_KR/usr/share/kicad"
  _KS="$_KR/usr/share/kicad"
else
  _KS="${KICAD_STOCK_DATA_HOME:-/usr/share/kicad}"
fi
export KICAD10_SYMBOL_DIR="${KICAD10_SYMBOL_DIR:-$_KS/symbols}"
export KICAD10_FOOTPRINT_DIR="${KICAD10_FOOTPRINT_DIR:-$_KS/footprints}"
export KICAD10_TEMPLATE_DIR="${KICAD10_TEMPLATE_DIR:-$_KS/template}"
export KICAD10_3DMODEL_DIR="${KICAD10_3DMODEL_DIR:-$_KS/3dmodels}"
export KICAD_CONFIG_HOME="${KICAD_CONFIG_HOME:-$_LLMPCB_ROOT/tools/kicad-config}"
unset _KR _KS
