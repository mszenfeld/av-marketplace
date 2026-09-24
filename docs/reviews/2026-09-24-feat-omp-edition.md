# Code review: `feat/omp-edition`, delivery 0.4.0

**Data:** 2026-09-24
**Zakres:** `git diff dd2db75..HEAD`, 9 commitów delivery planu `docs/plans/2026-09-24-delivery-hardening.md` (`dad5639`…`50d7d28`), 23 pliki, +856/−153 linii. Wygenerowane `plugins-omp/**` i `.omp-plugin/marketplace.json` oceniałem przez ich źródła.
**Przebieg:** audytorzy security, code quality i dokumentacji, moja analiza wydajności i architektury, potem Cross-Verifier i Challenger.
**Przechodzą:**
- `python3 scripts/build_omp_edition.py --check`, także z symlinkiem `omp/native/delivery/node_modules`;
- `python3 omp/native/delivery/tests/test_route_task.py` (48 testów);
- `python3 scripts/test_build_omp_edition.py` (22 testy) i `python3 scripts/test_check_omp_tools.py` (5 testów);
- `bun test tests/delivery.test.ts` (9 pass, 0 fail);
- `python3 scripts/check_omp_tools.py` → `OMP_TOOLS matches OMP 18.3.0 (29 tools)`;
- trufflehog (0 sekretów), semgrep (0 trafień), bandit (tylko B404/B603/B607 na poziomie LOW, odrzucone), mypy (0 błędów), ruff `E,W,F` bez E501 (czysto).

## Podsumowanie

| Severity | Liczba | Kategorie |
|---|---|---|
| CRITICAL | 0 | |
| HIGH | 0 | |
| MEDIUM | 0 | |
| LOW | 7 | ARCH 1, MAINT 5, DOC 1 |

Najważniejsze:
- `/delivery:execute` uruchomione na planie bez nagłówków `### Task` nadal tworzy gałąź i commituje plan, zanim się zatrzyma (ARCH-001).
- Nowe testy `scripts/test_check_omp_tools.py` nie są uruchamiane w CI, choć testy każdego innego checkera są (MAINT-001).
- Wznowienie może zgłosić fałszywy konflikt tytułu, gdy tytuł zadania zawiera separator linii Unicode, np. U+2028 (MAINT-003).

---

### [LOW] ARCH-001: `/delivery:execute` na planie bez zadań tworzy gałąź i commit, zanim się zatrzyma [verified]

**ID:** ARCH-001
**Location:** `omp/native/delivery/skills/orchestration/SKILL.md:39`
**Category:** Architecture
**Effort:** trivial

**Problem:**
Krok 8 preflightu zatrzymuje przebieg tylko wtedy, gdy lista `problems` nie jest pusta. Dla planu bez żadnej linii `### Task` polecenie `route_task.py check` zwraca `{"tasks": 0, "problems": [], "no_files": []}` (przypina to `test_check_without_tasks_reports_zero`). Przebieg idzie więc dalej: na `main` albo `master` krok 9 tworzy `delivery/<slug>`, a krok 10 commituje plan. Zatrzymuje się dopiero krok 12, bo `plan` kończy się kodem 2 (`no '### Task N:' headings`). Commit Task 6 zapowiada „stop on every plan problem before branching”. W 0.3.0 kolejność była taka sama, więc to luka, której nowy krok 8 nie domknął, a nie regresja.

**Impact:**
Ktoś, kto przez pomyłkę uruchomi `/delivery:execute` na planie badawczym, zostaje z nową gałęzią i commitem planu do ręcznego sprzątnięcia. Po zatwierdzeniu w plan mode to się nie dzieje, bo rozszerzenie nie uruchamia delivery dla planu z 0 zadań.

**Remediation:**
W kroku 8 zatrzymuj przebieg także wtedy, gdy `tasks` wynosi 0, a potem zregeneruj edycję (`python3 scripts/build_omp_edition.py`):

```markdown
8. **Plan check.** `CHECK=$(python3 "$ROUTER" check "$REPO" "$PLAN_PATH")`; a non-zero exit → stop with the router's error. When its JSON `tasks` is 0 → stop with `Delivery cannot run <PLAN_PATH>: it has no '### Task N: <title>' headings.` When its JSON `problems` list is not empty → stop with one line per problem:
```

### [LOW] MAINT-001: Testy `check_omp_tools.py` nie są uruchamiane w CI [verified]

