--!nonstrict
-- Wallpaper Engine (scene/web) - servicio headless.
-- Lanza linux-wallpaperengine (un proceso para todas las salidas que muestran el mismo
-- scene, así se carga y renderiza una sola vez; los fondos web van uno por salida),
-- desactiva el fondo de Noctalia en esas salidas para que se vea, y usa una captura
-- del fondo como imagen estática (colores del tema y respaldo si el renderizador se cae).

local HOME = noctalia.getenv("HOME") or ""
-- Ruta fija (no pluginDataDir) para que la app WE Wallpapers pueda leer qué está activo.
local DATA_DIR = HOME .. "/.local/state/we-mpvpaper"
local STATE_FILE = DATA_DIR .. "/assignments.json"
local CACHE_DIR = HOME .. "/.cache/we-mpvpaper/live"
-- linux-wallpaperengine parcheado (fondos web, JSON tolerante...), compilado desde
-- ~/Proyectos/linux-wallpaperengine (rama web-fixes). Si no existe, el del sistema.
local PATCHED_ENGINE = HOME .. "/.local/bin/linux-wallpaperengine-web"
local WORKSHOP_DIRS = {
  HOME .. "/.local/share/Steam/steamapps/workshop/content/431960",
  HOME .. "/.steam/steam/steamapps/workshop/content/431960",
}

-- connector ("*" = todas) -> { id = "123", props = { key = value } }
local assignments = {}
local lastAssignments = {}
-- Salidas donde dibuja waywallen (WE Wallpapers): el fondo de Noctalia, en la misma capa, lo taparía.
-- Se guarda y se reaplica en cada update: soltar un video con el plugin de mpvpaper lo vuelve a activar.
local hostHidden = {}
-- salida física -> { id, startedAt, paused, shot, shotApplied, group, members, proc }
-- Las salidas de un mismo proceso comparten `proc`.
local running = {}
-- Se incrementa en cada parada/arranque de una salida: un arranque cuyo pkill
-- asíncrono termina después de otra petición no lanza un segundo proceso.
local generation = {}
local procCounter = 0
local available = false
local tick = 0

-- ── Utilidades ───────────────────────────────────────────────────────────────

local function shellQuote(s)
  return "'" .. tostring(s):gsub("'", "'\\''") .. "'"
end

-- Ajustes del motor: los edita la app WE Wallpapers (solo guarda lo que difiere de los
-- valores por defecto) y avisa con el evento IPC «config».
local ENGINE_FILE = HOME .. "/.config/we-mpvpaper/engine.json"
local engineCfg = {}

local function loadEngineCfg()
  local body = noctalia.readFile(ENGINE_FILE)
  local data = body and noctalia.json.decode(body)
  engineCfg = type(data) == "table" and data or {}
end

local function cfg(key, default)
  local v = engineCfg[key]
  if v == nil then return default end
  return v
end

local function persist()
  noctalia.mkdirAll(DATA_DIR)
  local body = noctalia.json.encode({ assignments = assignments, lastAssignments = lastAssignments,
                                      hostHidden = hostHidden }, true)
  if body then noctalia.writeFile(STATE_FILE, body) end
end

local function load()
  local body = noctalia.readFile(STATE_FILE)
  if not body then return end
  local data = noctalia.json.decode(body)
  if type(data) == "table" then
    assignments = data.assignments or {}
    lastAssignments = data.lastAssignments or {}
    hostHidden = type(data.hostHidden) == "table" and data.hostHidden or {}
  end
end

local function publish()
  noctalia.state.set("assignments", assignments)
  local r = {}
  for name, info in pairs(running) do r[name] = info.id end
  noctalia.state.set("running", r)
end

local function physicalOutputs(connector)
  local out = {}
  for _, o in ipairs(noctalia.outputs()) do
    if connector == "*" or o.name == connector then table.insert(out, o.name) end
  end
  return out
end

-- Qué asignación aplica a una salida física: la suya o la de "*".
local function assignmentFor(name)
  return assignments[name] or assignments["*"]
end

local function wallpaperTitle(id)
  for _, dir in ipairs(WORKSHOP_DIRS) do
    local body = noctalia.readFile(dir .. "/" .. id .. "/project.json")
    if body then
      local data = noctalia.json.decode(body)
      if type(data) == "table" and data.title then return tostring(data.title) end
    end
  end
  return id
