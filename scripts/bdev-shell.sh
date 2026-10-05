# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.

# Source this file in Bash or Zsh, with scripts/ on PATH.
bdev() {
  if [ "$1" = cd ]; then
    local argument name="" count=0 valid=1
    for argument in "${@:2}"; do
      case "$argument" in
        --notify | --notify=*) ;;
        -*) valid=0 ;;
        *) name=$argument; count=$((count + 1)) ;;
      esac
    done
    if [ "$valid" -eq 1 ] && [ "$count" -eq 1 ]; then
      local destination
      destination=$(command bdev "$@") || return
      printf 'cd %q\n' "$destination" >&2
      builtin cd -- "$destination"
      return
    fi
  fi
  command bdev "$@"
}
