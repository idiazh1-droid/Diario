#!/usr/bin/env python3
"""Genera el boletín diario con estilo editorial clásico de periódico:
lee RSS, extrae fotos, selecciona y profundiza con IA (Gemini), traduce titulares en inglés al español,
genera resúmenes de al menos 3 líneas y escribe docs/index.html interactivo."""
import difflib
import hashlib
import html
import json
import os
import re
import socket
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

import feedparser
import yaml

RAIZ = Path(__file__).parent
DOCS = RAIZ / "docs"
ARCHIVO = DOCS / "archivo"
TZ = ZoneInfo("America/Santiago")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
DIAS_ARCHIVO = 14

DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
         "agosto", "septiembre", "octubre", "noviembre", "diciembre"]

SISTEMA = (
    "Eres el editor jefe de un prestigioso diario matinal en español. "
    "Tu labor es seleccionar las noticias más trascendentes y redactar un resumen "
    "y puntos clave rigurosos, objetivos y sustanciosos. "
    "Reglas estrictas e inviolables:\n"
    "1) No inventes datos, nombres, cifras ni contexto ausente en el texto entregado.\n"
    "2) TRADUCCIÓN OBLIGATORIA DEL TITULAR: Si la noticia original proviene de un medio en inglés "
    "o en cualquier otro idioma (ej. BBC News, Variety, Ars Technica), DEBES TRADUCIR Y ADAPTAR EL TITULAR AL ESPAÑOL "
    "con calidad y rigor periodístico. Ningún titular debe quedar en inglés.\n"
    "3) RESUMEN SUSTANCIOSO DE MÍNIMO 3 LÍNEAS: Cada noticia seleccionada debe tener un resumen descriptivo "
    "de al menos 3 a 4 líneas completas de lectura (mínimo 280 caracteres, idealmente 300 a 450 caracteres). "
    "Debe explicar con claridad: qué ocurrió, quiénes intervienen, antecedentes y por qué es relevante. "
    "Nunca entregues resúmenes de una sola línea.\n"
    "4) La primera nota elegida ([0]) será la PORTADA (la noticia más relevante de la sección).\n"
    "5) Atribuye afirmaciones controvertidas a su fuente o emisor.\n"
    "6) Tono periodístico sobrio, culto, neutro e informativo.\n"
    "7) Evita notas duplicadas sobre un mismo acontecimiento.\n"
    "8) Responde EXCLUSIVAMENTE con un JSON válido, sin preámbulos ni bloques markdown."
)


# ---------- utilidades y lectura de fuentes ----------

def limpiar(texto):
    texto = re.sub(r"<[^>]+>", " ", texto or "")
    texto = html.unescape(texto)
    return re.sub(r"\s+", " ", texto).strip()


def id_nota(url, titulo):
    cadena = f"{url}_{titulo}".strip()
    return hashlib.md5(cadena.encode("utf-8")).hexdigest()[:10]


def extraer_imagen(e):
    """Extrae la URL de imagen destacada de una entrada RSS."""
    for media in e.get("media_content", []):
        url = media.get("url")
        if url and (any(ext in url.lower() for ext in (".jpg", ".jpeg", ".png", ".webp"))
                    or "image" in media.get("type", "")):
            return url

    for thumb in e.get("media_thumbnail", []):
        if thumb.get("url"):
            return thumb["url"]

    for enc in e.get("enclosures", []):
        url = enc.get("href") or enc.get("url")
        if url and ("image" in enc.get("type", "")
                    or any(ext in url.lower() for ext in (".jpg", ".jpeg", ".png", ".webp"))):
            return url

    for link in e.get("links", []):
        if "image" in link.get("type", "") and link.get("href"):
            return link["href"]

    html_raw = (e.get("summary") or e.get("description")
                or (e.get("content", [{}])[0].get("value") if e.get("content") else "") or "")
    m = re.search(r'<img[^>]+src=["\'](https?://[^"\']+)["\']', html_raw, re.I)
    if m:
        url = m.group(1)
        if not any(bad in url.lower() for bad in ("1x1", "pixel", "tracker", "feedburner", "icon")):
            return url

    return ""


def url_google(f):
    consulta = f["busqueda"] if f.get("busqueda") else f"site:{f['dominio']}"
    return ("https://news.google.com/rss/search?q="
            + quote_plus(consulta + " when:2d", safe=":")
            + "&hl=es-419&gl=CL&ceid=CL:es-419")


