#!/usr/bin/env bash
# Read-only аудит Stage 0 для Debian VPS.
# Скрипт намеренно не использует sudo и не меняет состояние системы.

set -u

fail_count=0
unknown_count=0

usage() {
  cat <<'EOF'
Использование:
  bash deploy/check-stage0-vps.sh

Скрипт только читает локальное состояние и проверяет:
  - baseline ОС и Python;
  - наличие системного пользователя mailbot;
  - security APT sources и свежесть package lists;
  - синхронизацию времени;
  - наличие локального firewall;
  - количество слушающих TCP/UDP sockets без вывода адресов;
  - наличие и права целевых каталогов mail-organizer.

Коды завершения:
  0 — FAIL/UNKNOWN нет;
  2 — найден хотя бы один FAIL;
  3 — FAIL нет, но есть UNKNOWN.

Скрипт не выполняет sudo, apt update/upgrade, useradd/adduser, chmod/chown,
systemctl enable/start/restart и не читает содержимое credentials.
EOF
}

emit() {
  local status="$1"
  local check="$2"
  local detail="$3"
  printf '%-8s %-24s %s\n' "$status" "$check" "$detail"

  case "$status" in
    FAIL) fail_count=$((fail_count + 1)) ;;
    UNKNOWN) unknown_count=$((unknown_count + 1)) ;;
  esac
}

have() {
  command -v "$1" >/dev/null 2>&1
}

os_value() {
  local key="$1"
  awk -F= -v key="$key" '
    $1 == key {
      value = substr($0, index($0, "=") + 1)
      gsub(/^"/, "", value)
      gsub(/"$/, "", value)
      print value
      exit
    }
  ' /etc/os-release 2>/dev/null
}

check_os() {
  if [[ ! -r /etc/os-release ]]; then
    emit UNKNOWN os "нет доступа к /etc/os-release"
    return
  fi

  local os_id version major
  os_id="$(os_value ID)"
  version="$(os_value VERSION_ID)"
  major="${version%%.*}"

  if [[ "$os_id" != "debian" ]]; then
    emit WARN os "обнаружен ${os_id:-unknown} ${version:-unknown}; baseline проекта описан для Debian"
    return
  fi

  if [[ ! "$major" =~ ^[0-9]+$ ]]; then
    emit UNKNOWN os "Debian найден, но VERSION_ID не распознан"
    return
  fi

  case "$major" in
    13) emit PASS os "Debian 13 соответствует baseline новой установки" ;;
    12) emit WARN os "Debian 12 допустим только при подтверждённом LTS/security-контуре" ;;
    0|1|2|3|4|5|6|7|8|9|10|11)
      emit FAIL os "Debian ${version} ниже поддерживаемого baseline"
      ;;
    *)
      emit WARN os "Debian ${version} новее документированного baseline; требуется review"
      ;;
  esac

  if have dpkg; then
    emit INFO architecture "$(dpkg --print-architecture 2>/dev/null || printf 'unknown')"
  fi
}

check_python() {
  if ! have python3; then
    emit FAIL python "python3 не найден"
    return
  fi

  local version major minor
  version="$(python3 -c 'import sys; print(".".join(map(str, sys.version_info[:3])))' 2>/dev/null || true)"
  major="${version%%.*}"
  minor="${version#*.}"
  minor="${minor%%.*}"

  if [[ "$major" =~ ^[0-9]+$ && "$minor" =~ ^[0-9]+$ ]]; then
    if (( major > 3 || (major == 3 && minor >= 11) )); then
      emit PASS python "Python ${version} соответствует >=3.11"
    else
      emit FAIL python "Python ${version} ниже 3.11"
    fi
  else
    emit UNKNOWN python "версия python3 не распознана"
  fi

  if python3 -c 'import venv' >/dev/null 2>&1; then
    emit PASS python-venv "модуль venv доступен"
  else
    emit WARN python-venv "модуль venv недоступен"
  fi
}

