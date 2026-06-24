# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added
- Initial release of `ebook_pp.py`, a SABnzbd post-processing script for eBooks. After a download finishes it files the eBook(s) into `{EBOOK_DEST}/{Author Name}/`, deletes the leftover completed job folder, and removes the job's entry from SABnzbd via the History API. Pure Python standard library — no external binaries or packages — so it runs as-is inside the stock `lscr.io/linuxserver/sabnzbd` image.
- **Container-mounted library path:** `EBOOK_DEST` defaults to `/books`, the in-container mount of the NAS eBooks share (compose: `${MEDIA_PATH}/eBooks:/books`), rather than a host path the container can't see. Books are written through the bind mount onto the NAS.
- **Docs: category-based automatic post-processing.** README gains a "Category setup" section explaining how to wire the script to a SABnzbd category (Config → Categories → Script = `ebook_pp.py`) so it runs with no manual post-processing, what to set Folder/Path to (the completed-downloads area that becomes `SAB_COMPLETE_DIR`, not `/books`), and how jobs get *into* that category automatically — via the `.nzb`'s `<meta type="category">` header (honored from the Watched Folder), loose indexer-tag matching, or the requesting app's `&cat=` parameter. Mirrored as a short note in `CLAUDE.md`.
- **Author detection** (`determine_author()`): reads the author from EPUB metadata first (`extract_epub_author()` parses `META-INF/container.xml` → the OPF package document → `dc:creator`, preferring an `opf:role="aut"` creator), then falls back to parsing the download name as `Author - Title` (`parse_author_from_name()`), then to `Unknown Author`. The chosen name is run through `sanitize_author()`, which strips filesystem-illegal characters, collapses whitespace, and trims stray dots/spaces so it is safe as a single path component.
- **Format filtering** (`find_ebooks()`): walks the completed job folder and moves only `.epub`, `.mobi`, `.azw3`, `.azw`, and `.pdf` files (`EBOOK_EXTS`), ignoring NFOs, cover art and other cruft.
- **Non-destructive moves** (`move_ebook()`): creates `EBOOK_DEST/<author>/` and moves each eBook in. A pre-existing destination file is skipped with a warning unless `OVERWRITE` is set, so a re-download never silently clobbers the library.
- **Cleanup, gated on success**: the completed job folder is removed (`delete_job_folder()`) and the SABnzbd history entry is deleted (`sab_delete_history()`, `mode=history&name=delete` with `del_files=0`) **only after every eBook moved successfully**. If any move fails, cleanup is skipped and everything is left in place for inspection. The API URL resolves from the `SAB_API_URL` env var with a configurable `SAB_API_URL_DEFAULT` fallback; the API key is never logged.
- **Safety / non-blocking by design**: reads the job directory from `SAB_COMPLETE_DIR` (or `argv[1]`), skips jobs SABnzbd marked failed (`SAB_PP_STATUS`/`argv[7]`), logs to stdout plus a best-effort `LOG_FILE`, wraps every operation so an unexpected error is logged rather than fatal, and **always exits 0** so the SABnzbd pipeline never stalls. A `DRY_RUN` mode logs every intended move/delete/API call without performing it.
