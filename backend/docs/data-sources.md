# Fuentes de datos del MVP offline

MatchVision AI no llama APIs deportivas, no acepta API keys y no descarga datos en runtime.

## StatsBomb Open Data

- Entrada: carpeta local obtenida legítimamente por el usuario, con estructura `data/competitions.json`, `data/matches`, `data/lineups` y `data/events`.
- Licencia: la aplicable a StatsBomb Open Data en el momento en que el usuario obtiene la copia. El usuario debe conservar atribución y revisar sus términos antes de importar.
- Cobertura: competiciones, temporadas, partidos, alineaciones y eventos presentes en la copia local.
- Actualización: manual; MatchVision registra la procedencia y conserva caché cruda local.
- Limitaciones: cobertura desigual y posibles campos ausentes; el importador no completa valores faltantes.
- Uso: entrenamiento/análisis educativo local. No se redistribuyen los archivos originales.

## Football-Data.co.uk

- Entrada: CSV o ZIP local proporcionado expresamente por el usuario.
- Licencia/términos: deben revisarse en el sitio del proveedor antes de obtener y usar el archivo.
- Cobertura: resultados agregados y, según liga/temporada, tiros, córners, faltas, tarjetas y árbitro.
- Actualización: manual.
- Campos: `Date`, `HomeTeam`, `AwayTeam`, `FTHG`, `FTAG` y campos opcionales documentados en la plantilla/importador.
- Limitaciones: el esquema cambia entre archivos. El importador reporta columnas y errores por fila; cuotas históricas se aíslan y no forman parte del baseline web.
- Uso: análisis educativo local; no scraping ni descarga automatizada.

## Datos manuales y demostración

- Datos manuales: creados por formularios/API o plantillas CSV; `data_source=manual/local`.
- Datos demo: siempre llevan `is_mock_data=true`, `data_source=demo` y un aviso visible en cada predicción.
- Nunca se mezclan silenciosamente: la respuesta enumera `historical_sources`, antigüedad y faltantes.
