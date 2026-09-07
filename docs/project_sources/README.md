# ChatGPT Project Sources

Для ChatGPT Web Project `Mail Organizer` используется двухуровневая схема, чтобы не упираться в лимит поля Instructions.

## Поле Project Instructions

Скопировать целиком содержимое:

`docs/PROJECT_INSTRUCTIONS.md`

Файл намеренно короткий и укладывается в лимит 8000 символов.

## Project Sources

Добавить два файла:

1. `project_instructions_full.md` — расширенный устойчивый регламент проекта;
2. `github_project_bootstrap.md` — bootstrap доступа к целевому GitHub и центральной knowledge base.

README/ROADMAP/AGENTS и остальные документы не нужно дублировать в Project Sources: при GitHub-задаче модель читает их актуальные версии через Connector.

## Имя проекта

Рекомендуемое имя: `Mail Organizer`.

## После создания Project

Проверочный первый запрос:

> Прочитай Project Sources и через GitHub Connector проверь bootstrap для `dilukhin/mail-organizer`. Ничего не изменяй. Кратко перечисли текущие архитектурные ограничения, активную стадию ROADMAP и путь к runtime bundle knowledge base.

Ожидается read-only ответ без попыток `git`/`gh`.
