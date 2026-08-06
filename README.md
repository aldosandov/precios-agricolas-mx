# precios-agricolas-mx

Precios agrícolas abiertos para México · Scraper del SNIIM, pipeline ETL y
visualizador web · En colaboración con CIMA A.C.

Pipeline SNIIM → BigQuery → Streamlit. Traduce el precio de mayoreo del
SNIIM en un precio de referencia en parcela para productores de
Atzitzintla, Puebla. Ver [`CLAUDE.md`](CLAUDE.md) para arquitectura y
restricciones del proyecto; el PRD completo vive en Notion (link en
`CLAUDE.md`).

## Estructura

```
scraper/            Scrapy. Extracción y normalización sintáctica del SNIIM.
├── spiders/         Arañas de scraping
├── parsers/         Parseo de encabezados y filas de respuesta
└── pipelines/        Escritura a la capa cruda

transform/           SQL de BigQuery
├── canonical/        Cruda → intermedia (tipado, banderas de calidad)
└── metrics/           Intermedia → final (precio en parcela, agregados)

app/                 Streamlit. Solo presentación e interacción.
├── views/            Páginas/vistas de la app
└── texts/            Copys y textos estáticos

data/                Catálogos y mapeos canónicos versionados
└── catalogs/          products/origins/destinations.csv (nunca inferidos en runtime)
                       + market_coverage*.csv (profundidad histórica por mercado)

tests/               Pruebas de parser y aritmética
└── fixtures/sniim/   HTML real capturado del SNIIM + manifest.json

docs/                Notas de investigación de la fuente
.github/workflows/   CI (pendiente de definir)
```

De Fase 1 ya están en pie los catálogos, el constructor de consultas y el
parser de resultados; `spiders/`, `pipelines/`, `transform/` y `app/` siguen
vacíos y se llenan en los issues siguientes.

## Cómo se consulta el SNIIM

El scraper no envía el formulario ASP.NET: va directo por `GET` a la página
de resultados, con todos los criterios en la cadena de consulta y sin
cookies. Pide `RegistrosPorPagina=5000` y controla el volumen por ventana
de fechas, subdividiendo cuando el paginador reporta más de una página —
nunca paginando. Endpoint, parámetros, restricciones del servidor y
evidencia en [`docs/consulta-sniim.md`](docs/consulta-sniim.md).

`scraper/query.py` arma esas URLs y decide la subdivisión. Es puro: no hace
peticiones, solo construye la consulta y lee el indicador de página de la
respuesta que le pasen.

Los ids internos que espera ese endpoint salen de los `<select>` del
formulario y viven versionados en `data/catalogs/`. Se regeneran con:

```bash
uv run python -m scraper.catalogs            # todos los catálogos
uv run python -m scraper.catalogs products   # solo uno
```

Nunca se infieren en tiempo de consulta. `tests/test_catalogs.py` compara
el archivo versionado contra el formulario en vivo, así que un producto
nuevo o renombrado en el SNIIM rompe la prueba y obliga a revisar el diff.

## Profundidad histórica

El histórico del SNIIM arranca en **1998** (el PRD suponía 2007) y no es
parejo: 32 de los 49 mercados llegan hasta 1998, 15 empiezan después, dos no
tienen datos en ningún año y cinco dejaron de reportar. Medido mercado por
mercado y año por año, sin descargar resultados:

```bash
uv run python -m scraper.coverage        # regenera la tabla (~1 h)
uv run python -m scraper.coverage 210    # sondea un mercado, sin escribir
```

Resultado versionado en `data/catalogs/market_coverage.csv` (primer y último
año, huecos y volumen por mercado) con la evidencia por año en
`market_coverage_by_year.csv`. Hallazgos y método en
[`docs/profundidad-historica.md`](docs/profundidad-historica.md).

## Parser de resultados

`scraper/parsers/results.py` lee la tabla **por el encabezado de cada
respuesta**, nunca por posición. No es purismo: todo criterio que la consulta
fije a un solo valor desaparece de la tabla, así que una consulta de un solo
día no trae columna `Fecha` y una de todos los productos trae `Producto` y
`Calidad` en lugar de `Destino`. Un parser posicional no fallaría con esas
respuestas — escribiría presentaciones en la columna de fecha.

```python
table = parse_results(html)
table.columns   # ('date', 'presentation', 'origin', 'destination', ...)
table.rows[0]   # {'category': 'Hortalizas', 'date': '01/07/2026', ...}
```