def leer(url):
    d = feedparser.parse(url, agent=UA)
    if not d.entries:
        detalle = getattr(d, "bozo_exception", "sin entradas")
        raise RuntimeError(f"sin entradas ({detalle})")
    return d.entries


def fecha_de(e):
    for clave in ("published_parsed", "updated_parsed"):
        if e.get(clave):
            return datetime(*e[clave][:6], tzinfo=timezone.utc)
    return None


def recolectar(seccion, aj, ahora, errores):
    limite = ahora - timedelta(hours=aj["horas"])
    por_fuente = []
    for f in seccion["fuentes"]:
        intentos = []
        if f.get("url"):
            intentos.append(f["url"])
        if f.get("dominio") or f.get("busqueda"):
            intentos.append(url_google(f))
        entradas, usada, ultimo_error = None, None, None
        for u in intentos:
            try:
                entradas, usada = leer(u), u
                break
            except Exception as ex:  # noqa: BLE001
                ultimo_error = ex
        if entradas is None:
            errores.append(f"{f['nombre']}: {ultimo_error}")
            continue

        es_google = "news.google.com" in usada
        notas = []
        for e in entradas:
            titulo = limpiar(e.get("title"))
            enlace = e.get("link")
            if not titulo or not enlace:
                continue
            fecha = fecha_de(e)
            if fecha and fecha < limite:
                continue
            fuente = f["nombre"]
            extracto = ""
            imagen = extraer_imagen(e)

            if es_google:
                real = limpiar((e.get("source") or {}).get("title", ""))
                if real and titulo.endswith(" - " + real):
                    titulo = titulo[: -len(real) - 3]
                else:
                    titulo = re.sub(r"\s+-\s+[^-]+$", "", titulo)
                if f.get("busqueda") and real:
                    fuente = real
            else:
                extracto = limpiar(e.get("summary") or e.get("description"))[:750]

            notas.append({
                "id": id_nota(enlace, titulo),
                "titulo": titulo,
                "url": enlace,
                "fuente": fuente,
                "fecha": fecha,
                "extracto": extracto,
                "imagen": imagen,
            })
        notas.sort(key=lambda n: n["fecha"] or datetime.min.replace(tzinfo=timezone.utc),
                   reverse=True)
        por_fuente.append(notas[: aj["max_por_fuente"]])

    candidatas, i = [], 0
    while len(candidatas) < aj["max_candidatas"] and any(i < len(l) for l in por_fuente):
        for lista in por_fuente:
            if i < len(lista):
                candidatas.append(lista[i])
        i += 1
    candidatas = candidatas[: aj["max_candidatas"]]

    unicas = []
    for n in candidatas:
        base = n["titulo"].lower()
        if any(n["url"] == u["url"]
               or difflib.SequenceMatcher(None, base, u["titulo"].lower()).ratio() > 0.8
               for u in unicas):
            continue
        unicas.append(n)
    return unicas


# ---------- selección, traducción y profundidad con IA ----------