end

-- Patrón de pkill que coincide con el proceso que incluye esa salida exacta. "[l]inux"
-- evita que coincida con el propio sh que ejecuta el pkill (su línea de comandos
-- contiene el patrón), que si no moriría antes de terminar una cadena de pkills.
local function pkillPattern(name)
  local escaped = name:gsub("([%.%*%+%?%(%)%[%]%^%$|\\{}])", "\\%1")
  return shellQuote("[l]inux-wallpaperengine .*--screen-root " .. escaped .. " ")
end

-- SIGTERM y, si a los 3 s sigue vivo, SIGKILL: un motor con un hilo atascado no termina con TERM y
-- se quedaba dibujando y sonando junto al nuevo.
local function killCommand(name)
  local p = pkillPattern(name)
  return "pkill -CONT -f -- " .. p .. " ; pkill -TERM -f -- " .. p
    .. " ; for i in $(seq 30); do pgrep -f -- " .. p .. " >/dev/null || break; sleep 0.1; done"
    .. " ; pkill -KILL -f -- " .. p .. " ; true"
end

local function propValue(v)
  if type(v) == "boolean" then return v and "1" or "0" end
  return tostring(v)
end

local function buildCommand(names, a, shot)
  local engine = "linux-wallpaperengine"
  if noctalia.fileExists(PATCHED_ENGINE) then engine = PATCHED_ENGINE end
  local args = { engine }
  -- Ajuste elegido para este fondo en WE Wallpapers, si no el general. El motor aplica --scaling a la
  -- --screen-root anterior: va detrás de cada salida. Puesto una sola vez al final, solo valía para la
  -- última, y con ajustes distintos el motor no comparte la escena: una por pantalla, cada una con su audio.
  local scaling = a.scaling or cfg("scaling", "fill")
  for _, name in ipairs(names) do
    table.insert(args, "--screen-root"); table.insert(args, name)
    table.insert(args, "--bg"); table.insert(args, a.id)
    if scaling ~= "default" then
      table.insert(args, "--scaling"); table.insert(args, scaling)
    end
  end
  table.insert(args, "--layer"); table.insert(args, "background")
  table.insert(args, "--fps"); table.insert(args, tostring(cfg("fps", 30)))
  -- Silencio elegido para este fondo en WE Wallpapers, si no el general.
  local mute = a.mute
  if mute == nil then mute = cfg("mute", true) end
  if mute then
    table.insert(args, "--silent")
  else
    table.insert(args, "--volume"); table.insert(args, tostring(cfg("volume", 15)))
    if not cfg("automute", true) then table.insert(args, "--noautomute") end
  end
  if not cfg("audio_reactive", true) then table.insert(args, "--no-audio-processing") end
  local fp = cfg("fullscreen_pause", "any")
  if fp == "off" then table.insert(args, "--no-fullscreen-pause")
  elseif fp == "active" then table.insert(args, "--fullscreen-pause-only-active") end
  if not cfg("mouse", true) then table.insert(args, "--disable-mouse") end
  if not cfg("parallax", true) then table.insert(args, "--disable-parallax") end
  if not cfg("particles", true) then table.insert(args, "--disable-particles") end
  if type(a.props) == "table" then
    for k, v in pairs(a.props) do
      table.insert(args, "--set-property"); table.insert(args, k .. "=" .. propValue(v))
    end
  end
  -- La captura del motor sale corrupta con fondos web: solo para scenes.
  if a.kind ~= "web" then
    table.insert(args, "--screenshot"); table.insert(args, shot)
  end

  local parts = {}
  for _, s in ipairs(args) do table.insert(parts, shellQuote(s)) end
  -- pkillPattern exige un espacio tras el nombre para distinguir DP-1 de DP-10: cada
  -- "--screen-root <name>" va seguido de "--bg".
  return table.concat(parts, " ")
end

-- ── Procesos ─────────────────────────────────────────────────────────────────

local function stopOutput(name, restoreHost, callback)
  generation[name] = (generation[name] or 0) + 1
  local info = running[name]
  running[name] = nil
  local cmd = killCommand(name)
  noctalia.runAsync(cmd, function()
    if restoreHost and not hostHidden[name] then noctalia.setWallpaperEnabled(name, true) end
    if callback then callback(info) end
  end)
