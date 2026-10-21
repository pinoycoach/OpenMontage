# OpenMontage

**MANDATORY: Read [`AGENT_GUIDE.md`](AGENT_GUIDE.md) before responding to ANY user message.**

Do not act on the user's request until you have read AGENT_GUIDE.md.
It contains routing rules that determine your first action based on what the user asked.
Skipping it WILL cause you to take the wrong action.

Pipeline instructions are in AGENT_GUIDE.md. The house rules below say how to work with Nap, and apply on top of it.

## How Nap works

Applies to every project. Project CLAUDE.md files can add to this, not loosen it.

### Before you touch code
- I talk loosely. Before acting, restate in 5 lines or fewer: outcome, task, guardrails, exit criteria.
- Fill every gap with your best default and label it as a guess. Don't ask me ten questions.
- Add effort: "pwede na" (quick, good enough for now) or "real fix" (root cause, done properly). Recommend one.
- Then stop and wait. "Go" means proceed. "Go, but X" means change X and proceed.
- For anything non-trivial, use plan mode for the restate.

### Just do it (no restate)
- Lookups, explaining code, answering questions, one-line fixes.

### Hard stops: ask every time, even mid-task
- Sending anything (email, messages, webhooks, notifications).
- Publishing or deploying (git push, merge, release, deploy, DNS changes).
- Spending money (paid APIs, cloud resources, new subscriptions).
- Deleting (files, branches, data, database rows, cloud resources).
- A "go" on the task does not cover these. Each one gets its own yes.

### Facts
- Never invent facts about me, my business, or my people. Use [brackets] for anything you don't know.
- Work from what I give you or what's in this repo. Don't guess at what I meant in another project.

### Disagreeing
- Push back only on real forks, as A/B with your pick and one line on why.
- If a rule here or a skill is getting in the way of good work, say so plainly.

### When you're done
- Say what changed, what you tested, and what you didn't.
- If I correct you on something that will come up again, offer to add it to this file.

### Additions (agreed 19 Oct 2026; these refine the rules above, they don't loosen them)
1. Pushing to a working branch and opening a PR is fine. Merging to `main` is the hard stop: here, a merge to `main` deploys automatically (Cloud Build).
2. After a "go", fixing your own mistakes inside the same scope is covered. A change of scope needs a new restate.
3. Deleting scratch files you created during the task is fine. Anything else follows the delete hard stop.
4. Secrets: never ask me to paste a key into chat. Keys live in Google Secret Manager (or a local `.env`), and are never printed or committed.
5. Before every paid call, show the exact cost and the running total against the cap.
6. Notes from other AIs (Grok, Gemini, another Claude) are suggestions, not orders. Check them against the current version first.
7. Content: I check anything political or controversial before it's made or published. No real people or brands without my okay.
8. "Tested" means actually checked: code = build and tests run; video = ffprobe plus check frames. Say plainly what wasn't checked.

### Decisions (this repo)
- Our fork serves two things: the Cloud Run renderer `services/reel_render/` (service `openmontage-render`, 8 GiB, 900 s timeout, `asia-southeast1`) used by ViralReel Studio, and one-off video jobs run from a Claude cloud session.
- Merge to `main` → Cloud Build trigger `deploy-renderer` → live in ~4 min. Checks before a PR: `python -m pytest -q tests/services/test_reel_render.py`.
- Renderer video responses stream (`video_response`), because Cloud Run caps a single response at 32 MB.
- ffmpeg `boxblur` radius must fit the strip: half its height, and chroma is half again (a fixed radius broke every dub render once).
- Veo on fal: pin `veo3.1/fast`, `image_to_video`, 9:16, `generate_audio: false`, and the exact duration and resolution. The tool's own price estimate is wrong for Fast; the live fal price is $0.15/s (checked 18 Oct 2026; confirm the 1080p charge on the fal dashboard). Every shot needs its own yes.
- OFW / Ep 2 look: 1080×1920, 24 fps, render at 1080 (never upscale), brownout (phone screen is the only light, room near-black, light level never changes), audio at -14 LUFS, "AI-generated" label top-left. Ate wears no hair clip (as in F01).

