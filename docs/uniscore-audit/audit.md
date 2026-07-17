# Auditoría pública de referencia — Uniscore

Fecha: 2026-07-14  
Método: Chromium local mediante Playwright, navegación visual de páginas públicas.

## Alcance y límites

Se abrió únicamente la interfaz pública de `https://uniscore.com/es` con un navegador real. No se inspeccionaron, invocaron ni documentaron endpoints; no se descargaron ni reutilizaron recursos, código, textos de producto ni datos del sitio. Las capturas son evidencia visual de patrones de navegación, jerarquía y comportamiento responsive, no una fuente de contenido para el producto.

Playwright no estaba instalado en el proyecto, pero el entorno ya disponía de Playwright y de un Chromium local compatible. Chromium se ejecutó correctamente, por lo que no fue necesario instalar dependencias ni modificar `package.json`.

## Rutas y flujos comprobados

| Área pública | Resultado | Evidencia |
| --- | --- | --- |
| Inicio `/es` | Verificado (200). Cabecera, selector de fecha, filtros, grupos de partidos, panel contextual y footer. | `01-home-1440x900.png`, `16-home-footer.png` |
| Calendario y filtros | Verificado. Se abrió el calendario y se activaron En vivo, Próximo y Finalizado; En vivo mostró su estado vacío público. | `02`–`05` |
| Panel de partido contextual | Verificado en la página de inicio. Se abrió el detalle y las vistas disponibles Detalles, Estadísticas, Clasificaciones y Datos. | `06-detail-*.png` |
| Próximos partidos | Verificado (200): `/es/football/fixtures`. | `07-fixtures.png` |
| Resultados | Verificado (200): `/es/football/results`. | `08-results.png` |
| Competición | Verificado (200): ficha pública de Premier League, pestañas y Clasificación. | `09-competition-fixtures.png`, `10-competition-standings.png` |
| Equipo | Verificado (200): ficha pública de Manta, resumen y pestañas internas. | `11-team.png` |
| Búsqueda | Verificado: se abrió el overlay, se introdujo una consulta y se comprobaron los filtros Equipo/Jugador/Liga/Partido. | `12-search-home.png`, `13-search-player.png` |
| Siguiendo | Verificado (200): `/es/favorite`. No se comprobó la persistencia de una acción de favorito. | `15-favorites.png` |

No verificado: una ficha de jugador individual (la búsqueda no expuso un enlace público de jugador durante esta sesión), persistencia de favoritos, y contenido de alineaciones cuando el partido seleccionado no lo ofrecía. Esas piezas no se asumirán como requisitos de la primera entrega.

## Capturas responsive realizadas

| Viewport | Observación verificada |
| --- | --- |
| 1440×900 | Composición de escritorio en tres zonas: rail deportivo, navegación de competiciones/lista y detalle contextual a la derecha. Cabecera amplia con búsqueda. |
| 1280×800 | Se mantienen cabecera de escritorio, rail, columna de competiciones y panel contextual; la lista central se compacta. |
| 1024×768 | Aún se conserva la composición de columnas, con la lista de encuentros como prioridad visual. |
| 768×1024 | Cambio a composición de una columna: cabecera compacta, selector de deporte, tira de fechas, lista a ancho completo y navegación inferior fija. |
| 430×932 | Móvil de una columna; cabecera compacta, controles circulares, lista de tarjetas y barra inferior fija. |
| 390×844 | Mismo patrón móvil, con el detalle trasladado fuera del canvas inicial y acciones concentradas en cabecera/barra inferior. |
| 360×800 | Mismo patrón compacto; fecha y lista siguen siendo utilizables, con navegación inferior persistente. |

Archivos: `17-home-1440x900.png` a `23-home-360x800.png`. Las capturas responsive pueden mostrar skeletons transitorios durante la carga de datos públicos; la jerarquía, los puntos de ruptura y los controles son los elementos auditados. En móvil se observó además una franja promocional de apertura de app que ocupa altura inicial: no se trasladará al nuevo producto.

## Patrones útiles (sin replicar la marca ni el diseño)

