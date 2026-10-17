# ArtLayerr

> **Renamed:** ArtLayerr was previously called Layerr. If you installed the old image
> (`ghcr.io/minimaxpower/layerr`), switch to `ghcr.io/minimaxpower/artlayerr`: the old one no longer receives updates.

A web UI that lets you scroll through the images that Plex provides for each item (movie, show, season, episode, background) and pick the one you want with a single click.

When you click on an image, it is downloaded to a dedicated `assets` directory with the correct pathing and naming for Kometa.

You have to fill in the `.env` file for it to work.

## Requirements

- A Plex Media Server reachable from the machine running ArtLayerr
- Your [Plex token](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/)
- Docker with Docker Compose (on Synology: Container Manager)
- The folder where Kometa reads its assets, so ArtLayerr can write images there

## Installation (Docker Compose, recommended)

1. Create a folder for ArtLayerr and go into it:
   ```
   mkdir artlayerr && cd artlayerr
   ```

2. Create a `docker-compose.yml`:
   ```yaml
   services:
     artlayerr:
       image: ghcr.io/minimaxpower/artlayerr:latest
       container_name: artlayerr
       restart: unless-stopped
       init: true
       ports:
         - "3535:5000"
       volumes:
         - ${ASSETS_DIR}:/assets
       environment:
         - PLEX_BASE_URL=${PLEX_BASE_URL}
         - PLEX_TOKEN=${PLEX_TOKEN}
         - PLEX_ASSET_DIR=/assets
         - SECRET_KEY=${SECRET_KEY}
   ```

3. Create a `.env` file next to it (you can start from [`.env.example`](.env.example)):
   ```
   PLEX_BASE_URL=http://YOUR_PLEX_IP:32400
   PLEX_TOKEN=YOUR_PLEX_TOKEN
   SECRET_KEY=RANDOM_SECRET_KEY_OF_YOUR_CHOICE
   ASSETS_DIR=/path/to/your/assets
   ```

   | Variable | Description |
   |---|---|
   | `PLEX_BASE_URL` | Address of your Plex server, e.g. `http://192.168.1.10:32400` |
   | `PLEX_TOKEN` | Your Plex authentication token |
   | `SECRET_KEY` | Any long random string (generate one with `openssl rand -base64 32`) |
   | `ASSETS_DIR` | Host folder where images are saved (your Kometa assets directory) |

4. Start it:
   ```
   docker compose up -d
   ```

5. Open `http://YOUR_SERVER_IP:3535`.

To update to a newer version:

```
docker compose pull
docker compose up -d
```

### Synology (Container Manager)

1. Copy `docker-compose.yml` and `.env` to a folder on your NAS (for example `/volume1/docker/artlayerr`).
2. In Container Manager, go to **Project → Create**, choose that folder as the path, and select "Use existing docker-compose.yml".
3. Start the project, then open `http://YOUR_NAS_IP:3535`.

## Usage

The connection page should be filled in automatically if you completed the `.env` correctly:

![](images/connect.jpg)

After that you'll access the dashboard with every library of your Plex server. To begin with, I recommend you click the update button, especially if you already have some files in the assets directory.
Then you can click on any library you want:

![](images/libraries-dashboard.jpg)

You'll see a grid displaying all the movies of that library.
You also get a visual indication of whether a movie already has a poster/background selected in the Kometa assets directory.

![](images/movies-list.jpg)

You can also search by name, or only filter by movies missing a poster/background:

![](images/movies-filters.jpg)

If you click on a movie, you'll see a grid displaying all the posters available on Plex for that movie (you can also download one from a URL or import one from your computer):

![](images/movies-posters.jpg)

You can choose to view backgrounds instead of posters:

![](images/movies-backgrounds.jpg)

When you click the Download button, the file is downloaded to the assets directory, and a flash message reports the path:

![](images/movies-download.jpg)

Clicking the title/logo in the upper left takes you back to the libraries dashboard.
Clicking the library name brings you back to the items list of that library.

In a Show library, you'll see a grid displaying all the shows of that library.
You also get a visual indication of whether a show already has a poster/background/season poster/episode image selected in the Kometa assets directory

![](images/shows-list.jpg)

You can also search by name, or only filter by shows missing a poster/background/poster season/episode image:
Green indicates you have everything (for example all seasons and all episodes). Blue indicates that you're missing some. A good way to keep visual track of what is incomplete!

![](images/shows-filters.jpg)

If you click on a show, you'll get the same kind of grid as for movies:

![](images/shows-posters.jpg)

With more options, as you can also access seasons:

![](images/seasons-posters.jpg)

And episode images:

![](images/episodes-posters.jpg)

You can also select multiple images and compare them :

![](images/compare.jpg)

If you click on Mediux Check, you can select one or more [Mediux](https://mediux.pro) files to compare them with your library and see if you're missing anything.

![](images/mediux-check.jpg)

## Credits

ArtLayerr started as a fork of [Plex Image Picker](https://github.com/chazlarson/Media-Scripts/tree/main/Plex%20Image%20Picker)
by chazlarson and has since been heavily modified.

## License

MIT, see [LICENSE](LICENSE).
