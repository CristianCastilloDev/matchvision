# Fuentes de datos y condiciones de uso

Esta página es un registro técnico, no asesoría jurídica. Las condiciones pueden cambiar: confirma el documento oficial antes de descargar, publicar o usar comercialmente cualquier dato.

## OpenFootball

| Campo | Detalle |
| --- | --- |
| Fuentes oficiales | <https://github.com/openfootball/football.json>, repositorios de país/región y catálogos `leagues`, `clubs` y `players` de la organización OpenFootball |
| Tipo | Calendarios, fechas, jornadas, equipos y resultados históricos; catálogos de identidad de ligas, clubes, estadios y jugadores |
| Formato | JSON generado y Football.TXT; se aceptan archivos, carpetas o ZIP locales |
| Cobertura | Varía por repositorio y temporada. `world` incluye, cuando están disponibles, Liga MX/Liga de Expansión, MLS, J.League y otras competiciones fuera de Europa |
| Actualización | Voluntaria e irregular; siempre se muestra la fecha real del último partido importado |
| Licencia observada | Datos, esquema y scripts dedicados al dominio público mediante CC0 1.0 en los repositorios oficiales revisados |
| Campos usados | Competición, temporada, ronda/jornada, fecha/hora, equipos, resultado final/descanso, sede, asistencia y notas cuando existan |
| Limitaciones | No es una fuente completa de xG, tiros, córners, tarjetas, alineaciones ni rendimiento individual |

OpenFootball es una fuente histórica principal, pero no una fuente en vivo. La aplicación no consume sus URLs durante la ejecución: el usuario descarga o clona el material por separado y después lo previsualiza e importa desde disco. Los catálogos `clubs`, `leagues` y `players` sólo enriquecen identidad; en particular, el catálogo de jugadores nunca se transforma en estadísticas de rendimiento.

## StatsBomb Open Data

| Campo | Detalle |
| --- | --- |
| Fuente oficial | <https://github.com/statsbomb/open-data> |
| Tipo | Competiciones, temporadas, partidos, alineaciones, eventos y 360 en encuentros seleccionados |
| Formato | JSON |
| Cobertura | Selección de competiciones/temporadas; no debe asumirse cobertura completa |
| Actualización | Irregular; se conserva `source_updated_at` y una copia raw |
| Condiciones observadas | Uso público orientado a investigación/análisis. Al publicar o compartir resultados se solicita atribuir a StatsBomb y usar su marca según sus condiciones |
| Campos usados | Equipos, jugadores, fechas, marcadores, eventos, tiros/xG, faltas, tarjetas, minutos y posiciones cuando existan |
| Limitaciones | Esquema/eventos pueden evolucionar; 360 sólo existe en parte de la muestra |

Antes de ejecutar ingestión, revisa el `LICENSE.pdf`, el README y el acuerdo enlazado por StatsBomb. El proyecto guarda atribución y no redistribuye los JSON raw.

## Football-Data.co.uk

| Campo | Detalle |
| --- | --- |
| Fuente oficial | <https://www.football-data.co.uk/data.php> |
| Tipo | Resultados agregados, descanso, tiros, córners, faltas, tarjetas, árbitros y cuotas históricas según liga/temporada |
| Formato | CSV/XLSX; columnas variables |
| Cobertura | Múltiples ligas y temporadas, con profundidad desigual |
| Actualización | Por temporada/competición; registrar fecha de descarga |
| Condiciones observadas | El sitio permite descargar archivos gratuitamente, pero también indica derechos reservados; no se asume una licencia general de redistribución o uso comercial |
| Campos usados | FTHG, FTAG, HTHG, HTAG, HS, AS, HST, AST, HC, AC, HF, AF, HY, AY, HR, AR y árbitro cuando estén disponibles |
| Limitaciones | Columnas faltantes o renombradas; significado y cobertura deben validarse por archivo |

Uso por defecto: análisis educativo privado, caché local y sin redistribución. Antes de publicar un dataset derivado o usarlo comercialmente, solicita/valida autorización explícita. Las cuotas se mantienen separadas y opcionales.

## Otros datasets públicos

La extensión se realiza con importadores de archivos para repositorios públicos de GitHub, Kaggle u otras fuentes descargables. Ninguna fuente es obligatoria durante la ejecución. Cada importador debe documentar licencia, estructura, versión, cobertura, retención, redistribución y atribución; después convierte el archivo al mismo dominio normalizado.

## Estructura local

```text
data/external/
├── openfootball/
│   ├── football.json/
│   ├── england/
│   ├── espana/
│   ├── world/
│   ├── leagues/
│   ├── clubs/
│   └── players/
├── statsbomb/
│   ├── competitions.json
│   ├── matches/
│   ├── events/
│   ├── lineups/
│   └── three-sixty/
└── football-data/
    └── E0.csv
```

La información actual se incorpora mediante formularios y plantillas. No se configura ninguna API deportiva, clave, suscripción o prueba gratuita.

## Reglas comunes

- Conservar el payload raw sólo cuando el proveedor lo permita.
- Guardar `data_source`, `source_updated_at`, URL/versión y hash.
- Conservar cada registro original en `match_source_records`; un partido normalizado puede tener varias procedencias.
- Resolver coincidencias por fecha, local, visitante, competición y temporada, nunca únicamente por el nombre de un equipo.
- Registrar resultados discrepantes como conflictos revisables en vez de sobrescribirlos.
- Respetar términos y robots; no hacer scraping prohibido ni depender de descargas durante la ejecución.
- No mezclar observaciones mock y reales sin una columna explícita.
- Propagar campos faltantes en vez de convertirlos silenciosamente en cero.
- Añadir atribución en cualquier salida pública que la requiera.
