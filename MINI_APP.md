# OPS Bot Mini App

The first release covers SKED, Flight Card, and Preparation. Leon supplies the UTC
schedule, crew, duty, and handling. ForeFlight supplies the plan and briefing. WX
shows the latest reports available from Leon and AWC.

## Run

1. Install the Python requirements using the existing virtual environment. In
   `frontend`, run `npm ci` and `npm run build` with Node.js 22.12 or newer.
2. Use the same `.env` and SQLite database as the polling bot so both processes
   share Telegram access and source configuration.
3. Start FastAPI from the repo root.

   ```powershell
   if ($env:DEBUG -eq 'release') { Remove-Item Env:DEBUG }
   .\.venv\Scripts\python.exe -m uvicorn api.main:app --host 127.0.0.1 --port 8000
   ```

4. Serve that process at the root of an HTTPS origin. The origin must reach the
   Mini App, its assets, and `/api/v1/` on the same host.
5. Set `TELEGRAM_MINI_APP_URL` to that HTTPS URL and start the bot through
   `main.py`. Open **SKED** from the menu in the private bot chat. The menu can
   also be set in BotFather when the polling bot is already running.

Telegram launches expire after one hour. Reopen SKED to start a fresh session.
The API checks the OPS Bot access list on each request. An ordinary browser
opening the URL displays the Telegram launch instructions.

The API contract is served at `/api/openapi.json`. The Telegram auth adapter
guards the HTTP routes. The operational read models live in
`application/flight_views.py` and call the shared Leon, ForeFlight, and weather
services.
