# CineFinder AI

Agente autónomo de investigación cinematográfica: identifica películas a partir de texto e imágenes, contrasta fuentes verificables, persiste fichas en PostgreSQL, mantiene un canal privado de índices en Telegram y expone una API HTTP protegida, con despliegue en Render.

> Este proyecto respeta los derechos de autor. Solo gestiona contenido con autorización de distribución verificable. No elude DRM ni realiza scraping masivo de canales de terceros.

---

## 1. Arquitectura

```
app/
├── main.py                 FastAPI (lifespan, manejo de errores)
├── config.py               Pydantic Settings (.env.local / entorno)
├── api/                    Endpoints v1, auth Bearer, rate limiting
├── ai/                     Cliente NVIDIA NIM, agente, herramientas
├── search/                 TMDB, OMDb, Wikidata, web, YouTube, Internet Archive
├── catalog/                Alta, deduplicación y búsqueda de películas
├── database/               SQLAlchemy 2, 17 tablas, sesiones async
├── delivery/               Entrega de contenido autorizado + SHA-256
├── telegram/               Telethon, reconnect, canal privado
├── workers/                Worker de Telegram y de búsquedas persistentes
└── services/               Investigación y gestión de jobs
scripts/                    Asistentes de credenciales y despliegue
alembic/                    Migraciones
tests/                      Pruebas unitarias y de API
```

**Flujo de una búsqueda:** imágenes/texto → análisis multimodal (NVIDIA) → candidatos → catálogo interno → fuentes externas → verificación → confianza → ficha en PostgreSQL → publicación en el canal privado → respuesta al usuario.

---

## 2. Instalación en Windows

Requiere Python 3.12 y Git.

```powershell
cd D:\Proyectos\"Buscadorautomatico de peliculas"
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Comprueba la instalación:

```powershell
py -3.12 scripts\check_connections.py
py -3.12 -m pytest -q
```

---

## 3. Asistente de credenciales

Los secretos se guardan en `.env.local` (ignorado por git) con permisos restringidos (`icacls` en Windows, `600` en Linux). **El asistente pregunta una credencial a la vez usando `getpass`; los valores nunca se muestran, se registran ni se envían a GitHub.**

```powershell
py -3.12 scripts\setup_credentials.py --key RENDER_API_KEY
py -3.12 scripts\setup_credentials.py --key SUPABASE_DATABASE_URL
py -3.12 scripts\setup_credentials.py --key NVIDIA_API_KEY
py -3.12 scripts\setup_credentials.py --key TELEGRAM_API_ID
py -3.12 scripts\setup_credentials.py --key TELEGRAM_API_HASH
py -3.12 scripts\setup_credentials.py --key TELEGRAM_PHONE
py -3.12 scripts\setup_credentials.py --key TELEGRAM_OWNER_ID
py -3.12 scripts\setup_credentials.py --generate     # secretos automáticos
py -3.12 scripts\setup_credentials.py --status       # estados SIN valores
```

Proveedores opcionales (pueden omitirse): `TMDB_API_KEY`, `TAVILY_API_KEY`, `BRAVE_SEARCH_API_KEY`, `YOUTUBE_DATA_API_KEY`.

`API_ACCESS_TOKEN`, `SESSION_SECRET` y `JOB_ENCRYPTION_KEY` se generan automáticamente. `TELEGRAM_SESSION` se obtiene tras la autenticación local. `RENDER_API_KEY` es **exclusivamente local** y nunca se instala en producción.

---

## 4. Base de datos (Supabase / PostgreSQL)

```powershell
py -3.12 -m alembic upgrade head
py -3.12 scripts\initialize_catalog.py
```

17 tablas: `movies`, `movie_aliases`, `movie_images`, `movie_sources`, `movie_candidates`, `search_history`, `search_jobs`, `agent_tasks`, `agent_usage`, `telegram_channels`, `telegram_messages`, `telegram_catalog_publications`, `authorized_sources`, `media_assets`, `delivery_jobs`, `app_users`, `system_settings`.

Conexión con SSL obligatorio, `asyncpg`, pool con `pre_ping`, transacciones y deduplicación por ID externo o título+año normalizado.

---

## 5. NVIDIA NIM

Modelo: `nvidia/nemotron-3.5-super-vl-120b-a12b`
Endpoint: `https://integrate.api.nvidia.com/v1`