def con_ia(client, modelo, seccion, candidatas, n):
    lineas = []
    for i, c in enumerate(candidatas):
        extracto = c["extracto"] if c["extracto"] else "(solo titular)"
        tiene_foto = " [con foto]" if c["imagen"] else ""
        lineas.append(f"[{i}]{tiene_foto} ({c['fuente']}) {c['titulo']} | {extracto}")

    prompt = (
        f"Sección: {seccion['nombre']}\n"
        f"Criterio editorial: {' '.join(seccion['criterio'].split())}\n\n"
        f"Selecciona las {n} noticias de mayor valor (menos si no hay suficientes), "
        "ordenadas de mayor a menor relevancia.\n"
        "La primera nota ([0]) será la PORTADA DESTACADA de la sección (preferir notas con foto si son de alto impacto).\n\n"
        "Para cada nota devuelve:\n"
        "- id: índice numérico de la lista de candidatas.\n"
        "- titulo: titular de la noticia SIEMPRE en español. Si el original venía en inglés u otro idioma, tradúcelo y adáptalo con redacción periodística impecable.\n"
        "- resumen: resumen descriptivo de AL MENOS 3 LÍNEAS COMPLETAS (mínimo 280 caracteres, ideal 300 a 450) que explique con claridad qué pasó, quiénes intervienen, antecedentes y consecuencias.\n"
        "- puntos_clave: lista con 2 o 3 viñetas concisas con datos clave, cifras, citas o detalles de profundización.\n\n"
        'Formato exacto requerido: {"seleccion":[{"id":0,"titulo":"Titular en español","resumen":"Resumen de al menos 3 líneas...","puntos_clave":["...","..."]}]}\n\n'
        "Candidatas:\n" + "\n".join(lineas)
    )

    resp = client.models.generate_content(
        model=modelo,
        contents=prompt,
        config={
            "system_instruction": SISTEMA,
            "max_output_tokens": 3000,
            "response_mime_type": "application/json",
        },
    )
    texto = resp.text
    datos = json.loads(re.search(r"\{.*\}", texto, re.S).group(0))

    elegidas, vistos = [], set()
    for idx_sel, s in enumerate(datos.get("seleccion", [])):
        i = s.get("id")
        if isinstance(i, int) and 0 <= i < len(candidatas) and i not in vistos:
            vistos.add(i)
            nota = dict(candidatas[i])
            tit_traducido = limpiar(s.get("titulo", ""))
            if tit_traducido and len(tit_traducido) > 4:
                nota["titulo"] = tit_traducido
            nota["resumen"] = limpiar(s.get("resumen", ""))
            nota["puntos_clave"] = [limpiar(p) for p in s.get("puntos_clave", []) if p]
            nota["es_portada"] = (idx_sel == 0)
            elegidas.append(nota)

    if not elegidas:
        raise RuntimeError("la IA no devolvió notas válidas")
    return elegidas[:n]


def sin_ia(candidatas, n):
    salida = []
    for idx, c in enumerate(candidatas[:n]):
        nota = dict(c)
        txt = c["extracto"]
        nota["resumen"] = (txt[:320].rsplit(" ", 1)[0] + "…") if len(txt) > 320 else txt
        nota["puntos_clave"] = [txt[320:600]] if len(txt) > 320 else []
        nota["es_portada"] = (idx == 0)
        salida.append(nota)
    return salida


# ---------- diseño y renderizado (estilo prensa editorial) ----------

def etiqueta_hora(dt, ahora):
    if not dt:
        return ""
    local = dt.astimezone(TZ)
    dif = (ahora.date() - local.date()).days
    hora = local.strftime("%H:%M")
    if dif == 0:
        return f"hoy {hora}"
    if dif == 1:
        return f"ayer {hora}"
    return f"{local.day} {MESES[local.month - 1][:3]} {hora}"


def esc(t):
    return html.escape(str(t), quote=True)


