# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**London Noivas** — a wedding dress rental management system for a boutique in Manaus, Brazil. Built with Flask (Python) + DynamoDB (AWS). The UI is in Brazilian Portuguese.

## Development Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Run locally (Flask dev server)
python app.py

# Run tests
pytest

# Build and run with Docker
docker build -t london-noivas .
docker run -p 80:80 london-noivas
```

Production runs via Gunicorn: `gunicorn app:app --bind 0.0.0.0:80 --workers 2 --timeout 120`

## Architecture

### Entry Point & Route Registration

`app.py` is the main entry point. It initializes the Flask app, creates AWS clients (DynamoDB, S3, SES), and registers routes by calling `init_*_routes()` functions from each module — passing AWS clients as dependencies rather than using globals.

```python
# Pattern used throughout
init_item_routes(app, items_table, clients_table, s3, ...)
init_fittings_routes(app, fittings_table, clients_table, ...)
```

### Route Modules

| File | Responsibility |
|------|---------------|
| `auth.py` | Login, register, password reset, session management |
| `item_routes.py` | Item CRUD, inventory, image upload, QR code generation |
| `client_routes.py` | Client management |
| `transaction_routes.py` | Rental transaction creation/editing |
| `status_routes.py` | Status transitions (rented → returned → archived) |
| `fittings_routes.py` | Fitting/prova appointment scheduling |
| `static_routes.py` | Public pages, catalog, contact form |
| `schedul_example/public_scheduling_routes.py` | Public self-booking |

### Data Layer (DynamoDB)

All data is stored in DynamoDB. Key tables:
- `alugueqqc_itens` — inventory items
- `alugueqqc_clients` — clients
- `alugueqqc_transactions` — rental records
- `alugueqqc_fittings_table` — fitting appointments
- `alugueqqc_users` — user accounts
- `alugueqqc_payment_transactions` — Stripe payment records
- `alugue_qqc_text_models` — email/document templates
- `alugueqqc_scheduling_config_table` — scheduling settings

Pagination uses DynamoDB's `ExclusiveStartKey` cursor pattern. Filtering is applied post-scan with Python logic.

### Schemas (`schemas.py`)

Defines field metadata for Item, Client, and Transaction entities — which fields are filterable, previewable, and their display order. Items and clients support dynamic custom fields via a `key_values` dict.

### Authentication

Session-based auth. Session stores: `user_id`, `logged_in`, `role`, `account_id`, `timezone`. Role-based access control distinguishes admin from regular users. Default timezone: `America/Sao_Paulo`.

### Images

Item images are uploaded to S3 (`alugueqqc-images` bucket) and served via CloudFront CDN (`CLOUDFRONT_DOMAIN`). The `utils.py` file contains image validation and S3 helpers.

### Templates

66 Jinja2 HTML templates in `templates/`. `base.html` is the layout base. Reusable components are in `templates/components/`. The `schedul_example/templates/` directory has additional templates for the public scheduling module, loaded via a custom Jinja2 loader configured in `app.py`.

## Key External Integrations

- **AWS**: DynamoDB, S3, SES (email), CloudFront
- **Stripe**: Subscriptions/payments; webhook at `/webhook/stripe`. Toggled via `USE_STRIPE` env var.
- **OpenAI + Langfuse**: AI features with LLM observability tracing
- **Google reCAPTCHA**: Form protection on public-facing forms

## Environment Variables

Key `.env` variables (no `.env.example` exists — refer to `.env` directly, but never commit it):

```
AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_REGION
S3_BUCKET_NAME, CLOUDFRONT_DOMAIN
SES_SENDER
STRIPE_PUBLIC_KEY, STRIPE_SECRET_KEY, STRIPE_PRICE_ID, STRIPE_WEBHOOK_SECRET
OPENAI_API_KEY
LANGFUSE_BASE_URL, LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY
RECAPTCHA_SITE_KEY, RECAPTCHA_SECRET_KEY
USE_STRIPE, ALLOW_USER_REGISTRATION, debug_env
```
