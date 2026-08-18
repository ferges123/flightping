# FlightPingBot

Prywatny, asynchroniczny bot Telegram do sprawdzania planowanych odlotów i opóźnień przez [FlightAware AeroAPI](https://www.flightaware.com/commercial/aeroapi/).

Bot działa wyłącznie w prywatnych czatach. Dostęp nadaje administrator, a każdy zatwierdzony użytkownik przechowuje własny klucz AeroAPI. Klucze są szyfrowane przed zapisaniem w SQLite.

## Funkcje

- wnioski o dostęp zatwierdzane przez administratora;
- jednorazowe sprawdzanie lotów dla kodu IATA, np. `TFS`;
- monitoring lotniska z cyklicznym sprawdzaniem;
- alerty o lotach przekraczających skonfigurowany próg opóźnienia;
- osobny klucz AeroAPI dla każdego użytkownika;
- szyfrowanie kluczy Fernet i usuwanie wiadomości zawierającej klucz;
- limity i cooldown żądań do AeroAPI;
- audit log, statystyki użycia, backupy SQLite i automatyczne czyszczenie danych;
- uruchamianie jako utwardzona usługa systemd.

## Wymagania

- Python 3.11 lub nowszy;
- konto Telegram i token bota z BotFather;
- konto FlightAware z aktywnym AeroAPI;
- identyfikator Telegrama administratora;
- Linux z systemd — wymagany tylko do uruchomienia produkcyjnego.

## Instalacja deweloperska

```bash
cd /opt/flightping
python3 -m venv .venv
.venv/bin/pip install -e '.[test]'
mkdir -p config state
cp .env.example config/flightpingbot.env
```

Następnie uzupełnij `config/flightpingbot.env`. Nie umieszczaj tego pliku ani żadnych kluczy w repozytorium.

Uruchomienie:

```bash
.venv/bin/python -m flightpingbot
```

Testy:

```bash
.venv/bin/pytest -q
```

## Konfiguracja

Przykładowy plik znajduje się w [.env.example](/opt/flightping/.env.example).

| Zmienna | Znaczenie |
|---|---|
| `FPB_BOT_TOKEN` | Token bota Telegram z BotFather. |
| `FPB_CREDENTIALS_KEY` | Stabilny klucz Fernet do szyfrowania kluczy AeroAPI. |
| `FPB_ADMIN_USER_IDS` | Identyfikatory administratorów Telegrama, rozdzielone przecinkami. |
| `FPB_STATE_DIR` | Katalog bazy, backupów i danych stanu; domyślnie `/opt/flightping/state`. |
| `FPB_MONITOR_INTERVAL_MINUTES` | Odstęp między sprawdzeniami monitoringu. |
| `FPB_MONITOR_WINDOW_HOURS` | Zakres planowanych odlotów przekazywany do AeroAPI. |
| `FPB_MONITOR_DURATION_HOURS` | Maksymalny czas działania monitoringu. |
| `FPB_MIN_DELAY_MINUTES` | Minimalne opóźnienie uznawane za alert. |
| `FPB_TIMEZONE` | Strefa czasowa prezentowana w wiadomościach. |
| `FPB_MAX_ACTIVE_AIRPORTS` | Maksymalna liczba aktywnych lotnisk na użytkownika. |
| `FPB_DAILY_API_REQUEST_LIMIT` | Dzienny limit żądań; `0` wyłącza limit. |
| `FPB_MONTHLY_API_REQUEST_LIMIT` | Miesięczny limit żądań; `0` wyłącza limit. |
| `FPB_USAGE_WARNING_PERCENT` | Próg ostrzeżenia o wykorzystaniu limitu. |
| `FPB_USER_REQUEST_COOLDOWN_SECONDS` | Minimalny odstęp między ręcznymi sprawdzeniami użytkownika. |
| `FPB_*_RETENTION_DAYS` | Czas przechowywania obserwacji, audytu i logów API. |

### Generowanie klucza Fernet

Klucz musi być wygenerowany raz i zachowany przy kolejnych restartach. Zmiana klucza uniemożliwi odczyt zapisanych kluczy AeroAPI.

```bash
.venv/bin/python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Wygenerowaną wartość wpisz jako `FPB_CREDENTIALS_KEY` w pliku konfiguracyjnym.

## Użycie użytkownika

Wszystkie komendy działają w prywatnym czacie z botem.

1. Wyślij `/start` i poczekaj na zatwierdzenie.
2. Po zatwierdzeniu wyślij `/aeroapi` i wklej osobisty klucz AeroAPI.
3. Sprawdź konfigurację przez `/aeroapi status` lub `/aeroapi test`.

| Komenda | Działanie |
|---|---|
| `/start` | Złożenie wniosku lub pokazanie statusu dostępu. |
| `/check TFS` | Jednorazowe sprawdzenie planowanych odlotów. |
| `/monitor TFS` | Uruchomienie monitoringu lotniska. |
| `/status` | Pokazanie aktywnych monitoringów. |
| `/stop` | Zatrzymanie własnych monitoringów. |
| `/aeroapi` | Ustawienie lub zastąpienie klucza AeroAPI. |
| `/aeroapi status` | Pokazanie, czy klucz jest skonfigurowany. |
| `/aeroapi test` | Weryfikacja klucza przez endpoint konta AeroAPI. |
| `/aeroapi remove` | Usunięcie klucza i zatrzymanie monitoringu. |
| `/cancel` | Anulowanie bieżącego formularza. |
| `/help` | Pomoc i bieżące ustawienia. |
| `/hide` | Ukrycie klawiatury Telegrama. |

Kod lotniska musi być trzyznakowym kodem IATA ASCII, np. `WAW`, `TFS` albo `LHR`.

## Komendy administratora

Administratorzy są określeni przez `FPB_ADMIN_USER_IDS`.

| Komenda | Działanie |
|---|---|
| `/requests` | Lista oczekujących wniosków. |
| `/approve <user_id>` | Zatwierdzenie wniosku. |
| `/deny <user_id>` | Odrzucenie wniosku. |
| `/users` | Lista użytkowników i ich statusów. |
| `/revoke <user_id>` | Odebranie dostępu. |
| `/block <user_id>` | Zablokowanie użytkownika. |
| `/unblock <user_id>` | Przywrócenie dostępu. |
| `/checks [IATA]` | Ostatnie sprawdzenia. |
| `/checklog <check_id>` | Obserwacje dla konkretnego sprawdzenia. |
| `/usage [user_id]` | Statystyki wykorzystania AeroAPI. |
| `/alerts [IATA]` | Historia alertów. |
| `/audit [days]` | Zdarzenia audytowe. |
| `/admin_status` | Status użytkowników, monitoringu i bazy. |
| `/db_status` | Rozmiar bazy, WAL i backupy. |
| `/stopall` | Awaryjne zatrzymanie wszystkich monitoringów. |

Po `revoke` lub `block` aktywne monitoringi użytkownika są zatrzymywane, a rozpoczęte formularze nie mogą być dokończone.

## Uruchomienie przez systemd

Jednostka [flightpingbot.service](/opt/flightping/flightpingbot.service) zakłada instalację w `/opt/flightping` oraz konfigurację w `config/flightpingbot.env`.

Przykład instalacji jako usługa użytkownika:

```bash
mkdir -p ~/.config/systemd/user
cp flightpingbot.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now flightpingbot.service
systemctl --user status flightpingbot.service
```

Logi:

```bash
journalctl --user -u flightpingbot.service -f
```

Jeżeli usługa ma działać po wylogowaniu użytkownika systemowego, włącz linger dla tego użytkownika:

```bash
loginctl enable-linger "$USER"
```

Opcjonalny [flightpingbot-healthcheck.service](/opt/flightping/flightpingbot-healthcheck.service) pozwala systemd sprawdzić, czy główna usługa jest aktywna.

## Dane i backupy

Domyślnie dane są przechowywane w:

```text
state/flightpingbot.sqlite3
state/backups/
```

SQLite działa w trybie WAL. Przy każdym uruchomieniu baza, pliki `-wal` i `-shm` są zabezpieczane uprawnieniami `0600`, a katalog stanu `0700`. Konserwacja aplikacji tworzy do trzech backupów i usuwa stare rekordy zgodnie z ustawieniami retencji.

Sam backup nie zastępuje kopii na innym nośniku. W produkcji należy okresowo kopiować `state/backups/` do bezpiecznej lokalizacji.

## Bezpieczeństwo

- plik `config/flightpingbot.env` zawiera sekrety i musi pozostać poza Git;
- `FPB_CREDENTIALS_KEY` należy przechowywać niezależnie od repozytorium i backupu bazy;
- nie zmieniaj `FPB_CREDENTIALS_KEY` bez planu migracji zaszyfrowanych danych;
- bot nie powinien być używany w grupach ani kanałach;
- użytkownicy są identyfikowani przez numeryczne Telegram user ID, nie przez nazwy `@username`;
- wiadomość zawierająca klucz jest usuwana po odebraniu, ale uprawnienia Telegrama lub chwilowy błąd API mogą uniemożliwić usunięcie — wtedy bot ostrzega użytkownika;
- po zmianie kodu uruchom testy i sprawdź logi usługi.

## Weryfikacja przed wdrożeniem

```bash
.venv/bin/pytest -q
systemd-analyze verify flightpingbot.service flightpingbot-healthcheck.service
```

W CI workflow `.github/workflows/security-audit.yml` uruchamia `pip-audit` oraz testy automatyczne.
