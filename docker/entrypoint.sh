#!/bin/sh
set -eu
case "${1:-runtime}" in
  runtime|serve)
    if [ "$#" -gt 0 ]; then shift; fi
    exec infiagent-runtime --host 0.0.0.0 --port 4260 --user-root "${INFIAGENT_USER_ROOT:-/data/user_root}" "$@"
    ;;
  cli)
    shift
    exec infiagent "$@"
    ;;
  *) exec "$@" ;;
esac
