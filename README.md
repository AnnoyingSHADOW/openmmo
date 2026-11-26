# VoiceClone Studio

A local web app for cloning a voice from a short clip and then turning text into speech in that voice. It uses [VoxCPM2](https://github.com/OpenBMB/VoxCPM) and runs entirely on your Mac through Apple Silicon (MPS). No audio leaves the machine.

## Start

```bash
./run.sh
```

The script creates `.venv` on the first run if it is missing, starts the server on http://127.0.0.1:7860 and opens your browser.

The first launch downloads the VoxCPM2 weights from Hugging Face (about 5 GB, saved in `~/.cache/huggingface`). Later launches load from that cache in well under a minute.

Requirements: an Apple Silicon Mac with 16 GB of RAM or more, Python 3.12 (or [uv](https://docs.astral.sh/uv/)), and `ffmpeg` (`brew install ffmpeg`).

## Use

1. **Clone a voice** (left panel). A voice can be built from **several clips** of the same person. Each recording or uploaded file becomes a clip, and the clips are joined into one voice. More audio usually gives a closer match. Aim for 20–60 s in total; the limit is 120 s, and any single clip longer than 60 s is cut to 60 s.
   - Clips are level-matched and joined with short pauses. Clips that have a transcript also feed Max similarity, so you don't need a transcript for every clip.
   - **Manage** on a saved voice lets you rename it, play or remove its clips, edit each clip's transcript, and use **+ Add clips** to add more later.
   - **Record:** read the on-screen script aloud. Each take becomes a clip, its script is filled in as that clip's transcript, and the next script appears so you can record another take.
   - **Upload:** drop or pick several audio or video files at once. Every file must be the same speaker.
   - The recording script is available in English, Khmer and Chinese.
   - The transcript is optional. When it matches the clip exactly, "Max similarity" mode clones the voice more closely. **Auto-transcribe** can fill it in (supports English, Chinese, Japanese, Korean and Cantonese).
   - Tick **Remove background noise** for noisy recordings.
2. **Speak** (right panel)
   - Pick a voice, type your text and click **Generate** (or press ⌘+Enter).
   - **Style** (optional) controls delivery, for example "whispering", "excited" or "slow and calm". Style works best with Max similarity turned off.
   - **Design a new voice** creates a voice from a description alone, with no recording.
   - Long text is split into sentences and stitched back together automatically.
   - **Telegram voice** downloads the clip as Ogg/Opus (mono, 48 kHz), the format Telegram uses for voice messages. Send that file in Telegram and it appears as a voice message with a play button and waveform, not as a music file. **WAV** downloads full-quality audio.
   - Generated clips are kept in **History**, where you can play, download or reuse them. The **Reuse** button restores the text, voice and seed.

### Video maker (topic → narrated video with Khmer captions)

Open the **Video maker** tab:

1. **Idea → script:** choose how Claude uses your text:
   - **Claude writes the script:** type a topic, or paste an article, notes or a story (up to 20,000 characters) for Claude to adapt. Pick a length from 15 seconds to 5 minutes. Long videos use about 8-second scenes, so a 5-minute video has about 38 scenes.
   - **Keep my exact words:** paste your finished story or script. The narration stays word for word. Claude only decides where each scene starts, along the story's beats (it returns sentence numbers, so it can't change your text), and writes a title and image prompts. The limit is about 5 minutes of narration. Zero-width spaces in Khmer text are removed before narration and used as word breaks for captions.

   Then choose the image style and Claude model. **Write script with Claude** runs the local Claude Code CLI (`claude -p`) with a JSON schema. It returns a title plus scenes, each with Khmer narration and an English image prompt. It uses your Claude Code login and takes about 10–20 s. Your Claude Code plugins and hooks are not loaded for this call, so they can't change the writing style.
2. **Edit:** change any narration or image prompt, and add or remove scenes.
3. **Review images:** **Generate images** creates every scene's picture so you can check them before rendering. For each scene:
   - **New AI image** makes a different picture right away, from the same prompt. Edit the prompt first to change what it shows.
   - **Upload** or click the picture to use your own image. You can also drop a file onto it. JPG, PNG, WEBP and iPhone HEIC all work. Your picture fills the frame, cropped from the center.
   - **Use AI image** removes your upload and goes back to the AI picture.
   - **Copy prompt** copies a ready-made prompt for that scene (its description, your style, the frame shape, and "no text"). Paste it into any image tool you like, for example Gemini in your browser, then drop the picture you get onto the scene.
   - The badge under each picture shows **AI image**, **Your image**, or **AI · prompt changed** (click Generate images or New AI image to update it).
4. **Voice, images & video:** choose the narrator (any cloned voice), the format (9:16, 16:9 or 1:1), and the images:
   - **Pollinations AI** needs a free `sk_` key from [enter.pollinations.ai](https://enter.pollinations.ai/keys) and uses its pollen credits.
   - **On this Mac** makes the pictures on your Mac's GPU, with no key, no credits and no upload. It needs a one-time setup (see [Images on this Mac](#images-on-this-mac)).
   - **Plain gradient** works offline with no key.

   Then choose the caption font (your installed Khmer fonts are listed) and caption look, and click **Make video**.

Captions:
- **Font:** every font file in `static/fonts/` (for example `static/fonts/Koulen/Koulen-Regular.ttf`) appears first in **Caption font**, marked "project font". The fonts are passed to the renderer directly, so you don't need to install them. Koulen is the default for both captions and the title. To use another font, drop its `.ttf` into `static/fonts/` and restart.
- **Highlight while speaking:** **Current word** lights up only the word being spoken (TikTok style). **Karaoke** keeps spoken words lit. **Off** disables highlighting. You can pick any highlight color.
- **Word timing:** timings come from forced alignment. The script text is lined up with the narration audio using Meta's multilingual MMS CTC aligner ([`MahmoudAshraf/mms-300m-1130-forced-aligner`](https://huggingface.co/MahmoudAshraf/mms-300m-1130-forced-aligner), about 1.3 GB on first use, licensed **CC-BY-NC-4.0**, which is non-commercial) plus the `uroman` romanizer. This works for Khmer, English and other languages. If alignment fails, timings are estimated from word length.

How a video is built:
- **Narration:** each sentence is spoken in the cloned voice, which also gives exact caption timings.
- **Images:** they download at the same time as the narration runs.
- **Motion and captions:** each scene gets a slow zoom or pan, a fade, and burned-in captions. ffmpeg renders them with libass and HarfBuzz, so Khmer shapes correctly. Long Khmer lines wrap between words using [khmer-nltk](https://github.com/VietHoang1512/khmer-nltk) word segmentation.
- **Encoding:** the Mac's hardware H.264 encoder is used. A 30-second video usually takes 20–40 s to make.
- **Files:** saved under `data/videos/<id>/` (`video.mp4` plus per-scene images and audio). Download them with **Download MP4**.

The Pollinations key is stored only in `data/settings.json` (readable only by you). You can also set it with `POLLINATIONS_API_KEY=sk_... ./run.sh`. Images use the cheapest non-paid models by default (FLUX.1 schnell is about 0.002 pollen per image).

### Logo & assets

Put your own logo, stickers or product photos on top of scenes. Find **Logo & assets** on the right side of the Video maker.

- **Add from computer:** PNG, JPG, WEBP, SVG or iPhone HEIC, several at once, up to 15 MB each. Files go into a library in `data/assets/`, so every video can reuse them. Transparent edges are trimmed automatically.
- **Remove white background:** tick this before adding a logo saved on white, for example a JPG. Only the white around the logo is removed; white letters inside the logo stay.
- **Logo on every scene:** pick an asset, a corner and a size. It stays visible the whole time, slightly see-through, including during the fades between scenes.
- **Assets on one scene:** click **+ Logo / asset** in a scene and choose the asset, position (corners, top or bottom centre, or centre) and size, up to 4 per scene. They fade in at the start of the scene and stay still while the picture slowly zooms.
- The scene thumbnails show where everything will go. Captions are always drawn on top.

Assets are added when the video is made, so the scene pictures themselves are never changed. Deleting an asset from the library removes it from any scene that used it.

### Tutorials (lessons with code slides)

Choose **Tutorial** in the Video maker to make a lesson video about basic knowledge: coding, computers, math, science, everyday digital skills.

1. Type the lesson topic, for example "Python for loops for total beginners" or "How Wi-Fi works". You can add the level or what to cover. Or pick one of the **Suggested lessons**: 42 beginner topics in course order across Start here, Python basics, Web basics, Tools and Concepts. Click one to fill in the topic; a ✓ marks lessons you have already made. Edit the list in `static/lessons.js`. Pick the language, the length and the Claude model, then click **Write tutorial with Claude**.
2. Claude writes the lesson as scenes. Each scene has the narration plus one slide:
   - The **first and last scenes** are title scenes: a big title (and, at the end, a short recap) over an AI picture. Claude also picks the real logo of the technology the lesson is about, for example the Java logo for a Java lesson, or the HTML 5 and CSS 3 logos for a web lesson. It shows as a white tile above the title and pops in when the scene starts. The app checks every logo exists before using it. Lessons that aren't about one product get a fitting icon instead.
   - **Every other scene** is a teaching slide with a heading plus bullet points, or a code example with syntax colors and its output.

   For coding topics the code is meant to run as written, and the output block shows what it prints. Check it anyway before you publish.
3. Edit any slide in its text box. The format is simple:

   ````
   # Heading
   A short sentence.
   - a bullet point
   ```python
   for i in range(3):
       print(i)
   ```
   ```output
   0
   1
   2
   ```
   ````

   Click **Update preview** to redraw it, and click a slide thumbnail to see it full size. The **Slide / Title + AI picture** menu switches a scene's layout.
4. Click **▶ Play** on a scene to watch its animation (see below) in a loop before you make the video.
5. Click **Make video**. Your cloned voice narrates each scene and captions run under the slide. Code slides stay still so they are easy to read; title scenes zoom gently.

#### Quizzes, spotlights and sound

Claude adds these to new lessons by itself. In any slide you can add them with the buttons under the slide box.

- **Quiz** (**+ Quiz**): the options slide in, a 5-second countdown ring ticks down, then the right answer turns green with a ✓ and a chime while the wrong ones fade. The narrator asks the question and says "pause and choose". The video then pauses the voice for the countdown on its own, and after the `|` the narrator gives the answer. Format:

  ````
  # Quiz
  ```python
  x = "5"
  print(x * 2)
  ```
  ```quiz
  What does this code print?
  - [ ] 10
  - [x] 55
  - [ ] Error
  ```
  ````

  The question line is optional; mark exactly one option `[x]`. A quiz counts as one part (its answer), so the narration needs one `|` for it, placed right before the answer. Claude puts 1 quiz in short lessons and 2 in lessons longer than 90 s, and asks viewers to comment their score in the recap.
- **Spotlight** (**+ Focus**): `+++ focus 2` (or `+++ focus 2-3`) after a code block lights up that line, dims the others and zooms in a little until the next part. Code blocks now show line numbers, so the narrator can say "look at line 2". It counts as a part and needs a `|`.
- **Sound effects** play on animated slides: a soft pop when something appears, keyboard clicks while code types, a whoosh as a dot travels a diagram arrow and a blip when it arrives, ticks during a quiz countdown and a chime for the answer. They are generated by the app (no sound files, nothing to license) and mixed quietly under the voice. Turn them off with **Sound effects** under **Music & sound**.
- **Background music**: under **Music & sound**, click **+ Add music from computer** (MP3, M4A, WAV, OGG or FLAC; up to 40 MB). Pick the track and the volume; the ▶ player previews it. It loops under the whole video, fades in and out, and automatically gets quieter whenever the narrator speaks. Use music you have the rights to, your own or royalty-free. Works for every video, not only tutorials.

When Claude's narration has fewer `|` than the slide has parts because of a `+++` right under the heading, the app removes that `+++`, so the first block shows with the heading and everything stays in time.

#### Logos on a slide

The logo is a line at the top of the slide text, for example `[logo] logos:java` or `[logo] logos:html-5 logos:css-3` (up to 4). To add or change one in any slide, including lessons made before this feature, click **+ Logo**, type a name (java, python, excel, docker…), and click a result. Real logos are listed first. You can also put your own logo there with its `asset:…` code from **Logo & assets**. Delete the line to remove the logo. A slide with a logo is animated so the logo can pop in. Drawing a title scene this way takes about twice its length, for example ~20 s for a 10 s scene, because its picture zooms slowly the whole time.

#### Slides that build up part by part

Claude writes slides that appear piece by piece while the narrator talks, and draws "how it works" scenes as animated diagrams. You can add or change both yourself:

- **`+++` on its own line** splits a slide into parts. Everything before the first `+++` is on screen when the scene starts; each later part appears when the narration gets to it. Inside a code block, `+++` makes the next lines appear later. Code **types itself line by line** with a cursor, and output fades in. **+ Part** inserts a `+++` at the cursor.
- **`|` in the narration** marks the exact moment the next part appears. The narration needs as many `|` as the slide has `+++`. The line under the slide checks this for you, for example *"3 parts appear one by one, timed by the 2 "|" in the narration ✓"*. **+ | in narration** inserts one at the cursor. Without enough `|`, the leftover parts appear at the start of later sentences. The `|` itself is never spoken and never shown in the captions; the narrator pauses briefly there.
- **Diagrams** show how something works. The boxes pop in, then each arrow draws itself while a glowing dot travels along it, and the box it reaches lights up. **+ Diagram** inserts an example:

  ````
  # How a website works
  ```diagram
  [browser] logos:chrome Browser
  [server] tabler:server Web server | [db] tabler:database Database
  +++
  browser -> server: GET /home
  +++
  server -> db: SQL query
  +++
  db --> server: 3 posts
  server --> browser: HTML page
  ```
  ````

  Each `[id] icon Label` line is one row of boxes, and `|` puts boxes side by side, up to 6 boxes in all. The icon can be:
  - **an open-source icon**, written as `set:name`. Use `tabler:…` for things and ideas, from [Tabler Icons](https://tabler.io/icons) (MIT licence), for example `tabler:server`, `tabler:database`, `tabler:user`, `tabler:cloud`, `tabler:lock` or `tabler:cpu`. These are white line icons that light up in your highlight color when a dot arrives. Use `logos:…` for technology and brand logos in color, from [SVG Logos](https://github.com/gilbarbara/logos) (CC0), for example `logos:python`, `logos:html-5`, `logos:javascript`, `logos:git-icon`, `logos:github-icon` or `logos:chrome`. Color logos sit on a small white tile so dark logos stay visible. Any other set on [Iconify](https://icon-sets.iconify.design) works too: click **Icons ↗** under a slide to browse them. The app downloads each icon once from the free Iconify API (only the icon's name is sent) and keeps it in `data/icons`. A wrong name, or no internet the first time, just shows the box without an icon.
  - **your own picture**: click a picture in **Logo & assets** to copy its code, such as `asset:1a2b3c4d`, and paste it in place of the icon.
  - **an emoji**, as before.

  Claude picks icons from a checked list of about 150 Tabler icons and logos. In a vertical 9:16 video the rows run top to bottom; in 16:9 and 1:1 they run left to right. Arrow lines are `a -> b: label`. Use `-->` for a dashed arrow (a reply) and `<->` for both ways. Two arrows in the same part play one after the other. A diagram with no `+++` at all plays its arrows one by one, spread over the narration.

The **▶ Play** preview guesses the timing from the length of the text. The finished video uses the real voice timing. Animating a slide takes about as long as the scene itself, for example ~10 s for a 13 s scene. Animated slides are saved as `motion_*.mp4` in the project folder and reused until the slide, narration timing or settings change. If an animation fails, that scene falls back to the still slide.

Slides are drawn on your Mac by Google Chrome in headless mode (Brave, Edge or Chromium also work), so Khmer text shapes correctly. Headings use the caption font (Koulen by default), and the accent color follows the highlight color. Code blocks are kept short so they fit: about 34 characters per line in 9:16, 60 in 16:9. 16:9 suits longer code. Title pictures use the image source you picked (On this Mac, Pollinations or plain gradient).

### Images on this Mac

**On this Mac** generates scene pictures locally with [mflux](https://github.com/filipstrand/mflux), which runs image models on Apple's MLX. Two 4-bit models are available under **Local model**:

| Model | Download | Speed (M4 Pro) | Notes |
|---|---|---|---|
| [FLUX.2 klein 4B](https://huggingface.co/black-forest-labs/FLUX.2-klein-4B), [4-bit build](https://huggingface.co/mflux-community/flux2-klein-4b-mflux-q4) | 4.6 GB | about 25 s per picture | Recommended. Follows clear, literal prompts well; struggles when several people interact. Apache-2.0. |
| [Z-Image Turbo](https://huggingface.co/Tongyi-MAI/Z-Image-Turbo), [4-bit build](https://huggingface.co/filipstrand/Z-Image-Turbo-mflux-4bit) | 5.9 GB | about 1.5 minutes per picture | A more photographic look. In tests with Cambodian story scenes it followed prompts no better than FLUX.2 klein (it drew coconut palms instead of sugar palms) and was about 3.5 times slower. The base model is Apache-2.0; the 4-bit build is labelled with Tongyi's license. |

Set it up once:

```bash
./setup-local-images.sh          # FLUX.2 klein (fast)
./setup-local-images.sh best     # Z-Image Turbo (photographic, slow)
./setup-local-images.sh all      # both
```

The script installs mflux into its own environment, `.venv-img`, so its PyTorch and transformers versions never touch the voice app's `.venv`. It then downloads the model into `~/.cache/huggingface`. Restart `./run.sh` afterwards and pick **Images: On this Mac**.

How it runs:
- The model runs in a separate background process. It loads when the first picture is needed (2–9 seconds once downloaded) and stays loaded for 3 minutes so **New AI image** clicks are quick. Then it unloads and gives its memory (about 5–7 GB) back. Only one image model is in memory at a time.
- Pictures are made one at a time. A 40-scene, 5-minute video needs roughly 17 minutes of image time with FLUX.2 klein, or about 1 hour with Z-Image.
- During **Make video**, missing pictures are made first and the image model is unloaded before narration starts, so the image model and the voice model never compete for memory.
- After switching models, click **New AI picture for every scene** to redo the pictures.
- Progress and errors are written to `data/imagegen.log`.

**Getting pictures that match the story.** Small local models draw literally what the prompt says. Claude is told to write image prompts that show one clear moment with at most 2–3 people, put the subject and action first, and describe local things by how they look (for example a Cambodian sugar palm as "a very tall, thin Borassus palm with a round crown of fan-shaped leaves", or it will draw a coconut palm). When you edit a prompt yourself, follow the same pattern. Choose a "photo" image style before writing the script if you want real-looking pictures; the style is written into every prompt.

### Languages and Khmer

VoxCPM2 speaks 30 languages, including Khmer, Thai, Vietnamese, Lao, Burmese, English and Chinese. You do not need to choose a language: type the text and the model detects it. A voice cloned in one language can also speak another, so an English recording can say Khmer text.

For Khmer:
- **Record:** choose **ខ្មែរ Khmer** in the script menu and read the Khmer script aloud. The script is filled in as the transcript, so Max similarity works right away.
- **Upload:** choose **ខ្មែរ Khmer** next to Auto-transcribe. This uses [`seanghay/whisper-small-khmer-v2`](https://huggingface.co/seanghay/whisper-small-khmer-v2), which downloads about 1 GB on first use and runs on the Apple GPU at about the length of the clip. The transcript will have some mistakes, so correct it before saving.
- **Numbers:** the number normalizer is skipped automatically for Khmer and other non-English/Chinese scripts, because it would read "25" as "twenty five". The model reads digits by itself, but writing numbers out as Khmer words is more reliable.
- Long Khmer text is split at `។`. Khmer clusters are never cut in half.

### Advanced settings

| Setting | Effect |
|---|---|
| Guidance (CFG) | Higher values follow the prompt and voice more strictly. The default is 2.0. |
| Quality steps | More diffusion steps give slightly cleaner audio but run slower. The default is 10. |
| Seed | Fix the seed to reproduce a take exactly. Leave it blank for variety. |
| Normalize | Spells out numbers and symbols, for example "2026" or "$5". |

## Files

```
server.py         FastAPI backend (model, voices, jobs, history)
video.py          video maker (Claude script, narration, images, captions, ffmpeg render)
slides.py         tutorial slides: Markdown subset -> HTML -> PNG, or an animated MP4 (parts, typing code,
                  diagrams) filmed frame by frame from headless Chrome over the DevTools protocol
data/icons/       diagram icons downloaded from the Iconify API (safe to delete; they download again)
sfx.py            sound effects for animated slides (pop, typing, whoosh, quiz ticks, chime), made with numpy
music.py          background music library and the final mix (loop, fades, quieter under the voice)
data/music/       your background music tracks
static/lessons.js suggested lesson topics for Tutorial mode
assets.py         logo & asset library (upload, trim, remove white background) used as overlays
data/assets/      your logos and assets (PNG) + index.json
imagegen_worker.py  local image generator process (runs in .venv-img with mflux)
setup-local-images.sh  one-time setup for "On this Mac" images
.venv-img/        separate Python environment for mflux (created by setup-local-images.sh)
static/           web UI (index.html, app.js, style.css)
data/voices/      saved voices: clips/*.wav, ref.wav (all clips joined), prompt.wav (clips with transcripts), meta.json
data/outputs/     generated WAV files
data/history.json generation history
data/videos/      video projects (project.json, scene images/audio, video.mp4)
data/settings.json  local settings (Pollinations key)
data/imagegen.log local image generator log
```

## Configuration (environment variables)

| Variable | Default | Purpose |
|---|---|---|
| `PORT` | `7860` | Port the web server listens on |
| `HOST` | `127.0.0.1` | Use `0.0.0.0` to allow other devices on your LAN (no authentication) |
| `VOXCPM_MODEL` | `openbmb/VoxCPM2` | Model to load: `openbmb/VoxCPM1.5` is smaller and faster but supports only English and Chinese and has no style control |
| `VOXCPM_DEVICE` | `auto` | Force `mps` or `cpu` |
| `VOXCPM_MPS_DTYPE` | auto | `bfloat16` (about 6 GB) or `float32` (about 13 GB). The default is `bfloat16` on Macs with 24 GB of RAM or less, otherwise `float32` |
| `CHUNK_CHARS` | `260` | Maximum characters per generated chunk for long text |
| `KHMER_ASR_MODEL` | `seanghay/whisper-small-khmer-v2` | Hugging Face Whisper model used for Khmer auto-transcribe |

## Notes

- **Memory:** VoxCPM upstream defaults to float32 on Apple GPUs (about 13 GB). On a 24 GB Mac that also runs Docker or other heavy apps, float32 pushes macOS into swap and a 5-second clip can take 30–60 seconds. This app therefore uses bfloat16 on Macs with 24 GB of RAM or less. In testing, bfloat16 matched float32 on clip length, transcription accuracy and artifact checks, and generated a short sentence in about 4–6 seconds. If you notice glitches, start with `VOXCPM_MPS_DTYPE=float32 ./run.sh`.
- **Speed:** on an M4 Pro, generation runs at roughly real time: a 6-second clip takes about 5–6 seconds. Keep other apps with large memory use (Docker Desktop, VMs) small while generating.
- The noise remover runs on the Apple GPU (about 5 seconds per clip). It is about 40 times slower on the CPU.
- Auto-transcribe uses SenseVoiceSmall from Hugging Face for English, Chinese, Japanese, Korean and Cantonese. It downloads about 900 MB on first use and runs on the CPU in under 1 second. Khmer uses the Whisper model described above.
- Only clone voices you have permission to use.
