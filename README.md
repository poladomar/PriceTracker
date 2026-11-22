# PriceTracker

A local Python price monitor with product and Shopify-style Pandora collection scanning, price history, Telegram notifications, and a searchable dashboard.

## Run

Python 3.12 recommended. Copy `config.example.json` to `config.json`, add item objects with `url`, `name`, `target_price`, or category objects with `url`, `min_drop_percent`, `max_pages`. For categories use a Pandora collection URL. Availability depends on the store exposing its catalogue.

Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in your shell environment (the tracker does not automatically load `.env`). Never commit credentials. Run `python pandora_tracker.py --once` or `python pandora_tracker.py` for continuous tracking. In another terminal run `python dashboard.py` and open http://127.0.0.1:8080. Local use only; no authentication on dashboard.

Tests: `python -m unittest -v`. Live store and Telegram require separate integration checks.
