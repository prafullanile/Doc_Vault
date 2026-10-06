# ADR-0003: Short-lived JWT access tokens and rotating opaque refresh tokens

**Status:** Accepted · Phase 1

## Decision
- **Access token:** an HS256 JWT with a 15-minute lifetime. Claims are `sub`, `org` (the active
  tenant), `role`, `type=access`, `iss`, `aud`, `exp` and `jti`. Decoding pins the algorithm,
  issuer and audience.
- **Authorization reads the database on every request.** The token proves who the user is and
  which organization they selected, while the role comes from `memberships`. A demoted or
  removed member loses access immediately instead of when the token expires. This costs one
  indexed lookup per request; Redis can cache it in Phase 7.
- **Refresh token:** 256 random bits, stored only as a SHA-256 hash.
  - It rotates on every use.
  - Tokens descended from one login share a `family_id`. If a rotated token is presented
    again, the token has leaked, so the whole family is revoked.
  - Refresh takes `SELECT … FOR UPDATE`, so two concurrent refreshes of the same token rotate
    it exactly once.
- **Logout** revokes the family. Removing a member revokes their refresh tokens for that
  organization.
- **Passwords:** Argon2id, run off the event loop. Unknown emails still run a dummy hash, so
  response timing doesn't reveal which emails are registered.

## Consequences
- No Redis or deny-list is needed in Phase 1. Access tokens can't be revoked individually, but
  every request rechecks membership, which covers the important case.
- A client that fires two refreshes in parallel with the same token gets logged out. That is
  the cost of strict reuse detection; a short grace window can be added if it becomes a
  problem.
