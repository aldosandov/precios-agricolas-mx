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
precios de mayoreo todos los días hábiles a través del **SNIIM**. El problema es
que en la práctica no se puede usar. Vive detrás de un formulario ASP.NET que no
tiene API, ni descarga masiva, ni documentación, y la única forma de sacar algo
es consultarlo a mano, una pantalla a la vez. Nadie que esté trabajando una
parcela va a construir así una serie histórica.

El precio de la central tampoco es, tal cual, el precio del productor. Entre una
y otro hay flete, merma y comisión de bodeguero. Tomar el precio de mayoreo como
referencia directa genera una expectativa que nadie va a cumplir.

## Qué hace

```
precio_parcela = precio_central × (1 − merma) × (1 − comisión) − flete_por_kg
```

Quien consulta puede editar los tres parámetros. Los valores por defecto están a
la vista y el desglose del cálculo también, paso por paso. La herramienta **no
afirma que ese sea un precio justo** ni que alguien vaya a pagarlo: lo único que
muestra es aritmética sobre un precio publicado por la Secretaría de Economía y
sobre supuestos que el propio usuario controla.

También responde a la pregunta "¿este precio es alto o bajo para esta época del
año?", comparando el precio de hoy contra la distribución histórica de la misma
semana del año en años anteriores. Cuando no hay suficiente historia para esa
combinación de producto y mercado, la señal simplemente no se publica.

> **Sobre el campo Origen.** El propio SNIIM lo aclara: en frutas y hortalizas
> el origen es de **comercialización**, no de producción. Una fila con origen
> Puebla significa que el producto se comercializó desde Puebla, no que se haya
> cultivado ahí. Ninguna capa de este proyecto afirma lo contrario.

## Estado

| Fase | Contenido | Estado |
|---|---|---|
| 1 | Ingesta completa y capa cruda en BigQuery | **Lista** |
| 2 | Backfill histórico 1998–2026 | En curso |
| 3 | Capa canónica con vigencias y capa intermedia | Pendiente |
| 4 | Capa final: precio en parcela y estacionalidad | Pendiente |
| 5 | App de Streamlit publicada | Pendiente |

Hoy funciona de punta a punta el camino **SNIIM → NDJSON → BigQuery**. Eso
incluye el barrido diario de los 43 mercados activos, el backfill histórico
reanudable, un parser probado contra HTML real, la validación del contrato de
datos, una carga idempotente y el job programado en GitHub Actions. Por ahora
existe una sola tabla, la cruda: las capas intermedia y final, y la app que las
va a leer, son las fases siguientes.

---

# Cómo funciona el scraping

## Una petición al SNIIM

Todo el scraping se reduce a la misma petición repetida miles de veces: un `GET`
a la página de resultados, sin cookies ni estado de vista, con los siete
criterios de la consulta escritos en la URL.

```
ResultadosConsultaFechaFrutasYHortalizas.aspx
  ?fechaInicio=01/07/2011&fechaFinal=30/09/2011   ← la ventana
  &ProductoId=-1                                  ← todos los productos
  &OrigenId=-1                                    ← todos los orígenes
  &DestinoId=210                                  ← ESTE mercado (el criterio fijo)
  &PreciosPorId=2                                 ← kilogramo calculado
  &RegistrosPorPagina=5000
```

Esa URL la arma [`scraper/query.py`](scraper/query.py), un módulo puro que no
toca la red. Hay cuatro cosas que conviene entender de ella, todas verificadas
contra la fuente y documentadas en
[`docs/consulta-sniim.md`](docs/consulta-sniim.md):

- **El criterio que se fija es el mercado, no el producto.** Fijar el mercado
  cuesta 49 peticiones por ventana; fijar el producto costaría 222. Además, al
  fijar el mercado la tabla conserva las columnas `Producto`, `Calidad`,
  `Presentación` y `Origen`, que es justamente lo que se quiere guardar.
- **Cada ventana se pide dos veces**, una por cada valor de `PreciosPorId`: el 1
  devuelve el precio por presentación comercial y el 2 el precio por kilogramo.
  Las dos respuestas traen la columna `Presentación` idéntica, así que **la
  respuesta no dice bajo cuál de los dos modos se pidió**. La única forma de
  saberlo es registrar lo que se mandó, y por eso el modo de precio viaja dentro
  de la llave natural de cada fila.
- **Todo criterio que se fija a un solo valor desaparece de la tabla.** Si
  `fechaInicio` y `fechaFinal` son el mismo día, no hay columna `Fecha`; si el
  mercado va fijo, no hay columna `Destino`. La misma consulta puede devolver
  siete formas distintas de tabla, y de ahí viene la regla de que el parser lea
  siempre el encabezado en vez de contar posiciones.
- **Nunca se pagina.** El botón "siguiente" de la fuente es un postback que
  arrastra ~213 KB de estado de vista. En vez de usarlo, cuando el servidor avisa
  que truncó la respuesta (`Página 1 de N`, con N > 1) se parte la ventana de
  fechas a la mitad y se piden las dos mitades por separado, tantas veces como
  haga falta. Eso es `query.next_windows()`, y es el único control de volumen que
  existe en el proyecto.

