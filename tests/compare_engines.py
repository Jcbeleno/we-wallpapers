#!/usr/bin/env python3
"""Pone cada fondo en una pantalla con los dos motores y los compara con capturas reales.

  lwe: linux-wallpaperengine (scene/web) y mpvpaper (video), lo que usa WE Wallpapers hoy.
  ww:  waywallen + open-wallpaper-engine (scene/web) y su plugin de video.

Por fondo y motor: dos capturas de la salida (grim) separadas GAP s, el log del motor y un
veredicto automático: crash, nada (igual que la pantalla vacía o un color liso), estático o anima.
Los fallos visuales finos (piezas mal colocadas) se ven en report.html, lado a lado.

Uso: .venv/bin/python tests/compare_engines.py [--output DP-2] [--engines lwe,ww] [--only id,id]
Requiere grim, waywallen (Flatpak) con el plugin OWE, waywallen-layer-shell y tests/waywallen/setup.sh.
La salida debe estar libre de ventanas: todo lo que haya encima sale en las capturas."""
import argparse, html, json, os, re, runpy, signal, subprocess, sys, time
from pathlib import Path

from PIL import Image, ImageChops, ImageStat

ROOT = Path(__file__).resolve().parent.parent
HOME = Path.home()
WS = HOME / ".local/share/Steam/steamapps/workshop/content/431960"
OFFICIAL = HOME / ".local/share/Steam/steamapps/common/wallpaper_engine/projects/defaultprojects"
LWE = HOME / ".local/bin/linux-wallpaperengine-web"
WWCTL = ROOT / "tests/waywallen/wwctl.py"
WARM, GAP = 8, 2  # s hasta la primera captura (carga + intro) y entre las dos capturas
STAMP = time.strftime("%Y-%m-%d_%H%M")
OUT = ROOT / "tests/results" / STAMP
SHOTS = ROOT / "tests/results/shots" / STAMP  # pesado: fuera de git (.gitignore)

app = runpy.run_path(str(ROOT / "we-mpvpaper"), run_name="compare_engines")