Se usa para análisis multimodal, extracción de texto, generación de candidatos, function calling y contraste de resultados. `scripts/check_connections.py` comprueba que el modelo esté disponible en la cuenta. Si no lo está, cambia `NVIDIA_MODEL` en `.env.local` sin modificar la arquitectura. El consumo de tokens se registra en `agent_usage`.

---

## 6. Telegram (userbot)

Autenticación local interactiva (OTP y 2FA ocultos):

```powershell
py -3.12 scripts\authenticate_telegram.py
py -3.12 scripts\export_session.py
```

`export_session.py` valida la sesión y la escribe como secreto `TELEGRAM_SESSION` en Render, preservando las variables existentes. **No mantengas la misma sesión conectada localmente y en Render a la vez.**

### Comandos

| Comando | Función |
|---|---|
| `!busca [texto]` | Investiga texto, fotos, capturas o álbumes |
| *(enviar una foto o álbum sin comando)* | Inicia la investigación automáticamente |
| `!info CF-000001` | Ficha de una película |
| `!stats` | Estadísticas del catálogo |
| `!ultimas` | Últimas incorporaciones |
| `!autorizar <ID> [usuario]` | Autoriza a otro usuario (solo propietario) |
| `!desautorizar <ID>` | Revoca la autorización (solo propietario) |
| `!enviar <n>` | Entrega el archivo elegido de Telegram (solo fuentes autorizadas) |
| `!ayuda` | Ayuda |
| `!estado` | Estado de servicios |

Durante `!busca` el bot crea **un único mensaje** y lo va actualizando en vivo con el progreso real: pasos del agente, razonamiento, etapas del escaneo y fuentes consultadas. Al terminar, ese mismo mensaje se sustituye por el informe final profesional (confianza, fuentes, alternativas y estado). No se envía un mensaje por herramienta.

Si se encuentran archivos, la lista numerada y los botones aparecen en ese mismo mensaje. Puedes responder **solamente con el número** (`1`, `2`…) o usar `!enviar <n>` / `!enviararchivo <n>` para recibir exactamente el archivo elegido; la confirmación incluye el enlace al mensaje original.

Además, **no necesitas escribir ningún comando para buscar una imagen**: envía una foto o un álbum directamente y el bot inicia la investigación igual que con `!busca`. El texto del pie de la foto se usa como descripción adicional. También puedes autorizar usuarios con `py -3.12 scripts\authorize_user.py <ID> --username nombre`.

#### Búsqueda global en Telegram (solo propietario)

Cuando el propietario lo pide —o hay poca evidencia en el resto de fuentes— el agente ejecuta `search_telegram_all_channels`: recorre **todos los chats y canales accesibles al userbot**, busca el texto y devuelve coincidencias con chat, mensaje, fecha y fragmento. Es una búsqueda **bajo demanda**, sin indexación persistente ni redistribución de contenido de terceros, con límites estrictos (120 diálogos, 20 mensajes por diálogo, 100 resultados). Para forzarla, incluye en la petición algo como: «busca también en todos mis canales de Telegram».

Por API, envía la cabecera `X-Requester-ID` con tu `TELEGRAM_OWNER_ID` para habilitar las herramientas de propietario.

Si la búsqueda encuentra mensajes con archivos, el bot lista hasta 5 opciones con botones (o responde `!enviar 1`, `!enviar 2`, …). **El archivo solo se entrega si el chat está registrado como fuente autorizada**; si no lo está, se devuelve el enlace al mensaje original y se explica el motivo. Para autorizar un canal:

```powershell
py -3.12 scripts\authorize_source.py -1001234567890 --name "Mi canal"
```

Esto evita la distribución automática de copias no autorizadas de contenido comercial.

---

## Búsqueda profunda multichat de Telegram

Motor privado de búsqueda sobre el historial accesible de la cuenta vinculada.

### Comandos

| Comando | Función |
|---|---|
| `!archivos [texto]` | Búsqueda profunda en todos los chats/grupos/canales (admitime una imagen) |
| `!infoarchivo <n>` | Metadatos de un resultado (tamaño, origen, coincidencia) |
| `!enviararchivo <n>` | Reenvía exactamente el archivo elegido |
| `!cancelar` | Cancela la búsqueda en curso |

