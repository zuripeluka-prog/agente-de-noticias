import json
import os
import re
import smtplib
import ssl
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from urllib.parse import quote_plus

import feedparser
import resend
from google import genai
from google.genai import errors, types
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

# ---------------------------------------------------------------------------
# 1. Configuración (variables de entorno)
# ---------------------------------------------------------------------------
GEMINI_KEY = (os.environ.get("GEMINI_API_KEY") or "").strip().strip("'\"")
if not GEMINI_KEY:
    print("⚠️ GEMINI_API_KEY está vacía: revisa el secreto en GitHub.")
elif not GEMINI_KEY.startswith("AIza"):
    print("⚠️ GEMINI_API_KEY no tiene el formato habitual (empieza por 'AIza'). "
          "Comprueba que no pegaste otra clave en ese secreto.")
RESEND_KEY = (os.environ.get("RESEND_API_KEY") or "").strip().strip("'\"")

# Uno o varios correos separados por coma: "a@gmail.com, b@empresa.com"
DESTINATARIOS = [
    e.strip()
    for e in re.split(r"[,;]", os.environ.get("DESTINATION_EMAIL", ""))
    if e.strip()
]

# Opción para enviar a cualquier persona sin dominio propio: Gmail + contraseña de aplicación
GMAIL_USER = (os.environ.get("GMAIL_USER") or "").strip()
GMAIL_APP_PASSWORD = (os.environ.get("GMAIL_APP_PASSWORD") or "").replace(" ", "").strip()
USAR_GMAIL = bool(GMAIL_USER and GMAIL_APP_PASSWORD)

resend.api_key = RESEND_KEY
if not USAR_GMAIL and not RESEND_KEY.startswith("re_"):
    print("⚠️ RESEND_API_KEY está vacía o no empieza por 're_'. Revisa el secreto en GitHub.")

# Ventana de búsqueda de noticias (ej: 2d, 4d, 7d)
VENTANA = os.environ.get("VENTANA_NOTICIAS", "4d")

# Pausa (segundos) entre un tema y otro, para no pasar el límite de peticiones por minuto
PAUSA_ENTRE_TEMAS = int(os.environ.get("PAUSA_ENTRE_TEMAS", "30"))

# En el nivel gratuito los modelos "lite" tienen límites mucho más amplios: se prueban primero.
# Para priorizar los modelos grandes, define PREFERIR_LITE=0
PREFERIR_LITE = os.environ.get("PREFERIR_LITE", "1") != "0"

# Panel de control (Supabase). La clave de servicio vive SOLO en los Secrets de GitHub.
SUPABASE_URL = (os.environ.get("SUPABASE_URL") or "").strip().rstrip("/")
SUPABASE_SERVICE_KEY = (os.environ.get("SUPABASE_SERVICE_KEY") or "").strip()
# "true" cuando el envío se pide a mano (botón "Enviar ahora" o "Run workflow")
FORZAR = (os.environ.get("FORZAR") or "").strip().lower() == "true"

# Opcional: fijar modelos a mano, separados por coma
MODELOS_MANUALES = [
    m.strip() for m in os.environ.get("GEMINI_MODELS", "").split(",") if m.strip()
]
EXCLUIR = ("image", "live", "audio", "tts", "native", "embedding", "robotics", "computer")