check_mailbot() {
  if ! have getent; then
    emit UNKNOWN mailbot "getent недоступен"
    return
  fi

  local entry shell uid home
  entry="$(getent passwd mailbot 2>/dev/null || true)"
  if [[ -z "$entry" ]]; then
    emit WARN mailbot "системный пользователь mailbot отсутствует"
    return
  fi

  uid="$(printf '%s' "$entry" | cut -d: -f3)"
  home="$(printf '%s' "$entry" | cut -d: -f6)"
  shell="$(printf '%s' "$entry" | cut -d: -f7)"

  emit PASS mailbot "пользователь существует"
  if [[ "$uid" =~ ^[0-9]+$ && "$uid" -lt 1000 ]]; then
    emit PASS mailbot-uid "UID относится к системному диапазону"
  else
    emit WARN mailbot-uid "UID не выглядит системным; требуется inspect"
  fi

  case "$shell" in
    /usr/sbin/nologin|/sbin/nologin|/bin/false)
      emit PASS mailbot-shell "интерактивный вход отключён"
      ;;
    *)
      emit WARN mailbot-shell "shell не является nologin/false"
      ;;
  esac

  if [[ "$home" == "/var/lib/mail-organizer" ]]; then
    emit PASS mailbot-home "home соответствует /var/lib/mail-organizer"
  else
    emit WARN mailbot-home "home отличается от целевого /var/lib/mail-organizer"
  fi
}

check_apt_security() {
  if ! have apt; then
    emit UNKNOWN apt "apt недоступен"
    return
  fi

  local security_found=0
  if grep -RqsE '(security\.debian\.org|[[:alnum:]_.-]+-security)' \
      /etc/apt/sources.list /etc/apt/sources.list.d 2>/dev/null; then
    security_found=1
  fi

  if (( security_found == 1 )); then
    emit PASS apt-security "security source найден; строки source не выводятся"
  else
    emit WARN apt-security "security source не подтверждён"
  fi

  if have dpkg-query && dpkg-query -W -f='${Status}' unattended-upgrades 2>/dev/null \
      | grep -q '^install ok installed$'; then
    emit INFO unattended-upgrades "пакет установлен"
  else
    emit INFO unattended-upgrades "пакет не подтверждён как установлен"
  fi

  if have systemctl; then
    local enabled
    enabled="$(systemctl is-enabled apt-daily-upgrade.timer 2>/dev/null || true)"
    if [[ "$enabled" == "enabled" ]]; then
      emit INFO apt-daily-upgrade "timer enabled"
    else
      emit INFO apt-daily-upgrade "timer state: ${enabled:-unknown}"
    fi
  fi

  local newest now age_hours
  newest="$(find /var/lib/apt/lists -maxdepth 1 -type f -printf '%T@\n' 2>/dev/null \
      | sort -nr | head -n1 || true)"
  if [[ -n "$newest" ]]; then
    now="$(date +%s)"
    age_hours="$(awk -v now="$now" -v ts="$newest" 'BEGIN { printf "%d", (now-ts)/3600 }')"
    if (( age_hours <= 72 )); then
      emit PASS apt-lists "локальные package lists обновлялись примерно ${age_hours} ч назад"
    else
      emit WARN apt-lists "локальные package lists старше 72 ч: примерно ${age_hours} ч"
    fi
  else
    emit UNKNOWN apt-lists "не удалось определить свежесть локальных package lists"
  fi

  local upgradable
  upgradable="$(apt list --upgradable 2>/dev/null | tail -n +2 | sed '/^[[:space:]]*$/d' | wc -l | tr -d ' ')"
  emit INFO apt-upgradable "кандидатов на обновление по локальному cache: ${upgradable:-unknown}"
}

check_ntp() {
  if ! have timedatectl; then
    emit UNKNOWN ntp "timedatectl недоступен"
    return
  fi

  local synced
  synced="$(timedatectl show -p NTPSynchronized --value 2>/dev/null || true)"
  case "$synced" in
    yes) emit PASS ntp "системные часы синхронизированы" ;;
    no) emit WARN ntp "NTPSynchronized=no" ;;
    *) emit UNKNOWN ntp "состояние синхронизации не получено" ;;
  esac
}

