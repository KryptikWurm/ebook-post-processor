# eBook Post-Processor

Every eBook you grab lands in its own messy little download folder: the book itself buried next to an NFO, a cover thumbnail, a sample, and a folder named after whatever release group packed it. One book, whatever. Multiply it across a steady drip of downloads and your eBook storage turns into an archaeological dig where nothing is where you'd ever look for it.

This self-contained Python script fixes that. The moment a download finishes, it figures out who wrote the book, files it under `{EBOOK_DEST}/{Author Name}/` (the in-container mount of your NAS eBooks library — `/books` by default), and then cleans up after itself: the leftover download folder gets removed and the job is dropped from SABnzbd's history. What's left is a tidy, author-organized library and a clean queue, with zero clicks from you.

It leans on the book's own EPUB metadata to get the author right, and falls back to the download name when it has to. And like any good post-processing hook, it's built to never break your automation: if anything goes wrong, the files stay exactly where they are and the script exits cleanly.

**Requirements:** Python 3.6+. That's it — no MKVToolNix, no Calibre, no pip install. See [Prerequisites](#prerequisites).

## Contents

- [eBook Post-Processor](#ebook-post-processor)
  - [Contents](#contents)
  - [Features](#features)
  - [How author detection works](#how-author-detection-works)
  - [SABnzbd setup](#sabnzbd-setup)
    - [Category setup (run it automatically, no manual post-processing)](#category-setup-run-it-automatically-no-manual-post-processing)
    - [Can the .nzb file set the category itself?](#can-the-nzb-file-set-the-category-itself)
    - [Docker (LinuxServer.io SABnzbd)](#docker-linuxserverio-sabnzbd)
    - [Manual configuration](#manual-configuration)
  - [What it does, step by step](#what-it-does-step-by-step)
  - [Prerequisites](#prerequisites)
  - [Troubleshooting](#troubleshooting)
    - [Books keep landing in `Unknown Author`](#books-keep-landing-in-unknown-author)
    - [The SABnzbd entry isn't being removed](#the-sabnzbd-entry-isnt-being-removed)
    - [`Destination already exists, skipping`](#destination-already-exists-skipping)
  - [License](#license)

## Features

- **Files books by author:** every eBook lands in `{EBOOK_DEST}/{Author Name}/` (the mounted NAS library, `/books` by default), created on demand. No more hunting through release-group folders.
- **Reads the book's own metadata:** for EPUBs it cracks open the file and reads the real author out of the embedded metadata, so "Brandon Sanderson" beats whatever the filename happened to say.
- **Sensible fallback chain:** no metadata? It parses the author out of the `Author - Title` download name. Still nothing? The book goes to an `Unknown Author` folder instead of getting lost.
- **Format-aware:** moves the formats you actually read — `.epub`, `.mobi`, `.azw3`, `.azw`, `.pdf` — and ignores the NFOs, cover thumbnails and samples littering the folder.
- **Cleans up after SABnzbd:** once the book is filed, the leftover completed folder is deleted and the job is removed from SABnzbd's history via its API. Tidy library, tidy queue.
- **Cleanup is earned, not assumed:** the folder and history entry are only removed after *every* book in the job has been filed successfully. If a single move fails, nothing is deleted and the whole job is left in place for you to look at.
- **Non-destructive by default:** if a book already exists at the destination, it's skipped with a warning rather than overwritten, so a re-download can't quietly clobber your copy.
- **Never blocks your pipeline:** if anything throws, the error goes to the log, the files stay put, and the script still exits 0. A failed run is a no-op, not a stalled queue.
- **Pure standard library:** no external tools, no pip packages. It runs as-is inside the stock LinuxServer.io SABnzbd image.

## How author detection works

Getting the author folder right is the whole point, so the script tries hardest first and degrades gracefully:

1. **EPUB metadata.** For `.epub` files it reads `META-INF/container.xml` to find the OPF package document, then pulls the `dc:creator` out of it — preferring the creator explicitly tagged as the author. This is the source of truth when it's available.
2. **The download name.** For other formats (or an EPUB with no usable metadata), it parses the author out of the `Author - Title` naming convention — the text before the first ` - `. It tries the filename first, then the SABnzbd job name.
3. **`Unknown Author`.** If neither yields anything, the book is filed under `Unknown Author` rather than being skipped or lost.

Whatever name comes out is sanitized into a safe folder name: illegal characters stripped, whitespace collapsed, stray dots and spaces trimmed.

## SABnzbd setup

Drop `ebook_pp.py` into your SABnzbd scripts directory and make it executable:

```bash
chmod +x ebook_pp.py
```

Then head into the SABnzbd web UI and assign the script to your eBook category, as below.

### Category setup (run it automatically, no manual post-processing)

The whole point is that you never touch a "Manual Post-Processing" button again. SABnzbd does that for you when the script is tied to a **category**: every job that lands in that category runs the script on completion, automatically.

In the SABnzbd web UI, go to **Config → Categories** and set up the category you use for books (create one called `books` or `ebooks` if you don't have it):

| Field | Set it to | Why |
|-------|-----------|-----|
| **Category** | `books` (or `ebooks`) | The name. Matching this to your indexer's book category also makes auto-assignment work — see below. |
| **Script** | `ebook_pp.py` | **This is the part that removes the manual step.** Any completed job in this category runs the script with no intervention. |
| **Folder/Path** | your downloads/completed books area (e.g. `books`) | Where SABnzbd drops the finished job. This becomes `SAB_COMPLETE_DIR`, which the script reads and then files into `/books`. *Don't* point this straight at `/books` — SABnzbd would only make per-*job-name* folders full of junk; the script is what sorts by author. |
| **Processing** | `+Delete` (Download + Repair + Unpack + Delete) | Make sure the book is fully unpacked before the script runs. |

For the **Script** dropdown to list `ebook_pp.py`, SABnzbd's **Config → Folders → Post-Processing Scripts Folder** must point at the *directory* that contains the script (e.g. `/config/scripts`) — it's a folder setting, not a per-file one.

That's the manual-post-processing problem solved: drop a book into that category and it's filed and cleaned up with zero clicks.

### Can the .nzb file set the category itself?

Yes — that's how you also skip manually *picking* the category each time. The category can be carried in or attached to the job a few ways, in roughly increasing order of "just works":

- **Inside the `.nzb` file (meta header).** Most indexers embed the category in the NZB's header:

  ```xml
  <nzb xmlns="http://www.newzbin.com/DTD/2003/nzb">
    <head>
      <meta type="category">Books &gt; Ebook</meta>
    </head>
    ...
  </nzb>
  ```

  SABnzbd honors this most reliably when the `.nzb` is added via the **Watched Folder** (Config → Folders → Watched Folder) — drop the file in and it picks up the embedded category, which then runs the script. You can also add this `<meta type="category">` line yourself to a hand-grabbed `.nzb`.

- **By indexer-tag matching.** Since SABnzbd 1.2.0, the category's **Groups / Indexer Tags** field matches indexer category tags loosely, so a category literally named `books` auto-matches any `book*` tag the indexer sends (e.g. `ebook`). Name the category to match your indexer's book category and assignment is automatic for RSS/API adds too — no meta edit needed.

- **By the requesting app (cleanest for full automation).** If a downloader like Readarr or LazyLibrarian sends the NZB to SABnzbd, it passes the category on the API call (`&cat=books`), which wins outright. Set the category there once and every grab is filed and post-processed hands-off.

Bottom line: assign the script to the `books` category once (manual-processing problem solved), then let the `.nzb` meta tag, indexer-tag matching, or your downloader put jobs *into* that category automatically (category-picking problem solved).

### Docker (LinuxServer.io SABnzbd)

Good news: unlike tools that need MKVToolNix or similar baked in, this script is **pure Python standard library**, and the stock `lscr.io/linuxserver/sabnzbd` image already ships Python 3. There's nothing to install and **no custom Dockerfile to build** — just mount the script into the container's scripts directory (`/config/scripts`).

If you live in Compose like the rest of us:

```yaml
services:
  sabnzbd:
    image: lscr.io/linuxserver/sabnzbd:latest
    container_name: sabnzbd
    environment:
      - PUID=1000
      - PGID=1000
      - TZ=Etc/UTC
    ports:
      - "8080:8080"
    volumes:
      - /path/to/appdata/sabnzbd:/config
      - ./ebook_pp.py:/config/scripts/ebook_pp.py
      - /path/to/downloads:/downloads
      - ${MEDIA_PATH}/eBooks:/books
    restart: unless-stopped
```

The script files books to `EBOOK_DEST`, which is a path **inside the container** — so it's the mount target you bind your NAS eBooks share to, not the host path. The example above maps the host's `${MEDIA_PATH}/eBooks` to `/books` in the container, which is the default `EBOOK_DEST`. Set `MEDIA_PATH` in your `.env` (or hard-code the host path) and the books land on the NAS. Make sure the container can see **both** your downloads and that library, then mark the script executable and assign it to your eBook category in the web UI:

```bash
docker exec sabnzbd chmod +x /config/scripts/ebook_pp.py
```

The default `LOG_FILE = "/config/ebook_pp.log"` already points at the persistent `/config` volume, so your logs survive a container rebuild.

### Manual configuration

Open the script and tweak the config block at the top to match your setup:

```python
EBOOK_DEST   = "/books"                          # in-container mount of the NAS eBooks library
EBOOK_EXTS   = (".epub", ".mobi", ".azw3", ".azw", ".pdf")
LOG_FILE     = "/config/ebook_pp.log"            # change to a persistent path
DRY_RUN      = False                             # True = log only, change nothing
OVERWRITE    = False                             # True = replace existing books
DELETE_FILES = 0                                 # del_files flag for the history delete
HISTORY_DELETE_DELAY = 30                        # seconds to wait before the deferred history delete
SAB_API_URL_DEFAULT = "https://nzb.example.com/api"  # fallback if SAB_API_URL is unset
```

Always do a dry run first on a new setup (`DRY_RUN = True`). Look at the log, confirm it's filing books where you expect and resolving authors correctly, then set it loose.

## What it does, step by step

For each finished download, in order:

1. **Find the books.** Walk the completed job folder and collect the eBook-format files, ignoring everything else.
2. **Resolve the author** for each book (metadata → name → `Unknown Author`).
3. **Move** each book to `{EBOOK_DEST}/{Author Name}/` (the mounted NAS library, `/books` by default).
4. **Only if every book moved cleanly:** delete the completed job folder under `/downloads/completed`, then remove the job's entry from SABnzbd via its History API (`del_files=0`, since the files are already gone). The history removal is *deferred* — see below.

> **Why the history removal is deferred.** SABnzbd won't delete a job from history while that job's own post-processing script is still running — it isn't considered "finished" yet, so a delete call made mid-script is silently ignored. To work around this, the script spawns a small detached background process that waits `HISTORY_DELETE_DELAY` seconds (30 by default), by which point this script has exited and SABnzbd has marked the job finished, and *then* makes the delete call. So the entry disappears from history a few seconds after the rest of the work completes, not instantly.

If anything fails along the way, cleanup is skipped, the job is left untouched, and the script still exits 0.

## Prerequisites

- **Python 3.6+** (uses only the standard library — `zipfile`, `xml.etree`, `urllib`, `shutil`, `json`, `logging`).
- A SABnzbd install whose API the script can reach. SABnzbd hands the script everything it needs at runtime (`SAB_COMPLETE_DIR`, `SAB_NZO_ID`, `SAB_API_KEY`, `SAB_API_URL`), so there's nothing to wire up by hand beyond assigning the script to a category.

> **Docker users:** there's nothing extra to install. The stock LinuxServer.io SABnzbd image already has Python 3. See [Docker (LinuxServer.io SABnzbd)](#docker-linuxserverio-sabnzbd).

## Troubleshooting

### Books keep landing in `Unknown Author`

That means neither the EPUB metadata nor the download name gave the script an author to work with. Two common causes:

- **The format isn't EPUB.** Only `.epub` files carry metadata the script reads; `.mobi`/`.azw3`/`.pdf` rely entirely on the name. Make sure those downloads follow the `Author - Title` convention.
- **The naming convention is different.** The name parser expects `Author - Title` (author before the first ` - `). If your indexer names things `Title by Author` or `Title - Author`, the parse won't match. Adjust the convention or extend `parse_author_from_name()`.

### The SABnzbd entry isn't being removed

First, it's removed **on a delay, on purpose.** SABnzbd refuses to delete a job from history while that job's post-processing script is still running, so the script defers the delete to a detached background process that fires `HISTORY_DELETE_DELAY` seconds (30 by default) *after* the script exits. Give it that long before concluding it didn't work — in the log you'll see `Scheduled SABnzbd history delete … in 30s` immediately, then `Removed SABnzbd history entry …` about 30 seconds later.

If the entry still isn't gone after the delay:

- **Missing credentials.** The delete needs `SAB_NZO_ID` and `SAB_API_KEY`, which SABnzbd only provides when the script runs as a real post-processing hook. Testing by hand, they'll be absent and you'll see `Skipping SABnzbd history delete: missing nzo_id or API key` — expected.
- **Wrong API URL.** Confirm `SAB_API_URL` (or `SAB_API_URL_DEFAULT`) points at the right host. The deferred process logs the API response (with the key redacted) so you can see what came back.
- **Delay too short.** If your post-processing finalization is slow, bump `HISTORY_DELETE_DELAY` so the delete lands comfortably after the job is marked finished.

### `Destination already exists, skipping`

A book with that exact filename is already in the author's folder, so the script left both alone rather than overwriting your copy (this also means cleanup is skipped, since the move didn't "succeed"). If you actually want re-downloads to replace the existing file, set `OVERWRITE = True`.

## License

Open-source under the MIT License. See [LICENSE](LICENSE) for details.
