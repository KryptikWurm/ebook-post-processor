#!/usr/bin/env python3
"""
ebook_pp.py - SABnzbd post-processing script for eBooks.

After an eBook download finishes, this hook files it into a clean library and
tidies up after SABnzbd:

1. Moves the eBook file(s) to {EBOOK_DEST}/{Author Name}/ (the in-container
   mount of the NAS eBooks library, e.g. ${MEDIA_PATH}/eBooks -> /books)
2. Deletes the leftover completed job folder under /downloads/completed
3. Removes the job's entry from SABnzbd via the History API. This is deferred to
   a detached child process, because SABnzbd won't delete a job from history
   while its own post-processing script is still running.

Author detection reads the EPUB metadata (dc:creator) first, then falls back to
parsing the download name ("Author - Title"), then "Unknown Author".

Built to never break automation: any failure leaves files where they are, the
destructive cleanup runs only after every move succeeds, and the script always
exits 0 so the SABnzbd pipeline never stalls.

Pure Python standard library - no external binaries or packages required, so it
runs as-is inside the stock lscr.io/linuxserver/sabnzbd image.
"""

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import xml.etree.ElementTree as ET

# === configuration ===
# Destination library root. This is the path *inside the SABnzbd container*; the
# NAS eBooks share is bind-mounted here (e.g. compose: "${MEDIA_PATH}/eBooks:/books").
# Books are filed under EBOOK_DEST/<Author Name>/.
EBOOK_DEST = "/books"
EBOOK_EXTS = (".epub", ".mobi", ".azw3", ".azw", ".pdf")
UNKNOWN_AUTHOR = "Unknown Author"
LOG_FILE = "/config/ebook_pp.log"
DRY_RUN = False
DELETE_FILES = 0          # del_files flag for the SAB history delete call (0 = entry only)
API_TIMEOUT = 15          # seconds for the SABnzbd API call
OVERWRITE = False         # if a dest file already exists, skip (non-destructive) by default
RENAME = True             # rename filed eBooks to "{Author} - {Title}{ext}"; keep the
                          # original name when no title can be determined
# SABnzbd won't delete a job from history while its post-processing script is
# still running, so the history delete is deferred to a detached child that
# waits this many seconds (until this script has exited and the job is marked
# finished) before calling the API.
HISTORY_DELETE_DELAY = 30  # seconds
# Fallback SABnzbd API URL, used only when the SAB_API_URL env var isn't set
# (i.e. running the script outside SABnzbd). SABnzbd supplies SAB_API_URL at
# runtime, so this can normally stay empty; set it to your instance's API
# endpoint (e.g. "http://localhost:8080/api") to test the history delete by
# hand. With neither set, the history delete is skipped with a warning.
SAB_API_URL_DEFAULT = ""

log = logging.getLogger("ebook_pp")

# XML namespaces used inside an EPUB's OPF package document.
OPF_NS = {
    "container": "urn:oasis:names:tc:opendocument:xmlns:container",
    "opf": "http://www.idpf.org/2007/opf",
    "dc": "http://purl.org/dc/elements/1.1/",
}

def setup_logging():
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s  %(levelname)-7s  %(message)s")
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.addHandler(sh)
    # Best-effort log file: SABnzbd captures stdout, so an unwritable LOG_FILE
    # must not crash the post-processor (a non-zero exit would fail the job).
    try:
        log_dir = os.path.dirname(LOG_FILE)
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)
        fh = logging.FileHandler(LOG_FILE, encoding="utf-8", errors="surrogateescape")
        fh.setFormatter(fmt)
        log.addHandler(fh)
    except OSError as e:
        log.warning("Could not open log file %s (%s); logging to stdout only.", LOG_FILE, e)

def get_job_dir():
    d = os.environ.get("SAB_COMPLETE_DIR")
    if not d and len(sys.argv) > 1 and sys.argv[1]:
        d = sys.argv[1]
    return d

def download_failed():
    status = os.environ.get("SAB_PP_STATUS")
    if status is None and len(sys.argv) > 7:
        status = sys.argv[7]
    if status is None:
        return False
    try:
        return int(status) != 0
    except ValueError:
        return False

def find_ebooks(root):
    found = []
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            if name.lower().endswith(EBOOK_EXTS):
                found.append(os.path.join(dirpath, name))
    return sorted(found)