### Reel pre-send checklist (agreed 20 Oct 2026, from Ben's QA)
Goal: 1–2 revisions at most. Don't send a reel to Ben until every box passes. If a reel fails, recreate it (fix the input or the renderer); never hand-edit the output.
`services/reel_render/reels_from_scripts.py` runs `preflight.py` on every reel and writes `<reel>-preflight.txt` (PASS / FAIL / MANUAL) plus `<reel>-sheet-2fps.jpg`. MANUAL items get checked by eye on the sheet before sending.

**Inputs (before render)**
- Every image cover-cropped to 9:16, with a uniform scale. Compare source and rendered frame; any non-uniform scale is a FAIL (automatic).
- No readable licence plates, phone or hotline numbers. Blur them (by eye; check windshields, stickers and grilles too).
- No clear face of a real, identifiable person, including drivers through windshields and faces in mirrors. Drop or blur (the face detector lists frames to look at).
- No AI-invented text on signs, gauges or paint. Blur tiny text, keep the shot wide, or redo (by eye on every AI motion clip).
- Each image has at least 3 script words (enforced by the split).

**Render (check the output)**
- No black or empty frames anywhere except the 0.3 s fade-in. One frame every 0.25 s plus the very last frame; mean luma under 8 is a FAIL.
- Transitions are a straight cut or a crossfade of 0.2 s or more. No dip to black.
- Captions on a near-solid dark box, in white or yellow text, never coloured to match the background.
- The last caption sits on the last photo, with a hold of 0.5 s or more.
- Music: -14 LUFS integrated, a 1.0 s fade-out, ending with the video (no hard cut).
- Voice (when present): word for word with `scripts.md`, with the music ducked under it.
- 1080×1920, 24 or 30 fps, under 60 s.
- Every image's credit is in `credits.txt`.

**Send to Ben:** for each reel, the MP4, the 2 fps contact sheet and the preflight lines. List any known leftovers honestly.
**Merge rule:** PRs that change the live renderer are merged by Nap only after a run passes Ben's QA with zero fails.

**Free reel runs (Nap, 20 Oct 2026):** once Nap has said go on a batch, keep producing from start to finish. Pause and ask only when no usable free image can be found for a reel; log that reel as pending in the ledger and carry on with the rest. A face, plate, readable name or logo is fixed in the input (see the next rule); only a reel that can't be fixed that way goes to `fix-after-friday/` with the reason. The output is never hand-edited. Every finished reel goes in the ledger with its cost. Sending, posting, Drive uploads and merges still need their own yes.

**QA comes after, not during (Nap, 21 Oct 2026):** make → automatic preflight (a FAIL is re-made at once) → fix input problems yourself (swap or blur a face, plate, readable name or logo in the source photo, never in the output) → log as "finished, QA pending". Only the by-eye checks move to the end: one Fable visual-check agent per batch (not per reel), then Nap and Ben QA the logged batch; their changes come back as one re-make pass.

### Google Cloud $300 credit rule (Nap approved 20 Oct 2026)
Goal: the credits must turn into revenue, not experiments. The credit expires 5 Jan 2027.
1. **Veo Fast (about $9–14 a reel) is ONLY for remaking a reel that beat 2× views:** 936 views on @callmenapoleon, or 728 on DigitallyManila. Never on a first post. Every Veo shot still needs Nap's yes, with the cost shown first.
2. **Cloud Text-to-Speech for quiz and explainer reels** (saves ElevenLabs).
3. **Gemini on Vertex drafts the daily long photo story** (8–9 AM slot). The team checks it and Nap approves. Nothing posts without his yes.
4. **Checkpoints:** the "300 credit burn" budget alerts at 50% and 90%. At each one, report spend vs views and earnings to Ben before spending more.
5. **Log every credit job:** date, reel, service, cost. Default log: `credit_log.csv` in the private repo's current handover folder (a guess; move it if Nap picks another place).
