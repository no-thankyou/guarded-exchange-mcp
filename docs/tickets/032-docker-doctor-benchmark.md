# 032: Docker-запуск, doctor и NFR-4 benchmark
Статус: TODO
Зависит от: 019, 028
Требования: NFR-4, NFR-6, ADR-015, D-22, D-25

## Цель
Локальный режим запускается одной командой в Docker по stdio; `doctor` проверяет окружение и предупреждает об угрозах; benchmark измеряет накладные расходы.

## Scope
- Dockerfile, `docker run -i` для Claude Code/Desktop, named volume (D-11).
- `guarded-exchange doctor`: версия SQLite, права файлов, запуск вне Docker, CLI-канал, `allow_local_mainnet`, привязка Telegram.
- Benchmark NFR-4 по методу D-25; CI-job — сторож регрессии.

## Non-scope
- README (033).

## Acceptance criteria
- [ ] `doctor` выдаёт предупреждения на каждом проблемном пункте (unit-тесты на фейковом окружении).
- [ ] Benchmark выводит p95 и описание железа; CI-job зелёный с мягким порогом.
- [ ] Smoke: образ собирается, сервер отвечает на `tools/list` по stdio.

## Заметки
—