def read_opf_root(path):
    """Open an EPUB and return its parsed OPF package root element, or None.

    Reads META-INF/container.xml to locate the OPF package document. Any
    structural problem (not a zip, missing parts, bad XML) yields None so callers
    can fall back to filename parsing."""
    try:
        with zipfile.ZipFile(path) as zf:
            with zf.open("META-INF/container.xml") as cf:
                container = ET.parse(cf).getroot()
            rootfile = container.find(
                "container:rootfiles/container:rootfile", OPF_NS)
            if rootfile is None:
                return None
            opf_path = rootfile.get("full-path")
            if not opf_path:
                return None
            with zf.open(opf_path) as of:
                return ET.parse(of).getroot()
    except (zipfile.BadZipFile, KeyError, ET.ParseError, OSError) as e:
        log.debug("  EPUB metadata read failed on %s: %s", os.path.basename(path), e)
        return None

def opf_author(opf):
    """Return the author named in a parsed OPF root (dc:creator), or None.

    Prefers a <dc:creator> tagged with the 'aut' role."""
    creators = opf.findall(".//dc:creator", OPF_NS)
    if not creators:
        return None
    # Prefer a creator explicitly tagged as the author ('aut' role); the role
    # attribute lives in the opf namespace (opf:role).
    role_attr = "{%s}role" % OPF_NS["opf"]
    for c in creators:
        if (c.get(role_attr) or "").lower() == "aut" and (c.text or "").strip():
            return c.text.strip()
    for c in creators:
        if (c.text or "").strip():
            return c.text.strip()
    return None

def opf_title(opf):
    """Return the title named in a parsed OPF root (dc:title), or None."""
    for t in opf.findall(".//dc:title", OPF_NS):
        if (t.text or "").strip():
            return t.text.strip()
    return None

def extract_epub_metadata(path):
    """Return (author, title) from an EPUB's metadata; either may be None.

    Opens and parses the EPUB once for both values. Returns (None, None) on any
    structural problem so the caller can fall back to filename parsing."""
    opf = read_opf_root(path)
    if opf is None:
        return None, None
    return opf_author(opf), opf_title(opf)

def _strip_ebook_ext(name):
    """Drop a trailing eBook extension from a name, if present."""
    for ext in EBOOK_EXTS:
        if name.lower().endswith(ext):
            return name[: -len(ext)]
    return name

def parse_author_from_name(name):
    """Extract the author from a 'Author - Title' name, or None.

    Strips any eBook extension first, then takes the text before the first
    ' - ' delimiter."""
    base = _strip_ebook_ext(name)
    if " - " not in base:
        return None
    author = base.split(" - ", 1)[0].strip()
    return author or None

def parse_title_from_name(name):
    """Extract the title from a 'Author - Title' name, or None.

    Strips any eBook extension first, then takes the text after the first
    ' - ' delimiter."""
    base = _strip_ebook_ext(name)
    if " - " not in base:
        return None
    title = base.split(" - ", 1)[1].strip()
    return title or None

def sanitize_component(name):
    """Make a string safe to use as a single path component.

    Strips filesystem-illegal characters, collapses whitespace, and trims
    leading/trailing dots and spaces. May return '' if nothing usable remains."""
    cleaned = re.sub(r'[\\/:*?"<>|]', "", name or "")
    return re.sub(r"\s+", " ", cleaned).strip().strip(".").strip()

def sanitize_author(name):
    """Sanitize an author into a path component, falling back to UNKNOWN_AUTHOR."""
    return sanitize_component(name) or UNKNOWN_AUTHOR

def sanitize_title(name):
    """Sanitize a title for use in a filename.

    A subtitle colon is turned into a ' - ' separator ('Empire: Mistborn' ->
    'Empire - Mistborn') before the colon would otherwise be stripped as an
    illegal character; any separator left dangling by a trailing colon is
    dropped. May return ''."""
    name = re.sub(r"\s*:\s*", " - ", name or "")
    cleaned = sanitize_component(name)
    return re.sub(r"\s*-\s*$", "", cleaned).strip()

def determine_author(path, job_name, epub_author=None):
    """Resolve the destination author folder for an eBook.

    EPUB metadata (pre-extracted by the caller) first, then 'Author - Title'
    parsing of the filename and the SABnzbd job name, then UNKNOWN_AUTHOR. The
    result is always sanitized."""
    author = epub_author
    if author:
        log.info("  Author from EPUB metadata: %s", author)
    if not author:
        author = parse_author_from_name(os.path.basename(path))
        if not author and job_name:
            author = parse_author_from_name(job_name)
        if author:
            log.info("  Author from name: %s", author)
    if not author:
        log.warning("  Author not found; using %r.", UNKNOWN_AUTHOR)
    return sanitize_author(author)

