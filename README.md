# MatchVision AI

MatchVision AI es una aplicación educativa local de analítica de fútbol. Convierte históricos normalizados en estimaciones probabilísticas explicables, conserva la trazabilidad del modelo y mantiene separadas la probabilidad de un evento, la calidad de datos y la confianza calibrada.

> Las probabilidades presentadas son estimaciones estadísticas generadas a partir de datos históricos y modelos matemáticos. Los resultados reales pueden diferir. La información se proporciona exclusivamente con fines educativos y de análisis y no constituye una garantía ni una recomendación financiera.

## Qué incluye este MVP

- Frontend Next.js/React/TypeScript responsive con dashboard, análisis, resultados, rendimiento e historial.
- API FastAPI tipada, documentación OpenAPI, SQLite local y PostgreSQL en Docker.
- Entidades normalizadas y aliases independientes del proveedor.
- Importadores locales desacoplados para OpenFootball, StatsBomb Open Data y Football-Data.co.uk.
- OpenFootball como histórico principal de calendarios/resultados, con JSON, Football.TXT, carpetas y ZIP; previsualización antes de confirmar.
- Importación local de carpetas StatsBomb, CSV/JSON/ZIP/TXT y plantillas manuales.
- Creación de próximos partidos y registro de resultados sin proveedor en vivo.
- Pipeline reproducible para ingestión, variables previas al partido y baseline Poisson.
- Matriz de marcadores 0–8 normalizada y probabilidades derivadas 1X2, totales y ambos anotan.
- Calidad de datos, factores, advertencias, versión y fecha en cada análisis; la confianza permanece `unavailable` hasta contar con calibración validada.
- Pruebas de probabilidades, normalización, endpoints y fuga temporal.
- Docker Compose, Alembic y CI.

No se publican métricas de calidad inventadas. Las métricas visibles en modo demostración están etiquetadas como datos de ejemplo; un modelo entrenado debe evaluarse cronológicamente antes de activarse.

## Arquitectura

```text
matchvision-ai/
├── frontend/              # Next.js + UI y cliente REST
├── backend/               # FastAPI + dominio + ML + persistencia
├── data/
│   ├── raw/               # Copias inmutables por proveedor (no versionadas)
│   ├── interim/           # Datos validados/resueltos
│   ├── processed/         # Features reproducibles
│   └── external/          # OpenFootball, StatsBomb y Football-Data locales
├── models/                # Artefactos versionados fuera de Git
├── notebooks/             # Exploración, nunca lógica de producción
├── scripts/
├── docs/
├── docker-compose.yml
└── .env.example
```

El contrato interno utiliza IDs propios, `data_source`, `source_updated_at` e `is_mock_data`. Los importadores sólo traducen formatos externos; las reglas de negocio y los modelos nunca dependen directamente de columnas de una fuente. La ejecución normal no consulta APIs deportivas ni solicita claves.

## Flujo funcional

```text
Descarga/clonado manual de datasets públicos
→ validación y copia raw
→ normalización
→ resolución de entidades
→ orden cronológico
→ features con shift (sólo pasado)
→ entrenamiento Poisson
→ registro de modelo
→ creación manual de partido futuro
→ predicción y explicación
→ registro manual de resultado
→ evaluación sin reescribir la predicción
```

El baseline estima `lambda_home` y `lambda_away`, construye `P(G_local=x, G_visitante=y)` para 0…8 y normaliza la masa antes de derivar mercados. Consulta [docs/modeling.md](docs/modeling.md) para límites y evaluación.

## Inicio local sin Docker

Requisitos: Python 3.12+, Node.js 22.13+ y npm. Docker no es obligatorio.

```bash
cd backend
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
alembic upgrade head
uvicorn app.main:app --reload
```

En otra terminal:

```bash
cd frontend
npm install
npm run dev
```

El backend utiliza `sqlite:///./matchvision.db` de forma predeterminada y la aplicación sigue funcionando sin internet. `NEXT_PUBLIC_API_URL` sólo apunta a la API local propia, no a un servicio deportivo.

## Docker opcional

Requisitos: Docker y Docker Compose.

```bash
cp .env.example .env
docker compose up --build
```

- Aplicación: `http://localhost:3000`
- API: `http://localhost:8000/api/v1`
- OpenAPI: `http://localhost:8000/docs`

El arranque inicial carga un conjunto pequeño simulado y claramente marcado. No hace descargas externas.

## Desarrollo local

Alternativamente, los comandos `make` automatizan el mismo entorno local.

```bash
make setup
make dev-backend
# En otra terminal:
make dev-frontend
```

El backend usa SQLite si no se define `DATABASE_URL`. PostgreSQL queda como opción propia para instalaciones futuras; no se requiere una base administrada.

## Pipeline y CLI

Desde `backend/`, con el entorno activado:

```bash
python -m app.cli import-statsbomb \
  --directory ../data/external/statsbomb \
  --competition-id 11 \
  --season-id 90
python -m app.cli import-football-data \
  --file ../data/external/football-data/E0.csv \
  --competition premier-league \
  --season 2025-2026
python -m app.cli preview-openfootball \
  --path ../data/external/openfootball/espana
python -m app.cli validate-openfootball \
  --path ../data/external/openfootball/world
python -m app.cli import-openfootball \
  --path ../data/external/openfootball/england \
  --competition premier-league
python -m app.cli build-features --output data/processed/features.json
python -m app.cli train-goals-model \
  --features data/processed/features.json \
  --version 1.0.0
python -m app.cli evaluate-models
python -m app.cli predict-match --match-id 1
```

