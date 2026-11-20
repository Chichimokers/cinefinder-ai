#!/usr/bin/env python3
"""Asistente seguro de credenciales para CineFinder AI.

Características:
- Solicita una credencial a la vez usando ``getpass`` (entrada oculta).
- Valida el formato básico sin imprimir nunca el valor introducido.
- Guarda en ``.env.local`` (ignorado por git) con permisos restringidos.
- NUNCA pasa secretos como argumentos de línea de comandos.
- NUNCA escribe secretos en logs, diagnósticos ni archivos versionados.

Uso:
    py -3.12 scripts/setup_credentials.py --key RENDER_API_KEY
    py -3.12 scripts/setup_credentials.py --all
    py -3.12 scripts/setup_credentials.py --generate
    py -3.12 scripts/setup_credentials.py --status
"""

from __future__ import annotations

import argparse
import getpass
import os
import re
import secrets
import stat
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
ENV_LOCAL = ROOT / ".env.local"


# ---------------------------------------------------------------------------
# Especificación de credenciales
# ---------------------------------------------------------------------------

@dataclass
class CredentialSpec:
    name: str
    purpose: str
    secret: bool = True
    required: bool = True
    validator: Optional[Callable[[str], Tuple[bool, str]]] = None


def _v_nonempty(value: str) -> Tuple[bool, str]:
    v = value.strip()
    if not v:
        return False, "No puede estar vacío."
    if len(v) < 12:
        return False, "Demasiado corto (mínimo 12 caracteres)."
    return True, "OK"


def _v_supabase_url(value: str) -> Tuple[bool, str]:
    """Valida la URL sin mostrar la contraseña en ningún mensaje."""
    v = value.strip()
    if not re.match(r"^postgresql(?:\+asyncpg)?://", v):
        return False, "Debe empezar por postgresql:// (o postgresql+asyncpg://)."
    try:
        p = urlparse(v)
    except Exception:
        return False, "URL mal formada."
    if not p.username:
        return False, "Falta el usuario en la URL."
    if not p.password:
        return False, "Falta la contraseña en la URL."
    if not p.hostname:
        return False, "Falta el host en la URL."
    if not p.path or p.path == "/":
        return False, "Falta el nombre de la base de datos."
    return True, "OK"


def _v_telegram_id(value: str) -> Tuple[bool, str]:
    v = value.strip()
    if not v.isdigit():
        return False, "Debe ser exclusivamente numérico."
    if len(v) < 5:
        return False, "Demasiado corto para un ID de Telegram."
    return True, "OK"


def _v_phone(value: str) -> Tuple[bool, str]:
    v = value.strip()
    if not re.match(r"^\+?[0-9][0-9\s\-()]{6,20}$", v):
        return False, "Usa formato internacional, p. ej. +34600111222."
    digits = re.sub(r"\D", "", v)
    if len(digits) < 7 or len(digits) > 15:
        return False, "El número debe tener entre 7 y 15 dígitos."
    return True, "OK"


def _v_hash(value: str) -> Tuple[bool, str]:
    v = value.strip()
    if len(v) < 16:
        return False, "El API Hash suele tener 32 caracteres hexadecimales."
    if not re.match(r"^[0-9a-fA-F]+$", v):
        return False, "Debe contener solo caracteres hexadecimales."
    return True, "OK"


def _v_optional(value: str) -> Tuple[bool, str]:
    return True, "OK"


# Orden obligatorio según el master prompt.
CREDENTIALS: List[CredentialSpec] = [
    CredentialSpec(
        "RENDER_API_KEY",
        "permite crear, configurar y desplegar CineFinder en Render "
        "(uso exclusivo de administración local; NUNCA se instala en producción).",
        validator=_v_nonempty,
    ),
    CredentialSpec(
        "SUPABASE_DATABASE_URL",
        "cadena completa de conexión PostgreSQL de tu proyecto Supabase.",
        validator=_v_supabase_url,
    ),
    CredentialSpec(
        "NVIDIA_API_KEY",
        "API Key de NVIDIA NIM para el agente multimodal y el razonamiento.",
        validator=_v_nonempty,
    ),
    CredentialSpec(
        "TELEGRAM_API_ID",
        "Telegram API ID de tu aplicación de Telegram.",
        secret=False,
        validator=_v_telegram_id,
    ),
    CredentialSpec(
        "TELEGRAM_API_HASH",
        "Telegram API Hash de tu aplicación de Telegram.",
        validator=_v_hash,
    ),
    CredentialSpec(
        "TELEGRAM_PHONE",
        "número telefónico de la cuenta que utilizará CineFinder (formato internacional).",
        secret=False,
        validator=_v_phone,
    ),
    CredentialSpec(
        "TELEGRAM_OWNER_ID",
        "tu identificador numérico de Telegram: único propietario administrativo del sistema.",
        secret=False,
        validator=_v_telegram_id,
    ),
]

