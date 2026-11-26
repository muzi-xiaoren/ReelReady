# ReelReady

English | [中文](README.md)

Movies reach cinemas long before they are released online, and private trackers only get WEB-DL releases once that happens. ReelReady **collects highly rated movies currently in cinemas** in China and abroad, keeps checking whether they have been released digitally, and the moment a torrent matching your rules shows up on your PT sites it **sends it to qBittorrent** and emails you.

## Features

- **Personal ratings and reviews**: hover a poster to rate it from 1–5 stars; the adjacent edit button opens a review dialog. Evaluations are saved locally and included in record backups. Only the explicit “also mark watched” checkbox pauses monitoring and automatic downloads.
- **Evaluation sync**: sync one movie from its review dialog or all evaluated movies from More → Global sync, across all lists. Preview first; conflicts default to skip, with separate rating/review decisions and bulk overwrite/skip controls. Douban uses synced CookieCloud login cookies over a direct connection, preserves privacy and tags, and supports ratings and short reviews up to 350 characters; submission marks the movie watched on Douban without posting a separate broadcast. TMDB requires account authorization and supports ratings only (5 stars = 10 points), as its public API has no review-writing endpoint. Remote read failures stop the affected item; changed values are rechecked before publishing. Empty local fields never delete remote content. Sync is always initiated and confirmed by the user.

- **Immersive library**: posters, titles and compact ratings lead the page. Round category and navigation menus hug the bottom dock, stay open until toggled again, and retain their state across pages. The dock can hide completely and reappear from an invisible bottom-edge target. Activity uses a filtered timeline, site connection details collapse by default, and settings are grouped with automatic saving. Tap a poster for full details on mobile.
- **Collection**: Douban "now showing" plus TMDB now-playing lists for several regions, filtered by rating and vote count (Douban and TMDB are evaluated separately; passing either is enough). Old movies are excluded by their primary release date by default. Enable "Collect re-released movies" under Settings → Collection to include current re-releases while keeping the original year. Turning it off keeps movies already added
- **Recent high-score discovery**: supplement cinema feeds with Douban movies using the configured age, rating and vote thresholds. Persistent cursors continue beyond the first 150 entries across runs (up to 6 pages total and 3 per year per run). Failed details are queued with backoff and retried in later collection runs, up to 20 per run. The latest decisions for 2,000 films are retained for diagnosis. Feed coverage and ordering depend on Douban; exhaustive discovery is not guaranteed.
- **Candidate / Monitoring / Completed / Watched / Abandoned** lists: collected movies wait for your approval. Mark a movie as watched to pause monitoring and downloads while preserving its records; restore it to return to the previous list. A downloaded movie you gave up on can be moved to Abandoned with the × button. Watched and abandoned movies stay out of automatic collection. You can also add a movie by pasting a Douban / TMDB / IMDb link and choose whether it goes to Monitoring, Completed, Watched or Abandoned; the last three skip the collection age limit and never trigger checks or downloads.
- **Movie identity checks**: downloads require an independent matching IMDb ID. Missing list identities are checked against torrent details, with up to 10 extra checks per movie per scan. Mismatched or unverified identities appear in rejected results and cannot be downloaded directly. A purchase-only listing cannot override a future digital release date or establish availability without a date.
- **Release tracking**. Stages only move forward: `In cinemas → Dated → Streaming → Downloaded`
  - Dated: TMDB lists a digital or physical release date
  - Streaming: TMDB watch providers (JustWatch data) show it can be streamed, rented or bought, or Douban lists online platforms