## De la respuesta a la fila

El camino de la respuesta a la fila es el mismo para el barrido diario y para el
histórico. Esa mitad compartida vive en
[`scraper/harvest.py`](scraper/harvest.py) y son cuatro pasos:

| Paso | Archivo | Qué hace y dónde falla |
|---|---|---|
| 1. Leer la tabla | `scraper/parsers/results.py` | Mapea las columnas **por encabezado**, nunca por posición. Un encabezado desconocido levanta `UnknownColumn` y **detiene la ingesta a propósito**: qué significa una columna nueva es una decisión humana |
| 2. Armar el registro | `scraper/contract.py` | Junta lo que dijo la tabla con lo que mandó la consulta. Exige que la fila sea identificable —fecha, producto, presentación, origen, destino y modo de precio—, sin revisar los precios. Una fila que no se puede identificar levanta `IncompleteRow` |
| 3. Convertir a fila cruda | `scraper/pipelines/ndjson.py` | Tipa los precios, calcula `llave_fila` (el sha256 de la llave natural) y agrega las banderas de calidad. **Marca, nunca corrige** |
| 4. Escribir a disco | `scraper/pipelines/ndjson.py` (`PartitionWriter`) | Un archivo por fecha, mercado y modo de precio: `out/raw/fecha=2011-07-04/destino=210_precio=kilogramo_calculado.ndjson`. Cada archivo se trunca la primera vez que la corrida lo toca y se anexa después, de modo que repetir una ventana reescribe ese día en lugar de duplicarlo |

Cuando algo de ese camino falla, el daño queda contenido en una sola ventana.
Cualquier excepción del grupo `UNREADABLE` —del parser, del contrato, un rechazo
del servidor o una respuesta sin paginador— hace que se pierda esa ventana y
nada más: el error se anota, el barrido continúa con lo demás y el proceso
termina en rojo para que la falla no pase inadvertida.

El `errback` cubre el otro caso, el de la petición que nunca volvió. Scrapy agota
sus `RETRY_TIMES` y se rinde en silencio, sin levantar ninguna excepción que el
callback pueda ver. Sin un `errback` explícito esa ventana desaparecería sin
dejar rastro y la corrida terminaría en verde con un mercado de menos.

## Cómo se le pide a la fuente

Los ajustes están en [`scraper/settings.py`](scraper/settings.py). El SNIIM es un
servidor de gobierno que empieza a responder **503** en cuanto se le exige, así
que el crawl es lento a propósito:

| Ajuste | Valor | Por qué |
|---|---|---|
| `CONCURRENT_REQUESTS` | 2 | Con 4 peticiones en paralelo la fuente empieza a devolver 503 |
| `DOWNLOAD_DELAY` | 3 s | Espera mínima entre una petición y la siguiente |
| `AUTOTHROTTLE_MAX_DELAY` | 120 s | Cuando la fuente se satura tarda minutos en despejar, no segundos |
| `RETRY_TIMES` | 4 | Reintentos ante 500, 502, 503, 504, 408 y 429 |
| `DOWNLOAD_TIMEOUT` | 180 s | Un trimestre de un mercado son megabytes que el servidor arma en vivo |
| `ROBOTSTXT_OBEY` | `True` | Se respeta el archivo, y el user agent dice quién pregunta y dónde reclamar |
| `HTTPCACHE_ENABLED` | `False` | Cachear 9 600 respuestas de varios megabytes costaría más de 20 GB. Reanudar es trabajo de `out/cobertura.sqlite`, no de la caché |

Subir la concurrencia es la forma más rápida de conseguir que bloqueen el
proyecto entero, y el proyecto depende de esta única fuente. Ese ajuste no se
toca.

---

# La tabla en BigQuery

El proyecto de GCP es `precios-agricolas-mx` y está en la ubicación **US**, que
no se puede cambiar sin recrearlo todo. Hoy existe una sola tabla,
**`crudo.precios`**, que es la capa cruda. Su DDL está en
[`transform/raw/prices_table.sql`](transform/raw/prices_table.sql).

```sql
CREATE TABLE `precios-agricolas-mx.crudo.precios` (...)
PARTITION BY fecha
CLUSTER BY producto_raw, destino_id, tipo_precio
```