**ID:** MAINT-001
**Location:** `.github/workflows/omp-edition.yml:47-48`
**Category:** Maintainability
**Effort:** trivial

**Problem:**
Task 7 dodał `scripts/test_check_omp_tools.py`, a Task 8 tylko krok `Check OMP_TOOLS against OMP`. Testy każdego innego checkera w repozytorium CI uruchamia tuż przed nim (`agent-frontmatter.yml:37-38`, `execution-boundary.yml:34-35`, `superutils-contract.yml:33-34`), a ten sam workflow uruchamia `test_build_omp_edition.py`.

**Impact:**
CI nie wyłapie regresji w `builtin_tool_names` ani w kodach wyjścia `main`. Sam checker działa na `@latest`, więc pierwszym sygnałem byłby fałszywie czerwony albo fałszywie zielony wynik na niezwiązanym PR.

**Remediation:**
Testy budują własne pakiety tymczasowe i nie potrzebują instalacji OMP, więc mogą stać obok testów generatora:

```yaml
      - name: Test OMP edition generator
        run: python3 scripts/test_build_omp_edition.py

      - name: Test OMP_TOOLS checker
        run: python3 scripts/test_check_omp_tools.py
```

### [LOW] MAINT-002: Commit zadania nie wykrywa błędu routera, gdy hook uzupełni wiadomość [verified]

**ID:** MAINT-002
**Location:** `omp/native/delivery/skills/orchestration/SKILL.md:71`
**Category:** Maintainability
**Effort:** trivial

**Problem:**
`python3 "$ROUTER" message ... <N> | git commit -F -` zwraca status gita, nie routera. Gdy router zawiedzie, wiadomość jest pusta i git przerywa commit („Aborting commit due to empty commit message”), co skill traktuje jako nieudany commit. Hook `prepare-commit-msg`, który sam wpisuje treść, przepuści jednak commit bez trailerów `Delivery-*`.

**Impact:**
`done` nigdy nie uzna takiego zadania za dostarczone, więc wznowienie wykona je drugi raz. Krok 12 sprawdził już plan i numer zadania, dlatego ryzyko jest niskie.

**Remediation:**
Zapisz wiadomość w zmiennej, zatrzymaj przebieg przy niezerowym kodzie routera i dopiero wtedy przekaż ją gitowi:

```bash
MSG=$(python3 "$ROUTER" message "$REPO" "$PLAN_PATH" <N>)   # non-zero exit → stop with the router's error
printf '%s' "$MSG" | git commit -F -
```

### [LOW] MAINT-003: `delivered()` dzieli treść commita na separatorach linii Unicode i zgłasza fałszywy konflikt [verified]

**ID:** MAINT-003
**Location:** `omp/native/delivery/scripts/route_task.py:292`
**Category:** Maintainability
**Effort:** trivial

**Problem:**
`entries[index + 1].splitlines()` dzieli tekst także na U+2028, U+0085, `\x0b`, `\x0c` i `\x1c`–`\x1e`, a `TASK_HEADING` przyjmuje te znaki w tytule. Recenzent Task 4 odtworzył to na planie z `### Task 1: A\u2028B`: po `message | git commit -F -` polecenie `done` zgłasza konflikt z `committed_title: "A"`.

**Impact:**
Wznowienie takiego planu zatrzymuje się na konflikcie, którego nie ma. Tytuły z takimi znakami zdarzają się rzadko.

**Remediation:**
Dziel tylko na `\n`, tak jak parser planu, i dodaj do `DoneTest` przypadek z tytułem zawierającym U+2028:

```python
# Before
lines = entries[index + 1].splitlines()

# After
lines = entries[index + 1].split("\n")
```

### [LOW] MAINT-004: `check_omp_tools.py` kończy się tracebackiem i kodem 1, gdy nie odczyta listy narzędzi

**ID:** MAINT-004
**Location:** `scripts/check_omp_tools.py:41-43`
**Category:** Maintainability
**Effort:** trivial

**Problem:**
Według docstringu kod 1 oznacza niezgodność, a kod 2 brak OMP. `main` nie łapie `ValueError` z `builtin_tool_names` ani `KeyError` z `package.json` bez pola `version`. QualityAuditor sprawdził to na pakiecie z `export const BUILTIN_TOOL_NAMES: readonly string[] = ["read"];`: skrypt wypisał traceback zakończony `ValueError: BUILTIN_TOOL_NAMES not found` i wyszedł z kodem 1.

