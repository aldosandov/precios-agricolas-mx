# Petición GET sin estado al SNIIM (ALD-8)

Verificación y documentación de la petición que usa el scraper para leer
precios de mayoreo de frutas y hortalizas del SNIIM. Corresponde a §8.2 y
§18 del PRD.

Fecha de verificación: 2026-08-05.

## Conclusión

La página de resultados es alcanzable por `GET` con todos los criterios en
la cadena de consulta, **sin cookies, sin `__VIEWSTATE` y sin visitar antes
el formulario**. El scraper va directo a ese endpoint; nunca ejecuta el
`POST` del formulario.

## Endpoint

```
https://www.economia-sniim.gob.mx/nuevo/Consultas/MercadosNacionales/PreciosDeMercado/Agricolas/ResultadosConsultaFechaFrutasYHortalizas.aspx
```

Responde igual por `http` y por `https`; el scraper usa `https`.

La ruta cae bajo `/nuevo/Consultas/`, que `robots.txt` permite (ver
`tests/test_robots_compliance.py`).

## URL de ejemplo reproducible

Papa Alpha - Primera, todos los orígenes y destinos, primera semana de
enero de 2026, precio por kilogramo:

```
https://www.economia-sniim.gob.mx/nuevo/Consultas/MercadosNacionales/PreciosDeMercado/Agricolas/ResultadosConsultaFechaFrutasYHortalizas.aspx?fechaInicio=01/01/2026&fechaFinal=07/01/2026&ProductoId=740&OrigenId=-1&DestinoId=-1&PreciosPorId=2&RegistrosPorPagina=1000
```

Reproducir sin cookies:

```bash
curl -s -o /dev/null -w '%{http_code}\n' --cookie /dev/null --cookie-jar /dev/null \
  'https://www.economia-sniim.gob.mx/nuevo/Consultas/MercadosNacionales/PreciosDeMercado/Agricolas/ResultadosConsultaFechaFrutasYHortalizas.aspx?fechaInicio=01/01/2026&fechaFinal=07/01/2026&ProductoId=740&OrigenId=-1&DestinoId=-1&PreciosPorId=2&RegistrosPorPagina=1000'
```

## Parámetros

