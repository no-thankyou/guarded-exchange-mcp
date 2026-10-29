# Спайк 003: form-elicitation в Claude Code и Claude Desktop

Цель — руками проверить, как реальные клиенты обрабатывают подтверждение формой (тикет `docs/tickets/003-spike-client-elicitation.md`), и по фактам выбрать канал подтверждения для AC-1 (ADR-005). Исполнитель подготовил сервер и чек-лист; GUI клиентов открыть он не может, поэтому прогон делает пользователь.

## Что внутри
- `elicitation_probe.py` — stdio MCP-сервер (`mcp` 2.3.0) с одним инструментом `confirm_test_order(symbol="BTCUSDT", qty="0.001")`. Ничего не торгует и в сеть не ходит. Логика — шаблон ADR-005 / D-1 из спайка 002 (`build_dual_era_server()`):
  - протокол 2026-07-28 → MRTR: первый `tools/call` возвращает `InputRequiredResult` с формой (один boolean `confirm`), второй раунд распознаётся **только** по `requestState`; `inputResponses` без `requestState` → `invalid_params`;
  - протокол раньше 2026-07-28 (2025-11-25 и старше) → legacy push `elicitation/create` внутри того же вызова;
  - capability `elicitation` проверяется **до** создания pending; без неё — ошибка `confirmation_channel_unavailable`, формы не будет (голый `elicitation: {}` считается формой — так же, как в проверке самого SDK);
  - диагностический переключатель `SPIKE003_FORCE_ASK=1`: спрашивать даже без объявленной capability, чтобы увидеть, что такой клиент ответит (продукт так никогда не делает — fail closed);
  - подтверждено = `action == "accept"` **и** `content.confirm is True`, всё остальное — отказ.
- Каждый шаг — одна JSON-строка в stderr и, если задан `SPIKE003_LOG_FILE`, в этот файл (дописывается). Файл — главный источник правды: где клиенты хранят stderr серверов, отличается от клиента к клиенту.
- `test_elicitation_probe.py` — автоматический smoke-тест самого сервера (in-process `Client` с `elicitation_callback`: accept / decline / cancel / accept с `confirm=false` / без capability / оба протокола, плюс один настоящий stdio-процесс). Запуск: `uv run pytest spikes/003 -v`. Он доказывает, что сервер исправен, но не говорит ничего о Claude Code и Desktop — для этого ручной прогон ниже.

Проверить, что сервер стартует (должна появиться строка `server_start` и выход по закрытому stdin):

```bash
REPO=~/ai_engineer/portfolio/guarded-exchange-mcp
uv run --directory "$REPO" python spikes/003/elicitation_probe.py < /dev/null
```

## Как читать лог
| Событие | Что значит |
|---|---|
| `server_start` | процесс запущен; `mcp_version` |
| `tools_call` | вызов дошёл до инструмента: `protocol_version`, `client_info` (`name`, `version` — самоописание клиента), `client_capabilities_elicitation` (сырое: `null` / `{}` / `{"form": {}}` / `{"form": {}, "url": {}}`), `supports_form`, `has_request_state`, `input_responses` |
| `pending_created` | капабилити есть, «pending» создан (`p-1`, `p-2`, … в пределах процесса); `forced: true` — создан только из-за `SPIKE003_FORCE_ASK=1` |
| `input_required_sent` | MRTR: сервер вернул форму клиенту (раунд 1) |
| `legacy_elicit_sent` | legacy: сервер отправил `elicitation/create` внутри вызова |
| `elicitation_result` | что вернул клиент: `channel` (`mrtr` / `legacy_push`), `action` (`accept` / `decline` / `cancel`), `content` |
| `decision` | итог инструмента: `confirmed`, `decision` (`missing` — второй раунд без ответа по ключу `confirm`, `error` — клиент ответил ошибкой на legacy-запрос) |
| `elicitation_error` | legacy-запрос формы завершился ошибкой протокола (`code`, `message`) |
| `state_only_retry` | второй раунд пришёл с `requestState`, но без ответов — сервер переспросил про тот же pending |
| `tool_refused` | `confirmation_channel_unavailable` (клиент не объявил форму) или `invalid_params` |

Признаки:
- **MRTR работает** — два события `tools_call` на один вызов: первое с `"has_request_state": false`, второе с `true` и непустым `input_responses`; между ними `input_required_sent`; `protocol_version` = `2026-07-28`.
- **Только legacy** — `protocol_version` раньше `2026-07-28`, один `tools_call`, затем `legacy_elicit_sent` и `elicitation_result` с `"channel": "legacy_push"`.
- **Формы нет** — `tool_refused` с `confirmation_channel_unavailable` (капабилити не объявлена) либо `input_required_sent` / `legacy_elicit_sent` без последующего `elicitation_result` (клиент молча не ответил) — тогда запишите, что увидели в окне клиента.

