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
de los 49 mercados, parser probado contra HTML real, validación de contrato,
carga idempotente y job programado en GitHub Actions. Todavía no hay app.

## Cómo funciona

```
SNIIM (ASP.NET)
   │  GET sin cookies, ventana de fechas adaptativa
   ▼
scraper/        Scrapy: extracción y normalización sintáctica
   │  NDJSON particionado por fecha
   ▼
transform/      BigQuery: cruda → intermedia → final
   │  solo la capa final
   ▼
app/            Streamlit
```

Tres capas en BigQuery, con una regla por capa: la **cruda** es inmutable y
guarda la fila tal como se extrajo; la **intermedia** tipa, resuelve llaves
canónicas y marca problemas de calidad sin corregirlos; la **final** son
tablas agregadas y es lo único que la app puede leer.

### Las decisiones que sostienen la ingesta

Están documentadas con su evidencia en [`docs/`](docs/), pero estas son las
que explican el diseño:

- **El parser lee el encabezado de cada respuesta.** El SNIIM cambia el número
  y el orden de las columnas según lo que se fije en la consulta: pedir un solo
  día quita la columna `Fecha`, fijar el mercado quita `Destino` y añade
  `Producto` y `Calidad`. Un parser que mapeara por posición no fallaría con
  esas respuestas — escribiría datos corridos en silencio.
- **Un encabezado desconocido detiene la ingesta.** Una columna nueva en la
  fuente es una decisión humana, no un dato que se descarta.
- **Nunca se pagina.** El botón "siguiente" es un postback con ~213 KB de
  estado de vista. El volumen se controla partiendo la ventana de fechas cuando
  el servidor avisa que truncó.
- **La carga es idempotente** sobre la llave natural de cada fila, así que
  correr el mismo día dos veces no duplica nada y reprocesar un mes no ensucia
  el histórico.
- **El crawl es lento a propósito.** Dos peticiones a la vez, tres segundos de
  espera mínima y un user agent identificable: la fuente es un servidor de
  gobierno que responde 503 si se le exige.

### Lo que se midió en vez de suponerse

El PRD daba por hecho que el histórico empezaba en 2007. Sondeando mercado por
mercado resultó que **arranca en 1998** — nueve años más de datos — y que no es
parejo: 32 de los 49 mercados llegan hasta 1998, dos nunca tuvieron datos y
cinco dejaron de reportar. Detalle en
[`docs/profundidad-historica.md`](docs/profundidad-historica.md).

## Empezar

Requiere Python ≥3.11 y [`uv`](https://docs.astral.sh/uv/).

```bash
uv sync          # instala dependencias
uv run pytest    # la suite; parte pega al SNIIM en vivo
```

### Ingesta diaria

```bash
uv run python -m scraper.spiders.daily                # 49 mercados, 7 días atrás
uv run python -m scraper.spiders.daily --days 1 --market 210
```

Recorre los mercados con producto y origen en "todos", y pide cada uno dos
veces —una por tipo de precio— porque el SNIIM publica el precio por
presentación comercial y por kilogramo calculado, y la respuesta no dice cuál
de los dos contestó. Deja los archivos en `out/raw/fecha=YYYY-MM-DD/`.

Un mercado que falle no le cuesta el día a los otros 48: se anota, el barrido
sigue y el proceso termina en rojo.

### Carga a BigQuery

```bash
uv run python -m transform.load --dry-run   # listar sin tocar nada
uv run python -m transform.load
```

Corre aparte del scraper: el spider deja archivos, esto los lee. La carga por
archivo es gratuita y la inserción en streaming no.

### Catálogos y fixtures

Los identificadores internos que espera el SNIIM (222 productos, 35 orígenes,
49 mercados) viven versionados en `data/catalogs/`, nunca se infieren en
tiempo de consulta:

```bash
uv run python -m scraper.catalogs      # regenerar catálogos
uv run python -m scraper.coverage      # medir profundidad histórica (~1 h)
uv run python -m scraper.fixtures      # recapturar HTML de prueba
```

El parser se prueba contra HTML real guardado en `tests/fixtures/sniim/`, con
un manifiesto que registra la URL exacta que produjo cada respuesta. Nunca
contra marcado inventado: el marcado inventado le da la razón al parser.

## Automatización

[`.github/workflows/daily.yml`](.github/workflows/daily.yml) corre el barrido y
la carga a las 23:00 UTC de lunes a viernes, y también a mano desde la pestaña
Actions. Se autentica con Workload Identity Federation, así que no hay ninguna
llave guardada como secret. El backfill histórico no corre ahí: se ejecuta a
mano una sola vez.

## Estructura

```
scraper/     Scrapy. Solo extracción y normalización sintáctica
├── parsers/   lectura del HTML, aislada y probada con fixtures
├── spiders/   barrido diario (el de backfill viene en la fase 2)
└── pipelines/ salida a NDJSON particionado
transform/   SQL de BigQuery y carga a la capa cruda
app/         Streamlit (pendiente)
data/        catálogos y mapeos canónicos versionados
docs/        notas de investigación de la fuente, con evidencia
tests/       fixtures de HTML real y pruebas
```

## Fuente y atribución

Sistema Nacional de Información e Integración de Mercados (**SNIIM**),
Secretaría de Economía. Sección Mercados Nacionales, Frutas y Hortalizas.
Es la única fuente del proyecto.

La herramienta informa precios de mayoreo publicados por la Secretaría de
Economía. No garantiza que ningún comprador pague ese precio, no interviene en
la transacción y no constituye asesoría comercial.

## Licencia

[MIT](LICENSE).
