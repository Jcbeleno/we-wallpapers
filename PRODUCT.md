# Product

<!-- impeccable:product-schema 1 -->

## Platform

native-linux (GTK4 + libadwaita, Python). Not web/ios/android: the HTML design detector does not apply.

## Users
One user: the owner of this machine (Arch/CachyOS, niri Wayland compositor + Noctalia shell, two monitors DP-1 2560x1440 and DP-2 1920x1080). Spanish-speaking; the UI is in Spanish. They own Wallpaper Engine on Steam and want its wallpapers on Linux.

## Product Purpose
WE Wallpapers lets them find, subscribe to, configure and apply Wallpaper Engine wallpapers on their Linux desktop without Windows: video wallpapers via mpvpaper, scene and web wallpapers via a patched linux-wallpaperengine, presets mapped to their base wallpaper. Success: picking and applying a wallpaper (installed or from the Workshop) feels as quick as in Wallpaper Engine itself, and nothing the app shows lies about whether it will work.

## Positioning
The only front end that joins three things on this desktop: the Steam Workshop (browse + subscribe through the running Steam client, appid 431960, like Wallpaper Engine's own UI does), real per-wallpaper compatibility evidence (compat test + engine screenshots), and Noctalia integration (own plugin for scene/web, noctalia/mpvpaper for video).

## Operating Context
- Opened from the launcher on demand; used in short sessions, then closed. Wallpapers keep running through the Noctalia plugins after the app exits.
- Steam may or may not be running. Browsing the Workshop works without it (public pages); subscribing needs it.
- Workshop items land in ~/.local/share/Steam/steamapps/workshop/content/431960 (downloaded by Steam).
- Per-wallpaper user properties live in ~/.config/we-mpvpaper/props/<id>.json; compat results and engine screenshots in ~/.cache/we-mpvpaper.

## Capabilities and Constraints
- Wallpaper types (from Wallpaper Engine 2.8): Scene (scene.pkg / scene.json), Video (mp4/webm...), Web (index.html + JS API), Application (Windows .exe, not supported on Linux), Preset (project.json with `dependency` + `preset` values for another wallpaper).
- Scene/web support is partial (linux-wallpaperengine): the UI must keep showing compat status honestly.
- Wallpaper Engine's private backend (wallpaperengineapi.com) is not used.
- Python stdlib + PyGObject only; no new dependencies.

## Product Principles
1. Honest state: never imply a wallpaper works, is subscribed, or is downloading when it is not.
2. One click from seeing to applying; configuration is inline, never a modal detour.
3. Native first: libadwaita patterns and the system theme so it sits naturally next to Noctalia.
4. Fast to open, cheap to keep open: thumbnails and network work never block the UI thread.