# ---------------------------------------------------------------------------
# 2. Temas del informe
# ---------------------------------------------------------------------------
TEMAS = [
    {
        "titulo": "1. Adquisiciones, inversiones y estrategias de aseguradoras y reaseguradoras de primera línea",
        "enfoque": (
            "Movimientos corporativos (fusiones, adquisiciones, alianzas, inversiones, levantamiento de capital, "
            "cambios de estrategia) de aseguradoras y reaseguradoras de primera línea, es decir, las de alta "
            "calificación financiera ('first-class security'): por ejemplo Munich Re, Swiss Re, Hannover Re, SCOR, "
            "Lloyd's, Chubb, AXA, Allianz, Zurich, Berkshire Hathaway, Everest, RenaissanceRe, entre otras. "
            "Prioriza anuncios concretos con montos, socios o plazos."
        ),
        "queries": [
            ('("Munich Re" OR "Swiss Re" OR "Hannover Re" OR SCOR OR "Lloyd\'s") (acquisition OR acquires OR investment OR strategy)', "en"),
            ("(Chubb OR AXA OR Allianz OR Zurich OR Berkshire OR RenaissanceRe OR Everest) insurance (acquisition OR acquires OR stake OR strategy)", "en"),
            ("insurance reinsurance M&A deal acquisition capital", "en"),
            ("aseguradoras reaseguradoras adquisición inversión estrategia", "es"),
        ],
    },
    {
        "titulo": "2. Eventos climáticos y catastróficos: análisis de las aseguradoras y su impacto en riesgo, seguros y reaseguros",
        "enfoque": (
            "Análisis que hacen aseguradoras y reaseguradoras sobre tendencias climáticas y eventos catastróficos "
            "(huracanes, incendios, inundaciones, terremotos, calor extremo), pérdidas aseguradas, perspectivas "
            "futuras y cómo eso cambia la gestión de riesgo, el diseño y precio de productos de seguros y las "
            "condiciones de reaseguro (renovaciones, bonos catastróficos, capacidad, coberturas)."
        ),
        "queries": [
            ("reinsurers climate change catastrophe outlook risk", "en"),
            ("insured losses natural catastrophes reinsurance cat bond", "en"),
            ("insurers climate risk modelling hurricane wildfire flood underwriting", "en"),
            ("aseguradoras cambio climático catástrofes naturales riesgo reaseguro", "es"),
        ],
    },
    {
        "titulo": "3. Casos prácticos de tecnología en la gestión de riesgos de seguros y reaseguros",
        "enfoque": (
            "Casos concretos y verificables de incorporación de tecnología (inteligencia artificial, modelación "
            "catastrófica, seguros paramétricos, datos satelitales, IoT, automatización de suscripción y siniestros, "
            "analítica avanzada) en la gestión de riesgos de aseguradoras y reaseguradoras. Prioriza casos con "
            "resultados, cifras, metodología o lecciones aplicables a un proyecto."
        ),
        "queries": [
            ("insurers artificial intelligence underwriting claims case study", "en"),
            ("reinsurance technology catastrophe modeling parametric insurance satellite", "en"),
            ("insurtech risk management technology deployment insurer pilot results", "en"),
            ("aseguradoras tecnología inteligencia artificial gestión de riesgos reaseguro caso", "es"),
        ],
    },
]


# ---------------------------------------------------------------------------
# 3. Noticias (Google News RSS)
# ---------------------------------------------------------------------------
def url_rss(consulta: str, idioma: str) -> str:
    q = quote_plus(f"{consulta} when:{VENTANA}")
    if idioma == "en":
        return f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
    return f"https://news.google.com/rss/search?q={q}&hl=es-419&gl=CO&ceid=CO:es-419"


def recolectar_noticias(tema: dict, por_consulta: int = 7, maximo: int = 20) -> list[str]:
    vistos, lineas = set(), []
    for consulta, idioma in tema["queries"]:
        feed = feedparser.parse(url_rss(consulta, idioma))
        for entry in feed.entries[:por_consulta]:
            clave = entry.title.strip().lower()
            if clave in vistos:
                continue
            vistos.add(clave)
            medio = entry.get("source", {}).get("title", "") if entry.get("source") else ""
            fecha = entry.get("published", "")
            lineas.append(f"- {entry.title} | {medio} | {fecha} | {entry.link}")
    return lineas[:maximo]


# ---------------------------------------------------------------------------
# 4. Gemini: modelos, reintentos y fallback
# ---------------------------------------------------------------------------
def obtener_modelos(client, maximo: int = 3) -> list[str]:
    """Pregunta a la API qué modelos 'flash' existen hoy y devuelve los más nuevos."""
    if MODELOS_MANUALES:
        return MODELOS_MANUALES

    candidatos = []
    for m in client.models.list():
        nombre = m.name.replace("models/", "")
        acciones = getattr(m, "supported_actions", None) or []
        if "flash" not in nombre or any(x in nombre for x in EXCLUIR):
            continue
        if acciones and "generateContent" not in acciones:
            continue
        version = re.search(r"gemini-(\d+(?:\.\d+)?)", nombre)
        v = float(version.group(1)) if version else 0.0
        candidatos.append((-v, "preview" in nombre or "exp" in nombre, "lite" in nombre, nombre))

    candidatos.sort()
    lite = [c[3] for c in candidatos if c[2]][:2]
    normales = [c[3] for c in candidatos if not c[2]][:2]
    modelos = (lite + normales) if PREFERIR_LITE else (normales + lite)
    if not modelos:
        raise RuntimeError("No se encontró ningún modelo flash disponible en la API.")
    print(f"Modelos disponibles a probar: {modelos}")
    return modelos


