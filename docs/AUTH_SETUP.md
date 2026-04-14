# Google OAuth Authentication Setup

## Overview

The auth system implements a 3-tier access model:

| Tier | Role | Access |
|------|------|--------|
| **admin** | `ADMIN_EMAILS` | Full access to all features |
| **guest** | `GUEST_EMAILS` | Read-only access to all websites and dashboards |
| **stranger** | Everyone else | Redirected to login |

Sessions are JWT-based with configurable expiry. No passwords are stored; authentication is delegated entirely to Google.

---

## Prerequisites

- Google Cloud account
- Domain (real or ngrok) for OAuth callback URI

---

## Google Cloud Console Setup

### 1. Create or Select Project

1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Click **Select a Project** → New Project or existing one
3. Note your Project ID

### 2. Enable Google OAuth

1. Go to **APIs & Services** → **Library**
2. Search "Google OAuth"
3. Select **Google OAuth 2.0 API** → **Enable**

### 3. Configure OAuth Consent Screen

1. Go to **APIs & Services** → **OAuth consent screen**
2. Choose **External** user type → **Create**
3. Fill in:
   - App name
   - User support email
   - Developer contact email
4. Add scopes:
   - `email`
   - `profile`
   - `openid`
5. Under **Test users**: Add Google accounts that can authenticate before app verification (required for unverified apps)
6. Save and continue through remaining pages

### 4. Create OAuth 2.0 Credentials

1. Go to **APIs & Services** → **Credentials**
2. Click **Create Credentials** → **OAuth client ID**
3. Application type: **Web application**
4. Add authorized redirect URI:
   ```
   https://your-domain.com/auth/callback
   ```
5. Click **Create**
6. Copy the **Client ID** and **Client Secret**

---

## Environment Variables

Add these to your `.env` file:

```env
# Required
GOOGLE_CLIENT_ID=your-client-id.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=your-client-secret

# Access control (comma-separated emails)
ADMIN_EMAILS=admin@example.com,another@admin.com
GUEST_EMAILS=guest@example.com

# Optional (auto-generated if blank)
JWT_SECRET_KEY=your-256-bit-secret

# Optional (default: 24)
JWT_EXPIRY_HOURS=24
```

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `GOOGLE_CLIENT_ID` | Yes | — | OAuth client ID from Google Cloud |
| `GOOGLE_CLIENT_SECRET` | Yes | — | OAuth client secret |
| `ADMIN_EMAILS` | Yes | — | Comma-separated emails with admin access |
| `GUEST_EMAILS` | Yes | — | Comma-separated emails with read-only access |
| `JWT_SECRET_KEY` | No | Auto-generated | 256-bit secret for signing JWTs |
| `JWT_EXPIRY_HOURS` | No | `24` | JWT token lifetime |

---

## Testing Locally

### 1. Start the App

```bash
uv run uvicorn seo_indexing_tracker.main:app --reload --host 0.0.0.0 --port 8000
```

### 2. Set Up ngrok for OAuth Callback

```bash
ngrok http 8000
```

Copy the HTTPS URL (e.g., `https://abc123.ngrok.io`).

### 3. Configure Google Cloud

Add the ngrok callback URI in Google Cloud Console:

```
https://abc123.ngrok.io/auth/callback
```

Add your ngrok email as a test user if app is unverified.

### 4. Set Local Environment

```env
GOOGLE_CLIENT_ID=your-client-id
GOOGLE_CLIENT_SECRET=your-client-secret
ADMIN_EMAILS=your-google-email@gmail.com
GUEST_EMAILS=
JWT_SECRET_KEY=dev-secret-change-in-production
```

### 5. Test

1. Open `http://localhost:8000` in browser
2. Click login → Google OAuth flow
3. After login, you should see the dashboard

---

## Production Considerations

### HTTPS Required

Google OAuth only works with HTTPS redirect URIs in production. Options:

- Reverse proxy with TLS (nginx, Caddy, cloudflare Tunnel)
-托管 platform with built-in TLS (Railway, Render, Fly.io)

### Redirect URI Matching

The redirect URI in your code must match **exactly** what's configured in Google Cloud Console:

```
# Google Cloud Console
https://your-domain.com/auth/callback

# Must match exactly - no trailing slashes, same subdomain
```

### Cookie Security

In production, ensure:

- `SECRET_KEY` is a strong random value
- Cookies have `Secure`, `HttpOnly`, and `SameSite` attributes
- `JWT_SECRET_KEY` is distinct from `SECRET_KEY`

---

## Troubleshooting

### `redirect_uri_mismatch`

- Check the exact redirect URI in your code matches Google Cloud Console
- For local testing, ensure ngrok HTTPS URL is configured (not HTTP)
- Remove trailing slashes from URIs

### `access_denied`

- Your account is not in `ADMIN_EMAILS` or `GUEST_EMAILS`
- For unverified apps, you must be added as a test user in OAuth consent screen settings

### `invalid_client`

- `GOOGLE_CLIENT_ID` or `GOOGLE_CLIENT_SECRET` is incorrect
- Client ID format: `*.apps.googleusercontent.com`
- Ensure no extra spaces or newlines in env values

### App Not Verified

- All OAuth apps require verification for >100 users
- Use test users to bypass during development
- Add test users in: **Google Cloud Console** → **APIs & Services** → **OAuth consent screen** → **Test users**

### JWT Errors

- Clear browser cookies and log in again
- Verify `JWT_SECRET_KEY` hasn't changed between restarts
- Check that `JWT_EXPIRY_HOURS` hasn't expired
