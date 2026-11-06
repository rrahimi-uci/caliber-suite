#!/usr/bin/env sh
set -eu
if [ "$#" -ne 2 ]; then
  echo "Usage: cron-entry.sh REPO_ROOT PYTHON" >&2
  exit 2
fi
repo_root=$1
python_bin=$2
cd "$repo_root"
exec "$python_bin" -m autocd.cli tick
