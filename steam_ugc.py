#!/usr/bin/env python3
"""Puente con el cliente de Steam para WE Wallpapers.

Hace lo mismo que wallpaperui.exe de Wallpaper Engine: se conecta al Steam abierto
como la app 431960 e invoca ISteamUGC (suscribirse, desuscribirse, votar, estado de
descarga). Steam descarga los ítems en steamapps/workshop/content/431960.
No hay libsteam_api.so para Linux en el sistema, así que habla directamente con
steamclient.so (CreateInterface + vtables de los headers públicos del SDK).
Va en un proceso aparte: un error de ctypes no tumba la interfaz, y al cerrarlo
Steam deja de mostrar «Jugando a Wallpaper Engine».

Protocolo: una petición JSON por línea en stdin, una respuesta JSON por línea en stdout.
  {"cmd": "subscribe" | "unsubscribe", "id": "123"} -> {"ok": true, "result": 1}
  {"cmd": "state", "ids": ["123", ...]}  -> {"ok": true, "items": {"123": {"state": 5, "done": 0, "total": 0}}}
  {"cmd": "subscribed"}                  -> {"ok": true, "ids": ["123", ...]}
La primera línea que escribe es {"ok": true, "ready": true} o {"ok": false, "error": ...}.
Termina al cerrarse stdin."""

import ctypes as C
import json
import os
import sys
import time
from pathlib import Path

APPID = 431960
CLIENT_VERSION = b"SteamClient023"
UGC_VERSION = b"STEAMUGC_INTERFACE_VERSION021"
UTILS_VERSION = b"SteamUtils011"

# Índices de vtable (orden de declaración en isteamclient.h / isteamugc.h / isteamutils.h).
CLIENT_CREATE_PIPE, CLIENT_RELEASE_PIPE, CLIENT_CONNECT_USER = 0, 1, 2
CLIENT_RELEASE_USER, CLIENT_GET_UTILS, CLIENT_SHUTDOWN, CLIENT_GET_UGC = 4, 9, 22, 25
UGC_SUBSCRIBE, UGC_UNSUBSCRIBE = 72, 73
UGC_NUM_SUBSCRIBED, UGC_GET_SUBSCRIBED, UGC_ITEM_STATE, UGC_DOWNLOAD_INFO = 74, 75, 76, 78
UTILS_GET_APPID, UTILS_CALL_DONE, UTILS_CALL_RESULT = 8, 10, 12

# k_iCallback de cada resultado (steam_api_internal.h: RemoteStorage 1300, UGC 3400).
CB_SUBSCRIBE, CB_UNSUBSCRIBE = 1313, 1315


class SubscribeResult(C.Structure):  # RemoteStorage(Un)SubscribePublishedFileResult_t
    _pack_, _layout_ = 4, "ms"  # VALVE_CALLBACK_PACK_SMALL en Linux (pack 4)
    _fields_ = [("result", C.c_int), ("id", C.c_uint64)]


class CallbackMsg(C.Structure):
    _fields_ = [("user", C.c_int32), ("callback", C.c_int), ("param", C.c_void_p), ("size", C.c_int)]


def method(obj, index, restype, *argtypes):
    """Método virtual `index` del objeto C++ `obj` (ABI Itanium: this como primer argumento)."""
    vtable = C.cast(obj, C.POINTER(C.POINTER(C.c_void_p)))[0]
    fn = C.CFUNCTYPE(restype, C.c_void_p, *argtypes)(vtable[index])
    return lambda *args: fn(obj, *args)


def steam_running():
    # steam.pid queda tras cerrar Steam y el PID puede reutilizarse: se mira el nombre.
    try:
        pid = int((Path.home() / ".steam/steam.pid").read_text().strip())
        return Path(f"/proc/{pid}/comm").read_text().strip() == "steam"
    except (OSError, ValueError):
        return False