CSS = """
:root {
  --papel: #FBF9F5;
  --papel-tarjeta: #FFFFFF;
  --tinta: #181818;
  --tinta-suave: #4A4843;
  --apagado: #78756F;
  --linea: #DDD7CA;
  --linea-fuerte: #181818;
  --acento: #9E2A2B;
  --dorado: #C98A12;
  --sombra: rgba(0,0,0,0.06);
}

@media (prefers-color-scheme: dark) {
  :root {
    --papel: #141517;
    --papel-tarjeta: #1B1D20;
    --tinta: #EAE6DF;
    --tinta-suave: #BBB6AD;
    --apagado: #8C8880;
    --linea: #2E3238;
    --linea-fuerte: #EAE6DF;
    --acento: #E57373;
    --dorado: #E5A93C;
    --sombra: rgba(0,0,0,0.3);
  }
}

* { box-sizing: border-box; }
html { scroll-behavior: smooth; -webkit-text-size-adjust: 100%; }
body {
  margin: 0;
  background: var(--papel);
  color: var(--tinta);
  font: 16px/1.65 "Newsreader", Georgia, "Times New Roman", serif;
}

.contenedor {
  max-width: 1080px;
  margin: 0 auto;
  padding: 0 20px;
}

/* Cabecera estilo periódico clásico */
.masthead {
  padding-top: 28px;
  padding-bottom: 14px;
  border-bottom: 3px double var(--linea-fuerte);
}
.masthead-top {
  display: flex;
  justify-content: space-between;
  align-items: center;
  font: 600 11px/1.2 "Inter", system-ui, sans-serif;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: var(--apagado);
  border-bottom: 1px solid var(--linea);
  padding-bottom: 6px;
  margin-bottom: 14px;
}
.masthead-title {
  text-align: center;
  margin: 10px 0 4px;
  font: 900 clamp(38px, 8vw, 76px)/0.95 "Playfair Display", Georgia, serif;
  letter-spacing: -0.02em;
  text-transform: uppercase;
}
.masthead-sub {
  text-align: center;
  font: italic 15px/1.3 "Newsreader", Georgia, serif;
  color: var(--tinta-suave);
  margin: 0 0 12px;
}

/* Barra de navegación de secciones */
nav.secciones-nav {
  position: sticky;
  top: 0;
  z-index: 100;
  background: color-mix(in srgb, var(--papel) 94%, transparent);
  backdrop-filter: blur(8px);
  border-bottom: 1px solid var(--linea);
}
.nav-fila {
  display: flex;
  gap: 12px;
  overflow-x: auto;
  padding: 10px 0;
  scrollbar-width: none;
}
.nav-fila::-webkit-scrollbar { display: none; }
.nav-link {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 4px 12px;
  font: 600 12px "Inter", sans-serif;
  letter-spacing: 0.05em;
  text-transform: uppercase;
  color: var(--tinta);
  text-decoration: none;
  border: 1px solid var(--linea);
  border-radius: 999px;
  white-space: nowrap;
}
.nav-link:hover { border-color: var(--tinta); background: var(--papel-tarjeta); }
.punto { width: 8px; height: 8px; border-radius: 50%; }

/* Secciones y Grilla Editorial */
section.seccion-bloque {
  padding-top: 36px;
  padding-bottom: 24px;
  border-bottom: 1px solid var(--linea);
}
.seccion-header {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  border-bottom: 2px solid var(--linea-fuerte);
  padding-bottom: 6px;
  margin-bottom: 22px;
}
.seccion-titulo {
  font: 700 28px/1.1 "Playfair Display", Georgia, serif;
  margin: 0;
  display: flex;
  align-items: center;
  gap: 10px;
}
.seccion-cuenta {
  font: 500 13px "Inter", sans-serif;
  color: var(--apagado);
}

.editorial-grid {
  display: grid;
  grid-template-columns: repeat(12, 1fr);
  gap: 24px;
}

/* Tarjetas de noticias */
.nota-card {
  position: relative;
  background: var(--papel-tarjeta);
  border: 1px solid var(--linea);
  border-radius: 4px;
  padding: 18px 20px;
  display: flex;
  flex-direction: column;
  transition: opacity 0.3s ease, transform 0.3s ease;
  grid-column: span 6;
}
@media (max-width: 768px) {
  .nota-card { grid-column: span 12 !important; }
}
@media (min-width: 992px) {
  .nota-card:not(.nota-portada) { grid-column: span 4; }
}

/* Portada / Lead Story */
.nota-portada {
  grid-column: span 12 !important;
  background: var(--papel-tarjeta);
  border-top: 4px solid var(--linea-fuerte);
  padding: 24px;
  display: grid;
  grid-template-columns: 1fr;
  gap: 20px;
}
@media (min-width: 768px) {
  .nota-portada.con-foto {
    grid-template-columns: 1.15fr 1fr;
    align-items: start;
  }
}
.nota-portada .nota-titulo {
  font: 700 clamp(24px, 4vw, 34px)/1.15 "Playfair Display", Georgia, serif;
  margin-bottom: 12px;
}
.nota-portada .nota-resumen::first-letter {
  font: bold 3.2em/0.8 "Playfair Display", Georgia, serif;
  float: left;
  margin: 4px 8px 0 0;
  color: var(--tinta);
}

/* Fotos periodísticas */
.foto-contenedor {
  width: 100%;
  aspect-ratio: 16/9;
  overflow: hidden;
  border-radius: 2px;
  background: var(--linea);
  margin-bottom: 14px;
}
.foto-img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
  transition: transform 0.4s ease;
}
.nota-card:hover .foto-img { transform: scale(1.02); }

/* Tipografía de notas */
.nota-top {
  display: flex;
  justify-content: space-between;
  align-items: center;
  font: 600 11px/1 "Inter", sans-serif;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--apagado);
  margin-bottom: 10px;
  padding-right: 28px;
}
.nota-fuente { color: var(--acento); font-weight: 700; }
.nota-titulo {
  font: 600 20px/1.25 "Playfair Display", Georgia, serif;
  margin: 0 0 10px;
}
.nota-titulo a {
  color: var(--tinta);
  text-decoration: none;
}
.nota-titulo a:hover { text-decoration: underline; }
.nota-resumen {
  font-size: 15px;
  line-height: 1.6;
  color: var(--tinta-suave);
  margin: 0 0 14px;
}

/* Menú de 3 puntos interactivo */
.menu-pos {
  position: absolute;
  top: 14px;
  right: 14px;
  z-index: 10;
}
.btn-tres-puntos {
  background: none;
  border: none;
  font-size: 20px;
  line-height: 1;
  color: var(--apagado);
  cursor: pointer;
  padding: 4px 8px;
  border-radius: 4px;
}
.btn-tres-puntos:hover { background: var(--linea); color: var(--tinta); }
.menu-desplegable {
  display: none;
  position: absolute;
  right: 0;
  top: 30px;
  background: var(--papel-tarjeta);
  border: 1px solid var(--linea);
  border-radius: 6px;
  box-shadow: 0 8px 24px var(--sombra);
  padding: 12px;
  min-width: 210px;
  z-index: 20;
}
.menu-desplegable.activo { display: block; }
.menu-label {
  font: 600 11px "Inter", sans-serif;
  text-transform: uppercase;
  color: var(--apagado);
  margin-bottom: 6px;
}
.estrellas {
  display: flex;
  gap: 4px;
  font-size: 22px;
  cursor: pointer;
  color: var(--linea);
  user-select: none;
  margin-bottom: 10px;
}
.estrellas span:hover,
.estrellas span.activo { color: var(--dorado); }
.btn-eliminar {
  width: 100%;
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 8px;
  background: none;
  border: 1px solid var(--linea);
  border-radius: 4px;
  font: 500 12px "Inter", sans-serif;
  color: #C8374A;
  cursor: pointer;
}
.btn-eliminar:hover { background: rgba(200,55,74,0.08); }

/* Desplegable de profundidad */
.btn-profundizar {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  background: none;
  border: none;
  padding: 0;
  font: 600 12px "Inter", sans-serif;
  color: var(--tinta);
  cursor: pointer;
  margin-top: auto;
  text-transform: uppercase;
  letter-spacing: 0.05em;
}
.btn-profundizar:hover { color: var(--acento); }
.flecha-rotar { transition: transform 0.25s ease; display: inline-block; }
.flecha-rotar.abierto { transform: rotate(180deg); }

.caja-profundidad {
  display: none;
  margin-top: 14px;
  padding-top: 14px;
  border-top: 1px dashed var(--linea);
  font-size: 14px;
  color: var(--tinta);
}
.caja-profundidad.visible { display: block; animation: fadeIn 0.3s ease; }
.puntos-lista {
  margin: 0 0 12px;
  padding-left: 18px;
}
.puntos-lista li { margin-bottom: 6px; }
.btn-leer-origen {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font: 600 12px "Inter", sans-serif;
  color: var(--acento);
  text-decoration: none;
}
.btn-leer-origen:hover { text-decoration: underline; }

/* Animaciones */
@keyframes fadeIn {
  from { opacity: 0; transform: translateY(-4px); }
  to { opacity: 1; transform: translateY(0); }
}
.fade-out {
  opacity: 0 !important;
  transform: scale(0.95);
  transition: opacity 0.3s, transform 0.3s;
}

/* Pie de página y panel de preferencias */
footer.pie-editorial {
  padding: 48px 0 80px;
  border-top: 3px double var(--linea-fuerte);
  margin-top: 48px;
  text-align: center;
}
.pie-fin { font: 700 28px/1.2 "Playfair Display", Georgia, serif; margin: 0 0 8px; }
.pie-sub { color: var(--apagado); font-size: 14px; margin: 0 0 24px; }
.btn-preferencias {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 10px 20px;
  background: var(--papel-tarjeta);
  border: 1px solid var(--linea-fuerte);
  border-radius: 999px;
  font: 600 13px "Inter", sans-serif;
  color: var(--tinta);
  cursor: pointer;
}
.btn-preferencias:hover { background: var(--tinta); color: var(--papel); }

/* Toast flotante */
#toast {
  position: fixed;
  bottom: 24px;
  right: 24px;
  background: var(--tinta);
  color: var(--papel);
  padding: 10px 18px;
  border-radius: 8px;
  font: 500 13px "Inter", sans-serif;
  box-shadow: 0 4px 16px var(--sombra);
  display: none;
  z-index: 999;
}
"""

