# Detector de restaurantes — Market research para agentes de voz

## Contexto y fin del proyecto

Soy Juanjo, data engineer en Barcelona, construyendo un negocio de automatización con IA para pymes. Mi producto principal es una **recepcionista telefónica con IA**: contesta llamadas, agenda citas y responde preguntas frecuentes. Nada más; la idea es que sea simple.

- **Stack del agente de voz:** Retell AI (motor de voz) + Zadarma (números españoles vía SIP) + n8n (automatizaciones). Debe hablar español y catalán.
- **Modelo:** SaaS bajo mi propia marca, con varios clientes corriendo sobre mi infraestructura y cuentas.
- **Nicho principal:** salones (barberías, peluquerías, estética). Primer cliente en configuración: Ramiro Barber Shop (Les Corts), cuyas reservas están en Bewe.

Este repo es **market research para explorar el vertical de restaurantes**: encontrar restaurantes que no usan un sistema de reservas digital y que, por lo tanto, gestionan las reservas por teléfono. Son los candidatos ideales para el agente de voz.

### Por qué este enfoque
En España el software de reservas está concentrado en dos actores: **CoverManager** (fusionado con Zenchef, unos 13.000 restaurantes) y **TheFork** (más de 12.000 en España, comprado por American Express). Sus APIs no son de acceso libre (requieren acuerdo de partner o plan Enterprise), y CoverManager ya vende su propio asistente de voz, así que compite directamente. La oportunidad está en la **cola larga**: restaurantes sin sistema, que reservan por teléfono o WhatsApp.

## Qué hace el script (`src/detector_restaurantes.py`)

1. Busca restaurantes con **Google Places API (New) – Text Search** usando las consultas de `QUERIES_POR_DISTRITO` (hoy: Les Corts). Los distritos se **acumulan**: cada ejecución los cubre todos, porque `resultados` se reescribe entera (~24 requests por distrito; cuota diaria de 200). Solo se conservan los locales del distrito configurado (`sublocality_level_1`, no el código postal) y de tipo comida o bebida. Cada consulta devuelve máximo 60 resultados, por eso se divide por barrio y tipo de cocina. Deduplica por Place ID.
2. Descarga la web de cada restaurante (home + hasta 2 páginas que parezcan de reservas) con 10 hilos en paralelo y timeout de 10 s.
3. Busca **firmas** de sistemas de reservas conocidos (`BOOKING_SIGNATURES`) y señales de reserva manual (`MANUAL_SIGNALS`: WhatsApp, teléfono, en español y catalán).
4. Clasifica cada restaurante en un estado: `Usa sistema`, `Sin sistema detectado`, `Reservable en Google (proveedor no identificado)`, `Sin web`, `Solo redes sociales`, `Web no accesible`. Candidatos = `Sin sistema detectado`, `Sin web`, `Solo redes sociales` (vocabulario en `CONTEXT.md`).
5. **Prioriza** por percentiles de número de reseñas calculados **por distrito** sobre los datos de cada ejecución (p75 = Alta, p25 = Media, resto Baja; respaldo fijo 300/100 si hay pocos datos). `Usa sistema` = Descartar. "Reservable en Google" = Baja fija (Reserve with Google implica un partner; revisar tras validar 3–4 casos). Los veredictos de la pestaña `validacion` prevalecen, salvo que el script detecte un sistema (el veredicto queda como desactualizado).
6. **Descubre proveedores faltantes**: cuenta los dominios externos de `<script>` e `<iframe>` de todas las webs (filtrando ruido de `IGNORED_DOMAINS`) y los reporta por frecuencia.
7. **Historial**: guarda una foto diaria (fecha, Place ID, nombre, reseñas, rating) y calcula `Reseñas/mes (historial)` cuando hay al menos 14 días de historia.

### Estado de implementación (2026-09-25)
Puntos 1–7 implementados, junto con los reintentos en Places y la pestaña `validacion`.

### Salidas
Google Sheet con cuatro pestañas:
- `resultados`: un restaurante por fila, ordenado por prioridad.
- `dominios_externos`: dominios de terceros; la columna clave es "En restaurantes sin sistema detectado".
- `historial`: se acumula entre ejecuciones y **nunca se borra**. Es la fuente de verdad del historial.
- `validacion`: se escribe a mano (Place ID, Nombre, Veredicto de una lista cerrada, Nota, Fecha). El script solo la lee; **nunca se borra**. El seguimiento comercial va en otra hoja.

