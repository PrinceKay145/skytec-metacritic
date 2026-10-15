# Metacritic games crawler

Every hour this service picks 20 games from Metacritic that it hasn't processed yet that day,
then adds them to its database or updates them. It stores the cover, platforms with Metascore
and user score, developer, description and video link, plus an LLM summary of what critics and
players like and dislike, and shows the games in a web UI.

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

## Web UI

- `/`: every game with its cover, developer, platforms, Metascore and user score. Search by
  title, filter by platform and sort by Metascore, user score, recently added or title. The
  filters update the list in place with [htmx](https://htmx.org) and keep the URL shareable.
  With a platform selected, the scores shown and sorted on are that platform's; otherwise
  each game shows its best score across platforms.
- `/games/{slug}`: the game card with cover, developer, description, per-platform scores, the
  video link, a link to the game on Metacritic, and the critic and player summaries.
- `/health`: checks the app and its database.

## Crawl schedule

- A run starts when the app starts and then every `CRAWL_INTERVAL_MINUTES` (60 by default).
- **The crawl day is the UTC date.** The first run after 00:00 UTC takes the 20 games in
  New Releases. Later runs that day page through "See all, newest first", skipping games
  already processed that day.
- A run that fails for one game or one platform logs the error in the run's `error_log` and
  carries on. Runs still marked "running" when the app starts are marked failed.

## Review summaries

For each game the crawler reads the latest critic reviews (10) and user reviews (50). It asks an
LLM for up to five likes and five dislikes per source, and stores them with a hash of the
reviews it used. The LLM is called again only when those reviews change.

Any OpenAI-compatible chat API works, set through `LLM_BASE_URL`, `LLM_API_KEY` and `LLM_MODEL`.
The default is a local [Ollama](https://ollama.com) with `gemma3:4b`
(`ollama pull gemma3:4b`). Inside Docker, `localhost` is the container itself, so point
`LLM_BASE_URL` at `http://host.docker.internal:11434/v1` or a hosted API. If the LLM is down, the game is still saved and the failure goes into
the run's `error_log`.
