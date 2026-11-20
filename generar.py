#!/usr/bin/env python3
"""Genera el boletín diario: lee RSS, selecciona y resume con IA, escribe docs/index.html."""
import difflib
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
UA = "Mozilla/5.0 (compatible; MiBoletin/1.0)"
DIAS_ARCHIVO = 14

DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
         "agosto", "septiembre", "octubre", "noviembre", "diciembre"]

SISTEMA = (
    "Eres el editor de un boletín matinal personal, en español de Chile. "
    "Trabajas solo con el texto que se te entrega. Reglas: "
    "1) No inventes datos, cifras, nombres ni contexto que no estén en el texto. "
    "2) Si de una nota solo hay titular y no hay extracto, deja su resumen vacío (\"\"). "
    "3) Atribuye las afirmaciones controvertidas a quien las hace o a la fuente. "
    "4) Tono neutro y sobrio, sin adjetivos valorativos. "
    "5) Si varias fuentes cubren el mismo hecho, elige solo la mejor nota. "
    "6) Responde únicamente con JSON válido, sin texto adicional."
)


# ---------- lectura de fuentes ----------

def limpiar(texto):
    texto = re.sub(r"<[^>]+>", " ", texto or "")
    texto = html.unescape(texto)
    return re.sub(r"\s+", " ", texto).strip()


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
            if es_google:
                real = limpiar((e.get("source") or {}).get("title", ""))
                if real and titulo.endswith(" - " + real):
                    titulo = titulo[: -len(real) - 3]
                else:
                    titulo = re.sub(r"\s+-\s+[^-]+$", "", titulo)
                if f.get("busqueda") and real:
                    fuente = real
            else:
                extracto = limpiar(e.get("summary") or e.get("description"))[:500]
            notas.append({"titulo": titulo, "url": enlace, "fuente": fuente,
                          "fecha": fecha, "extracto": extracto})
        notas.sort(key=lambda n: n["fecha"] or datetime.min.replace(tzinfo=timezone.utc),
                   reverse=True)
        por_fuente.append(notas[: aj["max_por_fuente"]])

    # intercalar fuentes para que ninguna domine
    candidatas, i = [], 0
    while len(candidatas) < aj["max_candidatas"] and any(i < len(l) for l in por_fuente):
        for lista in por_fuente:
            if i < len(lista):
                candidatas.append(lista[i])
        i += 1
    candidatas = candidatas[: aj["max_candidatas"]]

    # quitar duplicados por enlace o título muy parecido
    unicas = []
    for n in candidatas:
        base = n["titulo"].lower()
        if any(n["url"] == u["url"]
               or difflib.SequenceMatcher(None, base, u["titulo"].lower()).ratio() > 0.8
               for u in unicas):
            continue
        unicas.append(n)
    return unicas


# ---------- selección y resumen con IA ----------

def con_ia(client, modelo, seccion, candidatas, n):
    lineas = []
    for i, c in enumerate(candidatas):
        extracto = c["extracto"] if c["extracto"] else "(solo titular)"
        lineas.append(f"[{i}] ({c['fuente']}) {c['titulo']} | {extracto}")
    prompt = (
        f"Sección: {seccion['nombre']}\n"
        f"Criterio de relevancia: {' '.join(seccion['criterio'].split())}\n\n"
        f"Elige las {n} notas más relevantes (menos si no hay tantas que valgan la pena), "
        "ordenadas de mayor a menor importancia. Para cada una escribe un resumen de 1 o 2 "
        "frases (máximo 220 caracteres) en español, aunque la nota original esté en otro idioma.\n"
        'Formato exacto: {"seleccion":[{"id":0,"resumen":"..."}]}\n\n'
        "Notas:\n" + "\n".join(lineas)
    )
    resp = client.models.generate_content(
        model=modelo,
        contents=prompt,
        config={
            "system_instruction": SISTEMA,
            "max_output_tokens": 2000,
            "response_mime_type": "application/json",
        },
    )
    texto = resp.text
    datos = json.loads(re.search(r"\{.*\}", texto, re.S).group(0))
    elegidas, vistos = [], set()
    for s in datos["seleccion"]:
        i = s.get("id")
        if isinstance(i, int) and 0 <= i < len(candidatas) and i not in vistos:
            vistos.add(i)
            nota = dict(candidatas[i])
            nota["resumen"] = limpiar(s.get("resumen", ""))
            elegidas.append(nota)
    if not elegidas:
        raise RuntimeError("la IA no devolvió notas válidas")
    return elegidas[:n]


