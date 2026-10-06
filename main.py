import os
import feedparser
import resend
from google import genai

# 1. Configurar APIs con Variables de Entorno
GEMINI_KEY = os.environ.get("AQ.Ab8RN6LXZ-yC3T5X_iZn6cE7cVwSc_ERYsEcPUvSY3McU7mIzQ")
RESEND_KEY = os.environ.get("re_H1zVj8F7_A74Avx9BU7QdVLJrnBHEWH1J")
DESTINATION_EMAIL = os.environ.get("alezulu1972@gmail.com")

# 2. Leer noticias desde RSS (Google News en español)
rss_url = "https://news.google.com/rss?hl=es-419&gl=CO&ceid=CO:es-419"
feed = feedparser.parse(rss_url)

# Tomar los 8 titulares más recientes
titulares = [f"- {entry.title}" for entry in feed.entries[:8]]
texto_noticias = "\n".join(titulares)

# 3. Resumir las noticias con Google Gemini
client = genai.Client(api_key=GEMINI_KEY)

prompt = f"""
Eres un asistente editorial. Analiza estos titulares recientes y genera un boletín breve.
Escribe una introducción amigable y luego presenta 4 o 5 puntos destacados en formato HTML simple (usa etiquetas <p>, <ul>, <li> y <strong> para resaltar lo importante).

Noticias:
{texto_noticias}
"""

response = client.models.generate_content(
    model="gemini-2.5-flash",
    contents=prompt
)
resumen_html = response.text

# 4. Enviar correo usando Resend
resend.api_key = RESEND_KEY

params = {
    "from": "Acme <onboarding@resend.dev>",  # Remitente de prueba por defecto en Resend
    "to": [DESTINATION_EMAIL],
    "subject": "📰 Tu Resumen Diario de Noticias",
    "html": f"<h2>Resumen de Noticias con IA</h2>{resumen_html}"
}

resend.Emails.send(params)
print("¡Correo enviado exitosamente!")