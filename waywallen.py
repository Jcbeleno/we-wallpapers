"""Control de waywallen, el motor principal de WE Wallpapers, por su WebSocket de control.

El daemon (Flatpak org.waywallen.waywallen) corre como servicio de usuario en el puerto fijo PORT
y waywallen-layer-shell lo dibuja en niri (systemd/*.service). Mensajes: control.proto de waywallen
(MIT, tests/waywallen/proto), compilado en waywallen_pb/ con `protoc --python_out` del mismo
protobuf que el python-protobuf del sistema."""
import itertools
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "waywallen_pb"))
import control_pb2 as pb  # noqa: E402
from websockets.sync.client import connect  # noqa: E402

PORT = 47321
URL = f"ws://127.0.0.1:{PORT}"
SERVICES = ("waywallen.service", "waywallen-layer-shell.service")
# Ajuste de WE Wallpapers -> modo de waywallen ("" / "default": el general de waywallen)
FILL = {"fill": pb.FILL_MODE_PRESERVE_ASPECT_CROP, "fit": pb.FILL_MODE_PRESERVE_ASPECT_FIT,
        "stretch": pb.FILL_MODE_STRETCHED}
_ids = itertools.count(1)


class Error(Exception):
    pass


def call(field, msg, timeout=30):
    """Una petición y su respuesta (los eventos que el daemon empuja entre medias se ignoran)."""
    with connect(URL, max_size=None, open_timeout=3) as ws:
        rid = next(_ids)
        req = pb.Request(request_id=rid)
        getattr(req, field).CopyFrom(msg)
        ws.send(req.SerializeToString())
        end = time.monotonic() + timeout
        while True:
            frame = pb.ServerFrame.FromString(ws.recv(timeout=max(0.1, end - time.monotonic())))
            if frame.WhichOneof("kind") == "response" and frame.response.request_id == rid:
                r = frame.response
                if r.error_code:
                    raise Error(r.message or pb.ErrorCode.Name(r.error_code))
                return getattr(r, field)


def running():
    try:
        call("health", pb.HealthRequest(), timeout=3)
        return True
    except (OSError, TimeoutError, Error):
        return False


def ensure_running(wait=25):
    if running():
        return True
    subprocess.run(["systemctl", "--user", "start", *SERVICES], capture_output=True)
    end = time.monotonic() + wait
    while time.monotonic() < end:
        if running():
            return True
        time.sleep(0.5)
    return False


def wallpaper_ids(rescan=False):
    """{carpeta resuelta del fondo: id}. waywallen llega a Steam por ~/.steam/steam; los web
    apuntan a la carpeta y scene/video a un archivo dentro de ella."""
    if rescan:
        call("wallpaper_scan", pb.WallpaperScanRequest(), timeout=180)
    out = {}
    for w in call("wallpaper_list", pb.WallpaperListRequest()).wallpapers:
        res = w.resource
        out[os.path.realpath(res if os.path.isdir(res) else os.path.dirname(res))] = w.id
    return out


def find(folder):
    key = os.path.realpath(folder)
    wid = wallpaper_ids().get(key) or wallpaper_ids(rescan=True).get(key)  # recién suscrito: reescanea
    if not wid:
        raise Error(f"waywallen no tiene el fondo {folder}")
    return wid


def displays():
    return {d.name: d.display_id for d in call("display_list", pb.DisplayListRequest()).displays}


def prop_str(value):
    return ("true" if value else "false") if isinstance(value, bool) else str(value)


def apply(folder, connector="*", scaling="", props=None, start=True):
    """Pone el fondo de esa carpeta en una salida (o en todas con "*") y devuelve su id. Con
    start=False solo cambia ajuste y propiedades: waywallen los aplica en vivo al que ya se ve."""
    wid = find(folder)
    layout = (pb.WallpaperLayoutSetRequest(wallpaper_id=wid, layout=pb.LayoutPrefs(fillmode=FILL[scaling]))
              if scaling in FILL else pb.WallpaperLayoutSetRequest(wallpaper_id=wid, clear=True))
    call("wallpaper_layout_set", layout)
    for key, value in (props or {}).items():
        call("wallpaper_property_set", pb.WallpaperPropertySetRequest(wallpaper_id=wid, key=key,
                                                                     value=prop_str(value)))
    if not start:
        return wid
    call("global_stop_set", pb.GlobalStopSetRequest(stopped=False))  # por si stop() lo detuvo todo
    req = pb.WallpaperApplyRequest(wallpaper_id=wid)
    outs = displays()
    names = list(outs) if connector == "*" else [connector]
    for name in names:
        if name not in outs:
            raise Error(f"waywallen no ve la pantalla {name}")
        call("display_pause_set", pb.DisplayPauseSetRequest(display_id=outs[name], paused=False))
        if connector != "*":
            req.targets.add(display_id=outs[name])
    call("wallpaper_apply", req, timeout=60)
    return wid


def pause_outputs(names):
    outs = displays()
    for name in names:
        if name in outs:
            call("display_pause_set", pb.DisplayPauseSetRequest(display_id=outs[name], paused=True))


def stop():
    call("global_stop_set", pb.GlobalStopSetRequest(stopped=True))


def set_muted(muted):
    # shortcut: waywallen solo silencia en global, no por fondo; si hace falta, mirar su volumen por renderer
    call("global_mute_set", pb.GlobalMuteSetRequest(muted=bool(muted)))
