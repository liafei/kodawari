# PRD: bookmark REST API

## 目标 / Goals

A minimal FastAPI service `bookmark` exposing 4 HTTP endpoints for adding,
listing, searching, and deleting bookmarks. SQLite-backed (single file),
zero external dependencies. Designed as a kodawari hello-world demo.

## 范围 / Scope

HTTP endpoints (FastAPI app at `app/main.py`):

- `POST /bookmarks` — Create. Body `{url, tag, note}`. `url` required,
  `tag`/`note` optional. Response 201 + `{id, url, tag, note, created_at}`.
- `GET /bookmarks` — List. Optional query `tag` filters by tag. Response
  200 + `[{id, url, tag, note, created_at}, ...]`.
- `GET /bookmarks/search?q=<keyword>` — Search. Case-insensitive substring
  match across `url` and `note`. Response 200 + `[{...}]`.
- `DELETE /bookmarks/{id}` — Delete. Hit → 204; missing → 404 +
  `{detail: "not found"}`.

Storage: SQLite. Path from env `BOOKMARK_DB_PATH` (default `./bookmark.db`).
Schema auto-created on first startup if absent.

## 数据契约 / Data Contract

source of truth: `db.bookmarks` table.

schema:
- `bookmarks(id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT NOT NULL,
  tag TEXT, note TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)`

不变量 / Invariants:
- `url` is required. Missing or empty → 400 `{detail: "url required"}`.
- Search is case-insensitive (SQL uses LOWER).
- Delete of non-existent id → 404.

## 分层 / Layers

- route 层：`app/main.py` (FastAPI app + 4 endpoints + 400/404 error handling)
- service 层：`app/service.py` (business logic: add/list/search/delete)
- repository 层：`app/repository.py` (SQLite SQL + schema migration)

## 不在范围 / Out of scope

- No auth / multi-user (every request allowed)
- No CORS / rate limiting
- No import/export / web UI
- No frontend
- No pagination (list returns all rows)

## 测试 / Tests

`tests/test_api.py` using `fastapi.testclient`. At least one happy path
+ one edge case per endpoint:
- POST happy + POST missing url → 400
- GET empty list + GET filtered by tag
- search hit + search case-insensitive
- DELETE hit + DELETE missing id → 404

Verify command: `pytest tests/test_api.py -q`

## Acceptance Criteria

- All endpoints return JSON matching the contract shapes above.
- `pytest tests/test_api.py -q` exits 0 on a fresh install.
- Service starts without any external resource beyond the SQLite file.
