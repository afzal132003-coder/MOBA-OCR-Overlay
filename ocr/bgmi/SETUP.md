# BGMI — setting it up

Do this once per machine. Takes about ten minutes, and most of it is the
Apps Script step.

---

## 1. Calibrate the capture region

Have the observer **team panel open in the game**, then:

```
ocr\bgmi\calibrate_bgmi.bat
```

It lists your screens and asks which one the game is on — this rig is
**screen 2**, the one at `(-1920, 0)`.

Then drag a box around **the game picture only**. Leave out the
BlueStacks title bar along the top and the toolbar down the right. If
either is included, every offset shifts by its width and nothing reads.

It reads the panel straight back and tells you what it found:

```
  whole cards       9
  players read      36, of which 21 alive
```

**Nine cards means it is right.** Zero means it isn't, and it names the
likely cause. Re-run it until you see nine.

## 2. Start the engine

```
ocr\start_bgmi.bat
```

Its own window, its own port (8767). It does not clash with the Free
Fire engine on 8765 or Dota 2 on 8766 — all three can run at once.

## 3. The Apps Script (the fiddly part)

Two spreadsheets are involved and they are **different files**:

| | what | where it is set |
|---|---|---|
| **Alive** | live table — tick boxes + elim count | `SPREADSHEET_ID`, already filled in |
| **Scoresheet** | per-game finishes and rank | `RESULTS_SPREADSHEET_ID`, **blank — you fill it** |

### 3a. Find the scoresheet's id

Open the scoresheet and look at its URL:

```
https://docs.google.com/spreadsheets/d/1AbCdEfGh...XyZ/edit#gid=0
                                      └──────┬──────┘
                                      this is the id
```

Everything between `/d/` and `/edit`. Copy it.

### 3b. Find the slot column on the ALIVE sheet

**This one matters more than it looks.** Your slots run **3 to 18**, not
1 to 16. The fallback assumes slot 1 sits on the first data row, so
without this every team lands three rows above where it belongs —
quietly, and consistently enough to look deliberate rather than broken.

Open the alive sheet. Look at rows **5 to 20**. Find the column showing
`3, 4, 5 … 18`. Note its letter — that is `SLOT_COLUMN`.

### 3c. Paste and set

1. Alive sheet → **Extensions → Apps Script**
2. Delete what's there, paste all of `ocr/bgmi/bgmi_sheet_push.gs`
3. Set these four near the top:

```js
var SLOT_COLUMN = "P";                    // ← 3b. NOT optional
var RESULTS_SPREADSHEET_ID = "1AbC...";   // ← 3a
var DATA_START_ROW = 5;                   // confirm against your sheet
var DATA_END_ROW = 20;
```

4. **Save** (Ctrl+S)

### 3d. Check it before deploying

Pick **`authorize`** in the function dropdown next to Run, press **Run**,
accept the permission prompt, then read the Execution log underneath.

It prints what it can actually see:

```
Slot column P reads: 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18
GAME 1  rows 4-19   slots: 3, 4, 5, ...
GAME 2  rows 25-40  slots: 3, 4, 5, ...
```

**Read those two lines.** If the slot column prints blanks, it is the
wrong column. If the game blocks print the wrong slots, the stride is
wrong for your sheet and I need to know.

If `SLOT_COLUMN` is blank it will shout at you here. That is deliberate.

### 3e. Deploy

**Deploy → New deployment → type: Web app → Execute as: Me → Who has
access: Anyone with the link → Deploy.** Copy the web app URL.

> After **any** later edit you must do **Deploy → Manage deployments →
> pencil → Version: New version → Deploy**. The web app serves the
> *deployed* version, not what is saved in the editor. An edit saved but
> not redeployed changes nothing, and it is a confusing hour.

## 4. Wire it to the dashboard

Dashboard → **BGMI** → *Push To The Sheet*:

- paste the **web app URL**
- **tab name** — blank uses the first tab
- **lobby size** — 16
- **Save Settings**, then **Push Now** to test

A good push says `Wrote 16 of 16 teams`. If it names unmatched slots,
`SLOT_COLUMN` or the row range is wrong.

## 5. Running a match

1. Open the observer team panel.
2. **Slide 1** — set *starts at slot* to `3`, press **Capture Slide 1**.
3. Scroll the panel. **Slide 2** — set *starts at slot* to `12`, press
   **Capture Slide 2**.
4. Repeat for a third slide if the lobby needs it.

Slides merge by slot. Re-capturing one replaces only itself, so a bad
frame never costs you the others. **Clear All** at the start of a new
match.

Tick *push automatically after every capture* and it goes to the sheet
each time the table changes.

### What the numbers you type are for

The slot you declare is the **source of truth**; the numbers on screen
are the **check**. If they disagree the capture is refused rather than
guessed at, because the wrong answer puts one team's kills on another
team's row.

It also teaches itself from what you declare — capturing the slide that
starts at 12 is what taught it the digit `2`, which nothing else could.

---

## Known gaps

- **Results push has no data source yet.** The plumbing and the
  scoresheet geometry are done; the result-screen reader is not built,
  so nothing fills it. Next job.
- **Two-digit kill counts are untested** — a player on 10+ finishes
  voids that team's total rather than reporting a wrong one.
- **Motion blur during a scroll is untested.**
