# Stage 0: подготовка VPS

## Назначение

Этот документ описывает безопасное завершение инфраструктурной части Stage 0 перед переходом к v0.1.

Он не подтверждает текущее состояние конкретного VPS. Источником истины для VPS является только свежий read-back самого сервера.

Цель Stage 0 на VPS:

- подтвердить поддерживаемую ОС и Python;
- проверить security-update контур, синхронизацию времени и firewall;
- подготовить отдельного непривилегированного пользователя `mailbot`;
- определить безопасные каталоги state/config/credentials;
- не подключать Gmail/Outlook write scopes и не запускать почтовую обработку.

## 1. Сначала только read-only аудит

Из checkout репозитория на VPS:

```bash
bash deploy/check-stage0-vps.sh
```

Скрипт:

- не использует `sudo`;
- не обновляет package lists и пакеты;
- не создаёт пользователей или каталоги;
- не меняет firewall/systemd;
- не читает содержимое credentials;
- не выводит IP-адреса, hostname или firewall rules;
- при недостатке прав возвращает `UNKNOWN`, а не повышает привилегии.

Коды завершения:

- `0` — нет `FAIL` и `UNKNOWN`;
- `2` — найден хотя бы один `FAIL`;
- `3` — `FAIL` нет, но часть состояния не удалось подтвердить.

Вывод аудита является эксплуатационной диагностикой. Его не следует коммитить в Git или публиковать целиком без проверки на инфраструктурные сведения.

## 2. Expected state до любых изменений

Перед mutation-задачей должны быть явно подтверждены:

### ОС и Python

- новая установка: Debian 13 stable;
- существующий Debian 12 допустим только в поддерживаемом Debian LTS/security-контуре;
- Debian 11 и старше — stop condition;
- Python >= 3.11;
- используется поддерживаемый пакетный Python дистрибутива;
- `venv` доступен.

Если фактическая ОС не соответствует baseline, upgrade/migration оформляется отдельной задачей. Нельзя смешивать её с установкой `mail-organizer`.

### Пользователь

Целевое состояние:

- пользователь: `mailbot`;
- системный UID;
- интерактивный shell отключён через `nologin`/equivalent;
- home/state root: `/var/lib/mail-organizer`;
- пользователь не получает `sudo`.

Если `mailbot` уже существует, нельзя слепо выполнять повторный `adduser/useradd`. Сначала нужно сравнить actual state с expected state.

### Каталоги

Целевой минимум:

| Путь | Owner | Group | Mode | Назначение |
|---|---|---|---|---|
| `/etc/mail-organizer` | `root` | `mailbot` | `0750` | конфигурация |
| `/etc/mail-organizer/credentials` | `root` | `mailbot` | `0750` | OAuth/client credentials |
| `/var/lib/mail-organizer` | `mailbot` | `mailbot` | `0700` | SQLite, cursors, runtime state |

Credential files внутри `/etc/mail-organizer/credentials` должны быть доступны только root и группе `mailbot`; world-readable запрещён. Конкретные значения credentials никогда не входят в Git, обычные журналы или audit output.

## 3. Security updates

Read-only аудит должен подтвердить:

- наличие Debian security source;
- свежесть локального APT cache;
- количество доступных обновлений по локальному cache;
- состояние `unattended-upgrades`/timer, если они используются.

Сам аудит намеренно не выполняет `apt update` и `apt upgrade`: это уже изменение внешнего состояния и отдельная задача.

Если package lists устарели, следующий план должен отдельно описать:

1. expected state;
2. выполнение одного `apt update`;
3. read-back источников/доступных обновлений;
4. только затем решение об upgrade.

## 4. NTP

Перед эксплуатацией `mail-worker` требуется подтверждённая синхронизация времени.

`NTPSynchronized=no` — не повод автоматически запускать или перенастраивать службу. Сначала определить используемый механизм (`systemd-timesyncd`, `chrony` или другой), затем подготовить отдельное bounded change.

## 5. Firewall и слушающие сервисы

Read-only аудит:

- пытается подтвердить активный UFW/nftables/firewalld;
- не печатает rules;
- считает listening TCP/UDP sockets без вывода адресов и портов.

Отсутствие подтверждённого локального firewall не означает автоматически небезопасную конфигурацию: фильтрация может находиться в control-plane провайдера VPS. Но Gate 0 нельзя считать подтверждённым, пока фактический firewall scope не понятен.

Любое изменение firewall выполняется отдельной задачей с:

- точным текущим способом доступа;
- перечнем необходимых портов;
- checkpoint/rollback;
- защитой от потери SSH-доступа;
- read-back после одной атомарной операции.

## 6. Будущая последовательность mutation-задач

После успешного read-only inventory изменения следует выполнять не одним bootstrap-скриптом, а отдельными атомарными шагами.

### Шаг A: `mailbot`

Перед действием:

- подтвердить, что пользователь отсутствует либо уже полностью соответствует expected state;
- зафиксировать используемый Debian-инструмент создания system user;
- убедиться, что операция не меняет существующего пользователя.

После действия:

- `getent passwd mailbot`;
- проверить UID, home и shell;
- не продолжать при расхождении.

### Шаг B: runtime state directory

Создать только `/var/lib/mail-organizer` с ожидаемым owner/group/mode и перечитать через `stat`.

### Шаг C: config/credentials directories

Создавать `/etc/mail-organizer` и `credentials` отдельно. После каждой операции — `stat` и сравнение с expected state.

Фактические OAuth secrets на этом этапе не требуются.

### Шаг D: security/NTP/firewall

Изменять только те элементы, где read-only audit показал конкретное несоответствие. Нельзя выполнять «универсальное hardening» без фактического scope.

## 7. Stop conditions

Исходную mutation-задачу нужно остановить и перейти к read-only диагностике, если:

- версия ОС или Python неожиданна;
- `mailbot` уже существует, но отличается от expected state;
- целевые каталоги уже существуют с неизвестным owner/mode;
- обнаружены world-readable credential files;
- неизвестно, где фактически реализован firewall;
- NTP не синхронизирован и используемая служба не определена;
- actual state после любой операции отличается от expected state;
- возникает риск потерять SSH-доступ.

Никаких blind retry, `userdel`, force, reset, cleanup или компенсирующих destructive actions до понимания причины.

## 8. Критерий завершения инфраструктурной части Stage 0

Пункт Stage 0 можно закрыть только после свежего VPS read-back, который подтверждает:

1. ОС/Python соответствуют baseline.
2. `mailbot` существует и соответствует минимальным привилегиям.
3. Security update contour понятен и работоспособен.
4. Время синхронизировано.
5. Firewall scope определён.
6. State/config/credential directories имеют ожидаемые права.
7. Secrets отсутствуют в Git и не выводятся в обычные журналы.
8. Никакие Gmail/Outlook write scopes или почтовые mutation actions ещё не включены.
