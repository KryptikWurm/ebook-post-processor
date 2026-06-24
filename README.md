# eBook Post-Processor

Every eBook you grab lands in its own messy little download folder: the book itself buried next to an NFO, a cover thumbnail, a sample, and a folder named after whatever release group packed it. One book, whatever. Multiply it across a steady drip of downloads and your eBook storage turns into an archaeological dig where nothing is where you'd ever look for it.

This self-contained Python script fixes that. The moment a download finishes, it figures out who wrote the book, files it under `/media/Storage/eBooks/{Author Name}/`, and then cleans up after itself: the leftover download folder gets removed and the job is dropped from SABnzbd's history. What's left is a tidy, author-organized library and a clean queue, with zero clicks from you.

It leans on the book's own EPUB metadata to get the author right, and falls back to the download name when it has to. And like any good post-processing hook, it's built to never break your automation: if anything goes wrong, the files stay exactly where they are and the script exits cleanly.

**Requirements:** Python 3.6+. That's it — no MKVToolNix, no Calibre, no pip install. See [Prerequisites](#prerequisites).

## Contents

- [eBook Post-Processor](#ebook-post-processor)
  - [Contents](#contents)
  - [Features](#features)
  - [How author detection works](#how-author-detection-works)
  - [SABnzbd setup](#sabnzbd-setup)
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

- **Files books by author:** every eBook lands in `/media/Storage/eBooks/{Author Name}/`, created on demand. No more hunting through release-group folders.
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

Then head into the SABnzbd web UI and assign the script to your eBook category. Done.

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
      - /media/Storage/eBooks:/media/Storage/eBooks
    restart: unless-stopped
```

Make sure the container can see **both** your downloads and your eBook library at the same paths the script expects. Then mark the script executable and assign it to your eBook category in the web UI:

```bash
docker exec sabnzbd chmod +x /config/scripts/ebook_pp.py
```

The default `LOG_FILE = "/config/ebook_pp.log"` already points at the persistent `/config` volume, so your logs survive a container rebuild.

### Manual configuration

Open the script and tweak the config block at the top to match your setup:

```python
EBOOK_DEST   = "/media/Storage/eBooks"           # where books get filed
EBOOK_EXTS   = (".epub", ".mobi", ".azw3", ".azw", ".pdf")
LOG_FILE     = "/config/ebook_pp.log"            # change to a persistent path
DRY_RUN      = False                             # True = log only, change nothing
OVERWRITE    = False                             # True = replace existing books
DELETE_FILES = 0                                 # del_files flag for the history delete
SAB_API_URL_DEFAULT = "https://nzb.example.com/api"  # fallback if SAB_API_URL is unset
```

Always do a dry run first on a new setup (`DRY_RUN = True`). Look at the log, confirm it's filing books where you expect and resolving authors correctly, then set it loose.

## What it does, step by step

For each finished download, in order:

1. **Find the books.** Walk the completed job folder and collect the eBook-format files, ignoring everything else.
2. **Resolve the author** for each book (metadata → name → `Unknown Author`).
3. **Move** each book to `/media/Storage/eBooks/{Author Name}/`.
4. **Only if every book moved cleanly:** delete the completed job folder under `/downloads/completed`, then remove the job's entry from SABnzbd via its History API (`del_files=0`, since the files are already gone).

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

The history-delete call needs `SAB_NZO_ID` and `SAB_API_KEY`, which SABnzbd only provides when the script runs as a real post-processing hook. If you're testing by hand they'll be missing and the script logs `Skipping SABnzbd history delete: missing nzo_id or API key` — that's expected. When it runs for real, check the log for the API response, and confirm `SAB_API_URL` (or `SAB_API_URL_DEFAULT`) points at the right host.

### `Destination already exists, skipping`

A book with that exact filename is already in the author's folder, so the script left both alone rather than overwriting your copy (this also means cleanup is skipped, since the move didn't "succeed"). If you actually want re-downloads to replace the existing file, set `OVERWRITE = True`.

## License

Open-source under the MIT License. See [LICENSE](LICENSE) for details.
