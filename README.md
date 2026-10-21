# Listing Hub

A listing collection/search pipeline with local storage, exports and an optional dashboard.

**Built with:** Python · SQLite · CLI · HTML

## What it includes

- A source adapter, response normalisation and deduplicated SQLite storage.
- Search, price/attribute filters and CSV/JSON exports.
- An offline test suite covering decoding, storage and the query/API layer.

## Project status

50 offline checks passed on 21 October 2026. G2G is the one implemented source. Live collection was not tested and may depend on the current source site and optional fetcher support. Authorship was confirmed by Hannan Noor.

## Folder guide

| Folder | Contents |
| --- | --- |
| [`src/`](src/) | Original implementation, assets and project configuration |
| [`docs/`](docs/) | Available specifications, proposals, reports, diagrams and project context |

## Run or open

The core pipeline uses Python. The original README in `src/` explains optional fetcher/browser support and configuration.

```text
cd src
python test_pipeline.py
python find.py
```

The included database lets the query tool show existing data without performing a live scrape.

## Supporting documents

| File | Format |
| --- | --- |
| [project-context.md](docs/project-context.md) | MD |

## Notes

API keys and demonstration passwords detected in the portfolio copies use
placeholders. Configure your own services before running integrations. Original
source credits and bundled third-party licences remain with their files.