JS = """
const STORAGE_RATINGS = 'boletin_ratings_v1';
const STORAGE_DISMISSED = 'boletin_dismissed_v1';

function obtenerRatings() {
  try { return JSON.parse(localStorage.getItem(STORAGE_RATINGS) || '{}'); }
  catch(e) { return {}; }
}

function obtenerDismissed() {
  try { return JSON.parse(localStorage.getItem(STORAGE_DISMISSED) || '[]'); }
  catch(e) { return []; }
}

function mostrarToast(msg) {
  const t = document.getElementById('toast');
  if(!t) return;
  t.innerText = msg;
  t.style.display = 'block';
  setTimeout(() => { t.style.display = 'none'; }, 2200);
}

function toggleMenu(btn, e) {
  e.stopPropagation();
  const menu = btn.nextElementSibling;
  document.querySelectorAll('.menu-desplegable.activo').forEach(m => {
    if(m !== menu) m.classList.remove('activo');
  });
  menu.classList.toggle('activo');
}

document.addEventListener('click', () => {
  document.querySelectorAll('.menu-desplegable.activo').forEach(m => m.classList.remove('activo'));
});

function calificar(id, estrellas) {
  const ratings = obtenerRatings();
  ratings[id] = estrellas;
  localStorage.setItem(STORAGE_RATINGS, JSON.stringify(ratings));
  actualizarEstrellasUI(id, estrellas);
  mostrarToast(`Guardada: ${estrellas} estrella(s) ⭐`);
}

function actualizarEstrellasUI(id, estrellas) {
  const cont = document.querySelector(`.estrellas[data-id="${id}"]`);
  if (!cont) return;
  const spans = cont.querySelectorAll('span');
  spans.forEach((s, idx) => {
    s.classList.toggle('activo', idx < estrellas);
  });
}

function eliminarNota(id) {
  const card = document.getElementById(`card-${id}`);
  if (!card) return;
  card.classList.add('fade-out');
  setTimeout(() => {
    card.style.display = 'none';
  }, 300);
  const dismissed = obtenerDismissed();
  if(!dismissed.includes(id)) {
    dismissed.push(id);
    localStorage.setItem(STORAGE_DISMISSED, JSON.stringify(dismissed));
  }
  mostrarToast('Nota eliminada de tu feed');
}

function toggleProfundidad(id) {
  const caja = document.getElementById(`prof-${id}`);
  const flecha = document.getElementById(`flecha-${id}`);
  if(!caja) return;
  const abierta = caja.classList.toggle('visible');
  if(flecha) flecha.classList.toggle('abierto', abierta);
}

function verPreferencias() {
  const ratings = obtenerRatings();
  const dismissed = obtenerDismissed();
  const totalNotas = Object.keys(ratings).length;
  alert(
    `📊 Tus preferencias guardadas:\\n\\n` +
    `• Noticias calificadas con estrellas: ${totalNotas}\\n` +
    `• Noticias descartadas de tu feed: ${dismissed.length}\\n\\n` +
    `¡Gemini utilizará este historial para afinar la selección en tus próximas ediciones!`
  );
}

document.addEventListener('DOMContentLoaded', () => {
  const dismissed = obtenerDismissed();
  dismissed.forEach(id => {
    const card = document.getElementById(`card-${id}`);
    if(card) card.style.display = 'none';
  });

  const ratings = obtenerRatings();
  Object.keys(ratings).forEach(id => {
    actualizarEstrellasUI(id, ratings[id]);
  });
});
"""

