# MCP-Cross_System_Update_Agent
A set of MCP servers that let an AI agent apply one business change across several systems, with entity-level locking, rollback and a verified audit trail.

Designed to work with LLMs via the MCP (Model Context Protocol). Tested with Claude Code as the host.

## How It Works

1. **Request**: The user asks in plain English, for example "Vendor 1042 has a new bank account. Update everything and pause payments until finance approves."
2. **Discovery**: The host connects to each MCP server and lists its tools.
3. **Lock**: The Coordinator locks the vendor and returns a change ID.
4. **Apply**: The agent calls write tools on the ERP, Payments, CRM and Notify servers, each with the change ID.
5. **Approve**: Finance approves before held payments are released.
6. **Commit or rollback**: The change is committed, or every completed step is undone in reverse order.
7. **Audit**: Every write is logged with the verified user, the change ID and the before and after values.

## MCP Servers

| Server | System | Purpose |
|--------|--------|---------|
| Coordinator | Redis + PostgreSQL | Locks, change log, rollback, audit |
| ERP | Odoo Community | Vendor master data |
| Payments | FastAPI + PostgreSQL | Payment holds and releases |
| CRM | HubSpot (free) | Vendor company records |
| Notify | Zammad + Slack | Verification tickets and announcements |

## Available Tools

| Tool | Server | Description | Parameters |
|------|--------|-------------|------------|
| `begin_change` | Coordinator | Lock an entity and start a change | `entity`, `reason` |
| `commit_change` | Coordinator | Complete a change and release the lock | `change_id` |
| `rollback_change` | Coordinator | Undo all completed steps | `change_id` |
| `get_audit_trail` | Coordinator | Show the history of an entity | `entity` |
| `find_vendor` | ERP | Search vendors by name | `query`, `limit` |
| `get_vendor` | ERP | Get full vendor details | `vendor_id` |
| `update_vendor_bank` | ERP | Change vendor bank details | `change_id`, `vendor_id`, `bank_details` |
| `list_pending_payments` | Payments | List unpaid payments for a vendor | `vendor_id` |
| `hold_payments` | Payments | Put pending payments on hold | `change_id`, `vendor_id` |
| `release_payments` | Payments | Release held payments | `change_id`, `vendor_id` |
| `get_company` | CRM | Get the vendor's CRM record | `vendor_id` |
| `update_company` | CRM | Update the vendor's CRM record | `change_id`, `vendor_id`, `fields` |
| `create_ticket` | Notify | Open a verification ticket | `change_id`, `title`, `body` |
| `post_message` | Notify | Post to a Slack channel | `change_id`, `channel`, `text` |

All write tools require a `change_id`, check the lock, save the before value and write an audit row.

## LLM MCP Configuration

Planned configuration (ports may change):

```
{
  "mcpServers": {
    "coordinator": { "url": "http://localhost:8001/mcp", "transport": "http" },
    "erp":         { "url": "http://localhost:8002/mcp", "transport": "http" },
    "payments":    { "url": "http://localhost:8003/mcp", "transport": "http" },
    "crm":         { "url": "http://localhost:8004/mcp", "transport": "http" },
    "notify":      { "url": "http://localhost:8005/mcp", "transport": "http" }
  }
}
```

Each server requires a Keycloak access token.

## Local Development

Instructions will be added as each server is built. Everything runs locally with Docker Compose.

## Project Structure

```
cross-system-agent/
├── servers/
│   ├── coordinator/      # Locks, change log, rollback, audit
│   ├── erp/              # Odoo vendor tools
│   ├── payments/         # Payments service tools
│   ├── crm/              # HubSpot tools
│   └── notify/           # Zammad and Slack tools
├── services/
│   └── payments_api/     # FastAPI + PostgreSQL payments service
├── eval/                 # Test requests and evaluation scripts
├── docker-compose.yml    # Odoo, PostgreSQL, Redis, Keycloak, Zammad, Jaeger
└── README.md
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `ODOO_URL` | `http://localhost:8069` | Odoo instance |
| `ODOO_API_KEY` | | Odoo API key |
| `HUBSPOT_TOKEN` | | HubSpot private app token |
| `ZAMMAD_TOKEN` | | Zammad API token |
| `SLACK_BOT_TOKEN` | | Slack bot token |
| `KEYCLOAK_URL` | `http://localhost:8080` | Keycloak instance |
| `REDIS_URL` | `redis://localhost:6379` | Lock store |
| `DATABASE_URL` | | PostgreSQL for change log and audit |

## Notes

- All systems are free: open source in Docker, or free-tier accounts (HubSpot, Slack).
- Odoo, HubSpot and Zammad do not accept Keycloak tokens. For them, identity is enforced at the MCP layer and recorded in the audit log.
- The full token-exchange chain applies end to end only to the payments service.
- Some actions cannot be undone (a Slack message); a correction is posted instead.
- For learning and portfolio use, not production.