def es_error_temporal(e: Exception) -> bool:
    """Saturación o fallo del servidor (se reintenta). El 429 de cuota se maneja aparte."""
    if isinstance(e, errors.APIError):
        return getattr(e, "code", None) in (500, 503, 504)
    return False


def segundos_de_espera(e: Exception):
    """Si Google indica cuánto esperar tras un 429 (retryDelay), lo extrae."""
    m = re.search(r"retry in (\d+(?:\.\d+)?)s|retryDelay\W+(\d+(?:\.\d+)?)s", str(e), re.I)
    if not m:
        return None
    return float(m.group(1) or m.group(2))


@retry(
    retry=retry_if_exception(es_error_temporal),
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=5, min=10, max=60),  # 10s, 20s, 40s...
    reraise=True,
)
def llamar_modelo(client, modelo: str, prompt: str, con_busqueda: bool):
    config = None
    if con_busqueda:
        config = types.GenerateContentConfig(
            tools=[types.Tool(google_search=types.GoogleSearch())]
        )
    return client.models.generate_content(model=modelo, contents=prompt, config=config)


def generar_texto(client, modelos: list[str], prompt: str) -> str:
    """Prueba cada modelo: primero con búsqueda de Google y, si falla por cuota u otro
    motivo, sin búsqueda. Si el modelo está saturado (503), pasa al siguiente modelo."""
    ultimo_error = None
    for modelo in modelos:
        saturado = False
        for con_busqueda in (True, False):
            for intento in range(2):  # 2.º intento solo tras esperar un 429 corto
                try:
                    print(f"Intentando con {modelo} (búsqueda={'sí' if con_busqueda else 'no'})...")
                    respuesta = llamar_modelo(client, modelo, prompt, con_busqueda)
                    texto = (respuesta.text or "").strip()
                    if not texto:
                        motivo = ""
                        try:
                            cand = respuesta.candidates[0] if respuesta.candidates else None
                            motivo = f"finish_reason={getattr(cand, 'finish_reason', None)}"
                            if getattr(respuesta, "prompt_feedback", None):
                                motivo += f" prompt_feedback={respuesta.prompt_feedback}"
                        except Exception:
                            pass
                        raise RuntimeError(f"Respuesta vacía del modelo ({motivo})")
                    return texto
                except Exception as e:
                    ultimo_error = e
                    print(f"{modelo} falló: {str(e)[:300]}")
                    if isinstance(e, RuntimeError) and intento == 0:
                        time.sleep(5)
                        continue  # respuesta vacía: un segundo intento
                    if getattr(e, "code", None) == 429:
                        espera = segundos_de_espera(e)
                        if intento == 0 and espera is not None and espera <= 60:
                            print(f"Límite por minuto: esperando {espera:.0f}s y reintentando...")
                            time.sleep(espera + 2)
                            continue
                        break  # cuota agotada: probar la siguiente variante
                    if es_error_temporal(e):
                        saturado = True
                    break
            if saturado:
                break
    raise ultimo_error


def limpiar_html(texto: str) -> str:
    texto = texto.strip()
    texto = re.sub(r"^```(?:html)?\s*", "", texto)
    texto = re.sub(r"\s*```$", "", texto)
    return texto


def construir_prompt(tema: dict, noticias: list[str], hoy: str, n: int = 9) -> str:
    lista = "\n".join(noticias) if noticias else "(no se encontraron titulares recientes)"
    return f"""Eres un editor especializado en seguros y reaseguros. Preparas una síntesis informativa: muchas noticias, cada una resumida brevemente y con su enlace, para que el lector escanee rápido y decida cuáles abrir. Fecha de hoy: {hoy}.

TEMA: {tema['titulo']}
ENFOQUE: {tema['enfoque']}

Titulares recientes (titular | medio | fecha | enlace):
{lista}

Instrucciones:
- Elige {n} noticias relevantes para el tema (menos solo si no hay suficientes). Descarta duplicadas y las poco relacionadas con el enfoque.
- Empieza con un <p> de 1 a 2 oraciones que dé el panorama general del tema hoy.
- Luego un <ul> con un <li> por noticia, con este formato exacto:
  <li><strong>Título claro y corto:</strong> resumen de 1 a 2 oraciones con el dato más importante (quién, qué, cifra o fecha si la hay). <a href="URL">Medio</a></li>
- Usa como URL el enlace exacto que aparece en la lista de titulares. No inventes ni modifiques enlaces.
- Ordena de la más a la menos relevante. Sin análisis largos, sin recomendaciones y sin repetir información.
- No inventes cifras ni hechos: basa cada resumen en lo que el titular o la fuente confirman. Si algo no está claro, resúmelo de forma general.
- Responde en español, solo con HTML (p, ul, li, strong, a), sin bloques de código markdown.
"""


