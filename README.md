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
| `!info CF-000001` | Ficha de una película |
| `!stats` | Estadísticas del catálogo |
| `!ultimas` | Últimas incorporaciones |
| `!autorizar <ID> [usuario]` | Autoriza a otro usuario (solo propietario) |
| `!desautorizar <ID>` | Revoca la autorización (solo propietario) |
| `!ayuda` | Ayuda |
| `!estado` | Estado de servicios |

Durante `!busca` el bot muestra en vivo su razonamiento, cada herramienta ejecutada y un resumen de resultados, y finalmente una respuesta profesional con confianza, fuentes, alternativas y estado de verificación. También puedes autorizar usuarios con `py -3.12 scripts\authorize_user.py <ID> --username nombre`.

#### Búsqueda global en Telegram (solo propietario)

Cuando el propietario lo pide —o hay poca evidencia en el resto de fuentes— el agente ejecuta `search_telegram_all_channels`: recorre **todos los chats y canales accesibles al userbot**, busca el texto y devuelve coincidencias con chat, mensaje, fecha y fragmento. Es una búsqueda **bajo demanda**, sin indexación persistente ni redistribución de contenido de terceros, con límites estrictos (120 diálogos, 20 mensajes por diálogo, 100 resultados). Para forzarla, incluye en la petición algo como: «busca también en todos mis canales de Telegram».

Por API, envía la cabecera `X-Requester-ID` con tu `TELEGRAM_OWNER_ID` para habilitar las herramientas de propietario.

Solo responde al propietario (`TELEGRAM_OWNER_ID`) y a usuarios autorizados. Deduplica eventos y agrupa álbumes por `grouped_id`.

### Canal privado de índices

`CineFinder AI — Movie Index` se crea automáticamente y su ID se persiste en PostgreSQL. Cada alta publica código `CF-000001`, títulos, año, géneros, sinopsis, cartel, fuentes, fecha, confianza y verificación.

---

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
