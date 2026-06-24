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
# SABnzbd won't delete a job from history while its post-processing script is
# still running, so the history delete is deferred to a detached child that
# waits this many seconds (until this script has exited and the job is marked
# finished) before calling the API.
HISTORY_DELETE_DELAY = 30  # seconds
# Fallback SABnzbd API URL, used only when the SAB_API_URL env var isn't set
# (i.e. running the script outside SABnzbd). SABnzbd supplies SAB_API_URL at runtime.
SAB_API_URL_DEFAULT = "https://nzb.example.com/api"

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

def extract_epub_author(path):
    """Return the author named in an EPUB's metadata (dc:creator), or None.

    Reads META-INF/container.xml to locate the OPF package document, then parses
    its <dc:creator> entries, preferring one tagged with the 'aut' role. Any
    structural problem (not a zip, missing parts, bad XML) yields None so the
    caller can fall back to filename parsing."""
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
                opf = ET.parse(of).getroot()
    except (zipfile.BadZipFile, KeyError, ET.ParseError, OSError) as e:
        log.debug("  EPUB metadata read failed on %s: %s", os.path.basename(path), e)
        return None

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

def parse_author_from_name(name):
    """Extract the author from a 'Author - Title' name, or None.

    Strips any eBook extension first, then takes the text before the first
    ' - ' delimiter."""
    base = name
    for ext in EBOOK_EXTS:
        if base.lower().endswith(ext):
            base = base[: -len(ext)]
            break
    if " - " not in base:
        return None
    author = base.split(" - ", 1)[0].strip()
    return author or None

def sanitize_author(name):
    """Make an author string safe to use as a single path component.

    Strips filesystem-illegal characters, collapses whitespace, and trims
    leading/trailing dots and spaces. Falls back to UNKNOWN_AUTHOR if nothing
    usable remains."""
    if not name:
        return UNKNOWN_AUTHOR
    cleaned = re.sub(r'[\\/:*?"<>|]', "", name)
    cleaned = re.sub(r"\s+", " ", cleaned).strip().strip(".").strip()
    return cleaned or UNKNOWN_AUTHOR

def determine_author(path, job_name):
    """Resolve the destination author folder for an eBook.

    EPUB metadata first, then 'Author - Title' parsing of the filename and the
    SABnzbd job name, then UNKNOWN_AUTHOR. The result is always sanitized."""
    author = None
    if path.lower().endswith(".epub"):
        author = extract_epub_author(path)
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

def move_ebook(path, author, dry_run=False):
    """Move one eBook into EBOOK_DEST/<author>/. Returns True on success.

    A pre-existing destination file is left untouched (skip + warn) unless
    OVERWRITE is set, so a re-download never silently clobbers the library."""
    dest_dir = os.path.join(EBOOK_DEST, author)
    dest = os.path.join(dest_dir, os.path.basename(path))

    if os.path.exists(dest) and not OVERWRITE:
        log.warning("  Destination already exists, skipping: %s", dest)
        return False

    if dry_run:
        log.info("  DRY RUN: would move -> %s", dest)
        return True

    try:
        os.makedirs(dest_dir, exist_ok=True)
        # If overwriting, remove the existing dest first so move can't fail/merge.
        if os.path.exists(dest) and OVERWRITE:
            os.remove(dest)
        shutil.move(path, dest)
        log.info("  Moved -> %s", dest)
        return True
    except OSError as e:
        log.error("  Failed to move %s -> %s: %s", os.path.basename(path), dest, e)
        return False

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
            author = determine_author(path, job_name)
            if move_ebook(path, author, DRY_RUN):
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
