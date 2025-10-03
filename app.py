from __future__ import annotations

import html
import queue
import threading
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import pandas as pd
import streamlit as st


def _ensure_tiktoklive_dependencies() -> None:
    try:
        from pyee import AsyncIOEventEmitter  # type: ignore[attr-defined]
    except ImportError:
        try:
            from pyee.asyncio import AsyncIOEventEmitter as _AsyncIOEventEmitter  # type: ignore[attr-defined]
        except ImportError:
            raise
        import pyee  # type: ignore[attr-defined]
        if getattr(pyee, "AsyncIOEventEmitter", None) is None:
            pyee.AsyncIOEventEmitter = _AsyncIOEventEmitter


def _format_tiktok_import_error(exc: BaseException) -> str:
    detail = str(exc)
    if "AsyncIOEventEmitter" in detail and "pyee" in detail:
        return (
            'TikTokLive requiere una version compatible de pyee. Ejecuta "pip install pyee==9.0.4" e intentalo de nuevo.'
        )
    return f"No se pudo cargar TikTokLive: {detail}"

_TIKTOK_IMPORT_ERROR: Optional[BaseException] = None

try:
    _ensure_tiktoklive_dependencies()
    from TikTokLive import TikTokLiveClient
except ModuleNotFoundError as exc:
    TikTokLiveClient = None
    _TIKTOK_IMPORT_ERROR = exc
except Exception as exc:
    TikTokLiveClient = None
    _TIKTOK_IMPORT_ERROR = exc


def format_seconds(total_seconds: int) -> str:
    seconds = max(int(total_seconds), 0)
    minutes, seconds = divmod(seconds, 60)
    return f"{minutes:02}:{seconds:02}"


def log_event(message: str) -> None:
    entry = f"[{time.strftime('%H:%M:%S')}] {message}"
    st.session_state.event_log.append(entry)
    st.session_state.event_log = st.session_state.event_log[-200:]