end

-- Clave de agrupación: las salidas con el mismo scene y propiedades comparten proceso.
-- Los fondos web van solos (CEF por salida es lo probado).
local function groupKey(name, a)
  if a.kind == "web" then return "web|" .. name end
  return (a.kind or "") .. "|" .. a.id .. "|" .. (a.scaling or "") .. "|" .. tostring(a.mute) .. "|"
    .. (noctalia.json.encode(a.props or {}) or "")
end

local function startGroup(names, a, key)
  if not available or #names == 0 then return end
  table.sort(names)
  local members = table.concat(names, ",")
  local stopCmd = {}
  for _, name in ipairs(names) do
    generation[name] = (generation[name] or 0) + 1
    running[name] = nil
    table.insert(stopCmd, killCommand(name))
  end
  local expected = {}
  for _, name in ipairs(names) do expected[name] = generation[name] end

  noctalia.runAsync(table.concat(stopCmd, " ; "), function()
    for _, name in ipairs(names) do
      if generation[name] ~= expected[name] then return end
    end
    noctalia.mkdirAll(CACHE_DIR)
    local shots = {}
    for _, name in ipairs(names) do
      shots[name] = CACHE_DIR .. "/" .. name .. ".png"
      noctalia.removeFile(shots[name])
    end
    -- El motor sustituye {screen} por cada salida al guardar la captura.
    local cmd = buildCommand(names, a, CACHE_DIR .. "/{screen}.png")
    noctalia.runAsync("cd " .. shellQuote(CACHE_DIR) .. " && exec " .. cmd .. " >/dev/null 2>&1 &", function() end)
    procCounter += 1
    local now = noctalia.nowMs()
    for _, name in ipairs(names) do
      running[name] = { id = a.id, startedAt = now, shot = shots[name], shotApplied = false,
        group = key, members = members, proc = procCounter }
      noctalia.setWallpaperEnabled(name, false)
    end
    publish()
  end)
end

-- Reconcilia los procesos con las asignaciones: agrupa las salidas, reinicia solo los
-- grupos que cambiaron y para las salidas sin fondo.
local function applyAll()
  local groups, order = {}, {}
  for _, o in ipairs(noctalia.outputs()) do
    local a = assignmentFor(o.name)
    if a then
      local key = groupKey(o.name, a)
      if not groups[key] then
        groups[key] = { names = {}, a = a }
        table.insert(order, key)
      end
      table.insert(groups[key].names, o.name)
    elseif running[o.name] then
      stopOutput(o.name, true)
    end
  end
  for _, key in ipairs(order) do
    local g = groups[key]
    table.sort(g.names)
    local members = table.concat(g.names, ",")
    local upToDate = true
    for _, name in ipairs(g.names) do
      local info = running[name]
      if not info or info.group ~= key or info.members ~= members then upToDate = false end
    end
    if not upToDate then startGroup(g.names, g.a, key) end
  end
  publish()
end

-- Reinicia todo aunque no haya cambiado (p. ej. tras cambiar la configuración).
local function restartAll()
  for name in pairs(running) do running[name].group = nil end
  applyAll()
end

local function snapshot()
  if next(assignments) == nil then return end
  lastAssignments = {}
  for k, v in pairs(assignments) do lastAssignments[k] = v end
end

local function clear(connector)
  snapshot()
  if connector == nil or connector == "" or connector == "*" then
    assignments = {}
  else
    -- Si había un fondo para todas las salidas, se mantiene en las demás.
    local all = assignments["*"]
    if all then
      assignments["*"] = nil
      for _, name in ipairs(physicalOutputs("*")) do
        if name ~= connector then assignments[name] = all end
      end
    end
    assignments[connector] = nil
  end
  persist()
  -- applyAll para las salidas sin fondo y reinicia las que compartían proceso con ellas
  applyAll()
end

local function set(connector, id, props, kind, scaling, mute)
  if type(id) ~= "string" or id == "" then return end
  if type(mute) ~= "boolean" then mute = nil end  -- nil = el ajuste general
  connector = (type(connector) == "string" and connector ~= "") and connector or "*"
  if connector == "*" then
    assignments = {}
  elseif assignments["*"] then
    local all = assignments["*"]
    assignments["*"] = nil
    for _, name in ipairs(physicalOutputs("*")) do assignments[name] = all end
  end
  assignments[connector] = { id = id, props = props or {}, kind = kind,
    scaling = type(scaling) == "string" and scaling ~= "" and scaling or nil,
    mute = mute }
  persist()
  applyAll()
