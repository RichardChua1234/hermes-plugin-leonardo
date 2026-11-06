# Leonardo AI — video generation provider

Adds [Leonardo.AI](https://leonardo.ai) as a video generation backend for Hermes.

```
video_generate(prompt="a lantern glowing at dusk over still water")            # text-to-video
video_generate(prompt="gentle ripples", image_url="https://…/frame.jpg")       # image-to-video
```

## Why this exists

Hermes ships video backends for FAL, xAI and DeepInfra — and that is the entire
list, because **`video_gen` has no hardcoded providers: every backend must arrive
as a plugin.** Anything Hermes cannot reach is simply unavailable to the agent.

Leonardo is a large hosted video provider that Hermes could not reach at all. It
is also the *cheapest practical door* to a hosted video model, because one
Leonardo account fronts several families — Leonardo's own Motion models plus
Veo, Kling, Seedance and Wan — instead of requiring a separate account, key and
billing relationship with every vendor.

## Install

```bash
hermes plugins install <this-directory-or-repo>
hermes plugins enable leonardo
hermes config set video_gen.provider leonardo
```

Then make sure the toolset is on:

```bash
hermes tools          # → Video Generation → Leonardo AI
```

Requires `LEONARDO_API_KEY` in `$HERMES_HOME/.env`.

> **API credits are separate from web-app tokens.** A Leonardo account can show
> web-subscription tokens while having **zero API credits**, in which case every
> call fails. Generate a key under *API Access → Create New Key* and buy API
> credits there; the `/me` endpoint reports the API balance as `apiPaidTokens`.

## Verified behaviour

Tested end to end against the live API, not just type-checked:

| Check | Result |
|---|---|
| `hermes plugins doctor` | `OK: runtime discovery, manifest parsing, import, and registration passed` |
| Live text-to-video | submitted → `COMPLETE` → 832×480 h264 MP4, 5.03 s, 2.29 MB |
| Output verified | downloaded and probed with `ffprobe` — real, playable video |
| Reported cost | `0.299 USD` per 480p clip |
| Failure paths | missing key and invalid key both return clean errors, no exception |

### Response shapes

The create endpoint has shipped **three** different id shapes. Missing any one of
them throws away a generation you have already been billed for, so all are
handled:

```jsonc
{"generate":       {"generationId": "...", "cost": {"amount": "0.299", "unit": "DOLLARS"}}}  // v2 — observed live
{"generationId":   "..."}                                                                    // flat
{"sdGenerationJob":{"generationId": "..."}}                                                  // v1
```

Polling reads `generations_by_pk.status` and takes
`generated_images[0].motionMP4URL` as the result.

## Capabilities

| | |
|---|---|
| Modalities | text → video, image → video |
| Aspect ratios | 16:9, 9:16, 4:5, 2:3 |
| Resolutions | 480p, 720p |
| Duration | fixed by the model (~5 s) — the Motion API accepts no duration parameter |
| Audio | no |
| Negative prompt | yes (≤1000 chars) |
| Seed | yes |

Dimensions follow Leonardo's documented table for Motion 2.0, e.g. 16:9 480p =
832×480 and 16:9 720p = 1280×720.

## Limitations

- **One model family today** (`motion-2.0`). Leonardo also fronts Veo, Kling,
  Seedance and Wan; adding them means adding verified model ids to the
  `FAMILIES` dict — the rest of the plugin is family-agnostic. They are not
  included on guessed ids.
- **Image-to-video passes the frame as `image.type = "URL"`.** This path is
  documented by Leonardo but was not exercised in the live test; if it rejects a
  URL, seed with an uploaded image id instead.
- Video URLs are on Leonardo's CDN, which **rejects requests without a browser
  `User-Agent`** (403). Hermes' own downloader handles this.
- Costs Leonardo API credits. Nothing here meters spend for you.

## Disclosure (catalog rule 13)

What a user should know before installing:

- **Network.** Outbound HTTPS to `cloud.leonardo.ai` (API calls) and
  `cdn.leonardo.ai` (fetching the finished clip). No other hosts.
- **Telemetry.** None. Nothing is reported to the plugin author or anyone else.
- **Credentials.** Reads `LEONARDO_API_KEY` through
  `agent.secret_scope.get_secret()`, which honours the active profile. It does
  **not** read `.env`, `auth.json`, a vendor CLI's token file, or any other
  credential store, and it never writes or rotates a token.
- **Reads outside its own data.** None. The plugin never touches Hermes core
  modules, other plugins, or user files.
- **Shell commands.** None. HTTP is stdlib `urllib` only.
- **Background processes.** None. Generation is submitted and polled in-process,
  bounded at 600 s, then the call returns.
- **Stored credentials.** None written by the plugin.
- **Spend.** Each generation bills Leonardo API credits. The plugin reports the
  cost the API returns but does not enforce a budget.

## Files

```
plugin.yaml    manifest — kind: backend, requires_env: LEONARDO_API_KEY
__init__.py    LeonardoVideoGenProvider + register(ctx)
```

MIT.
