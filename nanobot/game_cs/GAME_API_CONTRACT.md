# GAME API Contract

This document defines the game backend API contract required by `nanobot.game_cs.service`.

## Auth

- Header: `Authorization: Bearer <GAME_CS_GAME_API_TOKEN>`
- Content-Type: `application/json`

## Verify Role

- Method: `POST`
- Path: `/api/game/verify_role`

Request body:

```json
{
  "game_name": "string",
  "area_name": "string",
  "role_name": "string"
}
```

Success response (`200`):

```json
{
  "success": true,
  "role_id": "string",
  "error": null
}
```

Role-not-found response (`404`):

```json
{
  "success": false,
  "role_id": null,
  "error_code": "ROLE_NOT_FOUND",
  "error": "string"
}
```

## Bind User

- Method: `POST`
- Path: `/api/user/bind`

Request body:

```json
{
  "user_id": "string",
  "game_name": "string",
  "area_name": "string",
  "role_name": "string",
  "role_id": "string or null"
}
```

Success response (`200`):

```json
{
  "success": true,
  "bind_id": "string",
  "error": null
}
```

Invalid params (`400`):

```json
{
  "success": false,
  "error_code": "INVALID_PARAMS",
  "error": "string"
}
```

Already bound (`409`):

```json
{
  "success": false,
  "error_code": "ALREADY_BOUND",
  "error": "string"
}
```