class DonadoresStore:
    """Gestiona las donaciones de forma segura para multiples hilos."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._donors: Dict[str, Dict[str, Any]] = {}

    def reset(self) -> None:
        with self._lock:
            self._donors.clear()

    def register_gift(
        self,
        username: str,
        display_name: str,
        gift_name: str,
        quantity: int,
        coins: int,
        avatar_url: Optional[str] = None,
    ) -> None:
        key = username or display_name or "invitado"
        timestamp = time.strftime("%H:%M:%S")
        safe_avatar = avatar_url or ""

        with self._lock:
            record = self._donors.setdefault(
                key,
                {
                    "usuario": key,
                    "nombre": display_name or key,
                    "gifts": 0,
                    "monedas": 0,
                    "ultimo_gift": "",
                    "actualizado": timestamp,
                    "avatar": safe_avatar,
                },
            )

            record["gifts"] += max(quantity, 1)
            record["monedas"] += max(coins, 0)
            record["ultimo_gift"] = gift_name
            record["actualizado"] = timestamp
            if safe_avatar:
                record["avatar"] = safe_avatar

    def snapshot(self) -> pd.DataFrame:
        with self._lock:
            donors = sorted(
                (donor.copy() for donor in self._donors.values()),
                key=lambda item: item["monedas"],
                reverse=True,
            )
            data = list(donors)

        if not data:
            columns = [
                "Usuario",
                "Nombre",
                "Gifts",
                "Monedas",
                "Ultimo gift",
                "Actualizado",
            ]
            return pd.DataFrame(columns=columns)

        df = pd.DataFrame(data)
        display_df = df.rename(
            columns={
                "usuario": "Usuario",
                "nombre": "Nombre",
                "gifts": "Gifts",
                "monedas": "Monedas",
                "ultimo_gift": "Ultimo gift",
                "actualizado": "Actualizado",
            }
        )
        if "avatar" in display_df.columns:
            display_df.drop(columns=["avatar"], inplace=True)
        display_df.reset_index(drop=True, inplace=True)
        display_df.index += 1
        return display_df

    def top_donors(self, count: int = 3) -> List[Dict[str, Any]]:
        with self._lock:
            donors = sorted(
                (donor.copy() for donor in self._donors.values()),
                key=lambda item: item["monedas"],
                reverse=True,
            )
            return list(donors)[:count]



@st.cache_resource
def get_shared_store() -> DonadoresStore:
    return DonadoresStore()


class AuctionTimer:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._duration = 60
        self._remaining = 60
        self._running = False
        self._end_time: Optional[float] = None

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            if self._running and self._end_time is not None:
                remaining = int(max(self._end_time - time.time(), 0))
                if remaining <= 0:
                    self._running = False
                    self._end_time = None
                    self._remaining = 0
                elif remaining != self._remaining:
                    self._remaining = remaining
            return {
                "duration": self._duration,
                "remaining": self._remaining,
                "running": self._running,
            }

    def set_duration(self, duration: int) -> None:
        with self._lock:
            prev_duration = self._duration
            prev_remaining = self._remaining
            self._duration = max(int(duration), 0)
            if self._running and self._end_time is not None:
                elapsed = max(prev_duration - prev_remaining, 0)
                new_remaining = max(self._duration - elapsed, 0)
                self._remaining = int(new_remaining)
                if new_remaining > 0:
                    self._end_time = time.time() + new_remaining
                else:
                    self._running = False
                    self._end_time = None
            else:
                self._remaining = self._duration

    def start(self) -> bool:
        with self._lock:
            if not self._running:
                if self._remaining <= 0:
                    self._remaining = self._duration
                self._running = True
                self._end_time = time.time() + self._remaining
                return True
            return False

    def pause(self) -> None:
        with self._lock:
            if self._running and self._end_time is not None:
                self._remaining = int(max(self._end_time - time.time(), 0))
                self._running = False
                self._end_time = None

    def reset_round(self) -> None:
        with self._lock:
            self._running = False
            self._end_time = None
            self._remaining = self._duration


@st.cache_resource
def get_shared_timer() -> AuctionTimer:
    return AuctionTimer()

class TikTokWorker(threading.Thread):
    """Hilo que mantiene la conexion con TikTok LIVE."""

    def __init__(
        self,
        username: str,
        store: DonadoresStore,
        status_queue: "queue.Queue[tuple[str, str]]",
    ) -> None:
        super().__init__(daemon=True)
        self.username = username.strip().lstrip("@")
        self.store = store
        self.status_queue = status_queue
        self._stop_evt = threading.Event()
        self._init_error: Optional[str] = None
        self.client = self._create_client()

    def _create_client(self) -> Any:
        if TikTokLiveClient is None:
            if isinstance(_TIKTOK_IMPORT_ERROR, ModuleNotFoundError):
                self._init_error = "TikTokLive no esta instalado. Ejecuta 'pip install TikTokLive'."
            elif _TIKTOK_IMPORT_ERROR is not None:
                self._init_error = _format_tiktok_import_error(_TIKTOK_IMPORT_ERROR)
            else:
                self._init_error = "No se pudo inicializar el cliente de TikTok."
            return None

        try:
            client = TikTokLiveClient(unique_id=self.username)
        except Exception as exc:
            self._init_error = f"No se pudo crear el cliente: {exc}"
            return None

        self._register_handlers(client)
        return client

    def _register_handlers(self, client: Any) -> None:
        @client.on("connect")
        async def _(_event: Any) -> None:
            self.status_queue.put(("estado", f"Conectado a @{self.username}"))

        @client.on("disconnect")
        async def _(_event: Any) -> None:
            self.status_queue.put(("estado", "Se cerro la sesion"))
            self.stop()

        @client.on("live_end")
        async def _(_event: Any) -> None:
            self.status_queue.put(("estado", "El LIVE ha terminado"))
            self.stop()

        @client.on("error")
        async def _(event: Any) -> None:
            detail = getattr(event, "exception", None) or getattr(event, "error", None)
            self.status_queue.put(("error", f"Error del cliente: {detail}"))

        @client.on("gift")
        async def _(event: Any) -> None:
            gift = getattr(event, "gift", None)
            user = getattr(event, "user", None)
            if gift is None or not getattr(gift, "repeat_end", True):
                return

            raw_username = getattr(user, "uniqueId", "") if user else ""
            display_name = getattr(user, "nickname", "") if user else ""
            gift_name = getattr(gift, "name", "Gift")
            repeat_count = int(getattr(gift, "repeat_count", 1) or 1)
            diamond_count = int(getattr(gift, "diamond_count", 0) or 0)
            coins = diamond_count * repeat_count

            avatar_url: Optional[str] = None
            profile = getattr(user, "profile_picture", None)
            urls = getattr(profile, "urls", None) if profile else None
            if isinstance(urls, (list, tuple)) and urls:
                avatar_url = str(urls[0])

            self.store.register_gift(
                username=raw_username,
                display_name=display_name,
                gift_name=gift_name,
                quantity=repeat_count,
                coins=coins,
                avatar_url=avatar_url,
            )

            donor = display_name or raw_username or "Invitado"
            self.status_queue.put(
                (
                    "evento",
                    f"{donor} envio {repeat_count}x {gift_name} ({coins} monedas)",
                )
            )

    def run(self) -> None:
        if not self.client:
            message = self._init_error
            if not message:
                if isinstance(_TIKTOK_IMPORT_ERROR, ModuleNotFoundError):
                    message = "TikTokLive no esta instalado. Ejecuta 'pip install TikTokLive'."
                elif _TIKTOK_IMPORT_ERROR is not None:
                    message = _format_tiktok_import_error(_TIKTOK_IMPORT_ERROR)
                else:
                    message = "No se pudo inicializar el cliente de TikTok."
            self.status_queue.put(("error", message))
            return

        try:
            self.status_queue.put(("estado", f"Conectando a @{self.username}..."))
            self.client.run()
        except Exception as exc:
            self.status_queue.put(("error", f"No se pudo conectar: {exc}"))

    def stop(self) -> None:
        self._stop_evt.set()
        if self.client:
            try:
                self.client.stop()
            except Exception:
                pass


def sync_timer_state(timer: AuctionTimer, *, track_events: bool) -> Dict[str, Any]:
    snapshot = timer.snapshot()
    if track_events:
        st.session_state["timer_duration"] = snapshot["duration"]
        st.session_state["timer_remaining"] = snapshot["remaining"]
        st.session_state["timer_running_flag"] = snapshot["running"]
    else:
        st.session_state["overlay_timer_remaining"] = snapshot["remaining"]
    state_key = "last_timer_remaining" if track_events else "overlay_last_timer_remaining"
    previous = st.session_state.get(state_key)
    if track_events and previous is not None and previous > 0 and snapshot["remaining"] <= 0:
        if "event_log" in st.session_state:
            log_event("La subasta ha terminado.")
    st.session_state[state_key] = snapshot["remaining"]
    return snapshot


def render_rank_card(rank: int, donor: Optional[Dict[str, Any]]) -> str:
    color_map = {
        1: "#FFD54F",
        2: "#CFD8DC",
        3: "#D7A86E",
    }
    badge_color = color_map.get(rank, "#4F5B7A")
    if donor is None:
        name = "Disponible"
        coins = "0"
        username = "--"
        avatar_style = ""
    else:
        name = html.escape(str(donor.get("nombre", "Anonimo")))
        coins = html.escape(str(donor.get("monedas", 0)))
        username = html.escape(str(donor.get("usuario", "")))
        avatar_url = donor.get("avatar", "")
        if avatar_url:
            avatar_style = f'background-image: url("{html.escape(str(avatar_url), quote=True)}");'
        else:
            avatar_style = ""

    card = f"""
    <div class="rank-card">
        <div class="rank-badge" style="background:{badge_color};">{rank}</div>
        <div class="rank-avatar" style="{avatar_style}"></div>
        <div class="rank-details">
            <div class="rank-name">{name}</div>
            <div class="rank-username">@{username}</div>
        </div>
        <div class="rank-coins">{coins}</div>
    </div>
    """
    return card


# -----------------------------
# UI Streamlit
# -----------------------------
st.set_page_config(page_title="Subasta TikTok LIVE", page_icon=":trophy:", layout="wide")


def _is_overlay_mode() -> bool:
    params = st.query_params
    value = params.get("overlay")
    if not value:
        return False
    if isinstance(value, (list, tuple)):
        value = value[0]
    return str(value).lower() in {"1", "true", "yes", "on", "si"}


overlay_mode = _is_overlay_mode()

st.markdown(
    """
    <style>
    body {
        background-color: #040720;
        color: #E3ECFF;
    }
    .block-container {
        padding-top: 2rem;
        padding-bottom: 2rem;
        max-width: 1100px;
    }
    .auction-timer {
        text-align: center;
        font-size: 4rem;
        font-weight: 700;
        color: #00f2ff;
        padding: 1.5rem 0;
        margin-bottom: 1rem;
        border-radius: 24px;
        border: 2px solid rgba(0, 242, 255, 0.35);
        background: radial-gradient(circle at top, rgba(0, 242, 255, 0.25), rgba(4, 7, 32, 0.95));
        box-shadow: 0 0 24px rgba(0, 242, 255, 0.15);
    }
    .rank-wrapper {
        display: flex;
        flex-direction: column;
        gap: 0.75rem;
        margin-top: 1rem;
    }
    .rank-card {
        display: grid;
        grid-template-columns: 64px 64px 1fr 80px;
        align-items: center;
        padding: 0.75rem 1rem;
        background: linear-gradient(135deg, rgba(32, 41, 89, 0.9), rgba(14, 18, 46, 0.95));
        border-radius: 18px;
        border: 1px solid rgba(255, 255, 255, 0.08);
    }
    .rank-badge {
        width: 48px;
        height: 48px;
        border-radius: 50%;
        display: flex;
        align-items: center;
        justify-content: center;
        font-weight: 700;
        color: #10152F;
        font-size: 1.25rem;
    }
    .rank-avatar {
        width: 56px;
        height: 56px;
        border-radius: 50%;
        background: radial-gradient(circle, rgba(0, 242, 255, 0.2), rgba(0, 0, 0, 0.6));
        background-size: cover;
        background-position: center;
        border: 2px solid rgba(0, 242, 255, 0.3);
    }
    .rank-details {
        display: flex;
        flex-direction: column;
        gap: 0.15rem;
    }
    .rank-name {
        font-weight: 600;
        font-size: 1.05rem;
    }
    .rank-username {
        font-size: 0.85rem;
        color: rgba(227, 236, 255, 0.65);
    }
    .rank-coins {
        text-align: right;
        font-weight: 700;
        font-size: 1.25rem;
        color: #FFD54F;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

if overlay_mode:
    st.markdown(
        """
        <style>
        header {visibility: hidden;}
        [data-testid=\"stToolbar\"] {display: none;}
        section[data-testid=\"stSidebar\"] {display: none;}
        body {background: rgba(0, 0, 0, 0);}
        .block-container {padding-top: 0.5rem; padding-bottom: 0.5rem; max-width: 640px;}
        </style>
        """,
        unsafe_allow_html=True,
    )

shared_store = get_shared_store()
shared_timer = get_shared_timer()

if "status_queue" not in st.session_state:
    st.session_state.status_queue = queue.Queue()
if "event_log" not in st.session_state:
    st.session_state.event_log = []
if "worker" not in st.session_state:
    st.session_state.worker = None

timer_snapshot = sync_timer_state(shared_timer, track_events=not overlay_mode)
timer_duration = timer_snapshot["duration"]
timer_remaining = timer_snapshot["remaining"]
timer_running = timer_snapshot["running"]

if overlay_mode:
    st.markdown(
        f"<div class='auction-timer'>{format_seconds(timer_remaining)}</div>",
        unsafe_allow_html=True,
    )
    top_overlay = shared_store.top_donors(3)
    overlay_cards = []
    for idx in range(1, 4):
        donor = top_overlay[idx - 1] if idx <= len(top_overlay) else None
        overlay_cards.append(render_rank_card(idx, donor))
    st.markdown(f"<div class='rank-wrapper'>{''.join(overlay_cards)}</div>", unsafe_allow_html=True)
    time.sleep(1)
    st.experimental_rerun()

st.title("Subasta de Donadores TikTok LIVE")

with st.sidebar:
    st.header("Conexion")
    username = st.text_input("Tu usuario de TikTok (sin @)", key="username")
    if st.button("Conectar", use_container_width=True):
        if not username:
            st.warning("Escribe tu usuario.")
        elif st.session_state.worker and st.session_state.worker.is_alive():
            st.info("Ya estas conectado.")
        else:
            shared_store.reset()
            st.session_state.status_queue = queue.Queue()
            st.session_state.event_log = []
            worker = TikTokWorker(
                username=username,
                store=shared_store,
                status_queue=st.session_state.status_queue,
            )
            worker.start()
            st.session_state.worker = worker
            sync_timer_state(shared_timer, track_events=True)
            st.success("Intentando conectar... abre tu LIVE si aun no esta en vivo.")

    if st.button("Desconectar", type="secondary", use_container_width=True):
        if st.session_state.worker:
            st.session_state.worker.stop()
            st.session_state.worker.join(timeout=2.0)
            st.session_state.worker = None
        st.info("Desconectado.")

    st.divider()
    st.subheader("Overlay TikTok Studio")
    overlay_base_default = st.session_state.get("overlay_base_default", "http://localhost:8501")
    overlay_base = st.text_input(
        "URL base",
        value=overlay_base_default,
        key="overlay_base_default",
        help="Ejemplo: http://localhost:8501 o http://192.168.0.10:8501",
    )

    parsed = urlparse(overlay_base.strip()) if overlay_base else None
    if not parsed or not parsed.scheme or not parsed.netloc:
        st.error("Ingresa una URL valida que incluya http:// o https://")
        overlay_url = None
    else:
        overlay_url = overlay_base.rstrip("/") + "/?overlay=1"
        st.markdown(f"[Abrir overlay]({overlay_url})")
        st.code(overlay_url, language="text")
        st.caption("Copia esta URL en un Browser Source de TikTok Studio. Si usas otro equipo, reemplaza la direccion por la IP o dominio accesible.")

main_col, side_col = st.columns([2, 1])

with main_col:
    st.markdown(
        f"<div class='auction-timer'>{format_seconds(timer_remaining)}</div>",
        unsafe_allow_html=True,
    )

    duration = st.slider(
        "Duracion de la subasta (segundos)",
        min_value=10,
        max_value=900,
        step=5,
        value=int(timer_duration),
        help="Establece el tiempo objetivo para la ronda actual.",
    )
    if duration != int(timer_duration):
        shared_timer.set_duration(int(duration))
        timer_snapshot = sync_timer_state(shared_timer, track_events=True)
        timer_duration = timer_snapshot["duration"]
        timer_remaining = timer_snapshot["remaining"]
        timer_running = timer_snapshot["running"]

    controls = st.columns(3)
    if controls[0].button("Iniciar / Reanudar", use_container_width=True):
        if not timer_running:
            if shared_timer.start():
                if timer_remaining == timer_duration:
                    log_event("La subasta ha iniciado.")
                else:
                    log_event("La subasta se reanudo.")
        timer_snapshot = sync_timer_state(shared_timer, track_events=True)
        timer_duration = timer_snapshot["duration"]
        timer_remaining = timer_snapshot["remaining"]
        timer_running = timer_snapshot["running"]

    if controls[1].button("Pausar", use_container_width=True):
        was_running = timer_running
        shared_timer.pause()
        timer_snapshot = sync_timer_state(shared_timer, track_events=True)
        timer_duration = timer_snapshot["duration"]
        timer_remaining = timer_snapshot["remaining"]
        timer_running = timer_snapshot["running"]
        if was_running:
            log_event("Subasta en pausa.")

    if controls[2].button("Reiniciar ronda", use_container_width=True):
        shared_timer.reset_round()
        shared_store.reset()
        st.session_state.event_log = []
        try:
            while not st.session_state.status_queue.empty():
                st.session_state.status_queue.get_nowait()
        except queue.Empty:
            pass
        log_event("Nueva ronda lista. Reinicia tu LIVE para comenzar.")
        timer_snapshot = sync_timer_state(shared_timer, track_events=True)
        timer_duration = timer_snapshot["duration"]
        timer_remaining = timer_snapshot["remaining"]
        timer_running = timer_snapshot["running"]

    df_snapshot = shared_store.snapshot()
    top_donors = shared_store.top_donors(3)

    cards = []
    for idx in range(1, 4):
        donor = top_donors[idx - 1] if idx <= len(top_donors) else None
        cards.append(render_rank_card(idx, donor))

    st.subheader("Top 3 Donadores")
    st.markdown(f"<div class='rank-wrapper'>{''.join(cards)}</div>", unsafe_allow_html=True)

    st.subheader("Ranking completo")
    if df_snapshot.empty:
        st.info("Aun no hay donaciones registradas en esta ronda.")
    else:
        st.dataframe(df_snapshot, use_container_width=True, height=420)

processed_messages = 0
with side_col:
    st.subheader("Actividad")

    while not st.session_state.status_queue.empty():
        tipo, mensaje = st.session_state.status_queue.get()
        processed_messages += 1
        log_event(mensaje)
        if tipo == "error":
            st.error(mensaje)
        elif tipo == "estado":
            st.info(mensaje)
        else:
            st.success(mensaje)

    if st.session_state.event_log:
        st.text("\n".join(st.session_state.event_log[-25:]))
    else:
        st.caption("Sin eventos todavia.")

worker_alive = bool(st.session_state.worker and st.session_state.worker.is_alive())
timer_running_flag = st.session_state.get("timer_running_flag", timer_running)
should_refresh = bool(timer_running_flag or worker_alive or processed_messages)
if should_refresh:
    time.sleep(1)
    st.experimental_rerun()

