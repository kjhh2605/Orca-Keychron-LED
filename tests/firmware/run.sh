#!/bin/sh
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
mkdir -p "$root/firmware/build"
cc -std=c11 -Wall -Wextra -Werror -pedantic -fsanitize=address,undefined -g \
  -I"$root/firmware/src" "$root/firmware/src/orca_led.c" "$root/tests/firmware/test_orca_led.c" \
  -o "$root/firmware/build/test_orca_led"
"$root/firmware/build/test_orca_led"
cc -std=c11 -Wall -Wextra -Werror -pedantic -fsanitize=address,undefined -g \
  -I"$root/firmware/src" "$root/firmware/src/orca_input.c" "$root/tests/firmware/test_orca_input.c" \
  -o "$root/firmware/build/test_orca_input"
"$root/firmware/build/test_orca_input"
