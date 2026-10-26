#!/usr/bin/env bash
# Только чистые проверки функции распознавания ОС: без SSH/VPS и без сетевого доступа.
set -euo pipefail
source deploy/check-stage0-vps.sh

assert_os() {
  local os_id="$1" version="$2" expected="$3" actual
  actual="$(evaluate_os "$os_id" "$version")"
  case "$actual" in
    "$expected"*) ;;
    *) printf 'ОШИБКА: %s %s: ожидали %s, получили %s\n' "$os_id" "$version" "$expected" "$actual" >&2; exit 1 ;;
  esac
}

assert_os debian 13 PASS
assert_os debian 12 WARN
assert_os debian 11 FAIL
assert_os ubuntu 24.04 PASS
assert_os ubuntu 20.04 FAIL
assert_os ubuntu 26.10 FAIL
assert_os fedora 42 FAIL

printf 'Проверки baseline ОС завершены успешно.\n'