check_firewall() {
  local confirmed=0

  if [[ -r /etc/ufw/ufw.conf ]] && grep -qs '^ENABLED=yes' /etc/ufw/ufw.conf; then
    emit PASS firewall "UFW enabled; правила не выводятся"
    confirmed=1
  fi

  if have systemctl && systemctl is-active --quiet nftables 2>/dev/null; then
    emit PASS firewall "nftables service active; правила не выводятся"
    confirmed=1
  fi

  if have systemctl && systemctl is-active --quiet firewalld 2>/dev/null; then
    emit PASS firewall "firewalld active; правила не выводятся"
    confirmed=1
  fi

  if (( confirmed == 0 )); then
    emit WARN firewall "локальный firewall не подтверждён; внешний/cloud firewall проверяется отдельно"
  fi

  if have ss; then
    local tcp_count udp_count
    tcp_count="$(ss -H -ltn 2>/dev/null | wc -l | tr -d ' ')"
    udp_count="$(ss -H -lun 2>/dev/null | wc -l | tr -d ' ')"
    emit INFO listening-sockets "TCP=${tcp_count:-unknown}, UDP=${udp_count:-unknown}; адреса и порты не выводятся"
  else
    emit UNKNOWN listening-sockets "ss недоступен"
  fi
}

check_path() {
  local path="$1"
  local expected_owner="$2"
  local expected_group="$3"
  local expected_mode="$4"
  local label="$5"

  if [[ ! -e "$path" ]]; then
    emit INFO "$label" "$path отсутствует"
    return
  fi

  if ! have stat; then
    emit UNKNOWN "$label" "$path существует, но stat недоступен"
    return
  fi

  local owner group mode
  owner="$(stat -c '%U' "$path" 2>/dev/null || true)"
  group="$(stat -c '%G' "$path" 2>/dev/null || true)"
  mode="$(stat -c '%a' "$path" 2>/dev/null || true)"

  if [[ "$owner" == "$expected_owner" && "$group" == "$expected_group" && "$mode" == "$expected_mode" ]]; then
    emit PASS "$label" "$path: owner/group/mode соответствуют expected state"
  else
    emit WARN "$label" "$path: actual owner/group/mode=${owner:-?}/${group:-?}/${mode:-?}; expected=${expected_owner}/${expected_group}/${expected_mode}"
  fi
}

check_secret_storage() {
  check_path /etc/mail-organizer root mailbot 750 config-dir
  check_path /etc/mail-organizer/credentials root mailbot 750 credentials-dir
  check_path /var/lib/mail-organizer mailbot mailbot 700 state-dir

  if [[ -d /etc/mail-organizer/credentials ]]; then
    local world_readable
    world_readable="$(find /etc/mail-organizer/credentials -type f -perm -004 2>/dev/null | wc -l | tr -d ' ')"
    if [[ "$world_readable" =~ ^[0-9]+$ ]]; then
      if (( world_readable == 0 )); then
        emit PASS credentials-perms "world-readable credential files не найдены; содержимое не читалось"
      else
        emit FAIL credentials-perms "найдено world-readable credential files: ${world_readable}; имена не выводятся"
      fi
    else
      emit UNKNOWN credentials-perms "не удалось проверить права credential files"
    fi
  fi
}

main() {
  if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    usage
    exit 0
  fi

  if (( $# != 0 )); then
    printf 'Неизвестный аргумент: %s\n\n' "$1" >&2
    usage >&2
    exit 64
  fi

  printf 'Stage 0 VPS read-only audit\n'
  printf '===========================\n'

  check_os
  check_python
  check_mailbot
  check_apt_security
  check_ntp
  check_firewall
  check_secret_storage

  printf '\nИтог: FAIL=%d UNKNOWN=%d\n' "$fail_count" "$unknown_count"

  if (( fail_count > 0 )); then
    exit 2
  fi

  if (( unknown_count > 0 )); then
    exit 3
  fi

  exit 0
}

main "$@"