def determine_title(path, job_name, epub_title=None):
    """Resolve the book title for renaming, or None if it can't be found.

    EPUB metadata (pre-extracted by the caller) first, then 'Author - Title'
    parsing of the filename and the SABnzbd job name. Returns None (caller keeps
    the original filename) when no title can be determined; the result is
    otherwise raw (sanitized at use)."""
    title = epub_title
    if title:
        log.info("  Title from EPUB metadata: %s", title)
    if not title:
        title = parse_title_from_name(os.path.basename(path))
        if not title and job_name:
            title = parse_title_from_name(job_name)
        if title:
            log.info("  Title from name: %s", title)
    return title

def target_filename(path, author, title):
    """Build the filename to file an eBook under, keeping its extension.

    With RENAME on and a known title, returns '{Author} - {Title}{ext}';
    otherwise the original filename is preserved (NO renaming)."""
    original = os.path.basename(path)
    if not RENAME:
        return original
    safe_title = sanitize_title(title) if title else ""
    if not safe_title:
        return original
    ext = os.path.splitext(original)[1]
    return "%s - %s%s" % (author, safe_title, ext)

def move_ebook(path, author, dest_name=None, dry_run=False):
    """Move one eBook into EBOOK_DEST/<author>/ as dest_name. Returns True on
    success.

    dest_name defaults to the file's original basename. A pre-existing
    destination file is left untouched (skip + warn) unless OVERWRITE is set, so
    a re-download never silently clobbers the library."""
    dest_dir = os.path.join(EBOOK_DEST, author)
    dest = os.path.join(dest_dir, dest_name or os.path.basename(path))

    if os.path.exists(dest) and not OVERWRITE:
        log.warning("  Destination already exists, skipping: %s", dest)
        return False

    if dry_run:
        log.info("  DRY RUN: would move -> %s", dest)
        return True

    # The job folder and EBOOK_DEST are usually different mounts, so the move
    # degrades to copy+delete. Stage the copy under a temp name in the
    # destination directory and rename into place, so an interrupted copy can
    # never leave a truncated book at the final name (which the exists-check
    # above would treat as the real copy on a retry).
    tmp = None
    try:
        os.makedirs(dest_dir, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".ebook_pp_", dir=dest_dir)
        os.close(fd)
        shutil.move(path, tmp)
        os.replace(tmp, dest)
        log.info("  Moved -> %s", dest)
        return True
    except OSError as e:
        log.error("  Failed to move %s -> %s: %s", os.path.basename(path), dest, e)
        return False
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass

def delete_job_folder(job_dir, dry_run=False):
    """Recursively delete the completed job folder. Returns True on success."""
    if dry_run:
        log.info("  DRY RUN: would delete job folder %s", job_dir)
        return True
    try:
        shutil.rmtree(job_dir)
        log.info("  Deleted job folder: %s", job_dir)
        return True
    except OSError as e:
        log.error("  Failed to delete job folder %s: %s", job_dir, e)
        return False

def delete_history_now(nzo_id, api_key, api_url=None):
    """Issue the SABnzbd History delete API call immediately. Returns True on
    success. Never raises - a failed call is logged and swallowed so it can't
    fail the post-processing job.

    Note: SABnzbd ignores this for a job whose post-processing script is still
    running, so the live pipeline calls it via schedule_history_delete() (a
    deferred child); this is only invoked directly once the script has exited."""
    api_url = api_url or os.environ.get("SAB_API_URL") or SAB_API_URL_DEFAULT
    if not nzo_id or not api_key:
        log.warning("  Skipping SABnzbd history delete: missing nzo_id or API key.")
        return False
    if not api_url:
        log.warning("  Skipping SABnzbd history delete: no API URL "
                    "(set SAB_API_URL or SAB_API_URL_DEFAULT).")
        return False

    params = {
        "mode": "history",
        "name": "delete",
        "value": nzo_id,
        "del_files": DELETE_FILES,
        "apikey": api_key,
        "output": "json",
    }
    url = api_url + "?" + urllib.parse.urlencode(params)
    # Don't log the API key.
    safe_url = url.replace(api_key, "***")

    try:
        with urllib.request.urlopen(url, timeout=API_TIMEOUT) as resp:
            body = resp.read().decode("utf-8", errors="replace")
        data = json.loads(body)
        if data.get("status") is True:
            log.info("  Removed SABnzbd history entry %s.", nzo_id)
            return True
        log.error("  SABnzbd history delete reported failure: %s", body.strip())
        return False
    except (urllib.error.URLError, json.JSONDecodeError, OSError, ValueError) as e:
        log.error("  SABnzbd history delete call failed (%s): %s", safe_url, e)
        return False