Los importadores leen archivos locales. Una descarga pública opcional debe ser una acción separada, guardar primero la copia local y respetar las condiciones de la fuente; la aplicación y CI nunca dependen de red.

El panel **Fuentes de datos** acepta CSV, JSON, Football.TXT, carpetas y ZIP; lista campos, conteos y errores y permite reprocesar o eliminar una importación. El flujo específico **Importar OpenFootball** detecta el dataset, país, competición, temporada y catálogos de identidad, presenta una muestra y sólo persiste tras confirmación. Los conflictos se revisan ahí mismo: una resolución de entidad selecciona un candidato explícito y una discrepancia de partido exige elegir el origen de cada grupo de campos. El panel **Crear partido** captura la información actual que no existe en históricos. También se ofrecen plantillas CSV exactas para partidos, jugadores, jugador-partido y próximos partidos. Los catálogos OpenFootball de clubes, ligas y jugadores enriquecen identidad, nunca estadísticas deportivas inexistentes.

## API principal

```text
GET  /api/v1/health
GET  /api/v1/competitions
GET  /api/v1/competitions/{id}/seasons
GET  /api/v1/matches/upcoming
GET  /api/v1/matches/{id}
POST /api/v1/matches/manual
PUT  /api/v1/matches/{id}/lineups
POST /api/v1/matches/{id}/result
POST /api/v1/predictions/match
GET  /api/v1/predictions/{id}
GET  /api/v1/predictions/match/{match_id}
GET  /api/v1/models
GET  /api/v1/models/{name}/metrics
GET  /api/v1/models/{name}/calibration
GET  /api/v1/predictions/history
POST /api/v1/imports
GET  /api/v1/imports/templates/{name}
POST /api/v1/openfootball/preview
POST /api/v1/openfootball/imports/{id}/confirm
GET  /api/v1/openfootball/imports
POST /api/v1/openfootball/imports/{id}/reprocess
DELETE /api/v1/openfootball/imports/{id}
GET  /api/v1/openfootball/quality
GET  /api/v1/openfootball/conflicts
POST /api/v1/openfootball/conflicts/entities/{id}/resolve
POST /api/v1/openfootball/conflicts/matches/{source_record_id}/resolve
```

Ejemplo:

```bash
curl -X POST http://localhost:8000/api/v1/predictions/match \
  -H 'Content-Type: application/json' \
  -d '{"match_id":1,"prediction_types":["match_result","total_goals"],"use_confirmed_lineups":false}'
```

## Pruebas

```bash
make test
```

El backend comprueba, entre otros invariantes, que cada probabilidad esté entre 0 y 1, 1X2 sume 1, la matriz de marcadores esté normalizada, las ventanas móviles excluyan el partido objetivo y cualquier partido posterior, los periodos de un resultado sean coherentes y una importación repetida sea idempotente.

## Fuentes y uso permitido

No redistribuyas datos raw salvo que su licencia lo permita. OpenFootball declara sus datos, esquema y scripts bajo CC0; StatsBomb exige atribución al publicar análisis; Football-Data ofrece descargas gratuitas pero su sitio conserva derechos y no se ha asumido una licencia general de redistribución. Revisa las condiciones vigentes antes de cada ingestión o uso comercial. Detalle en [docs/data-sources.md](docs/data-sources.md).

## Decisiones y límites del MVP

- Poisson es un baseline interpretable, no una afirmación de rendimiento.
- Tarjetas, goleadores y riesgo de tarjeta se exponen como contratos/demo hasta entrenar modelos dedicados con cobertura suficiente.
- La calidad describe cobertura y disponibilidad. La confianza se reporta como `0/unavailable` mientras no exista calibración temporal validada; nunca se infiere desde la probabilidad.
- Sin historial previo real para ambos equipos y la competición, la API rechaza el análisis en vez de aplicar un prior ficticio.
- No se entrena dentro de una solicitud web.
- No se mezclan datos mock y reales sin etiqueta.
- Los registros originales de varias fuentes se conservan; duplicados y resultados discrepantes se muestran, no se sobrescriben silenciosamente.
- OpenFootball alimenta resultados, forma, Elo y Poisson, pero por sí solo no habilita modelos de tarjetas, goleadores, tiros, córners o xG.
- No se usan cuotas como objetivo ni se presenta el producto como herramienta de apuestas.

## Pendientes por fase

1. Ejecutar ingestiones permitidas y completar resolución manual de aliases.
2. Entrenar/backtestear el baseline por fecha, liga y temporada.
3. Añadir tarjetas con binomial negativa y calibración.
4. Incorporar modelos jugador-partido condicionados a participación.
5. Ampliar cobertura geográfica y catálogos OpenFootball sólo después de validar cada formato y temporada.
6. Añadir controles de acceso locales, colas y observabilidad sólo si se evoluciona a una instalación multiusuario, sin servicios externos obligatorios.

Consulta [docs/architecture.md](docs/architecture.md) y [docs/modeling.md](docs/modeling.md) para el diseño completo.
El flujo de importación y captura está documentado en [docs/offline-workflow.md](docs/offline-workflow.md).