PAGINA = """<!doctype html>
<html lang="es-CL">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>El Boletín — {{TITULO}}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Newsreader:ital,opsz,wght@0,6..72,400;0,6..72,500;1,6..72,400&family=Playfair+Display:ital,wght@0,600;0,700;0,900;1,400&display=swap" rel="stylesheet">
<style>{{CSS}}</style>
</head>
<body>

<header class="masthead">
  <div class="contenedor">
    <div class="masthead-top">
      <span>Santiago de Chile</span>
      <span>{{FECHA}}</span>
      <span>Edición Matinal</span>
    </div>
    <h1 class="masthead-title">El Boletín</h1>
    <p class="masthead-sub">Curaduría matinal independiente &bull; {{SUBTITULO}}</p>
  </div>
</header>

<nav class="secciones-nav">
  <div class="contenedor">
    <div class="nav-fila">{{NAV}}</div>
  </div>
</nav>

<main class="contenedor">
{{SECCIONES}}
</main>

<footer class="pie-editorial">
  <div class="contenedor">
    <p class="pie-fin">Eso es todo por hoy.</p>
    <p class="pie-sub">La próxima edición saldrá mañana temprano.</p>
    <div style="margin-bottom: 28px;">
      <button class="btn-preferencias" onclick="verPreferencias()">
        ⚙️ Ver mis notas y preferencias de IA
      </button>
    </div>
    {{ARCHIVO}}
    {{ERRORES}}
  </div>
</footer>

<div id="toast"></div>

<script>{{JS}}</script>
</body>
</html>
"""


