# Modelado, evaluación y prevención de fuga

## Baseline Poisson

El MVP estima intensidades positivas `λ_local` y `λ_visitante` con información disponible antes del saque inicial. La probabilidad conjunta inicial supone independencia condicional:

```text
P(X=x,Y=y) = Poisson(x; λ_local) · Poisson(y; λ_visitante)
```

Se calcula para 0…8 goles y se normaliza explícitamente para que la matriz sume uno. De ella se derivan 1X2, totales, ambos anotan y marcadores probables. El truncamiento, la independencia y la igualdad media-varianza son límites conocidos; Dixon–Coles o binomial negativa sólo se incorporarán después de compararlos mediante backtesting.

## Features point-in-time

Para un partido con fecha `t`, cada ventana aplica primero `shift(1)` dentro del equipo y después el rolling. Ningún resultado de `t` ni de fechas posteriores puede participar. El pipeline conserva `feature_cutoff_at` y valida que `max(source_match_date) < target_match_date`.

Los resultados históricos de OpenFootball pueden alimentar goles anotados/recibidos, forma, local/visitante, rachas, Elo derivado de resultados y el baseline Poisson. Si existe resultado de prórroga o penaltis, el entrenamiento de liga usa el marcador reglamentario disponible; los goles de tanda nunca entran al objetivo de goles. La fuente, archivo y versión importada permanecen en el snapshot de procedencia.

OpenFootball por sí solo no habilita features de tarjetas, goleadores, tiros, córners, xG o minutos. Esos bloques sólo se entrenan si otra fuente aporta observaciones reales y la cobertura queda documentada; la ausencia se conserva como nulo/no disponible, no como cero.

## División

- Entrenamiento: periodo antiguo.
- Validación: periodo posterior para selección e hiperparámetros.
- Test final: periodo más reciente, sellado hasta cerrar decisiones.
- Walk-forward adicional por fecha y reporte por competición/temporada.

Nunca se usa un split aleatorio como única validación.

## Métricas

- Conteos: MAE, RMSE y Poisson deviance.
- Resultado/marcados binarios: Log Loss, Brier Score, ROC-AUC/PR-AUC cuando sean válidos y calibración.
- Marcador: resultado correcto, error absoluto de goles, exact score accuracy y RPS.
- Calibración: reliability diagram, ECE y Brier; Platt/isotónica sólo ajustadas sobre validación.

No se muestra una cifra de rendimiento sin dataset, corte temporal, versión y tamaño de muestra.

## Calidad y confianza

La calidad de datos no reutiliza la probabilidad predicha. En el MVP es una heurística explícita de cobertura histórica y completitud (alineación, estadísticas y árbitro), acompañada por fuentes, antigüedad y campos faltantes. No se presenta como rendimiento del modelo.

La confianza sólo será distinta de cero cuando exista calibración temporal validada para el modelo y el ámbito correspondiente. Hasta entonces se reporta `0`, método `unavailable` y estado `not_calibrated`. Ningún score se rellena con datos inventados.

El reparto de intensidad del primer tiempo usa un supuesto técnico visible del 45 % y no se considera calibrado. Si falta historial real previo para ambos equipos o para la competición, no se genera una predicción.

## Registro

Cada artefacto guarda nombre/versión, fecha máxima de datos, features, hiperparámetros, métricas, fuentes, hash del dataset/pipeline y estado `candidate|active|archived`. Activar una versión es una acción explícita; nunca se reemplaza silenciosamente el modelo activo.
