#!/bin/sh
set -eu
kit_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
exec python3 "$kit_dir/control-center/launch.py"
