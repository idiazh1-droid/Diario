# Mi boletín diario

Cada mañana, GitHub ejecuta `generar.py`: lee las fuentes de `fuentes.yaml`, la IA (Google Gemini) elige y resume lo más relevante y se publica una página que abres desde el celular. Tiene un tope de 6 notas por sección, así que siempre termina.

## Puesta en marcha (una sola vez, desde un computador, ~20 minutos)

1. Crea una cuenta en https://github.com/signup.
2. Arriba a la derecha: **+ → New repository**. Nombre: `boletin`. Marca **Public**. No marques "Add a README". **Create repository**.
3. En la página del repositorio vacío, haz clic en **uploading an existing file** y arrastra `generar.py`, `fuentes.yaml`, `requirements.txt` y `README.md`. Abajo, **Commit changes**.
4. **Add file → Create new file**. En el nombre escribe exactamente `.github/workflows/diario.yml` (al escribir las barras se crean las carpetas). Pega el contenido del archivo `diario.yml` y haz **Commit changes**.
5. Clave de la IA: entra a https://aistudio.google.com/apikey, inicia sesión con tu cuenta de Google y haz clic en **Crear clave de API**. Copia la clave. En tu repositorio: **Settings → Secrets and variables → Actions → New repository secret**. Nombre: `GEMINI_API_KEY`. Valor: la clave.
6. Primera ejecución: pestaña **Actions → Boletín diario → Run workflow**. Espera 1 o 2 minutos. Si sale un check verde, ya existe la carpeta `docs`.
7. Publicar: **Settings → Pages → Source: Deploy from a branch → Branch: main, carpeta /docs → Save**. Después de un minuto aparece tu dirección: `https://TU-USUARIO.github.io/boletin/`.
8. En el celular abre esa dirección y agrégala a la pantalla de inicio (Chrome: menú ⋮ → *Agregar a pantalla de inicio*; Safari: Compartir → *Agregar a inicio*).

## Uso diario

- Se actualiza solo entre las 6 y las 7 de la mañana. Para actualizar a mano: Actions → Run workflow.
- Al final de la página, "fuentes con problemas" muestra qué fuente falló ese día.
- Si el workflow falla (ninguna fuente respondió), GitHub te avisa por correo y la página anterior se mantiene.

## Cambiar fuentes o secciones

Edita `fuentes.yaml` directamente en GitHub (lápiz ✏️ del archivo) y haz Commit. No hay que tocar nada más.

## Costos y privacidad

- GitHub Actions y Pages son gratis para repositorios públicos.
- El repositorio y la página son públicos (contienen solo titulares y resúmenes de medios, y tus temas de interés). La API key queda en Secrets y no se expone.
- Google Gemini tiene un nivel gratuito generoso. Con una cuenta de estudiante tienes cuota de sobra para este uso diario. Puedes ver el consumo en https://aistudio.google.com.