También se puede usar la API:

```
POST /api/v1/files/search   {"query":"Blender Story","async":true}
GET  /api/v1/files/searches/{search_id}
POST /api/v1/files/searches/{search_id}/cancel
```

### Funcionamiento

- **Descubrimiento:** `iter_dialogs(archived=True/False)` inventaría chats, grupos, supergrupos y canales (incluidos archivados) en `telegram_dialogs`.
- **Historial:** `iter_messages` paginado en lotes de 100 desde `last_scanned_message_id`, con reanudación y límite configurable (`DEEP_SEARCH_MAX_MESSAGES`, `DEEP_SEARCH_MAX_DIALOGS`). Se registran los chats inaccesibles y el motivo (`FloodWait`, `RPCError`).
- **Índice:** `telegram_messages_index` guarda texto, caption, nombre de archivo, tamaño, MIME, duración y resolución **sin descargar archivos**. `indexed_files` y `telegram_media_references` conservan la referencia exacta para recuperar el original.
- **Variantes:** se generan título, sin acentos/puntuación, año, temporada/episodio y alias (`app/search/text_variants.py`). La puntuación 0-100 procede de coincidencias reales, nunca inventada.
- **Imágenes:** dHash de 64 bits para comparar la imagen enviada con las encontradas (`app/search/image_similarity.py`).
- **Progreso:** etapas reales publicadas en `GET /api/v1/jobs/{id}/events` y notificadas por Telegram al terminar.
- **Recuperación:** las búsquedas interrumpidas se reencolan tras 30 minutos; el escaneo es incremental para no repetir historiales ya cubiertos.

### Entrega

`!enviararchivo <n>` usa `forward_messages` con el `dialog_id` y `message_id` exactos del resultado, sin volver a buscar. Solo se entrega si el chat está registrado como fuente autorizada; si no, se devuelve el enlace al mensaje original.

### Resultado verificado

En la cuenta vinculada: **200 conversaciones descubiertas**, **24.297 mensajes indexados**, **15.996 archivos indexados**, 0 duplicados. Una búsqueda real de «Blender Story» devolvió 3 archivos `.rar` con 97 % de coincidencia. Algunos chats pueden ser inaccesibles o tener historial pendiente: el informe lo indica en lugar de afirmar cobertura total.

Solo responde al propietario (`TELEGRAM_OWNER_ID`) y a usuarios autorizados. Deduplica eventos y agrupa álbumes por `grouped_id`.

### Canal privado de índices

`CineFinder AI — Movie Index` se crea automáticamente y su ID se persiste en PostgreSQL. Cada alta publica código `CF-000001`, títulos, año, géneros, sinopsis, cartel, fuentes, fecha, confianza y verificación.

---

## Control de acceso (usuarios autorizados)

El propietario es únicamente la cuenta configurada en `TELEGRAM_OWNER_ID`, con acceso absoluto. No puede ser revocado.

Comandos administrativos (solo propietario, en conversación privada):

| Comando | Función |
|---|---|
| `/autorizar @usuario` | Resuelve el username a Telegram User ID real y lo autoriza |
| `/autorizar` (respondiendo a su mensaje) | Autoriza por el ID real del remitente; es el método más fiable |
| `/desautorizar @usuario` | Revoca el acceso inmediatamente |
| `/autorizados` | Lista usuarios autorizados |
| `/info @usuario` (o `!infoadmin`) | Estado de autorización de un usuario |
| `/id` | Muestra tu Telegram User ID |

- El username se resuelve con Telethon (`get_entity`) y se guarda el **ID numérico** como referencia principal; si el usuario cambia de username, se actualiza al volver a autorizarlo. Escribir un ID numérico a mano **no basta**: se verifica contra MTProto para evitar autorizar identificadores inventados.
- **Recomendado:** responde al mensaje de la persona con `/autorizar` (sin argumentos). El bot toma el `sender_id` real de Telegram, sin depender de lo que escriba el usuario. Con `/id` respondiendo a un mensaje puedes ver el ID real de esa persona.
- La validación se ejecuta en **cada mensaje y cada callback**, no solo al iniciar.
- Los usuarios no autorizados reciben: «⛔ No tienes autorización para utilizar CineFinder AI. Contacta con el administrador.»
- Las sesiones de búsqueda y los resultados se aíslan por usuario (`pending_files:<user_id>`, `pending_media:<user_id>`): nadie puede consultar o recuperar resultados ajenos.
- Los usuarios autorizados pueden buscar, ver resultados y recibir archivos de fuentes autorizadas, pero **no pueden autorizar a otros ni usar comandos administrativos**.
- Si se revoca un permiso, el acceso se pierde en el acto (se marca `is_banned`).

