#!/bin/bash
bash "$(dirname "$0")/run.sh" setup
result=$?
if [ "$result" -ne 0 ]; then
  read -r -p "Setup did not finish. Press Return to close."
fi
exit "$result"