**Impact:**
Gdy OMP zmieni kształt tej deklaracji, CI pokaże traceback i kod, który znaczy „`OMP_TOOLS` jest nieaktualne”. Maintainer zacznie wtedy poprawiać nie to, co trzeba.

**Remediation:**
Zwracaj kod 2 z czytelnym komunikatem i rozszerz opis kodu 2 w docstringu do „OMP is not installed or its tool list cannot be read”:

```python
# Before
version = json.loads(manifest.read_text())["version"] if manifest.is_file() else "?"
installed = builtin_tool_names(names_file.read_text())

# After
version = json.loads(manifest.read_text()).get("version", "?") if manifest.is_file() else "?"
try:
    installed = builtin_tool_names(names_file.read_text())
except ValueError as error:
    sys.stderr.write(f"{names_file}: {error}; update NAMES in scripts/check_omp_tools.py\n")
    return 2
```

### [LOW] MAINT-005: `delivered()` łączy wywołania gita z parsowaniem trailerów

**ID:** MAINT-005
**Location:** `omp/native/delivery/scripts/route_task.py:275-326`
**Category:** Maintainability
**Effort:** easy

**Problem:**
Jedna funkcja (52 linie, CC≈13) uruchamia `git log`, parsuje wynik rozdzielony bajtami NUL, dopasowuje trailery `Delivery-Plan`, `Delivery-Task` i `Delivery-Task-Title`, zbiera konflikty i wywołuje `git rev-parse`. Reguły parsowania da się sprawdzić tylko na prawdziwym repozytorium: każdy z 10 przypadków `DoneTest` robi `git init`.

**Impact:**
Testy parsera są wolniejsze i słabiej odizolowane, a każda nowa reguła trailerów wymaga osobnego repozytorium testowego.

**Remediation:**
Wydziel czysty parser, a `delivered()` zostaw jako cienką warstwę nad gitem z niezmienioną sygnaturą:

```python
def scan_delivery_log(log: str, rel: str, titles: dict[int, str]) -> tuple[set[int], list[dict], str | None]:
    """Parse `git log --format=%H%x00%B%x00` output; no I/O."""
    ...

def delivered(root: Path, rel: str, tasks: list[dict]) -> dict:
    # git log → scan_delivery_log → git rev-parse
    ...
```

### [LOW] DOC-001: Checklista w szablonie PR nie ma nowego wymogu testów delivery

**ID:** DOC-001
**Location:** `.github/pull_request_template.md:22`
**Category:** Documentation
**Effort:** trivial
**Drift-class:** decision
**Fix-policy:** needs-decision

**Problem:**
Checklista szablonu PR odpowiada punkt po punkcie sekcji Pull Request Requirements w `docs/contributing.md`. Pięć takich par istniało przed tym zakresem. Task 1 dodał w `docs/contributing.md:172` wymóg „Passing delivery tests” i to jedyny wymóg bez pola w szablonie. Przy poprzednim dopisaniu wymogu poprawka zmieniła oba pliki naraz (`docs/reviews/2026-09-23-feat-omp-edition.md:225`).

**Impact:**
Kontrybutor, który zmienia `omp/native/delivery/` i idzie za szablonem, nie dostaje przypomnienia o `test_route_task.py` ani o testach hooków w bun. Te drugie wymagają lokalnej instalacji OMP opisanej tylko w `docs/contributing.md`, więc o błędzie dowie się dopiero z CI.

**Remediation:**
Dodaj po linii 22 `.github/pull_request_template.md`:

```markdown
- [ ] Delivery tests pass: `python3 omp/native/delivery/tests/test_route_task.py` and `bun test tests/delivery.test.ts` in `omp/native/delivery/` (if `omp/native/delivery/` changed)
```

Możesz też uznać, że szablon obejmuje tylko część wymagań (CI i tak uruchamia oba testy przy każdym PR), i zapisać tę decyzję.

---

## Wydajność

Brak znalezisk. `delivered()` przechodzi raz przez historię przefiltrowaną `--grep`, a nowy `os.walk` w generatorze nie wchodzi do `node_modules`. Pętla kopiowania w `build_native` nadal przegląda cały katalog `node_modules` z `bun install`, zanim go odfiltruje. Plan (Task 2) celowo zostawił ją bez zmian, a w CI generator działa przed instalacją OMP.

## Luki w pokryciu

