# MatchVision AI — backend offline

Backend educativo FastAPI para importar históricos locales, crear un partido futuro, generar un baseline Poisson 0–8 y evaluar después contra un resultado real. No contiene integraciones de red, proveedores Live ni variables para API keys.

## Arranque local

```bash
cd backend
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
alembic upgrade head
uvicorn app.main:app --reload
```

SQLite (`sqlite:///./matchvision.db`) es el valor por defecto. PostgreSQL es opcional mediante `DATABASE_URL=postgresql+psycopg://...` e instalando `pip install 'psycopg[binary]'`. La documentación OpenAPI queda en `http://localhost:8000/docs`.

El arranque carga un conjunto pequeño demo, siempre marcado con `is_mock_data=true`. Para desactivarlo usa `SEED_DEMO_DATA=false`. Las rutas se controlan con `DATA_ROOT` y `MODEL_ROOT`; el límite de uploads usa `MAX_IMPORT_SIZE_MB`.

## Flujo MVP reproducible

```bash
python -m app.cli import-football-data \
  --file /ruta/local/E0.csv \
  --competition "Premier League" \
  --season "2025-2026"

python -m app.cli build-features --output data/processed/features.json
python -m app.cli train-goals-model --features data/processed/features.json --version 1.0.0
python -m app.cli predict-match --match-id 1
```

StatsBomb sólo lee una carpeta local:

```bash
python -m app.cli import-statsbomb \
  --directory /ruta/local/statsbomb-open-data \
  --competition-id 11 --season-id 90
```

OpenFootball admite JSON, Football.TXT, ZIP o repositorios/carpetas completos. La CLI inspecciona rutas locales; `preview` y `validate` no persisten partidos:

```bash
python -m app.cli preview-openfootball --path ../data/external/openfootball/espana
python -m app.cli validate-openfootball --path ../data/external/openfootball/world
python -m app.cli import-openfootball \
  --path ../data/external/openfootball/england \
  --competition premier-league
```

El flujo web no recibe rutas del sistema. `POST /api/v1/openfootball/preview` carga uno o varios archivos locales y crea una previsualización pendiente; `POST /api/v1/openfootball/imports/{id}/confirm` realiza la persistencia. La importación conserva `match_source_records`, procedencia por campo, duplicados y conflictos, y nunca mezcla goles de tanda con el marcador del partido. `leagues`, `clubs` y `players` se persisten sólo como identidad (códigos/alias, ciudad/estadio y datos biográficos disponibles), nunca como rendimiento.

## API principal

- `GET /api/v1/health`
- `GET|POST /api/v1/competitions`
- `GET|POST|PATCH|DELETE /api/v1/teams` y `/players`
- `POST /api/v1/matches` para crear un partido futuro
- `POST /api/v1/matches/manual` para resolver o crear entidades por nombre
- `GET|PUT|DELETE /api/v1/matches/{id}/lineups`
- `POST /api/v1/predictions/match`
- `GET /api/v1/predictions/{id}` y `/predictions/history`
- `POST /api/v1/matches/{id}/result`
- `POST /api/v1/predictions/{id}/evaluate`
- `POST|GET /api/v1/imports`, reproceso, eliminación y `/imports/templates/{matches|players|player_matches|upcoming_matches}`
- `POST /api/v1/openfootball/preview`; confirmar, listar, consultar, reprocesar o eliminar en `/api/v1/openfootball/imports`
- `GET /api/v1/openfootball/quality` para cobertura y antigüedad reales por competición
- `GET /api/v1/openfootball/conflicts` y rutas `/resolve` para decisiones manuales auditables de entidad o campos de partido
- `GET /api/v1/models/{name}/metrics` y `/calibration`

La predicción guarda un snapshot inmutable de variables y respuesta. Registrar el resultado sólo añade metadatos de evaluación y filas `PredictionOutcome`; no recalcula ni modifica el análisis original.

## Seguridad y calidad

- ZIP sin extracción arbitraria, rechazo de `..`, límites de tamaño y un único payload permitido en uploads web.
- Sin rutas de archivo suministradas por el cliente HTTP.
- Deduplicación estable, errores por fila y trazabilidad de importaciones.
- Variables históricas con corte estricto `match_date < kickoff`; partidos simultáneos no se filtran entre sí.
- Matriz Poisson 9×9 normalizada y derivados obtenidos de la misma distribución.
- Métricas/calibración vacías hasta registrar una evaluación real; no se inventan.
- Sin historial previo real y aislado para ambos equipos y la competición, la predicción se rechaza.
- La calidad se etiqueta como heurística de cobertura; la confianza es `0/unavailable` hasta calibrarla.
- Rate limit básico, CORS configurable, logs JSON y secretos redactados.

En instalaciones persistentes ejecuta `alembic upgrade head` antes de arrancar Uvicorn. El `Dockerfile` y `docker-compose.yml` aplican este orden para que una actualización añada tablas/columnas nuevas sin depender de `create_all()`.

## Pruebas

```bash
pip install -r requirements-dev.txt
pytest
```

Las pruebas son completamente offline y cubren normalización, ZIP seguro, aliases, fuga temporal, Poisson, endpoints, importación y la inmutabilidad del snapshot al evaluar.

> Las probabilidades son estimaciones estadísticas educativas, no garantías ni recomendaciones financieras.
