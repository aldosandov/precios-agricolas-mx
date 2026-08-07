# precios-agricolas-mx

Precios agrícolas abiertos para México. Convierte el precio de mayoreo que
publica el SNIIM en un **precio de referencia en parcela**: el número que un
productor debería exigir antes de sentarse a negociar con el intermediario que
llega a comprarle la cosecha.

Proyecto de [CIMA A.C.](https://cimaac.org.mx) para productores de
Atzitzintla, Puebla.

## El problema

El intermediario que llega al pueblo sabe a cuánto se está pagando el producto
en la central de abasto. El productor no. La negociación arranca desequilibrada
y el precio lo pone quien tiene el dato.

El dato existe, es público y es gratuito: la Secretaría de Economía publica
precios de mayoreo todos los días hábiles a través del **SNIIM**. También es
prácticamente inusable — vive detrás de un formulario ASP.NET sin API, sin
descarga masiva y sin documentación. Nadie con una parcela va a construir una
serie histórica a mano.

Y el precio de la central tampoco es el precio del productor: entre una y otra
hay flete, merma y comisión de bodeguero. Usar el precio de mayoreo como
referencia directa produce una expectativa equivocada.

## Qué hace

```
precio_parcela = precio_central × (1 − merma) × (1 − comisión) − flete_por_kg
```

Los tres parámetros son editables por quien consulta, con valores por defecto
visibles y el desglose siempre a la vista. La herramienta **no afirma que ese
sea un precio justo** ni que alguien vaya a pagarlo: muestra aritmética sobre
un precio publicado por la Secretaría de Economía y supuestos que el propio
usuario controla.

Además responde "¿este precio es alto o bajo para esta época?", comparando
contra la distribución histórica de la misma semana del año en años
anteriores. Si no hay suficiente historia para esa combinación, la señal no se
publica.

> **Sobre el campo Origen.** El SNIIM lo declara: el origen de frutas y
> hortalizas es de **comercialización**, no de producción. Una fila con origen
> Puebla significa que el producto se comercializó desde Puebla, no que se
> cultivó ahí. Ninguna capa de este proyecto afirma lo contrario.

## Estado

| Fase | Contenido | Estado |
|---|---|---|
| 1 | Ingesta completa y capa cruda en BigQuery | **Lista** |
| 2 | Backfill histórico 1998–2026 | En curso |
| 3 | Capa canónica con vigencias y capa intermedia | Pendiente |
| 4 | Capa final: precio en parcela y estacionalidad | Pendiente |
| 5 | App de Streamlit publicada | Pendiente |

Hoy funciona el camino completo **SNIIM → NDJSON → BigQuery**: barrido diario
de los 43 mercados activos, backfill histórico reanudable, parser probado
contra HTML real, validación de contrato, carga idempotente y job programado en
GitHub Actions. Existe una sola tabla, la cruda; las capas intermedia y final
—y la app— son las fases siguientes.

---

# Cómo funciona el scraping

## Una petición al SNIIM

Todo el scraping es la misma petición repetida: un `GET` sin cookies ni estado
de vista a la página de resultados, con los siete criterios en la URL.

```
ResultadosConsultaFechaFrutasYHortalizas.aspx
  ?fechaInicio=01/07/2011&fechaFinal=30/09/2011   ← la ventana
  &ProductoId=-1                                  ← todos los productos
  &OrigenId=-1                                    ← todos los orígenes
  &DestinoId=210                                  ← ESTE mercado (el criterio fijo)
  &PreciosPorId=2                                 ← kilogramo calculado
  &RegistrosPorPagina=5000
```

La arma [`scraper/query.py`](scraper/query.py), que es puro: no toca la red.
Cuatro cosas que hay que saber de esa URL, todas verificadas en
[`docs/consulta-sniim.md`](docs/consulta-sniim.md):

- **El mercado es el criterio fijo, no el producto.** 49 peticiones por ventana
  en vez de 222, y la tabla conserva `Producto`, `Calidad`, `Presentación` y
  `Origen`.
- **Cada ventana se pide dos veces**, una por `PreciosPorId`: el 1 devuelve el
  precio por presentación comercial y el 2 el precio por kilogramo. La columna
  `Presentación` es idéntica en ambos, así que **cuál contestó no es inferible
  de la respuesta** — hay que registrar lo que se mandó.
- **Todo criterio fijado a un solo valor desaparece de la tabla.** Con
  `fechaInicio == fechaFinal` no hay columna `Fecha`; con el mercado fijo no hay
  `Destino`. Siete formas posibles de la misma tabla.
- **Nunca se pagina.** El botón "siguiente" es un postback con ~213 KB de estado
  de vista. Si el servidor avisa que truncó (`Página 1 de N`, con N > 1), se
  parte la ventana a la mitad y se piden las dos mitades. Eso es
  `query.next_windows()`, y es todo el control de volumen que existe.

## De la respuesta a la fila

El mismo camino para el barrido diario y para el histórico —eso es
[`scraper/harvest.py`](scraper/harvest.py), la mitad compartida—:

| Paso | Archivo | Qué hace y dónde falla |
|---|---|---|
| 1. Leer la tabla | `scraper/parsers/results.py` | Mapea columnas **por encabezado**, nunca por posición. Un encabezado desconocido levanta `UnknownColumn` y **detiene la ingesta**: una columna nueva es una decisión humana |
| 2. Armar el registro | `scraper/contract.py` | Junta lo que dijo la tabla con lo que mandó la consulta. Exige identidad —fecha, producto, presentación, origen, destino, modo de precio—, no calidad. Una fila que no se puede identificar levanta `IncompleteRow` |
| 3. Fila de la capa cruda | `scraper/pipelines/ndjson.py` | Tipa precios, calcula `llave_fila` (sha256 de la llave natural) y marca banderas de calidad. **Marca, nunca corrige** |
| 4. Escribir a disco | `scraper/pipelines/ndjson.py` (`PartitionWriter`) | Un archivo por fecha, mercado y modo: `out/raw/fecha=2011-07-04/destino=210_precio=kilogramo_calculado.ndjson`. Trunca la primera vez que lo toca en la corrida y anexa después, así que repetir una ventana reescribe el día en vez de duplicarlo |

Cualquier excepción del grupo `UNREADABLE` (parser, contrato, rechazo del
servidor, respuesta sin paginador) **pierde esa ventana y nada más**: se anota,
el barrido sigue y la corrida termina en rojo. Lo mismo hace el `errback` con la
petición que nunca volvió — Scrapy se rinde tras `RETRY_TIMES` en silencio, y
sin `errback` esa ventana desaparecería con la corrida en verde.

## Cómo se le pide a la fuente

[`scraper/settings.py`](scraper/settings.py). El SNIIM es un servidor de
gobierno que responde **503** bajo carga, así que el crawl es lento a propósito:

| Ajuste | Valor | Por qué |
|---|---|---|
| `CONCURRENT_REQUESTS` | 2 | Con 4 en paralelo la fuente empieza a dar 503 |
| `DOWNLOAD_DELAY` | 3 s | Mínimo entre peticiones |
| `AUTOTHROTTLE_MAX_DELAY` | 120 s | Un 503 tarda minutos en despejar, no segundos |
| `RETRY_TIMES` | 4 | Sobre 500/502/503/504/408/429 |
| `DOWNLOAD_TIMEOUT` | 180 s | Un trimestre de un mercado son megabytes que el servidor arma en vivo |
| `ROBOTSTXT_OBEY` | `True` | Y el user agent dice quién pregunta y dónde reclamar |
| `HTTPCACHE_ENABLED` | `False` | Cachear 9 600 respuestas de megabytes costaría 20+ GB; reanudar es trabajo de `out/cobertura.sqlite` |

**Subir la concurrencia es la forma más rápida de que bloqueen el proyecto
entero.** No se toca.

---

# La tabla en BigQuery

Proyecto GCP `precios-agricolas-mx`, ubicación **US** (no se puede cambiar sin
recrear). Hoy existe una sola tabla: **`crudo.precios`**, la capa cruda. DDL en
[`transform/raw/prices_table.sql`](transform/raw/prices_table.sql).

```sql
CREATE TABLE `precios-agricolas-mx.crudo.precios` (...)
PARTITION BY fecha
CLUSTER BY producto_raw, destino_id, tipo_precio
```

| Columna | Tipo | Qué guarda |
|---|---|---|
| `fecha` | DATE | Día del precio. Es la columna de partición |
| `producto_raw`, `calidad_raw` | STRING | Literales de la tabla del SNIIM |
| `presentacion_raw` | STRING | `"Caja de 10 kg."` y demás. No cambia entre modos de precio |
| `origen_raw` | STRING | Estado de **comercialización**, no de producción |
| `destino_raw`, `destino_id` | STRING, INT64 | Mercado: etiqueta y el id que se envió |
| `grupo_raw` | STRING | Categoría arrastrada del separador: `Frutas`, `Hortalizas`, `Chiles Secos` |
| `tipo_precio` | STRING | `presentacion_comercial` o `kilogramo_calculado` |
| `precio_min`, `precio_max`, `precio_frecuente` | NUMERIC | El frecuente es la **moda** de la muestra, no el promedio |
| `observaciones` | STRING | Columna `Obs.`; trae el país cuando el origen es Importación |
| `banderas_calidad` | ARRAY\<STRING\> | `precio_ausente`, `precio_cero`, `precio_no_numerico`, `precios_incoherentes` |
| `extraido_en`, `url_consulta` | TIMESTAMP, STRING | Cuándo se descargó y con qué URL exacta: la extracción es reproducible |
| `llave_fila` | STRING | sha256 de la llave natural. Es sobre esto que la carga hace upsert |

**La llave natural** es `(fecha, producto, presentación, origen, destino,
PreciosPorId)`, con la etiqueta cuando la tabla la dio y `id:<n>` cuando la
consulta fijó ese criterio. Incluir el modo de precio es lo que evita que el
precio por kilo pise al precio por caja del mismo día.

**La capa cruda es inmutable**: nada se edita ni se limpia aquí. Un precio en
cero o incoherente entra con su bandera; corregirlo es trabajo de la capa
intermedia, que se reconstruye desde esta sin tocarla.

## Cómo entra la carga

[`transform/load.py`](transform/load.py). Corre **aparte del scraper**: el
spider deja archivos, esto los lee.

1. Junta las particiones de un **lote** (un año calendario) en un temporal y las
   sube con **un solo job de carga** a una tabla de paso `_paso_<hex>` que expira
   sola en 6 horas.
2. `MERGE` de la tabla de paso contra `crudo.precios` sobre `llave_fila`, con el
   rango de fechas en el `ON` — eso es lo que poda el `MERGE` a las particiones
   de ese año en vez de leer el histórico completo.
3. Borra la tabla de paso.

Tres restricciones que explican ese diseño:

- **Un job de carga por lote, no por archivo.** BigQuery permite 1 500 jobs de
  carga por tabla y por día; por archivo el histórico serían ~30 000 y tronaría
  el primer día.
- **El lote es el año**, no el trimestre: un bloque del backfill nunca cruza el
  año, así que agrupar por año no parte ninguna unidad de trabajo. 29 jobs y 29
  `MERGE` para el histórico completo, contra 116.
- **`MERGE` y no reemplazo de partición.** El barrido escribe un mercado a la
  vez; truncar el día para cargar el mercado 210 tiraría los 48 ya cargados.

**Costo.** Los jobs de carga son cuota, no cobro. El `MERGE` del backfill
completo escanea ~16 GB (free tier: 1 TiB/mes) y el almacenamiento son ~$0.33/mes.
No hay nada que optimizar ahí — el presupuesto de USD 20/mes se gasta en la
fase 4, en las tablas que lea la app.

---

# Estrategia para armar la base

Dos frentes que escriben en la misma tabla y nunca se estorban, porque la carga
es idempotente sobre `llave_fila`:

| | Barrido diario | Backfill histórico |
|---|---|---|
| Qué cubre | Los últimos 5 días | 1998 → hoy, ~15.3 M filas |
| Dónde corre | GitHub Actions, 23:00 UTC L-V | **A mano, en la laptop** |
| Unidad | Una ventana de 5 días × 43 mercados × 2 modos | (mercado, modo, trimestre) — 9 628 bloques |
| Estado | Ninguno: pide siempre los mismos 5 días | `out/cobertura.sqlite` |
| Carga | En el mismo workflow, paso siguiente | Después del barrido, por año |

El histórico **no se pide todo de una vez**: son 25.3 GB de NDJSON y varias
horas de peticiones. Va en sesiones acotadas con `--limit`, y arrancar y parar
es el modo normal de operación.

## Empezar

Requiere Python ≥3.11, [`uv`](https://docs.astral.sh/uv/) y `gcloud` autenticado
para cualquier cosa que toque BigQuery.

```bash
uv sync                                    # dependencias
uv run pytest                              # la suite; parte pega al SNIIM en vivo
uv run ruff check .                        # tiene que pasar limpio
gcloud auth application-default login      # credenciales para transform/
```

## Barrido diario

```bash
uv run python -m scraper.spiders.daily                 # 43 mercados, 5 días atrás
uv run python -m scraper.spiders.daily --days 1 --market 210   # una prueba rápida
uv run python -m scraper.spiders.daily --progress      # barra de avance
uv run python -m transform.load --dir out/raw          # y cargar lo que dejó
```

Son 86 peticiones fijas (43 × 2 modos), unos 3 minutos. Deja los archivos en
`out/raw/fecha=YYYY-MM-DD/`.

Son 43 mercados y no los 49 del catálogo: seis llevan años sin publicar. La
contrapartida es que un mercado que reviva queda invisible, así que conviene
volver a correr `scraper.coverage` cada tanto.

La ventana llega **cinco días** hacia atrás porque el fin de semana se come dos:
desde un lunes alcanza el jueves anterior. Ampliarla no cuesta peticiones —son
86 sea cual sea— solo bytes: 5 MB con un día, 25 MB con siete.

La ventana se calcula con la fecha **del centro de México**
(`query.source_date()`), no la de la máquina: en un runner en UTC, después de
las 18:00 hora local ya es el día siguiente y se pediría un día que la fuente no
ha publicado. Cero filas y el job en verde.

Un mercado que falle no le cuesta el día a los otros: se anota en
`out/failures.txt`, el barrido sigue y el proceso **termina en rojo**.

## Backfill histórico

```bash
uv run python -m scraper.backfill_state                     # ¿cuánto falta? ¿qué falló?
uv run python -m scraper.spiders.backfill --limit 500       # una sesión acotada
uv run python -m scraper.spiders.backfill                   # todo lo que falte (horas)
uv run python -m scraper.spiders.backfill --solo-cargar     # cargar lo que quedó en disco
uv run python -m scraper.spiders.backfill --market 210 \
    --desde 2011-07-01 --hasta 2011-09-30                   # un pedazo concreto
```

### La rejilla

La unidad de trabajo es **(mercado, modo de precio, trimestre)**: 9 628 bloques.
Salen de `data/catalogs/market_coverage_by_year.csv` —el sondeo de ALD-14—,
**nunca del calendario**: solo años con datos. Eso deja fuera los dos mercados
que nunca publicaron, los quince que arrancan tarde y los dos huecos internos.
El trimestre en curso se recorta a hoy.

El orden es cronológico global: todos los mercados de 1998-Q1, luego 1998-Q2.
No es cosmético — es lo que mantiene el `MERGE` podado a un año. Barrer por
mercado haría que cada `MERGE` leyera el histórico entero.

### Los dos pasos de una sesión

`backfill.main()` hace, en este orden:

1. **Cargar y purgar** lo que ya estuviera en `out/raw` (de una sesión que murió
   entre el barrido y la carga), y marcar esos bloques.
2. **Barrer** los bloques de esta sesión, escribiendo NDJSON.
3. **Cargar y purgar** lo de esta sesión, año por año, y marcarlo.

La carga corre con el reactor de Scrapy muerto y las particiones cerradas: lo
que hay en disco es el archivo completo, sin esperar buffers ni pipelines. Los
tres bugs que costó la versión acoplada (filas perdidas, buffers sin cerrar, un
trimestre cargado dos veces) salían todos de compartir proceso con el crawler.

- `--no-load` salta 1 y 3 — deja NDJSON en disco sin tocar BigQuery. Para probar.
- `--solo-cargar` hace 1 y termina.
- **Correr `transform.load --purge` a mano no marca `cargado_en`**, y esos
  bloques se volverían a pedir. Fallo seguro (cuesta peticiones, no datos), pero
  para cargar lo del backfill está `--solo-cargar`.

### Elegir el `--limit`

**Nada se carga hasta que el barrido termina**, así que `--limit` es el control
de disco de la sesión, no una comodidad. Medido en ALD-57:

| | Medido |
|---|---|
| NDJSON por fila | 825 B |
| Por bloque | ~2.6 MB |
| Por hora de barrido | ~6.3 GB (≈ 2 400 bloques) |
| Un año en disco | ~870 MB |
| Histórico completo en NDJSON | **25.3 GB** |
| `crudo.precios` al terminar | 16.4 GB |

El comando lo anuncia antes de la primera petición:

```
9 628 bloques pendientes · esta sesión hará 500 · ~1.3 GB de NDJSON antes de cargar
```

Regla práctica: `--limit 500` es ~13 minutos y ~1.3 GB. Escoge el número por el
disco libre que tengas, no por el tiempo.

### Arrancar, parar, reanudar

`Ctrl-C` es seguro y es el modo normal de terminar una sesión. Lo que se pierde
son a lo sumo los bloques en vuelo (máximo 8, `MAX_OPEN_BLOCKS`), que quedan
`abierto` y vuelven a la cola completos. Nada de lo ya cerrado se vuelve a pedir.

Lo que sí se pierde con `Ctrl-C` es el **paso de carga**: el NDJSON de la sesión
se queda en disco sin marcar. La siguiente sesión lo carga sola en su paso 1, o
puedes hacerlo tú con `--solo-cargar`.

Un bloque sale de la cola solo si está `hecho` **y** tiene `cargado_en`. Un
bloque `abierto`, uno `fallido` y uno barrido pero no cargado vuelven igual. Y
"cubierto" significa cubierto *hasta la misma fecha*: el trimestre en curso crece
un día cada día, así que el bloque de hoy regresa solo, sin caso especial.

**Una sola ventana ilegible tiñe el trimestre entero de `fallido`** y se
reintenta completo. Registrar como cubierto un trimestre del que faltó un pedazo
es el único fallo que no deja rastro.

### Leer la consola

Con la barra encendida (por defecto; `--plain` la apaga) el log completo se va a
`out/backfill.log` y la terminal muestra:

```
⠹ Backfill ━━━━━━━━━━━━━━━━━━━━━  1 240/9 628  12%  0:31:12 ← 3h48m
  2003-Q2 · destino 210 · kilogramo_calculado · 4 812 filas/min
✗ 2003-Q1 destino=100 precio=1 ventana=2003-01-01..2003-03-31: NoPaginator: ...
```

- El contador va contra **la rejilla completa**, no contra la sesión: con
  `--limit 500` la barra nunca llega a 100%. Es a propósito — lo que interesa
  entre sesiones es cuánto falta del histórico.
- **La ETA miente al principio de cada sesión.** Se calcula como
  `restantes × transcurrido / hechos`, y `hechos` incluye todo lo que cubrieron
  las sesiones anteriores, que tomó cero tiempo en esta. Empieza absurdamente
  optimista y se corrige conforme la sesión avanza. Sirve para comparar contra
  sí misma, no como promesa.
- `filas/min` es el pulso real: si cae a cero y no hay `✗`, la fuente está
  tardando (autothrottle está esperando), no está trabada.
- Cada `✗` es una ventana perdida, impresa en cuanto ocurre. El detalle con
  traceback está en `out/backfill.log`.

Sin terminal (redirigido a archivo) degrada solo a una línea de estado cada 30
segundos.

### Qué **no** hacer

- **No subir `CONCURRENT_REQUESTS` ni bajar `DOWNLOAD_DELAY`.** Con 4 en paralelo
  la fuente da 503. Un bloqueo cuesta el proyecto entero, no la tarde.
- **No prender la caché HTTP** (`HTTPCACHE_ENABLED`). Reanudar es trabajo del
  SQLite; cachear serían 20+ GB extra en la misma laptop.
- **No correr dos backfills a la vez.** Comparten `out/cobertura.sqlite` y
  `out/raw`: se pisarían las particiones y el checkpoint.
- **No borrar `out/raw` a mano** si hay bloques `hecho` sin `cargado_en`: es la
  única copia de esas filas. Lo peor que pasa es que se vuelven a pedir, pero
  esa sesión se tiró.
- **No `transform.load --purge` para el backfill.** No marca los bloques.

---

# Comprobación

## Antes de empezar (una vez)

```bash
uv run pytest                       # parser contra HTML real, contrato, aritmética
uv run ruff check .
uv run python -m scraper.catalogs   # cotejar los catálogos contra el formulario vivo
```

La suite incluye pruebas en vivo: que `robots.txt` siga permitiendo la ruta, que
el `GET` sin estado siga funcionando y que los catálogos no hayan cambiado. Si
esas fallan, cambió la fuente, no el código.

## Después de un barrido diario

```bash
ls out/raw/                                  # ¿hay particiones? ¿de qué fechas?
cat out/failures.txt                         # existe solo si algo falló
uv run python -m transform.load --dry-run    # qué se cargaría, sin tocar nada
uv run python -m transform.monitor --days 7 --dry-run   # el reporte, impreso
```

`transform.monitor` es la comprobación real, y consulta **BigQuery, no el log del
spider** — una ingesta que dejó de capturar en silencio se ve igual que una
fuente que dejó de publicar, y las dos se ven como un job en verde:

| Señal | Cuándo suena |
|---|---|
| Resumen: filas, mercados y fechas cubiertas | siempre |
| Mercados activos sin una sola fila en la ventana | cuando falta alguno |
| Ventana entera vacía | cuando no llegó nada |
| Sin fecha nueva en 3 días hábiles o más | fuente detenida |
| Ventanas que reventaron en el barrido | cuando las hay |
| *Check-in* del monitor `ingesta-diaria-sniim` | **el job que nunca corrió** |

Requiere `SENTRY_DSN` para enviar; sin él el paso queda en `continue-on-error` y
no tumba la ingesta.

## Avance del backfill

El resumen, sin tocar el spider:

```bash
uv run python -m scraper.backfill_state
```

```
rejilla: 9628 bloques
cubiertos: 12 (0%)
pendientes: 9616
estados: hechos 12, fallidos 0, abiertos 0
siguiente: 1998-Q1 · destino 10 · kilogramo_calculado
```

Para lo que ese resumen no contesta, el checkpoint es SQLite y se consulta
directo (Python 3.12 trae CLI: `uv run python -m sqlite3 out/cobertura.sqlite`):

```sql
-- ¿En qué estado están los bloques, y cuántas filas capturaron?
SELECT estado, COUNT(*) bloques, SUM(filas) filas, SUM(peticiones) peticiones
FROM cobertura GROUP BY estado;

-- ¿Qué años ya están completos en BigQuery y cuáles siguen solo en disco?
SELECT substr(bloque_inicio,1,4) anio, COUNT(*) bloques,
       SUM(cargado_en IS NULL) sin_cargar
FROM cobertura WHERE estado='hecho' GROUP BY 1 ORDER BY 1;

-- ¿Qué falló y por qué? (el error completo, no el resumen)
SELECT bloque_inicio, destino_id, tipo_precio, intentos, error
FROM cobertura WHERE estado='fallido' ORDER BY bloque_inicio;

-- ¿Quedó algo abierto de una sesión que murió?
SELECT bloque_inicio, destino_id, tipo_precio, abierto_en
FROM cobertura WHERE estado='abierto';

-- Los últimos bloques cerrados, para ver el ritmo de la sesión
SELECT bloque_inicio, destino_id, tipo_precio, filas, peticiones, cerrado_en
FROM cobertura ORDER BY cerrado_en DESC LIMIT 20;

-- Bloques que necesitaron subdividirse (peticiones > 1): ventanas pesadas
SELECT bloque_inicio, destino_id, peticiones, filas
FROM cobertura WHERE peticiones > 1 ORDER BY peticiones DESC LIMIT 20;
```

Un bloque con `filas = 0` y estado `hecho` no es un error: el sondeo mide años y
un mercado con datos en 1998 no necesariamente reportó los cuatro trimestres.

## Contra BigQuery

```sql
-- ¿Qué hay cargado, en total?
SELECT COUNT(*) filas, MIN(fecha) desde, MAX(fecha) hasta,
       COUNT(DISTINCT destino_id) mercados
FROM `precios-agricolas-mx.crudo.precios`;

-- La llave natural tiene que ser única. Esto debe devolver cero filas SIEMPRE.
SELECT llave_fila, COUNT(*) n
FROM `precios-agricolas-mx.crudo.precios`
GROUP BY llave_fila HAVING n > 1;

-- Avance por año, para cotejar contra el SQLite del backfill
SELECT EXTRACT(YEAR FROM fecha) anio, COUNT(*) filas,
       COUNT(DISTINCT destino_id) mercados
FROM `precios-agricolas-mx.crudo.precios` GROUP BY 1 ORDER BY 1;

-- Salud de los datos: qué proporción entra marcada
SELECT bandera, COUNT(*) filas
FROM `precios-agricolas-mx.crudo.precios`, UNNEST(banderas_calidad) bandera
GROUP BY 1 ORDER BY 2 DESC;

-- Los dos modos de precio tienen que existir para el mismo día y mercado
SELECT tipo_precio, COUNT(*) filas
FROM `precios-agricolas-mx.crudo.precios`
WHERE fecha = CURRENT_DATE('America/Mexico_City') - 1
GROUP BY 1;
```

La comprobación que cierra el círculo es **cotejar el SQLite contra BigQuery**:
`SUM(filas)` de los bloques `hecho` con `cargado_en` de un año debe coincidir con
el `COUNT(*)` de ese año en la tabla. Si no coincide, sobran filas en BigQuery
(se cargó algo dos veces, imposible con el `MERGE`) o faltan (una carga se comió
filas), y en ese caso los bloques de ese año se vuelven a pedir.

---

# Dónde meterse cuando algo falla

| Síntoma | Dónde | Qué suele ser |
|---|---|---|
| `UnknownColumn` | `scraper/parsers/results.py`, dict `COLUMNS` | La fuente agregó una columna. **Detiene la ingesta a propósito**: hay que decidir qué significa y agregarla al vocabulario, a `RAW_SCHEMA` y al DDL |
| `QueryRejected` | `scraper/query.py` | Se mandaron `ProductoId`, `OrigenId` y `DestinoId` en `-1` a la vez. Nunca debería pasar desde los spiders |
| `NoPaginator` | `scraper/query.py` | La respuesta no trae `Página X de N`, así que no se puede descartar que esté truncada. Puede ser un error del servidor devuelto con 200 |
| `WindowExhausted` | `scraper/query.py` | Un solo día ya no cabe en una respuesta. Bajar `RECORDS_PER_PAGE` no ayuda: hay que revisar si el mercado se hizo enorme |
| `LostRequest: HTTP 503` | La fuente | Está bajo carga. Esperar y relanzar; el bloque vuelve solo a la cola |
| `IncompleteRow` | `scraper/contract.py` | Una fila sin fecha, presentación u origen. Casi siempre la fuente cambió de forma |
| `EMFILE` / demasiados archivos abiertos | `scraper/pipelines/ndjson.py`, `MAX_OPEN_PARTITIONS` | Un trimestre son ~8 460 particiones posibles; el tope LRU de 512 es lo que lo contiene |
| La carga falla en un año | `transform/load.py` | Ese año deja sus particiones en disco y sus bloques en la cola. Los demás años sí entraron. Relanzar `--solo-cargar` |
| `no se purga: N filas en disco contra M en la tabla de paso` | `transform/load.py`, `purge()` | La carga se comió filas. **Nada se borró**: es exactamente lo que esa comprobación existe para evitar |
| Bloques que reaparecen sesión tras sesión | `out/cobertura.sqlite` | Están `hecho` sin `cargado_en`. Correr `--solo-cargar` |
| Job diario en verde sin filas nuevas | `transform/monitor.py` | Para eso está el monitor: la fecha del runner (UTC) contra la del centro de México, o la fuente que dejó de publicar |
| La barra y el log se pelean la terminal | `scraper/console.py` | Se encendió `PROGRESS_CONSOLE_ENABLED` sin fijar `LOG_FILE` en la misma corrida |

---

# Automatización

[`.github/workflows/daily.yml`](.github/workflows/daily.yml) corre el barrido, la
carga y el monitoreo a las 23:00 UTC de lunes a viernes, y también a mano desde
la pestaña Actions. Se autentica con **Workload Identity Federation**: no hay
ninguna llave guardada como secret, solo el `SENTRY_DSN`.

El paso del barrido lleva `continue-on-error` para que lo que sí capturaron los
demás mercados llegue a BigQuery, y el último paso vuelve a fallar el job si
hubo ventanas rotas. El NDJSON de cada corrida queda como artefacto 7 días.

El backfill **no corre ahí**: se ejecuta a mano en la laptop.

> `workflow_dispatch` solo aparece si el workflow existe en `main`, así que un
> workflow nuevo no se puede probar sin llevarlo antes a esa rama.

# Catálogos y fixtures

Los identificadores internos que espera el SNIIM (222 productos, 35 orígenes,
49 mercados) viven versionados en `data/catalogs/`, nunca se infieren en tiempo
de consulta:

```bash
uv run python -m scraper.catalogs      # regenerar catálogos desde el formulario
uv run python -m scraper.coverage      # medir profundidad histórica (~30 min)
uv run python -m scraper.fixtures      # recapturar HTML de prueba
```

El parser se prueba contra HTML real guardado en `tests/fixtures/sniim/`, con un
manifiesto que registra la URL exacta que produjo cada respuesta. Nunca contra
marcado inventado: el marcado inventado le da la razón al parser.

**El histórico arranca en 1998, no en 2007** — el PRD lo daba por hecho y el
sondeo mercado por mercado lo desmintió. No es parejo: 32 de los 49 mercados
llegan a 1998, dos nunca tuvieron datos, cinco dejaron de reportar y dos tienen
huecos internos. Detalle en
[`docs/profundidad-historica.md`](docs/profundidad-historica.md).

# Estructura

```
scraper/     Scrapy. Solo extracción y normalización sintáctica
├── query.py          arma la URL y subdivide la ventana. Puro, sin red
├── parsers/          lectura del HTML por encabezado, probada con fixtures
├── contract.py       registro crudo, llave natural, rechazo
├── harvest.py        respuesta → filas: la mitad que comparten los dos spiders
├── pipelines/        salida a NDJSON particionado por fecha
├── spiders/          daily.py (Actions) y backfill.py (a mano)
├── backfill_state.py rejilla, checkpoint en SQLite y contabilidad de bloques
├── console.py        barra de avance para corridas largas
├── catalogs.py       regenera data/catalogs/ desde el formulario
├── coverage.py       profundidad histórica por mercado (sondeo barato)
└── settings.py       cómo se le pide a la fuente
transform/   BigQuery
├── raw/prices_table.sql   DDL de crudo.precios
├── load.py                upsert de NDJSON a la capa cruda, un job por año
└── monitor.py             reporte de cobertura y alertas a Sentry
app/         Streamlit (pendiente)
data/        catálogos y mapeos canónicos versionados
docs/        notas de investigación de la fuente, con evidencia
tests/       fixtures de HTML real y pruebas
out/         no versionado: NDJSON, logs y cobertura.sqlite
```

# Fuente y atribución

Sistema Nacional de Información e Integración de Mercados (**SNIIM**),
Secretaría de Economía. Sección Mercados Nacionales, Frutas y Hortalizas.
Es la única fuente del proyecto.

La herramienta informa precios de mayoreo publicados por la Secretaría de
Economía. No garantiza que ningún comprador pague ese precio, no interviene en
la transacción y no constituye asesoría comercial.

# Licencia

[MIT](LICENSE).
