import os
import re
import smtplib
import ssl
import time
from datetime import datetime
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
GEMINI_KEY = os.environ.get("GEMINI_API_KEY")
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
            "Explica qué buscan estratégicamente y qué señales dan al mercado."
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


def recolectar_noticias(tema: dict, por_consulta: int = 5, maximo: int = 16) -> list[str]:
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
    modelos = [c[3] for c in candidatos[:maximo]]
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
                        raise RuntimeError("Respuesta vacía del modelo")
                    return texto
                except Exception as e:
                    ultimo_error = e
                    print(f"{modelo} falló: {str(e)[:300]}")
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


def construir_prompt(tema: dict, noticias: list[str], hoy: str) -> str:
    lista = "\n".join(noticias) if noticias else "(no se encontraron titulares recientes)"
    return f"""Eres un analista senior de riesgos, seguros y reaseguros. Preparas un informe para una persona que lo usará como insumo de un proyecto, así que necesita profundidad, datos concretos y aplicabilidad. Fecha de hoy: {hoy}.

TEMA: {tema['titulo']}
ENFOQUE: {tema['enfoque']}

Titulares recientes (titular | medio | fecha | enlace):
{lista}

Instrucciones:
- Selecciona las 3 a 5 noticias o casos más relevantes. Puedes usar la búsqueda de Google para ampliar cada uno con hechos verificables: cifras, montos, fechas, partes involucradas, metodología.
- Para cada una escribe en HTML:
  <h3> con un título descriptivo, y un <ul> con estos <li>, cada uno de 2 a 4 oraciones:
  <strong>Qué pasó:</strong> contexto y hechos.
  <strong>Datos clave:</strong> cifras, montos, plazos, calificaciones.
  <strong>Por qué importa:</strong> impacto en gestión de riesgo, productos de seguros y/o reaseguros.
  <strong>Aplicación al proyecto:</strong> lecciones, ideas o preguntas concretas que se pueden aprovechar.
  <strong>Fuente:</strong> enlace <a href="..."> con el nombre del medio.
- Cierra con <h3>Conclusiones y tendencias</h3> y 3 a 5 viñetas que conecten las noticias entre sí y señalen hacia dónde va el tema.
- No inventes cifras ni hechos. Si algo no está confirmado, escribe "no confirmado". Si no hay noticias relevantes, dilo con honestidad en un <p>.
- Responde en español, solo con HTML (h3, p, ul, li, strong, a), sin bloques de código markdown.
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
    resend.Emails.send({
        "from": "Informe Seguros <onboarding@resend.dev>",
        "to": destinatarios,
        "subject": asunto,
        "html": html,
    })


def enviar_correo(asunto: str, html: str, destinatarios: list[str] | None = None):
    destinatarios = destinatarios or DESTINATARIOS
    if not destinatarios:
        raise RuntimeError("No hay destinatarios: revisa el secreto DESTINATION_EMAIL.")
    if USAR_GMAIL:
        enviar_por_gmail(asunto, html, destinatarios)
    else:
        enviar_por_resend(asunto, html, destinatarios)


# ---------------------------------------------------------------------------
# 6. Programa principal
# ---------------------------------------------------------------------------
def main():
    hoy = datetime.now().strftime("%d/%m/%Y")
    client = genai.Client(api_key=GEMINI_KEY)
    modelos = obtener_modelos(client)

    secciones, fallidas = [], 0
    for i, tema in enumerate(TEMAS):
        if i > 0:
            print(f"Pausa de {PAUSA_ENTRE_TEMAS}s antes del siguiente tema...")
            time.sleep(PAUSA_ENTRE_TEMAS)
        print(f"\n=== {tema['titulo']} ===")
        try:
            noticias = recolectar_noticias(tema)
            print(f"{len(noticias)} titulares recolectados")
            prompt = construir_prompt(tema, noticias, hoy)
            html = limpiar_html(generar_texto(client, modelos, prompt))
            secciones.append(f"<h2>{tema['titulo']}</h2>{html}")
        except Exception as e:
            fallidas += 1
            print(f"No se pudo generar el tema: {e}")
            secciones.append(
                f"<h2>{tema['titulo']}</h2><p><em>No se pudo generar esta sección hoy ({e}).</em></p>"
            )

    if fallidas == len(TEMAS):
        raise RuntimeError(
            "No se pudo generar ninguna sección del informe. Si en el log ves 429 / RESOURCE_EXHAUSTED, "
            "se agotó la cuota de Gemini: revisa https://ai.dev/rate-limit, espera a que se renueve "
            "o activa facturación en Google AI Studio."
        )

    cuerpo = (
        '<div style="font-family:Arial,Helvetica,sans-serif;max-width:760px;margin:auto;'
        'line-height:1.55;color:#222">'
        f"<h1>Informe de Seguros y Reaseguros — {hoy}</h1>"
        "<p>Resumen analítico de las noticias más relevantes de los últimos días en tres frentes: "
        "movimientos estratégicos del sector, riesgo climático y catastrófico, y tecnología aplicada "
        "a la gestión de riesgos.</p>"
        + "<hr>".join(secciones)
        + '<hr><p style="font-size:12px;color:#777">Generado automáticamente con IA a partir de '
        "noticias públicas. Verifica las cifras en las fuentes antes de usarlas en documentos formales.</p>"
        "</div>"
    )

    enviar_correo(f"📰 Informe de Seguros y Reaseguros — {hoy}", cuerpo)
    print(f"¡Correo enviado a {len(DESTINATARIOS)} destinatario(s)!")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"Error: {e}")
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
