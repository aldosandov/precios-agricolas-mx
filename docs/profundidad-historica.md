# Profundidad histórica por mercado (ALD-14)

Hasta cuándo llega hacia atrás el histórico del SNIIM, mercado por mercado.
Acota el backfill de la Fase 2 (ALD-27). Corresponde a §5.1 y §18 del PRD.

Fecha de medición: 2026-08-05.

## Conclusión

**El histórico arranca en 1998, no en 2007.** El 2007 del PRD venía de un
sondeo a la Central de Abasto de Iztapalapa y quedaba nueve años corto: ese
mercado tiene datos desde 1998, igual que otros 31.

De los 49 mercados del catálogo:

| Situación | Mercados |
|-----------|----------|
| Con datos desde 1998 | 32 |
| Con datos desde un año posterior | 15 |
| **Sin datos en ningún año** | **2** |
| Reportando todavía en 2026 | 42 |
| Dejaron de reportar antes de 2026 | 5 |

Nada antes de 1998: se sondeó desde 1990 y los años 1990–1997 salen vacíos
para los 49 mercados, así que el piso está medido, no supuesto.

Volumen total del histórico: **15 347 388 registros** (todos los productos,
todos los orígenes, los 49 mercados, 1998–2026). El año más flaco es 1999
con 94 249 filas y el más cargado 2016 con 659 007; 2026 lleva 321 554 al
momento de medir.

## Tablas versionadas

| Archivo | Contenido |
|---------|-----------|
| `data/catalogs/market_coverage.csv` | Una fila por mercado: `first_year`, `last_year`, `years_with_data`, `total_rows`, `gap_years` |
| `data/catalogs/market_coverage_by_year.csv` | La evidencia cruda: filas por mercado y año, los 1 813 sondeos |

El resumen es derivable del detalle y `tests/test_coverage.py` verifica que
sigan coincidiendo: si alguien edita a mano el resumen, la prueba falla.

Se regeneran con:

```bash
uv run python -m scraper.coverage        # los 49 mercados (~1 h)
uv run python -m scraper.coverage 210    # sondear uno, sin escribir
```

## Los 15 mercados que no arrancan en 1998

| Desde | Hasta | id | Mercado |
|-------|-------|----|---------|
| 2000 | 2022 | 102 | Durango: Central de Abasto "Francisco Villa" |
| 2000 | 2026 | 121 | Guerrero: Central de Abastos de Acapulco |
| 2000 | 2026 | 240 | San Luis Potosí: Centro de Abasto de San Luis Potosí |
| 2000 | 2026 | 306 | Veracruz: Mercado Malibrán |
| 2002 | 2026 | 33 | Baja California: Central de Abasto INDIA, Tijuana |
| 2002 | 2026 | 130 | Hidalgo: Central de Abasto de Pachuca |
| 2002 | 2019 | 231 | Quintana Roo: Módulo de Abasto Cancún |
| 2003 | 2026 | 61 | Chihuahua: Central de Abasto de Chihuahua |
| 2003 | 2026 | 63 | Chihuahua: Mercado de Abasto de Cd. Juárez |
| 2004 | 2026 | 281 | Tamaulipas: Módulo de Abasto de Reynosa |
| 2006 | 2026 | 301 | Veracruz: Central de Abasto de Jalapa |
| 2007 | 2017 | 112 | Guanajuato: Mercado de Abasto de Celaya ("Benito Juárez") |
| 2014 | 2026 | 172 | Morelos: Mercado "Adolfo López Mateos" de Cuernavaca |
| 2019 | 2026 | 181 | Nayarit: Nayarabastos de Tepic |
| 2023 | 2026 | 252 | Sinaloa: Zona de Abasto de Mazatlán |

## Mercados que el catálogo lista pero el histórico no respalda

**Dos mercados no tienen un solo registro en ningún año**: `71` (Chiapas:
Tapachula) y `122` (Guerrero: Chilpancingo de los Bravo). Están en el
`<select>` del formulario y son consultables sin error — la respuesta es un
`NO HAY REGISTROS` limpio. Estar en el catálogo no implica tener datos, y
nada aguas abajo debe asumir lo contrario.

