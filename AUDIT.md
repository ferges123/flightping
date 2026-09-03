# Raport z audytu bezpieczeństwa i jakości kodu: FlightPingBot

**Data audytu:** 2026-09-03  
**Audytowany komponent:** FlightPingBot (`/opt/flightping`)  
**Wersja:** 0.0.1  
**Środowisko:** Linux (x86_64), Python 3.13.5, SQLite 3 (WAL mode)  
**Weryfikacja ustaleń:** Statyczna analiza kodu, testy jednostkowe/integracyjne (100 passed), audyt zależności `pip-audit`, analiza jednostek `systemd`

---

## 1. Podsumowanie wykonawcze (Executive Summary)

Aplikacja **FlightPingBot** jest asynchronicznym botem Telegram z lekkim panelem webowym (Starlette/Uvicorn), integrującym się z FlightAware AeroAPI w celu monitorowania opóźnień lotów na wybranych lotniskach.

Architektura projektu wykazuje wiele dojrzałych wzorców:
- Poprawnie zaimplementowane szyfrowanie symetryczne Fernet dla kluczy użytkowników ([`CredentialCipher`](file:///opt/flightping/src/flightpingbot/credentials.py#L10-L25)).
- Skuteczne zarządzanie współbieżnością pojedynczego połączenia SQLite poprzez blokadę asynchroniczną ([`Database.write_lock`](file:///opt/flightping/src/flightpingbot/database.py#L58)) oraz dekorator transakcyjny ([`serialized_write`](file:///opt/flightping/src/flightpingbot/repositories/base.py#L13-L28)).
- W 100% sparametryzowane zapytania SQL (brak podatności na SQL Injection).
- Konsekwentna sanityzacja wyjścia HTML w panelu WWW i w wiadomościach Telegram.

Mimo to wykryto **8 potwierdzonych defektów logiki/bezpieczeństwa** oraz **3 problemy konfiguracyjno-architektoniczne**. Wśród nich znajduje się krytyczny błąd uniemożliwiający działanie systemd healthcheck po zabezpieczeniu panelu tokenem, brak odporności na różnorodne formaty stref czasowych (prowadzący do awarii zapytań o loty) oraz brak ochrony CSRF w panelu administracyjnym przy domyślnych ustawieniach.

---

## 2. Macierz podatności i defektów

| ID | Tytuł | Kategoria | Priorytet | Wpływ |
|---|---|---|---|---|
| **FPB-01** | Awaria usługi healthcheck po włączeniu `FPB_WEB_AUTH_TOKEN` | Operacyjny / Deployment | **Wysoki** | Fałszywy alarm jednostki healthcheck |
| **FPB-02** | Nieobsłużony `TypeError` przy odejmowaniu dat w `_normalize()` | Stabilność / Obsługa danych | **Wysoki** | Awaria sprawdzania lotów |
| **FPB-03** | Maskowanie błędów sieciowych w `user_facing_error()` | UX / Obsługa błędów | **Średni** | Ekspozycja surowych błędów / brak lokalizacji |
| **FPB-04** | Brak retencji osieroconych rekordów `checks` ze statusem `running` | Zarządzanie danymi / SQLite | **Średni** | Wieczny wyciek danych w bazie |
| **FPB-05** | Brak ochrony CSRF i nagłówków `Origin` w panelu WWW bez tokena | Bezpieczeństwo / WWW | **Średni** | Nieautoryzowane akcje via CSRF |
| **FPB-06** | Pomijanie preferencji użytkownika w panelu WWW (`start`/`remonitor`) | Logika biznesowa | **Średni** | Wymuszanie domyślnych wartości serwera |
| **FPB-07** | Ryzyko błędu `TelegramBadRequest` w komendach administracyjnych | Stabilność Telegram API | **Niski** | Brak odpowiedzi bota przy długiej liście |
| **FPB-08** | Błąd pakowania pliku statycznego `logo.jpeg` w instalacji standardowej | Packaging / Setup | **Niski** | HTTP 500 na endpoint `/logo.jpeg` |
| **FPB-09** | Ograniczenie zapisu dla niestandardowego `FPB_STATE_DIR` w unit service systemd | Konfiguracja | **Niski** | Niedostępny niestandardowy katalog stanu |
| **FPB-10** | Niekompletny plik `.env.example` względem `config.py` | Konfiguracja / Dokumentacja | **Niski** | Utrudniona konfiguracja limitów |
| **FPB-11** | Nieograniczony wzrost struktur pamięci w `InboundSecurityMiddleware` | Zasoby / Wyciek pamięci | **Niski** | Powolny wzrost zużycia RAM |

---

## 3. Szczegółowy opis ustaleń audytu

### FPB-01: Awaria usługi systemd healthcheck po włączeniu `FPB_WEB_AUTH_TOKEN`
* **Lokalizacja:** [scripts/web_healthcheck.py:L7-L11](file:///opt/flightping/scripts/web_healthcheck.py#L7-L11), [flightpingbot-healthcheck.service:L8-L9](file:///opt/flightping/flightpingbot-healthcheck.service#L8-L9), [src/flightpingbot/web.py:L77-L85](file:///opt/flightping/src/flightpingbot/web.py#L77-L85)
* **Mechanizm:**
  Skrypt [`scripts/web_healthcheck.py`](file:///opt/flightping/scripts/web_healthcheck.py#L1-L12) pobiera status panelu za pomocą standardowej biblioteki Pythona:
  ```python
  urllib.request.urlopen(f"http://{host}:{port}/", timeout=10)
  ```
  Gdy administrator ustawi zalecany w dokumentacji `FPB_WEB_AUTH_TOKEN`, oprogramowanie pośredniczące [`AdminTokenMiddleware`](file:///opt/flightping/src/flightpingbot/web.py#L58-L85) w Starlette wymaga autoryzacji HTTP Basic lub Bearer. Nieautoryzowane zapytanie ze skryptu zwraca status HTTP `401 Unauthorized`.
  W `urllib.request`, kody błędów HTTP (w tym 401) rzucają wyjątek `urllib.error.HTTPError`. Blok `except Exception as exc:` w skrypcie przechwytuje ten błąd i wywołuje `sys.exit(1)`.
* **Skutek:** Wdrożony timer systemd `flightpingbot-healthcheck.timer` co 5 minut uruchamia serwis `flightpingbot-healthcheck.service`, który nieustannie zgłasza błąd (`failed`), generując fałszywe alerty w monitoringu systemowym i dziennikach `journalctl`.
* **Rekomendowane rozwiązanie:**
  Skrypt powinien:
  1. Odczytywać `os.getenv("FPB_WEB_AUTH_TOKEN")` i dołączać nagłówek `Authorization: Bearer <token>`, lub
  2. Traktować kod `401 Unauthorized` jako dowód, że serwer WWW żyje i odpowiada na zapytania, lub
  3. Udostępnić dedykowany, niechroniony endpoint `/healthz` w Starlette zwracający `200 OK`.

---

### FPB-02: Nieobsłużony `TypeError` przy odejmowaniu dat w `AeroAPI._normalize()`
* **Lokalizacja:** [src/flightpingbot/aeroapi.py:L106-L111](file:///opt/flightping/src/flightpingbot/aeroapi.py#L106-L111)
* **Mechanizm:**
  W metodzie [`AeroAPI._normalize()`](file:///opt/flightping/src/flightpingbot/aeroapi.py#L100-L129):
  ```python
  if scheduled and estimated:
      try:
          if delay is None:
              delay = max(0, int((datetime.fromisoformat(estimated.replace("Z", "+00:00")) - datetime.fromisoformat(scheduled.replace("Z", "+00:00"))).total_seconds() // 60))
      except ValueError:
          pass
  ```
  W przypadku, gdy upstream AeroAPI zwróci jedną datę ze strefą czasową (np. `2026-09-03T12:00:00Z` -> `+00:00`), a drugą bez strefy (np. `2026-09-03T12:00:00`), próba odjęcia obiektu *offset-aware* od *offset-naive* wywołuje w Pythonie:
  ```text
  TypeError: can't subtract offset-naive and offset-aware datetimes
  ```
  Kod przechwytuje wyłącznie `except ValueError:`. Co więcej, pętla [`scheduled_departures`](file:///opt/flightping/src/flightpingbot/aeroapi.py#L93) przechwytuje wyłącznie `(httpx.HTTPError, ValueError)`.
* **Skutek:** Wyjątek `TypeError` powoduje przerwanie przetwarzania odpowiedzi z API. W [`FlightService.check`](file:///opt/flightping/src/flightpingbot/monitor.py#L113-L125) sprawdzanie zostaje oznaczone statusem `ERROR`, a użytkownik otrzymuje komunikat o błędzie zamiast listy lotów.
* **Rekomendowane rozwiązanie:**
  Wzorując się na bezpiecznej implementacji z [formatting.py:L18-L22](file:///opt/flightping/src/flightpingbot/formatting.py#L18-L22), należy znormalizować obie daty do UTC oraz rozszerzyć klauzulę przechwytywania:
  ```python
  try:
      dt_est = datetime.fromisoformat(estimated.replace("Z", "+00:00"))
      dt_sch = datetime.fromisoformat(scheduled.replace("Z", "+00:00"))
      if dt_est.tzinfo is None: dt_est = dt_est.replace(tzinfo=timezone.utc)
      if dt_sch.tzinfo is None: dt_sch = dt_sch.replace(tzinfo=timezone.utc)
      if delay is None:
          delay = max(0, int((dt_est - dt_sch).total_seconds() // 60))
  except (TypeError, ValueError, OverflowError):
      pass
  ```

---

### FPB-03: Maskowanie błędów sieciowych w `user_facing_error()` i ekspozycja surowych komunikatów
* **Lokalizacja:** [src/flightpingbot/errors.py:L57-L66](file:///opt/flightping/src/flightpingbot/errors.py#L57-L66), [src/flightpingbot/aeroapi.py:L97](file:///opt/flightping/src/flightpingbot/aeroapi.py#L97), [src/flightpingbot/formatting.py:L44](file:///opt/flightping/src/flightpingbot/formatting.py#L44)
* **Mechanizm:**
  W [`aeroapi.py`](file:///opt/flightping/src/flightpingbot/aeroapi.py#L97) błąd jest przekształcany do łańcucha znaków:
  ```python
  return [], getattr(last_error, "status_code", None), latency, str(last_error), retries
  ```
  Zwrócony string trafia do `CheckResult.error`, a następnie jest przekazywany do [`user_facing_error(result.error, language)`](file:///opt/flightping/src/flightpingbot/formatting.py#L44).
  Funkcja [`user_facing_error`](file:///opt/flightping/src/flightpingbot/errors.py#L29-L67) próbuje weryfikować typy:
  ```python
  if isinstance(exc, (httpx.TimeoutException, TimeoutError)): ...
  if isinstance(exc, (httpx.NetworkError, httpx.RemoteProtocolError)): ...
  ```
  Jednak gdy argumentem jest `str`, wszystkie warunki `isinstance` zwracają `False`.
* **Skutek:**
  1. Użytkownicy anglojęzyczni widzą surowe, techniczne komunikaty wyjątków biblioteki `httpx` (np. `"Read timed out"` lub `"[Errno 111] Connection refused"`).
  2. Użytkownicy polskojęzyczni zawsze trafiają do ogólnego fallbacku: `"Nie udało się wykonać żądania. Spróbuj ponownie później."` zamiast dedykowanego i przyjaznego komunikatu o przekroczeniu czasu oczekiwania.
* **Rekomendowane rozwiązanie:**
  Sprawdzać treść ciągu tekstowego w `user_facing_error` (np. `if "timed out" in message:` lub `"network" in message`) albo zachowywać oryginalny obiekt wyjątku w strukturze `CheckResult`.

---

### FPB-04: Brak retencji osieroconych rekordów `checks` ze statusem `running`
* **Lokalizacja:** [src/flightpingbot/maintenance.py:L24-L28](file:///opt/flightping/src/flightpingbot/maintenance.py#L24-L28), [src/flightpingbot/repositories/checks.py:L13](file:///opt/flightping/src/flightpingbot/repositories/checks.py#L13)
* **Mechanizm:**
  Metoda [`create_check`](file:///opt/flightping/src/flightpingbot/repositories/checks.py#L11-L15) tworzy wpis z `status='running'` i `finished_at=NULL`.
  W przypadku nagłego zatrzymania aplikacji (restart maszyny, awaria zasilania, SIGKILL systemd, błąd OOM) przed wywołaniem [`finish_check`](file:///opt/flightping/src/flightpingbot/repositories/checks.py#L43-L45), pole `finished_at` pozostaje równe `NULL`.
  Reguła retencji w [maintenance.py](file:///opt/flightping/src/flightpingbot/maintenance.py#L24-L28) zawiera warunek:
  ```sql
  DELETE FROM checks WHERE finished_at < ? ...
  ```
  W logice trójwartościowej SQL (3VL) `NULL < ?` ewaluuje się do `UNKNOWN`/`NULL` (czyli fałszu w klauzuli `WHERE`).
* **Skutek:** Przerwane operacje nigdy nie zostaną usunięte z bazy danych przez cykliczne zadanie czyszczące. Rekordy te na stałe widnieją jako aktywne w panelu administracyjnym oraz w podsumowaniu `/admin_status`.
* **Rekomendowane rozwiązanie:**
  1. W [`recover_monitors_after_restart`](file:///opt/flightping/src/flightpingbot/repositories/monitors.py#L31-L40) dodać procedurę aktualizującą wiszące rekordy `checks WHERE status='running'` na status `error` lub `interrupted` z ustawieniem `finished_at = utcnow()`.
  2. Zmodyfikować zapytanie retencji:
     ```sql
     DELETE FROM checks WHERE COALESCE(finished_at, started_at) < ? ...
     ```

---

### FPB-05: Brak ochrony CSRF i weryfikacji nagłówków `Origin` w panelu WWW bez tokena
* **Lokalizacja:** [src/flightpingbot/web.py:L262-L266](file:///opt/flightping/src/flightpingbot/web.py#L262-L266), [src/flightpingbot/web.py:L256](file:///opt/flightping/src/flightpingbot/web.py#L256)
* **Mechanizm:**
  W metodzie [`valid_csrf`](file:///opt/flightping/src/flightpingbot/web.py#L262-L266):
  ```python
  async def valid_csrf(self, request) -> bool:
      if not self.auth_token:
          return True
  ```
  Jeśli `FPB_WEB_AUTH_TOKEN` nie został skonfigurowany, ochrona CSRF jest wyłączana. Formularze nie zawierają ukrytego pola `csrf_token`, a serwer nie weryfikuje nagłówków `Origin` ani `Referer`.
* **Skutek:** Jeśli administrator korzysta z przeglądarki na tym samym hoście, złośliwa strona WWW może w tle przesłać formularz POST pod `http://127.0.0.1:8080/actions/user/<id>/blocked` lub `/actions/access/<id>/deny` (klasyczny atak Drive-by CSRF na localhost).
* **Rekomendowane rozwiązanie:**
  Generować losowy token sesyjny/CSRF per instancję serwera nawet przy wyłączonym `FPB_WEB_AUTH_TOKEN` lub weryfikować nagłówek `Origin`/`Sec-Fetch-Site` dla żądań modyfikujących stan (POST).

---

### FPB-06: Pomijanie preferencji użytkownika w panelu WWW (`start_monitor` i `remonitor`)
* **Lokalizacja:** [src/flightpingbot/web.py:L471](file:///opt/flightping/src/flightpingbot/web.py#L471), [src/flightpingbot/web.py:L499](file:///opt/flightping/src/flightpingbot/web.py#L499)
* **Mechanizm:**
  W bocie Telegram, uruchomienie monitoringu ([handlers/monitoring.py:L370-L376](file:///opt/flightping/src/flightpingbot/handlers/monitoring.py#L370-L376)) uwzględnia personalne preferencje użytkownika z bazy (`window_hours`, `interval_minutes`, `min_delay_minutes`, `duration_hours`).
  W panelu WWW:
  * [`start_monitor`](file:///opt/flightping/src/flightpingbot/web.py#L455-L480) wywołuje `monitor.start(user_id, user["chat_id"], airport, notify)` bez przekazania parametrów konfiguracyjnych.
  * [`remonitor`](file:///opt/flightping/src/flightpingbot/web.py#L481-L510) odczytuje rekord z `monitor_jobs`, lecz nie przekazuje archiwalnych ustawień tego zadania.
* **Skutek:** Monitor wznowiony lub dodany z poziomu przeglądarki gubi indywidualny próg opóźnienia i częstotliwość sprawdzania ustawione przez danego użytkownika, nadpisując je wartościami domyślnymi serwera.
* **Rekomendowane rozwiązanie:**
  W `start_monitor` pobierać `await self.repo.user_settings(user_id)` i przekazywać konfigurację do `self.monitor.start()`. W `remonitor` przekazywać parametry z `previous`.

---

### FPB-07: Ryzyko błędu `TelegramBadRequest: message is too long` w komendach administracyjnych
* **Lokalizacja:** [src/flightpingbot/handlers/admin.py:L110](file:///opt/flightping/src/flightpingbot/handlers/admin.py#L110), [admin.py:L117](file:///opt/flightping/src/flightpingbot/handlers/admin.py#L117), [admin.py:L224-L226](file:///opt/flightping/src/flightpingbot/handlers/admin.py#L224-L226)
* **Mechanizm:**
  Maksymalny rozmiar pojedynczej wiadomości tekstowej w Telegram Bot API to 4096 znaków.
  Podczas gdy komenda `/audit` implementuje prawidłowe dzielenie wiadomości na bloki do 3800 znaków ([admin.py:L280-L291](file:///opt/flightping/src/flightpingbot/handlers/admin.py#L280-L291)), komendy:
  - `/requests` (brak jakiegokolwiek limitu `LIMIT`),
  - `/users` (pobiera do 50 użytkowników z długimi nazwami),
  - `/usage` (agregacja po wszystkich użytkownikach `by_user`),
  łączą tekst za pomocą `"\n".join(...)` i wysyłają jednorazowo.
* **Skutek:** Przy rosnącej bazie użytkowników i zapytań API, wywołanie komendy przez administratora zakończy się nieobsłużonym błędem `aiogram.exceptions.TelegramBadRequest: Bad Request: message is too long`.
* **Rekomendowane rozwiązanie:**
  Wydzielić funkcję pomocniczą dzielącą długie odpowiedzi na paczki (chunking) i zastosować ją we wszystkich komendach listujących.

---

### FPB-08: Błąd pakowania pliku statycznego `logo.jpeg` w dystrybucji nieedytowalnej
* **Lokalizacja:** [src/flightpingbot/web.py:L269-L270](file:///opt/flightping/src/flightpingbot/web.py#L269-L270), [pyproject.toml:L23-L25](file:///opt/flightping/pyproject.toml#L23-L25)
* **Mechanizm:**
  W [`web.py`](file:///opt/flightping/src/flightpingbot/web.py#L269-L270):
  ```python
  logo_path = Path(__file__).resolve().parents[2] / "logo.jpeg"
  return FileResponse(logo_path, media_type="image/jpeg")
  ```
  Plik `logo.jpeg` znajduje się w katalogu głównym repozytorium `/opt/flightping/logo.jpeg`. W konfiguracji pakietu [pyproject.toml](file:///opt/flightping/pyproject.toml#L23-L25) uwzględniono wyłącznie katalog `src/`. Plik nie jest uwzględniony w `package_data`.
* **Skutek:** Jeżeli pakiet zostanie zbudowany jako wheel/sdist lub zainstalowany przez `pip install .` (bez flagi `-e`), plik `logo.jpeg` nie znajdzie się w instalacji docelowej. W Starlette `FileResponse` dla nieistniejącego pliku rzuca `RuntimeError`, co generuje błąd HTTP 500 przy każdym wejściu do panelu.
* **Rekomendowane rozwiązanie:**
  Przenieść `logo.jpeg` do `src/flightpingbot/static/` i dołączyć do `package_data` lub dodać obsługę błędu 404 zamiast rzucania błędu 500.

---

### FPB-09: Ograniczenie zapisu dla niestandardowego `FPB_STATE_DIR` w unit service systemd
* **Lokalizacja:** [flightpingbot.service:L11-L12](file:///opt/flightping/flightpingbot.service#L11-L12), [flightpingbot.service:L38](file:///opt/flightping/flightpingbot.service#L38), [.env.example:L4](file:///opt/flightping/.env.example#L4)
* **Mechanizm:**
  W pliku jednostki systemd:
  ```ini
  EnvironmentFile=/opt/flightping/config/flightpingbot.env
  Environment=FPB_STATE_DIR=/opt/flightping/state
  ...
  ReadWritePaths=/opt/flightping/state
  ```
  Zgodnie ze specyfikacją systemd, wartości z `EnvironmentFile=` nadpisują wpisy `Environment=`. Proces otrzyma więc wartość z pliku konfiguracyjnego, jednak restrykcyjne reguły `ProtectSystem=strict` i `ReadWritePaths=/opt/flightping/state` zablokują zapis w innej lokalizacji.
* **Skutek:** Konfiguracja nie jest ignorowana, ale aplikacja nie może utworzyć ani zapisać bazy w niestandardowym katalogu stanu.
* **Rekomendowane rozwiązanie:**
  Pozostawić sterowanie wartością wyłącznie przez plik środowiskowy oraz dostosować sandbox systemd tak, by pozwalał na zapis w skonfigurowanym katalogu.

## 6. Status poprawek (2026-09-03)

Wszystkie ustalenia FPB-01–FPB-11 zostały poprawione w kodzie i konfiguracji. Dodatkowo poprawiono użycie wartości `0` jako legalnego progu opóźnienia w serwisie monitoringu, aby nie była zamieniana na wartość domyślną przez operator `or`.

---

### FPB-10: Rozbieżność w `.env.example` względem `config.py`
* **Lokalizacja:** [.env.example](file:///opt/flightping/.env.example), [src/flightpingbot/config.py:L48-L49](file:///opt/flightping/src/flightpingbot/config.py#L48-L49), [README.md:L112-L113](file:///opt/flightping/README.md#L112-L113)
* **Mechanizm:**
  Parametry `FPB_TELEGRAM_MESSAGES_PER_MINUTE` (domyślnie 30) oraz `FPB_FSM_STATE_TTL_SECONDS` (domyślnie 900) są w pełni zaimplementowane w kodzie i udokumentowane w tabeli w `README.md`, ale nie występują w przykładowym pliku `.env.example`.
* **Skutek:** Utrudniona konfiguracja i brak widoczności tych parametrów dla administratora wdrażającego bota z szablonu.

---

### FPB-11: Nieograniczony wzrost struktur pamięci w `InboundSecurityMiddleware`
* **Lokalizacja:** [src/flightpingbot/security.py:L18-L19](file:///opt/flightping/src/flightpingbot/security.py#L18-L19), [security.py:L36-L48](file:///opt/flightping/src/flightpingbot/security.py#L36-L48)
* **Mechanizm:**
  W klasie [`InboundSecurityMiddleware`](file:///opt/flightping/src/flightpingbot/security.py#L11-L77):
  ```python
  self._message_times: dict[int, deque[float]] = defaultdict(deque)
  self._last_rate_notice: dict[int, float] = {}
  ```
  Przy nadejściu wiadomości od użytkownika tworzony jest wpis w słowniku. Nawet po opróżnieniu kolejki `deque` (starsze znaczniki czasu są usuwane z lewej strony), pusty obiekt `deque` oraz klucz `user_id` pozostają w słowniku na zawsze.
* **Skutek:** W przypadku bota wystawionego na zapytania od wielu przypadkowych kont Telegram, słowniki w pamięci rosną monotonicznie, stanowiąc powolny wyciek pamięci.

---

## 4. Pozytywne aspekty architektury i bezpieczeństwa

1. **Bezpieczne przechowywanie sekretów:**
   Aplikacja nie loguje ani nie eksponuje kluczy API ani tokenów w panelu WWW ([Settings](file:///opt/flightping/src/flightpingbot/web.py#L361-L444)).
2. **Izolacja transakcji bazodanowych:**
   Wprowadzenie [`write_lock`](file:///opt/flightping/src/flightpingbot/database.py#L58) oraz dedykowanego menedżera kontekstu [`consistent_reads()`](file:///opt/flightping/src/flightpingbot/database.py#L61-L70) całkowicie eliminuje ryzyko odczytania częściowo zatwierdzonych agregatów (tzw. dirty reads) przy pojedynczym połączeniu SQLite.
3. **Kolejkowanie zapytań z uwzględnieniem cooldownu:**
   Zastosowanie [`_queued_check`](file:///opt/flightping/src/flightpingbot/monitor.py#L321-L329) z per-użytkownikową blokadą `asyncio.Lock` zapobiega wyścigom pomiędzy równoległymi zadaniami monitorowania tego samego konta i automatycznie odczekuje czas schłodzenia (`RateLimited.retry_after`).

---

## 5. Rekomendowana kolejność działań naprawczych

1. **Natychmiast (Fix & Deploy):**
   - Poprawić [`scripts/web_healthcheck.py`](file:///opt/flightping/scripts/web_healthcheck.py) pod kątem autoryzacji tokenem (FPB-01).
   - Dodać obsługę `TypeError` i normalizację `tzinfo` w [`AeroAPI._normalize()`](file:///opt/flightping/src/flightpingbot/aeroapi.py#L100-L129) (FPB-02).
   - Poprawić weryfikację błędów w [`user_facing_error()`](file:///opt/flightping/src/flightpingbot/errors.py#L29-L67) (FPB-03).
2. **Kolejny sprint / Aktualizacja:**
   - Dodać procedurę naprawczą i aktualizację zapytań retencji dla `checks` (FPB-04).
   - Zabezpieczyć formularze panelu WWW tokenem CSRF / weryfikacją `Origin` nawet bez tokena HTTP Basic (FPB-05).
   - Przekazywać preferencje użytkownika w akcjach panelu WWW (FPB-06).
   - Zaimplementować dzielenie wiadomości w komendach administratora (FPB-07).