- **PT search and automatic download**: M-Team (official API), Rousi Pro (API key), NexusPHP sites and Monikadesign. Each scan picks the single best torrent across all sites and pushes it to qBittorrent
- **Cookies via CookieCloud**: just stay logged in to your sites in the browser and cookies are synced automatically. ReelReady also detects the NexusPHP sites you are logged into from the synced cookies
- **Local playback**: completed movies get Play and Open folder buttons on their posters and detail pages, plus Copy path on the detail page. Progress and file locations come from qBittorrent, and unfinished downloads show a percentage. Choose the system default, PotPlayer, VLC, mpv or a custom program under Settings → Downloader → Local playback; changes apply immediately. Docker cannot launch programs on your computer, so import `reelready-protocol.reg` from that page once on Windows: it registers `reelready://` links with nothing left running, and an uninstall file is provided. If qBittorrent also runs in Docker, set a path mapping from its paths to local ones
- **Email**: successful download submission, download failures and expired site logins are sent separately; dated / streaming / new candidates go into a daily digest. Submission mail includes the original event time, and each sent message is recorded separately with concurrent sending prevented. SMTP presets for QQ, 163, 126, Gmail, iCloud, Outlook and more; multiple recipients supported
- **Event management**: delete individual records or set a retention period; leaving it blank keeps records permanently
- **Import / export**: under Settings → Import / Export, export either "Everything" or "Movies and events only" as JSON. "Everything" also carries all settings, sites, CookieCloud data and job records, so a new machine or reinstall is restored in one step; that file contains plaintext passwords, cookies and API keys, so keep it private. A full import overwrites settings and site configuration with the same name. Movies merge by Douban / TMDB / IMDb id and only missing details of existing movies are filled in; imported events are never mailed again, and the database is backed up first
- **Cloud backup**: under Settings → Cloud backup, choose "Movies and events only" (default) or "Everything" and upload it to a GitHub repository (preferably private; the token only needs Contents read/write on that repository) and / or a WebDAV folder such as Jianguoyun. Back up on demand or on a schedule; each target keeps only the latest N files. "Everything" includes plaintext settings and logins, so use a private target you trust; the GitHub token and WebDAV password themselves are never uploaded, and restoring a cloud backup keeps the ones already entered. Failures are logged as events and included in the daily digest
- **Settings auto-save**: reorder regions and resolutions; background tasks run sequentially and duplicate requests are not repeatedly queued
- **Catch-up on startup**: the computer does not have to run 24/7; missed checks run right after startup

## Torrent rules (defaults, all configurable)

| Rule | Default |
|---|---|
| Allowed resolutions (by priority) | 2160p > 1080p; nothing at 720p or below |
| Excluded | Cam / TS / TC recordings, remuxes, full discs |
| Size limit | 30 GB |
| Ranking | seeders → resolution → Chinese subtitles → HDR / Dolby Vision |