def schedule_history_delete(nzo_id, api_key, dry_run=False):
    """Defer the SABnzbd history delete to a detached child process.

    A job can't delete itself from history while its post-processing script is
    still running (SABnzbd doesn't consider it finished), so we spawn a fully
    detached copy of this script that sleeps HISTORY_DELETE_DELAY seconds - long
    enough for this process to exit and the job to be marked finished - then
    calls the API. The child has its own session and /dev/null stdio so SABnzbd
    sees this script finish promptly. The nzo_id/API key/URL are read from the
    inherited SAB_* environment by the child. Never raises."""
    if not nzo_id or not api_key:
        log.warning("  Skipping SABnzbd history delete: missing nzo_id or API key.")
        return False

    if dry_run:
        log.info("  DRY RUN: would schedule SABnzbd history delete of %s in %ds.",
                 nzo_id, HISTORY_DELETE_DELAY)
        return True

    try:
        devnull = os.open(os.devnull, os.O_RDWR)
        proc = subprocess.Popen(
            [sys.executable, os.path.abspath(__file__),
             "--delayed-history-delete", str(HISTORY_DELETE_DELAY)],
            stdin=devnull, stdout=devnull, stderr=devnull,
            start_new_session=True, close_fds=True,
        )
        os.close(devnull)
        log.info("  Scheduled SABnzbd history delete of %s in %ds (pid %s).",
                 nzo_id, HISTORY_DELETE_DELAY, proc.pid)
        return True
    except OSError as e:
        log.error("  Could not schedule SABnzbd history delete: %s", e)
        return False

def run_delayed_history_delete():
    """Entry point for the detached child spawned by schedule_history_delete():
    wait out the delay, then delete the job from history. Reads the nzo_id, API
    key and URL from the inherited SAB_* environment."""
    setup_logging()
    try:
        delay = int(sys.argv[2])
    except (IndexError, ValueError):
        delay = HISTORY_DELETE_DELAY
    time.sleep(delay)
    log.info("Deferred history delete firing (waited %ds).", delay)
    delete_history_now(
        os.environ.get("SAB_NZO_ID"),
        os.environ.get("SAB_API_KEY"),
        os.environ.get("SAB_API_URL"),
    )
    return 0

def main():
    setup_logging()
    log.info("=" * 40)
    log.info("ebook_pp starting")

    job_dir = get_job_dir()
    if not job_dir or not os.path.isdir(job_dir):
        log.error("Job directory not found or not provided: %r. Exiting 0.", job_dir)
        return 0

    if download_failed():
        log.info("Download marked failed by SABnzbd; skipping. Exiting 0.")
        return 0

    job_name = os.environ.get("SAB_FINAL_NAME") or os.path.basename(job_dir.rstrip("/"))
    log.info("Job folder: %s", job_dir)

    ebooks = find_ebooks(job_dir)
    if not ebooks:
        log.info("No eBook files found. Leaving job untouched. Exiting 0.")
        return 0

    log.info("Found %d eBook file(s).", len(ebooks))
    moved = 0
    for path in ebooks:
        log.info("Filing: %s", os.path.basename(path))
        try:
            epub_author, epub_title = (extract_epub_metadata(path)
                                       if path.lower().endswith(".epub") else (None, None))
            author = determine_author(path, job_name, epub_author)
            dest_name = target_filename(path, author, determine_title(path, job_name, epub_title))
            if dest_name != os.path.basename(path):
                log.info("  Filing as: %s", dest_name)
            if move_ebook(path, author, dest_name, DRY_RUN):
                moved += 1
        except Exception as e:
            log.error("Unexpected error filing %s: %s", os.path.basename(path), e)

    if moved != len(ebooks):
        log.error("Only %d of %d eBook(s) filed; skipping cleanup so nothing is lost.",
                  moved, len(ebooks))
        log.info("Finished with errors. Exiting 0 so the pipeline continues.")
        return 0

    log.info("All %d eBook(s) filed. Cleaning up.", moved)

    # Cleanup order: delete the completed folder, then drop the SABnzbd entry.
    # The history delete is deferred to a detached child because SABnzbd won't
    # remove a job while this post-processing script is still running.
    delete_job_folder(job_dir, DRY_RUN)
    schedule_history_delete(
        os.environ.get("SAB_NZO_ID"),
        os.environ.get("SAB_API_KEY"),
        dry_run=DRY_RUN,
    )

    log.info("Finished. Exiting 0.")
    return 0

if __name__ == "__main__":
    # Detached child invoked by schedule_history_delete() to do the deferred
    # history delete after this script has exited.
    if len(sys.argv) > 1 and sys.argv[1] == "--delayed-history-delete":
        sys.exit(run_delayed_history_delete())
    sys.exit(main())
