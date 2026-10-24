#!/usr/bin/env python3
"""Prueba cada fondo scene/web con linux-wallpaperengine y guarda en compat.json si
funciona, funciona a medias (con los motivos) o no arranca, más una captura real que
sirve de miniatura. Incluye los fondos que trae Wallpaper Engine (clave = ruta de la
carpeta, captura shot-official-<nombre>.png).
Uso: compat-test.py [salida] [ids...]   (con ids, los vuelve a probar aunque no hayan cambiado)
     compat-test.py --all               (vuelve a probar todos, p. ej. tras actualizar el motor)"""
import json, os, re, struct, subprocess, sys, tempfile, time
from pathlib import Path

WS = Path.home() / ".local/share/Steam/steamapps/workshop/content/431960"
OFFICIAL = Path.home() / ".local/share/Steam/steamapps/common/wallpaper_engine/projects/defaultprojects"
args = [a for a in sys.argv[1:] if a != "--all"]
FORCE_ALL = "--all" in sys.argv
OUT = Path(args[0]) if args else Path.home() / ".cache/we-mpvpaper"
ONLY = set(args[1:])
OUTPUT = os.environ.get("WE_TEST_OUTPUT", "DP-1")
RUN_SECS = 12
# Versión parcheada (~/Proyectos/linux-wallpaperengine, rama web-fixes) si existe.
ENGINE = Path.home() / ".local/bin/linux-wallpaperengine-web"
VERSION = 3  # sube cuando cambian los criterios: obliga a repetir la prueba

# Mensajes del motor -> motivo que entiende el usuario. Cualquiera hace el fondo "parcial".
ISSUES = [
    (r"Unsupported puppet model header", "Usa animación puppet warp en un formato que el motor aún no soporta"),
    (r"VolumeLight objects are not supported", "Usa luces volumétricas (no soportadas)"),
    (r"createLayer is not supported", "Crea capas desde un script (no se dibujan)"),
    (r"ScriptEngine \[", "Alguno de sus scripts falla"),
    (r"unit parsing Failed|Linking Failed|Failed to setup object", "Algún efecto no se puede dibujar"),
]
FATAL = [
    (r"orthogonal projection|Projection must have a width", "Escena 3D con cámara en perspectiva (no soportada)"),
    (r"Project type missing|Unsupported project type", "Tipo de proyecto no soportado"),
]


def read_str(f):
    n = struct.unpack("<I", f.read(4))[0]
    return f.read(n).decode("utf-8", "replace")


def scene_json(d, project):
    """scene.json suelto o dentro de scene.pkg (formato PKGV)."""
    name = project.get("file") or "scene.json"
    if (d / name).is_file():
        return json.loads((d / name).read_text(encoding="utf-8-sig"))
    for pkg in d.glob("*.pkg"):
        with open(pkg, "rb") as f:
            read_str(f)
            count = struct.unpack("<I", f.read(4))[0]
            entries = [(read_str(f), *struct.unpack("<II", f.read(8))) for _ in range(count)]
            base = f.tell()
            for entry, off, size in entries:
                if entry == name:
                    f.seek(base + off)
                    return json.loads(f.read(size).decode("utf-8-sig"))
    return None


def unsupported_objects(scene):
    """Objetos que el motor no dibuja (los grupos sin tipo sí están soportados: solo agrupan).
    Las texturas de usuario sin elegir (iconos de accesos directos, imagen personalizada)
    no cuentan: Wallpaper Engine tampoco muestra nada hasta que se configuran."""
    issues = []
    objects = (scene or {}).get("objects") or []
    if any(o.get("model") for o in objects):
        issues.append("Tiene modelos 3D (no se dibujan)")
    if any(o.get("shape") for o in objects):
        issues.append("Tiene formas 3D (no se dibujan)")
    if any(o.get("light") for o in objects):
        issues.append("Usa luces de escena (no soportadas)")
    return issues


def projection(d, project):
    """(ancho, alto) de la escena, o None si es 3D/auto."""
    try:
        ortho = (scene_json(d, project) or {}).get("general", {}).get("orthogonalprojection")
        if isinstance(ortho, dict) and ortho.get("width") and ortho.get("height"):
            return [int(ortho["width"]), int(ortho["height"])]
    except (OSError, ValueError, struct.error):
        pass
    return None


OUT.mkdir(parents=True, exist_ok=True)
db_path = OUT / "compat.json"
db = json.loads(db_path.read_text()) if db_path.is_file() else {}

# (clave en compat.json, carpeta, nombre de la captura)
candidates = [(d.name, d, d.name) for d in sorted(WS.iterdir())]
if OFFICIAL.is_dir():
    candidates += [(str(d), d, f"official-{d.name}") for d in sorted(OFFICIAL.iterdir())]

for key, d, shot_name in candidates:
    if ONLY and key not in ONLY and d.name not in ONLY:
        continue
    try:
        project = json.loads((d / "project.json").read_text(encoding="utf-8-sig"))
    except Exception:
        continue
    kind = str(project.get("type", "")).lower()
    if not kind:  # como Wallpaper Engine: el tipo sale de la extensión del archivo principal
        kind = {".json": "scene", ".html": "web", ".htm": "web"}.get(Path(str(project.get("file", ""))).suffix.lower(), "")
    if kind not in ("scene", "web"):
        continue
    mtime = int((d / "project.json").stat().st_mtime)
    old = db.get(key, {})
    if not ONLY and not FORCE_ALL and old.get("mtime") == mtime and old.get("version") == VERSION:
        continue
    shot = OUT / f"shot-{shot_name}.png"
    shot.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory() as cwd:
        t0 = time.time()
        engine = str(ENGINE) if ENGINE.is_file() else "linux-wallpaperengine"
        p = subprocess.run(["timeout", str(RUN_SECS), engine, "--screen-root", OUTPUT,
                            "--bg", key, "--layer", "background", "--silent", "--no-fullscreen-pause",
                            "--screenshot", str(shot)], cwd=cwd,
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, errors="ignore")
    alive = p.returncode == 124  # siguió vivo hasta el timeout
    if kind == "web":
        shot.unlink(missing_ok=True)  # la captura del motor sale corrupta con fondos web
    log = p.stderr
    issues = [text for rx, text in ISSUES if re.search(rx, log)]
    if kind == "scene":
        try:
            issues += unsupported_objects(scene_json(d, project))
        except (OSError, ValueError, struct.error):
            pass
    if not alive:
        issues = [text for rx, text in FATAL if re.search(rx, log)] or issues
    status = "ok" if alive and not issues else "partial" if alive else "fail"
    db[key] = {"ok": alive, "status": status, "issues": issues,
               "code": p.returncode, "shot": shot.is_file(), "mtime": mtime, "version": VERSION,
               "type": kind, "projection": projection(d, project) if kind == "scene" else None,
               "secs": round(time.time() - t0, 1),
               "error": "" if alive else (log.strip().splitlines() or [""])[-1][:200]}
    db_path.write_text(json.dumps(db, indent=1, ensure_ascii=False))
    print(f"{shot_name} {kind:5} {status.upper():7} code={p.returncode} {'; '.join(issues)[:160]}", flush=True)