| Columna | Tipo | Qué guarda |
|---|---|---|
| `fecha` | DATE | Día al que corresponde el precio. Es la columna de partición |
| `producto_raw`, `calidad_raw` | STRING | Los literales tal como los escribe la tabla del SNIIM |
| `presentacion_raw` | STRING | `"Caja de 10 kg."` y similares. No cambia entre un modo de precio y el otro |
| `origen_raw` | STRING | Estado de **comercialización**, no de producción |
| `destino_raw`, `destino_id` | STRING, INT64 | El mercado, en sus dos planos: la etiqueta y el id que se envió |
| `grupo_raw` | STRING | Categoría heredada de la fila separadora: `Frutas`, `Hortalizas`, `Chiles Secos` |
| `tipo_precio` | STRING | `presentacion_comercial` o `kilogramo_calculado` |
| `precio_min`, `precio_max`, `precio_frecuente` | NUMERIC | El frecuente es la **moda** de la muestra, no el promedio |
| `observaciones` | STRING | La columna `Obs.` de la fuente; trae el país cuando el origen es Importación |
| `banderas_calidad` | ARRAY\<STRING\> | `precio_ausente`, `precio_cero`, `precio_no_numerico`, `precios_incoherentes` |
| `extraido_en`, `url_consulta` | TIMESTAMP, STRING | Cuándo se descargó la fila y con qué URL exacta, para que la extracción sea reproducible |
| `llave_fila` | STRING | sha256 de la llave natural. Es la columna sobre la que la carga hace upsert |

