-- Capa cruda: la fila tal como la extrajo el scraper (PRD §10).
--
-- Inmutable por regla del proyecto: nada se edita ni se limpia aquí. Las
-- banderas de calidad que trae cada fila marcan lo que salió raro en la
-- ingesta; corregirlo es trabajo de la capa intermedia, que se reconstruye
-- desde esta sin tocarla.
--
-- Se carga por archivo con `bq load` desde el NDJSON que deja
-- `scraper/pipelines/ndjson.py`. La carga por archivo es gratuita; la
-- inserción en streaming no.
--
-- Aplicar:
--   bq --location=US mk --dataset --description "Capa cruda del SNIIM" crudo
--   bq query --use_legacy_sql=false --project_id=precios-agricolas-mx \
--     < transform/raw/prices_table.sql

CREATE TABLE IF NOT EXISTS `precios-agricolas-mx.crudo.precios`
(
  fecha DATE NOT NULL OPTIONS (description = 'Día del precio, según la tabla del SNIIM o, en consultas de un solo día, la ventana consultada'),
  producto_raw STRING OPTIONS (description = 'Nombre literal del producto; nulo si la consulta fijó el producto'),
  calidad_raw STRING OPTIONS (description = 'Calidad literal, tal como la separa la tabla de resultados'),
  presentacion_raw STRING NOT NULL OPTIONS (description = 'Presentación comercial literal, p. ej. "Caja de 10 kg."'),
  origen_raw STRING OPTIONS (description = 'Estado de COMERCIALIZACIÓN, no de producción (PRD §5.3)'),
  destino_raw STRING OPTIONS (description = 'Etiqueta del mercado; con destino fijo se toma del catálogo versionado'),
  destino_id INT64 OPTIONS (description = 'Id de mercado que se envió en la consulta'),
  grupo_raw STRING OPTIONS (description = 'Categoría arrastrada del separador: Frutas, Hortalizas, Chiles Secos...'),
  tipo_precio STRING NOT NULL OPTIONS (description = 'kilogramo_calculado o presentacion_comercial; no es inferible de la respuesta'),
  precio_min NUMERIC OPTIONS (description = 'Precio más bajo de la muestra'),
  precio_max NUMERIC OPTIONS (description = 'Precio más alto de la muestra'),
  precio_frecuente NUMERIC OPTIONS (description = 'Moda de la muestra, no promedio (PRD §5.2)'),
  observaciones STRING OPTIONS (description = 'Columna Obs. del SNIIM; trae el país cuando el origen es Importación'),
  banderas_calidad ARRAY<STRING> OPTIONS (description = 'Marcas de la ingesta: precio_ausente, precio_cero, precio_no_numerico, precios_incoherentes'),
  extraido_en TIMESTAMP NOT NULL OPTIONS (description = 'Cuándo se descargó la respuesta'),
  url_consulta STRING NOT NULL OPTIONS (description = 'URL exacta que produjo la fila; hace la extracción reproducible'),
  llave_fila STRING NOT NULL OPTIONS (description = 'sha256 de la llave natural (PRD §8.8); distingue los dos tipos de precio')
)
PARTITION BY fecha
CLUSTER BY producto_raw, destino_id, tipo_precio
OPTIONS (
  description = 'Precios de mayoreo del SNIIM, capa cruda e inmutable. Una fila por llave natural: fecha, producto, calidad, presentación, origen, destino y tipo de precio.',
  require_partition_filter = FALSE
);
