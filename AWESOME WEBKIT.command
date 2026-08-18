#!/bin/sh
set -eu
kit_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
exec "$kit_dir/launch-control-center.sh"
