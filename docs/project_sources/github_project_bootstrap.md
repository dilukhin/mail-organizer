---
document_type: github_project_bootstrap
version: 2.0
status: active
language: ru
updated_at: 2026-09-07
---

# GitHub: bootstrap проекта mail-organizer

## Настройка

```yaml
target_repository:
  full_name: dilukhin/mail-organizer
  default_branch: main
knowledge_repository:
  full_name: dilukhin/github-connector-knowledge
  default_branch: main
  runtime_bundle_path: dist/projects/dilukhin__mail-organizer.md
  incident_directory: src/incidents/dilukhin__mail-organizer/
```

Это основной GitHub bootstrap-файл в источниках ChatGPT Project `mail-organizer`.

## Начало GitHub-задачи

1. Использовать GitHub Connector первым.
2. Не запускать `git` или `gh` как пробу удалённого доступа.
3. Через Connector прочитать `runtime_bundle_path`.
4. Проверить, что bundle относится к `dilukhin/mail-organizer`.
5. Применять bundle как основной рабочий GitHub-регламент.
6. Затем прочитать актуальные `README.md`, `ROADMAP.md`, `AGENTS.md` и затронутые документы целевого репозитория.
7. Если bundle недоступен, использовать минимальные правила ниже и явно сообщить о fallback.

## Новое наблюдение

В central knowledge base записывать только проблему/обход, которых нет в runtime bundle, либо изменившееся поведение известного общего правила GitHub Connector/API.

Обычные ошибки и решения, специфичные для `mail-organizer`, остаются в target repository и не являются knowledge-base incidents.

```text
knowledge repository
→ branch incident/YYYYMMDD-mail-organizer-<fingerprint>
→ один новый YAML incident
→ commit → PR → checks → merge → read-back
```

Прямое изменение `main` knowledge repository по умолчанию запрещено. Policy, catalog, schemas, profiles и существующие incidents меняются отдельным PR с ручной проверкой.

## Fallback при невозможности записи

1. Не переходить автоматически к `git`/`gh` в ChatGPT Web.
2. Зафиксировать требуемую Connector-операцию и точную ошибку.
3. Создать в чате `pending_github_incident_<UTC>_mail-organizer_<fingerprint>.yaml`.
4. Дать ссылку и явно указать, что incident ожидает импорта.
5. При восстановлении доступа проверить дубликат по fingerprint и импортировать через PR.

Минимальные поля fallback:

```yaml
fallback_schema_version: 1
status: pending_import
target:
  knowledge_repository: dilukhin/github-connector-knowledge
  base_branch: main
  intended_directory: src/incidents/dilukhin__mail-organizer/
source:
  repository: dilukhin/mail-organizer
  task: ""
  head_sha: null
connector_failure:
  required_operation: ""
  attempted_method: ""
  error: ""
  observed_at: ""
incident:
  id: ""
  fingerprint: ""
  observed_at: ""
  operation: ""
  symptom: ""
  classification: ""
  cause:
    status: confirmed | unconfirmed | not_investigated
    description: ""
  attempts: []
  resolution: []
  verification: ""
  reusable_rule: ""
import:
  duplicate_check_required: true
  publish_via_pull_request: true
```

Не включать tokens, cookies, private keys, authorization headers, OAuth secrets, содержимое личных писем и другие чувствительные данные.

## Минимальные аварийные правила

- Connector — первичный remote-транспорт.
- Локальный Git — только подтверждённая локальная копия, diff и тесты.
- Многофайловая публикация: `blob → tree → commit → ref`.
- Перед `update_ref` перечитать HEAD; после write выполнить один GitHub-side read-back.
- Не force-update `main`, protected branch, чужую ветку или ветку с неизвестными commits.
- После 4xx/truncation изменить стратегию, не повторять идентичный запрос.
- Combined status не доказывает отсутствие Actions.
- Для self-review использовать `COMMENT`.

## Завершение GitHub-задачи

Сообщить, появились ли новые замечания/обходы, и дать ссылку на incident/PR либо fallback-файл.

При отсутствии новых переносимых наблюдений написать:

> Новых неучтённых замечаний по работе с GitHub не выявлено.
