# ReelReady

English | [中文](README.md)

Movies reach cinemas long before they are released online, and private trackers only get WEB-DL releases once that happens. ReelReady **collects highly rated movies currently in cinemas** in China and abroad, keeps checking whether they have been released digitally, and the moment a torrent matching your rules shows up on your PT sites it **sends it to qBittorrent** and emails you.

## Features

- **Collection**: Douban "now showing" plus TMDB now-playing lists for several regions, filtered by rating and vote count (Douban and TMDB are evaluated separately; passing either is enough). Old movies are excluded by their primary release date by default. Enable "Collect re-released movies" under Settings → Collection to include current re-releases while keeping the original year. Turning it off keeps movies already added
- **Candidate / Monitoring / Completed / Blacklist** lists: collected movies wait for your approval; blacklisted movies are never collected again; you can also add a movie by pasting a Douban / TMDB / IMDb link
- **Release tracking**. Stages only move forward: `In cinemas → Dated → Streaming → Downloaded`
  - Dated: TMDB lists a digital or physical release date
  - Streaming: TMDB watch providers (JustWatch data) show it can be streamed, rented or bought, or Douban lists online platforms
- **PT search and automatic download**: M-Team (official API) and NexusPHP sites. Each scan picks the single best torrent across all sites and pushes it to qBittorrent
- **Cookies via CookieCloud**: just stay logged in to your sites in the browser and cookies are synced automatically. ReelReady also detects the NexusPHP sites you are logged into from the synced cookies
- **Email**: successful download submission, download failures and expired site logins are sent separately; dated / streaming / new candidates go into a daily digest. Submission mail includes the original event time, and each sent message is recorded separately with concurrent sending prevented. SMTP presets for QQ, 163, 126, Gmail, iCloud, Outlook and more; multiple recipients supported
- **Event management**: delete individual records or set a retention period; leaving it blank keeps records permanently
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
- **NexusPHP sites**: sync cookies with CookieCloud (below), or paste a cookie manually

## Syncing cookies with CookieCloud

[CookieCloud](https://github.com/easychen/CookieCloud) is an open source browser extension that uploads your cookies, end-to-end encrypted, to a server. ReelReady implements the CookieCloud server protocol, so no extra service is needed.

1. Install CookieCloud in Edge from the Chrome Web Store (Edge can install Chrome Web Store extensions)
2. On ReelReady's Sites page, copy the server address, user KEY and end-to-end password into the extension, mode "upload to server"
3. Optionally limit the synced domains to your PT sites
4. Click "sync now" in the extension and reload the Sites page; detected NexusPHP sites appear under "Add site" and can be added with one click

From then on, logging in to a site in the browser is all it takes. If a cookie expires you get an email; log in again and the next sync fixes it.

> Cookies are stored encrypted in your local `data/` folder and never sent to any third party.

## Local development

Requires Python 3.12+:

```bash
pip install -r requirements.txt
```

```bash
python -m reelready
```

Data goes to `./data` by default; override with `REELREADY_DATA_DIR` and `REELREADY_PORT`.

## Notes

- Please follow the rules of your PT sites. ReelReady only searches for monitored movies and waits between requests (5 s by default)
- This project only aggregates information and drives your download client; it does not provide any media

## License

[Apache-2.0](LICENSE)