def inventory():
    items = []
    dirs = [(d.name, d) for d in sorted(WS.iterdir())]
    if OFFICIAL.is_dir():
        dirs += [(str(d), d) for d in sorted(OFFICIAL.iterdir())]
    for key, d in dirs:
        try:
            project = json.loads((d / "project.json").read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        kind = str(project.get("type", "")).lower()
        if not kind:
            kind = {".json": "scene", ".html": "web", ".htm": "web", ".mp4": "video", ".webm": "video"}.get(
                Path(str(project.get("file", ""))).suffix.lower(), "")
        if kind in ("scene", "web", "video"):
            items.append({"key": key, "dir": str(d), "kind": kind, "title": project.get("title") or d.name,
                          "file": str(d / project["file"]) if project.get("file") else None})
    return items


def grab(output, path):
    tmp = path.with_suffix(".png")
    subprocess.run(["grim", "-o", output, str(tmp)], check=True, timeout=10)
    img = Image.open(tmp).convert("RGB")
    img.thumbnail((960, 540))
    img.save(path, quality=80)
    tmp.unlink()
    return img


def metrics(a, b, empty, prev=None):
    # sin la franja de arriba: la barra de Noctalia sale en todas las capturas
    small = lambda i: i.convert("L").crop((0, i.height // 20, i.width, i.height)).resize((240, 128))  # noqa: E731
    sa, sb, se = small(a), small(b), small(empty)
    return {"std": round(ImageStat.Stat(sa).stddev[0], 1),
            "motion": round(ImageStat.Stat(ImageChops.difference(sa, sb)).mean[0], 2),
            "vs_empty": round(ImageStat.Stat(ImageChops.difference(sa, se)).mean[0], 2),
            # waywallen deja el último fotograma del fondo anterior si el nuevo no llega a dibujar
            "vs_prev": None if prev is None else round(ImageStat.Stat(ImageChops.difference(sa, small(prev))).mean[0], 2)}


def verdict(alive, m, log_errors):
    if not alive:
        return "crash"
    if m["vs_prev"] is not None and m["vs_prev"] < 1.0:
        return "nada (sigue el anterior)"
    if m["vs_empty"] < 1.5 or m["std"] < 6:
        return "nada"  # no dibujó o un color liso (como el azul fijo de Techno)
    return ("anima" if m["motion"] > 0.3 else "estático") + (" con errores" if log_errors else "")


def run_lwe(item, output, logf):
    if item["kind"] == "video":
        cmd = ["mpvpaper", "-o", "loop-file=inf panscan=1.0 no-audio hwdec=auto", output, item["file"]]
    else:
        cmd = [str(LWE), "--screen-root", output, "--bg", item["key"], "--layer", "background", "--silent",
               "--no-fullscreen-pause", "--scaling", "fill"]
    return subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT, cwd=str(HOME), start_new_session=True)


def wwctl(*args, timeout=60):
    return subprocess.run([sys.executable, str(WWCTL), *args], capture_output=True, text=True, timeout=timeout)


def ww_start(logf):
    procs = [subprocess.Popen(["flatpak", "run", "--env=WW_LOG=info", "org.waywallen.waywallen", "--ws-port",
                               "47321", "--no-ui", "--no-tray", "--no-restore"], stdout=logf, stderr=subprocess.STDOUT,
                              start_new_session=True)]
    time.sleep(6)
    procs.append(subprocess.Popen(["waywallen-layer-shell"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                  start_new_session=True))
    time.sleep(3)
    wwctl("scan", timeout=180)
    ids = {}
    for line in wwctl("list").stdout.splitlines():
        w = json.loads(line)
        # waywallen llega a Steam por ~/.steam/steam: se compara la carpeta ya resuelta. Los web
        # apuntan a la carpeta; scene/video, a un archivo dentro de ella.
        res = w["resource"]
        ids[os.path.realpath(res if os.path.isdir(res) else os.path.dirname(res))] = w["id"]
    return procs, ids


def ww_id(item, ids):
    return ids.get(os.path.realpath(item["dir"]))


def clear_output(output):
    """Espera a que no quede nada dibujando en la salida (clear-all del plugin es asíncrono)."""
    patterns = [f"^mpvpaper .* {output} ", f"linux-wallpaperengine.* --screen-root {output} "]
    end = time.time() + 10
    while time.time() < end and any(subprocess.run(["pgrep", "-f", p], capture_output=True).returncode == 0
                                    for p in patterns):
        time.sleep(0.5)
    for p in patterns:
        subprocess.run(["pkill", "-f", p])
    time.sleep(1.5)


def stop(p):
    if p and p.poll() is None:
        os.killpg(p.pid, signal.SIGTERM)
        try:
            p.wait(5)
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGKILL)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", default="DP-2")
    ap.add_argument("--engines", default="lwe,ww")
    ap.add_argument("--only", default="")
    ap.add_argument("--warm", type=int, default=WARM, help="s antes de la primera captura")
    a = ap.parse_args()
    only = set(filter(None, a.only.split(",")))
    items = [i for i in inventory() if not only or i["key"] in only or Path(i["dir"]).name in only]
    OUT.mkdir(parents=True, exist_ok=True)
    SHOTS.mkdir(parents=True, exist_ok=True)
    (SHOTS / "logs").mkdir(exist_ok=True)

    # La salida en limpio: sin el video de mpvpaper, sin linux-wallpaperengine y sin el fondo de Noctalia encima.
    plugins, outs = app["enabled_plugins"](), app["outputs"]()
    prev_mpv = app["read_json"](app["MPV_STATE"], {}).get("assignments", {})
    app["release_mpv"](a.output, outs, plugins)
    app["release_we"](a.output, plugins)
    clear_output(a.output)
    # después de limpiar: al soltar la salida, el plugin de mpvpaper vuelve a activar el fondo de Noctalia
    app["nmsg"]("plugin", f"{app['WE_PLUGIN']}:service", "all", "host-hide", a.output)
    time.sleep(1.5)
    empty = grab(a.output, SHOTS / "empty.jpg")

    results = {i["key"]: {**i} for i in items}
    ww_procs = []
    try:
        for engine in a.engines.split(","):
            ids = {}
            if engine == "ww":
                ww_procs, ids = ww_start(open(SHOTS / "logs/waywallen-daemon.log", "w"))
            prev = None
            for n, item in enumerate(items, 1):
                tag = re.sub(r"[^\w.-]", "_", Path(item["dir"]).name)
                log_path = SHOTS / f"logs/{engine}-{tag}.log"
                proc, alive, started = None, True, time.time()
                with open(log_path, "w") as logf:
                    if engine == "lwe":
                        proc = run_lwe(item, a.output, logf)
                    else:
                        wid = ww_id(item, ids)
                        if wid is None:
                            results[item["key"]][engine] = {"verdict": "no lo encuentra"}
                            print(f"[{engine} {n}/{len(items)}] {item['kind']:5} {item['title'][:40]:40} no lo encuentra",
                                  flush=True)
                            continue
                        mark = (SHOTS / "logs/waywallen-daemon.log").stat().st_size
                        r = wwctl("apply", wid, a.output)
                        alive = r.returncode == 0
                    time.sleep(a.warm)
                    alive = alive and (proc is None or proc.poll() is None)
                    shot_a = grab(a.output, SHOTS / f"{engine}-{tag}-a.jpg")
                    time.sleep(GAP)
                    alive = alive and (proc is None or proc.poll() is None)
                    shot_b = grab(a.output, SHOTS / f"{engine}-{tag}-b.jpg")
                    stop(proc)
                if engine == "ww":
                    with open(SHOTS / "logs/waywallen-daemon.log", errors="replace") as f:
                        f.seek(mark)
                        log_path.write_text(f.read())
                log = log_path.read_text(errors="replace")
                errors = sorted(set(re.findall(r"(?:ERROR\]?|frame failed|Failed to setup object|panic)[^\n]{0,140}",
                                               log)))[:8]
                m = metrics(shot_a, shot_b, empty, prev)
                prev = shot_b
                results[item["key"]][engine] = {"verdict": verdict(alive, m, errors), **m, "errors": errors,
                                                "secs": round(time.time() - started, 1), "warm": a.warm,
                                                "shot": f"shots/{STAMP}/{engine}-{tag}-a.jpg"}
                print(f"[{engine} {n}/{len(items)}] {item['kind']:5} {item['title'][:40]:40} "
                      f"{results[item['key']][engine]['verdict']}", flush=True)
                (OUT / "results.json").write_text(json.dumps(results, indent=1, ensure_ascii=False))
            for p in ww_procs:
                stop(p)
            ww_procs = []
    finally:
        for p in ww_procs:
            stop(p)
        app["nmsg"]("plugin", f"{app['WE_PLUGIN']}:service", "all", "host-show", a.output)
        if prev_mpv:
            app["set_mpv_assignments"](prev_mpv)
        report(results)


def report(results):
    compat = app["read_json"](HOME / ".cache/we-mpvpaper/compat.json", {})
    rows = []
    for key, r in sorted(results.items(), key=lambda kv: (kv[1]["kind"], kv[1]["title"].lower())):
        cells = []
        for engine in ("lwe", "ww"):
            e = r.get(engine)
            if not e:
                cells.append("<td>—</td>")
                continue
            img = f'<img loading="lazy" src="../{e["shot"]}">' if e.get("shot") else ""
            err = "<br>".join(html.escape(x) for x in e.get("errors", []))
            cells.append(f'<td class="{e["verdict"].split()[0]}">{img}<b>{html.escape(e["verdict"])}</b>'
                         f'<small> std {e.get("std")} · mov {e.get("motion")}</small><div class="err">{err}</div></td>')
        rows.append(f'<tr><td><b>{html.escape(r["title"])}</b><br><small>{r["kind"]} · {html.escape(Path(key).name)}'
                    f' · compat: {compat.get(key, {}).get("status", "?")}</small></td>{"".join(cells)}</tr>')
    (OUT / "report.html").write_text(f"""<!doctype html><meta charset="utf-8"><title>Comparación de motores {STAMP}</title>
<style>body{{font:14px system-ui;background:#111;color:#ddd;margin:16px}}table{{border-collapse:collapse;width:100%}}
td{{border-bottom:1px solid #333;padding:8px;vertical-align:top;width:40%}}td:first-child{{width:20%}}
img{{width:100%;display:block;margin-bottom:4px}}.crash b,.nada b{{color:#ff6b6b}}.anima b{{color:#6bdc8b}}
.estático b{{color:#e8c25a}}.err{{color:#f99;font:11px monospace;white-space:pre-wrap}}small{{color:#999}}</style>
<h1>Comparación de motores · {STAMP}</h1><p>lwe = linux-wallpaperengine/mpvpaper · ww = waywallen/OWE</p>
<table><tr><th>Fondo</th><th>lwe</th><th>ww</th></tr>{"".join(rows)}</table>""")


if __name__ == "__main__":
    main()