Reglas que sostiene:

- Un encabezado fuera del vocabulario conocido levanta `UnknownColumn` y
  detiene la ingesta; nunca se descarta en silencio.
- Los separadores de categoría (`Frutas`, `Frutas de Temporada`,
  `Hortalizas`, `Chiles Secos`) no son datos: se arrastran como `category` de
  las filas que siguen.
- Devuelve los valores como strings, tal cual los sirvió el SNIIM. El tipado
  y las banderas de calidad son de la capa intermedia de BigQuery, porque la
  capa cruda es inmutable y la validación marca sin corregir.
- Distingue las tres respuestas 200 que no son datos: rechazo por criterios
  (`QueryRejected`), rango sin registros (tabla vacía) y cualquier otra cosa
  (`UnexpectedResponse`). Nunca lee un cambio del sitio como "no hubo
  precios".

Los campos que la consulta fijó no salen de aquí: el parser solo reporta lo
que dijo la respuesta, y el spider agrega lo que él mandó (producto,
`PreciosPorId`, ventana de fechas).

## Contrato de la capa cruda

`scraper/contract.py` arma el registro que se escribe, mezclando lo que dijo
la tabla con lo que mandó la consulta, y rechaza lo que no puede ser una
observación de precio.

```python
records = build_records(table, QueryContext(product_id="740", ..., window=w))
records[0].natural_key
# ('01/07/2026', 'id:740', 'Kilogramo', 'Guanajuato',
#  'Aguascalientes: Centro Comercial Agropecuario de Aguascalientes', '2')
```

Cada criterio queda registrado en dos planos: la etiqueta que sirvió la tabla
(`product`, `origin`, `destination`, `None` si la consulta lo fijó) y el id
que se envió (`product_id`, `origin_id`, `destination_id`). La llave natural
usa la etiqueta si la hay y si no `id:<n>`; `-1` no identifica nada, porque
significa "todos".

Qué exige el contrato: fecha, presentación, identidad de producto, origen y
destino, y el modo de precio. Faltar cualquiera levanta `IncompleteRow`
nombrando todos los que faltan, y dos filas de una misma respuesta con la
misma llave levantan `DuplicateNaturalKey`. Se detiene en vez de descartar:
una fila descartada deja un hueco idéntico a un día que el mercado no reportó.

La fecha sale de la tabla, salvo en una consulta de un solo día —que no trae
columna `Fecha`—, donde sale de la ventana. Una ventana más ancha sin esa
columna no es atribuible y detiene la ingesta.

**Los precios no entran al contrato.** Un precio faltante es calidad de dato:
lo marca la capa intermedia, que nunca corrige. El contrato es sobre
identidad, no sobre valores.

## Ingesta diaria

```bash
uv run python -m scraper.spiders.daily                    # 49 mercados, 7 días atrás
uv run python -m scraper.spiders.daily --days 3
uv run python -m scraper.spiders.daily --market 210       # un solo mercado
```

Recorre los 49 mercados con producto y origen en `-1`, y pide cada uno **dos
veces**, una por tipo de precio: con `PreciosPorId=2` los precios vienen
divididos entre el peso de la presentación, y la respuesta no dice con cuál
contestó.

La ventana llega varios días hacia atrás a propósito. La fuente publica tarde
algunos días y reprocesar no cuesta nada, porque la partición se reescribe.

Si la respuesta viene truncada, la ventana se parte a la mitad y se vuelve a
pedir — nunca se pagina. Si un solo día sigue desbordando ya no hay nada que
partir: eso se registra como fallo, no se escribe media página.

**Un mercado que falla no le cuesta el día a los otros 48.** Un encabezado
desconocido, un rechazo o una fila incompleta detienen ese mercado, se anotan
y el barrido sigue; al final el proceso sale con código distinto de cero para
que Actions marque el job en rojo. Perder un día es irrecuperable, pero un
cambio de esquema que nadie note también.

## Cortesía con la fuente

El SNIIM es un servidor de la Secretaría de Economía y responde **503** cuando
se le exige. El proyecto no tiene prisa que justifique arriesgarlo, así que el
crawl es lento a propósito (`scraper/settings.py`):

