# BGMI result screen — reading it, and working out whose rank is whose

Status: **planned, not built.** The alive-stats side is built and pushing
to the sheet; this is the post-match half.

---

## 1. Tags cannot be the join, and the screenshots prove it

The Free Fire engine identifies squads by the tag in their IGNs. That
worked there because Free Fire rosters tag consistently. BGMI does not,
and the operator was right to flag it before I built on it.

Taken from the five result-screen captures of one real match, counting
teams where **every** player shares one tag:

| | |
|---|---|
| Clean, all four share a tag | **6 of 16** — PILOTx, LGx, RRaven, TSx, LMEz, FLYNw |
| Mixed or no shared tag | **10 of 16** |

What the mixed ten actually look like:

```
rank 14   1857xValcan      777·STORMXxOP    AKzMohith7            no shared tag at all
rank  5   WEPAYxRIEEzOL    fyLYNX01         fyTROLLO2    HulkJOD  three different
rank  1   WRavenReapeR17   ApxNiran         ASLwSkyyyz   ASLwMinato18k
rank 10   LETMExGLITCH     LetMexSteve999   LMExHUNT     LetMexAudito7
rank  2   RIOT x STORE     RIOTzFizzy       RIOTwSNIPER  RIOT x STOCK
rank 13   iGxAsD           KOxTryOutBOMT    KOxSpr4yOG   KOxSeervi
```

Rank 10 is the instructive one: one team writing its own tag three
different ways (`LETMEx`, `LetMex`, `LMEx`).

And then the case that settles it outright — **rank 3 is `LMEz`, rank 10
is `LMEx`**. Two different teams, one character apart. A tag matcher
would not merely fail to identify them; it would confidently merge them,
and put one team's placement on the other's row in the sheet. That is the
single worst thing this can do, and it is the reason tags are out.

## 2. What the screen gives us, measured

Measured off the captures (1919×1079 BlueStacks window).

**Left panel — ranks 1 and 2, fixed.** Roughly x 128–973. Never scrolls,
so it is captured once and needs no slide handling.

**Right panel — ranks 3 to 16, scrolls.** x 995–1640.

Blocks are separated by dark rules, and those rules are crisp enough to
find directly — which is better than the alive panel, where the grid had
to be located by its rhythm:

```
separators found at y   231-236   440-447   669-674   896+
blocks                  236-440   447-669   674-896
heights                     204       222       222
```

**Block height is not constant.** 204 for the three-player squad at rank
14, 222 for the four-player ones. So rows must be found by their ink
inside each block; a fixed pitch would read a phantom fourth player on
every short squad.

Columns inside a block:

| what | x |
|---|---|
| gold accent bar | 996–1001 |
| **rank number** | 1037–1076 |
| **player IGN** | from 1162 |
| **"N finishes"** | 1626–1699 |

The rank number is a large glyph in the same stencil face as the alive
panel's slot number, so the digit store we already have should read it —
and the same refusal applies: it will not guess with a digit missing.

## 3. The join: IGN sets, with the operator as the authority

Tags are out, so the join is the **IGNs themselves**. They are stable
identifiers, and we already see them in two places.

Match a rank's set of 3–4 IGNs against each team's known set, and take
the best overlap. This is far more forgiving than reading a name cold,
for three reasons:

- It is a **closed set** — sixteen teams, around sixty players. A
  mangled read like `KOxTryOutB0MT` still lands on `KOxTryOutBOMT` by
  edit distance, because nothing else is close.
- Each block gives **3–4 independent attempts**. One confident player
  identifies the whole team.
- It is an **assignment problem**, not sixteen separate guesses: each
  rank takes exactly one team. A rank that is uncertain on its own is
  often settled by every other rank being certain.

Where the known IGNs come from, in order of preference:

1. **The roster** — typed text, no OCR, exact. Best source if the sheet
   carries player IGNs per team.
2. **The alive panel, captured during the match** — it shows IGNs beside
   every slot, so it gives slot → IGNs directly, and slot is what the
   sheet is keyed on. This needs IGN OCR on the alive panel, which is
   **not built yet** (the reader currently takes only alive state and
   kill counts).

### And the operator decides

Automatic matching is an assist, never the mechanism. Every rank gets a
dropdown in the dashboard:

```
#1   WRavenReapeR17, ApxNiran, ASLwSkyyyz…     [ ASL Esports      ▼ ]  auto, 4/4 matched
#2   RIOT x STORE, RIOTzFizzy, RIOTwSNIPER…    [ RIOT             ▼ ]  auto, 4/4 matched
#3   LMEzSPIDY7, LMEzDragon, LMEzClockko…      [ — select —       ▼ ]  ambiguous: LMEz / LMEx
```

Three rules, all learned the hard way on the Free Fire side:

- A team already taken by another rank is **not offered twice**, and a
  clash is refused rather than resolved.
- Nothing is **pre-selected from a weak guess**. Pre-selecting its own
  guess is exactly how three Free Fire teams were aliased to one in a
  single Apply press.
- Nothing pushes to the sheet until **every rank has a team**.

## 4. Capture workflow

Same shape as the alive panel, which the operator already uses:

1. `Capture Left` — ranks 1 and 2, once. No scrolling.
2. `Capture Slide 1` — the top of the right panel.
3. Scroll, `Capture Slide 2`, and so on to rank 16.
4. Blocks merge **by rank number**, so re-capturing a slide replaces only
   itself and overlapping slides are harmless — two slides that both see
   rank 9 must agree.
5. Resolve any dropdowns the matcher left open.
6. Push to the sheet: pick sub-sheet and match number, as the Free Fire
   results push already does.

## 5. To build it

| | |
|---|---|
| Block reader — separators, rank number, IGN rows, finishes | measured, ready to write |
| IGN OCR + fuzzy match against a known set | new; the real work |
| Slot → IGN from the alive panel | new, and only needed if the roster has no IGNs |
| Dashboard: slides, per-rank dropdowns, conflict display | pattern exists from the alive side |
| Sheet push: rank + team kills per match column | model on `freefire_sheet_push.gs` RESULTS |

**Open question for the operator:** does the sheet already hold player
IGNs per team? If yes, the roster is the match source and no alive-panel
IGN OCR is needed — which removes the hardest piece of this.

**Needed to build the reader:** one result-screen capture scrolled to the
bottom (ranks 15–16 with nothing below), to confirm how the last block
terminates when there is no separator under it.