def sin_ia(candidatas, n):
    salida = []
    for c in candidatas[:n]:
        nota = dict(c)
        nota["resumen"] = c["extracto"][:220].rsplit(" ", 1)[0] + "…" if len(c["extracto"]) > 220 else c["extracto"]
        salida.append(nota)
    return salida


# ---------- página ----------

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
    return html.escape(t, quote=True)


CSS = """
:root{--fondo:#E9EDF0;--tinta:#16212B;--apagado:#566472;--linea:#C5CED6;--acento:#2640C8;--visitado:#6B5B95}
@media (prefers-color-scheme:dark){:root{--fondo:#10171D;--tinta:#E6EBEF;--apagado:#93A0AC;--linea:#27333D;--acento:#8EA2FF;--visitado:#B7A6E0}}
*{box-sizing:border-box}
html{scroll-behavior:smooth;-webkit-text-size-adjust:100%}
@media (prefers-reduced-motion:reduce){html{scroll-behavior:auto}}
body{margin:0;background:var(--fondo);color:var(--tinta);font:16px/1.5 "Public Sans",system-ui,-apple-system,"Segoe UI",sans-serif}
main,header.cab,footer{max-width:640px;margin:0 auto;padding-left:20px;padding-right:20px}
header.cab{padding-top:36px;padding-bottom:18px}
h1{font:600 clamp(32px,9vw,46px)/1.05 "Newsreader",Georgia,"Times New Roman",serif;letter-spacing:-.01em;margin:0 0 10px}
.sub{margin:0;color:var(--apagado);font-size:14px}
nav{position:sticky;top:0;z-index:5;background:color-mix(in srgb,var(--fondo) 92%,transparent);backdrop-filter:blur(8px);border-bottom:1px solid var(--linea)}
nav .fila{max-width:640px;margin:0 auto;padding:10px 20px;display:flex;gap:8px;overflow-x:auto;scrollbar-width:none}
nav .fila::-webkit-scrollbar{display:none}
nav a{flex:none;display:flex;align-items:center;gap:7px;padding:6px 12px;border:1px solid var(--linea);border-radius:999px;color:var(--tinta);text-decoration:none;font-size:14px;white-space:nowrap}
.punto{width:9px;height:9px;border-radius:50%;display:inline-block;flex:none}
section{padding-top:34px;scroll-margin-top:56px}
h2{display:flex;align-items:center;gap:10px;margin:0 0 6px;font:600 25px/1.2 "Newsreader",Georgia,serif}
h2 .cuenta{margin-left:auto;font:400 14px "Public Sans",system-ui,sans-serif;color:var(--apagado)}
.nota{padding:18px 0;border-top:1px solid var(--linea)}
.nota h3{margin:0 0 6px;font:500 20px/1.28 "Newsreader",Georgia,serif}
.nota h3 a{color:var(--tinta);text-decoration:none}
.nota h3 a:visited{color:var(--visitado)}
.nota h3 a:hover{text-decoration:underline;text-decoration-color:var(--acento)}
a:focus-visible{outline:3px solid var(--acento);outline-offset:3px;border-radius:4px}
.res{margin:0 0 8px;color:var(--tinta);opacity:.86;font-size:15px}
.meta{margin:0;display:flex;gap:14px;font-size:13px;color:var(--apagado)}
.meta .fuente{font-weight:600;color:var(--tinta);opacity:.8}
.vacio{padding:18px 0;border-top:1px solid var(--linea);color:var(--apagado);font-size:15px}
footer{padding-top:48px;padding-bottom:64px}
.fin{font:600 30px/1.15 "Newsreader",Georgia,serif;margin:0 0 6px}
.fin-sub{margin:0 0 28px;color:var(--apagado);font-size:14px}
footer h4{margin:22px 0 8px;font-size:14px;font-weight:600}
footer ul{list-style:none;padding:0;margin:0;display:flex;flex-wrap:wrap;gap:6px 14px;font-size:14px}
footer a{color:var(--acento)}
details{margin-top:22px;font-size:13px;color:var(--apagado)}
details summary{cursor:pointer}
details li{display:block;margin-top:4px}
"""