**La llave natural** de una observación es
`(fecha, producto, presentación, origen, destino, PreciosPorId)`. Cada criterio
entra con la etiqueta que dio la tabla, o con `id:<n>` cuando la consulta lo fijó
y la fuente por eso borró su columna. Incluir el modo de precio es lo que evita
que el precio por kilogramo pise al precio por caja del mismo día, que son la
misma observación medida de dos formas. Cómo se usa esa llave para que nada se
duplique ni se pierda está en
[Idempotencia](#idempotencia-ni-filas-perdidas-ni-filas-dobles).

**La capa cruda es inmutable**: aquí no se edita ni se limpia nada. Un precio en
cero o incoherente entra con su bandera puesta. Corregirlo es trabajo de la capa
intermedia, que se reconstruye a partir de esta sin modificarla.

## Cómo entra la carga

De la carga se encarga [`transform/load.py`](transform/load.py), que corre
**aparte del scraper**: el spider deja archivos en disco y este módulo los lee.
Son tres pasos por lote:

1. Concatena las particiones del **lote** (un año calendario) en un archivo
   temporal y las sube con **un solo job de carga** a una tabla de paso llamada
   `_paso_<hex>`, que expira sola a las 6 horas.
2. Hace un `MERGE` de esa tabla de paso contra `crudo.precios` usando
   `llave_fila`. El rango de fechas va dentro del `ON`, y eso es lo que hace que
   BigQuery lea únicamente las particiones de ese año en lugar del histórico
   completo.
3. Borra la tabla de paso.

Ese diseño responde a tres restricciones concretas:

- **Un job de carga por lote, nunca uno por archivo.** BigQuery permite 1 500
  jobs de carga por tabla y por día. Cargando archivo por archivo el histórico
  necesitaría unos 30 000 jobs y agotaría la cuota el primer día.
- **El lote es el año calendario, no el trimestre.** Un bloque del backfill nunca
  cruza el fin de año, así que agrupar por año no parte ninguna unidad de
  trabajo. El histórico completo son 29 jobs de carga y 29 `MERGE`, contra los
  116 que costaría hacerlo por trimestre.
- **Se usa `MERGE` y no reemplazo de partición.** El barrido escribe un mercado a
  la vez, así que truncar la partición del día para cargar el mercado 210
  tiraría los 48 mercados que ya estaban en ella.

**Sobre el costo:** los jobs de carga cuentan contra la cuota, no contra la
factura. El `MERGE` del backfill completo escanea ~16 GB, muy por debajo del
free tier de 1 TiB al mes, y el almacenamiento sale en ~$0.33 mensuales. No hay
nada que optimizar en esta capa: el presupuesto de USD 20/mes se va a gastar en
la fase 4, en las tablas que consulte la app.

---

# Estrategia para armar la base

La base se arma desde dos frentes que escriben en la misma tabla sin estorbarse,
porque la carga es idempotente sobre `llave_fila`:

| | Barrido diario | Backfill histórico |
|---|---|---|
| Qué cubre | Los últimos 5 días | De 1998 a hoy, ~15.3 M de filas |
| Dónde corre | GitHub Actions, 23:00 UTC de lunes a viernes | **A mano, en la laptop** |
| Unidad de trabajo | Una ventana de 5 días × 43 mercados × 2 modos | (mercado, modo, trimestre): 9 628 bloques |
| Estado que guarda | Ninguno, porque siempre pide los mismos 5 días | `out/cobertura.sqlite` |
| Cuándo carga | En el mismo workflow, en el paso siguiente | Al terminar el barrido, año por año |

El histórico **no se pide todo de una sentada**: son 25.3 GB de NDJSON y varias
horas de peticiones. Se avanza en sesiones acotadas con `--limit`, y arrancar y
parar es el modo normal de operarlo, no una falla.

## Empezar

Hace falta Python ≥3.11, [`uv`](https://docs.astral.sh/uv/) y, para cualquier
cosa que toque BigQuery, `gcloud` autenticado.

```bash
uv sync                                    # instalar dependencias
uv run pytest                              # la suite; parte de ella pega al SNIIM en vivo
uv run ruff check .                        # tiene que pasar limpio
gcloud auth application-default login      # credenciales para todo lo de transform/
```

## Barrido diario

```bash
uv run python -m scraper.spiders.daily                 # 43 mercados, 5 días atrás
uv run python -m scraper.spiders.daily --days 1 --market 210   # una prueba rápida
uv run python -m scraper.spiders.daily --progress      # barra de avance
uv run python -m transform.load --dir out/raw          # y cargar lo que dejó
```

Son 86 peticiones fijas (43 mercados × 2 modos de precio) y toma unos 3 minutos.
Los archivos quedan en `out/raw/fecha=YYYY-MM-DD/`.

Se piden 43 mercados y no los 49 del catálogo porque seis llevan años sin
publicar nada. La contrapartida es que un mercado que reviva quedaría invisible,
así que conviene volver a correr `scraper.coverage` cada tanto para detectarlo.

La ventana llega **cinco días** hacia atrás porque el fin de semana se come dos
de ellos: partiendo de un lunes hay que alcanzar el jueves anterior. Ampliarla no
cuesta peticiones —siempre son 86—, solo bytes: alrededor de 5 MB pidiendo un día
y 25 MB pidiendo siete.

La ventana se calcula con la fecha **del centro de México**
(`query.source_date()`) y no con la de la máquina que corre el job. En un runner
que va en UTC, después de las 18:00 hora local ya es el día siguiente, así que se
pediría un día que la fuente todavía no publica: cero filas y el job en verde.

Un mercado que falle no le cuesta el día a los demás. Su error se anota en
`out/failures.txt`, el barrido continúa con el resto y el proceso **termina en
rojo** para que la falla se vea.

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

La unidad de trabajo es **(mercado, modo de precio, trimestre)**, y son 9 628
bloques. Salen de `data/catalogs/market_coverage_by_year.csv`, el sondeo de
ALD-14, y **nunca del calendario**: solo se piden los años en los que ese mercado
efectivamente reportó algo. Eso es lo que deja fuera a los dos mercados que nunca
publicaron, a los quince que arrancan tarde y a los dos huecos internos. El
trimestre en curso se recorta hasta hoy, porque pedir fechas futuras cuesta
peticiones y no devuelve nada.

El orden en que se recorre la rejilla es cronológico global: primero todos los
mercados de 1998-Q1, luego todos los de 1998-Q2, y así. No es cosmético. Es lo
que mantiene cada `MERGE` podado a un solo año; barrer mercado por mercado haría
que cada `MERGE` tuviera que leer el histórico entero.

### Los tres pasos de una sesión

`backfill.main()` ejecuta, en este orden:

1. **Cargar y purgar** lo que ya estuviera en `out/raw` —típicamente de una
   sesión anterior que murió entre su barrido y su carga— y marcar esos bloques
   como cargados.
2. **Barrer** los bloques que le tocan a esta sesión, escribiendo NDJSON a disco.
3. **Cargar y purgar** lo que acaba de barrer, año por año, y marcarlo.

Ese primer paso es lo que evita tirar peticiones a la basura: sin él, el barrido
volvería a pedirle al SNIIM datos que ya están en el disco.

La carga corre con el reactor de Scrapy ya muerto y todas las particiones
cerradas, así que lo que hay en disco es el archivo completo, sin buffers a medio
vaciar ni items pendientes en el pipeline. Los tres bugs que costó la versión
anterior, que cargaba dentro del crawl (filas perdidas, buffers sin cerrar y un
trimestre cargado dos veces), salían todos de compartir proceso y ciclo de vida
con el crawler.

Las banderas que cambian ese flujo:

- `--no-load` salta los pasos 1 y 3: deja el NDJSON en disco sin tocar BigQuery.
  Sirve para probar.
- `--solo-cargar` hace el paso 1 y termina, sin barrer nada.
- **Correr `transform.load --purge` a mano sí carga las filas, pero no escribe
  `cargado_en`**, así que esos bloques se volverían a pedir en la próxima sesión.
  Es un fallo seguro —cuesta peticiones, no datos—, pero para cargar lo del
  backfill lo correcto es `--solo-cargar`.

### Elegir el `--limit`

Como **nada se carga hasta que el barrido termina**, todo el NDJSON de la sesión
se acumula en disco al mismo tiempo. Por eso `--limit` es el control de disco de
la sesión y no una simple comodidad. Estas cifras están medidas en ALD-57, no
estimadas:

| | Medido |
|---|---|
| NDJSON por fila | 825 B |
| Por bloque | ~2.6 MB |
| Por hora de barrido | ~6.3 GB (≈ 2 400 bloques) |
| Un año en disco | ~870 MB |
| Histórico completo en NDJSON | **25.3 GB** |
| `crudo.precios` al terminar | 16.4 GB |

El comando lo anuncia antes de mandar la primera petición:

```
9 628 bloques pendientes · esta sesión hará 500 · ~1.3 GB de NDJSON antes de cargar
```

Como regla práctica, `--limit 500` son unos 13 minutos y ~1.3 GB. Conviene elegir
el número por el disco libre que haya, no por el tiempo disponible.

### Arrancar, parar, reanudar

`Ctrl-C` es seguro y es la forma normal de terminar una sesión. Lo más que se
pierde son los bloques que iban en vuelo en ese momento —ocho como máximo, según
`MAX_OPEN_BLOCKS`—, que quedan marcados `abierto` y vuelven completos a la cola.
Nada de lo que ya se había cerrado se vuelve a pedir.

Lo que sí se pierde al interrumpir es el **paso de carga**: el NDJSON de esa
sesión se queda en disco sin marcar. La siguiente sesión lo carga sola en su
paso 1, o se puede hacer de inmediato con `--solo-cargar`.

Un bloque solo sale de la cola si está `hecho` **y además** tiene `cargado_en`.
Un bloque `abierto`, uno `fallido` y uno que se barrió pero no se cargó vuelven
todos por igual. Y "cubierto" significa cubierto *hasta la misma fecha*: el
trimestre en curso crece un día cada día, así que un bloque cerrado ayer deja de
cuadrar con lo que pide la rejilla hoy y regresa solo, sin ningún caso especial
para "hoy".

**Basta con que una ventana resulte ilegible para que el trimestre entero quede
`fallido`**, y entonces se reintenta completo. Es a propósito: registrar como
cubierto un trimestre del que faltó un pedazo sería el único fallo que no deja
rastro. El detalle de por qué reintentar nunca duplica nada está en
[Idempotencia](#idempotencia-ni-filas-perdidas-ni-filas-dobles).

### Leer la consola

La barra de avance está encendida por defecto y `--plain` la apaga. Con ella, el
log completo se va a `out/backfill.log` y la terminal muestra esto:

```
⠹ Backfill ━━━━━━━━━━━━━━━━━━━━━  1 240/9 628  12%  0:31:12 ← 3h48m
  2003-Q2 · destino 210 · kilogramo_calculado · 4 812 filas/min
✗ 2003-Q1 destino=100 precio=1 ventana=2003-01-01..2003-03-31: NoPaginator: ...
```

- El contador avanza contra **la rejilla completa**, no contra la sesión, así que
  con `--limit 500` la barra nunca va a llegar a 100%. Es deliberado: lo que
  interesa saber entre una sesión y otra es cuánto falta del histórico.
- **La ETA miente al principio de cada sesión.** Se calcula como
  `restantes × transcurrido / hechos`, y `hechos` incluye todo lo que cubrieron
  las sesiones anteriores, que en esta tomó cero tiempo. Arranca absurdamente
  optimista y se va corrigiendo conforme la sesión avanza. Sirve para comparar la
  sesión contra sí misma, no como promesa.
- `filas/min` es el pulso real de la corrida. Si cae a cero y no aparecen `✗`, la
  fuente está tardando y el autothrottle está esperando; no es que el proceso se
  haya trabado.
- Cada `✗` es una ventana perdida y se imprime en cuanto ocurre. El detalle con
  su traceback queda en `out/backfill.log`.

Cuando la salida no es una terminal —por ejemplo si se redirige a un archivo— la
consola degrada a una sola línea de estado cada 30 segundos.

### Qué **no** hacer

- **No subir `CONCURRENT_REQUESTS` ni bajar `DOWNLOAD_DELAY`.** Con 4 peticiones
  en paralelo la fuente devuelve 503, y un bloqueo no cuesta la tarde: cuesta el
  proyecto entero.
- **No prender la caché HTTP** (`HTTPCACHE_ENABLED`). Reanudar es trabajo del
  SQLite, y cachear significaría más de 20 GB extra en la misma laptop.
- **No correr dos backfills al mismo tiempo.** Comparten `out/cobertura.sqlite` y
  `out/raw`, y se pisarían las particiones y el checkpoint. Este es el único
  escenario que sí puede corromper el estado.
- **No borrar `out/raw` a mano** mientras haya bloques `hecho` sin `cargado_en`:
  ese directorio es la única copia de esas filas. Lo peor que pasa es que se
  vuelvan a pedir, pero se tira el trabajo de la sesión.
- **No usar `transform.load --purge` para el backfill**, porque no marca los
  bloques como cargados.

---

# Idempotencia: ni filas perdidas ni filas dobles

Todo este pipeline se ejecuta a pedazos. El job diario puede repetirse, el
backfill se interrumpe con `Ctrl-C` a media sesión, una carga puede fallar
justo a la mitad. La pregunta que hay que poder contestar en cualquier momento
es doble: **¿se puede volver a correr lo mismo sin duplicar nada?** y **¿puede
algo darse por guardado sin estarlo?**

La respuesta corta es que **repetir trabajo siempre es seguro, y es siempre la
salida que se elige**. Cada punto donde el pipeline puede fallar está resuelto
hacia el mismo lado: ante la duda, el bloque vuelve a la cola y se vuelve a
pedir. Eso cuesta peticiones al SNIIM y nada más. La alternativa —dar por bueno
un pedazo que quizá no se guardó— es el único error que no deja rastro en
ninguna parte.

Esa garantía se sostiene en cuatro capas, y cada una tapa lo que la anterior no
alcanza a ver.

## 1. La llave natural: dos filas iguales son la misma fila

Cada fila de la capa cruda lleva `llave_fila`, que es el sha256 de su llave
natural:

```
(fecha, producto, presentación, origen, destino, PreciosPorId)
```

El componente `producto` es la etiqueta de la tabla con la calidad concatenada
(`"Tomate Saladet - Primera"`), igual que aparece en
`data/catalogs/products.csv`, de modo que dos calidades del mismo producto son
dos filas distintas y no una que pisa a la otra. Cuando la consulta fijó un
criterio y la fuente por eso borró su columna, en su lugar entra `id:<n>`, que
es el identificador que se mandó.

Esa llave es **determinista**: la misma observación descargada hoy, mañana y
dentro de un año produce exactamente el mismo `llave_fila`. Lo único que cambia
entre una descarga y otra son `extraido_en` y `url_consulta`, que son metadatos
de la extracción y no forman parte de la llave.

Dentro de una misma respuesta, dos filas que reclamen la misma llave natural
levantan `DuplicateNaturalKey` y **detienen esa ventana**. No se descarta la
segunda ni se promedian las dos: si la fuente sirve dos precios distintos bajo
una misma identidad, entonces la llave dejó de identificar, y eso es una
decisión humana, no algo que la ingesta pueda resolver sola.

## 2. El `MERGE`: cargar dos veces es igual que cargar una

[`transform/load.py`](transform/load.py) nunca hace `INSERT` contra
`crudo.precios`. Sube el lote a una tabla de paso y de ahí ejecuta un
`MERGE ... ON T.llave_fila = S.llave_fila`: si la fila ya existe la actualiza, y
si no existe la inserta. Correr la misma carga diez veces deja la tabla
exactamente igual que correrla una sola vez.

De ahí salen dos propiedades que el pipeline usa a propósito:

- **Una carga que murió a la mitad se relanza sin limpiar nada antes.** Las filas
  que alcanzaron a entrar se vuelven a escribir encima con el mismo valor.
- **Un bloque que se barre de nuevo sobrescribe lo que ya había.** Eso es lo que
  hace seguro reintentar un trimestre completo cuando lo único que falló fue una
  de sus ventanas: las filas que sí se habían capturado se pisan a sí mismas.

Tampoco se reemplazan particiones enteras, aunque sería más simple de escribir.
El barrido carga un mercado a la vez, así que truncar la partición del día para
meter el mercado 210 tiraría los 48 mercados que ya estaban ahí.

## 3. El archivo NDJSON: repetir una ventana reescribe el día

Antes de llegar a BigQuery, las filas viven en
`out/raw/fecha=…/destino=…_precio=….ndjson`. `PartitionWriter` trunca cada
partición **la primera vez que la toca en esa corrida** y anexa de ahí en
adelante. Así, si una ventana se vuelve a pedir dentro de la misma sesión, el
archivo se reescribe en lugar de quedarse con el día repetido dos veces.

La partición es por fecha, mercado y modo de precio, y cada ventana se pide
completa, así que las escrituras de dos bloques distintos nunca caen en el mismo
archivo.

## 4. El checkpoint: qué cuenta como guardado

Esta es la capa que impide el otro error, el de dar por hecho algo que nunca
llegó a BigQuery. Desde ALD-57 la carga corre **después** del barrido, así que
entre uno y otra el NDJSON en disco es la única copia de esas filas. El
checkpoint lo refleja distinguiendo dos cosas que antes eran una:

| Estado del bloque | Qué significa | ¿Sale de la cola? |
|---|---|---|
| `abierto` | Una sesión lo empezó y no alcanzó a cerrarlo | No |
| `fallido` | Al menos una de sus ventanas no se pudo leer | No |
| `hecho`, con `cargado_en` vacío | Barrido y escrito en disco, pero todavía no en BigQuery | No |
| `hecho`, con `cargado_en` | Sus filas están en BigQuery y el disco ya se purgó | **Sí** |

La secuencia que lleva de un estado al otro está ordenada justamente para que
ningún paso pueda mentir:

1. La carga sube el año a la tabla de paso y ejecuta el `MERGE`.
2. `purge()` **cuenta las líneas de los archivos en disco y las compara contra
   las filas que quedaron en la tabla de paso**. Si los números no coinciden, no
   borra nada y levanta el error. Una carga que se comió filas y una que las tomó
   todas se ven igual desde afuera, y la diferencia entre ambas es exactamente lo
   que está a punto de borrarse del disco.
3. Solo si la purga terminó sin error se llama a `mark_loaded()`, que es lo único
   que escribe `cargado_en`.

Cualquier interrupción en medio de esos pasos deja el bloque en la cola con su
NDJSON intacto. Lo peor que puede ocurrir es que ese año se cargue dos veces, y
eso ya lo absorbe el `MERGE`.

Los bloques que capturaron **cero filas** son la única excepción: se marcan como
cargados en el momento en que cierran. No escribieron ninguna partición, así que
no hay nada en disco que se pueda perder, y sin esa excepción se volverían a
pedir en cada sesión para siempre.

## Qué pasa si…

| Situación | Qué ocurre | Por qué no se pierde ni se duplica |
|---|---|---|
| `Ctrl-C` a media sesión del backfill | Los bloques en vuelo (8 como máximo) quedan `abierto` | Vuelven completos a la cola, y lo que ya estaba cerrado no se vuelve a pedir |
| La sesión muere entre el barrido y la carga | Quedan bloques `hecho` sin `cargado_en`, con su NDJSON en disco | El paso 1 de la siguiente sesión lo carga antes de barrer nada nuevo |
| La carga de un año falla | Ese año conserva sus particiones en disco y sus bloques en la cola | Los demás años sí entraron; `--solo-cargar` reintenta el que falló |
| El mismo NDJSON se carga dos veces | El `MERGE` actualiza las mismas filas con los mismos valores | La llave es determinista, así que la segunda pasada no inserta nada nuevo |
| Una ventana del trimestre no se pudo leer | El bloque entero queda `fallido` y se reintenta completo | Lo que ya se había capturado se sobrescribe; nunca queda registrado un trimestre a medias |
| El barrido diario vuelve a pedir un día ya cargado | Se carga de nuevo y el `MERGE` lo actualiza | Es lo normal: la ventana de cinco días se traslapa a propósito todos los días |
| Se borra `out/raw` con bloques sin cargar | Esos bloques regresan a la cola y se vuelven a pedir | Se pierde el trabajo de esa sesión, nunca filas ya cargadas en BigQuery |
| Se corre `transform.load --purge` a mano | Las filas entran a la tabla, pero los bloques no quedan marcados | Se vuelven a pedir. Cuesta peticiones, no datos; para esto está `--solo-cargar` |

**El único caso que sí rompe** es correr dos backfills al mismo tiempo. Comparten
`out/cobertura.sqlite` y `out/raw`, y ahí dos escritores sí se pisan las
particiones y el checkpoint. Nada en el código lo impide: simplemente no se hace.

## Se comprueba, no se supone

Dos consultas cierran el círculo, y están junto con las demás en
[Comprobación](#comprobación):

- **Unicidad en el destino.** Agrupar `crudo.precios` por `llave_fila` buscando
  `COUNT(*) > 1` tiene que devolver **cero filas, siempre**. Si devuelve algo, el
  `MERGE` no está haciendo lo que se cree que hace.
- **Cotejo del disco contra la tabla.** El `SUM(filas)` de los bloques `hecho`
  con `cargado_en` de un año debe coincidir con el `COUNT(*)` de ese mismo año en
  BigQuery. Si faltan filas, alguna carga se comió algo, y lo que corresponde es
  devolver esos bloques a la cola.

---

# Comprobación

## Antes de empezar (una vez)

```bash
uv run pytest                       # parser contra HTML real, contrato, aritmética
uv run ruff check .
uv run python -m scraper.catalogs   # cotejar los catálogos contra el formulario vivo
```

La suite incluye pruebas que pegan a la fuente en vivo: que `robots.txt` siga
permitiendo la ruta, que el `GET` sin estado siga funcionando y que los catálogos
no hayan cambiado. Si esas fallan, lo que cambió es la fuente, no el código.

## Después de un barrido diario

```bash
ls out/raw/                                  # ¿hay particiones? ¿de qué fechas?
cat out/failures.txt                         # existe solo si algo falló
uv run python -m transform.load --dry-run    # qué se cargaría, sin tocar nada
uv run python -m transform.monitor --days 7 --dry-run   # el reporte, impreso
```

La comprobación que de verdad cuenta es `transform.monitor`, y consulta
**BigQuery, no el log del spider**. El motivo es que una ingesta que dejó de
capturar en silencio se ve idéntica a una fuente que dejó de publicar, y las dos
se ven como un job en verde:

| Señal | Cuándo suena |
|---|---|
| Resumen de filas, mercados y fechas cubiertas | Siempre |
| Mercados activos sin una sola fila en la ventana | Cuando falta alguno |
| Ventana entera vacía | Cuando no llegó absolutamente nada |
| Sin fecha nueva en 3 días hábiles o más | Cuando la fuente se detuvo |
| Ventanas que reventaron durante el barrido | Cuando las hay |
| *Check-in* del monitor `ingesta-diaria-sniim` | Cuando **el job no corrió** |

Para enviar a Sentry necesita el secret `SENTRY_DSN`. Sin él, ese paso queda en
`continue-on-error` y no tumba la ingesta.

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

Para lo que ese resumen no contesta, el checkpoint es un SQLite y se puede
consultar directo. Python 3.12 trae CLI incluida:
`uv run python -m sqlite3 out/cobertura.sqlite`.

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

Un bloque con `filas = 0` y estado `hecho` no es un error. El sondeo de ALD-14
mide años completos, y un mercado con datos en 1998 no necesariamente reportó los
cuatro trimestres de ese año.

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
el `SUM(filas)` de los bloques `hecho` con `cargado_en` de un año debe coincidir
con el `COUNT(*)` de ese año en la tabla. Si no coinciden, o sobran filas en
BigQuery —lo que significaría que algo se cargó dos veces, cosa que el `MERGE`
debería hacer imposible— o faltan, porque alguna carga se comió filas. En
cualquiera de los dos casos, lo que corresponde es devolver los bloques de ese
año a la cola.

---

# Dónde meterse cuando algo falla

| Síntoma | Dónde buscar | Qué suele ser |
|---|---|---|
| `UnknownColumn` | `scraper/parsers/results.py`, el dict `COLUMNS` | La fuente agregó una columna. **Detiene la ingesta a propósito**: hay que decidir qué significa y darla de alta en el vocabulario, en `RAW_SCHEMA` y en el DDL |
| `QueryRejected` | `scraper/query.py` | Se mandaron `ProductoId`, `OrigenId` y `DestinoId` en `-1` a la vez. No debería ocurrir nunca desde los spiders |
| `NoPaginator` | `scraper/query.py` | La respuesta no trae `Página X de N`, así que no hay forma de descartar que venga truncada. A veces es un error del servidor devuelto con un 200 |
| `WindowExhausted` | `scraper/query.py` | Un solo día ya no cabe en una respuesta. Bajar `RECORDS_PER_PAGE` no ayuda; hay que revisar si ese mercado creció de golpe |
| `LostRequest: HTTP 503` | La fuente | El SNIIM está bajo carga. Esperar y relanzar: el bloque vuelve solo a la cola |
| `IncompleteRow` | `scraper/contract.py` | Llegó una fila sin fecha, sin presentación o sin origen. Casi siempre significa que la fuente cambió de forma |
| `EMFILE` o "demasiados archivos abiertos" | `scraper/pipelines/ndjson.py`, `MAX_OPEN_PARTITIONS` | Un trimestre puede tocar ~8 460 particiones distintas; el tope LRU de 512 es lo que lo contiene |
| La carga falla en un año | `transform/load.py` | Ese año deja sus particiones en disco y sus bloques en la cola, mientras que los demás años sí entraron. Se relanza con `--solo-cargar` |
| `no se purga: N filas en disco contra M en la tabla de paso` | `transform/load.py`, función `purge()` | La carga se comió filas. **No se borró nada**: evitar justo eso es la razón de que exista esa comprobación |
| Bloques que reaparecen sesión tras sesión | `out/cobertura.sqlite` | Están en `hecho` sin `cargado_en`. Se arregla corriendo `--solo-cargar` |
| Job diario en verde pero sin filas nuevas | `transform/monitor.py` | Justo para esto está el monitor. Suele ser la fecha del runner (UTC) contra la del centro de México, o la fuente que dejó de publicar |
| La barra de avance y el log se pelean la terminal | `scraper/console.py` | Se encendió `PROGRESS_CONSOLE_ENABLED` sin fijar `LOG_FILE` en la misma corrida |

---

# Automatización

[`.github/workflows/daily.yml`](.github/workflows/daily.yml) corre el barrido, la
carga y el monitoreo a las 23:00 UTC de lunes a viernes, y también puede
dispararse a mano desde la pestaña Actions. Se autentica contra GCP con
**Workload Identity Federation**, así que no hay ninguna llave guardada como
secret: el único secret del repo es `SENTRY_DSN`.

El paso del barrido lleva `continue-on-error` para que lo que sí capturaron los
demás mercados alcance a llegar a BigQuery, y el último paso del workflow vuelve
a marcar el job como fallido si hubo ventanas rotas. El NDJSON de cada corrida
queda guardado como artefacto durante 7 días.

El backfill **no corre en Actions**: se ejecuta a mano en la laptop.

> `workflow_dispatch` solo aparece si el workflow existe en `main`, así que un
> workflow nuevo no se puede probar sin llevarlo antes a esa rama.

# Catálogos y fixtures

Los identificadores internos que espera el SNIIM —222 productos, 35 orígenes y
49 mercados— viven versionados en `data/catalogs/` y nunca se infieren en tiempo
de consulta:

```bash
uv run python -m scraper.catalogs      # regenerar catálogos desde el formulario
uv run python -m scraper.coverage      # medir profundidad histórica (~30 min)
uv run python -m scraper.fixtures      # recapturar HTML de prueba
```

El parser se prueba contra HTML real guardado en `tests/fixtures/sniim/`, con un
manifiesto que registra la URL exacta que produjo cada respuesta. Nunca contra
marcado inventado, porque el marcado inventado siempre le da la razón al parser.

**El histórico arranca en 1998, no en 2007.** El PRD lo daba por hecho y el
sondeo mercado por mercado lo desmintió. Además no es parejo: 32 de los 49
mercados llegan hasta 1998, dos nunca tuvieron datos, cinco dejaron de reportar y
dos tienen huecos en medio. El detalle está en
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