class Steam:
    def __init__(self):
        if not steam_running():
            raise RuntimeError("steam-not-running")
        # steamclient toma el appid de estas variables al cargarse, como hace SteamAPI_Init
        # con steam_appid.txt.
        os.environ["SteamAppId"] = os.environ["SteamGameId"] = str(APPID)
        lib = None
        for p in (Path.home() / ".steam/sdk64/steamclient.so",
                  Path.home() / ".local/share/Steam/linux64/steamclient.so"):
            if p.is_file():
                lib = C.CDLL(str(p))
                break
        if lib is None:
            raise RuntimeError("steamclient.so no encontrado")
        lib.CreateInterface.restype = C.c_void_p
        lib.CreateInterface.argtypes = [C.c_char_p, C.POINTER(C.c_int)]
        self.client = lib.CreateInterface(CLIENT_VERSION, None)
        if not self.client:
            raise RuntimeError("Steam no ofrece " + CLIENT_VERSION.decode())
        self.lib = lib
        cl = self.client
        self.pipe = method(cl, CLIENT_CREATE_PIPE, C.c_int32)()
        if not self.pipe:
            raise RuntimeError("steam-not-running")
        self.user = method(cl, CLIENT_CONNECT_USER, C.c_int32, C.c_int32)(self.pipe)
        if not self.user:
            self.close()
            raise RuntimeError("Steam no tiene una sesión iniciada")
        self.ugc = method(cl, CLIENT_GET_UGC, C.c_void_p, C.c_int32, C.c_int32, C.c_char_p)(
            self.user, self.pipe, UGC_VERSION)
        self.utils = method(cl, CLIENT_GET_UTILS, C.c_void_p, C.c_int32, C.c_char_p)(self.pipe, UTILS_VERSION)
        if not self.ugc or not self.utils:
            self.close()
            raise RuntimeError("Steam no ofrece ISteamUGC")
        appid = method(self.utils, UTILS_GET_APPID, C.c_uint32)()
        if appid != APPID:
            self.close()
            raise RuntimeError(f"Steam asignó el appid {appid}, no {APPID}")

        u = self.ugc
        self._subscribe = method(u, UGC_SUBSCRIBE, C.c_uint64, C.c_uint64)
        self._unsubscribe = method(u, UGC_UNSUBSCRIBE, C.c_uint64, C.c_uint64)
        self._num_subscribed = method(u, UGC_NUM_SUBSCRIBED, C.c_uint32, C.c_bool)
        self._subscribed = method(u, UGC_GET_SUBSCRIBED, C.c_uint32, C.POINTER(C.c_uint64), C.c_uint32, C.c_bool)
        self._state = method(u, UGC_ITEM_STATE, C.c_uint32, C.c_uint64)
        self._download_info = method(u, UGC_DOWNLOAD_INFO, C.c_bool, C.c_uint64,
                                     C.POINTER(C.c_uint64), C.POINTER(C.c_uint64))
        self._call_done = method(self.utils, UTILS_CALL_DONE, C.c_bool, C.c_uint64, C.POINTER(C.c_bool))
        self._call_result = method(self.utils, UTILS_CALL_RESULT, C.c_bool, C.c_uint64, C.c_void_p,
                                   C.c_int, C.c_int, C.POINTER(C.c_bool))
        lib.Steam_BGetCallback.restype = C.c_bool
        lib.Steam_BGetCallback.argtypes = [C.c_int32, C.POINTER(CallbackMsg), C.POINTER(C.c_int32)]
        lib.Steam_FreeLastCallback.argtypes = [C.c_int32]

    def drain(self):
        """Vacía la cola de callbacks de la tubería (no se usan; los resultados se piden aparte)."""
        msg, call = CallbackMsg(), C.c_int32()
        while self.lib.Steam_BGetCallback(self.pipe, C.byref(msg), C.byref(call)):
            self.lib.Steam_FreeLastCallback(self.pipe)

    def wait(self, call, struct, callback_id, timeout=20):
        if not call:
            raise RuntimeError("Steam rechazó la petición")
        failed = C.c_bool()
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.drain()
            if self._call_done(call, C.byref(failed)):
                out = struct()
                if failed.value or not self._call_result(call, C.byref(out), C.sizeof(out), callback_id,
                                                         C.byref(failed)) or failed.value:
                    raise RuntimeError("La llamada a Steam falló")
                return out
            time.sleep(0.05)
        raise RuntimeError("Steam no respondió a tiempo")

    def subscribe(self, item_id, on=True):
        call = (self._subscribe if on else self._unsubscribe)(int(item_id))
        res = self.wait(call, SubscribeResult, CB_SUBSCRIBE if on else CB_UNSUBSCRIBE)
        return {"result": res.result}

    def state(self, ids):
        out = {}
        for i in ids:
            done, total = C.c_uint64(), C.c_uint64()
            self._download_info(int(i), C.byref(done), C.byref(total))
            out[str(i)] = {"state": self._state(int(i)), "done": done.value, "total": total.value}
        self.drain()
        return {"items": out}

    def subscribed(self):
        n = self._num_subscribed(False)
        arr = (C.c_uint64 * max(n, 1))()
        n = self._subscribed(arr, n, False)
        return {"ids": [str(arr[k]) for k in range(n)]}

    def close(self):
        cl = self.client
        if getattr(self, "user", 0):
            method(cl, CLIENT_RELEASE_USER, C.c_bool, C.c_int32, C.c_int32)(self.pipe, self.user)
        if getattr(self, "pipe", 0):
            method(cl, CLIENT_RELEASE_PIPE, C.c_bool, C.c_int32)(self.pipe)
        method(cl, CLIENT_SHUTDOWN, C.c_bool)()


def main():
    def send(obj):
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()

    try:
        steam = Steam()
    except Exception as e:  # noqa: BLE001
        send({"ok": False, "error": str(e)})
        return 1
    send({"ok": True, "ready": True})
    handlers = {
        "subscribe": lambda r: steam.subscribe(r["id"], True),
        "unsubscribe": lambda r: steam.subscribe(r["id"], False),
        "state": lambda r: steam.state(r.get("ids") or []),
        "subscribed": lambda r: steam.subscribed(),
    }
    try:
        for line in sys.stdin:
            try:
                req = json.loads(line)
                send({"ok": True, **handlers[req["cmd"]](req)})
            except Exception as e:  # noqa: BLE001
                send({"ok": False, "error": str(e)})
    finally:
        steam.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
