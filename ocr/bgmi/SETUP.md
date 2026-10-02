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

## 3. The Apps Script — TWO scripts, one per sheet

Two spreadsheets, and they each get their **own** script, their own
deployment and their own URL:

| file | paste into | writes |
|---|---|---|
| `bgmi_alive_push.gs` | **CODM STATS** (live) | ticks Q–T, elims in U |
| `bgmi_results_push.gs` | **SCORESHEET** | finishes in G, rank in I |

Neither script can reach the other's spreadsheet, and that is the point.
A script bound to a sheet reads and writes it with no extra permission;
reaching a *different* file by id needs a broader scope, and when that
scope is missing the failure arrives mid-match as a permission error.

### 3a. The alive script

1. **CODM STATS → Extensions → Apps Script**, paste `bgmi_alive_push.gs`, Save.
2. Run **`authorize`**. The part to read is the mapping:

```
   slot  3  ->  row 5    MYT ESP
   slot 10  ->  row 12   NEMESIS
   slot 18  ->  row 20   LIZARD G
```

3. Check the team beside each slot is the team the game shows in that
   slot. Both defaults are already set for this sheet:
   `SLOT_COLUMN = "V"`, `TEAM_NAME_COLUMN = "P"`.
4. **Deploy → New deployment → Web app → Execute as: Me → Anyone with
   the link.** Copy the URL — this is the **Alive webhook**.

> **Column V holds the slots** (3…18) and rows are read from it, so the
> sheet can be reordered without breaking anything.
>
> **Do not point `TEAM_NAME_COLUMN` at D.** D is a live leaderboard —
> it sorts itself by points, and two runs an hour apart gave two
> completely different orders. P is the fixed, slot-ordered column and
> matches the scoresheet on all sixteen teams. Checking a mapping
> against a column that re-sorts itself tells you nothing.

### 3b. The results script

1. **SCORESHEET → Extensions → Apps Script**, paste `bgmi_results_push.gs`, Save.
2. Run **`findTheTab`** first. This workbook has seventeen tabs; it scans
   them and names the ones actually laid out as game blocks, with gids.
3. Set `SHEET_GID` to the one it found, Save, run **`authorize`** to
   confirm the GAME lines read the right slots.
4. Deploy as above. Copy the URL — this is the **Results webhook**.

> The gid in a pasted URL is whichever tab was open at the time. That is
> how `MVP.D3` got configured once and reported its column D as
> `0, M1, KILL`. `findTheTab` exists so this is one run instead of
> seventeen guesses.

> After **any** later edit to either script: **Deploy → Manage
> deployments → pencil → Version: New version → Deploy.** The web app
> serves the *deployed* version, not what is saved in the editor.

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