- TypeScript nie przeszedł sprawdzenia typów: `omp/native/delivery/` nie ma `tsconfig.json`, a `bun test` transpiluje bez typecheckingu.
- Python nie ma konfiguracji lintera, więc ruff z domyślnymi regułami zgłasza 159 × E501 na przyjętym w repozytorium stylu długich linii.
- Test E2E wersji 0.4.0 (krok 9 weryfikacji planu) wymaga ręcznej sesji OMP i nie został wykonany.
- Audytorzy nie dostali skilli stackowych: w katalogu głównym i na pierwszym poziomie nie ma wskaźników stacku.

## Verification Summary

**Method:** Cross-domain correlation and adversarial review (Cross-Verifier + Challenger)

| Metric | Count |
|--------|-------|
| Findings verified | 4 |
| False positives removed | 1 |
| Severity adjustments | 1 |
| Cross-analysis findings | 0 |

### Cross-Analysis (Security <-> Quality)

Brak korelacji. SecurityAuditor nie zgłosił żadnego znaleziska, a znalezisko dokumentacyjne nie dzieli ścieżki kodu z żadnym znaleziskiem jakości ani architektury. Cross-Verifier nie złożył żadnego composite.

### Challenged Findings

- route_task.main() grew into a 78-line multi-command dispatcher (MEDIUM, Maintainability, `omp/native/delivery/scripts/route_task.py:329-406`): usunięte jako false positive. To jeden dyspozytor CLI ze wspólnym ładowaniem planu i jawną walidacją argumentów, a `len(argv) == 5` następuje po walidacji jedynej opcjonalnej flagi. Task 4 wymaga ręcznego parsowania argumentów, a proponowana tabela handlerów dodałaby pośrednią warstwę bez wykazanego błędu.
- New OMP_TOOLS checker unit tests never run in CI (MAINT-001): obniżone z MEDIUM do LOW. Luka jest realna, ale CI uruchamia sam checker na zainstalowanym OMP. Bez pokrycia zostają głównie parser i kontrakt komunikatów.

### Rejected by auditors (self-falsification)

**Security:**
- Subprocess calls with partial executable path (bandit B404/B603/B607) — false positive: listy argumentów bez shella; wejściem jest rozwiązany katalog główny i jeden element `--grep=` (`omp/native/delivery/scripts/route_task.py:277`).
- git rev-parse invoked with a git-derived ref (bandit B603/B607) — false positive: wynik `%H` z `^` albo dosłowne `HEAD` jako argument (`omp/native/delivery/scripts/route_task.py:320`).
- NUL byte in a commit body could desync delivered() parsing — brak scenariusza ataku: git odrzuca NUL w wiadomościach, a potrzebny dostęp do zapisu i tak pozwala wypchnąć kod (`omp/native/delivery/scripts/route_task.py:289`).
- Resume trusts Delivery-* trailers from any reachable commit (CWE-345) — celowe według Task 4; nie przekracza granicy zaufania, a `BASE` może się przesunąć tylko wcześniej (`omp/native/delivery/scripts/route_task.py:275`).
- Symlinks named node_modules no longer rejected in native trees; skills/node_modules/SKILL.md is read through the link — brak scenariusza ataku: próba nie przeniosła do wyniku nic spoza drzewa; błąd walidacji pokazuje jedną linię tylko lokalnemu budującemu (`scripts/build_omp_edition.py:331`).
- Plan heading text interpolated into ctx.ui.notify (CWE-150) — brak granicy zaufania: plan pochodzi z tej samej sesji albo od użytkownika (`omp/native/delivery/extensions/delivery.ts:130`).
- check_omp_tools.py lets ValueError/KeyError escape as a traceback with exit 1 (CWE-755) — bez wpływu na bezpieczeństwo, kończy się błędem; problem jakości opisany w MAINT-004 (`scripts/check_omp_tools.py:42`).
- CI and contributing docs install @oh-my-pi/pi-coding-agent@latest unpinned (CWE-1104/829) — celowe i sprzed tego zakresu (`.github/workflows/omp-edition.yml:45`).
- Review-report commit interpolates <report path> in a shell template — sprzed tego zakresu, przeniesione bez zmian (`omp/native/delivery/skills/orchestration/SKILL.md:98`).