## Claude Code

1. Версия клиента: `claude --version` — записать.
2. Подключить сервер (scope `local` по умолчанию — только для текущей папки; удобно открыть Claude Code в любой пустой папке). `--env` не ставить прямо перед именем сервера, поэтому между ними `--transport stdio`:

   ```bash
   REPO=~/ai_engineer/portfolio/guarded-exchange-mcp
   claude mcp add --env SPIKE003_LOG_FILE=/tmp/spike003-claude-code.log --transport stdio spike003 \
     -- uv run --directory "$REPO" python spikes/003/elicitation_probe.py
   claude mcp get spike003        # проверить конфигурацию
   ```

   Если ImportError из-за глобального `PYTHONPATH` — добавить `--env PYTHONPATH=` (пустое значение).
3. Запустить `claude`, командой `/mcp` убедиться, что `spike003` подключён. Командой `/hooks` проверить, что **нет** хука `Elicitation` (иначе он может ответить за вас, и результат будет про хук, а не про UI) — записать, что увидели.
4. Смотреть лог во втором терминале: `tail -f /tmp/spike003-claude-code.log`.
5. Подробный лог самого клиента (при необходимости): запустить `claude --debug-file /tmp/claude-debug.log` и искать в файле `spike003` / `elicitation`.
6. Пройти чек-лист ниже. Попросить модель: «Вызови инструмент confirm_test_order из сервера spike003». Сначала Claude Code может спросить разрешение на сам вызов инструмента — это не elicitation; разрешить и отметить в заметках.
7. Хук `Elicitation` (необязательно, последний сценарий): по документации https://code.claude.com/docs/en/hooks#elicitation хук может ответить на форму без диалога. Если хотите проверить — настройте по документации хук, который отвечает `accept`, повторите вызов и запишите, что пришло в `elicitation_result`. Это подтверждает риск из ADR-005: UI клиента может быть автоматизирован, поэтому FR-42 не доверяет ему на mainnet. После проверки хук удалить.
8. После спайка: `claude mcp remove spike003`.

