# Metacritic JSON backend

Findings from 2026-10-15. The site is a server-rendered Nuxt app: pages embed their data in
`__NUXT_DATA__`, and every block of that data was fetched from `https://backend.metacritic.com`.
Those endpoints answer plain GET requests with no API key, cookies or special headers, so the
crawler calls them directly and never parses HTML.

## Endpoints

All paths are relative to `https://backend.metacritic.com`. Pagination is `offset`/`limit`;
responses carry `data.totalResults` and `links.{next,prev,first,last}.href`.

| Purpose | Path | Notes |
|---|---|---|
| New Releases (`/game/` "Newly Released") | `/finder/metacritic/web?sortBy=-releaseDate&metaScoreMin=1&mcoTypeId=13&offset=0&limit=20` | Exactly what the page renders: 20 items, only games that already have a Metascore |
| Browse, all-time, newest | `/finder/metacritic/web?sortBy=-releaseDate&mcoTypeId=13&offset=N&limit=24` | 24 per site page; `limit` max is 50 (51 returns 400). ~181k games |
| Game details | `/games/metacritic/{slug}/web` | `data.item`: title, description, developer, images, video, platforms, genres |
| Per-platform Metascore | `/reviews/metacritic/critic/games/{slug}/platform/{platform}/stats/web` | `data.item.score` 0–100, `null` under 4 reviews |
| Per-platform user score | `/reviews/metacritic/user/games/{slug}/platform/{platform}/stats/web` | `data.item.score` 0–10 |
| Critic reviews | `/reviews/metacritic/critic/games/{slug}/web?offset=0&limit=10&sort=score` | Server caps pages at 10. Fields: quote, score, publicationName, url, date, platform |
| User reviews | `/reviews/metacritic/user/games/{slug}/web?offset=0&limit=50&filterBySentiment=all&sort=date` | Up to 50 per page. Fields: id, quote, score, date, author, platform, thumbsUp/Down, spoiler |

Both review lists accept `filterBySentiment=all|positive|neutral|negative`. Each also has a
`/platform/{platform}/web` variant.

## Mapping to our models

- `Game.title`, `description`: `item.title`, `item.description`
- `Game.developer`: `item.production.companies[]` where `typeName == "Developer"`
- `Game.cover_url`: `https://www.metacritic.com/a/img/catalog` + `bucketPath` of the `cardImage`
  image (portrait box art), falling back to `mainImage`. The unsigned URL returns 200. `/a/img/resize/...`
  URLs are signed and return 403 if built by hand.
- `Game.video_url`: `item.video.embedUrl` (JW Player page). Not always a trailer; can be `null`.
- `PlatformScore.platform`: `item.platforms[].slug` (`playstation-5`, `pc`, …). Scores come from the
  two stats endpoints. The game payload only has per-platform critic scores, they are often `null`,
  and it has no user scores at all.

## Caveats

- **Order is not stable within a release date.** `sortBy=-releaseDate` has no tiebreaker, and 132
  games are dated today. Each distinct query string returns those ties in its own order, so
  consecutive offset pages overlap and skip games: 4 pages of 50 gave 191 unique slugs out of 200.
  The site's own HTML pages have the same problem. The crawler must dedupe on slug (that is what
  `CrawlState.processed_slugs` is for) and keep reading pages until it has 20 slugs it hasn't
  processed today.
- **Caching and limits.** Responses go through Cloudflare and Google (`cf-cache-status: HIT`,
  `age` up to a few minutes). No rate-limit headers were seen. 20 sequential uncached game
  requests all returned 200 in about 0.4 s each.
- **robots.txt.** `backend.metacritic.com` has none (404). The `*` group on `www` only disallows
  `/search`, `/signup`, `/login`, `/user`, so `/game` and `/browse` are allowed.
- **Politeness.** Use a descriptive User-Agent, roughly 1 request per second, timeouts, and backoff
  on 429 and 5xx responses.

## Request budget per run

Each run makes 1–2 finder calls. Each of its 20 games needs 1 details call, 2 stats calls per
platform, and the first page of critic and user reviews. With two platforms on average that is
about 140 requests, roughly 2.5 minutes at 1 request per second, well within the hourly schedule.