## 7. API HTTP

```powershell
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Todos los endpoints (salvo `/health` y `/ready`) requieren:

```
Authorization: Bearer <API_ACCESS_TOKEN>
```

| Método | Ruta | Descripción |
|---|---|---|
| GET | `/api/v1/health` | Estado básico |
| GET | `/api/v1/ready` | Comprobación de base de datos |
| POST | `/api/v1/search` | JSON o `async: true` → 202 con `job_id` |
| POST | `/api/v1/search/multipart` | Envío de imágenes |
| GET | `/api/v1/jobs/{job_id}` | Estado del trabajo + resultado completo (`result.trace`, `result.formatted_answer`) |
| GET | `/api/v1/jobs/{job_id}/events` | SSE en vivo: pensamiento y herramientas del agente |
| GET | `/api/v1/movies` | Listado (`q`, `skip`, `limit`) |
| GET | `/api/v1/movies/{movie_id}` | Ficha por ID o código CF |
| GET | `/api/v1/stats` | Métricas |
| GET | `/api/v1/search/history` | Historial |
| POST | `/api/v1/admin/reindex` | Recupera trabajos interrumpidos |
| GET | `/api/v1/openapi.json` | Especificación (protegida) |

Ejemplo:

```powershell
curl -X POST http://localhost:8000/api/v1/search `
  -H "Authorization: Bearer $env:API_ACCESS_TOKEN" `
  -H "Content-Type: application/json" `
  -d '{"query":"película de un viaje espacial","async":true}'
```

---

## 8. Despliegue en Render

`render.yaml` define `cinefinder-api` (`pip install -r requirements.txt`, `uvicorn ... --port $PORT`, health check `/api/v1/health`). Las variables secretas usan `sync: false`.

```powershell
py -3.12 scripts\deploy_render.py
```

El script identifica o crea el servicio, inspecciona las variables existentes, las actualiza individualmente, verifica su presencia sin mostrar valores y lanza el deploy. Nunca instala `RENDER_API_KEY`.

**Limitaciones del plan gratuito:** los servicios se suspenden por inactividad y no hay Background Workers gratuitos. Los jobs se persisten en PostgreSQL y se recuperan con `/admin/reindex`. La recepción continua de mensajes requiere un proceso permanentemente activo; Render Free no lo garantiza. No se usan pings artificiales. Para producción, separa el worker de Telegram en un servicio siempre activo.

---

## 9. Seguridad y rotación de secretos

- `.env.local` y `.env.example` están gestionados para que los secretos reales nunca se versionen.
- Los logs redactan claves, tokens, contraseñas y URLs con credenciales.
- No se ejecutan comandos generados por el modelo; el contenido externo se trata como dato.
- Para rotar: actualiza la credencial con `setup_credentials.py --key ...`, vuelve a exportar la sesión si aplica y ejecuta `deploy_render.py`.

---

## 10. Troubleshooting

| Síntoma | Acción |
|---|---|
| `getaddrinfo failed` | Fallo DNS transitorio; vuelve a ejecutar el comando |
| Modelo no disponible | Cambia `NVIDIA_MODEL` tras verificar `/models` |
| Sesión revocada | Repite `authenticate_telegram.py` y `export_session.py` |
| `relation ... does not exist` | Ejecuta `alembic upgrade head` |
| 401 en la API | Revisa `API_ACCESS_TOKEN` en `.env.local` y en Render |
| 429 | Respeta el rate limit (`RATE_LIMIT_PER_MINUTE`) |

---

## 11. Estado verificado

`check_connections.py` valida PostgreSQL/SSL, NVIDIA (modelo disponible), Render y configuración de Telegram. `pytest` cubre configuración, modelos, seguridad, herramientas y endpoints. Ninguna prueba imprime secretos.