# ---------------------------------------------------------------------------
# 5. Envío de correo
# ---------------------------------------------------------------------------
def enviar_por_gmail(asunto: str, html: str, destinatarios: list[str]):
    contexto = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=contexto) as servidor:
        servidor.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        for destino in destinatarios:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = asunto
            msg["From"] = f"Informe Seguros <{GMAIL_USER}>"
            msg["To"] = destino
            msg.attach(MIMEText(re.sub(r"<[^>]+>", "", html), "plain", "utf-8"))
            msg.attach(MIMEText(html, "html", "utf-8"))
            servidor.sendmail(GMAIL_USER, destino, msg.as_string())


def enviar_por_resend(asunto: str, html: str, destinatarios: list[str]):
    # Con un dominio verificado en Resend, define RESEND_FROM (ej: "Informe <informe@tudominio.com>")
    remitente = os.environ.get("RESEND_FROM") or "Informe Seguros <onboarding@resend.dev>"
    for destino in destinatarios:
        resend.Emails.send({
            "from": remitente,
            "to": [destino],  # un correo por persona: nadie ve a los demás
            "subject": asunto,
            "html": html,
        })
        time.sleep(0.7)  # el plan gratuito de Resend limita a 2 peticiones por segundo


def enviar_correo(asunto: str, html: str, destinatarios: list[str] | None = None):
    destinatarios = destinatarios or DESTINATARIOS
    if not destinatarios:
        raise RuntimeError("No hay destinatarios: revisa el secreto DESTINATION_EMAIL.")
    if USAR_GMAIL:
        enviar_por_gmail(asunto, html, destinatarios)
    else:
        enviar_por_resend(asunto, html, destinatarios)


# ---------------------------------------------------------------------------
# 6. Panel de control (Supabase)
# ---------------------------------------------------------------------------
def supabase(metodo: str, ruta: str, cuerpo=None):
    """Llama a la API REST de Supabase con la clave de servicio."""
    headers = {
        "apikey": SUPABASE_SERVICE_KEY,
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }
    if SUPABASE_SERVICE_KEY.startswith("eyJ"):  # clave antigua (JWT)
        headers["Authorization"] = f"Bearer {SUPABASE_SERVICE_KEY}"
    req = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1/{ruta}",
        data=json.dumps(cuerpo).encode() if cuerpo is not None else None,
        method=metodo,
        headers=headers,
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        datos = r.read()
        return json.loads(datos) if datos else None


def cargar_config():
    if not (SUPABASE_URL and SUPABASE_SERVICE_KEY):
        print("Sin Supabase configurado: se usan las variables de entorno.")
        return None
    filas = supabase("GET", "config?id=eq.1&select=*")
    if not filas:
        raise RuntimeError("No existe la fila de configuración (config id=1) en Supabase.")
    return filas[0]


def debe_enviar(cfg: dict) -> bool:
    """Decide si este ciclo toca enviar el informe."""
    if FORZAR:
        print("Envío solicitado manualmente.")
        return True
    if not cfg.get("activo", True):
        print("El envío automático está en pausa.")
        return False

    ahora = datetime.now(ZoneInfo(cfg.get("zona") or "America/Caracas"))
    if cfg.get("ultimo_envio_fecha") == ahora.date().isoformat():
        print("Hoy ya se envió el informe.")
        return False

    ultimo = cfg.get("ultimo_intento")
    if ultimo:
        hace = datetime.now(timezone.utc) - datetime.fromisoformat(ultimo.replace("Z", "+00:00"))
        if hace < timedelta(minutes=60):
            print("Hubo un intento hace menos de 60 minutos; se espera antes de reintentar.")
            return False

    h, m = (int(x) for x in (cfg.get("hora") or "17:00").split(":")[:2])
    programada = ahora.replace(hour=h, minute=m, second=0, microsecond=0)
    pasados = (ahora - programada).total_seconds()
    # Ventana de 3 horas: si GitHub se retrasa o salta un ciclo, el siguiente lo recupera
    if 0 <= pasados < 3 * 3600:
        return True
    print(f"Aún no toca: hora local {ahora:%H:%M}, programado {h:02d}:{m:02d}.")
    return False


