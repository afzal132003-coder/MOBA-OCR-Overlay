; Valorant Tab-Hold Macro
; ------------------------------------------------------------------------
; Press F5 to toggle holding Tab down continuously -- keeps the in-game
; scoreboard on screen (useful when a stream overlay/graphic needs the
; scoreboard visible without a player having to physically hold Tab).
; Press F5 again to release it.
;
; Requires AutoHotkey v2 (already installed on this machine at
; C:\Program Files\AutoHotkey\v2\AutoHotkey64.exe). Double-click this file
; to run it, or right-click -> "Run with AutoHotkey" if .ahk isn't
; associated with v2 by default.
; ------------------------------------------------------------------------

#Requires AutoHotkey v2.0
#SingleInstance Force

tabHeld := false

F5::{
    global tabHeld
    tabHeld := !tabHeld
    if (tabHeld) {
        Send("{Tab down}")
        ToolTip("TAB HOLD: ON")
    } else {
        Send("{Tab up}")
        ToolTip("TAB HOLD: OFF")
    }
    SetTimer(() => ToolTip(), -1000)
}

; Safety net -- if the script is closed (or the game/PC needs a reset)
; while Tab is being held down, make sure it actually gets released
; instead of staying stuck held in whatever window has focus next.
OnExit(ReleaseTabOnExit)
ReleaseTabOnExit(*) {
    Send("{Tab up}")
}
