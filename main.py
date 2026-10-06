import os
import re
import feedparser
import resend
from google import genai
from google.genai import errors
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

# 1. Configurar APIs con Variables de Entorno
GEMINI_KEY = os.environ.get("GEMINI_API_KEY")
RESEND_KEY = os.environ.get("RESEND_API_KEY")
DESTINATION_EMAIL = os.environ.get("DESTINATION_EMAIL")

resend.api_key = (RESEND_KEY or "").strip().strip("'\"")
if not resend.api_key.startswith("re_"):
    print("⚠️ RESEND_API_KEY está vacía o no empieza por 're_'. Revisa el secreto en GitHub.")

# Opcional: fijar modelos a mano con una variable de entorno, separados por coma
# (ej: GEMINI_MODELS="gemini-3.6-flash,gemini-3.5-flash")
MODELOS_MANUALES = [
    m.strip() for m in os.environ.get("GEMINI_MODELS", "").split(",") if m.strip()
]

EXCLUIR = ("image", "live", "audio", "tts", "native", "embedding", "robotics", "computer")


def obtener_modelos(client, maximo: int = 4) -> list[str]:
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
        es_lite = "lite" in nombre
        es_preview = "preview" in nombre or "exp" in nombre
        # Más nuevo primero; a igual versión: estable antes que preview, normal antes que lite
        candidatos.append((-v, es_preview, es_lite, nombre))

    candidatos.sort()
    modelos = [c[3] for c in candidatos[:maximo]]
    if not modelos:
        raise RuntimeError("No se encontró ningún modelo flash disponible en la API.")
    print(f"Modelos disponibles a probar: {modelos}")
    return modelos


def es_error_temporal(e: Exception) -> bool:
    """Errores que vale la pena reintentar (saturación, límites, fallos del servidor)."""
    if isinstance(e, errors.APIError):
        return getattr(e, "code", None) in (429, 500, 503, 504)
    return False


@retry(
    retry=retry_if_exception(es_error_temporal),
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=5, min=10, max=60),  # 10s, 20s, 40s...
    reraise=True,
)
def llamar_modelo(client, modelo: str, prompt: str):
    return client.models.generate_content(model=modelo, contents=prompt)


def generar_resumen(client, prompt: str) -> str:
    """Prueba cada modelo (con reintentos); si todos fallan, lanza el último error."""
    ultimo_error = None
    for modelo in obtener_modelos(client):
        try:
            print(f"Intentando con {modelo}...")
            response = llamar_modelo(client, modelo, prompt)
            return response.text
        except errors.APIError as e:
            print(f"{modelo} falló: {e}")
            ultimo_error = e
    raise ultimo_error


def limpiar_html(texto: str) -> str:
    """Gemini a veces envuelve la respuesta en ```html ... ```; lo quitamos."""
    texto = texto.strip()
    texto = re.sub(r"^```(?:html)?\s*", "", texto)
    texto = re.sub(r"\s*```$", "", texto)
    return texto


def enviar_correo(asunto: str, cuerpo_html: str):
    resend.Emails.send({
        "from": "Acme <onboarding@resend.dev>",  # Remitente de prueba de Resend
        "to": [DESTINATION_EMAIL],
        "subject": asunto,
        "html": cuerpo_html,
    })


def main():
    # 2. Leer noticias desde RSS (Google News en español)
    rss_url = "https://news.google.com/rss?hl=es-419&gl=CO&ceid=CO:es-419"
    feed = feedparser.parse(rss_url)

    titulares = [f"- {entry.title}" for entry in feed.entries[:8]]
    if not titulares:
        raise RuntimeError("No se pudieron leer noticias del RSS.")
    texto_noticias = "\n".join(titulares)

    # 3. Resumir con Gemini
    client = genai.Client(api_key=GEMINI_KEY)

    prompt = f"""
Eres un asistente editorial. Analiza estos titulares recientes y genera un boletín breve.
Escribe una introducción amigable y luego presenta 4 o 5 puntos destacados en formato HTML simple (usa etiquetas <p>, <ul>, <li> y <strong> para resaltar lo importante).
Responde solo con el HTML, sin bloques de código markdown.

Noticias:
{texto_noticias}
"""
    resumen_html = limpiar_html(generar_resumen(client, prompt))

    # 4. Enviar correo
    enviar_correo(
        "📰 Tu Resumen Diario de Noticias",
        f"<h2>Resumen de Noticias con IA</h2>{resumen_html}",
    )
    print("¡Correo enviado exitosamente!")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"Error: {e}")
        # Te avisa por correo para que no te enteres tarde
        try:
            enviar_correo(
                "⚠️ Error en tu agente de noticias",
                f"<p>El resumen de hoy falló:</p><pre>{e}</pre>",
            )
        except Exception as e2:
            print(f"Tampoco se pudo enviar el aviso de error: {e2}")
        raise
