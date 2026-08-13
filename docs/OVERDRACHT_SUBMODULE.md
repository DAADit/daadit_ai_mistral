# Overdracht: de gekopieerde map in de deploy-repo vervangen door deze submodule

Deze module bestond in twee lijnen: deze productrepo en een gekopieerde map
`daadit_ai_mistral/` in de deploy-repo (`DAADit/daadit`, en de productielijn
`adriedaadit/daadit` die daaruit draait). Sinds `19.0.9.0.0` staat al het werk
van beide lijnen hier; de laatste stap is de map in de deploy-repo vervangen
door een submodule die op de release van deze repo gepind staat.

Die stap kan alleen een mens met push-rechten op `adriedaadit/daadit` doen.
Hieronder de exacte commando's, de controle achteraf en de valkuil.

## 0. Voorwaarden

- De consolidatie-PR op `DAADit/daadit_ai_mistral` is **gemerged** in `main`.
- Op die merge-commit staat de release-tag, en die tag hoort gelijk te zijn aan
  het manifest. De workflow `.github/workflows/sync-release.yml` weigert bij
  ongelijkheid.

```bash
# in een clone van DAADit/daadit_ai_mistral, op de gemergede main
git checkout main && git pull
python3 -c "import ast; print(ast.literal_eval(open('daadit_ai_mistral/__manifest__.py').read())['version'])"
# → 19.0.9.0.0

git tag -a v19.0.9.0.0 -m "Samenvoeging van de product- en deploy-lijn (OAS 711)"
git push origin v19.0.9.0.0
```

> Let op: het pushen van een `v*`-tag start `sync-release.yml`, die met de
> `DEPLOY_PAT` een **PR** opent op `adriedaadit/daadit` én op
> `nimbleconsulting/BroStar` waarin de map wordt overschreven met de tag-inhoud.
> Dat is de oude, kopiërende route. Wil je alleen de submodule-omzetting, sluit
> die PR's dan of zet de workflow uit voordat je tagt.

## 1. De omzetting in de deploy-repo

Het patroon is dat van `daadit_ai_claude`: de submodule hangt op het pad van de
oude map, en de submodule-root bevat zelf weer de map met het manifest
(`daadit_ai_mistral/daadit_ai_mistral/__manifest__.py`). Odoo.sh zoekt recursief
naar `__manifest__.py`, dus de addons-path blijft ongemoeid.

```bash
git clone https://github.com/adriedaadit/daadit.git && cd daadit
git checkout -b oas711-mistral-submodule

curl -fsSLO https://raw.githubusercontent.com/DAADit/daadit_ai_mistral/v19.0.9.0.0/tools/adopt_as_submodule.sh
chmod +x adopt_as_submodule.sh
./adopt_as_submodule.sh --release v19.0.9.0.0
```

Het script weigert zolang de gekopieerde map afwijkt van de tag en schrijft dat
verschil naar `deploy-only.diff` (exitcode 2). **Lees dat bestand.** Wat er
inhoudelijk in staat en niet in de tag zit, is deploy-only werk: PR dat eerst
hier, tag opnieuw, en draai dan verder. Alleen als het verschil aantoonbaar
alleen uit de twee bewust behouden productverbeteringen bestaat (het
JSON-schema van een operator-tool en `MIN_LANG_REF_LETTERS`) plus versienummers,
mag `--force` erbij — bewaar `deploy-only.diff` dan als bewijs bij de PR.

Handmatig komt hetzelfde hierop neer:

```bash
git rm -r daadit_ai_mistral
git submodule add https://github.com/DAADit/daadit_ai_mistral.git daadit_ai_mistral
git -C daadit_ai_mistral checkout v19.0.9.0.0   # detached HEAD = de pin
git add .gitmodules daadit_ai_mistral
git commit -m "daadit_ai_mistral als submodule, gepind op v19.0.9.0.0"
```

`.gitmodules` krijgt er dan dit blok bij, naast dat van `daadit_ai_claude`:

```ini
[submodule "daadit_ai_mistral"]
	path = daadit_ai_mistral
	url = https://github.com/DAADit/daadit_ai_mistral.git
```

Geen `branch =` regel: de pin is een tag, niet een branch. (`daadit_ai_claude`
heeft `branch = 19.0` omdat die pin met `git submodule update --remote`
meebeweegt — dat is precies wat je hier niet wil.)

## 2. Controleren dat Odoo.sh de module vindt

Voordat je de PR mergt:

```bash
# de map met het manifest zit één niveau diep in de submodule
test -f daadit_ai_mistral/daadit_ai_mistral/__manifest__.py && echo pad OK
git -C daadit_ai_mistral describe --tags   # → v19.0.9.0.0
git submodule status                        # geen '+' of '-' voor de hash
```

Op de Odoo.sh dev/staging-build:

- de buildlog noemt `daadit_ai_mistral` bij de geladen modules; blijft die weg,
  dan is de submodule niet uitgechecked (Odoo.sh doet dat wel, maar alleen als
  `.gitmodules` en de gitlink-commit samen in dezelfde commit staan);
- er staat geen tweede kopie van de module meer in de addons-path
  (`find . -name __manifest__.py | grep daadit_ai_mistral` geeft één regel).

## 3. Controleren dat de upgrade daadwerkelijk gedraaid heeft

Na de build op de betreffende tenant:

```
model:  ir.module.module
domein: [("name", "=", "daadit_ai_mistral")]
velden: state, installed_version, latest_version
```

Verwacht: `state = installed`, `installed_version = 19.0.9.0.0` en gelijk aan
`latest_version`. Staat `installed_version` nog op de oude waarde terwijl
`latest_version` 19.0.9.0.0 is, dan is de module gevonden maar niet
geüpgraded — start de upgrade van die module dan expliciet.

Extra bewijs dat de migraties uit `migrations/19.0.9.0.0/` gedraaid zijn:

- het model `daadit.ai.agent.skill` bestaat en heeft records;
- de agents Bo, Dirk, Marit en Coen hebben regels in
  `daadit.ai.agent.activity.scope`;
- Robin heeft `daadit_is_orchestrator = True`;
- Argus heeft `daadit_repair_channel = True`.

## Valkuil: nooit een lager versienummer

Odoo voert een upgrade alleen uit als de versie in het manifest **hoger** is dan
`installed_version`, en slaat bij een lagere waarde de migraties stilzwijgend
over. Meet daarom vlak vóór de omzetting opnieuw wat er in productie draait.

Bij het schrijven van dit document was dat `19.0.6.28.0`, tegen `19.0.6.27.0` in
de map van `DAADit/daadit`: de productielijn liep dus één versie vóór op de
fork. Is productie inmiddels boven `19.0.9.0.0` uitgekomen, tag deze repo dan
eerst op een nummer daarboven en pin op die nieuwe tag — en zoek uit welk werk
achter dat hogere nummer zat, want dat is per definitie een derde lijn die hier
nog niet staat.
