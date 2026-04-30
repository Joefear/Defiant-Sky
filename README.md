# Space Domain Awareness Visualization

Local FastAPI and CesiumJS app for viewing current LEO satellite positions from Space-Track.org TLE data.

## Setup

1. Create a Python virtual environment.

   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   ```

2. Install server dependencies.

   ```powershell
   pip install -r server/requirements.txt
   ```

3. Create a `.env` file from `.env.example` and add your Space-Track.org credentials.

   ```text
   SPACETRACK_USER=your_email@example.com
   SPACETRACK_PASS=your_password
   ```

## Run

Start the API from the repository root:

```powershell
uvicorn server.main:app --reload
```

Open `client/index.html` in a browser. The static page calls `http://localhost:8000/api/tle`, propagates each TLE with satellite.js, and updates rendered satellite positions every 2 seconds.

## Data

The backend logs in to Space-Track.org, requests the latest 50 active LEO general perturbations records, and serves them from:

```text
GET /api/tle
```

The endpoint returns:

```json
[
  {
    "name": "SATELLITE NAME",
    "tle1": "1 ...",
    "tle2": "2 ..."
  }
]
```

Fetched TLE data is cached at `data/tle_cache.json` and refreshed every 10 minutes while the FastAPI app is running.
