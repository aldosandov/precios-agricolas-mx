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
uv run python -m scraper.spiders.daily                # 43 mercados, 5 días atrás
uv run python -m scraper.spiders.daily --days 1 --market 210
uv run python -m scraper.spiders.daily --progress     # barra de avance
```

Recorre los mercados con producto y origen en "todos", y pide cada uno dos
veces —una por tipo de precio— porque el SNIIM publica el precio por
presentación comercial y por kilogramo calculado, y la respuesta no dice cuál
de los dos contestó. Deja los archivos en `out/raw/fecha=YYYY-MM-DD/`.

Son 43 mercados y no los 49 del catálogo: seis llevan años sin publicar y
pedirles todos los días gastaría un décimo del barrido en filas que no
existen. La contrapartida es que un mercado que reviva quedaría invisible,
así que conviene volver a correr `scraper.coverage` cada tanto.

La ventana llega cinco días hacia atrás porque el fin de semana se come dos:
desde un lunes alcanza el jueves anterior, que es el margen para recuperar lo
que la fuente publique tarde. Ampliarla no cuesta peticiones —son 86 sea cual
sea la ventana— solo respuestas más grandes: medido, 5 MB por corrida con un
día y 25 MB con siete.

Un mercado que falle no le cuesta el día a los otros 48: se anota, el barrido
sigue y el proceso termina en rojo. Eso incluye la petición que se rinde tras
agotar sus reintentos —un 503 sostenido, un timeout—: Scrapy la deja ir sin más
que una línea de log, así que un `errback` la anota igual que a una respuesta
ilegible. Sin eso el mercado desaparecería del día y la corrida terminaría en
verde, que es justo la forma de falla que esta ingesta existe para no tener.

La ventana se calcula con la fecha **del centro de México**, no la de la
máquina que corre el barrido: en un runner en UTC, después de las 18:00 hora
local ya es el día siguiente y se pediría un día que la fuente aún no publica.

Con `--progress` el log completo se va a `out/daily.log` y la terminal queda
para una barra de avance y una línea por ventana fallida. Existe para el
backfill de la fase 2 —horas de corrida en una laptop, donde la consola es una
interfaz y no un log—; el barrido diario corre sin ella, porque en Actions un
log plano se lee mejor que una barra que nadie mira. Si la salida no es una
terminal, degrada sola a una línea de estado cada 30 segundos.

### Backfill histórico

```bash
uv run python -m scraper.spiders.backfill                  # seguir donde quedó
uv run python -m scraper.spiders.backfill --limit 500      # una sesión acotada
uv run python -m scraper.spiders.backfill --market 210 \
    --desde 2011-07-01 --hasta 2011-09-30
uv run python -m scraper.backfill_state                    # cuánto falta, qué falló
```

No corre en Actions: se ejecuta a mano, en sesiones de tiempo libre a lo largo
de varios días. **Arrancar y parar es el modo normal de operación**, no una
falla — `Ctrl-C` o un apagón dejan a lo sumo los bloques que estaban en vuelo,
y la sesión siguiente los retoma sin volver a pedir nada de lo ya cubierto.

La unidad de trabajo es (mercado, modo de precio, trimestre): **9 628 bloques**
que salen del sondeo de profundidad histórica, nunca del calendario, así que no
se gasta una sola petición en años que la fuente nunca tuvo. El estado vive en
`out/cobertura.sqlite`, con un commit por bloque cerrado.

Una sola ventana ilegible tiñe su trimestre entero y lo devuelve a la cola.
Repetirlo solo cuesta peticiones, porque la carga es idempotente; registrar como
cubierto un trimestre del que faltó un pedazo sería el único fallo sin rastro.

**Cada trimestre terminado se carga a BigQuery y se borra del disco.** El
histórico completo son ~10.7 GB de NDJSON y esto corre en una laptop, así que no
se guarda lo que ya está en un lugar mejor: en vuelo nunca hay más de un
trimestre, ~93 MB. Antes de borrar se cuentan las filas del disco contra las que
entraron a la tabla de paso — una carga que se saltó filas y una que las tomó
todas se ven igual desde afuera, y la diferencia es justo lo que se va a borrar.
El orden cronológico de la rejilla es lo que además abarata el `MERGE`: el rango
de fechas lo poda a las particiones de ese trimestre en vez de barrer el
histórico. Con `--no-load` solo se deja el NDJSON.

La caché HTTP se queda apagada. Se había puesto para que el backfill reanudara
sin volver a descargar; eso lo hace ahora la tabla de cobertura con unos cientos
de KB, contra los 20+ GB que costaría cachear 9 600 respuestas de megabytes.

### Carga a BigQuery

```bash
uv run python -m transform.load --dry-run   # listar sin tocar nada
uv run python -m transform.load
uv run python -m transform.load --desde 2011-07-01 --hasta 2011-09-30 --purge
```

Corre aparte del scraper: el spider deja archivos, esto los lee. La carga por
archivo es gratuita y la inserción en streaming no. El único lugar donde las dos
mitades se conocen es el `main()` del backfill, que engancha la carga de cada
trimestre terminado; el spider nunca importa `transform/`.

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

### Trazabilidad de cada corrida

```bash
uv run python -m transform.monitor --days 7 --dry-run   # imprimir sin enviar
uv run python -m transform.monitor --days 7             # requiere SENTRY_DSN
```

Una ingesta que deja de capturar en silencio se ve igual que una fuente que
dejó de publicar, y las dos se ven como un job en verde. Así que cada corrida
consulta BigQuery —no el log del spider— y reporta a Sentry qué se tiene y qué
falta:

| Señal | Cuándo |
|---|---|
| Resumen: filas, mercados y fechas cubiertas | siempre |
| Mercados activos sin una sola fila en la ventana | cuando falta alguno |
| Ventana entera vacía | cuando no llegó nada |
| Sin fecha nueva en 3 días hábiles o más | fuente detenida |
| Ventanas que reventaron en el barrido | cuando las hay |

Los "mercados activos" salen de `market_coverage.csv`: los dos que nunca
tuvieron datos y los cinco que dejaron de reportar quedan fuera, porque una
alerta que suena todos los días deja de leerse. Los días hábiles se cuentan de
lunes a viernes; los feriados no se modelan, el umbral los absorbe.

La corrida además hace *check-in* en un monitor de Sentry que conoce el
horario, así que también avisa del caso que ningún log detecta: **el job que
nunca corrió**.

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
├── spiders/   barrido diario y backfill histórico
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
