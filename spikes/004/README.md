# Спайк 004: Bybit Demo Trading

Ручной скрипт, в CI не запускается (нужны ключ и сеть). Результаты — раздел «Спайк 004» в `docs/PLAN.md`, фикстуры — `tests/fixtures/bybit/spike004/` (описание каждого запроса — `index.json`: среда, хост, метод, путь, параметры без авторизации, HTTP-статус, заголовки лимитов `X-Bapi-Limit*`, время).

## Запуск

```bash
# ключ demo без права вывода; файл 0600 с BYBIT_DEMO_API_KEY / BYBIT_DEMO_API_SECRET
# (опционально BYBIT_TESTNET_API_KEY / BYBIT_TESTNET_API_SECRET для classic testnet)
uv run python spikes/004/bybit_demo_probe.py --read-only ~/.config/guarded-exchange/bybit-demo.env   # только чтение
uv run python spikes/004/bybit_demo_probe.py ~/.config/guarded-exchange/bybit-demo.env               # полный прогон
# или GX_SPIKE_ENV_FILE=<путь> uv run python spikes/004/bybit_demo_probe.py
uv run pytest spikes/004 -v   # офлайн-проверки скрипта и записанных фикстур
```

Полный прогон перезаписывает фикстуры. `demo_apply_money.json` записан только в первом прогоне (пополнение делается, если USDT < 1000).

## Предохранители
- Хосты — жёсткий allowlist `api-demo.bybit.com` / `api-testnet.bybit.com`; mainnet и любой другой хост → `ForbiddenHostError` до открытия сокета.
- Ключ читается из файла по пути; ключ, секрет, подпись и заголовки запроса не печатаются и не сохраняются; всё, что пишется в stdout и фикстуры, проходит `scrub` (поля `apiKey`, `secret`, `id`, `note`, `ips`, `userID`, `parentUid`, `inviterID`, `affiliateID` и любая строка, содержащая ключ/секрет). В конце — скан каталога фикстур на значения ключа/секрета (печатаются только имена файлов и длины).
- Торговля только на `BTCUSDT` (linear и spot), минимальный объём; если на символе уже есть открытые ордера или позиция — скрипт не торгует. `finally`: cancel-all по символу в обеих категориях, закрытие linear-позиции reduce-only IOC, отчёт об остатке (открытые ордера linear/spot, позиции linear, дельта BTC на споте).
- Настройка «Set Limit Price Behaviour» для фьючерсов на время теста переключается в reject и возвращается в исходное значение.
