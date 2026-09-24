# Code review: `feat/omp-edition`

**Data:** 2026-09-23
**Zakres:** `git diff 1390fe0..HEAD` (merge base z `upstream/master`), 4 commity: `cc5721e`, `bf01c00`, `ab4ad37`, `610bfed`. Linia po linii przejrzałem 19 plików źródłowych (+1631 linii). Wygenerowane `plugins-omp/**` i `.omp-plugin/marketplace.json` (74 pliki, +26 530 linii) służyły tylko jako dowód, co produkuje generator.
**Przebieg:** audytorzy security, code quality i dokumentacji, moja analiza wydajności i architektury, potem Cross-Verifier i Challenger.
**Przechodzą:**
- `python3 scripts/build_omp_edition.py --check`;
- `python3 omp/native/delivery/tests/test_route_task.py` (22 testy);
- `check_plugin_versions.py`, `check_agent_frontmatter.py` i `check_execution_boundary.py` razem z ich testami;
- `test_composite_contract.py` i `check-prefix-sync.sh`;
- trufflehog (0 sekretów), semgrep (321 reguł, 0 trafień), actionlint, shellcheck, mypy (0 błędów).

## Podsumowanie

| Severity | Liczba | Kategorie |
|---|---|---|
| CRITICAL | 0 | |
| HIGH | 0 | |
| MEDIUM | 9 | ARCH 4, MAINT 3, DOC 2 |
| LOW | 16 | SEC 2, ARCH 2, MAINT 6, DOC 6 |

Najważniejsze:
- Generator obiecuje, że każde niezmapowane wejście przerwie build. Nic tego nie testuje, a katalogi takie jak `hooks/` znikają bez błędu (ARCH-001, MAINT-001).
- Rozszerzenie `delivery.ts` i router parsują gramatykę planu każdy po swojemu. Regexy routera dodatkowo łapią sąsiednią linię, więc bramka potrafi przepuścić plan, którego hook zatwierdzenia nie rozpozna (ARCH-002, MAINT-002).
- Delivery zostawia niezacommitowany raport review. Przez niego następny przebieg zatrzymuje się na preflight (ARCH-004).

---

### [MEDIUM] ARCH-001: Nieznane katalogi pluginu (np. `hooks/`) znikają z edycji OMP bez błędu [verified]
**Status:** ✅ Fixed (2026-09-23)


**ID:** ARCH-001
**Location:** `scripts/build_omp_edition.py:332-370`
**Category:** Architecture
**Effort:** trivial

**Problem:**
`build()` mapuje tylko `scripts/`, `skills/`, `commands/`, `agents/` i manifest, a pozostałe wpisy pluginu pomija. QualityAuditor sprawdził to na kopii repo: overlay `{"plugin":"commit","agents":{}}` buduje się z kodem 0, a w `plugins-omp/commit/` nie ma `hooks/`. Znika więc hook PreToolUse, który blokuje bezpośredni `git commit`. Tak samo skończyłby `simple-language`.

**Impact:**
Overlay dla pluginu z hookami (`commit`, `simple-language`) da zielony build i edycję OMP bez hooka, na którym ten plugin się opiera. Dziś żaden wygenerowany plugin nie ma hooków, więc ryzyko pojawi się przy dodawaniu kolejnych.

**Remediation:**
Przejrzyj wszystkie wpisy `src_root` i przerwij build na tych, których generator nie mapuje:

```python
KNOWN_ENTRIES = {".claude-plugin", "agents", "commands", "skills", *VERBATIM_DIRS}
unknown = {p.name for p in src_root.iterdir()} - KNOWN_ENTRIES - {"tests"}
if unknown:
    raise BuildError(f"{src_root}: no OMP mapping for {sorted(unknown)}")
```

### [MEDIUM] ARCH-002: Dwa parsery planu dają różne wyniki: bramka `xd://propose` przepuszcza plany, których hook zatwierdzenia nie rozpoznaje [verified]
**Status:** ✅ Fixed (2026-09-23)


**ID:** ARCH-002
**Location:** `omp/native/delivery/extensions/delivery.ts:94-111`
**Category:** Architecture
**Effort:** easy

**Problem:**
`approvedPlan()` ma własną implementację gramatyki nagłówków i bloków kodu (regexy w liniach 16-17), a `route_task.py` swoją (`parse_plan`, linie 147-172). Dla nagłówka bez tytułu (`### Task 1:`) router liczy zadanie, bo jego regex przechodzi do następnej linii (MAINT-002). Regex TS wymaga treści w tej samej linii, więc zwraca 0 zadań. Sprawdziłem to: router podaje zadanie o tytule `**Files:**`, a TS nagłówka nie liczy. Bramka przy `xd://propose` akceptuje taki plan (`check` → `{"tasks": 1, "problems": []}`). Po zatwierdzeniu hook nie dodaje jednak komunikatu delivery i model wykonuje plan sam. Poza tym `APPROVED_PLAN_OPEN` (linia 19) przyjmuje tylko ścieżki `local://`, choć szablon zatwierdzenia w OMP dopuszcza też inne (`plan-mode-approved.md:17-19`).

**Impact:**
Delivery po cichu się nie uruchamia dla planu, który przeszedł bramkę.

**Remediation:**
Zostaw jeden parser. Z promptu wyciągaj tylko ścieżkę planu, a liczbę zadań bierz z `route_task.py check`, tak jak robi już bramka:

```ts
const url = /<plan path="([^"]+)">\n/.exec(prompt)?.[1];
const file = url?.startsWith("local:") ? localPath(url, ctx) : url && path.resolve(ctx.cwd, url);
const { tasks } = JSON.parse(await run("python3", [ROUTER, "check", root, file], 10_000));
if (tasks === 0) return undefined;
```

### [MEDIUM] ARCH-003: Wznowienie delivery rozpoznaje zadania tylko po numerze i nie odrzuca zdublowanych numerów [verified]
**Status:** ✅ Fixed (2026-09-23)


**ID:** ARCH-003
**Location:** `omp/native/delivery/skills/orchestration/SKILL.md:48-50`
**Category:** Architecture
**Effort:** easy

**Problem:**
Krok 9 wywołuje `route_task.py plan`, który przyjmuje zdublowane numery zadań. Wykrywa je tylko `check`, a ten działa wyłącznie w rozszerzeniu przy `xd://propose` i przepuszcza plan, gdy sam zawiedzie. `/delivery:execute` z ręcznie napisanym planem nigdy go nie wywołuje. Krok 10 uznaje zadanie N za zrobione na podstawie samej linii `Delivery-Task: <N>`. Przy dwóch blokach `Task 1` i przerwanym przebiegu drugi blok zostanie przy wznowieniu pominięty bez ostrzeżenia. To samo może się stać po edycji planu, do której procedura sama zachęca (krok 7 „update delivery plan”, krok 11 „Split it”): po przenumerowaniu status „zrobione” przechodzi na inne zadania.

**Impact:**
Przy wznowieniu zadanie może zostać po cichu pominięte albo zaimplementowane drugi raz.

**Remediation:**
W kroku 9 najpierw uruchom `python3 "$ROUTER" check` i zatrzymaj przebieg, gdy zgłosi duplikaty. Możesz też sprawić, żeby `plan` kończył się przy nich kodem 2. Dodaj trailer `Delivery-Task-Title: <title>` i zatrzymuj przebieg, gdy zrobione N ma w commicie inny tytuł niż w planie.

### [MEDIUM] ARCH-004: Delivery zostawia niezacommitowany raport review, przez który następny przebieg staje na preflight [verified]
**Status:** ✅ Fixed (2026-09-23)


**ID:** ARCH-004
**Location:** `omp/native/delivery/skills/orchestration/SKILL.md:47`
**Category:** Architecture
**Effort:** easy

**Problem:**
Krok 5 (Final review, linie 91-101) uruchamia `review.md` z code-review. Po odpowiedzi „Yes” zapisuje on raport do `docs/reviews/…` jako plik nieśledzony i nic go potem nie commituje. Następny przebieg w tej samej kopii roboczej, automatyczny po kolejnym zatwierdzeniu planu albo przez `/delivery:execute`, w krokach 5-7 tworzy gałąź i commituje plan, a w kroku 8 widzi `?? docs/reviews/` i się zatrzymuje. Dowody z testu E2E z 2026-09-23:
- podsumowanie przebiegu kończyło się zdaniem „It is not committed yet (`?? docs/reviews/`)”;
- plan weryfikacji musiał przed testem wznowienia wykonać `rm -rf docs/reviews`.

Challenger doprecyzował, że problem występuje wtedy, gdy użytkownik zapisze raport.

**Impact:**
Po każdym delivery z zapisanym raportem następne wymaga ręcznego sprzątania i ręcznego `/delivery:execute`, więc przepływ bez slash commandów się przerywa. Zatrzymanie zostawia też nową gałąź i commit planu.

**Remediation:**
Zacommituj zapisany raport na końcu przebiegu, ograniczając commit do tej ścieżki jak w kroku 7 (`docs: add review of delivery <SLUG>`). Druga możliwość: niech krok 8 pomija nieśledzone pliki w `docs/reviews/`. Warto też sprawdzać czystość drzewa (z pominięciem `PLAN_PATH`) przed krokiem 5, żeby zatrzymanie niczego po sobie nie zostawiało.

### [MEDIUM] MAINT-001: Nic nie testuje kontraktu „mapowania są totalne” w generatorze
**Status:** ✅ Fixed (2026-09-23)


**ID:** MAINT-001
**Location:** `scripts/build_omp_edition.py:312-401`
**Category:** Maintainability
**Effort:** medium