OPTIONAL_CREDENTIALS: List[CredentialSpec] = [
    CredentialSpec(
        "TMDB_API_KEY",
        "The Movie Database: metadatos, carteles y búsqueda cinematográfica.",
        validator=_v_optional, required=False,
    ),
    CredentialSpec(
        "TAVILY_API_KEY",
        "Tavily: búsqueda web con motores de IA paracontrastar información.",
        validator=_v_optional, required=False,
    ),
    CredentialSpec(
        "BRAVE_SEARCH_API_KEY",
        "Brave Search: buscador web alternativo para verificar fuentes.",
        validator=_v_optional, required=False,
    ),
    CredentialSpec(
        "YOUTUBE_DATA_API_KEY",
        "YouTube Data API: tráilers y vídeos authorized.",
        validator=_v_optional, required=False,
    ),
]

AUTO_GENERATED = ["API_ACCESS_TOKEN", "SESSION_SECRET", "JOB_ENCRYPTION_KEY"]

ALL_SPECS: Dict[str, CredentialSpec] = {s.name: s for s in CREDENTIALS + OPTIONAL_CREDENTIALS}


# ---------------------------------------------------------------------------
# Persistencia en .env.local
# ---------------------------------------------------------------------------

def read_env() -> Dict[str, str]:
    data: Dict[str, str] = {}
    if not ENV_LOCAL.exists():
        return data
    for raw in ENV_LOCAL.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        data[key] = value
    return data


def write_env(data: Dict[str, str]) -> None:
    """Reescribe .env.local preservando el orden y comentarios existentes."""
    lines: List[str] = []
    seen: set[str] = set()

    if ENV_LOCAL.exists():
        for raw in ENV_LOCAL.read_text(encoding="utf-8").splitlines():
            stripped = raw.strip()
            if stripped and "=" in stripped and not stripped.startswith("#"):
                key = stripped.split("=", 1)[0].strip()
                if key in data:
                    lines.append(f"{key}={data[key]}")
                    seen.add(key)
                    continue
            lines.append(raw)

    for key, value in data.items():
        if key not in seen:
            lines.append(f"{key}={value}")

    ENV_LOCAL.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    restrict_permissions(ENV_LOCAL)


