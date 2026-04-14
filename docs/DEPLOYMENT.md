# CapRover Deployment Guide

## Prerequisites
- CapRover server installed and running
- A domain/subdomain pointed to your CapRover instance

## Deployment Method
This application uses **git-based deployment** where pushing to the `main` branch triggers an automatic build and deploy on CapRover.

## Persistent Volume Mounts

CapRover persistent storage is required for:

| Volume Name | Host Path | Container Path | Purpose |
|-------------|-----------|----------------|---------|
| `seo-data` | `/caprover-tasks/volume.s seo-indexing-tracker-data` | `/app/data` | SQLite databases, logs |
| `seo-credentials` | `/caprover-tasks/volume.seo-indexing-tracker-credentials` | `/app/credentials` | Google service account JSON keys |

**Important:** The service account credentials must be uploaded to the `seo-credentials` volume and their paths stored in the database must match the container paths.

## captains-definition File

The project includes a `captains-definition` file for CapRover git-based deployment.

## Environment Variables

Configure these in CapRover's app configuration (Config -> Environment Variables):

| Variable | Value | Purpose |
|----------|-------|---------|
| `DATABASE_URL` | `sqlite+aiosqlite:///./data/seo_indexing_tracker.db` | Database location |
| `SCHEDULER_JOBSTORE_URL` | `sqlite:///./data/scheduler-jobs.sqlite` | Scheduler job storage |
| `LOG_FILE` | `./data/app.log` | Log file location |
| `SECRET_KEY` | `<generate-a-secure-random-string>` | Application secret |
| `HOST` | `0.0.0.0` | Server bind address |
| `PORT` | `8000` | Server port |
| `SCHEDULER_ENABLED` | `true` | Enable background jobs |
| `SCHEDULER_URL_SUBMISSION_INTERVAL_SECONDS` | `300` | URL submission frequency |
| `SCHEDULER_INDEX_VERIFICATION_INTERVAL_SECONDS` | `900` | Verification frequency |
| `SCHEDULER_SITEMAP_REFRESH_INTERVAL_SECONDS` | `3600` | Sitemap refresh frequency |
| `INDEXING_DAILY_QUOTA_LIMIT` | `200` | Indexing API daily limit |
| `INSPECTION_DAILY_QUOTA_LIMIT` | `2000` | Inspection API daily limit |
| `OUTBOUND_HTTP_USER_AGENT` | `BlueBeastBuildAgent` | HTTP User-Agent |
| `GOOGLE_CLIENT_ID` | `your-client-id.apps.googleusercontent.com` | Google OAuth client ID |
| `GOOGLE_CLIENT_SECRET` | `your-client-secret` | Google OAuth client secret |
| `ADMIN_EMAILS` | `admin@example.com` | Comma-separated admin emails |
| `GUEST_EMAILS` | `guest@example.com` | Comma-separated guest emails |
| `JWT_SECRET_KEY` | `<generate-random>` | 256-bit secret for JWT signing |
| `JWT_EXPIRY_HOURS` | `168` | JWT lifetime (168h = 7 days recommended for production) |

For Google OAuth setup, see [AUTH_SETUP.md](./AUTH_SETUP.md).

## CapRover Setup Steps

### 1. Create the App
In CapRover dashboard:
- Go to **Apps** → **Create New App**
- Name it `seo-indexing-tracker`
- Select **HTTP** (this app has its own web server)

### 2. Configure Git Deployment
- Go to **Deployment** → **Deploy via Git**
- Connect your git repository
- Set branch to `main`
- CapRover will auto-deploy on push to main

### 3. Set Persistent Volumes
Go to **Config** → **Persistent Volumes**:

Click **Add New Volume** for each:

1. **Volume A**:
   - Name: `seo-data`
   - Container Path: `/app/data`

2. **Volume B**:
   - Name: `seo-credentials`
   - Container Path: `/app/credentials`

### 4. Set Environment Variables
Go to **Config** → **Environment Variables** and add all variables from the table above.

### 5. Configure Health Check
Go to **App Config** → **HTTP Settings**:
- Health Check: `http://your-domain:8000/health`
- Health Check Period: `30`

### 6. Enable Websocket Support (for HTMX)
Go to **App Config** → **Enable Websocket** → **Save**

### 7. Deploy
Push to main branch:
```bash
git push origin main
```

Watch the deployment logs in CapRover dashboard.

## Service Account Credentials Setup

1. Create your Google service account JSON key files
2. Upload them to the `seo-credentials` volume:
   ```bash
   # Using CapRover's terminal or SSH
   docker cp service-account.json caprover_seo-indexing-tracker:/app/credentials/
   ```
3. When adding a service account in the web UI, use the path:
   ```
   /app/credentials/service-account.json
   ```

## First-Time Setup After Deploy

1. Access the application at `http://seo-indexing-tracker.your-domain.com`
2. Go to **Websites** → **Add Website** to add your first tracked website
3. Upload service account credentials
4. Add sitemaps
5. Click **Trigger Indexing** to start

## Updating the Application

Simply push to main:
```bash
git add .
git commit -m "Update message"
git push origin main
```

CapRover will automatically rebuild and deploy.

## Troubleshooting

### App won't start
- Check logs in CapRover dashboard (App Logs)
- Verify environment variables are set correctly
- Ensure persistent volumes are mounted

### Database errors
- Check that `/app/data` volume is mounted and writable
- Verify file permissions: `ls -la /caprover-tasks/volume.seo-indexing-tracker-data/`

### Service account errors
- Verify credentials are in `/app/credentials`
- Check path matches what you entered in the web UI

## Docker Compose Reference (Alternative)

If running standalone with docker-compose:
```yaml
version: '3.8'
services:
  seo-indexing-tracker:
    build: .
    ports:
      - "8000:8000"
    volumes:
      - seo-data:/app/data
      - seo-credentials:/app/credentials
    environment:
      - DATABASE_URL=sqlite+aiosqlite:///./data/seo_indexing_tracker.db
      - SECRET_KEY=your-secret-key
    restart: unless-stopped

volumes:
  seo-data:
  seo-credentials:
```