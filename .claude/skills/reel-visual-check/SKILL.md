---
name: reel-visual-check
description: The by-eye part of the reel pre-send checklist (plates, faces, AI-invented text, brands). Use after reels_from_scripts.py / preflight.py, before any reel goes to Ben or Nap.
---

# Reel visual check

`preflight.py` measures what code can measure. Four items need eyes, and the 2 fps contact sheet is too
small to read a plate. This skill does them on sharp frames, the same way every time.

## Steps
1. For each reel in the output folder run
   `python services/reel_render/review_frames.py <out>/<reel>.mp4 [--motion 3,4]`
   (`--motion` = the 1-based scenes that are AI or supplied motion clips; they get a frame every second).
   It writes `<reel>-review-sNN.jpg` (one per scene) and a `<reel>-visual.txt` template.
2. Open every `-review-sNN.jpg` (Read the image). Check each frame for:
   - a readable licence plate, phone or hotline number (windshields, stickers, grilles too);
   - a clear face of a real, identifiable person (drivers through windshields, faces in mirrors);
     small, distant or back-turned figures are fine;
   - AI-invented text on signs, gauges or paint (motion scenes especially);
   - a readable brand logo or shop name (needs Nap's OK).
3. Fill `<reel>-visual.txt`: replace each `[ ]` with `[PASS]` or `[FLAG]`, and for a FLAG add the scene,
   the time and what it is, e.g. `[FLAG] ... (scene 2, 4.33 s: plate on the jeep's rear)`.
4. Any FLAG: fix the **input** (blur it in the source image, drop or swap the image, redo the clip), then
   re-render and re-run both checks. Never hand-edit the output. After 2 failed tries, ask a person.
5. Report per reel: the preflight lines, the visual lines, and the contact sheet. Only reels with zero
   FAIL and zero FLAG go to Ben.

## Rules
- Judge only what is visible in the frames; when unsure, FLAG it and say why. Never mark PASS unseen.
- Use the strongest available model and effort for this step; it is a judgment call.
- This is a check, not a gate on sending: sending still needs Nap's yes.