**Problem:**
Docstring (linie 25-26) i CLAUDE.md obiecują, że niezmapowane narzędzie, agent albo klucz przerwie build. W CI jedynym zabezpieczeniem jest `--check` (`.github/workflows/omp-edition.yml:30-31`). Porównuje on świeży wynik z zacommitowanym, a oba powstają z tego samego kodu. QualityAuditor zamienił na no-op trzy strażniki: nieznane narzędzie, nieznany klucz agenta i nieaktualnego agenta w overlayu. `--check` dalej zwracał „OMP edition is up to date”. Generator ma 27 miejsc z `raise BuildError` i zero testów. Tymczasem w repo przyjęło się, że skrypty uruchamiane w CI mają `scripts/test_<name>.py` (`test_check_agent_frontmatter.py`, `test_check_execution_boundary.py`). Wejścia to stałe modułu przywiązane do `REPO` (linie 44-58), co utrudnia testy na fixture'ach. Challenger obniżył severity z HIGH: stałe da się w teście podmienić, a `build_native()` już przyjmuje ścieżki.

**Impact:**
Refaktoryzacja może zamienić „przerwij build” na „pomiń” przy zielonym CI. Reguła z CLAUDE.md trzyma się tylko dlatego, że nikt nie ruszył strażników.

**Remediation:**
Przekaż katalog źródeł jako parametr i wydziel `build_generated()` na wzór `build_native()`. Dodaj `scripts/test_build_omp_edition.py`, który dla każdego strażnika sprawdza `BuildError`, i uruchamiaj go w `omp-edition.yml`.

### [MEDIUM] MAINT-002: Regexy gramatyki zadań w routerze łapią następną linię [verified]
**Status:** ✅ Fixed (2026-09-23)


**ID:** MAINT-002
**Location:** `omp/native/delivery/scripts/route_task.py:47-50`
**Category:** Maintainability
**Effort:** easy

**Problem:**
`\s*` po dwukropku w `TASK_HEADING`, `FILE_LINE` i `COMMIT_LINE` dopasowuje też `\n` (flaga `re.MULTILINE`). Odtworzone przypadki:
- pusty tytuł `### Task 1:` daje tytuł `**Files:**`;
- pusty `**Commit:**` przed `**Files:**` daje commit `**Files:**`, więc taki temat trafi do historii gita;
- pusty tytuł przed `**Commit:** feat: y` daje tytuł `**Commit:** feat: y` i commit `None`, a w efekcie temat `chore: **Commit:** feat: y` (SKILL.md:69).

`check()` uznaje wszystkie te plany za poprawne.

**Impact:**
Błędne tematy commitów i tytuły zadań, a zniekształcone plany przechodzą bramkę. Ten błąd powoduje też rozjazd opisany w ARCH-002.

**Remediation:**
Dopuszczaj tylko spacje i tabulatory, a wartość musi zaczynać się w tej samej linii:

```python
TASK_HEADING = re.compile(r"^### Task (\d+):[ \t]*(\S.*?)[ \t]*$", re.MULTILINE)
COMMIT_LINE = re.compile(r"^\*\*Commit:\*\*[ \t]*(\S.*?)[ \t]*$", re.MULTILINE)
FILE_LINE = re.compile(r"^[ \t]*[-*][ \t]*(?:Create|Modify|Test|Delete):[ \t]*(\S.*?)[ \t]*$", re.MULTILINE)
```

Niech `check()` zgłasza nagłówki `### Task`, które nie spełniają ścisłej gramatyki. Dodaj testy pustego tytułu i pustego commitu.

### [MEDIUM] MAINT-003: `delivery.ts` kopiuje logikę OMP bez testów i bez kroku w CI [verified]
**Status:** ✅ Fixed (2026-09-23)


**ID:** MAINT-003
**Location:** `omp/native/delivery/extensions/delivery.ts:18-111`
**Category:** Maintainability
**Effort:** medium

**Problem:**
Cztery fragmenty odtwarzają wewnętrzną logikę OMP:
- `localPath` (linie 70-77) odtwarza `resolveLocalRoot`;
- `proposedPlanUrl` (80-91) odtwarza `normalizePlanTitle`;
- `PROPOSE_TARGET` (18) odtwarza `parseXdUrl`;
- `approvedPlan` (94-111) odtwarza układ `plan-mode-approved.md`.

Żaden nie ma testu, a CI nawet nie transpiluje pliku. Smoke test z czasu implementacji był jednorazowy. Rozjazd już istnieje: OMP przyjmuje `XD://propose` bez względu na wielkość liter (`pi-tui/src/tools/xd-url.ts:13-19`), a regex rozszerzenia nie, więc bramka zostaje wtedy pominięta. Gałąź win32 z `resolveLocalRoot` nie jest odtworzona. OMP rozwiązuje importy `@oh-my-pi/pi-coding-agent/*` z pluginów na własną kopię (`extensibility/plugins/legacy-pi-compat.ts:789-805`), więc te helpery można importować zamiast kopiować.

**Impact:**
Po aktualizacji OMP rozszerzenie może niezauważenie przestać działać. Każda niezgodność kończy się przepuszczeniem, więc plan nie zostanie sprawdzony, a delivery nie wystartuje.

**Remediation:**
Importuj helpery OMP (`@oh-my-pi/pi-coding-agent/plan-mode/approved-plan`, `@oh-my-pi/pi-coding-agent/internal-urls`) zamiast je kopiować. Dodaj `omp/native/delivery/tests/delivery.test.ts` (`bun test`), który podaje hookom przykładowy prompt zatwierdzenia i wywołania `xd://propose`. Uruchamiaj go w `omp-edition.yml`, a jeśli nie, to przynajmniej `bun build`.