- Jerarquía práctica: fecha y estado del partido antes de los detalles; la ficha completa aparece sólo al seleccionar una tarjeta.
- La lista agrupa encuentros por competición y deja acciones secundarias (favorito, más opciones) en cada fila.
- Un layout de tres columnas funciona en escritorio si el contexto del partido es opcional y puede convertirse en página/drawer en pantallas pequeñas.
- Las fichas de competición y equipo usan encabezado identificable, pestañas cortas y contenido denso en tarjetas; es un modelo apropiado para datos locales, no un diseño que se vaya a copiar.
- La barra inferior móvil reduce la navegación a acciones de alta frecuencia. Para MatchVision se limitará a Hoy, Calendario, Favoritos y Más.

## Auditoría del proyecto actual

### Activos que se conservan

- Backend FastAPI, SQLite/migraciones, validaciones y contratos ya existentes.
- Importación local, conectores y catálogos de OpenFootball, junto con StatsBomb, datos manuales y resolución de entidades.
- Pipeline ML (features, entrenamiento, evaluación y predicciones) y registro de modelos.
- Paneles de administración/importación/conflictos y los proveedores de React Query.
- Componentes reutilizables de UI y gráficas donde encajen, sin sustituirlos por dependencias nuevas.

### Hallazgos de arquitectura

- El frontend actual sólo expone la raíz (`frontend/app/page.tsx`) y monta `MatchVisionApp`; sus secciones (`analysis`, `data`, `models`, `history`) viven en estado local, no en rutas navegables.
- La interfaz actual tiene una shell de laboratorio/administración, no un portal de partidos. Su rail lateral fijo y el área de análisis deben pasar a `/admin`, no eliminarse.
- El listado existente consulta principalmente próximos partidos y normaliza datos mínimos. Para el portal se necesitará una lectura aditiva de partidos locales con fecha, estado, competición y resultado, sin cambiar el flujo de importación.
- Los datos locales pueden estar vacíos tras una instalación limpia. La interfaz debe comunicar estado vacío/carga de forma honesta y no presentar predicciones ni resultados inventados.

## Plan de entrega incremental

### UI-1 — base del portal (siguiente implementación)

1. Añadir rutas `/football` y `/admin`; hacer que `/` redirija a `/football`, conservando el shell administrativo actual bajo `/admin`.
2. Crear el shell MatchVision: cabecera, búsqueda local, accesos, tira de fechas y layout de tres zonas en escritorio.
3. Implementar la vista inicial de agenda local con grupos por competición, estados y una tarjeta seleccionable; el panel derecho mostrará sólo información local disponible.
4. Aplicar los breakpoints auditados: tres zonas en escritorio, una columna/drawer en tablet y móvil, y barra inferior móvil con Hoy, Calendario, Favoritos y Más.
5. Mantener la estética propia de MatchVision (sin logotipo, paleta, textos, componentes ni datos de Uniscore) y sin peticiones a proveedores externos desde el frontend.

Validación de UI-1: navegación real entre rutas, selección de fecha y filtros con estados locales, comportamiento de búsqueda vacía, layouts en 1440/1024/768/430/390/360, y `lint`, TypeScript y tests existentes.

### Entregas posteriores, no incluidas en UI-1

- UI-2: detalle de partido con rutas propias y pestañas disponibles según datos locales.
- UI-3: calendario, competición, equipo, jugador y favoritos persistentes locales.
- UI-4: mejoras aditivas de API/SQLite para el listado de portal y administración pulida de fuentes/importaciones.
- UI-5: incorporación contextual de análisis y predicciones existentes, siempre distinguiendo pronóstico de resultado real.

## Riesgos a controlar

| Riesgo | Mitigación |
| --- | --- |
| Contrato de partidos insuficiente para agenda/resultado | Añadir endpoints o campos de lectura de forma compatible; no alterar importadores. |
| Estados inconsistentes entre fuentes locales | Normalizar `scheduled/live/finished/postponed` en una capa de presentación documentada. |
| Datos locales vacíos o incompletos | Estados vacíos explícitos, etiquetas de fuente y fecha de actualización. |
| UI administrativa mezclada con portal público | Rutas separadas, componentes compartidos sólo donde aporten valor. |
| Densidad excesiva en móvil | Una columna, detalle bajo demanda y navegación inferior de cuatro acciones. |

## Inventario de evidencia

Se guardaron 25 PNG en este directorio: inicio, calendario, filtros, detalle contextual, fixtures, resultados, competición, clasificación, equipo, búsqueda, favorito, footer y los siete viewports requeridos. Los nombres son secuenciales para que la auditoría se pueda revisar sin reejecutar el navegador.
