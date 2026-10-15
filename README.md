# Metacritic games crawler

Every hour this service picks 20 games from Metacritic that it hasn't processed yet that day,
then adds them to its database or updates them. It stores the cover, platforms with Metascore
and user score, developer, description and video link, and shows the games in a web UI.

Data comes from Metacritic's JSON backend, not HTML scraping. See
[docs/metacritic-api.md](docs/metacritic-api.md).

## Running

```bash
cp .env.example .env
docker compose up --build        # app on http://localhost:8000, PostgreSQL in a container
```

Local development uses SQLite and [uv](https://docs.astral.sh/uv/):

```bash
make run     # web app with the hourly scheduler
make crawl   # one crawl run, then exit
make test
make lint
```

All settings are environment variables, listed with defaults in [.env.example](.env.example).

## Crawl schedule

- A run starts when the app starts and then every `CRAWL_INTERVAL_MINUTES` (60 by default).
- **The crawl day is the UTC date.** The first run after 00:00 UTC takes the 20 games in
  New Releases. Later runs that day page through "See all, newest first", skipping games
  already processed that day.
- A run that fails for one game or one platform logs the error in the run's `error_log` and
  carries on. Runs still marked "running" when the app starts are marked failed.
