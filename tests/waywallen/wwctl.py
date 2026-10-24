"""Cliente mínimo del control de waywallen para pruebas: wwctl.py scan | list | displays | apply <id> [pantalla] | stop
(preparar con tests/waywallen/setup.sh y ejecutar con .venv/bin/python)"""
import asyncio, json, os, sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "wwpb"))
import control_pb2 as pb  # noqa: E402
import websockets  # noqa: E402

URL = "ws://127.0.0.1:47321"
_rid = 0


async def call(ws, field, msg, timeout=30):
    global _rid
    _rid += 1
    req = pb.Request(request_id=_rid)
    getattr(req, field).CopyFrom(msg)
    await ws.send(req.SerializeToString())
    end = asyncio.get_event_loop().time() + timeout
    while True:
        raw = await asyncio.wait_for(ws.recv(), max(0.1, end - asyncio.get_event_loop().time()))
        frame = pb.ServerFrame.FromString(raw)
        if frame.WhichOneof("kind") == "response" and frame.response.request_id == _rid:
            return frame.response


async def main():
    async with websockets.connect(URL, max_size=None) as ws:
        cmd = sys.argv[1]
        if cmd == "scan":
            print(await call(ws, "library_auto_detect", pb.LibraryAutoDetectRequest()))
            print(await call(ws, "wallpaper_scan", pb.WallpaperScanRequest(), timeout=120))
        elif cmd == "list":
            r = await call(ws, "wallpaper_list", pb.WallpaperListRequest())
            for w in r.wallpaper_list.wallpapers:
                print(json.dumps({"id": w.id, "name": w.name, "type": w.wp_type, "resource": w.resource,
                                  "meta": dict(w.metadata)}, ensure_ascii=False))
        elif cmd == "displays":
            r = await call(ws, "display_list", pb.DisplayListRequest())
            for d in r.display_list.displays:
                print(d.display_id, d.name, d.width, d.height)
        elif cmd == "apply":
            req = pb.WallpaperApplyRequest(wallpaper_id=sys.argv[2])
            if len(sys.argv) > 3:
                r = await call(ws, "display_list", pb.DisplayListRequest())
                for d in r.display_list.displays:
                    if sys.argv[3] in d.name:
                        req.targets.add(display_id=d.display_id)
            print(await call(ws, "wallpaper_apply", req))
        elif cmd == "stop":
            print(await call(ws, "global_stop_set", pb.GlobalStopSetRequest(stopped=True)))


asyncio.run(main())