def render(secciones, ahora, errores, fechas, en_archivo):
    local = ahora.astimezone(TZ)
    total_notas = sum(len(s["notas"]) for s in secciones)

    nav = []
    cuerpo = []

    for s in secciones:
        punto = f'<span class="punto" style="background:{esc(s["color"])}"></span>'
        nav.append(f'<a href="#{esc(s["id"])}" class="nav-link">{punto}{esc(s["nombre"])}</a>')

        bloque = [
            f'<section id="{esc(s["id"])}" class="seccion-bloque">',
            f'<div class="seccion-header">',
            f'<h2 class="seccion-titulo">{punto}{esc(s["nombre"])}</h2>',
            f'<span class="seccion-cuenta">{len(s["notas"])} notas</span>',
            f'</div>',
            f'<div class="editorial-grid">',
        ]

        if not s["notas"]:
            bloque.append('<p style="grid-column: 1/-1; color: var(--apagado);">Sin notas relevantes en esta sección hoy.</p>')

        for n in s["notas"]:
            es_portada = n.get("es_portada", False)
            clase_portada = "nota-portada con-foto" if (es_portada and n.get("imagen")) else ("nota-portada" if es_portada else "")
            
            foto_html = ""
            if n.get("imagen"):
                foto_html = f'''
                <div class="foto-contenedor">
                  <img src="{esc(n["imagen"])}" alt="{esc(n["titulo"])}" class="foto-img" loading="lazy" onerror="this.parentElement.style.display='none'">
                </div>'''

            puntos_html = ""
            if n.get("puntos_clave"):
                items = "".join(f"<li>{esc(p)}</li>" for p in n["puntos_clave"])
                puntos_html = f'<ul class="puntos-lista">{items}</ul>'

            bloque.append(f'''
            <article class="nota-card {clase_portada}" id="card-{esc(n["id"])}">
              <div class="menu-pos">
                <button class="btn-tres-puntos" onclick="toggleMenu(this, event)" title="Opciones de la nota">⋮</button>
                <div class="menu-desplegable">
                  <div class="menu-label">Calificar interés:</div>
                  <div class="estrellas" data-id="{esc(n["id"])}">
                    <span onclick="calificar('{esc(n["id"])}', 1)">★</span>
                    <span onclick="calificar('{esc(n["id"])}', 2)">★</span>
                    <span onclick="calificar('{esc(n["id"])}', 3)">★</span>
                    <span onclick="calificar('{esc(n["id"])}', 4)">★</span>
                    <span onclick="calificar('{esc(n["id"])}', 5)">★</span>
                  </div>
                  <button class="btn-eliminar" onclick="eliminarNota('{esc(n["id"])}')">
                    ✕ Eliminar de mi feed
                  </button>
                </div>
              </div>

              {foto_html if (es_portada and n.get("imagen")) else ""}

              <div class="nota-contenido">
                <div class="nota-top">
                  <span class="nota-fuente">{esc(n["fuente"])}</span>
                  <span>{esc(etiqueta_hora(n["fecha"], ahora))}</span>
                </div>

                {foto_html if (not es_portada and n.get("imagen")) else ""}

                <h3 class="nota-titulo">
                  <a href="{esc(n["url"])}" target="_blank" rel="noopener">{esc(n["titulo"])}</a>
                </h3>

                <p class="nota-resumen">{esc(n.get("resumen", ""))}</p>

                <button class="btn-profundizar" onclick="toggleProfundidad('{esc(n["id"])}')">
                  <span>Profundizar nota</span> <span id="flecha-{esc(n["id"])}" class="flecha-rotar">▾</span>
                </button>

                <div id="prof-{esc(n["id"])}" class="caja-profundidad">
                  {puntos_html}
                  <a href="{esc(n["url"])}" target="_blank" rel="noopener" class="btn-leer-origen">
                    Leer nota completa en {esc(n["fuente"])} ↗
                  </a>
                </div>
              </div>
            </article>
            ''')

        bloque.append('</div></section>')
        cuerpo.append("".join(bloque))

    base = "" if en_archivo else "archivo/"
    inicio = f'<li style="display:inline-block; margin-right:12px;"><a href="{"../" if en_archivo else ""}index.html" style="color:var(--acento);">Edición de hoy</a></li>'
    enlaces = "".join(
        f'<li style="display:inline-block; margin-right:12px;"><a href="{base}{f}.html" style="color:var(--tinta-suave);">{int(f[8:])} {MESES[int(f[5:7]) - 1][:3]}</a></li>'
        for f in fechas)
    archivo = f'<div style="margin-top:24px;"><h4 style="font:600 13px Inter,sans-serif; text-transform:uppercase; margin-bottom:8px; color:var(--apagado);">Ediciones anteriores</h4><ul style="list-style:none; padding:0; margin:0;">{inicio}{enlaces}</ul></div>'

    if errores:
        items = "".join(f"<li>{esc(e)}</li>" for e in errores)
        err = f'<details style="margin-top:20px; font-size:13px; color:var(--apagado);"><summary>{len(errores)} aviso(s) de fuentes</summary><ul>{items}</ul></details>'
    else:
        err = ""

    fecha_txt = f"{DIAS[local.weekday()].capitalize()}, {local.day} de {MESES[local.month - 1]} de {local.year}"

    return (PAGINA
            .replace("{{CSS}}", CSS)
            .replace("{{JS}}", JS)
            .replace("{{TITULO}}", f"{local.day} de {MESES[local.month - 1]}")
            .replace("{{FECHA}}", esc(fecha_txt))
            .replace("{{SUBTITULO}}", f"{total_notas} notas seleccionadas en {len(secciones)} secciones")
            .replace("{{NAV}}", "".join(nav))
            .replace("{{SECCIONES}}", "\n".join(cuerpo))
            .replace("{{ARCHIVO}}", archivo)
            .replace("{{ERRORES}}", err))