def restrict_permissions(path: Path) -> None:
    """Permisos restrictivos para el usuario actual."""
    try:
        if os.name == "nt":
            # Romper herencia y conceder solo al usuario actual.
            user = os.environ.get("USERNAME", "")
            subprocess.run(
                ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:(F)"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        else:
            path.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 600
    except Exception:
        # No es fatal: el archivo ya está fuera de git.
        pass


# ---------------------------------------------------------------------------
# Entrada de usuario
# ---------------------------------------------------------------------------

def prompt_value(spec: CredentialSpec, existing: Optional[str]) -> Optional[str]:
    """Pregunta una credencial, valida y devuelve el valor (o None si se omite)."""
    print()
    print("=" * 66)
    print(f"Credencial: {spec.name}")
    print(f"Finalidad: {spec.purpose}")
    if existing:
        print("Estado: YA CONFIGURADA (el valor no se muestra).")
    print("=" * 66)

    if existing:
        while True:
            choice = input("¿Mantener la actual o sustituirla? [m/s] (m): ").strip().lower()
            if choice in ("", "m", "mantener"):
                return None
            if choice in ("s", "sustituir", "c", "cambiar"):
                break
            print("Responde 'm' para mantener o 's' para sustituir.")

    if not spec.secret:
        value = input(f"Introduce {spec.name}: ").strip()
    else:
        try:
            value = getpass.getpass(f"Introduce {spec.name} (entrada oculta): ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nNo se pudo leer la entrada oculta en este terminal.")
            print("Ejecuta el script desde un terminal local seguro de Windows.")
            raise SystemExit(2)

    if not value:
        if not spec.required:
            print(f"→ {spec.name} omitida (opcional). El sistema funcionará sin esta fuente.")
            return ""
        print("→ No puede estar vacía. Inténtalo de nuevo.")
        return prompt_value(spec, None)

    if spec.validator is not None:
        ok, msg = spec.validator(value)
        if not ok:
            print(f"→ Formato no válido: {msg}")
            return prompt_value(spec, None)

    print(f"→ {spec.name} configurada correctamente.")
    return value


def ask_yes_no(question: str) -> bool:
    while True:
        r = input(f"{question} [s/n]: ").strip().lower()
        if r in ("s", "si", "sí", "y", "yes"):
            return True
        if r in ("n", "no"):
            return False
        print("Responde 's' o 'n'.")


# ---------------------------------------------------------------------------
# Generación automática de secretos
# ---------------------------------------------------------------------------

def generate_auto() -> None:
    data = read_env()
    changed = []
    for name in AUTO_GENERATED:
        if not data.get(name):
            if name == "JOB_ENCRYPTION_KEY":
                # Clave compatible con cryptography.Fernet (urlsafe base64 de 32 bytes).
                import base64
                data[name] = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
            elif name == "SESSION_SECRET":
                data[name] = secrets.token_hex(32)
            else:
                data[name] = secrets.token_urlsafe(48)
            changed.append(name)
    # Valores por defecto no sensibles.
    defaults = {
        "NVIDIA_MODEL": "nvidia/nemotron-3.5-super-vl-120b-a12b",
        "NVIDIA_BASE_URL": "https://integrate.api.nvidia.com/v1",
        "AGENT_MAX_STEPS": "8",
        "AGENT_MAX_PARALLEL_TOOLS": "4",
        "AGENT_TIMEOUT_SECONDS": "120",
        "AGENT_MAX_CANDIDATES": "10",
        "API_HOST": "0.0.0.0",
        "PORT": "8000",
        "RATE_LIMIT_PER_MINUTE": "60",
    }
    for k, v in defaults.items():
        if not data.get(k):
            data[k] = v
            changed.append(k)

    write_env(data)
    if changed:
        print("Generados/autocompletados (valores no mostrados):")
        for name in changed:
            print(f"  - {name}")
    else:
        print("Los valores automáticos ya estaban presentes.")


# ---------------------------------------------------------------------------
# Comandos
# ---------------------------------------------------------------------------

def configure_one(name: str) -> None:
    spec = ALL_SPECS.get(name) or next(
        (s for s in CREDENTIALS + OPTIONAL_CREDENTIALS if s.name == name), None
    )
    if spec is None:
        # Permite configurar también los generados manualmente si se desea.
        spec = CredentialSpec(name, "valor de configuración.", secret=True, validator=None)
    data = read_env()
    value = prompt_value(spec, data.get(name))
    if value is None:
        print(f"→ {name} sin cambios.")
        return
    data[name] = value
    write_env(data)
    print(f"→ Guardado en {ENV_LOCAL.name}.")


def configure_all() -> None:
    data = read_env()
    for spec in CREDENTIALS:
        value = prompt_value(spec, data.get(spec.name))
        if value is not None:
            data[spec.name] = value
            write_env(data)

    print()
    if ask_yes_no("¿Deseas configurar proveedores opcionales (TMDB, Tavily, Brave, YouTube)?"):
        for spec in OPTIONAL_CREDENTIALS:
            value = prompt_value(spec, data.get(spec.name))
            if value is not None:
                data[spec.name] = value
                write_env(data)

    generate_auto()
    print()
    print("Configuración completada. Ejecuta '--status' para comprobar el estado.")


def show_status() -> None:
    data = read_env()
    print("Estado de credenciales (los valores NUNCA se muestran):")
    print("-" * 50)
    for spec in CREDENTIALS + OPTIONAL_CREDENTIALS + [
        CredentialSpec(n, "", secret=True, required=False) for n in AUTO_GENERATED
    ]:
        state = "CONFIGURADA" if data.get(spec.name) else "PENDIENTE"
        req = "obligatoria" if spec.required else "opcional"
        print(f"  {state:<12} {spec.name:<28} ({req})")


def main() -> int:
    parser = argparse.ArgumentParser(description="Asistente de credenciales de CineFinder AI")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--key", metavar="NOMBRE", help="Configura una sola credencial")
    group.add_argument("--all", action="store_true", help="Configura todas las credenciales")
    group.add_argument("--generate", action="store_true", help="Genera secretos automáticos")
    group.add_argument("--status", action="store_true", help="Muestra el estado (sin valores)")
    args = parser.parse_args()

    if args.status:
        show_status()
    elif args.generate:
        generate_auto()
    elif args.key:
        configure_one(args.key)
    elif args.all:
        configure_all()
    else:
        # Por defecto, configurar una sola recomendada para evitar formularios gigantes.
        parser.print_help()
        print("\nSugerencia: usa --key NOMBRE para configurar credenciales de una en una.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
