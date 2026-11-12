#!/usr/bin/env bash
set -eo pipefail
STATION_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROS_SETUP="${ECAT_ROS_SETUP:-/opt/ros/humble/setup.bash}"
ECAT_WORKSPACE="${ECAT_WORKSPACE:-/home/hby/Rolling_ZLT}"
if [[ -f "$ROS_SETUP" ]]; then
    source "$ROS_SETUP"
fi
if [[ -f "$ECAT_WORKSPACE/install/setup.bash" ]]; then
    source "$ECAT_WORKSPACE/install/setup.bash"
fi
export PYTHONPATH="$STATION_DIR${PYTHONPATH:+:$PYTHONPATH}"
exec /usr/bin/python3 -m ecat_groundstation "$@"
