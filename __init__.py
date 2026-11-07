# -*- coding: utf-8 -*-
#
# picard3-beatport-id-auto-tagger 1.0.0 — plugin para MusicBrainz Picard 3 (API de plugins v3).
# Autor: Arturo
# Licencia: GPL-2.0-or-later
#
# A partir del ID (o URL) de un lanzamiento de Beatport, obtiene todas sus
# pistas, las empareja con los archivos seleccionados (ISRC → título →
# posición) y escribe BEATPORT_TRACK_ID y BEATPORT_RELEASE_ID.
#
# Fuente de datos: el JSON que Beatport renderiza en el servidor
# (__NEXT_DATA__) en https://www.beatport.com/release/x/<release_id>.
# No usa tokens de la API ni cookies: una única petición por lanzamiento,
# con caché en memoria.

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher

from types import SimpleNamespace

from PyQt6 import QtCore, QtGui, QtWidgets

from picard.plugin3.api import BaseAction, PluginApi

# ──────────────────────────────────────────────
# Constantes
# ──────────────────────────────────────────────
TAG_TRACK_ID = "BEATPORT_TRACK_ID"
TAG_RELEASE_ID = "BEATPORT_RELEASE_ID"
DOCK_OBJECT_NAME = "picard3_beatport_id_auto_tagger_dock"

# Beatport acepta cualquier slug si el ID es correcto (verificado).
RELEASE_URL_TEMPLATE = "https://www.beatport.com/release/x/{release_id}"

RE_RELEASE_ID = re.compile(r"^\d{1,12}$")
RE_RELEASE_URL = re.compile(r"/release/[^/?#]+/(\d{1,12})(?:[/?#]|$)")
RE_NEXT_DATA = re.compile(
    r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL
)
RE_NON_ALNUM = re.compile(r"[^a-z0-9]+")

MAX_PAGES = 20           # Tope de seguridad (Beatport sirve 100 pistas por página)

TITLE_THRESHOLD =0.82   # Similitud mínima para emparejar por título
ARTIST_WEIGHT = 0.05     # Peso del artista como desempate

MULTIPLE = object()      # Centinela: valores distintos entre archivos

COL_FILE, COL_TRACK, COL_ID, COL_METHOD = range(4)


# ──────────────────────────────────────────────
# Modelo y lógica pura (sin Qt, fácil de probar)
# ──────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class BeatportTrack:
    track_id: str
    name: str
    mix_name: str
    artists: str
    isrc: str
    position: int  # Estimada: orden ascendente de ID

    @property
    def full_title(self) -> str:
        return f"{self.name} ({self.mix_name})" if self.mix_name else self.name

    @property
    def label(self) -> str:
        return f"{self.position:02d}. {self.artists} — {self.full_title}"


@dataclass(frozen=True, slots=True)
class BeatportRelease:
    release_id: str
    tracks: tuple[BeatportTrack, ...]
    total: int  # 'count' declarado por Beatport

    @property
    def complete(self) -> bool:
        return len(self.tracks) >= self.total


class BeatportParseError(Exception):
    pass


def parse_release_input(text: str) -> str | None:
    """Acepta un ID numérico o una URL de lanzamiento de Beatport."""
    text = text.strip()
    if RE_RELEASE_ID.match(text):
        return text
    match = RE_RELEASE_URL.search(text)
    return match.group(1) if match else None


def _normalize_isrc(value) -> str:
    return str(value or "").upper().replace("-", "").strip()


