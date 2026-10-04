# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.

# Source this file in Bash or Zsh, with scripts/ on PATH.
bdev() {
  if [ "$#" -eq 2 ] && [ "$1" = cd ]; then
    case "$2" in
      -*) command bdev "$@" ;;
      *)
        local destination
        destination=$(command bdev "$@") || return
        printf 'cd %q\n' "$destination" >&2
        builtin cd -- "$destination"
        ;;
    esac
  else
    command bdev "$@"
  fi
}