| Ajuste | Valor | Por qué |
|---|---|---|
| `CONCURRENT_REQUESTS(_PER_DOMAIN)` | 2 | Techo, no meta |
| `DOWNLOAD_DELAY` / `AUTOTHROTTLE_START_DELAY` | 3 s | Piso entre peticiones |
| `AUTOTHROTTLE_TARGET_CONCURRENCY` | 1.0 | Una petición en vuelo en promedio |
| `AUTOTHROTTLE_MAX_DELAY` | 120 s | Un 503 tarda minutos en despejarse |
| `USER_AGENT` | `precios-agricolas-mx/0.1 (+URL del repo)` | Identificable, con dónde reclamar |

Medido en una corrida real de tres mercados: 6 peticiones en 29.5 s, con
separaciones de 3 a 7 segundos. Autothrottle sube el intervalo solo cuando el
servidor tarda más en contestar.

La **caché HTTP** existe para el backfill, que corre horas y no puede volver a
descargar lo que ya trajo. Está apagada por defecto: la corrida diaria tiene
que ver los precios de hoy, no la copia de ayer. Se prende con `--cache`:

```bash
uv run python -m scraper.spiders.daily --days 1 --market 10 --cache
```

La segunda corrida del ejemplo tarda 0.15 s en vez de 13.7 s y produce las
mismas 120 filas. Los 5xx nunca se cachean: un 503 guardado reaparecería como
ventana perdida en cada reanudación. `httpcache/` no se versiona.

## Salida: NDJSON particionado por fecha

`scraper/pipelines/ndjson.py` convierte los registros del contrato en la fila
de la capa cruda del PRD (§10) y los escribe a archivos, nunca por streaming:
la carga por archivo a BigQuery es gratuita y la inserción en streaming no.

```
out/raw/fecha=2026-07-01/destino=210_precio=kilogramo_calculado.ndjson
out/raw/fecha=2026-07-02/destino=210_precio=kilogramo_calculado.ndjson
```

Un archivo por fecha, mercado y modo de precio. Como una ventana se consulta
completa, cada fecha cae en un solo archivo, y volver a correr esa ventana lo
**reescribe** en vez de agregarle una segunda copia del mismo día: ahí está la
idempotencia, sin necesidad de deduplicar después.

Aquí es donde las cadenas se vuelven la fila de §10: `fecha` en ISO,
`precio_min/max/frecuente` numéricos (con separador de miles resuelto),
`tipo_precio` como nombre (`kilogramo_calculado`, `presentacion_comercial`) en
vez del id que se mandó, y `llave_fila` como sha256 de la llave natural, que
distingue los dos modos de precio de la misma observación.

Las banderas de calidad viajan con la fila y **nada se corrige**: un precio en
cero, ausente o ilegible se marca (`precio_cero`, `precio_ausente`,
`precio_no_numerico`) y se conserva tal cual; si el mínimo, el frecuente y el
máximo no van en orden, se marca `precios_incoherentes`. Lo único que detiene
la ingesta aquí es una fecha ilegible, porque sin fecha no hay partición.

`out/` no se versiona.

## Capa cruda en BigQuery

DDL versionado en [`transform/raw/prices_table.sql`](transform/raw/prices_table.sql).
Proyecto `precios-agricolas-mx`, dataset `crudo`, ubicación **US**
(multirregión, donde aplica la capa gratuita y donde corre la app).

```sql
`precios-agricolas-mx.crudo.precios`
PARTITION BY fecha
CLUSTER BY producto_raw, destino_id, tipo_precio
```

Partición **por día**, no por mes: el job incremental escribe un día a la vez
y las auditorías de cobertura razonan por día. El clustering sigue lo que se
consulta junto — producto y mercado (§9.2).

Los tipos no son cosméticos: `fecha` es `DATE` porque de ahí sale la
partición, y los precios son `NUMERIC` porque el dinero en punto flotante
deriva al agregarse. Solo van `NOT NULL` los campos que el contrato de la
ingesta garantiza; las etiquetas que la consulta borra de la tabla
(`producto_raw`, `origen_raw`…) quedan nulas si algún día se fija otro
criterio.

Aplicar desde cero:

```bash
bq --location=US mk --dataset precios-agricolas-mx:crudo
bq query --use_legacy_sql=false --project_id=precios-agricolas-mx \
  < transform/raw/prices_table.sql
```

`tests/test_raw_table_ddl.py` compara el DDL contra `RAW_SCHEMA` del pipeline:
si el scraper empieza a emitir un campo que la tabla no declara, falla la
prueba en vez de fallar la carga.

## Carga a BigQuery