On the 1st of each month, a new patch version is released automatically if the code in the image has changed since the last release (changes to docs, tests or workflows alone do not trigger one); see [Releases](https://github.com/muzi-xiaoren/ReelReady/releases). Pin a version such as `ghcr.io/muzi-xiaoren/reelready:1.0.1`, or use `:latest` to follow releases. Images support `linux/amd64` and `linux/arm64`.

## Quick start (Windows + Docker Desktop)

1. Install and start [Docker Desktop](https://www.docker.com/products/docker-desktop/), and enable "Start Docker Desktop when you sign in" so ReelReady starts with your computer
2. Create a folder (e.g. `D:\ReelReady`) and put [`docker-compose.yml`](docker-compose.yml) in it
3. Open a terminal in that folder and run:

   ```bash
   docker compose up -d
   ```

4. Open <http://localhost:8765>

The database, cookies and settings live in the `data/` folder next to the compose file, so removing or upgrading the container keeps them.

To update:

```bash
docker compose pull
```

```bash
docker compose up -d
```

## First-time setup

On the **Settings** page:

1. **TMDB**: get a free API key at [themoviedb.org](https://www.themoviedb.org/settings/api). If TMDB is unreachable from your network, set a proxy under "Network" (e.g. `http://host.docker.internal:7890`)
2. **qBittorrent**: enable the Web UI in qBittorrent (Tools → Options → Web UI), then fill in its address (default `http://host.docker.internal:8080`, i.e. the host machine), username and password. Save path and category are optional
3. **Email**: pick your provider and enter the address and SMTP authorization code (QQ / 163) or app password (Gmail / iCloud). Add as many recipients as you like, or leave it empty to mail yourself. Use "Send test email" to verify

On the **Sites** page:

- **M-Team**: create an API key on the M-Team website and paste it
- **Rousi Pro**: choose "Rousi Pro (API Key)" and paste a personal API key created in your account settings (needs profile:read, torrent:read and torrent:download). Browser cookies cannot replace the key
- **Monikadesign**: sync the login cookie with CookieCloud, or add it manually as "Monikadesign (Cookie)"
- **NexusPHP sites**: sync cookies with CookieCloud (below), or paste a cookie manually

## Syncing cookies with CookieCloud

[CookieCloud](https://github.com/easychen/CookieCloud) is an open source browser extension that uploads your cookies, end-to-end encrypted, to a server. ReelReady implements the CookieCloud server protocol, so no extra service is needed.

1. Install CookieCloud in Edge from the Chrome Web Store (Edge can install Chrome Web Store extensions)
2. On ReelReady's Sites page, copy the server address, user KEY and end-to-end password into the extension, mode "upload to server"
3. Optionally limit the synced domains to your PT sites
4. Click "sync now" in the extension and reload the Sites page; detected NexusPHP sites appear under "Add site" and can be added with one click

From then on, logging in to a site in the browser is all it takes. If a cookie expires you get an email; log in again and the next sync fixes it.

> Cookies are stored encrypted in your local `data/` folder and never sent to any third party.

## Local playback (optional)

Completed movies get Play and Open folder buttons on their posters and detail pages. ReelReady runs in Docker and cannot start programs on your computer, so you register a `reelready://` link on Windows once: the browser hands the link to Windows, which opens the player or Explorer. Nothing keeps running and nothing starts at login. It only works on computers where the link is registered.

1. Under Settings → Downloader → Local playback, choose the default player (system default, PotPlayer, VLC, mpv or a custom program). Changes apply immediately without registering again
2. If qBittorrent runs directly on Windows, leave the path mapping empty. If it also runs in Docker, enter the download folder it sees (e.g. `/downloads`) and its real location on this computer (e.g. `D:\Movies`)
3. Download `reelready-protocol.reg` from that page and double-click it. It only writes `HKEY_CURRENT_USER\Software\Classes\reelready` and needs no administrator rights. If the browser blocks the `.reg` download, choose "Keep" in the download list, or run this in PowerShell (replace the address with the one you use for ReelReady):
   ```powershell
   $f = "$env:TEMP\reelready-protocol.reg"; Invoke-WebRequest "http://localhost:8765/playback/reelready-protocol.reg" -OutFile $f -UseBasicParsing; reg import $f
   ```
4. **Fully restart the browser.** Chrome and Edge only pick up newly registered links when they start. Closing all Chrome windows is enough. Edge has Startup boost on by default and keeps running after its windows close, so end every "Microsoft Edge" process in Task Manager, or turn off "Startup boost" and "Continue running background extensions and apps when Microsoft Edge is closed" under Settings → System and performance, then reopen it
5. The first time you click Play, the browser asks whether to open "Console Window Host" (Windows uses it to run the launcher script in the background, without a console window). Tick "Always allow" and click Open, and it won't ask again

The player usually appears within 1–5 seconds. If something goes wrong, such as a deleted file or a missing player, a "ReelReady" message box explains why. Import the file again if the address you use for ReelReady changes. To remove the link, download `reelready-protocol-uninstall.reg` from the same page and double-click it.

**If nothing happens when you click:** make sure the registry file was imported and the browser was fully restarted as in step 4. If no prompt appears at all, the browser usually hasn't been restarted. If the prompt appears but nothing opens, check the path mapping and player settings.

## Local development

Requires Python 3.12+:

```bash
pip install -r requirements.txt
```

```bash
python -m reelready
```

Data goes to `./data` by default; override with `REELREADY_DATA_DIR` and `REELREADY_PORT`.

The tests only use the standard library's `unittest`, so the dependencies above are enough:

```bash
python -m unittest discover -s tests
```

## Notes

- Please follow the rules of your PT sites. ReelReady only searches for monitored movies and waits between requests (5 s by default)
- This project only aggregates information and drives your download client; it does not provide any media

## License

[Apache-2.0](LICENSE)