**Quality:**
- Split import lines in test_check_omp_tools.py — duplikat wyniku narzędzia: ruff I001 (`scripts/test_check_omp_tools.py:13-21`).
- DEFAULT_PACKAGE is resolved against the CWD — celowe według Task 7; CI uruchamia skrypt z katalogu głównego (`scripts/check_omp_tools.py:18`).
- NATIVE_SKIPPED_DIRS declared mid-module — celowe według Task 2 (`scripts/build_omp_edition.py:279`).
- build_native copy loop walks a real bun-installed node_modules before filtering — celowe według Task 2 (`scripts/build_omp_edition.py:328-345`).
- CI tool check against @latest OMP can fail unrelated PRs — celowe według Assumptions planu (`.github/workflows/omp-edition.yml:44-48`).
- DoneTest.run_router duplicates RoutingFixture.run_router — 2 linie, poniżej progu duplikacji (`omp/native/delivery/tests/test_route_task.py:420-421`).
- Repeated approved-prompt construction in delivery.test.ts — poniżej progu, zgodne ze stylem pliku (`omp/native/delivery/tests/delivery.test.ts:137-168`).
- checkPlan returns unvalidated JSON typed as PlanCheck — router jest dostarczany w tym samym pluginie, a testy hooków uruchamiają prawdziwy router (`omp/native/delivery/extensions/delivery.ts:58-62`).
- Signature test hard-depends on ssh-keygen and git 2.34 without a skip guard — spekulatywne: CI i lokalne środowisko je mają (`omp/native/delivery/tests/test_route_task.py:458-475`).
- commit_message ValueError text discarded and rebuilt in main — celowe według Task 4 (`omp/native/delivery/scripts/route_task.py:379-382`).
- Approval hands a plan with valid tasks plus problems to delivery, which then stops at preflight — celowe według Task 5; bramka `xd://propose` blokuje taki plan wcześniej (`omp/native/delivery/extensions/delivery.ts:130-135`).
- Review commit uses $SLUG while plan commit uses <SLUG> — sformułowanie sprzed tego zakresu (`omp/native/delivery/skills/orchestration/SKILL.md:98`).
- zip() without strict in route() — poza zakresem zmian; ruff B905 (`omp/native/delivery/scripts/route_task.py:121`).

**Documentation:**
- check_omp_tools.py CI gate not described in contributing.md or CLAUDE.md — żadne udokumentowane twierdzenie nie stało się fałszywe (`docs/contributing.md:171`).
- README Python prerequisite sentence omits the new approval warning — zdanie nadal jest prawdziwe (`README.md:27`).
- Delivery guide does not describe the approval-time Delivery skipped warnings — twierdzenia przewodnika pozostają prawdziwe (`docs/plugins/delivery.md:31`).
- Guide omits that a plan saved to docs/plans stays untracked when the plan check stops — zapisana decyzja planu (Task 6 A, krok 8) (`docs/plugins/delivery.md:31`).
- CLAUDE.md native-copy rule omits the node_modules exclusion — sprzed tego zakresu (`CLAUDE.md:60`).
- Extension header says xd://propose rejects blocks the router cannot route — komentarz w kodzie, nie dokumentacja (`omp/native/delivery/extensions/delivery.ts:5`).
- SKILL.md says done reads the branch's delivery commits while it scans all history reachable from HEAD — sformułowanie ustalone w Task 6 E (`omp/native/delivery/skills/orchestration/SKILL.md:123`).
- scripts/test_check_omp_tools.py is not run in CI — to nie problem dokumentacji; przekazane do jakości jako MAINT-001 (`.github/workflows/omp-edition.yml:47`).

### Doctrine-gap candidates

- No repo rule on pinning npm packages installed in CI — akcje są przypięte do SHA, a `@oh-my-pi/pi-coding-agent@latest` jest instalowany i wykonywany; ten zakres dodaje krok i instrukcję, które od niego zależą.
- No Python lint configuration — domyślny ruff zgłasza 159 × E501 na przyjętym stylu długich linii i zagłusza realne sygnały.
- No TypeScript type check for the delivery extension — brak `tsconfig.json`; CI uruchamia tylko `bun test`.
- No written rule on symlinks in plugin sources — zakaz symlinków i wyjątek dla `node_modules` w `omp/native/` nie są opisane w CLAUDE.md (zgłoszone już w `docs/reviews/2026-09-23-feat-omp-edition.md:700`).
- No rule that the PR template checklist mirrors contributing.md's Pull Request Requirements — powiązanie istnieje w praktyce, ale nigdzie nie jest zapisane.
- No rule on which CI gates CLAUDE.md or contributing.md must document — CLAUDE.md opisuje część checkerów CI, ale nie `check_execution_boundary.py`, `check_contract.py` ani nowy `check_omp_tools.py`.
