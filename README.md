# 3-1.video — Triple Vertical Player

1 × 3 synced video wall for 9:16 vertical videos. One file plays across all
three panes in sync — column 1 is the master, columns 2–3 stay frame-locked.

## Run (Windows)

```bat
pip install -r requirements.txt
python app.py
```

## Install on any laptop (no Python needed)

See `README-INSTALL.txt`. Short version:

- **Setup:** run `Output\TripleVerticalPlayer-Setup.exe` (from `Build-Installer.bat`)
- **Portable:** unzip `TripleVerticalPlayer-Portable.zip`, double-click `Install.bat`
  (or just run `TripleVerticalPlayer.exe` directly)

First run may ask for the free Microsoft Edge WebView2 Runtime (one-time).

## Build the exe (developer PC)

```bat
build_exe.bat        - builds dist\TripleVerticalPlayer.exe, never opens the app
Build-Installer.bat  - builds exe + Portable ZIP + Setup.exe (if Inno Setup 6 installed)
```

Output: `dist\TripleVerticalPlayer.exe` (no install needed, Windows 10/11).

## Controls

| Button / key | What it does |
|---|---|
| Open video(s) | Pick file(s) — a single file queues its whole folder |
| Open photo | Show one original photo in all three columns, using the video Fit setting |
| Open photo folder | Browse photos in the folder and its subfolders with Prev / Next or N / P |
| Open folder | Every video in the folder plays continuously |
| Play (Space) | Play / pause all 3 panes |
| Sync | Force-resync slaves to master |
| Fullscreen (F) | Borderless fullscreen, UI auto-hides |
| Hide UI (H) | Hide panels for a pure video wall |
| Same view | Reset zoom/pan so all 3 show the identical frame |
| Prev / Next (P / N) | Step through the queue |
| Loop | ON = repeat the same video · OFF = play queue once |

Photos and videos are loaded from their original files without re-encoding.
In 3VC mode, photos appear in all three columns and use the same Fit setting as
videos (Fill by default). Choose Full frame to keep the entire photo visible;
rendered size follows the pane and monitor resolution.
