# picard3-beatport-id-auto-tagger

Plugin para [MusicBrainz Picard 3](https://picard.musicbrainz.org/) (API de plugins v3) que asigna automáticamente las etiquetas **`BEATPORT_TRACK_ID`** y **`BEATPORT_RELEASE_ID`** a tus archivos a partir de un lanzamiento de Beatport.

## Qué hace

1. Introduces el **ID** o la **URL** de un lanzamiento de Beatport (por ejemplo `https://www.beatport.com/release/nombre/1234567`).
2. El plugin descarga la página del lanzamiento y obtiene su listado de pistas.
3. Empareja cada archivo seleccionado con una pista de Beatport, por este orden de prioridad:
   1. **ISRC** exacto.
   2. **Similitud de título** (el artista se usa como desempate).
   3. **Número de pista** (marcado como «Posición (revisar)», porque es una estimación).
4. Muestra una tabla donde puedes revisar y corregir cada asignación con un desplegable.
5. Al pulsar **Aplicar**, escribe ambas etiquetas en los archivos. Después hay que **guardar** en Picard para escribirlas en disco.

## Uso

- El panel aparece acoplado a la derecha de la ventana principal. Si lo cierras, vuelve a mostrarlo desde **Herramientas → Mostrar panel picard3-beatport-id-auto-tagger**.
- Selecciona archivos, pistas, álbumes o clústeres. El panel trabaja con todos los archivos incluidos en la selección.
- Si todos los archivos seleccionados ya comparten un `BEATPORT_RELEASE_ID`, se propone automáticamente.
- **Aplicar** se desactiva si una misma pista de Beatport está asignada a más de un archivo.
- **Limpiar** vacía el lanzamiento cargado y el campo de entrada.

## Instalación

Copia esta carpeta (con `MANIFEST.toml` y `__init__.py`) en el directorio de plugins de Picard 3 y activa el plugin desde las opciones de Picard.

En Windows, el directorio habitual es:

```
%LOCALAPPDATA%\MusicBrainz\Picard\plugins\picard3-beatport-id-auto-tagger\
```

Requisitos: Picard 3 (API de plugins `3.0`) y Python 3.10 o superior.

## Cómo obtiene los datos

Lee el JSON (`__NEXT_DATA__`) que Beatport incluye en la página pública de cada lanzamiento. No usa tokens de API ni cookies, hace una única petición por lanzamiento y guarda el resultado en memoria durante la sesión.

## Limitaciones conocidas

- Al depender del HTML de Beatport, un cambio en su web puede romper la lectura del listado.
- Solo se lee la primera página de pistas del lanzamiento: si Beatport declara más pistas de las obtenidas, el panel avisa con «listado incompleto».
- Las posiciones de pista se estiman ordenando por ID de pista de Beatport, por lo que el emparejamiento por número de pista debe revisarse.

## Licencia

[GPL-2.0-or-later](https://www.gnu.org/licenses/gpl-2.0.html)