```bash
uv run python -m transform.load                  # todo lo que haya en out/raw
uv run python -m transform.load --dry-run        # listar sin tocar BigQuery
uv run python -m transform.load --table proyecto.dataset.tabla
```

Corre **aparte del scraper**: el spider deja archivos, esto los lee. Así la
ingesta no depende de que la facturación esté lista ni de tener credenciales
de GCP.

La carga es un **upsert sobre `llave_fila`**, no un reemplazo de particiones.
El PRD (§8.8) pide inserción o actualización sobre la llave natural, y hay una
razón concreta: el barrido escribe un mercado a la vez, así que truncar el día
para cargar el mercado 210 borraría los 48 que ya estaban. Además el SNIIM
revisa precios pasados, y una recarga tiene que traer la corrección, no
conservar la fila vieja.

El camino es: cargar los archivos a una tabla de paso (que expira sola a las 6
horas por si una corrida muere), hacer un `MERGE` y borrarla. El `MERGE` lleva
el rango de fechas en el `ON` para que BigQuery lea solo las particiones que
se están cargando y no las 19 años de histórico.

Correr la misma carga dos veces no cambia el conteo de filas — verificado
contra BigQuery real en `tests/test_load_raw_live.py`, que también comprueba
que un precio revisado sobrescribe al anterior.

## Job diario en GitHub Actions

[`.github/workflows/daily.yml`](.github/workflows/daily.yml). Corre a las
**23:00 UTC de lunes a viernes** (17:00 en el centro de México, con el día
hábil ya publicado) y también a mano desde la pestaña Actions, con la ventana
y el mercado como parámetros.

Un job de una tarea al día no justifica un orquestador con base de datos,
planificador y trabajadores (§8.9). Los reintentos que importan ya viven en
Scrapy, que reintenta los 503 cuatro veces con autothrottle.

Tres decisiones que evitan fallos silenciosos:

- **Un mercado caído no cancela la carga.** El barrido sale distinto de cero
  si algún mercado falló, pero el paso está en `continue-on-error` y el fallo
  se arrastra hasta el último paso. Lo que capturaron los otros 48 llega a
  BigQuery igual, y la corrida termina en rojo.
- **Una ventana sin precios no es un fallo.** Si no hay archivos, la carga se
  salta. Que no haya datos nuevos lo vigila la alerta, no el código de salida.
- **Sin llaves.** Se autentica con Workload Identity Federation: GitHub emite
  un token OIDC y GCP lo cambia por credenciales de la service account
  `ingesta-diaria`. No hay ningún secret que rotar, y la federación está
  restringida a este repositorio.

El NDJSON de cada corrida queda como artifact 7 días, para poder recargar sin
volver a pedirle nada al SNIIM.

El backfill histórico **no** corre aquí: se ejecuta a mano una sola vez.

## Fixtures

El parser se prueba contra HTML real guardado, nunca inventado. Las
respuestas están en `tests/fixtures/sniim/`, con `manifest.json` guardando
la URL exacta que produjo cada una y qué caso ejercita. Cubren las formas
en que cambia el esquema de la tabla, el truncamiento por paginador, los
separadores de categoría y la respuesta de rechazo.

```bash
uv run python -m scraper.fixtures                     # recapturar todas
uv run python -m scraper.fixtures paginated_overflow  # solo una
```

Recapturar reescribe historia: el SNIIM revisa precios pasados, así que hay
que revisar el diff antes de commitear. `tests/test_fixtures.py` corre sin
red y detecta si una recaptura cambió la forma de la respuesta.

## Setup

Requiere Python ≥3.11 y [`uv`](https://docs.astral.sh/uv/).

```bash
uv sync              # instala dependencias (scraper + transform + app + dev)
```

## Correr el proyecto

Aún no hay entry points ejecutables — scraper, transform y app se
construyen en los issues siguientes de Fase 1 (ver Linear, proyecto
`precios-agricolas-mx`). Esta sección se actualiza según se materialice
cada pieza:

```bash
# ingesta diaria (49 mercados, dos tipos de precio)
uv run python -m scraper.spiders.daily --days 7

# tests
uv run pytest
```

El backfill histórico (ALD-30) y la carga a BigQuery (ALD-23) siguen
pendientes.

## Dependencias

Gestionadas con `uv` en `pyproject.toml` / `uv.lock`. Stack: Scrapy,
parsel, pandas, google-cloud-bigquery, Streamlit, Plotly, pytest.