По документации Claude Code (https://code.claude.com/docs/en/mcp, раздел «Elicitation modes Claude Code declares», прочитано 2026-10-27): на соединениях с протоколом 2026-07-28, а также на stdio/http-серверах из пользовательской конфигурации на более ранних ревизиях (с v2.1.287) он объявляет `elicitation: {form: {}, url: {}}`; на прочих соединениях ранних ревизий — голый `elicitation: {}` (только form). Это надо подтвердить логом: поле `client_capabilities_elicitation` в `tools_call`.

## Claude Desktop

1. Версия: меню Claude → About Claude (macOS) / Help → About (Windows) — записать.
2. Открыть конфиг: Settings → Developer → Edit Config. Расположение файла `claude_desktop_config.json`:
   - macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`;
   - Windows: `%APPDATA%\Claude\claude_desktop_config.json`;
   - Linux: официальной сборки Claude Desktop для Linux исполнитель подтвердить не смог. Если у вас неофициальная или новая сборка, путь обычно `~/.config/Claude/claude_desktop_config.json` — сверьте через Edit Config и запишите фактический путь.
3. GUI-приложение может не видеть `PATH` из shell, поэтому `uv` — абсолютным путём (`which uv`), путь к репозиторию — тоже абсолютный (без `~`):

   ```json
   {
     "mcpServers": {
       "spike003": {
         "command": "/ABS/PATH/TO/uv",
         "args": ["run", "--directory", "/ABS/PATH/TO/guarded-exchange-mcp", "python", "spikes/003/elicitation_probe.py"],
         "env": {"SPIKE003_LOG_FILE": "/tmp/spike003-claude-desktop.log", "PYTHONPATH": ""}
       }
     }
   }
   ```

   Если в файле уже есть `mcpServers`, добавить только ключ `spike003`.
4. Полностью перезапустить Desktop (выйти из приложения, а не закрыть окно). В новом чате проверить, что инструменты `spike003` видны (значок инструментов / коннекторов).
5. Логи: наш файл `/tmp/spike003-claude-desktop.log` (`tail -f`). Логи самого Desktop по документации MCP: macOS `~/Library/Logs/Claude/` (`mcp.log` и `mcp-server-spike003.log` со stderr сервера), Windows `%APPDATA%\Claude\logs\`. На Linux — записать, где нашли (если нашли).
6. Пройти чек-лист ниже той же фразой. Ожидание из исследования (`docs/research/clients-and-agent.md`, ADR-005): по открытому багу Desktop не объявляет elicitation или присылает фиктивный `decline` — это и надо подтвердить или опровергнуть. Если в сценарии 0 пришёл `tool_refused` с `confirmation_channel_unavailable`, добавьте в `env` конфига `"SPIKE003_FORCE_ASK": "1"`, перезапустите Desktop и повторите сценарии 0–5: так видно, рисует ли Desktop форму без объявленной capability и что отвечает (фиктивный `decline`, ошибка, тишина). В таблице отметьте такие строки «force».
7. После спайка удалить `spike003` из конфига и перезапустить Desktop.

## Чек-лист сценариев
Для каждого клиента. Сценарии делать последовательно и записывать время начала каждого — события в логе идут по порядку и имеют `ts` (UTC). Скриншот окна формы (если появилась) и итоговое сообщение модели.

| # | Сценарий | Что сделать | Что записать |
|---|---|---|---|
| 0 | Подключение | первый вызов `confirm_test_order` | `protocol_version`, `client_info`, `client_capabilities_elicitation`, `supports_form` из первого `tools_call`; показалась ли форма; как она выглядит (поле `confirm` чекбоксом? текст `message` виден целиком?) |
| 1 | accept + галочка | поставить `confirm`, нажать «принять / submit» | `elicitation_result.action`, `content`; `decision.confirmed` (ожидается `true`); MRTR или legacy (см. «Признаки») |
| 2 | accept без галочки | не ставить `confirm`, нажать «принять» | `content` (`{"confirm": false}`? `{}`? `null`?); `confirmed` должен быть `false`. Если клиент не даёт отправить без поля — записать |
| 3 | decline | кнопка «отклонить / decline» | `action` (ожидается `decline`), `content` |
| 4 | cancel | Esc или «отмена», если есть отдельная кнопка | `action` (`cancel` или `decline`?) — в Claude Code документация говорит, что Esc — это ответ серверу |
| 5 | Закрыть окно / прервать | Claude Code: прервать ход (Esc по запросу модели или Ctrl+C) пока форма открыта; Desktop: закрыть диалог крестиком или остановить генерацию | пришёл ли вообще `elicitation_result`; если MRTR — пришёл ли второй `tools_call`; что увидела модель; висит ли «pending» без ответа (последнее событие в логе) |
| 6 | Второй вызов | ещё раз вызвать инструмент в той же сессии | новый `pending_created` (`p-2`, …) и новая форма; тот же `protocol_version`; если пришёл `state_only_retry` или повтор без формы — записать |
| 7 | Новая сессия | перезапустить клиент и вызвать снова | не изменились ли `protocol_version` / capability |
| 8 | Хук `Elicitation` (только Claude Code, по желанию) | см. шаг 7 раздела Claude Code | `action`/`content` от хука; был ли показан диалог |

## Таблица результатов
Строка — одна возможность одного клиента. «Доказательство» — строка лога (событие + время) или имя скриншота.

| Клиент | Версия клиента | ОС | Дата проверки | Возможность | Результат | Доказательство |
|---|---|---|---|---|---|---|
| Claude Code | 2.1.312 | Linux Mint 22.3 | 2026-10-28 | `protocol_version` | `2026-07-28` во всех вызовах | `tools_call` 06:20:14 |
| Claude Code | 2.1.312 | Linux Mint 22.3 | 2026-10-28 | `clientInfo` (`name`, `version`) | `claude-code` / `2.1.312` | `tools_call` 06:20:14 |
| Claude Code | 2.1.312 | Linux Mint 22.3 | 2026-10-28 | объявляет `elicitation` (сырое значение) | `{"form": {}, "url": {}}`, `supports_form: true` | `tools_call` 06:20:14 |
| Claude Code | 2.1.312 | Linux Mint 22.3 | 2026-10-28 | рисует форму | да, в терминале: текст `message` и поле `confirm` чекбоксом. Галочка ставится пробелом, Enter отправляет, Esc отменяет. Как поставить галочку, с первого раза не очевидно (UX-риск) | слова пользователя |
| Claude Code | 2.1.312 | Linux Mint 22.3 | 2026-10-28 | MRTR (2 раунда `tools/call`) | да: на каждый вызов два `tools_call` (`has_request_state` false → true) с `input_responses.confirm`, `channel: mrtr` | `tools_call` 06:20:14 + 06:20:58 |
| Claude Code | 2.1.312 | Linux Mint 22.3 | 2026-10-28 | legacy push `elicitation/create` | не использовался (на 2026-07-28 сервер идёт через MRTR) | нет событий legacy в логе |
| Claude Code | 2.1.312 | Linux Mint 22.3 | 2026-10-28 | accept + `confirm=true` → что пришло | `accept`, `{"confirm": true}` → `confirmed: true` | `elicitation_result` p-3 06:23:47 |
| Claude Code | 2.1.312 | Linux Mint 22.3 | 2026-10-28 | accept без галочки → что пришло | `accept`, `{"confirm": false}` → `confirmed: false`; отправить без галочки можно | `elicitation_result` p-4 06:24:17 |
| Claude Code | 2.1.312 | Linux Mint 22.3 | 2026-10-28 | decline → что пришло | `decline`, `content: null` | `elicitation_result` p-2 06:23:04 |
| Claude Code | 2.1.312 | Linux Mint 22.3 | 2026-10-28 | Esc / cancel → что пришло | `cancel`, `content: null` (не `decline`) | `elicitation_result` p-1 06:20:58 |
| Claude Code | | | | закрыть / прервать → что пришло | не проверено | |
| Claude Code | 2.1.312 | Linux Mint 22.3 | 2026-10-28 | второй вызов | в той же сессии каждый вызов получил новый pending (p-1…p-4) и новую форму; `state_only_retry` не было | `pending_created` p-2..p-4 |
| Claude Code | | | | хук `Elicitation` отвечает без диалога | не проверено (необязательно) | |
| Claude Desktop | — | — | 2026-10-29 | все возможности | не проверено: у пользователя Linux, официального клиента Claude Desktop под Linux нет. До проверки считается клиентом без UI-канала (ADR-005) | — |

## Итог (2026-10-29)
- **Claude Code 2.1.312** на протоколе 2026-07-28 объявляет `elicitation: {"form": {}, "url": {}}`, рисует форму и ходит по MRTR (два `tools/call`, второй со `requestState`). Legacy push `elicitation/create` не нужен.
- Исходы различимы: `accept` + `confirm=true` — подтверждение; `accept` + `confirm=false` (форму можно отправить без галочки) — отказ; `decline` и `cancel` (Esc) приходят разными `action`, `content: null`.
- Каждый вызов получает новый pending и новую форму; ретраев «только со state» не было.
- UX-риск: в терминальной форме галочка ставится пробелом, это не очевидно — в `message` формы нужна подсказка «Пробел — отметить, Enter — отправить».
- Не проверено: закрытие/прерывание хода с открытой формой (при любом исходе, кроме подтверждения, действие не исполняется — путь безопасен по построению), хук `Elicitation`, Claude Desktop (нет клиента под Linux).
- Решение для AC-1 — в PLAN, ADR-005 «Спайк 003».

## Черновик для README проекта
> **Подтверждение в клиенте.** На testnet сервер может спросить подтверждение формой прямо в клиенте (MRTR, протокол 2026-07-28). Проверено в Claude Code 2.1.312 (Linux, 2026-10-28): в форме отметьте галочку `confirm` пробелом и нажмите Enter; Esc или отправка без галочки — отказ. Клиенты, которые не объявляют `elicitation.form` или работают на протоколе старше 2026-07-28, получают подтверждение через Telegram или CLI. Claude Desktop не проверялся; из-за известного бага с фиктивным `decline` для него рекомендуется `prefer_external_channel: true`. Хук Claude Code `Elicitation` может отвечать на форму без человека, поэтому на mainnet UI клиента не доверяется (FR-42).

## Что прислать обратно
1. Оба файла лога целиком: `/tmp/spike003-claude-code.log`, `/tmp/spike003-claude-desktop.log` (в них нет секретов: только версии клиентов, капабилити и ответы формы).
2. Версии: вывод `claude --version`, версия Claude Desktop из About, ОС.
3. Заполненную таблицу (или просто ответы по чек-листу в свободной форме — исполнитель перенесёт).
4. Скриншоты формы и итогового сообщения модели для сценариев 1, 3, 4, 5 (если формы не было — скриншот того, что было вместо неё).
5. Что увидели в `/hooks` Claude Code и, если делали сценарий 8, — какой хук настроили.
6. Всё неожиданное: ошибки подключения, двойные формы, повторные вызовы инструмента моделью, тексты ошибок.

По этим данным оркестратор (или следующий исполнитель) заполнит таблицу в PLAN (ADR-005), черновик для README и запишет решение для AC-1 по каждому клиенту: UI-канал MRTR, UI-канал legacy или только внешний канал.