| Parámetro           | Obligatorio | Formato / valores                                  | Notas |
|---------------------|-------------|----------------------------------------------------|-------|
| `fechaInicio`       | sí          | `dd/mm/aaaa`                                        | Inclusive |
| `fechaFinal`        | sí          | `dd/mm/aaaa`                                        | Inclusive |
| `ProductoId`        | sí          | id del catálogo, o `-1` = todos                     | Ver restricción abajo |
| `OrigenId`          | sí          | id de estado, o `-1` = todos                        | Estado de comercialización, no de producción |
| `DestinoId`         | sí          | id de mercado, o `-1` = todos                       | |
| `PreciosPorId`      | no          | `1` = presentación comercial, `2` = kilogramo       | Si se omite, el sitio asume `1` |
| `RegistrosPorPagina`| sí          | entero positivo, máx. `2147483647`                  | Si se omite, el sitio asume `100`. Ver [límite superior](#límite-superior-de-registrosporpagina) |

El formulario también manda `Origen=<texto>` y `Destino=<texto>`. Son
redundantes: la respuesta es idéntica si se omiten. El scraper no los envía.

Los ids de producto, origen y destino salen de los `<select>` del
formulario (`ddlProducto`, `ddlOrigen`, `ddlDestino`), que se sirve en:

```
https://www.economia-sniim.gob.mx/nuevo/Consultas/MercadosNacionales/PreciosDeMercado/Agricolas/ConsultaFrutasYHortalizas.aspx?SubOpcion=4|0
```

Ojo: la URL que se ve en el navegador es `Home.aspx?opcion=...`, un
contenedor con un `<iframe>`. El formulario real es la URL de arriba.

`scraper/catalogs.py` los extrae a `data/catalogs/`. Ya versionado:

| Catálogo | `<select>` | Archivo | Entradas |
|----------|-----------|---------|----------|
| Productos | `ddlProducto` | `data/catalogs/products.csv` | 222 (ALD-10) |
| Orígenes | `ddlOrigen` | pendiente | ALD-11 |
| Destinos | `ddlDestino` | pendiente | ALD-12 |

La opción `-1` (`Todos`) no entra al catálogo: es un modificador de la
consulta, no un miembro. Las etiquetas de producto vienen como
`Nombre - Calidad`; se parten por el **último** separador, porque
`Nuez - Western - Primera` es la variedad Western, calidad Primera. Se
guarda también la etiqueta cruda, así que la partición es reversible.

## Restricciones del servidor observadas

1. **No se puede pedir todo a la vez.** Con `ProductoId=-1` **y**
   `OrigenId=-1` **y** `DestinoId=-1` simultáneamente, la respuesta es 200
   pero sin filas y con el mensaje literal:

   > No puede seleccionar todos los productos todos los origenes y todos los destinos

   Basta fijar uno de los tres para que devuelva datos. La estrategia de
   barrido es iterar por `ProductoId` con origen y destino en `-1`.

2. **`PreciosPorId` sí cambia los valores.** Con `2`, los precios de
   presentaciones no unitarias vienen divididos entre el peso de la
   presentación (ej. `Manojo de 5 kg.` en Tuxtla Gutiérrez: 100.00 con
   `PreciosPorId=1`, 20.00 con `PreciosPorId=2`). La columna
   `Presentación` no cambia entre ambos modos, así que el modo **no es
   inferible de la respuesta**: el scraper debe registrar qué valor mandó.
   El encabezado de la página lo confirma en texto
   (`Pesos ($) por kilogramo calculado` vs `por presentación comercial`).

3. **Truncamiento silencioso por página.** El servidor corta en
   `RegistrosPorPagina` sin error. La única señal es el paginador:

   ```html
   <span id="lblPaginacion">Página  1 de  3</span>
   ```

   El parser debe leer ese `N`; si `N > 1`, la ventana de fechas se
   subdivide y se reintenta (restricción 1 del proyecto: nunca paginar).

## Límite superior de `RegistrosPorPagina`

Verificado en ALD-9, el 2026-08-05.

**No hay tope propio del parámetro más allá del `Int32` de .NET.** El
servidor acepta hasta `2147483647` y devuelve el conjunto completo en una
sola página. Se comprobó con una consulta de 67 834 registros (todos los
productos, origen Puebla, año 2025 completo): con
`RegistrosPorPagina=100000` responde 200 con las 67 834 filas y
`Página 1 de 1`. El PRD asumía 5000 como máximo verificado; el techo real
es mucho mayor.

| Valor | Respuesta | Comportamiento |
|-------|-----------|----------------|
| omitido o vacío | 200 | Asume `100` y pagina |
| `0` | **500** | `Attempted to divide by zero.` |
| `-1` (o cualquier negativo) | 200 | **Peligroso**: devuelve 1 sola fila y el paginador dice `Página 1 de -2241`. Sin error. |
| `abc` (no numérico) | **500** | `Input string was not in a correct format.` |
| `1000` … `2147483647` | 200 | Correcto; trunca solo si el total excede el valor |
| `2147483648` (desborde `Int32`) | **500** | `Arithmetic operation resulted in an overflow.` |

Los 500 son páginas de error de ASP.NET, no respuestas vacías, así que
fallan ruidosamente por sí solos. El caso `-1` es el único que pierde datos
en silencio: **el parser debe rechazar un denominador de paginador ≤ 0**,
no solo mirar si es mayor que 1.

Detalle útil: el denominador del paginador es `ceil(total / RegistrosPorPagina)`,
y con `RegistrosPorPagina=-1` sale exactamente `-total`. Sirve para conocer
el tamaño real del conjunto con una respuesta de 14 KB, pero es un truco de
sondeo, no algo de lo que dependa la ingesta.

### Qué manda el scraper

El límite que importa no es el del parámetro sino el peso de la respuesta.
Medido sobre la misma consulta (origen Puebla):

| `RegistrosPorPagina` | Filas devueltas | Bytes | Tiempo |
|----------------------|-----------------|-------|--------|
| 5 000  | 5 000 (truncado) | 4.0 MB | 4.0 s |
| 10 000 | 10 000 (truncado) | 8.0 MB | 4.7 s |
| 20 000 | 15 290 (completo, Q1) | 12 MB | 5.3 s |
| 100 000 | 67 834 (completo, año 2025) | **54 MB** | 24 s |

Una respuesta de 54 MB para parsear en memoria no vale la pena. El scraper
manda **`RegistrosPorPagina=5000`** y controla el volumen por ventana de
fechas: arranca en bloques trimestrales y subdivide cuando el paginador
reporta `N > 1`. Subir el valor no evitaría la subdivisión (un trimestre de
todos los productos ya rebasa 15 000 filas), solo cambiaría dónde duele.

## Formato de la respuesta

Tabla HTML con encabezado en la primera fila:

```
Fecha | Presentación | Origen | Destino | Precio Mín | Precio Max | Precio Frec | Obs.
```

Fila de ejemplo:

```
02/01/2026 | Kilogramo | México | Aguascalientes: Centro Comercial Agropecuario de Aguascalientes | 8.00 | 11.00 | 10.00 |
```

El parser mapea por nombre de encabezado, nunca por posición
(restricción 2), y se detiene ante un encabezado desconocido
(restricción 3).

**`Origen` es el estado desde el que se comercializa el producto, no
necesariamente donde se produjo** (restricción 7).

## Evidencia

Todas las peticiones se hicieron con cookies deshabilitadas
(`--cookie /dev/null --cookie-jar /dev/null`), sin request previo al
formulario y sin `__VIEWSTATE`.

| Prueba | Resultado |
|--------|-----------|
| `GET` Papa Alpha, 01/01–31/03/2026, `RegistrosPorPagina=1000` | `200`, 735 330 bytes, 1000 filas de datos + encabezado |
| Misma URL, segunda petición independiente | `200`, byte-idéntica → no hay estado de sesión |
| Acelga, 01/01–31/03/2026 | `200`, 926 filas, `Página 1 de 1` (sin truncar) |
| `ProductoId=-1`, `OrigenId=-1`, `DestinoId=-1` | `200`, 0 filas + mensaje de restricción |
| `ProductoId=-1`, `DestinoId=210` (Central de Abasto de Puebla), 1 día | `200`, 63 filas |
| `ProductoId=-1`, `OrigenId=21` (Puebla), 1 día | `200`, 262 filas |

El servidor manda `Set-Cookie: ASP.NET_SessionId=...` en la respuesta, pero
no la exige: ignorarla no cambia el resultado.

El `POST` del formulario, en cambio, **no** es una vía viable sin trabajo
extra: `btnBuscar` es un `<input type="image">`, así que ASP.NET espera
`btnBuscar.x` / `btnBuscar.y` además de `__VIEWSTATE` y
`__EVENTVALIDATION` frescos. Un `POST` con `btnBuscar=Buscar` devuelve el
formulario otra vez, sin resultados. Razón de más para ir directo al `GET`.

La prueba automatizada `tests/test_stateless_get.py` re-verifica en vivo lo
esencial: 200, sin cookies, con el encabezado esperado.
