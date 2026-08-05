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
| `RegistrosPorPagina`| sí          | entero                                              | Se probó `1000`; el límite superior real lo determina ALD-9 |

El formulario también manda `Origen=<texto>` y `Destino=<texto>`. Son
redundantes: la respuesta es idéntica si se omiten. El scraper no los envía.

Los ids de producto, origen y destino salen de los `<select>` del
formulario (`ddlProducto`, `ddlOrigen`, `ddlDestino`); extraerlos y
versionarlos es trabajo de ALD-10, ALD-11 y ALD-12.

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