end

-- Vigila los procesos: detecta caídas, aplica la captura como fondo estático y
-- vuelve a ocultar el fondo de Noctalia si otro plugin lo reactivó.
local function supervise()
  local now = noctalia.nowMs()
  for name, info in pairs(running) do
    local age = now - info.startedAt
    if not info.shotApplied and age > 3000 and noctalia.fileExists(info.shot) then
      info.shotApplied = true
      noctalia.setWallpaper(name, info.shot)
    end
    if age < 20000 then
      noctalia.setWallpaperEnabled(name, false)
    end
    if age > 2500 and not info.paused then
      noctalia.processMatches(function(alive)
        if alive or running[name] ~= info then return end
        -- Se cayó el proceso: afecta a todas las salidas que lo compartían.
        snapshot()
        for other, otherInfo in pairs(running) do
          if otherInfo.proc == info.proc then
            running[other] = nil
            noctalia.setWallpaperEnabled(other, true)
          end
        end
        -- No reintentar en el próximo arranque: se volvería a caer.
        for conn, a in pairs(assignments) do
          if a.id == info.id then assignments[conn] = nil end
        end
        persist()
        noctalia.notifyError("Wallpaper Engine", noctalia.tr("crashed", { name = wallpaperTitle(info.id) }))
        publish()
      end, "linux-wallpaperengine", "--screen-root", name, "--bg", info.id)
    end
  end
end

-- ── Callbacks del host ───────────────────────────────────────────────────────

function update()
  for name in pairs(hostHidden) do noctalia.setWallpaperEnabled(name, false) end
  if not available then return end
  tick += 1
  supervise()
end

function onOutputsChanged()
  if not available then return end
  local present = {}
  for _, o in ipairs(noctalia.outputs()) do present[o.name] = true end
  -- una salida que desaparece puede compartir proceso con otras: se reagrupa todo
  for name in pairs(running) do
    if not present[name] then
      stopOutput(name, false)
      for _, info in pairs(running) do info.group = nil end
    end
  end
  applyAll()
end

function onConfigChanged()
  restartAll()
end

function onExit(signal, reason)
  for name in pairs(running) do
    noctalia.runAsync(killCommand(name))
    if reason == "disable" or reason == "uninstall" then noctalia.setWallpaperEnabled(name, true) end
  end
  running = {}
end

function onIpc(event, payload)
  if event == "set" then
    local data = noctalia.json.decode(payload or "")
    if type(data) ~= "table" then
      noctalia.log("wallpaper-engine: payload inválido para set: " .. tostring(payload))
      return
    end
    set(data.connector, tostring(data.id or ""), data.props, data.kind, data.scaling, data.mute)
  elseif event == "clear" or event == "clear-all" then
    clear(payload)
  elseif event == "reapply" then
    if next(assignments) == nil then
      for k, v in pairs(lastAssignments) do assignments[k] = v end
      persist()
    end
    restartAll()
  elseif event == "config" then
    loadEngineCfg()
    restartAll()
  elseif event == "host-hide" or event == "host-show" then
    -- Otro motor (waywallen) dibuja esa salida en la capa de fondo: el de Noctalia lo tapaba.
    if payload and payload ~= "" then
      hostHidden[payload] = event == "host-hide" or nil
      persist()
      noctalia.setWallpaperEnabled(payload, event == "host-show")
    end
  elseif event == "pause" or event == "resume" then
    local sig = event == "pause" and "STOP" or "CONT"
    for name, info in pairs(running) do
      if payload == nil or payload == "" or payload == name then
        info.paused = event == "pause"
        noctalia.runAsync("pkill -" .. sig .. " -f -- " .. pkillPattern(name))
      end
    end
  end
end

-- ── Arranque ─────────────────────────────────────────────────────────────────

noctalia.setUpdateInterval(2000)
load()
loadEngineCfg()
available = noctalia.commandExists("linux-wallpaperengine")
if available then
  applyAll()
else
  noctalia.notifyError("Wallpaper Engine", "linux-wallpaperengine no está instalado")
end
publish()