**Cinco dejaron de reportar** y su última cifra no es reciente:

| id | Mercado | Último año |
|----|---------|------------|
| 111 | Guanajuato: Módulo de Abasto Irapuato | 2017 |
| 112 | Guanajuato: Mercado de Abasto de Celaya | 2017 |
| 231 | Quintana Roo: Módulo de Abasto Cancún | 2019 |
| 102 | Durango: Central de Abasto "Francisco Villa" | 2022 |
| 170 | Morelos: Central de Abasto de Cuautla | 2025 |

Para la app importa: una serie que se corta en 2017 no es un hueco de
ingesta, es el mercado que dejó de reportar. La capa final tiene que poder
distinguirlas, y esta tabla es la referencia para hacerlo.

**Dos tienen huecos internos**, años sin datos entre su primer y su último
año: `270` (Villahermosa) no reporta 1999, 2000 ni 2001; `320` (Zacatecas)
no reporta 2003. Por eso el resumen guarda `gap_years` y no solo el rango:
un backfill que asuma continuidad entre `first_year` y `last_year` va a
registrar como fallidas unas ventanas que simplemente están vacías.

## Método

Un sondeo por mercado y año — 49 × 37 años (1990–2026) = 1 813 peticiones.
La consulta fija el destino y deja producto y origen en `-1`, con la ventana
en el año calendario completo.

**No se descargan resultados.** Con `RegistrosPorPagina=-1` el servidor
devuelve una sola fila y el paginador dice `Página 1 de -N`, donde `N` es el
total exacto del conjunto (`ceil(total / -1) == -total`). Una respuesta de
~14 KB en lugar de decenas de MB. El truco ya estaba documentado en
[`consulta-sniim.md`](consulta-sniim.md) como algo **prohibido en la
ingesta** — ahí pierde datos en silencio — y aquí es justo lo que se quiere:
el conteo sin las filas.

Implementado en `scraper/coverage.py`.

### Trampas encontradas

- **Un año sin datos responde 200 con el texto literal `NO HAY REGISTROS`**
  y sin paginador. Es distinto del mensaje de rechazo por criterios (todos
  los `-1` a la vez) y no se pueden confundir: el sondeo lee el rechazo como
  error, no como "no hubo precios". Cualquier otra respuesta también levanta
  excepción, en vez de contarse como cero.
- **El SNIIM devuelve 503 bajo carga.** Con cuatro peticiones en paralelo y
  reintento inmediato, la corrida murió a los ~280 sondeos. Los 503 no son
  permanentes, pero reintentar al instante gana otro: el sondeo espera
  5 s, 15 s, 45 s y 120 s entre intentos.
- Por eso el escaneo escribe cada resultado a un checkpoint
  (`data/catalogs/market_coverage.partial.csv`, ignorado por git) antes de
  pedir el siguiente, y una corrida nueva retoma donde se cayó. Una hora de
  sondeos no se puede perder por un 503.
- Los años vacíos responden en milisegundos y los que tienen datos tardan
  segundos: el costo real del escaneo es proporcional a los datos que hay,
  no a la reja completa.

## Qué se hace con esto

1. **El backfill (ALD-27) arranca en 1998**, no en 2007. Son nueve años más
   de histórico de los que suponía el PRD.
2. El barrido de la Fase 2 fija el mercado de destino (PRD §8.2), así que
   esta tabla no solo acota el rango global: da el piso y el techo de cada
   mercado y permite **no pedir los años que nunca tuvieron datos**. Dos
   mercados se saltan enteros y cinco terminan antes de 2026. Los huecos
   internos también quedan documentados aquí, para que una ventana vacía se
   reconozca como esperada y no como fallo de ingesta.
3. Las cifras de `total_rows` dan el orden de magnitud para dimensionar
   BigQuery: ~15.3 millones de filas en la capa cruda, lo que cabe holgado
   en la restricción de USD 20/mes con partición por día.