### [MEDIUM] DOC-001: Agenci delivery sprawdzają bloki `**Interfaces**` i `**Produces**`, których format planu nie definiuje [verified]
**Status:** ✅ Fixed (2026-09-23)
**Decision:** A — In `omp/native/delivery/agents/task-reviewer.md` replace line 15 `- The task block: title, **Files**, **Interfaces** and steps.` with `- The task block: title, **Files** and steps.` and line 25 `   - every **Produces** interface exists with exactly the stated name and signature;` with `   - every function, type and signature the task names exists exactly as stated;`, and in `omp/native/delivery/agents/implementer.md` replace line 14 `1. Read the task completely, including its **Files** and **Interfaces** blocks.` with `1. Read the task completely, including its **Files** block and the functions, types and signatures it names.`; leave `PLAN_FORMAT` in `omp/native/delivery/extensions/delivery.ts` unchanged and do not bump the delivery version again (`omp/native/delivery/.omp-plugin/plugin.json` and `omp/native/delivery/package.json` already read 0.3.0 in the working tree against 0.2.0 at HEAD, and the plugin is not on `master` yet); finally run `python3 scripts/build_omp_edition.py` to refresh `plugins-omp/delivery/agents/task-reviewer.md` and `plugins-omp/delivery/agents/implementer.md`. [user, 2026-09-23]
**Verification-plan:** tool: grep pattern=\*\*Interfaces|\*\*Produces|Consumes: path=omp/native/delivery;plugins-omp/delivery case=true gitignore=true → (empty); git diff --no-index --stat omp/native/delivery/agents plugins-omp/delivery/agents → (empty); python3 scripts/build_omp_edition.py --check → prints `OMP edition is up to date`; Read omp/native/delivery/extensions/delivery.ts:41 → `- Each task stands alone: its agent implements only that task. Name the functions, types and signatures that later tasks rely on.` (soft); Read omp/native/delivery/agents/task-reviewer.md:25 → `   - every function, type and signature the task names exists exactly as stated;` (soft)
**Decision-pin:** block=fbf32fb7cade30440c3b907134654a505f5254e7e109065a9add193d6eda8ff8 | omp/native/delivery/agents/task-reviewer.md=547e254f0adc5e344f3587480771638ab40f8cab:edit | omp/native/delivery/agents/implementer.md=b895f1bb2440ae0e036aaaeae38f0ef76980165b:edit | omp/native/delivery/extensions/delivery.ts=c9befe5360e4dd9240fd07232479bf1b1af1b409:ref | omp/native/delivery/.omp-plugin/plugin.json=d4ee6ef71dd454f1e97538b0b79bed42068abe9c:ref | omp/native/delivery/package.json=51faa3653181a95a93a627b96c83c5efef4d2a1e:ref | scripts/build_omp_edition.py=55b4efef37dff340f15dd6fa4e8e21cd01292a0e:ref | plugins-omp/delivery/agents/task-reviewer.md=547e254f0adc5e344f3587480771638ab40f8cab:edit | plugins-omp/delivery/agents/implementer.md=b895f1bb2440ae0e036aaaeae38f0ef76980165b:edit
**Dispatch:** attempt 1 dispatched 2026-09-23
**Verification:** advisory — grep **Interfaces/**Produces/Consumes: (empty), git diff --no-index agents (empty), build_omp_edition.py --check (up to date), read delivery.ts:41 (soft), read task-reviewer.md:25 (soft)

**ID:** DOC-001
**Location:** `omp/native/delivery/agents/task-reviewer.md:15` (was: `omp/native/delivery/agents/task-reviewer.md:25`)
**Category:** Documentation
**Drift-class:** dead-reference
**Fix-policy:** needs-decision
**Effort:** trivial

**Problem:**
Agenci delivery odwołują się do bloków, których nie ma w formacie planu:
- `task-reviewer.md:15` opisuje blok zadania jako „title, **Files**, **Interfaces** and steps”;
- krok 3 w linii 25 wymaga, żeby „every **Produces** interface exists with exactly the stated name and signature”;
- `implementer.md:14` każe czytać bloki „**Files** and **Interfaces**”.

`PLAN_FORMAT` (`delivery.ts:21-44`) definiuje tylko `**Commit:**` i `**Files:**`, a sygnatury każe opisywać prozą. Usunięty planner miał te bloki (`git show 610bfed^:omp/native/delivery/agents/planner.md`, linie 44-46), więc to niedokończone przejście na wersję 0.2.0. To samo zgłosił QualityAuditor.

**Impact:**
Sprawdzanie interfejsów między zadaniami nie ma danych wejściowych, więc niezgodność sygnatur między zadaniami może przejść niezauważona.

**Remediation:**
Są dwie drogi. Możesz dodać do `PLAN_FORMAT` opcjonalny blok `**Interfaces:**` (Consumes/Produces). Możesz też zmienić opis w obu agentach na „every function, type and signature the task names exists exactly as stated”. Potem podbij wersję delivery (`.omp-plugin/plugin.json` i `package.json`) i zregeneruj edycję.

### [MEDIUM] DOC-002: Przewodnik dla kontrybutorów pomija regenerację edycji OMP, którą CI teraz wymusza [verified]
**Status:** ✅ Fixed (2026-09-23)
**Decision:** B — In docs/contributing.md insert directly after the "Updated version in `plugin.json`" bullet of Pull Request Requirements (currently line 168) the bullet "- Regenerated OMP edition (if you changed `plugins/<name>/` of a plugin that has an `omp/overlay/<name>.json` — a version bump included — or anything under `omp/` or `scripts/build_omp_edition.py`): `plugins-omp/` and `.omp-plugin/marketplace.json` are generated, so never edit them by hand — run `python3 scripts/build_omp_edition.py` and commit both. OMP-only plugins in `omp/native/<name>/` are versioned in their own `.omp-plugin/plugin.json` and `package.json`, not in the four places above. The `OMP Edition` GitHub Actions workflow (`.github/workflows/omp-edition.yml`) enforces this and runs the generator and delivery tests listed there; the full rules are in [CLAUDE.md](../CLAUDE.md#omp-edition).", insert after the closing code fence of the Plugin Architecture tree (currently line 39) a blank line and the paragraph "Plugins with an OMP edition also have an `omp/overlay/<name>.json`, from which `plugins-omp/<name>/` is generated; OMP-only plugins live in `omp/native/<name>/`. See [CLAUDE.md](../CLAUDE.md#omp-edition).", and insert between lines 21 and 22 of .github/pull_request_template.md the checkbox "- [ ] OMP edition regenerated with `python3 scripts/build_omp_edition.py` and committed (if a plugin with an `omp/overlay/` entry, anything under `omp/`, or the generator changed)", keeping any list of OMP plugins and of CI test commands out of docs/contributing.md so they stay single-sourced in omp/overlay/*.json and .github/workflows/omp-edition.yml. [user, 2026-09-23]
**Verification-plan:** tool: Grep pattern=python3 scripts/build_omp_edition\.py path=docs/contributing.md case=true gitignore=true → exactly one matched line, and it also contains "plugins-omp/", ".omp-plugin/marketplace.json", "omp/native/<name>/", ".github/workflows/omp-edition.yml" and "../CLAUDE.md#omp-edition"; git blame -s -L '/^## Pull Request Requirements$/,+10' docs/contributing.md → the build_omp_edition.py bullet is one of the output lines and comes directly after the "Updated version in `plugin.json`" bullet; tool: Grep pattern=omp/overlay/<name>\.json path=docs/contributing.md case=true gitignore=true → at least two matched lines, one being the Plugin Architecture paragraph and one the Pull Request Requirements bullet; Read the paragraph after the closing code fence of the Plugin Architecture tree in docs/contributing.md → the excerpt names omp/overlay/<name>.json, plugins-omp/<name>/ and omp/native/<name>/ and links ../CLAUDE.md#omp-edition (soft); tool: Grep pattern=test_route_task|delivery\.test\.ts|test_build_omp_edition path=docs/contributing.md case=true gitignore=true → (empty); git blame -s -L '/^## OMP edition$/,+1' CLAUDE.md → exactly one output line, ending in "## OMP edition"; tool: Glob path=.github/workflows/omp-edition.yml gitignore=true hidden=true → lists omp-edition.yml under .github/workflows/; tool: Grep pattern="version" path=omp/native/delivery/.omp-plugin/plugin.json;omp/native/delivery/package.json case=true gitignore=true → exactly two matched lines, one per file, each carrying the same version string; tool: Grep pattern=build_omp_edition path=.github/pull_request_template.md case=true gitignore=true → exactly one matched line, beginning with "- [ ]"; python3 scripts/build_omp_edition.py --check → stdout is "OMP edition is up to date"
**Decision-pin:** block=0cc52e281501d39b198ba813a4e24703ddc726ed67087910130ee119f4767f5e | docs/contributing.md=71bfb187041bf43adb7be0d73ed90bcd3ebd0bd7:edit | .github/pull_request_template.md=450365cf90688ab378e56fdd3023067b6a394e9a:edit | scripts/build_omp_edition.py=55b4efef37dff340f15dd6fa4e8e21cd01292a0e:ref | .github/workflows/omp-edition.yml=a8ff1a356253736e7bbdf632207adbef0a284270:ref | CLAUDE.md=d280b1e57e4dd4f4bbc8f57d8905ff887167f74e:ref
**Dispatch:** attempt 1 dispatched 2026-09-23
**Verification:** advisory — grep build_omp_edition.py in contributing (1 line, 5 strings), git blame PR requirements (bullet after version bullet), grep omp/overlay/<name>.json (2 lines), read Plugin Architecture paragraph (soft), grep test names (empty), git blame CLAUDE.md OMP edition (1 line), glob omp-edition.yml, grep delivery versions (0.3.0 x2), grep PR template (1 checkbox), build_omp_edition.py --check (up to date)

**ID:** DOC-002
**Location:** `docs/contributing.md:161` (was: `docs/contributing.md:168`)
**Category:** Documentation
**Drift-class:** decision
**Fix-policy:** needs-decision
**Effort:** easy

**Problem:**
Wymagania dla PR wymieniają tylko sprawdzanie zgodności wersji. Nie mówią, że zmiana w `plugins/{code-review,python-developer,frontend-developer,php-developer}/`, także samo podbicie wersji, wymaga uruchomienia `python3 scripts/build_omp_edition.py` oraz commitu `plugins-omp/` i `.omp-plugin/marketplace.json`. Sekcja o architekturze pluginów (linia 27) nie wspomina `omp/overlay/` ani `omp/native/`. Te reguły są zapisane tylko w CLAUDE.md.

**Impact:**
Kontrybutor, który trzyma się przewodnika, dostaje czerwony check „OMP Edition” bez żadnego wyjaśnienia albo poprawia `plugins-omp/` ręcznie.

**Remediation:**
Dodaj do `docs/contributing.md` podsekcję „OMP edition” opisującą:
- które pluginy mają edycję OMP;
- krok regeneracji;
- pluginy natywne i ich wersjonowanie;
- test routera.

Dodaj też checkbox w szablonie PR i link do sekcji w CLAUDE.md.

### [LOW] SEC-001: Generator podąża za symlinkami w źródłach i kopiuje ich cel do `plugins-omp/` [verified]
**Status:** ✅ Fixed (2026-09-23)


**ID:** SEC-001
**Location:** `scripts/build_omp_edition.py:249`
**Category:** Security
**OWASP:** A01:2025
**CWE:** CWE-59
**Effort:** easy

**Problem:**
`guarded()` (linie 234-240) sprawdza tylko ścieżkę docelową. Po stronie źródeł `src.is_file()` (301, 335, 342) i `shutil.copy2` (249) podążają za symlinkami. Symlink w `omp/native/<name>/`, `plugins/<name>/scripts/` albo `plugins/<name>/skills/` trafia do wyniku jako zwykły plik z zawartością celu, także spoza repo. SecurityAuditor potwierdził to na kopii: symlink do pliku z sekretem dał w `plugins-omp/` zwykły plik z tym sekretem. Do tego `shutil.rmtree(..., ignore_errors=True)` (linia 427) po cichu pomija symlinkowany `plugins-omp`, a build pisze wtedy do celu symlinka.

**Impact:**
Jeśli symlink przejdzie review i zostanie scalony, przy następnej regeneracji plik maintainera (np. dane logowania) trafi do zacommitowanego i publikowanego `plugins-omp/`. CI nie wykryje tego przed merge'em, bo na runnerze cel symlinka nie istnieje. CVSS 4.7.

**Remediation:**
Odrzucaj symlinki we wszystkich drzewach źródłowych i kopiuj bez podążania za nimi:

```python
def copy(src: Path, path: Path, out_root: Path) -> None:
    if src.is_symlink():
        raise BuildError(f"{src}: symlinks are not allowed in plugin sources")
    shutil.copy2(src, guarded(path, out_root), follow_symlinks=False)
```

Odrzucaj też symlinkowany katalog wyjściowy i usuń `ignore_errors=True`.

### [LOW] SEC-002: Agent, którego narzędzia nie mają odpowiedników w OMP, dostaje wszystkie narzędzia [verified]
**Status:** ✅ Fixed (2026-09-23)


**ID:** SEC-002
**Location:** `scripts/build_omp_edition.py:208`
**Category:** Security
**OWASP:** A01:2025
**CWE:** CWE-276
**Effort:** easy

**Problem:**
`if tools:` (linia 208) pomija klucz `tools:`, gdy lista po mapowaniu jest pusta, a OMP traktuje brak `tools:` jako dostęp do wszystkich narzędzi (`task/executor.ts:3363-3371`). Lista jest pusta w dwóch przypadkach:
- źródło daje tylko narzędzia bez odpowiednika: `Skill` albo `TaskCreate/Update/List`, które mapują się na `todo` usuwane w linii 187;
- źródło używa samego `disallowedTools` bez `tools:`. Sprawdzenie w linii 193 porównuje wtedy zakaz z pustą listą i zakaz przepada bez słowa, wbrew komentarzowi z linii 191-192.

Dziś żaden agent nie trafia w żaden z tych przypadków.

**Impact:**
Przyszły wąski agent, np. z `tools: Skill` albo recenzent z samym `disallowedTools: Edit, Write`, dostanie w OMP bash, edit i write. Prompt injection w czytanej treści mógłby wtedy zmieniać repo.

**Remediation:**
Przerywaj build w dwóch przypadkach: gdy źródło deklaruje `tools` albo `disallowedTools`, a lista po mapowaniu jest pusta, oraz gdy `disallowedTools` występuje bez `tools`.

### [LOW] ARCH-005: Katalogi pluginów są wyszukiwane po samej nazwie, bez marketplace'u
**Status:** ✅ Fixed (2026-09-23)


**ID:** ARCH-005
**Location:** `omp/native/delivery/skills/orchestration/SKILL.md:112`
**Category:** Architecture
**Effort:** trivial

**Problem:**
Zainstalowane pluginy mają id `name@marketplace`. `split("@")[0]` łączy pluginy o tej samej nazwie z różnych marketplace'ów oraz duplikaty user/project (`shadowedBy`); wygrywa ostatni.

**Impact:**
`code-review` może wskazać plugin z innego marketplace'u, a wtedy jego `review.md` wykona się jako końcowe review. Cross-Verifier zauważa, że to także kwestia zaufania do źródła instrukcji.

**Remediation:**
Szukaj po `code-review@av-marketplace` i `delivery@av-marketplace` i pomijaj wpisy z `shadowedBy`.

### [LOW] ARCH-006: Pętla zadań cofa aktualnie wybraną gałąź, gdy implementer przesunął HEAD [verified]
**Status:** ✅ Fixed (2026-09-23)


**ID:** ARCH-006
**Location:** `omp/native/delivery/skills/orchestration/SKILL.md:178`
**Category:** Architecture
**Effort:** trivial

**Problem:**
Krok 3 traktuje każdą zmianę `git rev-parse HEAD` jako „agent zacommitował” i wykonuje `git reset --soft "$TASK_BASE"`. Implementerowi nie wolno przełączać gałęzi. Pętla i tak broni się jednak przed naruszeniem reguł, bo cofa commity implementera. Jeśli implementer przełączy gałąź, reset przestawi tę gałąź na `TASK_BASE`.

**Impact:**
Historia innej gałęzi zostaje po cichu przepisana.

**Remediation:**
Przed resetem sprawdź, czy `git branch --show-current` zwraca `BRANCH`. Jeśli nie, zatrzymaj przebieg z nazwą bieżącej gałęzi.

### [LOW] MAINT-004: Wartości w overlayach (`role`, `add_tools`, `thinking`) nie są walidowane
**Status:** ✅ Fixed (2026-09-23)


**ID:** MAINT-004
**Location:** `scripts/build_omp_edition.py:188-212`
**Category:** Maintainability
**Effort:** easy

**Problem:**
Generator sprawdza klucze overlaya, ale nie ich wartości. Overlay `{role: "exector", add_tools: ["ast-edit"], thinking: true}` buduje się z kodem 0 i daje `model: "@exector, opus"`, `tools: …, ast-edit` oraz `thinking-level: True`. Rola z literówką działa jak rola bez mapowania i po cichu przechodzi na `opus`. Nazwy narzędzi agentów natywnych (linie 287-293) też nie są sprawdzane. Challenger obniżył severity z MEDIUM, bo to ryzyko przyszłej pomyłki, a nie błąd na HEAD. Zauważył też, że ról nie można ograniczyć do wbudowanych, bo OMP przyjmuje role skonfigurowane przez użytkownika.

**Impact:**
Agent po cichu traci narzędzie albo zamierzony model.

**Remediation:**
Sprawdzaj `role` względem ról opisanych w README (`code_review`, `executor`, `challenger`, `analyst`), `add_tools` i narzędzia agentów natywnych względem nazw narzędzi OMP, a `thinking` względem poziomów myślenia. Możesz też usunąć nieużywane opcje `thinking` i `fallback_model`.

### [LOW] MAINT-005: Nieudana regeneracja zostawia `plugins-omp/` częściowo usunięty
**Status:** ✅ Fixed (2026-09-23)


**ID:** MAINT-005
**Location:** `scripts/build_omp_edition.py:427-428`
**Category:** Maintainability
**Effort:** easy

**Problem:**
`rmtree` wykonuje się przed `build()`. Przebieg z nieaktualnym agentem w overlayu zakończył się kodem 1 i zostawił `plugins-omp/` bez `delivery/` i bez `python-developer/.omp-plugin`.

**Impact:**
Drzewo robocze zostaje uszkodzone aż do `git checkout`, a częściowy wynik można przez pomyłkę zacommitować.

**Remediation:**
Buduj do katalogu tymczasowego, tak jak `--check`, i podmieniaj `plugins-omp/` dopiero po udanym buildzie.

### [LOW] MAINT-006: OMP podstawia `$ARGUMENTS` w preambule każdej wygenerowanej komendy
**Status:** ✅ Fixed (2026-09-23)


**ID:** MAINT-006
**Location:** `omp/preamble.md:9`
**Category:** Maintainability
**Effort:** trivial

**Problem:**
OMP podstawia argumenty w całym szablonie komendy (`extensibility/slash-commands.ts:136`), również w preambule, którą dokleja `build_command()` (`scripts/build_omp_edition.py:231`). To review jest tego dowodem: wywołanie `/code-review:review` z argumentem „wykonaj gruntowne code review zmian wprowadzonych na tym branchu” wyrenderowało linię „- `wykonaj gruntowne code review zmian wprowadzonych na tym branchu` in an agent's instructions stands for the task text you were given.” Przez placeholder w preambule OMP uznaje też każdą wygenerowaną komendę za używającą placeholderów. W takim przypadku nie dopisuje argumentów na końcu (`config/prompt-templates.ts:59-71`).

**Impact:**
Każde wywołanie komendy OMP zawiera zniekształconą instrukcję, a wieloliniowe argumenty rozbijają blok cytatu. Przyszła komenda bez `$ARGUMENTS` po cichu straci argumenty.

**Remediation:**
Daj komendom preambułę bez tej linii, żeby trafiała tylko do agentów, albo usuń `$` z jej treści.

### [LOW] MAINT-007: Bramka `xd://propose` przepuszcza plan bez sprawdzenia i nie informuje o tym
**Status:** ✅ Fixed (2026-09-23)


**ID:** MAINT-007
**Location:** `omp/native/delivery/extensions/delivery.ts:152-160`
**Category:** Maintainability
**Effort:** trivial

**Problem:**
Awaria routera, brak `python3`, przekroczenie limitu 10 s i nieznaleziony plik planu kończą się `return undefined` bez żadnego komunikatu.

**Impact:**
Użytkownik zakłada, że plany są sprawdzane, a nie są.

**Remediation:**
Dodaj `ctx.ui.notify("Delivery plan check skipped: …", "warning")` w `catch` i w gałęzi brakującego pliku. Plan dalej przepuszczaj.

### [LOW] MAINT-008: Szablon poprawek przeczy sam sobie
**Status:** ✅ Fixed (2026-09-23)


**ID:** MAINT-008
**Location:** `omp/native/delivery/skills/orchestration/SKILL.md:236`
**Category:** Maintainability
**Effort:** trivial

**Problem:**
Linia 232 mówi „fix exactly these, do not redo the task”, a linia 236 powtarza regułę implementera: „Implement exactly this task. Write tests first when the task lists a Test file.”

**Impact:**
Runda poprawek może robić zadanie od nowa zamiast naprawić znaleziska.

**Remediation:**
Zastąp linię 236 regułą „Change only what the findings require; add a failing test first when a finding reports missing coverage.”

### [LOW] MAINT-009: `run()` powiela `ExtensionAPI.exec`
**Status:** ✅ Fixed (2026-09-23)


**ID:** MAINT-009
**Location:** `omp/native/delivery/extensions/delivery.ts:46-50`
**Category:** Maintainability
**Effort:** easy

**Problem:**
API rozszerzeń ma już `pi.exec(command, args, { timeout })` (`extensibility/extensions/types.ts:1443`).

**Impact:**
To drugi helper do uruchamiania procesów, który trzeba utrzymywać.

**Remediation:**
Przekaż `pi` do helperów, używaj `pi.exec` i sprawdzaj `code`.

### [LOW] DOC-003: Format planu dla `/delivery:execute` nie jest opisany dla użytkownika, a delivery nie ma przewodnika [verified]
**Status:** ✅ Fixed (2026-09-23)
**Decision:** A — Utwórz nowy plik `docs/plugins/delivery.md` — przewodnik po pluginie Delivery (tylko Oh My Pi), bez nagłówka `**Version:**` (wersja delivery żyje w `omp/native/delivery/.omp-plugin/plugin.json`, a `scripts/check_plugin_versions.py` sprawdza tylko katalogi `plugins/<slug>/`) — z pięcioma sekcjami: (1) format planu z przykładem w jednym bloku kodu oznaczonym ```markdown, bez zagnieżdżonych bloków kodu, zawierającym zadanie `### Task 1: <title>` z linią `**Commit:** <conventional commit subject>` i blokiem `**Files:**` z liniami `- Create|Modify|Test|Delete: `path``, przepisany z `PLAN_FORMAT` w `omp/native/delivery/extensions/delivery.ts:21-44`, wraz z regułami: numery 1, 2, 3 w kolejności wykonania, każdy tylko raz, jeden stack na zadanie, ścieżki względne w backtickach, brak nagłówków `##` i `###` wewnątrz zadania poza blokami kodu, bez `**Commit:**` commit dostaje temat `chore: <title>`, opcjonalna sekcja `## Verification` ze sprawdzeniami po ostatnim zadaniu; (2) gałąź i położenie planu: na `main` lub `master` powstaje gałąź `delivery/<slug>`, na każdej innej gałęzi delivery pracuje na niej, plan leżący w repozytorium zostaje na swojej ścieżce, plan spoza repozytorium trafia do `docs/plans/<date>-<slug>.md`, a plan jest commitowany przed pierwszym zadaniem; (3) warunki wstępne: repozytorium git, wybrana gałąź (przy detached HEAD delivery kończy się komunikatem `Check out a branch first.`), brak zmian poza samym planem, zainstalowany plugin każdego agenta, do którego trafia zadanie; (4) trailery `Delivery-Plan: <PLAN_PATH>`, `Delivery-Task: <N>`, `Delivery-Task-Title: <title>` i `Delivery-Review: accepted-with-open-findings` oraz wznawianie przez `/delivery:execute <PLAN_PATH>`, które uznaje zadanie za zrobione tylko wtedy, gdy commit ma jego numer i dokładnie ten sam tytuł, a przy innym tytule zatrzymuje się i pokazuje commit, numer zadania i oba tytuły; (5) rundy poprawek: review po każdym zadaniu, znaleziska `critical` lub `important` wracają do tego samego agenta najwyżej na 3 rundy, potem użytkownik wybiera `Accept and commit with open findings` albo `Stop delivery`; następnie na końcu akapitu w `README.md:33` dopisz zdanie z linkiem `[Delivery guide](docs/plugins/delivery.md)` do tego przewodnika, a w `docs/README.md` w sekcji „Plugin Guides” między wierszem Commit (linia 10) a wierszem Frontend Developer (linia 11) dodaj wpis `- [Delivery](plugins/delivery.md)` z opisem oznaczonym jako Oh My Pi only; nie dodawaj wiersza Delivery do tabeli „Available Plugins” w `README.md`. [user, 2026-09-23]
**Verification-plan:** Run from the repository root: python3 -B -c 'import re,sys,pathlib;sys.path.insert(0,"omp/native/delivery/scripts");import route_task as r;t=pathlib.Path("docs/plugins/delivery.md").read_text();print(r.check(pathlib.Path(".").resolve(),re.search(r"```markdown\n(.*?)\n```",t,re.S).group(1)))' → output is one dict containing 'problems': [] and a 'tasks' value of 1 or more; Run python3 scripts/check_plugin_versions.py → output contains "Version parity OK for" and no line containing "[delivery]"; tool: Grep pattern=Delivery-[A-Z][A-Za-z-]*: path=docs/plugins/delivery.md case=true gitignore=true → every hit names only Delivery-Plan, Delivery-Task, Delivery-Task-Title or Delivery-Review, and each of those four names appears in at least one hit, matching omp/native/delivery/skills/orchestration/SKILL.md:72-76; tool: Grep pattern=\]\((docs/)?plugins/delivery\.md\) path=README.md;docs/README.md case=true gitignore=true → exactly one hit in README.md at line 33 and exactly one hit in docs/README.md on the line directly after the Commit entry; git status --porcelain -- docs/plugins/delivery.md → one line ending in docs/plugins/delivery.md (the link target exists); tool: Grep pattern=^\|\s*\[Delivery\] path=README.md case=true gitignore=true → (empty); tool: Grep pattern=\*\*Version:\*\* path=docs/plugins/delivery.md case=true gitignore=true → (empty); Re-read the resuming section of docs/plugins/delivery.md → the excerpt says a task counts as done only when a commit carries its number and its exact title, and that a differing title stops /delivery:execute (soft); Re-read the preconditions section of docs/plugins/delivery.md → the excerpt names the detached-HEAD stop "Check out a branch first." and the requirement of no changes besides the plan (soft); Re-read the fix-rounds section of docs/plugins/delivery.md → the excerpt names at most 3 fix rounds followed by the choice between "Accept and commit with open findings" and "Stop delivery" (soft)
**Decision-pin:** block=e240c93c881d6a839ccc3775fb83ec0595e3557d6d63d578a527ad64ee9628ad | README.md=f292d02b212661cdde809d2fed0bf9c00daed078:edit | docs/plugins/delivery.md=absent:edit | docs/README.md=f1863e4e72dcdc0be165835a02aabd8ede3af4bb:edit | omp/native/delivery/.omp-plugin/plugin.json=d4ee6ef71dd454f1e97538b0b79bed42068abe9c:ref | scripts/check_plugin_versions.py=35fb96f33f93e59ce7155a1c76dcf6550abcf3d9:ref | omp/native/delivery/extensions/delivery.ts=c9befe5360e4dd9240fd07232479bf1b1af1b409:ref | omp/native/delivery/skills/orchestration/SKILL.md=f89a12c7989db9d982b40cef2be014769983d071:ref
**Dispatch:** attempt 1 dispatched 2026-09-23
**Verification:** advisory — router check on guide example (tasks 1, problems []), check_plugin_versions.py (OK, no [delivery]), grep Delivery-* trailers (4 names), grep guide links (README.md:33, docs/README.md:11), git status guide (?? docs/plugins/delivery.md), grep Available Plugins row (empty), grep **Version:** (empty), re-read resuming (soft), re-read preconditions (soft), re-read fix rounds (soft)

**ID:** DOC-003
**Location:** `README.md:33` (was: `README.md:33`)
**Category:** Documentation
**Drift-class:** decision
**Fix-policy:** needs-decision
**Effort:** easy

**Problem:**
README mówi, że `/delivery:execute` „delivers a plan file you wrote yourself”. Format zadań istnieje jednak tylko w `PLAN_FORMAT`: linie plików, `**Commit:**`, jeden stack na zadanie. `PLAN_FORMAT` trafia wyłącznie do promptu systemowego w plan mode, a `/delivery:execute` uruchamia `plan`, nie `check`. Nie ma `docs/plugins/delivery.md` ani wpisu w `docs/README.md`. Warunki wstępne (wybrana gałąź, czyste drzewo) i trailery do wznawiania nie są nigdzie opisane.

**Impact:**
Ręcznie napisane plany trafiają do Jeva albo do pytania zamiast do routingu po plikach, a użytkownik nie wie dlaczego.

**Remediation:**
Dodaj `docs/plugins/delivery.md` i podlinkuj go z README.md:33 oraz `docs/README.md`. Przewodnik powinien opisać:
- format planu z przykładem;
- gałąź i położenie planu;
- warunki wstępne;
- trailery i wznawianie;
- rundy poprawek.

Nie dodawaj wiersza w tabeli „Available Plugins”, bo `check_plugin_versions.py` go odrzuci.

### [LOW] DOC-004: README opisuje rolę `analyst` jako „single-finding analysis”, a `composition-analyst` grupuje wiele znalezisk [verified]
**Status:** ✅ Fixed (2026-09-23)
**Decision:** A — In README.md:35 replace the words "single-finding analysis `analyst`" with "finding analysis (composite grouping, needs-decision findings, PR feedback) `analyst`", leaving the rest of that sentence, the `modelRoles` example at README.md:37-43, omp/overlay/code-review.json, plugins-omp/ and scripts/build_omp_edition.py unchanged, with no plugin version bump since README.md:35 is not part of any plugin. [user, 2026-09-23]
**Verification-plan:** tool: grep pattern="single-finding analysis" path=README.md → (empty); tool: grep pattern="\"role\": \"analyst\"" path=omp/overlay → exactly three starred match lines in omp/overlay/code-review.json, line 9 composition-analyst, line 10 decision-analyst and line 11 feedback-analyzer, the three users the new README wording lists; tool: read path=README.md:35 → excerpt from README.md:35 reads "Agents route through model roles instead of a fixed model: reviewers use `code_review`, fixers and developers `executor`, adversarial verification `challenger`, finding analysis (composite grouping, needs-decision findings, PR feedback) `analyst`, and plan mode `plan`. Map each role in `~/.omp/agent/config.yml`, for example:" (soft)
**Decision-pin:** block=bcf3a00ee452635c4507f6d2684251a80f9f2d3c27f8d2927b9d5761178bdd06 | README.md=17c8e1883471fbeb7d2e7b6a83541b9a89ae8f98:edit | omp/overlay/code-review.json=4803fa30ef0f78071e5109776138e2a5f629bd5e:ref | scripts/build_omp_edition.py=55b4efef37dff340f15dd6fa4e8e21cd01292a0e:ref
**Dispatch:** attempt 1 dispatched 2026-09-23
**Verification:** advisory — grep single-finding analysis in README.md (empty), grep analyst role in omp/overlay (3 lines: 9, 10, 11), read README.md:35 (soft)

**ID:** DOC-004
**Location:** `README.md:35` (was: `README.md:35`)
**Category:** Documentation
**Drift-class:** decision
**Fix-policy:** needs-decision
**Effort:** trivial

**Problem:**
`composition-analyst` grupuje w composites znaleziska z całego raportu (`plugins/code-review/agents/composition-analyst.md:3,11,22-24`). Challenger poprawił tu audytora: `feedback-analyzer` analizuje jeden komentarz PR, więc opisowi przeczy tylko jeden z trzech agentów tej roli.

**Impact:**
Użytkownik może przypisać tej roli mały model, choć obsługuje ona też grupowanie całego raportu.

**Remediation:**
Opisz rolę jako „finding analysis (composite grouping, needs-decision findings, PR feedback) `analyst`”.

### [LOW] DOC-005: Przewodnik instalacji opisuje tylko Claude Code i wymaga Claude Code CLI [verified]
**Status:** ✅ Fixed (2026-09-23)
**Decision:** B — In `docs/installation.md`, insert one paragraph between the `## Quick Start` heading (line 3) and the install block (line 5) reading "These commands are for Claude Code. For Oh My Pi (`omp`), follow [Oh My Pi (OMP)](../README.md#oh-my-pi-omp) in the README instead: only some plugins have an OMP edition, and the README lists them.", leave every other Quick Start line unchanged and add no `omp` install commands to this file, and change the Prerequisites row (line 21 before the insertion) so its Tool cell reads "Claude Code CLI or Oh My Pi (`omp`)" with the Required cell `Yes` and the Purpose cell `Latest version recommended` unchanged. [user, 2026-09-23]
**Verification-plan:** tool: Grep pattern="^\| Claude Code CLI \|" path=docs/installation.md → (empty); tool: Grep pattern="^\| Claude Code CLI or Oh My Pi \(.omp.\) \| Yes \| Latest version recommended \|$" path=docs/installation.md → exactly one match; tool: Grep pattern="README\.md#oh-my-pi-omp|^/plugin marketplace add" path=docs/installation.md → exactly two matches, with the `../README.md#oh-my-pi-omp` line at a lower line number than `/plugin marketplace add AppVerk/av-marketplace`; tool: Grep pattern="^### Oh My Pi \(OMP\)$" path=README.md → exactly one match at line 16, the heading that GitHub slugs to `oh-my-pi-omp`; tool: Grep pattern="omp plugin|for p in" path=docs/installation.md → (empty); git diff -- docs/installation.md → added lines appear only between `## Quick Start` and the first bash fence, plus the replacement Prerequisites row, and the only removed line is `| Claude Code CLI | Yes | Latest version recommended |`; Read docs/installation.md:3-8 → the new paragraph says the Quick Start commands are for Claude Code and sends Oh My Pi (`omp`) users to the README's Oh My Pi (OMP) section (soft)
**Decision-pin:** block=6ce7543f7b02c6e1f42f43d518eaa641bd11ec7b619a7ed6c53a87968bfd926d | docs/installation.md=482c505c94f99ee114b8b143422e78b3e4920072:edit | README.md=c8f436b1ff35801af842d5c014b71ef85550c9a1:ref
**Dispatch:** attempt 1 dispatched 2026-09-23
**Verification:** advisory — grep old Claude Code CLI row (empty), grep new prerequisite row (1), grep README link before /plugin (lines 5, 8), grep README OMP heading (line 16), grep omp install loop (empty), git diff installation.md (only the pointer paragraph and the row), read docs/installation.md:3-8 (soft)

**ID:** DOC-005
**Location:** `docs/installation.md:21` (was: `docs/installation.md:21`)
**Category:** Documentation
**Drift-class:** decision
**Fix-policy:** needs-decision
**Effort:** trivial

**Problem:**
Przewodnik podlinkowany z README jako „Installation & Optional Tools” pokazuje tylko `/plugin marketplace add` i wymienia Claude Code CLI jako wymagane.

**Impact:**
Użytkownik OMP nie znajdzie tam nic dla siebie, za to dostanie wymaganie, które go nie dotyczy.

**Remediation:**
Dodaj podsekcję „Oh My Pi” albo odsyłacz do sekcji OMP w README. Zmień wymaganie na „Claude Code CLI or Oh My Pi (`omp`)”.

### [LOW] DOC-006: Docstring generatora obiecuje błąd dla każdego nieznanego klucza frontmattera, a klucze komend odrzuca bez błędu [verified]
**Status:** ✅ Fixed (2026-09-23)
**Decision:** B — In scripts/build_omp_edition.py, make the command frontmatter mapping total: directly after COMMAND_KEYS at line 50 add `COMMAND_DROPPED_KEYS = {"allowed-tools", "model"}` under a comment saying OMP commands read only `description` and `argument-hint`, so these Claude Code keys are dropped on purpose; in build_command() at lines 264-268, before `kept` is built, compute `unknown = {k for k, _ in pairs} - set(COMMAND_KEYS) - COMMAND_DROPPED_KEYS` and raise `BuildError(f"{src}: no OMP mapping for frontmatter keys {sorted(unknown)}")` when it is non-empty; reword the docstring bullet at lines 11-12 so commands keep `description` / `argument-hint`, drop `allowed-tools` / `model` (OMP commands read neither), and gain the harness preamble above the unchanged body; leave the totality sentence at lines 25-26 unchanged, since it becomes true; and in scripts/test_build_omp_edition.py add an "unknown command key" entry to the `cases` dict of test_malformed_skill_and_command_fail_instead_of_disappearing (lines 197-201) with path "plugins/sample/commands/check.md", content "---\ndescription: Check\nsurprise: yes\n---\n\nCommand body.\n" and expected error "no OMP mapping for frontmatter keys". [user, 2026-09-23]
**Verification-plan:** python3 -c "import sys,tempfile,pathlib;sys.path.insert(0,'scripts');from test_build_omp_edition import fixture,put;from build_omp_edition import build;d=pathlib.Path(tempfile.mkdtemp());fixture(d/'s');put(d/'s/plugins/sample/commands/check.md','---\ndescription: Check\nsurprise: yes\n---\n\nBody.\n');build(d/'o',d/'s')" → the output ends with a BuildError traceback whose last line contains `check.md: no OMP mapping for frontmatter keys ['surprise']`; python3 -c "import sys,tempfile,pathlib;sys.path.insert(0,'scripts');from test_build_omp_edition import fixture,put;from build_omp_edition import build;d=pathlib.Path(tempfile.mkdtemp());fixture(d/'s');put(d/'s/plugins/sample/commands/check.md','---\nallowed-tools: Read\ndescription: Check\nmodel: opus\nargument-hint: [target]\n---\n\nBody.\n');build(d/'o',d/'s');print((d/'o/plugins-omp/sample/commands/check.md').read_text().split('---')[1])" → no traceback, and the printed frontmatter contains only a `description:` line with Check and an `argument-hint:` line with [target], with no `allowed-tools:` or `model:` line; python3 scripts/build_omp_edition.py --check → no output line starts with `error:`, showing that all eight real command sources in the four overlay plugins pass the new key check; python3 scripts/test_build_omp_edition.py → the last output line is `OK`; Read scripts/build_omp_edition.py:11-13 → the commands bullet says commands keep `description` / `argument-hint` and drop `allowed-tools` / `model` (soft)
**Decision-pin:** block=afb19e24c87d3fa18b6ea9fed65f453ff33bc0150f7d2975515dbe50eb0e437a | scripts/build_omp_edition.py=55b4efef37dff340f15dd6fa4e8e21cd01292a0e:edit | scripts/test_build_omp_edition.py=77b1c29706d844b3114e4aae5340c862f023aada:edit
**Dispatch:** attempt 1 dispatched 2026-09-23
**Verification:** advisory — fixture with unknown command key (BuildError 'no OMP mapping for frontmatter keys [surprise]'), fixture with allowed-tools/model (only description and argument-hint kept), build_omp_edition.py --check (up to date, no error:), test_build_omp_edition.py (OK), read build_omp_edition.py:11-13 (soft)

**ID:** DOC-006
**Location:** `scripts/build_omp_edition.py:25` (was: `scripts/build_omp_edition.py:25-26`)
**Category:** Documentation
**Drift-class:** decision
**Fix-policy:** needs-decision
**Effort:** trivial

**Problem:**
Docstring mówi, że nieznany „frontmatter key” przerywa build. Tymczasem `build_command()` (linie 227-231) po cichu odrzuca wszystko poza `description` i `argument-hint`, w tym `model: opus` ze wszystkich pięciu komend code-review. Samo odrzucenie jest poprawne, bo komendy OMP czytają tylko te dwa pola. Challenger zawęził znalezisko: CLAUDE.md:48-50 mówi wyraźnie o kluczach frontmattera *agentów*, więc nieścisły jest tylko docstring. Brak obsługi `hooks/` opisuje ARCH-001.

**Impact:**
Czytelnik docstringu spodziewa się, że nowy klucz w komendzie przerwie build, a klucz po prostu zniknie. W efekcie komendy code-review w OMP działają na modelu sesji, a nie na `opus`.

**Remediation:**
Zawęź docstring (linie 25-26) do frontmattera agentów i dopisz, że komendy zachowują tylko `description` i `argument-hint`. Jeśli klucze komend też mają być mapowane w całości, pomijaj w kodzie tylko jawną listę kluczy (np. `allowed-tools`, `model`), a na każdym innym przerywaj build.

### [LOW] DOC-007: README nie mówi, że gdy sędzią nie jest Jev, routing zawsze pyta użytkownika
**Status:** ✅ Fixed (2026-09-23)
**Decision:** A — W README.md:33 zamień końcówkę zdania "are routed by Jev (the `judge` model role), falling back to asking you below 0.8 confidence." na "are routed by Jev (the `judge` model role, e.g. `typesafe/jev-latest`) when it is at least 0.8 confident; below that, or on every such task when the `judge` role resolves to a non-Jev model, delivery asks you." i nie zmieniaj niczego więcej: omp/native/delivery/skills/orchestration/SKILL.md, plugins-omp/ oraz wersja delivery zostają bez zmian, bo scripts/build_omp_edition.py nie czyta tego zdania. [user, 2026-09-23]
**Verification-plan:** tool: Read path=README.md:33-33:raw → zdanie mówi, że zadanie bez listy plików rozdziela bez pytania tylko Jev z pewnością co najmniej 0.8, a przy modelu roli `judge` innym niż Jev delivery pyta przy każdym takim zadaniu (soft); tool: Grep pattern=falling back to asking you below 0\.8 confidence path=README.md case=true → brak dopasowań w README.md, czyli stare, niepełne zdanie zniknęło; tool: Read path=omp/native/delivery/skills/orchestration/SKILL.md:151-155:raw → oba warunki z tej listy, na które użytkownik ma wpływ, czyli `"jev" in model.lower()` i `answer["confidence"] >= 0.8`, mają odpowiednik w zdaniu README.md:33 (soft); tool: Grep pattern=in model\.lower\(\) path=omp/native/delivery/skills/orchestration/SKILL.md;plugins-omp/delivery/skills/orchestration/SKILL.md case=true → w obu plikach jest linia dopasowania *153| z `"jev" in model.lower()`, więc warunek opisany teraz w README nadal obowiązuje w kodzie
**Decision-pin:** block=121f0f762a42b5664e36c176836cba8289614f27fe9f5e464c3d1dcf46938677 | README.md=c8f436b1ff35801af842d5c014b71ef85550c9a1:edit | omp/native/delivery/skills/orchestration/SKILL.md=f89a12c7989db9d982b40cef2be014769983d071:ref | scripts/build_omp_edition.py=ec0b61e619b9ac6cd83b35765bce9eaa97d22f3a:ref
**Dispatch:** attempt 1 dispatched 2026-09-23
**Verification:** advisory — read README.md:33 (soft), grep old fallback sentence (empty), read SKILL.md:151-155 (soft), grep jev condition in both SKILL.md copies (line 153 in each)

**ID:** DOC-007
**Location:** `README.md:33` (was: `README.md:33`)
**Category:** Documentation
**Drift-class:** decision
**Fix-policy:** needs-decision
**Effort:** trivial

**Problem:**
README mówi, że zadania bez listy plików rozdziela Jev (rola `judge`), a poniżej 0,8 pewności delivery pyta użytkownika. `SKILL.md:149-158` pyta jednak także wtedy, gdy nazwa modelu z roli `judge` nie zawiera `jev`, nawet przy pewności 0,8 lub wyższej. DocsAuditor uznał to za zwykłe pominięcie i odrzucił, a Challenger przywrócił: to osobny warunek, który użytkownik widzi.

**Impact:**
Użytkownik z innym modelem w roli `judge` dostanie pytanie przy każdym zadaniu bez plików i nie znajdzie w README powodu.

**Remediation:**
Dopisz w README.md:33, że automatyczny routing działa tylko wtedy, gdy rola `judge` wskazuje model Jev, a przy innym modelu delivery zawsze pyta.

### [LOW] DOC-008: `python3` nie jest opisany jako wymaganie delivery
**Status:** ✅ Fixed (2026-09-23)
**Decision:** A — W README.md, w sekcji `### Oh My Pi (OMP)`, wstaw między zamykającym płotem bloku z komendami instalacji (linia 25) a akapitem zaczynającym się od `Delivery runs approved plans end to end` (linia 27) nowy akapit, oddzielony od sąsiednich bloków pustymi liniami, o dokładnej treści „Delivery needs Python 3.9 or newer, available as `python3` on `PATH`: its plan check, task router and preflight run Python. Without it, approving a plan does not start a delivery and the plan runs as usual.”. Następnie w docs/installation.md, w tabeli sekcji `## Prerequisites`, dodaj bezpośrednio pod wierszem zaczynającym się od `| GitHub CLI` (linia 23) wiersz o dokładnej treści „| Python 3.9+ (`python3`) | For Delivery (Oh My Pi) | Plan check, task routing and preflight of the Delivery plugin — see [Oh My Pi (OMP)](../README.md#oh-my-pi-omp) |”. Pozostałych wierszy tabeli i zdania o narzędziach opcjonalnych w linii 27 docs/installation.md nie zmieniaj. [user, 2026-09-23]
**Verification-plan:** tool: grep pattern="python3|^### Oh My Pi|^## Workflow" path=README.md → linie oznaczone `*` występują w kolejności: `### Oh My Pi (OMP)`, dokładnie jedna linia zawierająca `python3` i `3.9`, `## Workflow`, a żadna inna oznaczona linia nie zawiera `python3`; tool: grep pattern="python3|^## Prerequisites|^## Optional Tools" path=docs/installation.md → linie oznaczone `*` występują w kolejności: `## Prerequisites`, dokładnie jeden wiersz tabeli zaczynający się od `| Python 3.9+` i zawierający `For Delivery (Oh My Pi)` oraz `../README.md#oh-my-pi-omp`, `## Optional Tools`; tool: grep pattern="^### Oh My Pi \(OMP\)$" path=README.md → dokładnie jedna oznaczona linia `### Oh My Pi (OMP)`, czyli nagłówek, na który wskazuje kotwica `#oh-my-pi-omp` z docs/installation.md, istnieje; tool: grep pattern="removesuffix" path=omp/native/delivery → co najmniej jedna oznaczona linia w `skills/orchestration/SKILL.md` zawierająca `.removesuffix(`, czyli kod rzeczywiście wymaga Pythona 3.9 lub nowszego; tool: grep pattern="tomllib|strict=|ExceptionGroup|\bmatch\s+\w+:|TypeAlias|ParamSpec" path=omp/native/delivery/scripts/route_task.py → wynik pusty (empty), czyli router nie używa konstrukcji z Pythona 3.10+ i udokumentowany próg 3.9 nie jest za niski; tool: read path=docs/installation.md (sekcja `## Prerequisites`) → cytat tabeli z numerami linii pokazuje nagłówek, separator oraz wiersze `Git 2.x+`, `GitHub CLI` i `Python 3.9+`, każdy z dokładnie czterema znakami `|` (soft); tool: read path=omp/native/delivery/extensions/delivery.ts (hook `before_agent_start`, blok `try` wokół `checkPlan`) → cytat z numerami linii pokazuje, że błąd `checkPlan` kończy się `return undefined;` przed wiadomością `delivery-run`, co zgadza się ze zdaniem „approving a plan does not start a delivery” (soft)
**Decision-pin:** block=c1de4b0aebeea430b5417ee58de4ec479c6912c07edda76e3aa07ecbc949fa79 | README.md=1ec2b8674c8fe39c9feccd3940a16d53d633e9d4:edit | docs/installation.md=397c0e7b81dd3035734f4cd9dc59ba1a94d9e225:edit | omp/native/delivery/skills/orchestration/SKILL.md=f89a12c7989db9d982b40cef2be014769983d071:ref | omp/native/delivery/extensions/delivery.ts=c9befe5360e4dd9240fd07232479bf1b1af1b409:ref | omp/native/delivery/scripts/route_task.py=5c68e24765c0a9f34314732c255b28dd6a4aed01:ref
**Dispatch:** attempt 1 dispatched 2026-09-23
**Verification:** advisory — grep README python3 and section bounds (1 line at 27 between 16 and 50), grep installation.md python3 row (26, between 19 and 28), grep OMP heading (line 16), grep removesuffix (SKILL.md:28), grep 3.10+ constructs in router (empty), read prerequisites table (soft), read delivery.ts approval try/catch (soft)

**ID:** DOC-008
**Location:** `README.md:16` (was: `README.md:16`)
**Category:** Documentation
**Drift-class:** decision
**Fix-policy:** needs-decision
**Effort:** trivial

**Problem:**
Preflight delivery zawsze uruchamia `python3` (`SKILL.md:27-28,47-48`), podobnie jak bramka w rozszerzeniu (`delivery.ts:152-159`). Tymczasem `docs/installation.md:15-28` wymienia wymagania i mówi, że pluginy działają bez dodatkowych narzędzi. DocsAuditor odrzucił to jako lukę sprzed tej gałęzi. Challenger przywrócił znalezisko, bo delivery wprowadza nowe, bezwarunkowe wymaganie.

**Impact:**
Na maszynie bez `python3` delivery zatrzyma się na preflight, a bramka będzie przepuszczać plany bez sprawdzenia (MAINT-007).

**Remediation:**
Dopisz `python3` (3.9 lub nowszy; komenda sluga w SKILL.md używa `str.removesuffix`) jako wymaganie delivery w sekcji OMP w README i w `docs/installation.md`.

---

## Wydajność

Brak znalezisk. Zmiany nie dotykają bazy danych, sieci ani nieograniczonych kolekcji. Przy każdym prompcie rozszerzenie skanuje w pamięci gałąź sesji. W plan mode dochodzi jedno wywołanie `git rev-parse` na prompt. Czas pracy routera rośnie z rozmiarem planu. Kwadratowy backtracking regexów SecurityAuditor zmierzył dopiero przy 20 tys. spacji na końcu linii (0,94 s). Generator działa tylko w CI i przy regeneracji.

## Poza zakresem (istniało przed tą gałęzią)

- Komendy code-review wywołują skrypty pomocnicze ścieżkami względnymi repo (`bash plugins/code-review/scripts/slugify-branch.sh`, `analyze-feedback.md:378, 398, 424, 451`). Działa to tylko w tym repozytorium, w obu edycjach.
- `code-review:feedback-analyzer` analizuje komentarze PR, czyli niezaufane wejście, mając nieograniczony shell w obu edycjach. Ryzyko `decision-analyst` opisane w `docs/plugins/code-review.md:236-238` przechodzi do edycji OMP bez zmian.

## Luki w pokryciu

- TypeScript nie przeszedł sprawdzenia typów, bo brakuje `tsc`, `eslint` i `biome`. `delivery.ts` został tylko przetranspilowany (`bun build`).
- Brakuje `zizmor` (workflow sprawdziły actionlint, semgrep i ręczny przegląd) oraz `coverage.py` (pokrycie oceniono z kodu).
- Review nie wczytało skilli stackowych. W katalogu głównym i na pierwszym poziomie nie ma wskaźników stacku, a pluginy deweloperskie nie są zainstalowane w tej sesji.

## Verification Summary

**Method:** Cross-domain correlation and adversarial review (Cross-Verifier + Challenger)

| Metric | Count |
|--------|-------|
| Findings verified | 15 |
| False positives removed | 1 |
| Severity adjustments | 2 |
| Cross-analysis findings | 0 |

### Cross-Analysis (Security <-> Quality)

Cross-Verifier nie złożył żadnego composite: każda korelacja wymaga osobnych napraw.

- SEC-001 i MAINT-001: granica źródeł nie jest pilnowana, a CI nie ma jak sprawdzić złośliwego fixture'a. Odrzucanie symlinków warto od razu objąć testem.
- SEC-001 i MAINT-005: regeneracja usuwa poprzedni wynik przed walidacją wejść i może pisać przez symlinkowany katalog wyjściowy. Budowanie do katalogu tymczasowego i podmiana po sukcesie zamykają oba problemy.
- SEC-002 i MAINT-004: ta sama granica mapowania agentów przyjmuje pustą listę uprawnień i niesprawdzone wartości overlaya.
- Odrzucone przez Challengera znalezisko o komunikatach commitów i MAINT-002: luźny parser poszerza zakres treści planu, która trafia do tematu commita. Router mógłby wypisywać gotową wiadomość jako dane do `git commit -F -`.
- To samo odrzucone znalezisko i DOC-003: ścieżka ręcznych planów nie ma ani opisu formatu, ani ostrzeżenia, że treść planu to niezaufane wejście.
- ARCH-001 i DOC-006: dokumentacja obiecuje totalność, a generator nie sprawdza wszystkich wpisów pluginu.
- Luka do zbadania przy ARCH-005: czy plugin o tej samej nazwie z innego marketplace'u może dostarczyć instrukcje końcowego review.
- Luka do zbadania przy MAINT-007 i `/delivery:execute`: czy niesprawdzona ścieżka może przenieść niebezpieczny temat commita do `git commit`.

### Challenged Findings

- Plan-derived commit messages reach the shell through model-written command text (LOW, Security, `omp/native/delivery/skills/orchestration/SKILL.md:67`): usunięte jako false positive. `git commit -F -` czyta wiadomość ze stdin i niczego nie interpoluje. Wykonanie kodu wymagałoby, żeby model sam wybrał niebezpieczny `echo` albo heredoc bez cudzysłowów, a instrukcja tego nie podaje. To ryzyko do sprawdzenia w teście E2E, a nie ustalona ścieżka wstrzyknięcia.
- Nothing tests the generator's totality contract (MAINT-001): obniżone z HIGH do MEDIUM. To realna luka w CI, ale stałe modułu można w teście podmienić, a `build_native()` już przyjmuje ścieżki.
- Overlay values role, add_tools and thinking are not validated (MAINT-004): obniżone z MEDIUM do LOW. To ryzyko przyszłej konfiguracji, nie błąd na HEAD.
- DOC-004: zawężone, bo `feedback-analyzer` analizuje jeden komentarz PR.
- DOC-006: zawężone do docstringu, bo CLAUDE.md:48-50 mówi wyraźnie o kluczach agentów.
- Przywrócone z odrzuconych: DOC-007 (warunek „model nie jest Jevem”) i DOC-008 (`python3` jako wymaganie).

### Rejected by auditors (self-falsification)

**Security:**
- Spoofed 'Plan approved.' prompt or newline in plan.url injects text into the extension's <critical> block — `before_agent_start` widzi tylko prompty użytkownika i syntetyczne zatwierdzenie OMP, a `plan.url` to slug znormalizowany przez OMP.
- localPath() lacks OMP's ensureWithinRoot/realpath — wejścia to znormalizowane slugi albo oczyszczone tytuły, a ścieżka trafia tylko do `existsSync` i routera.
- Command injection through execFile of git/python3 — argumenty idą jako tablice, bez powłoki i bez wartości kontrolowanych przez atakującego.
- Router follows symlinks when reading manifests or the plan — odczyty decydują tylko o etykiecie stacku, a `classify()` odrzuca ścieżki absolutne i `..`.
- Quadratic regex backtracking in TASK_HEADING / FILE_LINE — 0,94 s przy 20 tys. spacji; użytkownik może spowolnić tylko sam siebie, a limit 10 s kończy się przepuszczeniem planu.
- `git add -A` may stage and commit secrets — działanie celowe; recenzent zadania sprawdza sekrety, końcowe review skanuje `BASE..HEAD`, a delivery niczego nie pushuje.
- `git reset --soft "$TASK_BASE"` rewrites another branch if the implementer switched branches — to kwestia poprawności, nie bezpieczeństwa; przejęte jako ARCH-006.
- Done-task detection by `git log -F --grep` substring can over-match or be spoofed — to nie jest granica bezpieczeństwa; stan wznowienia opisuje ARCH-003.
- Symlinked plugins-omp plus rmtree(ignore_errors) writes outside the repo (standalone) — wymaga kontroli nad checkoutem; włączone do SEC-001.
- Overlay `thinking` written raw into YAML frontmatter — overlaye powstają w repo; to kwestia spójności kodowania wyjścia (zob. MAINT-004).
- omp.extensions entry like '../x' passes the file check — dane pochodzą z repo; to kwestia dokładności walidacji.
- Scoped `Bash(git:*)` broadened to unrestricted `bash` — w Claude Code taki zakres też niczego nie ogranicza (`docs/plugins/code-review.md:236`), a preambuła to mówi.
- actions/checkout keeps the default persist-credentials — `contents: read`, `pull_request`, bez sekretów, tak jak w 6 istniejących workflowach.
- Bandit B404/B603: subprocess in router tests — kod testów ze stałymi argumentami.
- delivery:task-reviewer described as 'Read-only' while holding bash — to zobowiązanie modelu bez eskalacji uprawnień; zapisane jako luka w doktrynie.
- Unpinned marketplace install in README — ten sam model instalacji co w edycji Claude.
- Preamble tells subagents to pick 'the most likely option' instead of asking — żaden agent nie uzależnia destrukcyjnej akcji od pytania.

**Quality:**
- Line length E501 in the changed Python files — projekt nie konfiguruje długości linii, a istniejące walidatory mają to samo.
- zip() without strict= in route() — `files` powstaje z `paths` jeden do jednego.
- mypy --strict type-arg and no-any-return — projekt nie używa trybu strict, a domyślny mypy przechodzi.
- CRLF plans hide fences from the TS fence regex — `format()` w OMP obcina końcowe białe znaki razem z `\r`.
- Plan-mode state still active at approval — `#approvePlan` dopisuje `mode_change none` przed syntetycznym promptem.
- A queued approval prompt batched behind another message would not start with 'Plan approved.' — to zależy od kolejności w czasie działania; do sprawdzenia w E2E.
- Generated agents emit thinking-level, which OMP might not read — agenci OMP sami używają `thinking-level`, a żaden overlay go nie ustawia.
- build_command drops model and allowed-tools from commands — komendy OMP czytają tylko `description` i `argument-hint`; nieścisły docstring opisuje DOC-006.
- Bash(git:*) becomes unrestricted bash for feedback-analyzer — to udokumentowane mapowanie, a w Claude Code ten zakres też niczego nie ogranicza.
- Jev routing hard-codes a model-name substring — odwzorowuje politykę OMP dla natywnego Jeva.
- --check ignores file modes although copy() relies on the executable bit — skrypty uruchamia się przez `bash` i `python3`, a tryby w gicie się zgadzają.
- Duplicate frontmatter keys collapse silently through dict(pairs) — żadne źródło ich nie ma; kandydat do testów z MAINT-001.
- preamble.format() would crash on a literal brace — preambuła nie ma dziś nawiasów klamrowych.
- Skill mapped to None could leave an agent unable to read skills — każdy agent z `Skill` ma też `Read`.
- gitRoot() spawns git on every plan-mode turn — jeden krótki proces na turę.
- .gitignore comment names tracked project agents and WATCHDOG files that do not exist — bez skutku dla działania.

**Documentation:**
- Delivery missing from README Available Plugins table — tak musi być: `check_plugin_versions.py:331-349` odrzuca wiersze bez `plugins/<slug>/`, a README.md:16-45 opisuje delivery.
- README fallback sentence does not cover Delivery agents — zdanie dotyczy tylko agentów, którzy mają edycję Claude.
- OMP edition silently drops command model opus — komendy OMP mają tylko `description` i `argument-hint`; sformułowanie docstringu opisuje DOC-006.
- Workflow guide and plugin guides do not mention OMP or Delivery — `docs/workflow.md` opisuje cykl Claude Code, a OMP opisuje README.
- CLAUDE.md input list omits omp/native and the Claude catalog — opisane w CLAUDE.md:51 i :59-65.
- Gitignore comment names tracked .omp agents/commands/WATCHDOG that do not exist — komentarz wyjaśnia wąskie wzorce i nie jest dokumentacją dla użytkownika.

### Doctrine-gap candidates

- No provenance rule for plans run by /delivery:execute — `/delivery:execute` wykonuje dowolny plik planu, także zacommitowany przez kogoś innego, a implementerzy z bashem uruchamiają każdą komendę z zadania bez osobnej akceptacji.
- No rule requiring a residual-risk note for bash-holding agents described as read-only — `decision-analyst` ma taką notę (`docs/plugins/code-review.md:236-238`), a `delivery:task-reviewer` nie.
- No rule against symlinks in plugin source trees — ani CLAUDE.md, ani walidatory nie zabraniają symlinków w `plugins/` i `omp/`.
- Tests for CI scripts — dwa z trzech walidatorów mają testy, ale żadna reguła tego nie wymaga. Kandydat na regułę: każdy skrypt z CI ma `scripts/test_<name>.py`.
- Native OMP extensions — żadna reguła nie wymaga w CI testu, sprawdzenia typów ani transpilacji `omp/native/*/extensions/*.ts`.
- Mirrored harness internals — nie ma reguły dla kodu skopiowanego z OMP. Kandydat: importuj eksport albo przypnij kopię testem.
- One parser per grammar — gramatykę planu czytają rozszerzenie, router i skill. Kandydat: jedna gramatyka, jeden parser.
- No drift check for the README OMP section — README.md:18 i :35 powtarzają treść `omp/overlay/*.json`, a nic tego nie sprawdza.
- No documentation rule for OMP-only plugins — reguła czterech miejsc wersji obejmuje tylko pluginy Claude, więc pluginy natywne nie muszą mieć przewodnika.
