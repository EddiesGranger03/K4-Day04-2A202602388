---
name: ticket_status_lookup
track: bonus
kind: local_inventory
provider: local_ticket_store
requires_env: []
inputs: [ticket_id]
outputs: [status, ticket_id, summary, priority, asset_id, created_at, source]
side_effect: false
---
# ticket_status_lookup

Looks up the status and details of a previously created helpdesk ticket by its
ticket ID. The ticket must exist in the local `tickets/` directory. This is a
read-only tool with no side effects.

Input must be a valid ticket ID in the format LAB-XXXXXXXX (8 hex characters).
Returns the full ticket record if found, or an error if the ticket does not exist
or the ID format is invalid. Credential-like content found in ticket summaries is
redacted before being returned.
