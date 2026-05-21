# ben-ran-watcher

Следит за организатором [БЕН РАН на Timepad](https://ben-ran.timepad.ru/events/) и присылает в Telegram уведомления о новых активных мероприятиях.

Использует официальный API Timepad (`api.timepad.ru/v1/events`) — это надёжнее HTML-парсинга.

## Что считается «новым»

Уникальный ID = `event.id` из Timepad. Уведомление приходит, когда:
- появилось новое мероприятие, у которого `ends_at` ≥ сейчас (т.е. ещё не прошло)
- ранее закрытое (прошедшее) снова стало активным (если организатор перенёс дату)

## Первая настройка

См. шаги в README репозитория [`um-mos-watcher`](https://github.com/tatianajagoda-debug/um-mos-watcher) — последовательность та же:

1. `git push` файлов в этот репо
2. `Settings → Secrets and variables → Actions` → добавить `TELEGRAM_TOKEN` и `TELEGRAM_CHAT_ID`
3. `Actions → Check ben-ran.timepad.ru events → Run workflow` — первый прогон записывает базу без уведомлений

Дальше cron запускает проверку каждые 20 минут.

## Если что-то ломается

- Лог в `Actions` показывает `Fetched: N, active: M, new: K`. Если `Fetched=0` или ошибка про API — Timepad сменил API или потребовал авторизацию (тогда нужно добавить `Bearer` токен; см. https://dev.timepad.ru)
- Telegram-ошибка появится с кодом и текстом