PAGINA = """<!doctype html>
<html lang="es-CL">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#E9EDF0" media="(prefers-color-scheme:light)">
<meta name="theme-color" content="#10171D" media="(prefers-color-scheme:dark)">
<title>Boletín del {{TITULO}}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Newsreader:opsz,wght@6..72,500;6..72,600&family=Public+Sans:wght@400;600&display=swap" rel="stylesheet">
<style>{{CSS}}</style>
</head>
<body>
<header class="cab">
<h1>{{FECHA}}</h1>
<p class="sub">{{SUBTITULO}}</p>
</header>
<nav><div class="fila">{{NAV}}</div></nav>
<main>
{{SECCIONES}}
</main>
<footer>
<p class="fin">Eso es todo por hoy.</p>
<p class="fin-sub">La próxima edición sale mañana temprano.</p>
{{ARCHIVO}}
{{ERRORES}}
</footer>
</body>
</html>
"""


def render(secciones, ahora, errores, fechas, en_archivo):
    local = ahora.astimezone(TZ)
    total = sum(len(s["notas"]) for s in secciones)
    nav, cuerpo = [], []
    for s in secciones:
        punto = f'<span class="punto" style="background:{esc(s["color"])}"></span>'
        nav.append(f'<a href="#{esc(s["id"])}">{punto}{esc(s["nombre"])}</a>')
        partes = [f'<section id="{esc(s["id"])}"><h2>{punto}{esc(s["nombre"])}'
                  f'<span class="cuenta">{len(s["notas"])}</span></h2>']
        if not s["notas"]:
            partes.append('<p class="vacio">Sin notas relevantes en las últimas horas.</p>')
        for n in s["notas"]:
            partes.append('<article class="nota">'
                          f'<h3><a href="{esc(n["url"])}" rel="noopener">{esc(n["titulo"])}</a></h3>')
            if n.get("resumen"):
                partes.append(f'<p class="res">{esc(n["resumen"])}</p>')
            partes.append('<p class="meta">'
                          f'<span class="fuente">{esc(n["fuente"])}</span>'
                          f'<span>{esc(etiqueta_hora(n["fecha"], ahora))}</span></p></article>')
        partes.append("</section>")
        cuerpo.append("".join(partes))

    base = "" if en_archivo else "archivo/"
    inicio = f'<li><a href="{"../" if en_archivo else ""}index.html">Edición de hoy</a></li>'
    enlaces = "".join(
        f'<li><a href="{base}{f}.html">{int(f[8:])} {MESES[int(f[5:7]) - 1][:3]}</a></li>'
        for f in fechas)
    archivo = f"<h4>Ediciones anteriores</h4><ul>{inicio}{enlaces}</ul>"

    if errores:
        items = "".join(f"<li>{esc(e)}</li>" for e in errores)
        err = f"<details><summary>{len(errores)} fuente(s) con problemas</summary><ul>{items}</ul></details>"
    else:
        err = ""

    fecha_txt = f"{DIAS[local.weekday()]} {local.day} de {MESES[local.month - 1]}"
    return (PAGINA
            .replace("{{CSS}}", CSS)
            .replace("{{TITULO}}", f"{local.day} de {MESES[local.month - 1]}")
            .replace("{{FECHA}}", esc(fecha_txt))
            .replace("{{SUBTITULO}}",
                     f"Actualizado a las {local.strftime('%H:%M')}. "
                     f"{total} notas en {len(secciones)} secciones.")
            .replace("{{NAV}}", "".join(nav))
            .replace("{{SECCIONES}}", "\n".join(cuerpo))
            .replace("{{ARCHIVO}}", archivo)
            .replace("{{ERRORES}}", err))


# ---------- ejecución ----------

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

    print(f"Listo: {sum(len(s['notas']) for s in secciones)} notas, {len(errores)} aviso(s).")
    for e in errores:
        print(" -", e)


if __name__ == "__main__":
    main()