Respaldos locales: `resultados_restaurantes.csv`, `dominios_externos.csv`, `historial_resenas.csv` (ignorados por git).

## Configuración

- Variables en `.env` (cargado con `python-dotenv`): `GOOGLE_PLACES_API_KEY`, `SHEET_ID`, `GOOGLE_SA_FILE=service_account.json`.
- **API key** restringida solo a Places API (New), sin restricción de aplicación. Cuota de `SearchTextRequest per day` limitada a 200.
- **Service account** sin roles de proyecto; tiene acceso a la Sheet porque está compartida con su `client_email` como Editor. Google Sheets API habilitada.
- `.gitignore`: `.venv`, `.env`, `service_account.json`, `*.csv`. **Nunca commitear secretos.**
- Dependencias (`requirements.txt`): `requests`, `gspread`, `python-dotenv`. Entorno virtual en `.venv` (Python 3.12).
- Ejecutar desde la raíz del repo (las rutas de CSV y service account son relativas): `.venv/bin/python src/detector_restaurantes.py`.

## Decisiones tomadas (y por qué)

- **Script en Python, no n8n (por ahora):** paginación, cientos de requests con reintentos y lógica que cambia seguido son más simples en código. n8n entrará cuando la lógica esté validada, para orquestar ejecuciones recurrentes.
- **Google Sheets en vez de Airtable:** más simple y rápido para esta fase.
- **Percentiles en vez de umbrales fijos:** se adaptan a cada zona.
- **Reseñas como proxy de volumen de llamadas:** es imperfecto (locales turísticos sin reservas acumulan muchas; locales antiguos acumulan por años). Por eso se construye el historial.

## Limitaciones conocidas

- Widgets cargados por JavaScript no aparecen en el HTML crudo → posibles falsos "Sin sistema detectado". Hay que validar a mano los de prioridad Alta.
- Algunas firmas de la cola larga (MesaBot, Tablein, QuickSit) no están verificadas.
- Places API (New) solo devuelve 5 reseñas por lugar, ordenadas por relevancia, y el campo `reviews` sube el costo. No sirve para medir reseñas recientes; por eso se usa el historial.
- Los campos `websiteUri`, `nationalPhoneNumber` y `reservable` están en tramos de precio altos de Places. Revisar costos antes de escalar a toda la ciudad.
- Contacto comercial en España: las llamadas y emails comerciales en frío tienen restricciones legales (consentimiento previo). El canal preferido para contactar candidatos es la visita en persona.

## Próximos pasos

1. **Primera ejecución** en Les Corts y revisar el resumen por estado y los umbrales de percentiles que salgan.
2. **Validar a mano** 4–5 restaurantes "Sin sistema detectado" de prioridad Alta (falsos negativos).
3. **Iterar firmas:** revisar `dominios_externos`, identificar proveedores de reservas desconocidos y agregarlos a `BOOKING_SIGNATURES`. Repetir 2–3 veces.
4. **Ejecutar una vez al mes** para acumular historial.
5. Con ~3 meses de historial, **priorizar por reseñas recientes ponderadas**: convertir cada tramo a reseñas/mes y ponderar 0–3 meses × 0,5; 3–6 meses × 0,3; 6–12 meses × 0,15; más de 12 meses × 0,05. Calcular los percentiles sobre ese puntaje en vez del total de reseñas.
6. Más adelante: migrar la ejecución recurrente a **n8n** (cron + Execute Command o endpoint) y evaluar un scraper de reseñas (Apify) solo para candidatos, si hace falta precisión antes de tener historial.
7. Posibles mejoras: usar el tipo de local (bar de tapas vs. restaurante con servicio de mesa) como señal adicional; ampliar a otros barrios.

## Cómo trabajar conmigo

- Escribe en **español latinoamericano/neutro** (ustedes, no vosotros).
- Prefiero soluciones simples; explica el porqué de los parámetros y no inventes umbrales sin decir que son supuestos.
- Antes de cambios grandes, propón el enfoque. Después de cambios, prueba la lógica y resume qué cambió.
- Haz commits pequeños con mensajes claros en español.
