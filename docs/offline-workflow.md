# Flujo completamente local

## 1. Preparar históricos

Coloca repositorios o exportaciones OpenFootball en `data/external/openfootball/`, una copia de `statsbomb/open-data/data` en `data/external/statsbomb/` o CSV descargados manualmente en `data/external/football-data/`. MatchVision no requiere conexión para importarlos, procesarlos o entrenar.

La descarga es una acción manual, previa y separada de la aplicación. Por ejemplo, cuando dispongas de conexión:

```bash
cd data/external/openfootball
git clone https://github.com/openfootball/football.json.git
git clone https://github.com/openfootball/england.git
git clone https://github.com/openfootball/espana.git
git clone https://github.com/openfootball/world.git
git clone https://github.com/openfootball/leagues.git
git clone https://github.com/openfootball/clubs.git
```

No es necesario clonar todos los repositorios. `football.json`, `england`, `espana`, `world`, `leagues` y `clubs` son el conjunto inicial priorizado; `world` aporta, según disponibilidad del propio repositorio, México, Estados Unidos/Canadá, Japón y otras regiones.

El importador mantiene el archivo original, calcula un hash para deduplicar, valida esquema/tipos por fila y registra conteos, campos detectados, errores y duración. Un ZIP se extrae en un temporal aislado: se rechazan rutas absolutas, `..`, enlaces y archivos incompatibles antes de mover cualquier contenido.

OpenFootball acepta JSON, Football.TXT, carpetas y ZIP. La detección identifica repositorio, país, competición y temporada cuando el propio material permite hacerlo. Ausencia de marcador significa `scheduled` y goles nulos, no 0–0. Los resultados de prórroga y tanda permanecen separados del marcador a 90 minutos.

## 2. Importar y entrenar

```bash
cd backend
source .venv/bin/activate
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
```

Las features se calculan con cortes anteriores al partido. Una importación inválida no se mezcla parcialmente con datos válidos sin dejar registro.

En **Fuentes de datos → Importar OpenFootball**, selecciona uno o varios JSON/TXT, un ZIP o una carpeta. La app envía los archivos reales y sus rutas relativas seguras, muestra una muestra antes de persistir y exige **Confirmar importación**. El resultado informa archivos analizados, partidos encontrados/terminados/programados, equipos, competiciones, catálogos, duplicados, conflictos y errores. Una importación registrada puede reprocesarse o eliminarse desde el mismo panel. La sección de conflictos permite seleccionar un candidato de entidad o decidir, campo por campo y con nota de auditoría, cuál valor conservar; no acepta identificadores o campos fuera del conflicto registrado.

La calidad por competición incluye primera y última fecha disponibles, cantidad total/terminada/programada, temporadas, campos presentes y última importación. Es una medida de cobertura; no implica actualidad ni confianza predictiva.

## 3. Crear un próximo partido

En **Crear partido** captura competición, temporada, fecha/hora, local, visitante, estadio, jornada e importancia. Árbitro, clima, lesiones, suspensiones y alineaciones son opcionales. El sistema enlaza los equipos mediante aliases; una coincidencia ambigua requiere revisión.

Si faltan árbitro, alineaciones u otros datos opcionales, el análisis enumera cada ausencia y deja los bloques no sustentados como no disponibles. Si no existe historial previo aislado para ambos equipos y su competición, la API rechaza la predicción: no aplica un prior ficticio. La calidad de datos sí se reporta; la confianza queda `0/unavailable` hasta contar con calibración validada.

## 4. Registrar el resultado

Después del partido, **Registrar resultado** permite añadir marcador final/descanso, goleadores y minutos, tarjetas y amonestados, córners, tiros, tiros a puerta, alineación real y minutos. Se agrega un outcome y se actualizan métricas; el payload, features, probabilidad y fecha de la predicción original permanecen inmutables.

## Plantillas

- `data/templates/matches.csv`
- `data/templates/players.csv`
- `data/templates/player_matches.csv`
- `data/templates/upcoming_matches.csv`

La UI ofrece las mismas cabeceras como descargas locales. Los errores se muestran por número de fila y columna.

## Limitaciones visibles

- Los datasets abiertos no cubren todas las ligas ni necesariamente están actualizados.
- Traspasos, lesiones, suspensiones y alineaciones recientes requieren captura manual.
- Sin minutos esperados, las probabilidades de goleador pierden fiabilidad.
- Sin árbitro, disminuye la confianza del modelo de tarjetas.
- El nivel relativo de un equipo puede cambiar entre temporadas.
- Un histórico antiguo no representa necesariamente la situación actual.

Cada análisis muestra fuente histórica, último partido disponible, cantidad utilizada, antigüedad, campos manuales y campos faltantes.