def parse_tracks_page(
    html: str, release_id: str, allow_empty: bool = False
) -> tuple[list[dict], int, int]:
    """Extrae de una página (?page=N) las pistas en bruto, el 'count' total
    declarado y el número total de páginas. Con allow_empty, una página sin
    pistas (más allá de la última) devuelve ([], count, pages) sin error."""
    match = RE_NEXT_DATA.search(html)
    if not match:
        raise BeatportParseError("La página no contiene datos __NEXT_DATA__.")
    try:
        data = json.loads(match.group(1))
    except ValueError as exc:
        raise BeatportParseError(f"JSON no válido: {exc}") from exc

    queries = (
        data.get("props", {})
        .get("pageProps", {})
        .get("dehydratedState", {})
        .get("queries", [])
    )
    rid = int(release_id)

    for query in queries:
        payload = (query.get("state") or {}).get("data")
        if not isinstance(payload, dict):
            continue
        results = payload.get("results")
        if allow_empty and results == []:
            key = query.get("queryKey") or ()
            if (len(key) > 1 and key[0] == "tracks" and isinstance(key[1], dict)
                    and key[1].get("release_id") == rid):
                return [], int(payload.get("count") or 0), 1
        if not isinstance(results, list) or not results:
            continue
        # Solo es el listado buscado si TODAS son pistas de este lanzamiento.
        if not all(
            isinstance(t, dict)
            and "mix_name" in t
            and (t.get("release") or {}).get("id") == rid
            for t in results
        ):
            continue

        count = int(payload.get("count") or len(results))
        # 'page' llega como "1/3" (página actual / total de páginas).
        pages = 1
        m = re.fullmatch(r"\s*\d+\s*/\s*(\d+)\s*", str(payload.get("page") or ""))
        if m:
            pages = max(1, int(m.group(1)))
        elif count > len(results):
            pages = -(-count // len(results))
        return results, count, pages

    raise BeatportParseError(
        "No se encontró el listado de pistas de este lanzamiento."
    )


def build_release(release_id: str, raw_tracks: list[dict], count: int) -> BeatportRelease:
    """Construye el lanzamiento con las pistas de todas las páginas
    (sin duplicados), ordenadas por ID."""
    unique = {int(t["id"]): t for t in raw_tracks}
    ordered = [unique[k] for k in sorted(unique)]
    tracks = tuple(
        BeatportTrack(
            track_id=str(t["id"]),
            name=(t.get("name") or "").strip(),
            mix_name=(t.get("mix_name") or "").strip(),
            artists=", ".join(a.get("name", "") for a in (t.get("artists") or ())),
            isrc=_normalize_isrc(t.get("isrc")),
            position=pos,
        )
        for pos, t in enumerate(ordered, start=1)
    )
    return BeatportRelease(release_id, tracks, max(count, len(tracks)))


def parse_release_page(html: str, release_id: str) -> BeatportRelease:
    """Atajo para una única página."""
    raw, count, _pages = parse_tracks_page(html, release_id)
    return build_release(release_id, raw, count)


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return RE_NON_ALNUM.sub(" ", text.lower()).strip()


def match_files(files, tracks) -> list[tuple[int | None, str]]:
    """Empareja archivos con pistas. Devuelve (índice de pista | None, método)
    por archivo. Cada pista se asigna como máximo una vez.
    Orden de prioridad: ISRC exacto → similitud de título → número de pista."""
    result: list[tuple[int | None, str]] = [(None, "")] * len(files)
    pending = set(range(len(files)))
    free = set(range(len(tracks)))

    def assign(fi: int, ti: int, method: str):
        result[fi] = (ti, method)
        pending.discard(fi)
        free.discard(ti)

    # 1. ISRC
    by_isrc = {t.isrc: i for i, t in enumerate(tracks) if t.isrc}
    for fi in sorted(pending):
        ti = by_isrc.get(_normalize_isrc(files[fi].metadata.get("isrc")))
        if ti is not None and ti in free:
            assign(fi, ti, "ISRC")

    # 2. Título (+ artista como desempate), asignación voraz global
    keys = [(_norm(t.full_title), _norm(t.name), _norm(t.artists)) for t in tracks]
    candidates = []
    for fi in pending:
        meta = files[fi].metadata
        title = _norm(meta.get("title", ""))
        if not title:
            continue
        artist = _norm(meta.get("artist", ""))
        # seq2 se cachea en SequenceMatcher: se fija una vez por archivo.
        sm = SequenceMatcher(None)
        sm.set_seq2(title)
        for ti in free:
            full, base, t_artist = keys[ti]
            score = 0.0
            sm.set_seq1(full)
            if (sm.real_quick_ratio() >= TITLE_THRESHOLD
                    and sm.quick_ratio() >= TITLE_THRESHOLD):
                score = sm.ratio()
            sm.set_seq1(base)
            if (0.95 * sm.real_quick_ratio() >= TITLE_THRESHOLD
                    and 0.95 * sm.quick_ratio() >= TITLE_THRESHOLD):
                score = max(score, 0.95 * sm.ratio())
            if score < TITLE_THRESHOLD:
                continue
            if artist and t_artist:
                score += ARTIST_WEIGHT * SequenceMatcher(None, artist, t_artist).ratio()
            candidates.append((score, fi, ti))
    for score, fi, ti in sorted(candidates, reverse=True):
        if fi in pending and ti in free:
            assign(fi, ti, f"Título ({min(score, 1.0):.0%})")

    # 3. Número de pista (posición estimada)
    by_pos = {t.position: i for i, t in enumerate(tracks)}
    for fi in sorted(pending):
        raw = str(files[fi].metadata.get("tracknumber", "")).split("/")[0]
        if raw.isdigit():
            ti = by_pos.get(int(raw))
            if ti is not None and ti in free:
                assign(fi, ti, "Posición (revisar)")

    return result


def _collect_files(objects) -> list:
    """Archivos únicos (en orden) de archivos, pistas, álbumes o clústeres."""
    seen = {}
    for obj in objects or ():
        iterfiles = getattr(obj, "iterfiles", None)
        if iterfiles is None:
            continue
        for f in iterfiles():
            seen.setdefault(id(f), f)
    return list(seen.values())


def _common_value(files, tag):
    values = {f.metadata.get(tag, "") for f in files}
    return values.pop() if len(values) == 1 else MULTIPLE


# ──────────────────────────────────────────────
# Cliente de red (servicio web de Picard, asíncrono)
# ──────────────────────────────────────────────
class BeatportClient:
    def __init__(self, api: PluginApi):
        self._api = api
        self._cache: dict[str, BeatportRelease] = {}

    def fetch_release(self, release_id: str, callback):
        """callback(release_id, BeatportRelease | None, error: str | None)"""
        cached = self._cache.get(release_id)
        if cached is not None:
            callback(release_id, cached, None)
            return

        raw_tracks: list[dict] = []
        state = {"count": 0, "pages": 1}

        def finish(release, error=None):
            if release is not None:
                self._cache[release_id] = release
            callback(release_id, release, error)

        def request(page: int):
            url = RELEASE_URL_TEMPLATE.format(release_id=release_id)
            if page > 1:
                url += f"?page={page}"
            self._api.web_service.get_url(
                url=url,
                handler=lambda document, reply, error: handler(page, document, reply, error),
                parse_response_type=None,  # HTML en bruto
            )

        def handler(page, document, reply, error):
            if error:
                finish(None, f"Error de red (página {page}): {reply.errorString()}")
                return
            try:
                html = bytes(document).decode("utf-8", "replace")
                results, count, pages = parse_tracks_page(
                    html, release_id, allow_empty=page > 1
                )
                if page == 1:
                    state["count"], state["pages"] = count, min(pages, MAX_PAGES)
                raw_tracks.extend(results)
                if page < state["pages"] and results:
                    request(page + 1)  # Siguiente página, en secuencia
                    return
                finish(build_release(release_id, raw_tracks, state["count"]))
            except BeatportParseError as exc:
                finish(None, str(exc))
            except Exception as exc:  # Defensa ante cambios en la web
                self._api.logger.exception("Error al analizar el lanzamiento %s", release_id)
                finish(None, f"Error inesperado: {exc}")

        request(1)


# ──────────────────────────────────────────────
# Panel
# ──────────────────────────────────────────────
class _MatchSignals(QtCore.QObject):
    done = QtCore.pyqtSignal(int, object)


class _MatchJob(QtCore.QRunnable):
    """Ejecuta match_files fuera del hilo de la interfaz."""

    def __init__(self, seq, snapshots, tracks, signals):
        super().__init__()
        self._seq, self._snapshots = seq, snapshots
        self._tracks, self._signals = tracks, signals

    def run(self):
        try:
            result = match_files(self._snapshots, self._tracks)
        except Exception:
            result = None
        self._signals.done.emit(self._seq, result)


def _snapshot(f):
    """Copia ligera de los metadatos que necesita match_files (segura entre hilos)."""
    meta = f.metadata
    return SimpleNamespace(metadata={
        k: str(meta.get(k, "")) for k in ("isrc", "title", "artist", "tracknumber")
    })


class BeatportTaggerPanel(QtWidgets.QWidget):
    def __init__(self, api: PluginApi, client: BeatportClient, parent=None):
        super().__init__(parent)
        self._api = api
        self._client = client
        self._files: list = []
        self._release: BeatportRelease | None = None
        self._combos: list[QtWidgets.QComboBox] = []
        self._combo_model = QtGui.QStandardItemModel(self)  # Compartido por todas las filas
        self._request_seq = 0
        self._match_seq = 0
        self._rebuilding = False
        self._match_signals = _MatchSignals(self)
        self._match_signals.done.connect(self._on_matched)
        self._disposed = False
        self._build_ui()
        self._connect_signals()
        self.set_files([])

    # ── Interfaz ─────────────────────────────
    def _build_ui(self):
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        self._lbl_selection = QtWidgets.QLabel()
        self._lbl_selection.setWordWrap(True)
        font = self._lbl_selection.font()
        font.setItalic(True)
        self._lbl_selection.setFont(font)
        layout.addWidget(self._lbl_selection)

        row = QtWidgets.QHBoxLayout()
        self._input_release = QtWidgets.QLineEdit()
        self._input_release.setPlaceholderText("Release ID o URL de Beatport")
        self._input_release.setClearButtonEnabled(True)
        self._btn_fetch = QtWidgets.QPushButton("Obtener pistas")
        row.addWidget(self._input_release, 1)
        row.addWidget(self._btn_fetch)
        layout.addLayout(row)

        self._lbl_release = QtWidgets.QLabel()
        self._lbl_release.setWordWrap(True)
        layout.addWidget(self._lbl_release)

        self._table = QtWidgets.QTableWidget(0, 4)
        self._table.setHorizontalHeaderLabels(
            ["Archivo", "Pista de Beatport", "Track ID", "Método"]
        )
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(
            QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self._table.setSelectionMode(
            QtWidgets.QAbstractItemView.SelectionMode.NoSelection
        )
        header = self._table.horizontalHeader()
        resize = QtWidgets.QHeaderView.ResizeMode
        header.setSectionResizeMode(COL_FILE, resize.Stretch)
        header.setSectionResizeMode(COL_TRACK, resize.Stretch)
        header.setSectionResizeMode(COL_ID, resize.ResizeToContents)
        header.setSectionResizeMode(COL_METHOD, resize.ResizeToContents)
        layout.addWidget(self._table, 1)

        buttons = QtWidgets.QHBoxLayout()
        self._btn_apply = QtWidgets.QPushButton("Aplicar")
        self._btn_apply.setDefault(True)
        self._btn_clear = QtWidgets.QPushButton("Limpiar")
        buttons.addWidget(self._btn_apply)
        buttons.addWidget(self._btn_clear)
        layout.addLayout(buttons)

        self._lbl_status = QtWidgets.QLabel()
        self._lbl_status.setWordWrap(True)
        layout.addWidget(self._lbl_status)

    def _connect_signals(self):
        self._btn_fetch.clicked.connect(self._fetch)
        self._input_release.returnPressed.connect(self._fetch)
        self._input_release.textChanged.connect(self._update_fetch_state)
        self._btn_apply.clicked.connect(self._apply_tags)
        self._btn_clear.clicked.connect(self._clear)

    def dispose(self):
        """Invalida respuestas de red pendientes antes de destruir el widget."""
        self._disposed = True

    # ── Estado ───────────────────────────────
    def set_files(self, files: list):
        self._files = files
        count = len(files)
        if count == 0:
            self._lbl_selection.setText("Sin archivos seleccionados")
        elif count == 1:
            meta = files[0].metadata
            artist, title = meta.get("artist", ""), meta.get("title", "")
            self._lbl_selection.setText(
                f"{artist} — {title}" if (artist or title) else files[0].base_filename
            )
        else:
            self._lbl_selection.setText(f"{count} archivos seleccionados")

        # Propone el release ya etiquetado si es común a toda la selección.
        if files and not self._input_release.text():
            common = _common_value(files, TAG_RELEASE_ID)
            if common is not MULTIPLE and common:
                self._input_release.setText(common)

        self._rebuild_table()
        self._update_fetch_state()

    def _fill_combo_model(self, tracks):
        model = self._combo_model
        model.clear()
        model.appendRow(QtGui.QStandardItem("— Sin asignar —"))
        for idx, track in enumerate(tracks):
            item = QtGui.QStandardItem(track.label)
            item.setData(idx, QtCore.Qt.ItemDataRole.UserRole)
            model.appendRow(item)

    def _rebuild_table(self):
        self._match_seq += 1  # Invalida emparejamientos en curso
        self._rebuilding = True
        self._table.setUpdatesEnabled(False)
        self._table.setRowCount(0)
        self._combos.clear()
        tracks = self._release.tracks if self._release else ()
        self._fill_combo_model(tracks)

        self._table.setRowCount(len(self._files))
        for row, f in enumerate(self._files):
            meta = f.metadata
            label = meta.get("title", "") or f.base_filename
            item = QtWidgets.QTableWidgetItem(label)
            item.setToolTip(f.filename)
            self._table.setItem(row, COL_FILE, item)

            combo = QtWidgets.QComboBox()
            combo.setModel(self._combo_model)  # Modelo compartido: sin addItem por fila
            combo.setEnabled(bool(tracks))
            self._table.setItem(
                row, COL_ID,
                QtWidgets.QTableWidgetItem("" if tracks else meta.get(TAG_TRACK_ID, "")),
            )
            self._table.setItem(row, COL_METHOD, QtWidgets.QTableWidgetItem(""))
            combo.currentIndexChanged.connect(
                lambda _i, r=row: self._on_combo_changed(r, manual=True)
            )
            self._table.setCellWidget(row, COL_TRACK, combo)
            self._combos.append(combo)
        self._rebuilding = False
        self._table.setUpdatesEnabled(True)
        self._update_apply_state()

        if tracks and self._files:
            self._set_status(f"Emparejando {len(self._files)} archivos…")
            job = _MatchJob(
                self._match_seq, [_snapshot(f) for f in self._files],
                tracks, self._match_signals,
            )
            QtCore.QThreadPool.globalInstance().start(job)

    def _on_matched(self, seq, matches):
        if self._disposed or seq != self._match_seq or matches is None:
            return
        self._table.setUpdatesEnabled(False)
        for row, (ti, method) in enumerate(matches):
            if ti is not None:
                combo = self._combos[row]
                combo.blockSignals(True)
                combo.setCurrentIndex(ti + 1)
                combo.blockSignals(False)
            self._table.item(row, COL_METHOD).setText(method)
            self._on_combo_changed(row, manual=False)
        self._table.setUpdatesEnabled(True)
        self._update_apply_state()

    def _selected_track(self, row: int) -> BeatportTrack | None:
        idx = self._combos[row].currentData()
        return None if idx is None or self._release is None else self._release.tracks[idx]

    def _on_combo_changed(self, row: int, manual: bool):
        # Al reconstruir, el modelo compartido se vacía y los combos antiguos
        # (aún vivos hasta deleteLater) emiten señales: se ignoran.
        if self._rebuilding or row >= len(self._combos):
            return
        track = self._selected_track(row)
        self._table.item(row, COL_ID).setText(track.track_id if track else "")
        if manual:
            self._table.item(row, COL_METHOD).setText("Manual" if track else "")
            self._update_apply_state()

    def _update_fetch_state(self):
        valid = parse_release_input(self._input_release.text()) is not None
        self._btn_fetch.setEnabled(valid)

    def _update_apply_state(self):
        assigned = [t.track_id for r in range(len(self._combos))
                    if (t := self._selected_track(r)) is not None]
        duplicates = len(assigned) != len(set(assigned))
        self._btn_apply.setEnabled(bool(assigned) and not duplicates)
        if duplicates:
            self._set_status("Hay pistas asignadas a más de un archivo.", error=True)
        elif self._release is not None:
            self._set_status(f"{len(assigned)} de {len(self._files)} archivos asignados.")

    # ── Acciones ─────────────────────────────
    def _fetch(self):
        release_id = parse_release_input(self._input_release.text())
        if release_id is None:
            return
        self._request_seq += 1
        seq = self._request_seq
        self._btn_fetch.setEnabled(False)
        self._lbl_release.setText(f"Consultando lanzamiento {release_id}…")
        self._client.fetch_release(
            release_id, lambda rid, rel, err: self._on_fetched(seq, rid, rel, err)
        )

    def _on_fetched(self, seq, release_id, release, error):
        if self._disposed or seq != self._request_seq:
            return  # Respuesta obsoleta o panel destruido
        self._update_fetch_state()
        if error:
            self._release = None
            self._lbl_release.setText("")
            self._set_status(f"Lanzamiento {release_id}: {error}", error=True)
            self._rebuild_table()
            return
        self._release = release
        info = f"Lanzamiento {release_id}: {len(release.tracks)} pistas"
        if not release.complete:
            info += f" de {release.total} (listado incompleto)"
        self._lbl_release.setText(info)
        self._rebuild_table()

    def _apply_tags(self):
        if not self._btn_apply.isEnabled() or self._release is None:
            return
        release_id = self._release.release_id
        applied = failed = 0
        for row, f in enumerate(self._files):
            track = self._selected_track(row)
            if track is None:
                continue
            try:
                f.metadata[TAG_TRACK_ID] = track.track_id
                f.metadata[TAG_RELEASE_ID] = release_id
                f.update()
                applied += 1
            except Exception:
                failed += 1
                self._api.logger.exception("Error al etiquetar %s", f.filename)

        if failed:
            self._set_status(f"{failed} archivos fallaron; {applied} etiquetados.", error=True)
        else:
            self._set_status(
                f"Etiquetas aplicadas a {applied} archivo{'s' if applied != 1 else ''}. "
                "Guarde para escribirlas en disco."
            )

    def _clear(self):
        self._request_seq += 1  # Descarta peticiones en curso
        self._release = None
        self._input_release.clear()
        self._lbl_release.setText("")
        self._rebuild_table()
        self._set_status("")

    def _set_status(self, message: str, error: bool = False):
        # Hereda el tema (claro/oscuro); solo los errores van en rojo.
        self._lbl_status.setStyleSheet("color: #e53935;" if error else "")
        self._lbl_status.setText(message)


# ──────────────────────────────────────────────
# Controlador del ciclo de vida (enable/disable)
# ──────────────────────────────────────────────
class _Controller:
    def __init__(self, api: PluginApi):
        self.api = api
        self.client = BeatportClient(api)
        self.window = None
        self.dock: QtWidgets.QDockWidget | None = None
        self.panel: BeatportTaggerPanel | None = None
        self._pending_objects = ()
        self._timer = QtCore.QTimer()
        self._timer.setSingleShot(True)
        self._timer.setInterval(250)
        self._timer.timeout.connect(self._flush_selection)

    def install(self):
        self.window = self.api.tagger.window
        if self.window is None:
            QtCore.QTimer.singleShot(0, self.install)
            return

        self.panel = BeatportTaggerPanel(self.api, self.client)
        self.dock = QtWidgets.QDockWidget("picard3-beatport-id-auto-tagger", self.window)
        self.dock.setObjectName(DOCK_OBJECT_NAME)
        self.dock.setAllowedAreas(
            QtCore.Qt.DockWidgetArea.LeftDockWidgetArea
            | QtCore.Qt.DockWidgetArea.RightDockWidgetArea
        )
        self.dock.setWidget(self.panel)
        self.window.addDockWidget(QtCore.Qt.DockWidgetArea.RightDockWidgetArea, self.dock)

        self.window.selection_updated.connect(self._on_selection)
        self._on_selection(self.window.selected_objects)

    def uninstall(self):
        self._timer.stop()
        if self.panel is not None:
            self.panel.dispose()
        if self.window is not None:
            try:
                self.window.selection_updated.disconnect(self._on_selection)
            except (TypeError, RuntimeError):
                pass
            if self.dock is not None:
                self.window.removeDockWidget(self.dock)
        if self.dock is not None:
            self.dock.deleteLater()
        self.dock = self.panel = self.window = None

    def show_dock(self):
        if self.dock is not None:
            self.dock.show()
            self.dock.raise_()

    def _on_selection(self, objects):
        # Debounce: al seleccionar/deseleccionar muchos elementos Picard emite
        # la señal repetidamente; solo se procesa la última selección.
        self._pending_objects = objects
        self._timer.start()

    def _flush_selection(self):
        if self.panel is not None:
            self.panel.set_files(_collect_files(self._pending_objects))


_controller: _Controller | None = None


class ShowPanelAction(BaseAction):
    TITLE = "Mostrar panel picard3-beatport-id-auto-tagger"

    def callback(self, objs):
        if _controller is not None:
            _controller.show_dock()


def enable(api: PluginApi):
    global _controller
    _controller = _Controller(api)
    _controller.install()
    api.register_tools_menu_action(ShowPanelAction)


def disable():
    global _controller
    if _controller is not None:
        _controller.uninstall()
        _controller = None