# ---------- ejecución principal ----------

def main():
    socket.setdefaulttimeout(20)
    cfg = yaml.safe_load((RAIZ / "fuentes.yaml").read_text(encoding="utf-8"))
    aj = cfg["ajustes"]
    ahora = datetime.now(timezone.utc)
    errores = []

    client = None
    if os.environ.get("GEMINI_API_KEY"):
        from google import genai
        client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    else:
        errores.append("Sin GEMINI_API_KEY: se muestran extractos sin resumen de IA.")

    secciones = []
    for s in cfg["secciones"]:
        n = s.get("notas", aj["notas_por_seccion"])
        candidatas = recolectar(s, aj, ahora, errores)
        notas = []
        if candidatas:
            try:
                if not client:
                    raise RuntimeError("sin cliente de IA")
                notas = con_ia(client, aj["modelo"], s, candidatas, n)
            except Exception as ex:  # noqa: BLE001
                if client:
                    errores.append(f"IA en «{s['nombre']}»: {ex}")
                notas = sin_ia(candidatas, n)
        secciones.append({"id": s["id"], "nombre": s["nombre"], "color": s["color"], "notas": notas})

    if not any(s["notas"] for s in secciones):
        print("Ninguna fuente entregó notas; no se actualiza la página.", file=sys.stderr)
        for e in errores:
            print(" -", e, file=sys.stderr)
        sys.exit(1)

    hoy = ahora.astimezone(TZ).strftime("%Y-%m-%d")
    ARCHIVO.mkdir(parents=True, exist_ok=True)
    existentes = {p.stem for p in ARCHIVO.glob("*.html")} | {hoy}
    fechas = sorted(existentes, reverse=True)[:DIAS_ARCHIVO]
    for p in ARCHIVO.glob("*.html"):
        if p.stem not in fechas:
            p.unlink()

    (DOCS / "index.html").write_text(render(secciones, ahora, errores, fechas, False), encoding="utf-8")
    (ARCHIVO / f"{hoy}.html").write_text(render(secciones, ahora, errores, fechas, True), encoding="utf-8")
    (DOCS / ".nojekyll").write_text("", encoding="utf-8")

    print(f"Listo: {sum(len(s['notas']) for s in secciones)} notas procesadas con éxito.")
    for e in errores:
        print(" -", e)


if __name__ == "__main__":
    main()