def registrar(estado: str, detalle: str, fecha_envio: str | None = None):
    """Guarda el resultado en el historial del panel (si Supabase está configurado)."""
    if not (SUPABASE_URL and SUPABASE_SERVICE_KEY):
        return
    try:
        supabase("POST", "envios", {"estado": estado, "detalle": detalle[:300]})
        if fecha_envio:
            supabase("PATCH", "config?id=eq.1", {"ultimo_envio_fecha": fecha_envio})
    except Exception as e:
        print(f"No se pudo registrar en Supabase: {e}")


# ---------------------------------------------------------------------------
# 7. Programa principal
# ---------------------------------------------------------------------------
def main():
    global DESTINATARIOS
    cfg = cargar_config()
    n = 9
    temas_activos = TEMAS
    fecha_local = datetime.now().date().isoformat()

    if cfg is not None:
        if not debe_enviar(cfg):
            return
        if cfg.get("destinatarios"):
            DESTINATARIOS = cfg["destinatarios"]
        n = int(cfg.get("noticias_por_tema") or 9)
        activos = cfg.get("temas") or {}
        temas_activos = [t for i, t in enumerate(TEMAS) if activos.get(f"t{i + 1}", True)]
        fecha_local = datetime.now(ZoneInfo(cfg.get("zona") or "America/Caracas")).date().isoformat()
        if not FORZAR:
            supabase("PATCH", "config?id=eq.1", {"ultimo_intento": datetime.now(timezone.utc).isoformat()})

    if not temas_activos:
        raise RuntimeError("No hay temas activos: activa al menos uno en el panel.")

    hoy = datetime.now().strftime("%d/%m/%Y")
    client = genai.Client(api_key=GEMINI_KEY)
    modelos = obtener_modelos(client)

    secciones, fallidas = [], 0
    for i, tema in enumerate(temas_activos):
        if i > 0:
            print(f"Pausa de {PAUSA_ENTRE_TEMAS}s antes del siguiente tema...")
            time.sleep(PAUSA_ENTRE_TEMAS)
        print(f"\n=== {tema['titulo']} ===")
        try:
            noticias = recolectar_noticias(tema, maximo=max(12, n * 2))
            print(f"{len(noticias)} titulares recolectados")
            prompt = construir_prompt(tema, noticias, hoy, n)
            html = limpiar_html(generar_texto(client, modelos, prompt))
            secciones.append(f"<h2>{tema['titulo']}</h2>{html}")
        except Exception as e:
            fallidas += 1
            print(f"No se pudo generar el tema: {e}")
            secciones.append(
                f"<h2>{tema['titulo']}</h2><p><em>No se pudo generar esta sección hoy ({e}).</em></p>"
            )

    if fallidas == len(temas_activos):
        raise RuntimeError(
            "No se pudo generar ninguna sección del informe. Si en el log ves 429 / RESOURCE_EXHAUSTED, "
            "se agotó la cuota de Gemini: revisa https://ai.dev/rate-limit, espera a que se renueve "
            "o activa facturación en Google AI Studio."
        )

    cuerpo = (
        '<div style="font-family:Arial,Helvetica,sans-serif;max-width:760px;margin:auto;'
        'line-height:1.55;color:#222">'
        f"<h1>Informe de Seguros y Reaseguros — {hoy}</h1>"
        "<p>Síntesis de las noticias más relevantes de los últimos días sobre seguros y reaseguros. "
        "Cada noticia incluye el enlace a la fuente.</p>"
        + "<hr>".join(secciones)
        + '<hr><p style="font-size:12px;color:#777">Generado automáticamente con IA a partir de '
        "noticias públicas. Los resúmenes son breves: abre el enlace de cada fuente para ver el detalle y verificar las cifras.</p>"
        "</div>"
    )

    enviar_correo(f"📰 Informe de Seguros y Reaseguros — {hoy}", cuerpo, DESTINATARIOS)
    # Un envío manual no marca el día como cumplido: el automático de la hora elegida sigue en pie
    registrar(
        "ok",
        f"{len(DESTINATARIOS)} destinatario(s), {len(temas_activos)} tema(s)"
        + (" · envío manual" if FORZAR else ""),
        None if (FORZAR or cfg is None) else fecha_local,
    )
    print(f"¡Correo enviado a {len(DESTINATARIOS)} destinatario(s)!")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"Error: {e}")
        registrar("error", str(e))
        # Aviso de error solo al primer destinatario (el dueño del agente)
        try:
            enviar_correo(
                "⚠️ Error en tu agente de noticias",
                f"<p>El informe de hoy falló:</p><pre>{e}</pre>",
                DESTINATARIOS[:1],
            )
        except Exception as e2:
            print(f"Tampoco se pudo enviar el aviso de error: {e2}")
        raise
